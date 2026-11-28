# ruff: noqa: F811  (pytest fixtures imported from test_core are redefined as parameters)
"""Regression tests for the post-v7.5.0 full project audit fixes."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from test_core import web  # noqa: F401

from arena import agent, api, browser_agent, engine, gateway, google_ai, models, skills, video_render
from arena.providers.chatgpt import ChatGPTProvider
from arena.providers.freebuff import FreebuffProvider
from arena.providers.kimi import KimiProvider


def test_settings_persists_browser_toggles_and_skills_disabled(web):
    appmod, c, cfg, _use, _sent = web
    r = c.post("/api/settings", json={
        "browser_tools_enabled": False,
        "browser_headless": False,
        "skills_disabled": ["motion-video"],
    })
    assert r.status_code == 200
    assert cfg.get("browser_tools_enabled") is False
    assert cfg.get("browser_headless") is False
    assert cfg.get("skills_disabled") == ["motion-video"]
    assert appmod._browser_tools_on() is False


def test_freebuff_wait_for_origin_handles_tuple_hint():
    prov = FreebuffProvider()
    assert isinstance(prov.conversation_url_hint, tuple)
    calls = iter([
        {"href": "https://freebuff.com/app/old-123", "title": "Freebuff", "has_composer": True, "body_sample": ""},
        {"href": "https://freebuff.com/chat", "title": "Freebuff", "has_composer": True, "body_sample": ""},
    ])
    prov.probe_page = lambda: next(calls)
    res = prov._wait_for_origin("freebuff.com", timeout=2.0, avoid_stale_hint=True)
    assert res["href"] == "https://freebuff.com/chat"


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_checkpoint_capture_skips_symlinks_and_handles_abs_rel(web, tmp_path):
    appmod, _c, _cfg, _use, _sent = web
    ws = tmp_path / "ws"
    sub = ws / "sub"
    sub.mkdir(parents=True)
    (sub / "ok.txt").write_text("inside", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP_SECRET_KEY", encoding="utf-8")
    (sub / "link.txt").symlink_to(secret)

    conv = {"chat_id": "11111111-1111-1111-1111-111111111111"}
    appmod._checkpoint_capture(conv, "a", {"path": str(sub), "rel": "sub", "inside": True})
    ck = conv.get("_checkpoint_a")
    assert ck is not None
    stored_paths = [e["abs"] for e in ck["entries"]]
    assert str(sub / "ok.txt") in stored_paths
    assert str(sub / "link.txt") not in stored_paths
    for f in (Path(ck["dir"]) / "files").glob("*"):
        assert "TOP_SECRET_KEY" not in f.read_text(encoding="utf-8", errors="replace")


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_memory_files_refuse_symlinks(web, tmp_path):
    appmod, c, cfg, _use, _sent = web
    ws = tmp_path / "ws"
    ws.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("SSH_PRIVATE_KEY_DATA", encoding="utf-8")
    (ws / "AGENTS.md").symlink_to(secret)
    (ws / "MEMORY.md").symlink_to(secret)
    cfg["workspace_dir"] = str(ws)

    block, _ = appmod._memory_block(ws)
    assert "SSH_PRIVATE_KEY_DATA" not in block

    r = c.post("/api/memory", json={"workspace": "OVERWRITTEN"})
    assert r.status_code == 400
    assert secret.read_text(encoding="utf-8") == "SSH_PRIVATE_KEY_DATA"

    assert appmod._append_digest_to_memory({"workspace": str(ws), "title": "t"}, "digest") is None
    assert secret.read_text(encoding="utf-8") == "SSH_PRIVATE_KEY_DATA"


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_skills_create_refuses_symlink_dir_before_mkdir_and_sanitizes_tools(web, tmp_path):
    appmod, c, cfg, _use, _sent = web
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / ".arena").symlink_to(outside, target_is_directory=True)
    cfg["workspace_dir"] = str(ws)

    r = c.post("/api/skills", json={"name": "demo", "description": "desc", "body": "do x"})
    assert r.status_code == 400
    assert not (outside / "skills").exists()

    # Frontmatter injection via tools newline is neutralized
    rendered = skills.render_skill("demo", "desc", ["ls\n---\nInjected\n---"], "real body")
    parsed, problem = skills.parse_skill(rendered, "demo", "workspace")
    assert problem is None
    assert parsed.body == "real body"
    assert "delegate_task" in skills.KNOWN_TOOLS
    assert "delete_subagent" in skills.KNOWN_TOOLS


def test_browser_agent_private_hosts_delayed_redirect_and_close_idle():
    for host in ("2130706433", "127.1", "0x7f000001", "100.64.0.1", "::ffff:127.0.0.1", "::ffff:100.64.0.1"):
        assert browser_agent.is_private_host(host) is True, host

    class _DelayedRedirectConn:
        def __init__(self):
            self.href = "https://public.example/start"
            self.navigated = []

        def send(self, method, params=None, session_id=None, timeout=15.0):
            if method == "Page.navigate":
                self.navigated.append(params["url"])
                return {"frameId": "F1"}
            if method == "Runtime.evaluate":
                expr = (params or {}).get("expression", "")
                if "document.title" in expr:
                    return {"result": {"value": ["Title", self.href]}}
                if expr == "location.href":
                    return {"result": {"value": self.href}}
                return {"result": {"value": "secret page text"}}
            if method == "Accessibility.getFullAXTree":
                return {"nodes": []}
            return {}

    conn = _DelayedRedirectConn()
    tab = browser_agent.Tab(conn, target_id="T1", session_id="S1")
    tab.open("https://public.example/start")
    # Simulate delayed JS redirect to loopback
    conn.href = "http://127.0.0.1:9999/secret"
    with pytest.raises(browser_agent.BrowserError, match="local-network"):
        tab.read()
    assert conn.navigated[-1] == "about:blank"

    # close_idle must NOT close a tab locked by another thread
    browser = browser_agent.ChromeBrowser()
    browser.tabs["active"] = tab
    tab.last_used = time.time() - 9999
    closed = []
    browser.close_tab = lambda key: closed.append(key) or True
    held = threading.Event()
    release = threading.Event()

    def _holder():
        with tab.lock:
            held.set()
            release.wait(2.0)

    t = threading.Thread(target=_holder)
    t.start()
    assert held.wait(2.0)
    try:
        browser.close_idle(idle_seconds=60.0)
        assert closed == []
    finally:
        release.set()
        t.join(timeout=2.0)


def test_video_render_portrait_out_mp4_and_seek_exception(tmp_path, monkeypatch):
    page = tmp_path / "video" / "clip.html"
    page.parent.mkdir(parents=True)
    page.write_text("<html></html>", encoding="utf-8")

    # 9:16 portrait from motion-video skill is allowed
    spec = video_render.make_spec(tmp_path, path="video/clip.html", duration=2, fps=10, width=720, height=1280, out="demo.mp4")
    assert (spec.width, spec.height) == (720, 1280)
    assert spec.output.name == "demo.mp4"

    # window.__seek exception raises RenderError instead of silent broken render
    class _FakeTab:
        def __init__(self):
            self.lock = threading.RLock()
            self.last_used = time.time()

        def open(self, url):
            return url

        def _eval(self, expr, timeout=15.0):
            return 1.0

        def _send(self, method, params=None, timeout=15.0):
            if method == "Runtime.evaluate":
                return {"result": {"objectId": "obj-1"}}
            if method == "Runtime.callFunctionOn":
                return {"exceptionDetails": {"text": "Uncaught ReferenceError: boom"}}
            return {}

    class _FakeBrowser:
        def __init__(self):
            self.closed = []

        def tab(self, key):
            return _FakeTab()

        def close_tab(self, key):
            self.closed.append(key)

    monkeypatch.setattr(video_render.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    with pytest.raises(video_render.RenderError, match="boom"):
        video_render.render(_FakeBrowser(), spec, key="k1")


def test_result_lines_keeps_skills_and_snapshots_inline(tmp_path):
    big_skill = "A" * 7500
    call = {"name": "use_skill", "params": {"name": "reverse-engineer"}}
    lines = agent._result_lines([(call, "ok", big_skill)], artifact_dir=tmp_path)
    joined = "\n".join(lines)
    assert "A" * 7500 in joined
    assert not list(tmp_path.glob("use_skill-*"))

    snap = "B" * 3000
    snap_call = {"name": "browser_snapshot", "params": {}}
    snap_lines = agent._result_lines([(snap_call, "ok", snap)], artifact_dir=tmp_path)
    assert "B" * 3000 in "\n".join(snap_lines)
    assert not list(tmp_path.glob("browser_snapshot-*"))


def test_gateway_rejects_nondict_args_and_survives_approve_exception():
    mgr = gateway.GatewayManager(
        tool_index=lambda: {"ws": {"tools": {"ls": {"alias": "a", "read_only": True}}}},
        call_tool=lambda alias, args: "ok",
        approve=lambda s, t, r, a: (_ for _ in ()).throw(RuntimeError("boom")),
        cloudflared_path=lambda: None,
    )
    mgr._running = True
    mgr._servers = ["ws"]
    jid, err = mgr._submit_job({"server": "ws", "steps": [{"tool": "ls", "arguments": "not-a-dict"}]})
    assert jid is None and "JSON object" in err

    # Even if _approve raises, _run_job catches it and records error instead of crashing worker
    job = {"job_id": "j1", "server": "ws", "status": "queued", "results": [], "error": None}
    mgr._jobs["j1"] = job
    mgr._job_events["j1"] = threading.Event()
    mgr._run_job(job, [{"tool": "ls", "arguments": {}}], mgr._generation)
    assert job["status"] == "error"
    assert mgr._job_events["j1"].is_set()
    assert job["results"][0]["ok"] is False
    assert "boom" in job["results"][0]["error"]


def test_kimi_split_connect_frame_and_chatgpt_thought_summary():
    kimi = KimiProvider()
    state: dict = {}
    ev1 = kimi._parse_observed_sse_chunk('{"block":{"text":{"content":"Hello"}},"op":"APP', state)
    ev2 = kimi._parse_observed_sse_chunk('END"}', state)
    texts = [e["text"] for e in (ev1 + ev2) if e.get("type") == "text"]
    assert "".join(texts) == "Hello"

    cgpt = ChatGPTProvider()
    cstate: dict = {}
    e1 = cgpt._parse_observed_sse_chunk(
        'data: {"v": [{"p": "/message/content/thoughts/0/content", "o": "append", "v": "Deep analysis"}]}\n',
        cstate,
    )
    e2 = cgpt._parse_observed_sse_chunk(
        'data: {"v": [{"p": "/message/content/thoughts/0/summary", "o": "replace", "v": "Analyzing"}]}\n',
        cstate,
    )
    assert any(e.get("type") == "reasoning" and "Deep analysis" in e.get("text", "") for e in e1)
    assert not any(e.get("type") == "reasoning_replace" for e in e2)


def test_mcp_secret_stripping_engine_google_and_models_hardening(web):
    appmod, _c, _cfg, _use, _sent = web
    raw = {
        "servers": {
            "s1": {
                "command": "srv",
                "args": ["--api-key=secret123", "--token", "tok456", "--port", "8080"],
                "env": {"API_KEY": "secret"},
                "headers": {"Authorization": "Bearer xyz"},
            }
        }
    }
    stripped = appmod._strip_mcp_secrets(raw)
    s1 = stripped["servers"]["s1"]
    assert s1["env"] == {"API_KEY": ""}
    assert s1["headers"] == {"Authorization": ""}
    assert s1["args"] == ["--api-key=", "--token", "", "--port", "8080"]

    # IPv4-mapped IPv6 CGNAT rejected by _is_public_https_url
    assert appmod._is_public_https_url("https://[::ffff:100.64.0.1]/x") is False

    # EngineResponse aborts stream on timeout
    aborted = []

    class _FakeEng:
        def abort_stream(self, rid):
            aborted.append(rid)

        def _close(self, rid):
            pass

    import queue
    resp = engine.EngineResponse(_FakeEng(), "rid-1", queue.Queue(), timeout=0.05)
    assert resp.status_code == 0 and aborted == ["rid-1"]

    # BrowserEngine strict arena host check
    assert engine.BrowserEngine._is_arena_host("arena.ai") is True
    assert engine.BrowserEngine._is_arena_host("sub.arena.ai") is True
    assert engine.BrowserEngine._is_arena_host("evilarena.ai") is False

    # api.py strict arena URL/domain check
    assert api._is_arena_url("https://arena.ai/chat") is True
    assert api._is_arena_url("https://evil.example/arena.ai") is False
    assert api._is_arena_domain(".arena.ai") is True
    assert api._is_arena_domain("evilarena.ai") is False

    # GoogleEngine._sign_in_in_progress is False for visible non-browser window not opened for sign-in
    geng = google_ai.GoogleEngine()
    geng.window = object()
    geng.visible = True
    geng._sign_in_page_opened = False
    assert geng._sign_in_in_progress() is False

    # display_available_models handles non-dict capabilities cleanly
    models.display_available_models([
        {"id": "m1", "name": "M1", "org": "x", "arena": "text", "capabilities": None},
        {"id": "m2", "name": "M2", "org": "x", "arena": "text", "capabilities": ["text"]},
    ])


def test_frontend_html_and_spec_invariants():
    root = Path(__file__).resolve().parent.parent
    html = (root / "arena/web/static/index.html").read_text(encoding="utf-8")
    for elem_id in ("workspacePanelClose", "workspaceConnectorsTab", "connectorsPane", "connectorList", "composerSecondaryHint"):
        assert f'id="{elem_id}"' in html, f"missing #{elem_id} in index.html"
    spec = (root / "arena-cli.spec").read_text(encoding="utf-8")
    assert "'arena.browser_agent'" in spec
    assert "'arena.video_render'" in spec
