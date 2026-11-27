"""Regression tests for the full codebase bug & security audit fixes."""

from __future__ import annotations

import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from arena import agent, config, connectors, crypto, deep_links, engine, google_ai, models, permissions, workspace
from arena.api import ArenaClient
from arena.providers.chatgpt import ChatGPTProvider
from arena.providers.claude import ClaudeProvider
from arena.providers.deepseek import DeepSeekProvider
from arena.providers.freebuff import FreebuffProvider
from arena.providers.kimi import KimiProvider
from arena.web import app as web


# ---------------------------------------------------------------------------
# Critical fixes
# ---------------------------------------------------------------------------

def test_vote_js_recaptcha_selector_is_valid():
    assert 'script[src*="recaptcha"][src*="render="]' in engine._VOTE_JS
    assert 'script[src*="recaptcha"][src*="render="' not in engine._VOTE_JS.replace(
        'script[src*="recaptcha"][src*="render="]', ""
    )


def test_ssrf_blocked_in_save_media_sandbox_and_file_bridge(monkeypatch, tmp_path):
    cfg = {"workspace_dir": str(tmp_path / "ws"), "permission_mode": "bypassPermissions"}
    monkeypatch.setattr(web, "load_config", lambda: dict(cfg))
    monkeypatch.setattr(web, "save_config", lambda v: cfg.update(v))
    called = []
    monkeypatch.setattr(web.requests, "get", lambda *a, **kw: called.append(a))

    # _save_media must reject loopback/private/http URLs without touching requests.get
    out = list(web._save_media({"url": "http://127.0.0.1:8080/secret", "kind": "image"}, "a", {"title": "t"}))
    assert not called
    assert any("public https" in chunk.lower() for chunk in out)

    # _native_file_bridge must reject private/metadata URLs
    gen = web._native_file_bridge([{"url": "http://169.254.169.254/latest/meta-data", "title": "meta.txt"}], {})
    events = list(gen)
    assert not called
    assert any("public https" in chunk.lower() for chunk in events)


def test_download_public_https_rejects_redirect_loop_and_oversize(monkeypatch):
    monkeypatch.setattr(web, "_is_public_https_url", lambda u: u.startswith("https://"))

    class RedirectResp:
        status_code = 302
        headers = {"Location": "https://cdn.example/next"}
        closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(web.requests, "get", lambda *a, **kw: RedirectResp())
    with pytest.raises(ValueError, match="Too many redirects"):
        web._download_public_https("https://cdn.example/start", max_bytes=1024)

    # Redirect loop in /api/media/save must return 400 (never write 0-byte file with 200)
    client = web.app.test_client()
    r = client.post("/api/media/save", json={"url": "https://cdn.example/start"})
    assert r.status_code == 400

    # Oversized streaming response must raise OverflowError before reading infinite bytes
    class HugeResp:
        status_code = 200
        headers = {"Content-Type": "application/octet-stream"}

        def raise_for_status(self):
            pass

        def iter_content(self, _n):
            for _ in range(10):
                yield b"x" * 2048

        def close(self):
            pass

    monkeypatch.setattr(web.requests, "get", lambda *a, **kw: HugeResp())
    with pytest.raises(OverflowError):
        web._download_public_https("https://cdn.example/huge", max_bytes=4096)


def test_settings_import_blocks_mcp_rce_and_bad_workspace_dir(monkeypatch, tmp_path):
    cfg = {}
    monkeypatch.setattr(web, "load_config", lambda: dict(cfg))
    monkeypatch.setattr(web, "save_config", lambda v: cfg.update(v))
    monkeypatch.setattr(web, "COOKIE_FILE", tmp_path / "cookies.txt")
    mcp_path = tmp_path / "mcp.json"
    monkeypatch.setattr(connectors, "_config_path", lambda p=None: mcp_path)

    client = web.app.test_client()

    # 1. Default import_mcp is False: MCP payload is NOT written automatically.
    r = client.post("/api/settings/import", json={
        "app": "Arena Desktop",
        "config": {"theme": "dark"},
        "mcp": {"mcpServers": {"evil": {"command": "sh", "args": ["-c", "id"]}}},
    })
    assert r.status_code == 200
    assert not mcp_path.exists()

    # 2. Explicit import_mcp=True rejects list payloads (cannot corrupt mcp.json).
    r = client.post("/api/settings/import", json={
        "app": "Arena Desktop",
        "config": {},
        "import_mcp": True,
        "mcp": ["not", "a", "dict"],
    })
    assert r.status_code == 400
    assert not mcp_path.exists()

    # 3. Explicit import_mcp=True forces disabled=True on every imported server.
    r = client.post("/api/settings/import", json={
        "app": "Arena Desktop",
        "config": {},
        "import_mcp": True,
        "mcp": {"mcpServers": {"demo": {"command": "echo", "args": ["hi"]}}},
    })
    assert r.status_code == 200
    saved_mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert saved_mcp["mcpServers"]["demo"]["disabled"] is True

    # 4. workspace_dir root / $HOME rejected in both /api/settings and /api/settings/import.
    assert client.post("/api/settings", json={"workspace_dir": "/"}).status_code == 400
    assert client.post("/api/settings", json={"workspace_dir": str(Path.home())}).status_code == 400
    assert client.post("/api/settings/import", json={
        "app": "Arena Desktop",
        "config": {"workspace_dir": str(Path.home())},
    }).status_code == 400


# ---------------------------------------------------------------------------
# High security & backend fixes
# ---------------------------------------------------------------------------

def test_auto_shell_filter_blocks_quotes_traversal_and_no_index():
    for bad in (
        'cat "/etc/passwd"',
        "cat '/home/user/.arena-personal/cookies.txt'",
        "cat ./../../etc/passwd",
        "ls ..",
        "cat /tmp/secret",
        'git diff --no-index "/etc/passwd" /dev/null',
        "cat ~/.ssh/id_rsa",
    ):
        assert permissions.decide("auto", "run_cmd", True, bad) == "ask", f"should block: {bad}"

    for good in (
        "ls",
        "git status",
        "git diff src/app.py",
        "cat README.md",
        "python -m pytest tests/test_core.py",
    ):
        assert permissions.decide("auto", "run_cmd", True, good) == "allow", f"should allow: {good}"


def test_local_tools_delete_protects_dual_lane_git_hooks_and_outside_symlinks(tmp_path):
    root = tmp_path / "repo"
    out = root / "option-a"
    root.mkdir()
    out.mkdir()
    target = root / "real.txt"
    target.write_text("keep", encoding="utf-8")

    # Symlink in root must NOT be unlinked when dual-lane out is set
    root_link = root / "link.txt"
    root_link.symlink_to(target)
    lt_dual = agent.LocalTools(root, out=out)
    ok, _ = lt_dual.run({"name": "delete", "params": {"path": "link.txt"}})
    assert not ok
    assert root_link.is_symlink()

    # Symlink inside .git/hooks pointing into root must NOT be unlinked
    hooks = root / ".git" / "hooks"
    hooks.mkdir(parents=True)
    hook_link = hooks / "pre-commit"
    hook_link.symlink_to(target)
    lt = agent.LocalTools(root)
    ok, _ = lt.run({"name": "delete", "params": {"path": ".git/hooks/pre-commit"}})
    assert not ok
    assert hook_link.is_symlink()

    # Symlink outside root pointing into root must NOT be unlinked
    ext_link = tmp_path / "ext_link"
    ext_link.symlink_to(target)
    ok, _ = lt.run({"name": "delete", "params": {"path": "../ext_link"}})
    assert not ok
    assert ext_link.is_symlink()


def test_workspace_zip_skips_symlinks_and_caps_size(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "ok.txt").write_text("hello", encoding="utf-8")
    (proj / "leak.txt").symlink_to(secret)

    raw = workspace.zip_dir(proj)
    import io
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        assert z.namelist() == ["ok.txt"]

    with pytest.raises(ValueError, match="limit"):
        workspace.zip_dir(proj, max_bytes=2)


def test_workspace_project_dir_sanitizes_traversal(tmp_path):
    p = workspace.project_dir("../../etc", "demo", "../evil", base=tmp_path)
    assert tmp_path.resolve() in p.parents


def test_google_accounts_preserve_unreadable_protected_cookie(tmp_path, monkeypatch):
    import base64
    gfile = tmp_path / "google-accounts.json"
    stored = crypto.LINUX_PREFIX + base64.b64encode(b"z" * 32).decode("ascii")
    gfile.write_text(json.dumps({"current": "g1", "accounts": [{"id": "g1", "cookie": stored}]}), encoding="utf-8")
    monkeypatch.setattr(web, "GOOGLE_ACCOUNTS_FILE", lambda: gfile)
    monkeypatch.setattr(crypto, "_linux_unprotect", lambda _d: (_ for _ in ()).throw(OSError("locked")))

    loaded = web._google_accounts_load()
    assert loaded["accounts"][0]["cookie"] == ""
    web._google_accounts_save(loaded)

    saved = json.loads(gfile.read_text(encoding="utf-8"))
    assert saved["accounts"][0]["cookie"] == stored
    assert "_protected_cookie" not in saved["accounts"][0]
    assert stat.S_IMODE(os.stat(gfile).st_mode) == 0o600


def test_perm_mode_respects_google_track(monkeypatch):
    cfg = {"permission_mode": "bypassPermissions", "google_settings": {"permission_mode": "plan"}}
    monkeypatch.setattr(web, "load_config", lambda: dict(cfg))
    web._set_track("google")
    try:
        assert web._perm_mode() == "plan"
    finally:
        web._set_track("arena")
    assert web._perm_mode() == "bypassPermissions"


def test_gateway_approve_does_not_auto_allow_external_readonly_mcp(monkeypatch):
    cfg = {"permission_mode": "default"}
    monkeypatch.setattr(web, "load_config", lambda: dict(cfg))
    broker = permissions.PermissionBroker()
    asked = []

    def fake_ask(info):
        asked.append(info)
        pid = permissions.PermissionBroker.ask(broker, info)
        broker.resolve(pid, "deny")
        return pid

    monkeypatch.setattr(web, "BROKER", broker)
    monkeypatch.setattr(broker, "ask", fake_ask)

    # External MCP server declaring readOnlyHint=True MUST ask (inside=False), not auto-allow.
    assert web._gateway_approve("evil-mcp", "read_secrets", True, {}) is False
    assert asked and asked[0]["inside"] is False


def test_credential_files_created_with_0600(tmp_path, monkeypatch):
    inst = tmp_path / "instance.json"
    monkeypatch.setattr(deep_links, "_INSTANCE_FILE", inst)
    deep_links.write_instance(12345, "tok-secret")
    assert stat.S_IMODE(os.stat(inst).st_mode) == 0o600

    mcp_file = tmp_path / "mcp.json"
    connectors.save_config({"mcpServers": {}}, mcp_file)
    assert stat.S_IMODE(os.stat(mcp_file).st_mode) == 0o600


def test_cleanup_pid_does_not_delete_other_process_pid(tmp_path, monkeypatch):
    pid_file = tmp_path / "arena.pid"
    pid_file.write_text("99999999", encoding="utf-8")
    monkeypatch.setattr(config, "PID_FILE", pid_file)
    config.cleanup_pid()
    assert pid_file.exists()

    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    config.cleanup_pid()
    assert not pid_file.exists()


def test_ls_handles_broken_symlink_and_inline_eval_allows_pytest_c(tmp_path):
    broken = tmp_path / "dangling"
    broken.symlink_to(tmp_path / "missing_target")
    lt = agent.LocalTools(tmp_path)
    ok, out = lt.run({"name": "ls", "params": {"path": "."}})
    assert ok and "broken symlink" in out

    assert not agent._has_inline_eval("python -m pytest -c pyproject.toml")
    assert not agent._has_inline_eval("python script.py -c config.yaml")
    assert agent._has_inline_eval('python -c "import os"')
    assert agent._has_inline_eval('bash -lc "id"')
    assert agent._has_inline_eval('node --eval "1+1"')


def test_permission_broker_cancel_all_clears_pending():
    b = permissions.PermissionBroker()
    b.ask({"tool": "run_cmd"})
    b.ask({"tool": "delete"})
    assert b.cancel_all() == 2
    assert b._pending == {}


# ---------------------------------------------------------------------------
# Providers, engines & streaming fixes
# ---------------------------------------------------------------------------

def test_google_engine_ask_handles_small_final_growth(monkeypatch):
    g = google_ai.GoogleEngine()
    g.window = object()
    g.loaded = True
    g.host = "www.google.com"
    monkeypatch.setattr(google_ai, "POLL", 0.001)
    monkeypatch.setattr(google_ai, "MAX_WAIT", 5.0)
    monkeypatch.setattr(g, "_submit_in_page", lambda _p: True)
    seq = iter([
        "Hello world from Google AI mode",
        "Hello world from Google AI mode!",
        "Hello world from Google AI mode!",
        "Hello world from Google AI mode!",
        "Hello world from Google AI mode!",
    ])
    monkeypatch.setattr(g, "_answer_text", lambda: next(seq, "Hello world from Google AI mode!"))
    events = list(g.ask("hi", first=False))
    done = [e for e in events if e.get("type") == "done"]
    assert done and done[0]["text"] == "Hello world from Google AI mode!"


def test_chatgpt_sse_replace_and_shorthand_deltas():
    p = ChatGPTProvider()
    state: dict = {"buf": ""}
    chunk1 = (
        'data: {"v": {"conversation_id": "conv-123"}}\n'
        'data: {"v": [{"p": "/message/content/parts/0", "o": "append", "v": "Hello"}]}\n'
        'data: {"v": " world"}\n'
    )
    ev1 = p._parse_observed_sse_chunk(chunk1, state)
    assert any(e.get("type") == "meta_update" and "conv-123" in e.get("external_url", "") for e in ev1)
    assert "".join(e["text"] for e in ev1 if e["type"] == "text") == "Hello world"

    # Replace patch must NOT duplicate existing text
    chunk2 = 'data: {"v": [{"p": "/message/content/parts/0", "o": "replace", "v": "Hello world!"}]}\n'
    ev2 = p._parse_observed_sse_chunk(chunk2, state)
    assert ev2 == [{"type": "text", "text": "!"}]


def test_kimi_connect_stream_does_not_wedge_on_0x7b_length_byte():
    p = KimiProvider()
    state: dict = {"buf": ""}
    # Binary header containing 0x7b ('{') followed by a real JSON frame without newline
    raw = '\x00\x00\x00\x00{"block": {"text": {"content": "Hi"}}}'
    raw_with_7b = '\x00\x00\x00{"block": {"text": {"content": "Hi"}}}'
    # Explicit 0x7b byte right before valid JSON
    chunk = '\x00\x00\x00\x7b{"block": {"text": {"content": "Hi"}}}'
    ev = p._parse_observed_sse_chunk(chunk, state)
    assert ev == [{"type": "text", "text": "Hi"}]
    assert raw and raw_with_7b


def test_deepseek_sse_ignores_search_fragments_and_non_fragment_lists():
    p = DeepSeekProvider()
    state: dict = {"buf": ""}
    chunk = (
        'data: {"p": "response/fragments", "o": "SET", "v": [{"id": 1, "type": "SEARCH"}, {"id": 2, "type": "RESPONSE"}]}\n'
        'data: {"p": "response/fragments/0/content", "o": "APPEND", "v": "search metadata"}\n'
        'data: {"p": "response/fragments/1/content", "o": "APPEND", "v": "real answer"}\n'
        'data: {"p": "response/search_results", "o": "SET", "v": [{"content": "snippet"}]}\n'
    )
    ev = p._parse_observed_sse_chunk(chunk, state)
    assert ev == [{"type": "text", "text": "real answer"}]


def test_freebuff_recognizes_app_and_chat_thread_urls():
    fb = FreebuffProvider()
    assert fb._is_conversation_url("https://freebuff.com/app/thread-123") is True
    assert fb._is_conversation_url("https://freebuff.com/chat/abc") is True
    assert fb._is_conversation_url("https://freebuff.com/app") is False
    assert fb._is_conversation_url("https://freebuff.com/app/") is False


def test_claude_dated_slug_selection_strips_8_digit_date():
    c = ClaudeProvider()
    seen = []

    def fake_eval(js, timeout=3.0):
        seen.append(js)
        return {"ok": True, "already": True}

    c.eval_js = fake_eval  # type: ignore[method-assign]
    assert c._apply_model_selection("claude-sonnet-4-6-20260514") is True
    assert seen and 'const wantTokens = ["sonnet", "4", "6"];' in seen[0]


def test_arena_client_parse_stream_dedupes_sources_utf8_tail_and_reasoning():
    client = ArenaClient(allow_cdp=False)

    class FakeResp:
        def iter_content(self, chunk_size=None):
            # Split multi-byte UTF-8 emoji across chunks + source + format-2 reasoning + unterminated tail
            b1 = b'a0:"hi \xf0\x9f'
            b2 = b'\x98\x80"\nah:{"url":"https://a.test"}\ng:"secret thought"\n0:"tail"'
            yield b1
            yield b2

    out = list(client.parse_stream(FakeResp(), show_reasoning=False))
    assert ("text", "hi \U0001f600") in out
    assert out.count(("source", {"url": "https://a.test"})) == 1
    assert not any(k == "reasoning" for k, _ in out)
    assert ("text", "tail") in out


def test_models_handles_explicit_null_organization():
    tbl = models.format_models_table([{"id": "m1", "name": "M1", "org": None, "rank": 1, "arena": "text"}])
    assert tbl is not None


def test_frontend_static_bug_regressions():
    app_js = Path("arena/web/static/app.js").read_text(encoding="utf-8")
    style_css = Path("arena/web/static/style.css").read_text(encoding="utf-8")
    index_html = Path("arena/web/static/index.html").read_text(encoding="utf-8")

    # Critical: renderUsagePanel passes props object, not bare string
    assert "const wrap = el('div', { class: 'memory-panel' });" in app_js
    assert "el('div', 'memory-panel')" not in app_js

    # High: SVG data URLs rejected in safeUrl
    assert "data:image\\/svg\\+xml" not in app_js

    # Medium: demoApi defines getGoogleStatus, no undefined CSS vars, chatsDrawer outside topbar
    assert "async getGoogleStatus()" in app_js
    assert "var(--border)" not in style_css
    assert "var(--surface-3)" not in style_css
    topbar_block = index_html.split("<header class=\"topbar\"", 1)[1].split("</header>", 1)[0]
    assert 'id="chatsDrawer"' not in topbar_block
