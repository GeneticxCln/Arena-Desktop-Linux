# ruff: noqa: F811  (pytest fixtures imported from test_core are redefined as parameters)
"""Browser tools against real headless Chrome and a local page (skipped without Chrome)."""
from __future__ import annotations

import http.server
import threading
from functools import partial

import pytest
from test_core import FakeResp, events, web  # noqa: F401  (web is a fixture)

from arena import browser_agent as ba

CHROME = ba.find_chrome()
pytestmark = pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")

PAGE = """<!doctype html><html><head><title>Sign up</title></head><body>
<h1>Sign up</h1>
<form onsubmit="event.preventDefault(); document.getElementById('out').textContent =
  'Welcome ' + document.getElementById('email').value + ' (' + document.getElementById('plan').value + ')';">
  <label>Email <input id="email" type="email" value="old@x.test"></label>
  <label>Plan <select id="plan"><option value="free">Free</option><option value="pro">Pro</option></select></label>
  <button type="submit">Create account</button>
</form>
<p id="out"></p>
<a href="/next.html">Next page</a>
</body></html>"""

NEXT = "<!doctype html><title>Next</title><h1>Second page</h1>"


@pytest.fixture
def site(tmp_path):
    (tmp_path / "index.html").write_text(PAGE, encoding="utf-8")
    (tmp_path / "next.html").write_text(NEXT, encoding="utf-8")
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def browser(tmp_path):
    b = ba.ChromeBrowser(chrome_path=CHROME, profile_dir=tmp_path / "profile", headless=True)
    yield b
    b.shutdown()


def _ref(snapshot: str, needle: str) -> str:
    line = next(line for line in snapshot.splitlines() if needle in line)
    return line.strip().split()[0]


def test_fill_select_click_wait_read(browser, site):
    tab = browser.tab("chat-1")
    assert "Sign up" in tab.open(site + "/index.html")
    snap = tab.snapshot()
    assert '[heading] "Sign up"' in snap
    email, plan, button = _ref(snap, '"Email"'), _ref(snap, '"Plan"'), _ref(snap, '"Create account"')

    tab.fill(email, "me@arena.test")
    tab.select(plan, "Pro")
    tab.click(button)
    tab.wait("text", "Welcome me@arena.test (pro)", timeout=10)
    assert "Welcome me@arena.test (pro)" in tab.read()


def test_navigation_invalidates_refs_and_back_works(browser, site):
    tab = browser.tab("chat-2")
    tab.open(site + "/index.html")
    snap = tab.snapshot()
    link = _ref(snap, '"Next page"')
    tab.click(link)
    tab.wait("url", "**/next.html", timeout=10)
    with pytest.raises(ba.BrowserError, match="snapshot"):
        tab.click(link)
    assert '[heading] "Second page"' in tab.snapshot()
    tab.back()
    tab.wait("text", "Sign up", timeout=10)


def test_each_chat_gets_its_own_tab_and_screenshot(browser, site, tmp_path):
    one, two = browser.tab("a"), browser.tab("b")
    assert one.target_id != two.target_id
    one.open(site + "/index.html")
    two.open(site + "/next.html")
    assert "Sign up" in one.read() and "Second page" in two.read()
    shot = one.screenshot(tmp_path / "shot.png")
    assert shot.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    browser.close_tab("a")
    assert "a" not in browser.tabs


def test_chrome_has_no_debugging_port(browser, site):
    """The pipe transport means no TCP port another program could attach to."""
    browser.tab("x").open(site + "/index.html")
    # Chrome rewrites its own process title into one string, so search the whole of it.
    cmdline = open(f"/proc/{browser.pid}/cmdline", "rb").read().replace(b"\0", b" ")
    assert b"--remote-debugging-pipe" in cmdline
    assert b"--remote-debugging-port" not in cmdline
    # Chrome writes this file only when it listens on a TCP debugging port.
    assert not (browser.profile_dir / "DevToolsActivePort").exists()


def test_agent_turn_drives_the_browser_through_api_chat(web, site, tmp_path, monkeypatch):
    """A fake model opens the page, fills and submits the form; the real browser does it."""
    import json as _json
    appmod, c, cfg, use, sent = web
    cfg["workspace_dir"] = str(tmp_path / "ws")
    # The test site is on 127.0.0.1, a local address, which asks even in Auto mode.
    cfg["permission_mode"] = "bypassPermissions"
    real = ba.ChromeBrowser(chrome_path=CHROME, profile_dir=tmp_path / "profile", headless=True)
    monkeypatch.setattr(ba, "get_browser", lambda headless=True: real)

    def say(text):
        return FakeResp(["a0:" + _json.dumps(text)])

    try:
        use(
            say(f'<tool name="browser_open"><url>{site}/index.html</url></tool>'
                '<tool name="browser_snapshot"></tool>'),
            say('<tool name="browser_fill"><ref>@e1</ref><text>agent@arena.test</text></tool>'
                '<tool name="browser_click"><ref>@e3</ref></tool>'
                '<tool name="browser_wait"><text>Welcome agent@arena.test</text></tool>'
                '<tool name="browser_read"></tool>'),
            say("Signed up."),
        )
        ev = events(c.post("/api/chat", json={"message": "sign me up", "mode": "agent",
                                              "modality": "text", "agent_backend": "local"}))
        assert "browser_open" in sent[0][1]["userMessage"]["content"]      # the guide teaches the tools
        second = sent[1][1]["userMessage"]["content"]
        assert '@e1 [textbox] "Email"' in second and "untrusted" in second  # snapshot came back, marked
        third = sent[2][1]["userMessage"]["content"]
        assert "Welcome agent@arena.test (free)" in third                  # the click really submitted
        results = {e["tool"]: e["ok"] for e in ev if e.get("type") == "tool_result"}
        assert results.get("browser_click") is True and results.get("browser_read") is True
    finally:
        real.shutdown()



def test_chats_recover_after_chrome_exits(browser, site):
    import os
    import signal
    import time
    browser.tab("a").open(site + "/index.html")
    os.killpg(browser.pid, signal.SIGKILL)
    time.sleep(1.0)
    assert "Title: Next" in browser.tab("b").open(site + "/next.html")   # a new chat restarts Chrome
    assert "Sign up" in browser.tab("a").open(site + "/index.html")      # the old chat gets a fresh tab


def test_an_orphaned_chrome_on_the_profile_is_cleared(site, tmp_path):
    import os
    import signal
    first = ba.ChromeBrowser(chrome_path=CHROME, profile_dir=tmp_path / "p", headless=True)
    first.tab("x").open(site + "/index.html")
    orphan = first.pid
    first._conn, first.pid = None, None        # Arena died without shutting Chrome down
    second = ba.ChromeBrowser(chrome_path=CHROME, profile_dir=tmp_path / "p", headless=True)
    try:
        assert "Sign up" in second.tab("y").open(site + "/index.html")
        try:
            os.kill(orphan, 0)
            alive = open(f"/proc/{orphan}/stat").read().split()[2] != "Z"
        except ProcessLookupError:
            alive = False
        assert not alive
    finally:
        second.shutdown()
        try:
            os.killpg(orphan, signal.SIGKILL)
        except ProcessLookupError:
            pass
