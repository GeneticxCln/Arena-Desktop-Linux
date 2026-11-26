"""Freeze diagnostics: the module must exist, record events and dump stacks.

The app's diagnostic hooks and the cache wipe both import ``arena.diag``. When
the module was missing, every hook silently no-op'd and the cache wipe had no
way to know which log was being written right now.
"""

from __future__ import annotations

import logging

from arena import appcache, diag
from arena.web import app as web


def test_enabled_defaults_on_and_honors_the_switch(monkeypatch):
    monkeypatch.delenv("ARENA_DIAG", raising=False)
    assert diag.enabled() is True
    monkeypatch.setenv("ARENA_DIAG", "0")
    assert diag.enabled() is False


def test_note_writes_a_breadcrumb(caplog, monkeypatch):
    monkeypatch.delenv("ARENA_DIAG", raising=False)
    with caplog.at_level(logging.INFO, logger="arena.desktop"):
        diag.note("sign-in clicked")
    assert "sign-in clicked" in caplog.text


def test_dump_threads_writes_every_thread_stack(tmp_path, monkeypatch):
    monkeypatch.setattr("arena.config.CONFIG_DIR", tmp_path)
    diag.dump_threads("unit freeze")
    text = (tmp_path / "desktop-trace.log").read_text(encoding="utf-8")
    assert "THREAD DUMP: unit freeze" in text
    assert "MainThread" in text


def test_diag_dump_endpoint_runs_a_dump(monkeypatch):
    seen = []
    monkeypatch.setattr(diag, "dump_threads", lambda reason: seen.append(reason))
    client = web.app.test_client()
    response = client.post("/api/diag/dump?reason=ui-froze")
    assert response.status_code == 200
    assert seen == ["ui-froze"]
    assert response.get_json()["ok"] is True


def test_diag_endpoint_records_ui_events(monkeypatch):
    seen = []
    monkeypatch.setattr(diag, "note", lambda message, level=None: seen.append(message))
    client = web.app.test_client()
    response = client.post("/api/diag", json={"message": "click sign-in", "kind": "info"})
    assert response.status_code == 200
    assert any("[ui] click sign-in" in message for message in seen)


def test_live_trace_log_is_kept_by_cache_clear(tmp_path, monkeypatch):
    monkeypatch.setattr("arena.config.CONFIG_DIR", tmp_path)
    live = tmp_path / "desktop-trace.log"
    live.write_text("stack dump", encoding="utf-8")
    old = tmp_path / "desktop-4.log"
    old.write_text("old run", encoding="utf-8")

    appcache.clear(tmp_path)

    assert live.exists(), "the diagnostics dump file was deleted while in use"
    assert not old.exists()


class _Window:
    is_browser = False
    native = None

    def __init__(self):
        self.calls = []

    def show(self):
        self.calls.append("show")

    def hide(self):
        self.calls.append("hide")

    def load_url(self, url):
        self.calls.append(("load", url))


def test_nonblocking_window_calls_still_reach_the_window(monkeypatch):
    """A wrapper with no WinForms host must call through, never drop the call."""
    monkeypatch.delenv("ARENA_DIAG", raising=False)
    window = _Window()
    diag.make_window_calls_non_blocking(window)
    window.show()
    window.hide()
    window.load_url("https://example.test/")
    assert window.calls == ["show", "hide", ("load", "https://example.test/")]


def test_browser_windows_keep_their_own_methods(monkeypatch):
    monkeypatch.delenv("ARENA_DIAG", raising=False)
    window = _Window()
    window.is_browser = True
    diag.make_window_calls_non_blocking(window)
    window.show()
    assert window.calls == ["show"]
    assert "show" not in window.__dict__
