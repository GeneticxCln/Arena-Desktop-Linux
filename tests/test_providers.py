"""Tests for pluggable multi-provider tabs (DeepSeek, Claude, ChatGPT, Antigravity)."""
from __future__ import annotations

import json
import threading

from arena import providers
from arena.providers.antigravity import AntigravityProvider
from arena.providers.deepseek import DeepSeekProvider
from arena.providers.web_engine import WebChatEngine
from arena.web import app as web_app


def _sse_events(resp) -> list[dict]:
    out = []
    for raw in resp.get_data(as_text=True).splitlines():
        if raw.startswith("data: "):
            try:
                out.append(json.loads(raw[6:]))
            except ValueError:
                pass
    return out


class _FakeEvents:
    def __init__(self):
        self._closing = []
        self._closed = []

    class _Hook:
        def __init__(self, target):
            self._target = target

        def __iadd__(self, fn):
            self._target.append(fn)
            return self

    @property
    def closing(self):
        return self._Hook(self._closing)

    @closing.setter
    def closing(self, _v):
        pass

    @property
    def closed(self):
        return self._Hook(self._closed)

    @closed.setter
    def closed(self, _v):
        pass


class _FakeNative:
    def __init__(self, calls):
        self._calls = calls

    def present(self):
        self._calls.append("present")


class _FakeWebWindow:
    def __init__(self, calls, js_handler):
        self._calls = calls
        self._js = js_handler
        self.events = _FakeEvents()
        self.native = _FakeNative(calls)

    def show(self):
        self._calls.append("show")

    def hide(self):
        self._calls.append("hide")

    def restore(self):
        self._calls.append("restore")

    def load_url(self, url):
        self._calls.append(("load_url", url))

    def evaluate_js(self, script):
        return self._js(script)

    def get_cookies(self):
        return []


def test_provider_registry_lists_all_providers():
    listed = providers.list_providers()
    ids = [p["id"] for p in listed]
    assert ids == [
        "deepseek",
        "claude",
        "chatgpt",
        "qwen",
        "kimi",
        "zai",
        "minimax",
        "freebuff",
        "opencode",
        "antigravity",
    ]
    for pid in ids:
        assert providers.get_provider(pid) is not None
    assert providers.get_provider("unknown-provider") is None


def test_web_engine_show_and_human_check_surfaces_window_without_bypass(monkeypatch):
    calls = []

    def js(script):
        if "window.location ? String" in script:
            return "https://chat.deepseek.com/"
        if "has_composer" in script:
            return json.dumps({
                "href": "https://chat.deepseek.com/",
                "title": "Just a moment...",
                "has_composer": False,
                "body_sample": "Verify you are human",
            })
        return None

    win = _FakeWebWindow(calls, js)
    eng = DeepSeekProvider()
    eng.poll_interval = 0.01
    monkeypatch.setattr("arena.providers.web_engine._WINDOW_FACTORY", lambda t, u, h: win)

    evs = list(eng.stream_turn("c1", "hello", []))
    err = next(e for e in evs if e.get("type") == "error")
    assert err["code"] == "human_check"
    assert err.get("open_window") is True
    assert "show" in calls and "present" in calls


def test_web_engine_dom_streaming_emits_reasoning_text_and_url(monkeypatch):
    calls = []
    poll_state = {"n": 0}

    def js(script):
        if "window.location ? String" in script:
            return "https://chat.deepseek.com/"
        if "has_composer" in script:
            return json.dumps({
                "href": "https://chat.deepseek.com/",
                "title": "DeepSeek",
                "has_composer": True,
                "body_sample": "DeepSeek Chat",
            })
        if "promptText" in script:
            calls.append("submit")
            return json.dumps({"ok": True})
        if "const msgSels" in script:
            poll_state["n"] += 1
            n = poll_state["n"]
            if n == 1:
                return json.dumps({"count": 0, "text": "", "reasoning": "", "streaming": False,
                                   "href": "https://chat.deepseek.com/"})
            if n == 2:
                return json.dumps({"count": 1, "text": "Hello", "reasoning": "Thinking...",
                                   "streaming": True,
                                   "href": "https://chat.deepseek.com/a/chat/s/abc123"})
            return json.dumps({"count": 1, "text": "Hello world!", "reasoning": "Thinking... done.",
                               "streaming": False,
                               "href": "https://chat.deepseek.com/a/chat/s/abc123"})
        return None

    win = _FakeWebWindow(calls, js)
    eng = DeepSeekProvider()
    eng.poll_interval = 0.01
    monkeypatch.setattr("arena.providers.web_engine._WINDOW_FACTORY", lambda t, u, h: win)

    evs = list(eng.stream_turn("c1", "hi", []))
    assert "submit" in calls
    text_full = "".join(e["text"] for e in evs if e.get("type") == "text")
    reason_full = "".join(e["text"] for e in evs if e.get("type") == "reasoning")
    meta_urls = [e["external_url"] for e in evs if e.get("type") == "meta_update"]
    assert text_full == "Hello world!"
    assert reason_full == "Thinking... done."
    assert meta_urls == ["https://chat.deepseek.com/a/chat/s/abc123"]


def test_provider_api_routes_and_shared_workspace_memory(tmp_path, monkeypatch):
    ws = tmp_path / "multi_proj"; ws.mkdir()
    (ws / "MEMORY.md").write_text("- STACK: FastAPI + GTK\n", encoding="utf-8")
    cfg = {"workspace_dir": str(ws), "memory_enabled": True, "cross_tab_memory": True}
    monkeypatch.setattr(web_app, "load_config", lambda: cfg)
    monkeypatch.setattr(web_app, "save_config", lambda d: cfg.update(d))

    captured = {}

    class StubDeepSeek(DeepSeekProvider):
        def open_sign_in(self):
            return {"ok": True, "open": True}

        def stream_turn(self, chat_id, message, history, *, memory_block="", workspace="",
                        conversation_url="", model="", permission_mode="", is_followup_round=False, stop_event=None):
            captured["deepseek_mem"] = memory_block
            captured["deepseek_ws"] = workspace
            yield {"type": "reasoning", "text": "Checking stack"}
            yield {"type": "text", "text": "DeepSeek recommends uvicorn."}
            yield {"type": "meta_update", "external_url": "https://chat.deepseek.com/a/chat/s/99"}

    class StubClaude(WebChatEngine):
        id = "claude"
        name = "Claude"
        short_name = "Claude"
        badge = "\u25c8"
        origin = "https://claude.ai/new"

        def stream_turn(self, chat_id, message, history, *, memory_block="", workspace="",
                        conversation_url="", model="", permission_mode="", is_followup_round=False, stop_event=None):
            captured["claude_mem"] = memory_block
            yield {"type": "text", "text": "Claude agrees on uvicorn."}

    orig_ds = providers.PROVIDERS["deepseek"]
    orig_cl = providers.PROVIDERS["claude"]
    providers.PROVIDERS["deepseek"] = StubDeepSeek()
    providers.PROVIDERS["claude"] = StubClaude()
    try:
        client = web_app.app.test_client()
        assert client.get("/api/providers").status_code == 200
        assert client.post("/api/providers/deepseek/sign-in", json={}).status_code == 200
        assert client.post("/api/providers/nope/sign-in", json={}).status_code == 404

        # Turn 1 in a DeepSeek tab inside ws
        r1 = client.post("/api/chat", json={
            "message": "Pick a server",
            "track": "deepseek",
            "workspace": str(ws),
        })
        assert r1.status_code == 200
        ev1 = _sse_events(r1)
        assert any(e.get("type") == "done" and e.get("text_a") == "DeepSeek recommends uvicorn." for e in ev1)
        assert "STACK: FastAPI + GTK" in captured["deepseek_mem"]
        ds_chat_id = next(e["chat_id"] for e in ev1 if e.get("chat_id"))
        saved_ds = web_app._load_chat(ds_chat_id)
        assert saved_ds["source"] == "deepseek"
        assert saved_ds["external_url"] == "https://chat.deepseek.com/a/chat/s/99"

        # Turn 2 in a Claude tab in the SAME workspace inherits DeepSeek's live summary!
        r2 = client.post("/api/chat", json={
            "message": "Draft deployment config",
            "track": "claude",
            "workspace": str(ws),
        })
        assert r2.status_code == 200
        ev2 = _sse_events(r2)
        assert any(e.get("type") == "done" and e.get("text_a") == "Claude agrees on uvicorn." for e in ev2)
        assert "[DeepSeek]" in captured["claude_mem"]
        assert "DeepSeek recommends uvicorn." in captured["claude_mem"]
    finally:
        providers.PROVIDERS["deepseek"] = orig_ds
        providers.PROVIDERS["claude"] = orig_cl


def test_antigravity_provider_reports_missing_or_streams_cli(tmp_path, monkeypatch):
    prov = AntigravityProvider()
    monkeypatch.setattr("arena.providers.antigravity._has_antigravity_sdk", lambda: False)
    monkeypatch.setattr("arena.providers.antigravity._find_agy_cli", lambda: None)
    st = prov.status()
    assert st["available"] is False and st["signed_in"] is False
    evs = list(prov.stream_turn("c1", "hi", []))
    assert evs and evs[0]["code"] == "not_installed"

    # Mock executable agy CLI script
    fake_agy = tmp_path / "agy"
    fake_agy.write_text("#!/bin/sh\necho 'Antigravity reply from CLI'\n", encoding="utf-8")
    fake_agy.chmod(0o755)
    monkeypatch.setattr("arena.providers.antigravity._find_agy_cli", lambda: str(fake_agy))
    assert prov.status()["available"] is True
    evs2 = list(prov.stream_turn("c2", "hello", [], workspace=str(tmp_path)))
    text = "".join(e.get("text", "") for e in evs2 if e.get("type") == "text")
    assert "Antigravity reply from CLI" in text


def test_eval_js_prefers_direct_webkit_api_to_avoid_csp_unsafe_eval(monkeypatch):
    eng = DeepSeekProvider()
    calls = []

    class CSPWindow:
        uid = "child_test"

        def evaluate_js(self, script):
            raise AssertionError("Window.evaluate_js (JS eval wrapper) should not be called when WebKit direct is available")

    eng.window = CSPWindow()
    monkeypatch.setattr(eng, "_eval_js_webkit_direct", lambda script, timeout=6.0: (True, '{"has_composer": true}'))
    res = eng.eval_js("(() => 1)()")
    assert res == {"has_composer": True}


def test_claude_snapshot_excludes_prosemirror_composer_and_resets_stale_chat(monkeypatch):
    from arena.providers.claude import ClaudeProvider

    calls = []
    scripts = []
    poll = {"n": 0}

    def js(script):
        scripts.append(script)
        if "window.location ? String" in script:
            return "https://claude.ai/chat/old-conversation-id"
        if "has_composer" in script:
            return json.dumps({
                "href": "https://claude.ai/new",
                "title": "Claude",
                "has_composer": True,
                "body_sample": "Good afternoon",
            })
        if "promptText" in script:
            return json.dumps({"ok": True})
        if "const msgSels" in script:
            poll["n"] += 1
            if poll["n"] == 1:
                return json.dumps({"count": 0, "text": "", "reasoning": "", "streaming": False,
                                   "href": "https://claude.ai/new"})
            return json.dumps({"count": 1, "text": "Hello from Claude!", "reasoning": "",
                               "streaming": False,
                               "href": "https://claude.ai/chat/new-conversation-id"})
        return None

    win = _FakeWebWindow(calls, js)
    eng = ClaudeProvider()
    eng.poll_interval = 0.01
    monkeypatch.setattr("arena.providers.web_engine._WINDOW_FACTORY", lambda t, u, h: win)

    evs = list(eng.stream_turn("c_new", "hi", []))
    # New tab on stale /chat/... URL must navigate back to https://claude.ai/new
    assert ("load_url", "https://claude.ai/new") in calls
    text = "".join(e.get("text", "") for e in evs if e.get("type") == "text")
    assert text == "Hello from Claude!"
    snap_script = next(s for s in scripts if "const msgSels" in s)
    assert ".ProseMirror" in snap_script
    assert '[data-testid="user-message"]' in snap_script


def test_provider_models_endpoint_and_live_discovery(tmp_path, monkeypatch):
    from arena.providers.antigravity import _parse_agy_models_output
    from arena.providers.chatgpt import ChatGPTProvider
    from arena.providers.claude import ClaudeProvider

    # 1. Antigravity `agy models` parser handles spinner frames and columns
    sample = (
        "\r⠋ Fetching available models...\rgemini-4-neon-high             Gemini 4 Neon (High)\n"
        "claude-opus-5-5-medium         Claude Opus 5.5 (Medium)\n"
        "gpt-oss-120b-medium            GPT-OSS 120B (Medium)\n"
    )
    parsed = _parse_agy_models_output(sample)
    assert [m["id"] for m in parsed] == [
        "gemini-4-neon-high",
        "claude-opus-5-5-medium",
        "gpt-oss-120b-medium",
    ]
    assert parsed[0]["org"] == "Google"
    assert parsed[1]["org"] == "Anthropic"
    assert parsed[2]["org"] == "OpenAI"

    # 2. Claude live model probing from dropdown DOM + no hardcoded V3 in DeepSeek
    cl = ClaudeProvider()
    cl.window = object()
    monkeypatch.setattr(
        cl,
        "eval_js",
        lambda script, timeout=4.0: {
            "active": "Sonnet 5.5",
            "models": [
                {"id": "Sonnet 5.5", "name": "Sonnet 5.5", "description": "Active", "active": True},
                {"id": "Opus 5.5", "name": "Opus 5.5", "description": "Reasoning", "active": False},
            ],
        },
    )
    cl_models = cl.list_models(force=True)
    assert cl_models["live"] is True
    assert cl_models["active_model"] == "Sonnet 5.5"
    assert [m["id"] for m in cl_models["models"]] == ["Sonnet 5.5", "Opus 5.5"]

    ds_defaults = DeepSeekProvider().list_models()
    assert all("v3" not in m["name"].lower() for m in ds_defaults["models"])

    # 3. ChatGPT model URL selection
    cg = ChatGPTProvider()
    assert cg._target_url_for_model("", "gpt-4o") == "https://chatgpt.com/?model=gpt-4o"
    assert cg._target_url_for_model("", "auto") == "https://chatgpt.com/"

    # 4. Claude direct same-origin API streaming (identical to arena/engine.py)
    cl_api = ClaudeProvider()
    cl_api.window = object()

    def fake_fetch_json(url, *, method="GET", body=None, headers=None, timeout=8.0):
        if url == "/api/organizations":
            return {"ok": True, "status": 200, "data": [{"uuid": "org-123", "capabilities": ["chat"]}]}
        if url == "/api/organizations/org-123/chat_conversations":
            return {"ok": True, "status": 201, "data": {"uuid": "conv-abc-1234567890"}}
        return {"ok": False, "status": 404, "data": None}

    def fake_stream(url, *, method="POST", body=None, headers=None, timeout=180.0, stop_event=None):
        assert url == "/api/organizations/org-123/chat_conversations/conv-abc-1234567890/completion"
        assert body["model"] == "claude-sonnet-4-6"
        yield {"event": "status", "status": 200, "ok": True}
        yield {
            "event": "chunk",
            "data": (
                'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"Plan..."}}\n'
                'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi "}}\n'
                'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"there!"}}\n'
            ),
        }
        yield {"event": "end"}

    monkeypatch.setattr(cl_api, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(cl_api, "iter_fetch_stream", fake_stream)
    api_evs = list(cl_api._stream_turn_api("c1", "hello", model="claude-sonnet-4-6"))
    assert any(e.get("external_url") == "https://claude.ai/chat/conv-abc-1234567890" for e in api_evs)
    assert "".join(e["text"] for e in api_evs if e.get("type") == "reasoning") == "Plan..."
    assert "".join(e["text"] for e in api_evs if e.get("type") == "text") == "Hi there!"

    # 5. DeepSeek & ChatGPT observation-only SSE parsers
    ds_p = DeepSeekProvider()
    ds_state = {}
    ds_evs = ds_p._parse_observed_sse_chunk(
        'data: {"p":"response/thinking_content","v":"hmm"}\n'
        'data: {"p":"response/content","v":"Answer!"}\n',
        ds_state,
    )
    assert ds_evs == [
        {"type": "reasoning", "text": "hmm"},
        {"type": "text", "text": "Answer!"},
    ]

    cg_state = {}
    cg_evs = cg._parse_observed_sse_chunk(
        'data: {"conversation_id":"conv-99","p":"/message/content/parts/0","o":"append","v":"Hello"}\n'
        'data: {"o":"append","v":" world"}\n'
        'data: {"v":[{"p":"/message/content/parts/0","o":"append","v":"!"}]}\n',
        cg_state,
    )
    assert cg_evs == [
        {"type": "meta_update", "external_url": "https://chatgpt.com/c/conv-99"},
        {"type": "text", "text": "Hello"},
        {"type": "text", "text": " world"},
        {"type": "text", "text": "!"},
    ]

    # 6. Claude stream_turn runs API BEFORE requiring DOM has_composer
    monkeypatch.setattr(
        cl_api,
        "probe_page",
        lambda: {"href": "https://claude.ai/new", "title": "Claude", "has_composer": False, "body_sample": ""},
    )
    monkeypatch.setattr(cl_api, "page_url", lambda: "https://claude.ai/new")
    hidden_evs = list(cl_api.stream_turn("c_hidden", "hello", [], model="claude-sonnet-4-6"))
    assert "".join(e["text"] for e in hidden_evs if e.get("type") == "text") == "Hi there!"

    # 7. DeepSeek history_messages API fallback parses hidden-window replies
    ds_api = DeepSeekProvider()
    ds_api.window = object()
    monkeypatch.setattr(ds_api, "eval_js", lambda script, timeout=2.0: "tok-xyz")
    monkeypatch.setattr(
        ds_api,
        "fetch_json",
        lambda url, headers=None, timeout=4.0: {
            "ok": True,
            "status": 200,
            "data": {
                "data": {
                    "biz_data": {
                        "chat_messages": [
                            {"role": "USER", "content": "hi"},
                            {"role": "ASSISTANT", "thinking_content": "thought", "content": "Hello!", "status": "FINISHED"},
                        ]
                    }
                }
            },
        },
    )
    hist = ds_api._poll_conversation_api("https://chat.deepseek.com/a/chat/s/f1897132-cd79-4d8d-95fc-31954a9c5d84")
    assert hist == {"count": 1, "text": "Hello!", "reasoning": "thought", "streaming": False}


def test_providers_read_and_write_shared_workspace_with_permissions(tmp_path, monkeypatch):
    ws = tmp_path / "shared_repo"
    ws.mkdir()
    cfg = {"workspace_dir": str(ws), "permission_mode": "acceptEdits", "memory_enabled": False}
    monkeypatch.setattr(web_app, "load_config", lambda: cfg)
    monkeypatch.setattr(web_app, "save_config", lambda d: cfg.update(d))

    claude_prompts = []
    ds_prompts = []

    class ToolClaude(WebChatEngine):
        id = "claude"
        name = "Claude"
        short_name = "Claude"
        badge = "\u25c8"
        origin = "https://claude.ai/new"

        def stream_turn(self, chat_id, message, history, *, memory_block="", workspace="",
                        conversation_url="", model="", permission_mode="", is_followup_round=False, stop_event=None):
            claude_prompts.append(message)
            if len(claude_prompts) == 1:
                yield {
                    "type": "text",
                    "text": '<tool name="write_file"><path>hello.txt</path><content>written by claude\n</content></tool>',
                }
            else:
                yield {"type": "text", "text": "Created hello.txt."}

    class ToolDeepSeek(DeepSeekProvider):
        def stream_turn(self, chat_id, message, history, *, memory_block="", workspace="",
                        conversation_url="", model="", permission_mode="", is_followup_round=False, stop_event=None):
            ds_prompts.append(message)
            if len(ds_prompts) == 1:
                yield {
                    "type": "text",
                    "text": (
                        '<tool name="read_file"><path>hello.txt</path></tool>'
                        '<tool name="write_file"><path>blocked.txt</path><content>nope</content></tool>'
                    ),
                }
            else:
                yield {"type": "text", "text": "Read hello.txt; write was blocked in plan mode."}

    orig_cl = providers.PROVIDERS["claude"]
    orig_ds = providers.PROVIDERS["deepseek"]
    providers.PROVIDERS["claude"] = ToolClaude()
    providers.PROVIDERS["deepseek"] = ToolDeepSeek()
    try:
        client = web_app.app.test_client()
        # 1. Claude writes hello.txt under acceptEdits
        r1 = client.post("/api/chat", json={
            "message": "Create hello.txt",
            "track": "claude",
            "workspace": str(ws),
            "tools": True,
        })
        assert r1.status_code == 200
        ev1 = _sse_events(r1)
        assert any(e.get("type") == "tool_result" and e.get("tool") == "write_file" and e.get("ok") for e in ev1)
        assert (ws / "hello.txt").read_text(encoding="utf-8") == "written by claude\n"
        assert "[Arena Desktop]" in claude_prompts[0]
        assert "Tool results:" in claude_prompts[1]

        # 2. Switch to plan (read-only) mode: DeepSeek reads hello.txt, but write_file is blocked!
        cfg["permission_mode"] = "plan"
        r2 = client.post("/api/chat", json={
            "message": "Inspect hello.txt and try writing blocked.txt",
            "track": "deepseek",
            "workspace": str(ws),
            "tools": True,
        })
        assert r2.status_code == 200
        ev2 = _sse_events(r2)
        read_res = next(e for e in ev2 if e.get("type") == "tool_result" and e.get("tool") == "read_file")
        write_res = next(e for e in ev2 if e.get("type") == "tool_result" and e.get("tool") == "write_file")
        assert read_res["ok"] is True
        assert "written by claude" in read_res["output"]
        assert write_res["ok"] is False
        assert "plan mode" in write_res["status"]
        assert not (ws / "blocked.txt").exists()
    finally:
        providers.PROVIDERS["claude"] = orig_cl
        providers.PROVIDERS["deepseek"] = orig_ds

    # 3. Antigravity passes permission_mode flags to agy CLI
    fake_agy = tmp_path / "agy_perm"
    fake_agy.write_text("#!/bin/sh\necho \"ARGS:$*\"\n", encoding="utf-8")
    fake_agy.chmod(0o755)
    ag = AntigravityProvider()
    monkeypatch.setattr("arena.providers.antigravity._has_antigravity_sdk", lambda: False)
    monkeypatch.setattr("arena.providers.antigravity._find_agy_cli", lambda: str(fake_agy))
    out_plan = "".join(e.get("text", "") for e in ag.stream_turn("a1", "hi", [], workspace=str(ws), permission_mode="plan") if e.get("type") == "text")
    out_edits = "".join(e.get("text", "") for e in ag.stream_turn("a2", "hi", [], workspace=str(ws), permission_mode="acceptEdits") if e.get("type") == "text")
    assert "--mode plan" in out_plan
    assert "--mode accept-edits" in out_edits


def test_audit_fixes_security_stop_history_and_antigravity_stderr(tmp_path, monkeypatch):
    from arena.providers.chatgpt import ChatGPTProvider

    ws = tmp_path / "sec_ws"
    ws.mkdir()
    (ws / "ok.txt").write_text("inside workspace\n", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("TOP_SECRET_DATA\n", encoding="utf-8")

    cfg = {"workspace_dir": str(ws), "permission_mode": "plan", "memory_enabled": False}
    monkeypatch.setattr(web_app, "load_config", lambda: cfg)
    monkeypatch.setattr(web_app, "save_config", lambda d: cfg.update(d))

    # 1. CRITICAL #1: read_files with workspace file + outside file MUST NOT auto-allow!
    prompts_seen = []

    class TraversalProv(WebChatEngine):
        id = "claude"
        name = "Claude"
        short_name = "Claude"
        badge = "\u25c8"
        origin = "https://claude.ai/new"

        def stream_turn(self, chat_id, message, history, *, memory_block="", workspace="",
                        conversation_url="", model="", permission_mode="", is_followup_round=False, stop_event=None):
            prompts_seen.append(message)
            if len(prompts_seen) == 1:
                yield {
                    "type": "text",
                    "text": f'<tool name="read_files"><paths>ok.txt\n{outside}</paths></tool>',
                }
            else:
                yield {"type": "text", "text": "Done checking."}

    orig_cl = providers.PROVIDERS["claude"]
    providers.PROVIDERS["claude"] = TraversalProv()
    try:
        client = web_app.app.test_client()
        seen_perm_prompts = []

        def _auto_deny_outside():
            import time as _t
            deadline = _t.time() + 5.0
            while _t.time() < deadline:
                pending = web_app.BROKER.pending()
                if pending:
                    for item in pending:
                        seen_perm_prompts.append(item)
                        web_app.BROKER.resolve(item["id"], "deny")
                    return
                _t.sleep(0.02)

        t_deny = threading.Thread(target=_auto_deny_outside, daemon=True)
        t_deny.start()
        r = client.post("/api/chat", json={
            "message": "Read both",
            "track": "claude",
            "workspace": str(ws),
            "tools": True,
        })
        assert r.status_code == 200
        evs = _sse_events(r)
        t_deny.join(timeout=2.0)
        assert len(seen_perm_prompts) == 1
        assert seen_perm_prompts[0]["inside"] is False
        assert "secret.txt" in seen_perm_prompts[0]["summary"]
        rf = next(e for e in evs if e.get("type") == "tool_result" and e.get("tool") == "read_files")
        assert rf["ok"] is False
        assert "TOP_SECRET_DATA" not in (rf.get("output") or "")
        # CRITICAL #7: Saved assistant message has text_b="" and stripped <tool> XML
        cid = next(e["chat_id"] for e in evs if e.get("chat_id"))
        saved = web_app._load_chat(cid)
        asst = next(m for m in saved["messages"] if m.get("role") == "assistant")
        assert asst.get("text_b") == ""
        assert "<tool" not in asst.get("text", "")

        # CRITICAL #2: Stop event set on a chat must NOT break subsequent sends!
        web_app._chat_stop_event(cid).set()
        web_app._stop.set()
        r_after_stop = client.post("/api/chat", json={
            "chat_id": cid,
            "message": "Follow up after stop",
            "track": "claude",
            "workspace": str(ws),
        })
        ev_after = _sse_events(r_after_stop)
        assert any(e.get("type") == "done" and e.get("text_a") == "Done checking." for e in ev_after)
    finally:
        providers.PROVIDERS["claude"] = orig_cl

    # 2. ChatGPT SSE parser ignores non-content append patches & flushes trailing line
    cg = ChatGPTProvider()
    cg_state = {}
    evs_meta = cg._parse_observed_sse_chunk(
        'data: {"p":"/title","o":"append","v":"SHOULD_NOT_APPEAR"}\n'
        'data: {"p":"/message/content/parts/0","o":"append","v":"Real"}',
        cg_state,
    )
    assert [e.get("text") for e in evs_meta if e.get("type") == "text"] == []
    evs_flushed = cg._parse_observed_sse_chunk("", cg_state, flush=True)
    assert [e.get("text") for e in evs_flushed if e.get("type") == "text"] == ["Real"]

    # 3. Antigravity CLI drains >64 KiB stderr without deadlocking & includes history
    heavy_agy = tmp_path / "agy_stderr"
    heavy_agy.write_text(
        "#!/bin/sh\n"
        "python3 -c 'import sys; sys.stderr.write(\"E\"*90000); sys.stderr.flush()' \n"
        "printf 'STDOUT_OK:%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    heavy_agy.chmod(0o755)
    ag = AntigravityProvider()
    monkeypatch.setattr("arena.providers.antigravity._has_antigravity_sdk", lambda: False)
    monkeypatch.setattr("arena.providers.antigravity._find_agy_cli", lambda: str(heavy_agy))
    out = "".join(
        e.get("text", "")
        for e in ag.stream_turn(
            "ag_h",
            "second question",
            [{"role": "user", "text": "FIRST_TURN_MARKER"}],
            workspace=str(ws),
        )
        if e.get("type") == "text"
    )
    assert "STDOUT_OK:" in out
    assert "FIRST_TURN_MARKER" in out

    # 4. Flask API `/api/providers/<id>/models` + model_id passthrough on `/api/chat`
    client = web_app.app.test_client()
    for pid in ("deepseek", "claude", "chatgpt", "qwen", "kimi", "zai", "minimax", "freebuff", "opencode", "antigravity"):
        r = client.get(f"/api/providers/{pid}/models")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["provider"] == pid
        assert isinstance(body["models"], list) and len(body["models"]) >= 1
    assert client.get("/api/providers/unknown/models").status_code == 404

    seen_model = {}

    class StubModelProvider(DeepSeekProvider):
        def stream_turn(self, chat_id, message, history, *, memory_block="", workspace="",
                        conversation_url="", model="", permission_mode="", is_followup_round=False, stop_event=None):
            seen_model["model"] = model
            yield {"type": "text", "text": f"Used {model}"}

    orig = providers.PROVIDERS["deepseek"]
    providers.PROVIDERS["deepseek"] = StubModelProvider()
    try:
        resp = client.post("/api/chat", json={
            "message": "test model",
            "track": "deepseek",
            "model_id": "deepseek-reasoner",
        })
        assert resp.status_code == 200
        evs = _sse_events(resp)
        assert seen_model["model"] == "deepseek-reasoner"
        cid = next(e["chat_id"] for e in evs if e.get("chat_id"))
        saved = web_app._load_chat(cid)
        assert saved["model_a"] == "deepseek-reasoner"
    finally:
        providers.PROVIDERS["deepseek"] = orig


def test_qwen_kimi_zai_minimax_live_models_and_sse_parsers(monkeypatch):
    from arena.providers.kimi import KimiProvider
    from arena.providers.minimax import MiniMaxProvider
    from arena.providers.qwen import QwenProvider
    from arena.providers.zai import ZaiProvider

    # Qwen: /api/models live discovery + phase=think/answer SSE + flush
    qw = QwenProvider()
    monkeypatch.setattr(qw, "eval_js", lambda s, timeout=2.0: "tok-qwen")
    monkeypatch.setattr(
        qw,
        "fetch_json",
        lambda ep, headers=None, timeout=4.5: {
            "ok": True,
            "data": {
                "data": [
                    {"id": "qwen3-coder-plus", "name": "Qwen3 Coder Plus"},
                    {"id": "qwq-32b", "name": "QwQ 32B"},
                ]
            },
        },
    )
    q_live = qw._probe_live_models()
    assert q_live and q_live["live"] is True
    assert [m["id"] for m in q_live["models"]] == ["qwen3-coder-plus", "qwq-32b"]
    assert "Coding" in q_live["models"][0]["capabilities"]
    assert "Reasoning" in q_live["models"][1]["capabilities"]

    q_state = {}
    q_evs = qw._parse_observed_sse_chunk(
        'data: {"choices":[{"delta":{"phase":"think","content":"plan"}}]}\n'
        'data: {"choices":[{"delta":{"phase":"answer","content":"ok"}}]}',
        q_state,
    )
    q_evs += qw._parse_observed_sse_chunk("", q_state, flush=True)
    assert [(e["type"], e["text"]) for e in q_evs] == [("reasoning", "plan"), ("text", "ok")]

    # Kimi: European origin + non-chat URL detection + native SSE & Connect/gRPC JSON + flush
    km = KimiProvider()
    assert km.origin == "https://www.kimi.ai/en"
    assert km._is_non_chat_url("https://www.kimi.ai/make3d") is True
    assert km._is_non_chat_url("https://www.kimi.ai/en") is False
    assert km._is_non_chat_url("https://www.kimi.ai/chat/abc123") is False
    k_state = {}
    k_evs = km._parse_observed_sse_chunk(
        'data: {"event":"k1","text":"thinking..."}\n'
        'data: {"event":"cmpl","text":"answer"}\n'
        '\x00\x00\x00\x00\x1f{"block":{"text":{"content":"!"}}}',
        k_state,
    )
    k_evs += km._parse_observed_sse_chunk("", k_state, flush=True)
    assert [(e["type"], e["text"]) for e in k_evs] == [
        ("reasoning", "thinking..."),
        ("text", "answer"),
        ("text", "!"),
    ]

    # Z.AI / GLM: /api/models live discovery (matching active button + skipping hidden) + delta_content + history fallback
    za = ZaiProvider()
    monkeypatch.setattr(
        za,
        "eval_js",
        lambda s, timeout=2.5: {"label": "GLM-5.3-Flash Max"} if "modelSelectorButton" in s else "tok-zai",
    )
    monkeypatch.setattr(
        za,
        "fetch_json",
        lambda ep, headers=None, timeout=4.5: {
            "ok": True,
            "data": {
                "data": [
                    {"id": "glm-5.3", "name": "GLM-5.3", "info": {"meta": {"hidden": False}}},
                    {"id": "glm-5.4", "name": "GLM-5.4", "info": {"meta": {"hidden": False}}},
                    {"id": "x-preview-l", "name": "GLM-5.3-Flash", "info": {"meta": {"hidden": False}}},
                    {"id": "glm-hidden", "name": "Hidden", "info": {"meta": {"hidden": True}}},
                ]
            },
        },
    )
    z_live = za._probe_live_models()
    assert z_live and z_live["live"] is True
    assert [m["id"] for m in z_live["models"]] == ["glm-5.3", "glm-5.4", "x-preview-l"]
    # Exact normalized match: "GLM-5.3-Flash Max" must match "x-preview-l" (GLM-5.3-Flash), NOT "glm-5.3"!
    assert z_live["active_model"] == "x-preview-l"
    assert "Fast" in z_live["models"][2]["capabilities"]

    z_state = {}
    z_evs = za._parse_observed_sse_chunk(
        'data: {"type":"chat:completion","data":{"phase":"thinking","delta_content":"step"}}\n'
        'data: {"type":"chat:completion","data":{"phase":"answer","delta_content":"code"}}',
        z_state,
    )
    z_evs += za._parse_observed_sse_chunk("", z_state, flush=True)
    assert [(e["type"], e["text"]) for e in z_evs] == [("reasoning", "step"), ("text", "code")]

    # MiniMax: European origin + cumulative reply diff + non-prefix text_replace + flush
    mm = MiniMaxProvider()
    assert mm.origin == "https://agent.minimax.io/"
    assert 'div.tiptap.ProseMirror[contenteditable="true"]' in mm.composer_selectors
    m_state = {}
    m_evs = mm._parse_observed_sse_chunk(
        'data: {"data":{"reply":"Hel"}}\n'
        'data: {"data":{"reply":"Hello!"}}\n'
        'data: {"data":{"reply":"Replaced"}}',
        m_state,
    )
    m_evs += mm._parse_observed_sse_chunk("", m_state, flush=True)
    assert [(e["type"], e["text"]) for e in m_evs] == [
        ("text", "Hel"),
        ("text", "lo!"),
        ("text_replace", "Replaced"),
    ]


def test_audit_round2_regressions(tmp_path, monkeypatch):
    """Regression suite for round-2 audit findings (checkpoints, workspace guard, cookies, UTF-8, DeepSeek)."""
    # 1. DeepSeek ignores non-content status patches (e.g. FINISHED / empty path)
    ds = DeepSeekProvider()
    ds_state = {}
    ds_evs = ds._parse_observed_sse_chunk(
        'data: {"p":"response/status","v":"FINISHED"}\n'
        'data: {"p":"response/content","v":"Real reply"}\n'
        'data: {"p":"response/status","v":"DONE"}\n',
        ds_state,
    )
    assert [(e["type"], e["text"]) for e in ds_evs] == [("text", "Real reply")]

    # 2. Multi-account cookie sync persists rotated cookie for account #2 (not just index 0)
    acc_store = {
        "current": "acc-2",
        "accounts": [
            {"id": "acc-1", "cookie": "old-1"},
            {"id": "acc-2", "cookie": "old-2"},
        ],
    }
    saved_stores = []
    monkeypatch.setattr(web_app, "_accounts_load", lambda: acc_store)
    monkeypatch.setattr(web_app, "_accounts_save", lambda s: saved_stores.append(dict(s)))
    web_app._sync_current_account_cookie("fresh-cookie-2")
    assert acc_store["accounts"][1]["cookie"] == "fresh-cookie-2"
    assert len(saved_stores) == 1

    # 3. Provider track rejects "/" as workspace & emits dict checkpoints {"a": ck_id} + msg_index
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "note.txt").write_text("before", encoding="utf-8")
    cfg = {"workspace_dir": str(ws), "permission_mode": "auto", "memory_enabled": False}
    monkeypatch.setattr(web_app, "load_config", lambda: cfg)
    monkeypatch.setattr(web_app, "save_config", lambda d: cfg.update(d))

    seen_ws = []

    class StubWriteProvider(DeepSeekProvider):
        def __init__(self):
            super().__init__()
            self._round = 0

        def stream_turn(self, chat_id, message, history, *, memory_block="", workspace="",
                        conversation_url="", model="", permission_mode="", is_followup_round=False, stop_event=None):
            seen_ws.append(workspace)
            self._round += 1
            if self._round == 1 and workspace:
                yield {"type": "text", "text": '<tool name="write_file"><path>note.txt</path><content>after</content></tool>'}
            else:
                yield {"type": "text", "text": "Updated."}

    orig_ds = providers.PROVIDERS["deepseek"]
    providers.PROVIDERS["deepseek"] = StubWriteProvider()
    try:
        client = web_app.app.test_client()
        # Passing workspace="/" must be rejected (empty workspace, no local tools on /)
        r_root = client.post("/api/chat", json={"message": "hi", "track": "deepseek", "workspace": "/"})
        assert r_root.status_code == 200
        _sse_events(r_root)
        assert seen_ws[-1] == ""

        # Valid project workspace writes file, creates checkpoint dict {"a": "<id>"}, and sets msg_index
        providers.PROVIDERS["deepseek"]._round = 0
        r_ok = client.post("/api/chat", json={"message": "update note", "track": "deepseek", "workspace": str(ws)})
        assert r_ok.status_code == 200
        evs = _sse_events(r_ok)
        done = next(e for e in evs if e.get("type") == "done")
        assert isinstance(done.get("checkpoints"), dict)
        assert isinstance(done["checkpoints"].get("a"), str) and done["checkpoints"]["a"]
        assert isinstance(done.get("msg_index"), int)
    finally:
        providers.PROVIDERS["deepseek"] = orig_ds

    # 4. Antigravity CLI decodes multi-byte UTF-8 split across 4096-byte chunks without U+FFFD
    utf8_agy = tmp_path / "agy_utf8"
    utf8_agy.write_text(
        "#!/bin/sh\n"
        "python3 -c 'import sys; b = (\"a\"*4095 + \"\\U0001f680ok\").encode(\"utf-8\"); sys.stdout.buffer.write(b); sys.stdout.buffer.flush()'\n",
        encoding="utf-8",
    )
    utf8_agy.chmod(0o755)
    ag = AntigravityProvider()
    monkeypatch.setattr("arena.providers.antigravity._has_antigravity_sdk", lambda: False)
    monkeypatch.setattr("arena.providers.antigravity._find_agy_cli", lambda: str(utf8_agy))
    out = "".join(e.get("text", "") for e in ag.stream_turn("ag_u8", "-starts-with-dash", []) if e.get("type") == "text")
    assert "\ufffd" not in out
    assert out.endswith("\U0001f680ok")
