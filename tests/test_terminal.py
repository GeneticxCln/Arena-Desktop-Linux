"""Tests for the built-in native Linux PTY Terminal Emulator."""
from __future__ import annotations

import base64
import json
import time

from arena.terminal import TerminalManager
from arena.web import app as web_app


def test_pty_manager_runs_real_shell_and_resizes(tmp_path):
    mgr = TerminalManager()
    sess = mgr.create(cwd=str(tmp_path), cols=90, rows=28, shell="/bin/sh")
    try:
        assert sess.alive is True
        assert sess.cwd == str(tmp_path.resolve())
        assert sess.resize(120, 40) is True
        assert sess.cols == 120 and sess.rows == 40

        assert sess.write(b"printf 'ARENA_PTY_OK_%s\\n' \"$TERM\"\n") is True
        # Drain replay until marker arrives
        collected = b""
        deadline = time.time() + 5.0
        while time.time() < deadline:
            with sess._lock:
                collected = b"".join(sess._replay)
            if b"ARENA_PTY_OK_xterm-256color" in collected:
                break
            time.sleep(0.05)
        assert b"ARENA_PTY_OK_xterm-256color" in collected
    finally:
        assert mgr.shutdown_all() == 1
        assert sess.alive is False


def test_terminal_api_routes_and_shutdown_cleanup(tmp_path):
    client = web_app.app.test_client()
    # Legacy unpermissioned single-shot route remains removed (404)
    assert client.post("/api/terminal", json={"command": "echo hi"}).status_code == 404

    # Create session via REST API
    r_create = client.post("/api/terminal/sessions", json={"cwd": str(tmp_path), "cols": 100, "rows": 25})
    assert r_create.status_code == 200
    body = r_create.get_json()
    assert body["ok"] is True
    sid = body["session"]["id"]
    assert body["session"]["cwd"] == str(tmp_path.resolve())
    assert body["session"]["alive"] is True

    # List sessions includes it
    r_list = client.get("/api/terminal/sessions")
    assert r_list.status_code == 200
    assert any(s["id"] == sid for s in r_list.get_json()["sessions"])

    # Resize session
    r_res = client.post(f"/api/terminal/sessions/{sid}/resize", json={"cols": 110, "rows": 32})
    assert r_res.status_code == 200
    assert r_res.get_json()["cols"] == 110

    # Send input + exit so stream terminates cleanly
    r_in = client.post(f"/api/terminal/sessions/{sid}/input", json={"data": "echo TERMINAL_API_OK && exit\n"})
    assert r_in.status_code == 200
    assert r_in.get_json()["ok"] is True

    # Stream SSE output and verify decoded bytes contain marker + exit event
    r_stream = client.get(f"/api/terminal/sessions/{sid}/stream")
    assert r_stream.status_code == 200
    raw_sse = r_stream.get_data(as_text=True)
    decoded = bytearray()
    saw_exit = False
    for line in raw_sse.splitlines():
        if not line.startswith("data: "):
            continue
        ev = json.loads(line[6:])
        if ev.get("type") == "data" and ev.get("b64"):
            decoded.extend(base64.b64decode(ev["b64"]))
        elif ev.get("type") == "exit":
            saw_exit = True
    assert b"TERMINAL_API_OK" in bytes(decoded)
    assert saw_exit is True

    # Close session & verify bad IDs are rejected
    assert client.delete(f"/api/terminal/sessions/{sid}").status_code == 200
    assert client.post("/api/terminal/sessions/../../etc/input", json={"data": "x"}).status_code == 404
    assert client.post("/api/terminal/sessions/not-valid-id!/input", json={"data": "x"}).status_code == 400


def test_pty_nonblocking_write_and_clean_reap(tmp_path):
    mgr = TerminalManager()
    sess = mgr.create(cwd=str(tmp_path), cols=80, rows=24, shell="/bin/sh")
    try:
        # Rapid writes stay non-blocking and deliver cleanly
        for i in range(5):
            assert sess.write(f"echo LINE_{i}\n".encode("ascii")) is True
        deadline = time.time() + 4.0
        buf = b""
        while time.time() < deadline:
            with sess._lock:
                buf = b"".join(sess._replay)
            if b"LINE_4" in buf:
                break
            time.sleep(0.05)
        assert b"LINE_0" in buf and b"LINE_4" in buf
    finally:
        assert mgr.close(sess.id) is True
        assert sess.proc.poll() is not None


def test_terminal_utf8_helpers_queue_force_exit_and_invalid_cwd():
    import queue as _q
    from arena.terminal import _queue_force_put, _trim_leading_utf8_cont, _trim_utf8_tail

    # Multi-byte UTF-8 truncation never splits a codepoint
    sample = ("a" * 10 + "\U0001f680").encode("utf-8")  # 14 bytes (10 + 4)
    trimmed = _trim_utf8_tail(sample, 12)
    assert trimmed == b"a" * 10
    assert trimmed.decode("utf-8") == "a" * 10

    # Leading orphaned continuation bytes trimmed cleanly
    assert _trim_leading_utf8_cont(b"\x80\x9fhi") == b"hi"

    # Full subscriber queue evicts oldest item so exit event is never dropped
    q = _q.Queue(maxsize=2)
    q.put_nowait({"type": "data", "b64": "QQ=="})
    q.put_nowait({"type": "data", "b64": "Qg=="})
    _queue_force_put(q, {"type": "exit", "code": 0})
    items = [q.get_nowait(), q.get_nowait()]
    assert items[-1] == {"type": "exit", "code": 0}

    # Explicit non-existent cwd returns 400 instead of silently spawning in $HOME
    client = web_app.app.test_client()
    r = client.post("/api/terminal/sessions", json={"cwd": "/nonexistent-dir-arena-test-xyz"})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False
