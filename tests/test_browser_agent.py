"""Browser tools, fast parts: snapshot rendering, refs, URL rules, keys, stale refs."""

from __future__ import annotations

import pytest

from arena import browser_agent as ba


def _ax(node_id, role, name="", children=(), backend=None, ignored=False, props=None):
    node = {
        "nodeId": str(node_id),
        "role": {"value": role},
        "name": {"value": name},
        "childIds": [str(c) for c in children],
        "ignored": ignored,
    }
    if backend is not None:
        node["backendDOMNodeId"] = backend
    if props:
        node["properties"] = [
            {"name": k, "value": {"value": v}} for k, v in props.items()
        ]
    return node


AX = [
    _ax(1, "RootWebArea", "Log in", children=[2, 3, 9]),
    _ax(2, "heading", "Log in", backend=20),
    _ax(3, "form", "", children=[4, 5, 6, 7], backend=30),
    _ax(4, "textbox", "Email", backend=40, props={"value": ""}),
    _ax(5, "textbox", "Password", backend=50),
    _ax(6, "checkbox", "Remember me", backend=60, props={"checked": "true"}),
    _ax(7, "button", "Continue", backend=70),
    _ax(8, "generic", "", ignored=True),
    _ax(9, "StaticText", "Forgot your password? Ask support.", backend=90),
]


def test_snapshot_numbers_interactive_elements_and_keeps_headings():
    text, refs = ba.render_snapshot(AX, title="Log in", url="https://x.test/login")
    assert text.splitlines()[:2] == ["Page: Log in", "URL: https://x.test/login"]
    assert '[heading] "Log in"' in text
    assert '@e1 [textbox] "Email"' in text
    assert '@e2 [textbox] "Password"' in text
    assert '@e3 [checkbox] "Remember me" checked' in text
    assert '@e4 [button] "Continue"' in text
    assert refs == {"e1": 40, "e2": 50, "e3": 60, "e4": 70}
    assert "Forgot your password" not in text  # plain text only in the full snapshot


def test_full_snapshot_includes_page_text():
    text, _ = ba.render_snapshot(
        AX, title="Log in", url="https://x.test/login", full=True
    )
    assert "Forgot your password? Ask support." in text


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "chrome://settings",
        "data:text/html,<b>x</b>",
        "ftp://x.test/a",
        "",
        "view-source:https://x.test",
        "blob:https://x.test/1",
    ],
)
def test_unsafe_urls_are_refused(url):
    with pytest.raises(ba.BrowserError):
        ba.check_url(url)


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://x.test/a", "https://x.test/a"),
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000/"),
        ("example.com/docs", "https://example.com/docs"),
        ("example.com:8080/a", "https://example.com:8080/a"),
        # Local dev servers: no scheme means plain http.
        ("localhost:3000", "http://localhost:3000"),
        ("localhost", "http://localhost"),
        ("127.0.0.1:8000/x", "http://127.0.0.1:8000/x"),
        ("192.168.1.10:8080", "http://192.168.1.10:8080"),
        ("about:blank", "about:blank"),
    ],
)
def test_web_urls_are_allowed(url, expected):
    assert ba.check_url(url) == expected


def test_key_events():
    down, up = ba.key_events("Enter")
    assert (
        down["type"] == "keyDown"
        and down["windowsVirtualKeyCode"] == 13
        and up["type"] == "keyUp"
    )
    down, _ = ba.key_events("Control+a")
    assert down["modifiers"] == 2 and down["key"] == "a"
    with pytest.raises(ba.BrowserError):
        ba.key_events("Hyper+Nope")


class _FakeConn:
    """Answers CDP calls from a table and records them."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def send(self, method, params=None, session_id=None, timeout=15.0):
        self.calls.append(method)
        answer = self.answers.get(method, {})
        return answer(params) if callable(answer) else answer


def _tab(conn):
    tab = ba.Tab(conn, target_id="T1", session_id="S1")
    return tab


def test_refs_are_dropped_when_the_page_navigates():
    conn = _FakeConn(
        {
            "Accessibility.getFullAXTree": {"nodes": AX},
            "Runtime.evaluate": {
                "result": {"value": ["Log in", "https://x.test/login"]}
            },
        }
    )
    tab = _tab(conn)
    tab.snapshot()
    assert tab.refs["e4"] == 70
    tab.on_event(
        "Page.frameNavigated", {"frame": {"id": "F1"}}
    )  # main frame: no parentId
    with pytest.raises(ba.BrowserError, match="snapshot"):
        tab.click("@e4")
    assert "Input.dispatchMouseEvent" not in conn.calls


def test_child_frame_navigation_keeps_refs():
    conn = _FakeConn(
        {
            "Accessibility.getFullAXTree": {"nodes": AX},
            "Runtime.evaluate": {
                "result": {"value": ["Log in", "https://x.test/login"]}
            },
        }
    )
    tab = _tab(conn)
    tab.snapshot()
    tab.on_event("Page.frameNavigated", {"frame": {"id": "F2", "parentId": "F1"}})
    assert tab.refs["e4"] == 70


def test_unknown_ref_asks_for_a_snapshot():
    tab = _tab(_FakeConn({}))
    with pytest.raises(ba.BrowserError, match="snapshot"):
        tab.click("@e9")
    with pytest.raises(ba.BrowserError):
        tab.click("button")  # not a ref


def test_browser_permission_kinds():
    from arena import agent, permissions

    acts = (
        "browser_open",
        "browser_click",
        "browser_fill",
        "browser_press",
        "browser_select",
        "browser_back",
        "browser_close",
    )
    reads = ("browser_snapshot", "browser_read", "browser_wait", "browser_screenshot")
    for tool in acts:
        assert permissions.decide("default", tool) == "ask"
        assert permissions.decide("acceptEdits", tool) == "allow"
        assert permissions.decide("auto", tool) == "allow"
        assert permissions.decide("plan", tool) == "deny"
        assert permissions.decide("bypassPermissions", tool) == "allow"
    for tool in reads:
        assert all(permissions.decide(m, tool) == "allow" for m in permissions.MODES)
        assert tool in agent.READ_ONLY_TOOLS


def test_browser_tools_are_off_without_a_browser(tmp_path):
    from arena import agent

    tools = agent.LocalTools(tmp_path)  # no browser given: the feature is switched off
    ok, out = tools.run({"name": "browser_open", "params": {"url": "https://x.test"}})
    assert not ok and "switched off" in out
    assert agent.browser_guide(False) == ""
    assert '<tool name="browser_snapshot">' in agent.browser_guide(True)
    info = agent.LocalTools(tmp_path, browser=lambda: None).describe(
        {"name": "browser_click", "params": {"ref": "@e3"}}
    )
    assert info["summary"] == "Browser: click @e3" and info["inside"] is True


def test_options_of_a_dropdown_are_listed_without_refs():
    nodes = [
        _ax(1, "RootWebArea", "Form", children=[2, 5]),
        _ax(2, "combobox", "Plan", children=[3, 4], backend=20),
        _ax(3, "option", "Free", backend=30, props={"selected": True}),
        _ax(4, "option", "Pro", backend=40),
        _ax(5, "button", "Create account", backend=50),
    ]
    text, refs = ba.render_snapshot(nodes, title="Form", url="https://x.test/")
    assert '@e1 [combobox] "Plan"' in text
    assert '- option "Free" selected' in text and '- option "Pro"' in text
    assert '@e2 [button] "Create account"' in text
    assert refs == {"e1": 20, "e2": 50}


def test_listbox_options_stay_clickable():
    nodes = [
        _ax(1, "RootWebArea", "Search", children=[2]),
        _ax(2, "listbox", "Suggestions", children=[3, 4], backend=20),
        _ax(3, "option", "Paris", backend=30),
        _ax(4, "option", "Prague", backend=40),
    ]
    text, refs = ba.render_snapshot(nodes, title="Search", url="https://x.test/")
    assert '[option] "Paris"' in text and refs["e2"] == 30 and refs["e3"] == 40


@pytest.mark.parametrize(
    "host, private",
    [
        ("localhost", True),
        ("app.localhost", True),
        ("127.0.0.1", True),
        ("10.0.0.5", True),
        ("192.168.1.1", True),
        ("172.16.3.4", True),
        ("169.254.169.254", True),
        ("[::1]", True),
        ("printer.local", True),
        ("example.com", False),
        ("8.8.8.8", False),
    ],
)
def test_private_hosts(host, private):
    assert ba.is_private_host(host) is private


def test_opening_a_local_network_address_always_asks(tmp_path):
    from arena import agent, permissions

    tools = agent.LocalTools(tmp_path, browser=lambda: None)
    local = tools.describe(
        {"name": "browser_open", "params": {"url": "http://192.168.1.1/admin"}}
    )
    public = tools.describe(
        {"name": "browser_open", "params": {"url": "https://example.com"}}
    )
    assert local["inside"] is False and public["inside"] is True
    assert permissions.decide("auto", "browser_open", inside=False) == "ask"
    assert (
        permissions.decide("bypassPermissions", "browser_open", inside=False) == "allow"
    )


class _NavConn(_FakeConn):
    """Page.navigate 'succeeds' but the page ends up somewhere else (a redirect)."""

    def __init__(self, lands_on):
        super().__init__(
            {
                "Page.navigate": {"frameId": "F1"},
                "Runtime.evaluate": {"result": {"value": ["Router", lands_on]}},
            }
        )
        self.navigated = []

    def send(self, method, params=None, session_id=None, timeout=15.0):
        if method == "Page.navigate":
            self.navigated.append(params["url"])
        return super().send(method, params, session_id, timeout)


def test_a_public_page_redirecting_to_the_local_network_is_blocked():
    conn = _NavConn("http://192.168.1.1/admin")
    tab = ba.Tab(conn, target_id="T1", session_id="S1")
    with pytest.raises(ba.BrowserError, match="local-network"):
        tab.open("https://evil.test/start")
    assert conn.navigated == ["https://evil.test/start", "about:blank"]


def test_an_approved_local_address_may_load():
    tab = ba.Tab(_NavConn("http://192.168.1.1/admin"), target_id="T1", session_id="S1")
    assert "192.168.1.1" in tab.open("http://192.168.1.1/admin")


def test_navigation_during_a_snapshot_discards_its_refs():
    tab = None

    def tree(_params):
        tab.on_event(
            "Page.frameNavigated", {"frame": {"id": "F1"}}
        )  # page changes mid-snapshot
        return {"nodes": AX}

    tab = ba.Tab(
        _FakeConn(
            {
                "Accessibility.getFullAXTree": tree,
                "Runtime.evaluate": {
                    "result": {"value": ["Log in", "https://x.test/"]}
                },
            }
        ),
        target_id="T1",
        session_id="S1",
    )
    text = tab.snapshot()
    assert tab.refs == {} and "changed while" in text


def test_sending_does_not_block_reply_delivery():
    """The reply lock must not be held while writing, or a full pipe deadlocks both sides."""
    import os
    import threading

    r, w = os.pipe()
    pipe = ba.CdpPipe(w, r, lambda *a: None)
    try:
        assert pipe._write_lock is not pipe._lock
        with pipe._write_lock:  # a writer is busy (for example blocked on a full pipe)
            got = threading.Event()
            threading.Thread(
                target=lambda: (
                    pipe._lock.acquire(timeout=1) and got.set() and pipe._lock.release()
                )
            ).start()
            assert got.wait(2)  # the reader can still take the reply lock
    finally:
        pipe.close()
