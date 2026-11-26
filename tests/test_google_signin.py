"""Regression tests for Google sign-in completion and account expiry.

Two failures are pinned here:

* signing in on the Linux build ended without the cookie jar being saved, so
  the app knew the email but every HTTPS turn answered "no session";
* one dead Google turn marked *every* saved account expired when there was no
  current account.

The engine tests use stand-in windows: they assert the wiring (show before
wipe, export before save, callback after detection), not Google's live UI.
"""

from __future__ import annotations

import json
import threading

from arena.google_ai import GoogleEngine
from arena.web import app as web

COOKIE = "SID=sid-value; __Secure-1PSID=psid-value; HSID=hsid-value"


def _store(tmp_path, monkeypatch, current, accounts):
    path = tmp_path / "google_accounts.json"
    monkeypatch.setattr(web, "GOOGLE_ACCOUNTS_FILE", lambda: path)
    path.write_text(json.dumps({"current": current, "accounts": accounts}),
                    encoding="utf-8")
    return path


def _accounts(path):
    return {item["id"]: item
            for item in json.loads(path.read_text(encoding="utf-8"))["accounts"]}


# -- sign-in completion ----------------------------------------------------

def test_signed_in_window_saves_the_exported_cookie(tmp_path, monkeypatch):
    """The account is only usable if the jar is saved with it."""
    path = _store(tmp_path, monkeypatch, None, [])

    class Engine:
        closed = False

        def cookies(self, tries=1):
            return COOKIE

        def close_sign_in_window(self):
            Engine.closed = True

    engine = Engine()
    monkeypatch.setattr(web, "GOOGLE_ENGINE", engine)

    web._google_on_signed_in({"signed_in": True, "email": "user@example.com",
                              "name": "User"})

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["current"] == data["accounts"][0]["id"]
    record = data["accounts"][0]
    assert record["email"] == "user@example.com"
    assert record["cookie"] == COOKIE
    assert record["verified"] is True
    assert record["added_by"] == "window"
    assert Engine.closed is True, "the single-use sign-in window was not closed"


def test_sign_in_with_no_cookie_is_saved_but_marked_cookieless(tmp_path, monkeypatch):
    """A window that exported nothing must not look like a usable account."""
    path = _store(tmp_path, monkeypatch, None, [])
    monkeypatch.setattr(web.time, "sleep", lambda _seconds: None)

    class Engine:
        def cookies(self, tries=1):
            return ""

        def close_sign_in_window(self):
            pass

    monkeypatch.setattr(web, "GOOGLE_ENGINE", Engine())

    web._google_on_signed_in({"signed_in": True, "email": "user@example.com",
                              "name": "User"})

    record = _accounts(path)[json.loads(path.read_text())["accounts"][0]["id"]]
    assert record["email"] == "user@example.com"
    assert "cookie" not in record
    assert web._google_has_auth_cookie(record.get("cookie", "")) is False


def test_open_sign_in_shows_before_wiping_and_navigating():
    """pywebview gates clear/load on `shown`; the wrong order discarded both."""
    engine = GoogleEngine()
    engine.window = object()
    engine._window_is_live = lambda: False
    calls = []

    def show():
        calls.append("show")
        engine._shown_evt.set()

    engine.show = show
    engine.clear_cookies = lambda: (calls.append("clear"), True)
    engine.load = lambda url: calls.append(("load", url))

    engine.open_sign_in(fresh=True)

    assert calls[0] == "show"
    assert calls[1] == "clear"
    assert calls[2][0] == "load" and "google.com" in calls[2][1]


def test_watch_sign_in_reports_the_account_once():
    engine = GoogleEngine()
    engine.window = object()
    engine.ready = lambda: True
    engine.account = lambda: {"signed_in": True, "email": "user@example.com"}
    seen = []
    hit = threading.Event()

    def on_signed(info):
        seen.append(info)
        hit.set()

    try:
        assert engine.watch_sign_in(on_signed, interval=0.01, timeout=2.0)
        assert hit.wait(1.0), "the sign-in watcher never reported the session"
        assert seen and seen[0]["email"] == "user@example.com"
    finally:
        engine.stop_watch()


# -- account expiry --------------------------------------------------------

def test_one_dead_turn_only_expires_the_account_in_use(tmp_path, monkeypatch):
    path = _store(tmp_path, monkeypatch, "g1", [
        {"id": "g1", "email": "one@x.com", "cookie": "SID=1"},
        {"id": "g2", "email": "two@x.com", "cookie": "SID=2"},
    ])

    web._google_flag_account("", reason="dead session")

    accounts = _accounts(path)
    assert accounts["g1"].get("expired") is True
    assert "expired" not in accounts["g2"], (
        "a failed turn for the current account expired an unrelated one")


def test_no_current_account_falls_back_to_the_first_with_a_cookie(tmp_path, monkeypatch):
    path = _store(tmp_path, monkeypatch, None, [
        {"id": "g1", "email": "one@x.com"},                 # no session
        {"id": "g2", "email": "two@x.com", "cookie": "SID=2"},
        {"id": "g3", "email": "three@x.com", "cookie": "SID=3"},
    ])

    web._google_flag_account("", reason="dead session")

    accounts = _accounts(path)
    assert "expired" not in accounts["g1"]
    assert accounts["g2"].get("expired") is True
    assert "expired" not in accounts["g3"], (
        "the fallback flagged more than the account that answers")


def test_explicit_account_is_the_only_one_flagged(tmp_path, monkeypatch):
    path = _store(tmp_path, monkeypatch, "g1", [
        {"id": "g1", "email": "one@x.com", "cookie": "SID=1"},
        {"id": "g2", "email": "two@x.com", "cookie": "SID=2"},
    ])

    web._google_flag_account("g2", reason="dead session")

    accounts = _accounts(path)
    assert "expired" not in accounts["g1"]
    assert accounts["g2"].get("expired") is True


def test_unknown_explicit_account_flags_nobody(tmp_path, monkeypatch):
    path = _store(tmp_path, monkeypatch, "g1", [
        {"id": "g1", "email": "one@x.com", "cookie": "SID=1"},
        {"id": "g2", "email": "two@x.com", "cookie": "SID=2"},
    ])

    web._google_flag_account("missing", reason="dead session")

    accounts = _accounts(path)
    assert "expired" not in accounts["g1"] and "expired" not in accounts["g2"]


def test_clearing_the_mark_touches_only_the_target(tmp_path, monkeypatch):
    path = _store(tmp_path, monkeypatch, "g1", [
        {"id": "g1", "email": "one@x.com", "expired": True,
         "expired_reason": "old", "expired_at": 1},
        {"id": "g2", "email": "two@x.com", "expired": True,
         "expired_reason": "old", "expired_at": 1},
    ])

    web._google_flag_account("g1", ok=True)

    accounts = _accounts(path)
    assert "expired" not in accounts["g1"]
    assert "expired_reason" not in accounts["g1"]
    assert accounts["g2"].get("expired") is True, (
        "clearing one account's mark cleared another's")


def test_flagging_with_no_accounts_is_a_noop(tmp_path, monkeypatch):
    path = _store(tmp_path, monkeypatch, None, [])
    before = path.read_text(encoding="utf-8")

    web._google_flag_account("", reason="dead session")

    assert path.read_text(encoding="utf-8") == before
