"""Unit tests for the Freebuff web provider (freebuff.com/chat)."""

from __future__ import annotations

import json
import re

from arena import providers
from arena.providers.freebuff import FreebuffProvider
from arena.web import app as web_app


class _FakeWindow:
    def __init__(self):
        self.shown = False
        self.loaded = []
        self.events = type(
            "_Ev", (), {"closing": _EventList(), "closed": _EventList()}
        )()

    def show(self):
        self.shown = True

    def hide(self):
        self.shown = False

    def load_url(self, url):
        self.loaded.append(url)

    def evaluate_js(self, script):
        return None


class _EventList(list):
    def __iadd__(self, fn):
        self.append(fn)
        return self


def test_freebuff_provider_metadata_and_urls():
    fb = providers.get_provider("freebuff")
    assert isinstance(fb, FreebuffProvider)
    assert fb.id == "freebuff"
    assert fb.kind == "webview"
    # The full Freebuff Web workspace (code mode, model picker), not the plain /chat.
    assert fb.origin == "https://freebuff.com/app"
    assert fb.badge == "F"
    assert fb.badge_class == "is-freebuff"
    assert "div.composer-input[data-composer-input]" in fb.composer_selectors
    assert "button.send-key:not([disabled])" in fb.send_selectors

    assert fb._is_non_chat_url("https://freebuff.com/app") is False
    assert fb._is_non_chat_url("https://freebuff.com/app/thread-123") is False
    assert fb._is_non_chat_url("https://freebuff.com/chat") is False
    assert fb._is_non_chat_url("https://freebuff.com/chat/thread-123") is False
    assert fb._is_non_chat_url("https://freebuff.com/") is True
    assert fb._is_non_chat_url("https://freebuff.com/pricing") is True
    assert fb._is_non_chat_url("https://freebuff.com/desktop") is True


def test_freebuff_verify_distinguishes_guest_composer_from_signed_in(monkeypatch):
    fb = FreebuffProvider()
    fb.window = _FakeWindow()
    monkeypatch.setattr(fb, "has_desktop_runtime", lambda: True)
    monkeypatch.setattr(
        fb,
        "probe_page",
        lambda: {
            "href": "https://freebuff.com/chat",
            "title": "Freebuff Chat: 100% free AI chat",
            "has_composer": True,
            "body_sample": "How can I help?",
        },
    )
    monkeypatch.setattr(fb, "read_cookie_names", lambda: set())
    monkeypatch.setattr(fb, "eval_js", lambda s, timeout=2.0: {"guest": True})

    # Guest session: /api/auth/session returns {} -> must NOT report signed_in=True even with composer visible
    monkeypatch.setattr(
        fb,
        "fetch_json",
        lambda ep, timeout=3.0: {"ok": True, "status": 200, "data": {}},
    )
    guest = fb.verify()
    assert guest["signed_in"] is False
    assert "Sign in" in guest["detail"]

    # Signed-in paid session: /api/auth/session returns user object
    monkeypatch.setattr(
        fb,
        "fetch_json",
        lambda ep, timeout=3.0: {
            "ok": True,
            "status": 200,
            "data": {"user": {"email": "paid@example.com", "name": "Paid User"}},
        },
    )
    monkeypatch.setattr(fb, "eval_js", lambda s, timeout=2.0: {"guest": False})
    authed = fb.verify()
    assert authed["signed_in"] is True
    assert "paid@example.com" in authed["detail"]


def test_freebuff_stream_turn_requires_signin_before_submitting_as_guest(monkeypatch):
    fb = FreebuffProvider()
    win = _FakeWindow()
    fb.window = win
    monkeypatch.setattr(fb, "has_desktop_runtime", lambda: True)
    monkeypatch.setattr(fb, "page_url", lambda: "https://freebuff.com/chat")
    monkeypatch.setattr(
        fb,
        "verify",
        lambda: {
            "available": True,
            "signed_in": False,
            "open": False,
            "window": True,
            "detail": "Open the Freebuff window and click Sign in to use your account.",
        },
    )

    events = list(fb.stream_turn("c1", "hello", []))
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["code"] == "auth"
    assert events[0]["open_window"] is True
    assert win.shown is True


def _tier_page(monkeypatch, fb, *, active, menu):
    """Fake freebuff.com page: a model button labelled ``active`` whose menu
    holds ``menu`` items ({name, desc}). Returns the list of clicked tiers."""
    clicked: list[str] = []
    monkeypatch.setattr(fb, "_read_active_model_label", lambda: active)
    monkeypatch.setattr("arena.providers.freebuff.time.sleep", lambda s: None)

    def fake_eval(script, timeout=0):
        if "__freebuffTierScan" in script:
            return {"items": list(menu)}
        if "__freebuffTierClick" in script:
            want = json.loads(re.search(r"const want = (\"[^\"]*\");", script).group(1))
            hit = next((m["name"] for m in menu if m["name"].lower() == want), "")
            if hit:
                clicked.append(hit)
            return {"ok": bool(hit)}
        return {"ok": True}  # open / escape

    monkeypatch.setattr(fb, "eval_js", fake_eval)
    monkeypatch.setattr(
        fb,
        "fetch_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no API probing")),
    )
    return clicked


def test_freebuff_lists_the_tiers_its_menu_offers(monkeypatch):
    fb = FreebuffProvider()
    _tier_page(
        monkeypatch,
        fb,
        active="Powerful",
        menu=[
            {"name": "Instant", "desc": "Fast answers"},
            {"name": "Balanced", "desc": ""},
            {"name": "Powerful", "desc": "Best results"},
        ],
    )
    live = fb._probe_live_models()
    assert [m["id"] for m in live["models"]] == ["Instant", "Balanced", "Powerful"]
    assert live["active_model"] == "Powerful"
    assert live["models"][0]["description"] == "Fast answers"
    # A tier without menu text explains which model it runs.
    assert "m-61c26317cc" in live["models"][1]["description"]


def test_freebuff_falls_back_to_standard_tiers_when_the_menu_is_unreadable(monkeypatch):
    fb = FreebuffProvider()
    _tier_page(monkeypatch, fb, active="Powerful", menu=[])
    live = fb._probe_live_models()
    assert [m["id"] for m in live["models"]] == ["Instant", "Balanced", "Powerful"]
    assert live["active_model"] == "Powerful"
    assert "DeepSeek V4.1 Flash" in live["models"][2]["description"]


def test_freebuff_keeps_a_named_model_button_as_is(monkeypatch):
    fb = FreebuffProvider()
    _tier_page(monkeypatch, fb, active="DeepSeek V4 Pro", menu=[])
    live = fb._probe_live_models()
    assert [m["id"] for m in live["models"]] == ["DeepSeek V4 Pro"]


def test_freebuff_switches_tier_by_its_name(monkeypatch):
    fb = FreebuffProvider()
    menu = [
        {"name": "Instant", "desc": ""},
        {"name": "Balanced", "desc": ""},
        {"name": "Powerful", "desc": ""},
    ]
    clicked = _tier_page(monkeypatch, fb, active="Powerful", menu=menu)
    assert (
        fb._apply_model_selection("Powerful") is True and clicked == []
    )  # already on it
    assert fb._apply_model_selection("Balanced") is True and clicked == ["Balanced"]
    assert fb._apply_model_selection("Ultra") is False


def test_freebuff_sse_parser_handles_all_stream_formats():
    fb = FreebuffProvider()
    state: dict = {}
    evs = fb._parse_observed_sse_chunk(
        'g:"plan first"\n'
        '0:"hello "\n'
        'data: {"type":"reasoning-delta","delta":"more thought"}\n'
        'data: {"type":"text-delta","delta":"world"}\n'
        'data: {"type":"content_block_delta","delta":{"text":"!"}}\n'
        'data: {"choices":[{"delta":{"reasoning_content":"step","content":" ok"}}]}\n'
        'data: {"type":"text","text":" done"}',
        state,
    )
    evs += fb._parse_observed_sse_chunk("", state, flush=True)
    assert [(e["type"], e["text"]) for e in evs] == [
        ("reasoning", "plan first"),
        ("text", "hello "),
        ("reasoning", "more thought"),
        ("text", "world"),
        ("text", "!"),
        ("reasoning", "step"),
        ("text", " ok"),
        ("text", " done"),
    ]


def test_freebuff_chat_route_streams_and_persists_source(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "CHATS_DIR", tmp_path / "chats")

    class _StubFreebuff(FreebuffProvider):
        def stream_turn(
            self,
            chat_id,
            message,
            history,
            *,
            memory_block="",
            workspace="",
            conversation_url="",
            model="",
            permission_mode="",
            is_followup_round=False,
            stop_event=None,
        ):
            yield {"type": "text", "text": "Freebuff web reply"}

    orig = providers.PROVIDERS["freebuff"]
    providers.PROVIDERS["freebuff"] = _StubFreebuff()
    try:
        client = web_app.app.test_client()
        resp = client.post(
            "/api/chat",
            json={"message": "hi freebuff", "track": "freebuff", "model_id": "default"},
        )
        assert resp.status_code == 200
        raw = resp.get_data(as_text=True)
        events = []
        for line in raw.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    events.append(json.loads(payload))
        cid = next(e["chat_id"] for e in events if e.get("chat_id"))
        saved = web_app._load_chat(cid)
        assert saved["source"] == "freebuff"
        assert any(
            "Freebuff web reply" in str(m.get("text") or "")
            for m in saved.get("messages") or []
        )
    finally:
        providers.PROVIDERS["freebuff"] = orig


def _signed_out_probe_setup(monkeypatch, *, user, guest_cta):
    prov = FreebuffProvider()
    prov.window = _FakeWindow()
    probed: list[bool] = []
    monkeypatch.setattr(prov, "page_url", lambda: "https://freebuff.com/chat")
    monkeypatch.setattr(prov, "_read_session_user", lambda: user)
    monkeypatch.setattr(prov, "_has_guest_signin_button", lambda: guest_cta)
    monkeypatch.setattr(prov, "_wait_for_composer", lambda timeout=0: {})
    monkeypatch.setattr(
        prov,
        "_probe_live_models",
        lambda: (
            probed.append(True)
            or {
                "live": True,
                "active_model": "m1",
                "models": [{"id": "m1", "name": "M1"}],
            }
        ),
    )
    return prov, probed


def test_freebuff_guest_on_chat_page_is_reported_signed_out(monkeypatch):
    """freebuff.com/chat shows its composer to guests, with no /login URL."""
    prov, probed = _signed_out_probe_setup(monkeypatch, user=None, guest_cta=True)
    res = prov.list_models(force=True)
    assert res["signed_in"] is False
    assert res["source"] == "default"
    assert probed == []


def test_freebuff_signed_in_user_gets_the_live_list(monkeypatch):
    prov, probed = _signed_out_probe_setup(
        monkeypatch, user={"email": "a@b.c"}, guest_cta=False
    )
    res = prov.list_models(force=True)
    assert res["signed_in"] is True and res["source"] == "live"
    assert probed == [True]


def test_freebuff_tier_labels_with_a_reasoning_level(monkeypatch):
    """The live button reads "Powerful Max": tier name plus reasoning level."""
    fb = FreebuffProvider()
    _tier_page(monkeypatch, fb, active="Powerful Max", menu=[])
    live = fb._probe_live_models()
    assert [m["id"] for m in live["models"]] == ["Instant", "Balanced", "Powerful Max"]
    assert live["active_model"] == "Powerful Max"
    assert "DeepSeek V4.1 Flash" in live["models"][2]["description"]

    menu = [
        {"name": "Instant Low", "desc": ""},
        {"name": "Balanced High", "desc": ""},
        {"name": "Powerful Max", "desc": ""},
    ]
    clicked = _tier_page(monkeypatch, fb, active="Powerful Max", menu=menu)
    live = fb._probe_live_models()
    assert [m["id"] for m in live["models"]] == [
        "Instant Low",
        "Balanced High",
        "Powerful Max",
    ]
    assert [m["active"] for m in live["models"]] == [False, False, True]
    assert "low reasoning" in live["models"][0]["description"]
    assert fb._apply_model_selection("Balanced High") is True and clicked == [
        "Balanced High"
    ]


def test_freebuff_model_check_returns_to_the_app_page_first(monkeypatch):
    """The window drifted to the home page: read models on /app, not there."""
    prov, probed = _signed_out_probe_setup(
        monkeypatch, user={"email": "a@b.c"}, guest_cta=False
    )
    url = {"now": "https://freebuff.com/"}
    loaded: list[str] = []
    monkeypatch.setattr(prov, "page_url", lambda: url["now"])

    def fake_load(target):
        loaded.append(target)
        url["now"] = target

    monkeypatch.setattr(prov, "load_url", fake_load)
    monkeypatch.setattr(prov, "_wait_for_origin", lambda *a, **k: {})
    res = prov.list_models(force=True)
    assert loaded == ["https://freebuff.com/app"]
    assert res["source"] == "live" and probed == [True]
