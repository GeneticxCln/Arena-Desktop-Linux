"""Regression tests covering the full BUG_AUDIT.md Critical + Medium + Low sweep."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from arena import agent, api, config, connectors, contracts, crypto, history_sync, models, permissions
from arena.engine import BrowserEngine
from arena.providers.antigravity import AntigravityProvider
from arena.providers.claude import ClaudeProvider
from arena.providers.deepseek import DeepSeekProvider
from arena.providers.kimi import KimiProvider
from arena.providers.minimax import MiniMaxProvider
from arena.providers.web_engine import WebChatEngine
from arena.tools import ToolRegistry, parse_tool_calls as cli_parse_tool_calls
from arena.tools.browser_mcp import BrowserExecJsTool, BrowserOpenUrlTool
from arena.tools.file_tools import ReadFileTool, WriteFileTool
from arena.ui.markdown import print_tool_call, print_tool_result
from arena.web import app as web


# ---------------------------------------------------------------------------
# Critical #1: GET / boot-nonce protection in desktop mode
# ---------------------------------------------------------------------------

def test_index_requires_boot_nonce_when_set(monkeypatch):
    client = web.app.test_client()
    monkeypatch.setattr(web, "_BOOT_NONCE", "secret-boot-nonce-123")
    # Unauthenticated local process scraping GET / gets 403 and never sees token
    r_bad = client.get("/")
    assert r_bad.status_code == 403
    assert web._API_TOKEN.encode() not in r_bad.data
    # Desktop window bootstrapping with ?boot=<nonce> receives HTML + HttpOnly cookie
    r_ok = client.get("/?boot=secret-boot-nonce-123")
    assert r_ok.status_code == 200
    assert web._API_TOKEN.encode() in r_ok.data
    # Subsequent reload in the same window succeeds via the session cookie
    r_reload = client.get("/")
    assert r_reload.status_code == 200
    assert web._API_TOKEN.encode() in r_reload.data


# ---------------------------------------------------------------------------
# Critical #2: send_chat_via_cdp accepts mode & validates loopback CDP URL
# ---------------------------------------------------------------------------

def test_cdp_loopback_validation_and_send_chat_via_cdp(monkeypatch):
    assert api._is_loopback_ws_url("ws://127.0.0.1:9222/devtools/page/1") is True
    assert api._is_loopback_ws_url("ws://localhost:9222/devtools/page/1") is True
    assert api._is_loopback_ws_url("ws://evil.example:9222/devtools/page/1") is False
    assert api._is_loopback_ws_url("http://127.0.0.1:9222/") is False
    assert api._cdp_execute("ws://evil.example:9222/devtools/page/1", "Runtime.evaluate") is None

    captured = {}

    class _FakeWS:
        def send(self, raw):
            captured["msg"] = json.loads(raw)

        def recv(self):
            return json.dumps({"result": {"result": {"value": {"text": "ok", "evalId": "ev-1"}}}})

        def close(self):
            pass

    import sys
    monkeypatch.setitem(sys.modules, "websocket", SimpleNamespace(create_connection=lambda url, timeout=60: _FakeWS()))
    monkeypatch.setattr(api, "_cdp_ensure_arena_page", lambda port=9222: "ws://127.0.0.1:9222/devtools/page/1")
    text, eval_id, err = api.send_chat_via_cdp("m1", "hi", mode="battle")
    assert text == "ok" and eval_id == "ev-1" and err is None
    expr = captured["msg"]["params"]["expression"]
    assert "self.active_mode" not in expr
    assert '"battle"' in expr


# ---------------------------------------------------------------------------
# Critical #3 & #4: permissions Auto bypass, catastrophic rm, per-chat always
# ---------------------------------------------------------------------------

def test_permissions_auto_safe_cmd_blocks_bypasses():
    assert permissions.decide("auto", "run_cmd", True, "git status") == "allow"
    assert permissions.decide("auto", "run_cmd", True, "python -m pytest -q") == "allow"
    # Newline injection
    assert permissions.decide("auto", "run_cmd", True, "echo hi\nrm -rf /tmp/x") == "ask"
    # Git diff --output / --ext-diff / branch delete
    assert permissions.decide("auto", "run_cmd", True, "git diff --output=pwn.sh") == "ask"
    assert permissions.decide("auto", "run_cmd", True, "git diff --ext-diff") == "ask"
    assert permissions.decide("auto", "run_cmd", True, "git branch -D main") == "ask"
    # Reading sensitive paths outside workspace
    assert permissions.decide("auto", "run_cmd", True, "cat ~/.ssh/id_rsa") == "ask"
    assert permissions.decide("auto", "run_cmd", True, "cat /etc/passwd") == "ask"
    assert permissions.decide("auto", "run_cmd", True, "cat ../secrets.txt") == "ask"
    # Catastrophic split flags / $HOME / ~/
    assert permissions.decide("bypassPermissions", "run_cmd", True, "rm -r -f /") == "deny"
    assert permissions.decide("bypassPermissions", "run_cmd", True, "rm -rf ~/") == "deny"
    assert permissions.decide("bypassPermissions", "run_cmd", True, "rm -rf $HOME") == "deny"


def test_permission_broker_always_is_chat_scoped_and_never_applies_to_gateway():
    b = permissions.PermissionBroker()
    pid = b.ask({"tool": "write_file", "chat_id": "chat-a"})
    assert b.resolve(pid, "always") is True
    assert b.wait(pid, 0) == "allow"
    # Allowed in chat-a, NOT in chat-b, NOT for gateway
    assert b.check("default", "write_file", inside=True, chat_id="chat-a") == "allow"
    assert b.check("default", "write_file", inside=True, chat_id="chat-b") == "ask"
    assert b.check("default", "write_file", inside=True, chat_id="chat-a", gateway=True) == "ask"
    # Cancel cleans up pending entries
    pid2 = b.ask({"tool": "delete", "chat_id": "chat-a"})
    assert b.cancel(pid2) is True
    assert all(p["id"] != pid2 for p in b.pending())


# ---------------------------------------------------------------------------
# Critical #5 & #6: Protected .git/config, checkpoint validation, git safety
# ---------------------------------------------------------------------------

def test_local_tools_protects_git_config_hooks_and_checkpoints(tmp_path):
    lt = agent.LocalTools(tmp_path)
    _, inside_cfg = lt.resolve(".git/config", write=True)
    _, inside_hook = lt.resolve(".git/hooks/pre-commit", write=True)
    _, inside_ck = lt.resolve(".arena-artifacts/checkpoints/ck1/manifest.json", write=True)
    _, inside_src = lt.resolve("src/main.py", write=True)
    assert inside_cfg is False
    assert inside_hook is False
    assert inside_ck is False
    assert inside_src is True


def test_local_tools_delete_symlink_does_not_rmtree_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "precious.txt"
    victim.write_text("keep me", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    link = ws / "escape"
    link.symlink_to(outside)

    lt = agent.LocalTools(ws)
    ok, msg = lt.run({"name": "delete", "params": {"path": "escape"}})
    assert ok is True
    assert "symlink" in msg.lower()
    assert not link.exists() and not link.is_symlink()
    assert victim.exists() and victim.read_text(encoding="utf-8") == "keep me"


def test_checkpoint_restore_blocks_paths_outside_workspace_and_bad_store(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside_file = tmp_path / "pwned.txt"
    outside_file.write_text("original", encoding="utf-8")
    inside_file = ws / "ok.txt"
    inside_file.write_text("modified", encoding="utf-8")

    cid = "20261127-120000-abcd1234"
    ck_dir = ws / ".arena-artifacts" / "checkpoints" / cid
    (ck_dir / "files").mkdir(parents=True)
    (ck_dir / "files" / "f0").write_bytes(b"restored-ok")
    (ck_dir / "files" / "f1").write_bytes(b"EVIL")
    manifest = {
        "id": cid,
        "truncated": True,
        "entries": [
            {"abs": str(inside_file), "existed": True, "store": "f0"},
            {"abs": str(outside_file), "existed": True, "store": "f1"},
            {"abs": str(ws / "evil.txt"), "existed": True, "store": "../../../etc/passwd"},
            {"abs": str(ws / ".git" / "config"), "existed": True, "store": "f1"},
        ],
    }
    (ck_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(web, "_chosen_ws", lambda: ws)

    client = web.app.test_client()
    r = client.post(f"/api/checkpoints/{cid}/restore", json={})
    assert r.status_code == 200
    body = r.get_json()
    assert body["restored"] == 1
    assert body["skipped"] == 3
    assert body["truncated"] is True
    assert inside_file.read_text(encoding="utf-8") == "restored-ok"
    assert outside_file.read_text(encoding="utf-8") == "original"
    assert not (ws / ".git" / "config").exists()


# ---------------------------------------------------------------------------
# Critical #7: Gateway defaults to OFF
# ---------------------------------------------------------------------------

def test_gateway_defaults_disabled():
    assert web._gateway_enabled({}) is False
    assert web._gateway_enabled({"gateway_enabled": True}) is True


# ---------------------------------------------------------------------------
# Critical #8: Fenced code blocks ignored by tool parsers + grep -e allowed
# ---------------------------------------------------------------------------

def test_parse_tool_calls_ignores_fenced_and_inline_code():
    sample = (
        "Here is an example:\n"
        "```xml\n"
        '<tool name="delete"><path>README.md</path></tool>\n'
        "```\n"
        'Also inline `<tool name="run_cmd"><cmd>echo bad</cmd></tool>`.\n'
        '<tool name="ls"><path>.</path></tool>\n'
    )
    agent_calls = agent.parse_tool_calls(sample)
    assert [c["name"] for c in agent_calls] == ["ls"]
    cli_calls = cli_parse_tool_calls(sample)
    assert [c["name"] for c in cli_calls] == ["ls"]


def test_run_shell_command_allows_grep_e_and_blocks_python_c(tmp_path):
    (tmp_path / "a.txt").write_text("hello world\n", encoding="utf-8")
    out_grep = agent.run_shell_command("grep -e hello a.txt", tmp_path)
    assert "hello world" in out_grep
    out_py = agent.run_shell_command("python3 -c 'print(1)'", tmp_path)
    assert "blocked for safety" in out_py.lower()


# ---------------------------------------------------------------------------
# Critical #9: CLI tools boundary + Brave MCP cwd trust + BrowserExecJs gate
# ---------------------------------------------------------------------------

def test_cli_file_tools_enforce_workspace_boundary(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("top-secret", encoding="utf-8")

    rf = ReadFileTool(str(ws))
    wf = WriteFileTool(str(ws))
    assert "escapes workspace" in rf.execute({"path": "../secret.txt"}).lower()
    assert "escapes workspace" in wf.execute({"path": str(outside), "content": "x"}).lower()
    assert "protected git metadata" in wf.execute({"path": ".git/config", "content": "x"}).lower()
    assert "Successfully wrote" in wf.execute({"path": "ok.txt", "content": "hi"})
    assert "hi" in rf.execute({"path": "ok.txt"})


def test_cli_browser_tools_safe_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("ARENA_ALLOW_BROWSER_JS", raising=False)
    reg = ToolRegistry(str(tmp_path))
    assert "browser_exec_js" not in reg.tools
    res = BrowserExecJsTool(str(tmp_path)).execute({"script": "1+1"})
    assert "disabled by default" in res.lower()
    bad_url = BrowserOpenUrlTool(str(tmp_path)).execute({"url": "file:///etc/passwd"})
    assert "unsupported url scheme" in bad_url.lower()


def test_rich_markdown_escapes_brackets():
    # Must never raise rich.errors.MarkupError on raw [/] or [red] in output
    print_tool_call("run_cmd", {"cmd": "echo [/] [bold]"})
    print_tool_result("run_cmd", "raw output with [/] and [unknown_tag]")


# ---------------------------------------------------------------------------
# Critical #12: history_sync.merge_messages preserves duplicates & ISO sort
# ---------------------------------------------------------------------------

def test_merge_messages_preserves_duplicates_and_does_not_mutate():
    local = [{"role": "user", "text": "hi"}]
    remote = [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": "ok"},
        {"role": "user", "text": "continue"},
        {"role": "assistant", "text": "done 1"},
        {"role": "user", "text": "continue"},
        {"role": "assistant", "text": "done 2"},
    ]
    # Non-prefix case: local has an extra unshared entry
    local_div = [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "local note"}]
    snap = list(local_div)
    merged, added = history_sync.merge_messages(local_div, remote)
    assert local_div == snap  # no in-place mutation
    assert added == 5
    continue_msgs = [m for m in merged if m.get("text") == "continue"]
    assert len(continue_msgs) == 2


def test_history_sync_sorts_mixed_fractional_iso_timestamps():
    session = {
        "id": "s1",
        "messages": [
            {"role": "assistant", "content": "second", "createdAt": "2026-01-01T00:00:01Z"},
            {"role": "user", "content": "first", "createdAt": "2026-01-01T00:00:00.5Z"},
            {"role": "assistant", "content": "third", "createdAt": "2026-01-01T00:00:01.05Z"},
        ],
    }
    res = history_sync.session_to_local(session)
    # Consecutive assistant turns merge in chronological order: "second" before "third"
    # (lexicographical string sort would have put "01.05Z" before "01Z").
    assert [m["text"] for m in res["messages"]] == ["first", "second\n\nthird"]


# ---------------------------------------------------------------------------
# Critical #14: _resolve_delegate_model searches beyond first 14 models
# ---------------------------------------------------------------------------

def test_resolve_delegate_model_finds_model_beyond_14(monkeypatch):
    many = {f"model-{i:03d}" for i in range(30)}
    monkeypatch.setattr(web, "_allowed_model_ids", lambda mode, mod: many)
    monkeypatch.setattr(web, "_model_name", lambda mid: f"Name {mid}")
    mid, name, auto = web._resolve_delegate_model("model-025", "work")
    assert mid == "model-025" and name == "Name model-025" and auto is False


# ---------------------------------------------------------------------------
# Engine, Providers, Contracts, Models, Semver, URL Redaction, Media SSRF
# ---------------------------------------------------------------------------

def test_contracts_rate_limit_reset_handles_relative_and_ignores_remaining():
    now = time.time()
    # Small value is relative seconds
    rel = contracts._rate_limit_reset({"RateLimit-Reset": "120"})
    assert rel is not None and 115 <= (rel - now) <= 125
    # Large value is Unix epoch
    epoch = contracts._rate_limit_reset({"RateLimit-Reset": str(int(now + 3600))})
    assert epoch is not None and 3590 <= (epoch - now) <= 3610
    # RateLimit-Remaining is a count, NOT a reset duration
    assert contracts._rate_limit_reset({"RateLimit-Remaining": "5"}) is None


def test_version_tuple_semver_prerelease_ordering():
    assert web._version_tuple("7.4.0") > web._version_tuple("7.4.0-beta.1")
    assert web._version_tuple("7.4.1") > web._version_tuple("7.4.0")
    assert web._version_tuple("7.4.0-beta.2") > web._version_tuple("7.4.0-beta.1")


def test_redact_urls_strips_creds_port_and_query():
    line = "GET https://user:secret@example.com:8443/api/v1?token=abc#frag failed"
    redacted = web._redact_urls(line)
    assert "secret" not in redacted
    assert "token=abc" not in redacted
    assert "https://example.com:8443/api/v1" in redacted


def test_media_save_ssrf_blocks_private_ipv6_and_cgnat():
    assert web._is_public_https_url("https://127.0.0.1/a.png") is False
    assert web._is_public_https_url("https://[::1]/a.png") is False
    assert web._is_public_https_url("https://172.16.0.1/a.png") is False
    assert web._is_public_https_url("https://100.64.0.1/a.png") is False
    assert web._is_public_https_url("http://93.184.216.34/a.png") is False


def test_deepseek_fragments_sse_routing():
    ds = DeepSeekProvider()
    st = {}
    evs1 = ds._parse_observed_sse_chunk(
        'data: {"p": "response/fragments", "o": "SET", "v": [{"id": 1, "type": "THINK", "content": "hmm"}]}\n',
        st,
    )
    evs2 = ds._parse_observed_sse_chunk(
        'data: {"p": "response/fragments/0/content", "o": "APPEND", "v": " still thinking"}\n',
        st,
    )
    evs3 = ds._parse_observed_sse_chunk(
        'data: {"p": "response/fragments", "o": "APPEND", "v": [{"id": 2, "type": "RESPONSE", "content": "answer"}]}\n',
        st,
    )
    assert evs1 == [{"type": "reasoning", "text": "hmm"}]
    assert evs2 == [{"type": "reasoning", "text": " still thinking"}]
    assert evs3 == [{"type": "text", "text": "answer"}]
    assert "HWWAFSESID" not in ds.auth_cookie_names


def test_kimi_connect_incremental_json_without_newline():
    kimi = KimiProvider()
    st = {}
    evs = kimi._parse_observed_sse_chunk(
        '\x00\x00\x00\x20{"block":{"text":{"content":"hello"}}}',
        st,
        flush=False,
    )
    assert evs == [{"type": "text", "text": "hello"}]


def test_claude_stream_does_not_swallow_midstream_error_or_empty(monkeypatch):
    cl = ClaudeProvider()
    cl._org_id = "org-1"

    def fake_stream_err(*a, **k):
        yield {"event": "status", "status": 200, "ok": True}
        yield {"event": "chunk", "data": 'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"partial"}}\n'}
        yield {"event": "error", "error": "connection reset"}

    monkeypatch.setattr(cl, "iter_fetch_stream", fake_stream_err)
    evs = list(cl._stream_turn_api("c1", "hi", conversation_url="https://claude.ai/chat/11111111-1111-1111-1111-111111111111"))
    assert any(e.get("type") == "text" and e.get("text") == "partial" for e in evs)
    assert any(e.get("type") == "error" for e in evs)

    def fake_stream_empty(*a, **k):
        yield {"event": "status", "status": 200, "ok": True}
        yield {"event": "end"}

    monkeypatch.setattr(cl, "iter_fetch_stream", fake_stream_empty)
    evs2 = list(cl._stream_turn_api("c1", "hi", conversation_url="https://claude.ai/chat/11111111-1111-1111-1111-111111111111"))
    assert any(e.get("type") == "error" and e.get("code") == "claude_empty" for e in evs2)


def test_engine_abort_all_streams_wakes_waiters_and_probe_id_filters_stale():
    eng = BrowserEngine()
    q_box = []
    import queue
    q = queue.Queue()
    eng._streams["r1"] = {"q": q, "next": 0, "held": {}}
    eng._abort_all_streams("navigated")
    assert q.get_nowait() == ("error", "navigated")
    assert not eng._streams

    # Stale probeId is ignored
    dq = queue.Queue()
    eng._drive_queue = dq
    eng._drive_probe_id = "probe-new"
    eng._on_drive_event("vote", json.dumps({"probeId": "probe-old", "ok": False}))
    assert dq.empty()
    eng._on_drive_event("vote", json.dumps({"probeId": "probe-new", "ok": True}))
    assert dq.get_nowait()[1]["ok"] is True


def test_models_sync_does_not_clobber_cache_with_empty(tmp_path, monkeypatch):
    cache = tmp_path / "models_cache.json"
    cache.write_text(json.dumps({"_cached_at": 0, "total": 1, "models": [{"id": "m1", "name": "M1"}]}), encoding="utf-8")
    monkeypatch.setattr(models, "CACHE_FILE", cache)

    class _FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"arenas": []}

    monkeypatch.setattr(models.requests, "get", lambda *a, **k: _FakeResp())
    got = models.sync_models(force=True)
    assert len(got) == 1 and got[0]["id"] == "m1"
    # Existing cache on disk was preserved
    assert json.loads(cache.read_text(encoding="utf-8"))["total"] == 1


def test_settings_export_strips_mcp_env_and_import_sanitizes_bypass(tmp_path, monkeypatch):
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {"srv": {"command": "echo", "env": {"API_KEY": "super-secret"}}}}), encoding="utf-8")
    monkeypatch.setattr(connectors, "_config_path", lambda p=None: mcp_path)
    bundle = web._settings_bundle(include_secrets=False)
    assert bundle["mcp"]["mcpServers"]["srv"]["env"]["API_KEY"] == ""
