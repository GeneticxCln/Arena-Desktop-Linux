"""Unit tests for the Freebuff web provider (freebuff.com/chat)."""
from __future__ import annotations

import json

from arena import providers
from arena.providers.freebuff import FreebuffProvider
from arena.web import app as web_app


class _FakeWindow:
    def __init__(self):
        self.shown = False
        self.loaded = []
        self.events = type("_Ev", (), {"closing": _EventList(), "closed": _EventList()})()

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
    assert fb.origin == "https://freebuff.com/chat"
    assert fb.badge == "F"
    assert fb.badge_class == "is-freebuff"
    assert "div.composer-input[data-composer-input]" in fb.composer_selectors
    assert "button.send-key:not([disabled])" in fb.send_selectors

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
    monkeypatch.setattr(fb, "fetch_json", lambda ep, timeout=3.0: {"ok": True, "status": 200, "data": {}})
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


def test_freebuff_live_models_discovery(monkeypatch):
    fb = FreebuffProvider()
    monkeypatch.setattr(fb, "eval_js", lambda s, timeout=2.5: {"label": "Solar Pro 4"})

    def _fake_fetch(ep, timeout=3.5):
        if ep == "/api/v1/models":
            return {
                "ok": True,
                "data": {
                    "models": [
                        {"id": "deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash", "provider": "DeepSeek"},
                        {"id": "solar-pro-4", "name": "Solar Pro 4", "provider": "Upstage"},
                        {"id": "hidden-model", "name": "Hidden", "hidden": True},
                    ]
                },
            }
        return {"ok": False, "data": None}

    monkeypatch.setattr(fb, "fetch_json", _fake_fetch)
    live = fb._probe_live_models()
    assert live is not None
    assert live["live"] is True
    assert [m["id"] for m in live["models"]] == ["deepseek-v4.1-flash", "solar-pro-4"]
    assert live["active_model"] == "solar-pro-4"
    assert "Fast" in live["models"][0]["capabilities"]
    assert "Reasoning" in live["models"][1]["capabilities"]


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
        assert any("Freebuff web reply" in str(m.get("text") or "") for m in saved.get("messages") or [])
    finally:
        providers.PROVIDERS["freebuff"] = orig
