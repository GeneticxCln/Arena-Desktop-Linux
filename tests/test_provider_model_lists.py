"""Provider model lists: live vs cached vs default, honest selection results."""

from __future__ import annotations

from arena.providers.claude import ClaudeProvider
from arena.providers.kimi import KimiProvider
from arena.providers.qwen import QwenProvider
from arena.providers.web_engine import WebChatEngine


class _FakeProvider(WebChatEngine):
    id = "fake"
    name = "Fake"
    origin = "https://fake.example"
    default_models = (
        {"id": "default", "name": "Fake (Account default)", "capabilities": ["Text"]},
    )

    def __init__(self) -> None:
        super().__init__()
        self.window = object()
        self.live = None
        self.applied = None
        self.apply_calls: list[str] = []

    def _wait_for_composer(self, timeout: float = 0.0) -> dict:
        return {}

    def _probe_live_models(self):
        return self.live

    def _apply_model_selection(self, model_id: str):
        self.apply_calls.append(model_id)
        return self.applied


def _live(*ids, active=""):
    return {
        "live": True,
        "active_model": active,
        "models": [{"id": i, "name": i.upper(), "active": i == active} for i in ids],
    }


def test_list_models_reports_live_then_cached_then_default():
    prov = _FakeProvider()
    assert prov.list_models(force=True)["source"] == "default"

    prov.live = _live("m-1", "m-2", active="m-2")
    first = prov.list_models(force=True)
    assert first["source"] == "live" and first["live"] is True
    assert [m["id"] for m in first["models"]] == ["m-1", "m-2"]

    prov.live = None  # provider page stopped answering
    again = prov.list_models(force=True)
    assert again["source"] == "cached"
    assert again["live"] is False
    assert [m["id"] for m in again["models"]] == ["m-1", "m-2"]
    assert again["active_model"] == "m-2"
    assert again["fetched_at"] > 0


def test_select_model_rejects_ids_missing_from_the_live_list():
    prov = _FakeProvider()
    prov.live = _live("m-1", "m-2", active="m-1")
    prov.list_models(force=True)

    res = prov.select_model("ghost-model")
    assert res["ok"] is False
    assert res["applied"] is False
    assert res["code"] == "unknown_model"
    assert prov.apply_calls == []


def test_select_model_accepts_date_suffixed_ids():
    prov = _FakeProvider()
    prov.live = _live("claude-x-5", active="claude-x-5")
    prov.list_models(force=True)
    prov.applied = True
    assert prov.select_model("claude-x-5-20260101")["applied"] is True


def test_select_model_reports_failed_switch_and_keeps_list():
    prov = _FakeProvider()
    prov.live = _live("m-1", "m-2", active="m-1")
    prov.list_models(force=True)

    prov.applied = False
    res = prov.select_model("m-2")
    assert res["ok"] is True and res["applied"] is False

    prov.applied = None  # an override that cannot tell must not claim success
    assert prov.select_model("m-2")["applied"] is False

    prov.applied = True
    assert prov.select_model("m-2")["applied"] is True
    prov.live = None
    cached = prov.list_models()
    assert [m["id"] for m in cached["models"]] == ["m-1", "m-2"]
    assert cached["active_model"] == "m-2"


def test_kimi_selection_returns_false_when_no_menu_item_matches(monkeypatch):
    kimi = KimiProvider()
    kimi.window = object()
    monkeypatch.setattr(kimi, "_read_active_model_label", lambda: "K2 Fast")
    replies = iter([{"ok": True}, {"ok": False}])
    monkeypatch.setattr(kimi, "eval_js", lambda script, timeout=0: next(replies))
    monkeypatch.setattr("arena.providers.kimi.time.sleep", lambda s: None)
    assert kimi._apply_model_selection("K9 Imaginary") is False


def test_qwen_marks_the_model_the_page_has_selected_as_active(monkeypatch):
    qw = QwenProvider()

    def fake_eval(script, timeout=0):
        if "label" in script:
            return {"label": "QwQ 32B"}
        return "tok"

    monkeypatch.setattr(qw, "eval_js", fake_eval)
    monkeypatch.setattr(
        qw,
        "fetch_json",
        lambda ep, headers=None, timeout=4.5: {
            "ok": True,
            "data": {
                "data": [
                    {"id": "qwen3-max", "name": "Qwen3 Max"},
                    {"id": "qwq-32b", "name": "QwQ 32B"},
                ]
            },
        },
    )
    live = qw._probe_live_models()
    assert live["active_model"] == "qwq-32b"
    assert [m["active"] for m in live["models"]] == [False, True]


def test_claude_label_without_api_slug_uses_the_page_not_the_account_default(
    monkeypatch,
):
    cl = ClaudeProvider()
    cl.window = object()
    calls: list[str] = []

    def fake_fetch(url, *, method="GET", body=None, headers=None, timeout=8.0):
        calls.append(url)
        if url == "/api/organizations":
            return {"ok": True, "data": [{"uuid": "org-1", "capabilities": ["chat"]}]}
        if url in ("/api/bootstrap", "/api/account"):
            return {
                "ok": True,
                "data": {"models": [{"model": "claude-opus-5-5", "name": "Opus 5.5"}]},
            }
        return {"ok": True, "status": 201, "data": {"uuid": "conv-1"}}

    monkeypatch.setattr(cl, "fetch_json", fake_fetch)
    # A label the account JSON knows maps to its real slug, even in a fresh session.
    assert cl._resolve_api_model("Opus 5.5") == "claude-opus-5-5"
    # A label only seen in the dropdown has no slug: skip the API path so the
    # page itself switches to it, instead of sending no model at all.
    assert cl._stream_turn_api("c1", "hi", model="Haiku 9 Preview") is None
    assert not any("chat_conversations" in u for u in calls)


def test_model_routes_carry_source_and_reject_unknown_ids(monkeypatch):
    from arena import providers
    from arena.web import app as web_app

    prov = _FakeProvider()
    prov.live = _live("m-1", "m-2", active="m-1")
    prov.applied = True
    monkeypatch.setitem(providers.PROVIDERS, "fake", prov)
    client = web_app.app.test_client()

    listed = client.get("/api/providers/fake/models?refresh=1").get_json()
    assert listed["source"] == "live"

    prov.live = None
    cached = client.get("/api/providers/fake/models?refresh=1").get_json()
    assert cached["source"] == "cached" and [m["id"] for m in cached["models"]] == [
        "m-1",
        "m-2",
    ]

    bad = client.post("/api/providers/fake/model", json={"model_id": "ghost"})
    assert bad.status_code == 409
    assert bad.get_json()["code"] == "unknown_model"

    good = client.post("/api/providers/fake/model", json={"model_id": "m-2"})
    assert good.status_code == 200 and good.get_json()["applied"] is True


def test_list_calls_during_a_running_check_wait_for_its_result():
    """Refresh while a check is running must not answer with the placeholder:
    the picker keeps only the newest answer, so that placeholder would win."""
    import threading
    import time as _time

    prov = _FakeProvider()
    started = threading.Event()

    def slow_probe():
        started.set()
        _time.sleep(0.3)
        return _live("claude-opus-5-5", "claude-sonnet-5-5", active="claude-opus-5-5")

    prov._probe_live_models = slow_probe
    results: list[dict] = []
    first = threading.Thread(
        target=lambda: results.append(prov.list_models(force=True))
    )
    first.start()
    assert started.wait(2)
    second = prov.list_models(force=True)  # the Refresh click
    first.join(2)

    assert second["source"] == "live"
    assert [m["id"] for m in second["models"]] == [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
    ]
    assert results and results[0]["source"] == "live"


def test_signed_out_provider_says_so_instead_of_showing_a_default_list():
    """claude.ai on its /login page: the picker must say "signed out", not offer
    the account-default placeholder as if it were the model list."""
    prov = _FakeProvider()
    prov.login_url_hints = ("/login",)
    prov.page_url = lambda: "https://fake.example/login?returnTo=/new"
    probed: list[bool] = []
    prov._probe_live_models = lambda: probed.append(True) or _live("m-1")

    res = prov.list_models(force=True)
    assert res["signed_in"] is False
    assert res["live"] is False
    assert res["source"] == "default"
    assert probed == []  # nothing to read on a sign-in page

    prov.page_url = lambda: "https://fake.example/new"
    back = prov.list_models(force=True)
    assert back["source"] == "live" and back["signed_in"] is True
