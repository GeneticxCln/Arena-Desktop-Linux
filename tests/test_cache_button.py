"""The cache endpoint and the browser's real cache call.

``/api/cache`` and ``/api/cache/clear`` imported ``arena.appcache``, which did
not exist, so both answered HTTP 500. The browser also replaced
``API.clearCache`` with a stub that reported success without asking the server.
These tests pin both halves of the fix, and the rule that a cache wipe never
touches the person's chats, cookies or settings.
"""

from __future__ import annotations

from pathlib import Path

from arena import appcache

ROOT = Path(__file__).resolve().parent.parent


def _populate(tmp_path: Path) -> Path:
    """A data folder with both cache and irreplaceable data in it."""
    (tmp_path / "google-browser").mkdir()
    (tmp_path / "google-browser" / "Cookies").write_text("x" * 2000)
    (tmp_path / "arena-browser").mkdir()
    (tmp_path / "arena-browser" / "Cookies").write_text("y" * 500)
    (tmp_path / "models_cache.json").write_text("{}")
    (tmp_path / "desktop-3.log").write_text("old run")
    # data, not cache:
    (tmp_path / "chats").mkdir()
    (tmp_path / "chats" / "chat.json").write_text('{"title": "keep me"}')
    (tmp_path / "cookies.txt").write_text("arena session")
    (tmp_path / "config.json").write_text('{"setting": 1}')
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "notes.md").write_text("my work")
    return tmp_path


def test_the_cache_wipe_reports_what_it_removed(tmp_path):
    data = _populate(tmp_path)
    result = appcache.clear(data)
    assert "google-browser" in result["removed"]
    assert "arena-browser" in result["removed"]
    assert "models_cache.json" in result["removed"]
    assert result["freed"] >= 2500
    assert result["skipped"] == []


def test_the_cache_wipe_never_touches_the_persons_data(tmp_path):
    data = _populate(tmp_path)
    appcache.clear(data)
    assert (data / "chats" / "chat.json").read_text() == '{"title": "keep me"}'
    assert (data / "cookies.txt").read_text() == "arena session"
    assert (data / "config.json").read_text() == '{"setting": 1}'
    assert (data / "workspace" / "notes.md").read_text() == "my work"


def test_a_profile_in_use_can_be_kept(tmp_path):
    data = _populate(tmp_path)
    result = appcache.clear(data, keep=("google-browser",))
    assert (data / "google-browser").exists(), (
        "a profile a live window is using must survive the wipe")
    assert "arena-browser" in result["removed"]


def test_the_summary_lists_the_cache_and_its_size(tmp_path):
    data = _populate(tmp_path)
    summary = appcache.summary(data)
    ids = [item["id"] for item in summary["items"]]
    assert "google-browser" in ids and "models_cache.json" in ids
    assert summary["bytes"] >= 2500
    # data is not cache, so it is not offered for deletion
    assert "chats" not in ids and "cookies.txt" not in ids


def test_the_live_log_is_never_deleted(tmp_path, monkeypatch):
    """Deleting the log being written stops the log for the rest of the run."""
    data = _populate(tmp_path)
    live = data / "desktop-3.log"
    live.write_text("current run")
    old = data / "desktop-4.log"
    old.write_text("old run")
    monkeypatch.setattr(appcache, "_live_log_paths",
                        lambda: {str(live).lower()})
    result = appcache.clear(data)
    assert live.exists(), "the current run's log was deleted"
    assert not old.exists()
    assert "logs" in result["removed"]


def test_clearing_a_missing_folder_is_not_an_error(tmp_path):
    result = appcache.clear(tmp_path / "does-not-exist")
    assert result["removed"] == [] and result["skipped"] == []


def test_cache_endpoints_report_and_clear(tmp_path, monkeypatch):
    """Both routes must answer, and the clear must keep the live profile."""
    from arena.web import app as web

    monkeypatch.setattr(web, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(web, "_cache_browser_profiles",
                        lambda: ("google-browser",))
    (tmp_path / "google-browser").mkdir()
    (tmp_path / "google-browser" / "Cookies").write_text("g" * 100)
    (tmp_path / "arena-browser").mkdir()
    (tmp_path / "arena-browser" / "Cookies").write_text("a" * 2000)
    (tmp_path / "models_cache.json").write_text("{}")
    (tmp_path / "chats").mkdir()
    (tmp_path / "chats" / "keep.json").write_text("keep me")

    client = web.app.test_client()
    listing = client.get("/api/cache")
    assert listing.status_code == 200
    data = listing.get_json()
    ids = {item["id"] for item in data["items"]}
    assert {"google-browser", "arena-browser", "models_cache.json"} <= ids
    assert data["bytes"] >= 2100

    web._update_cache["stable"] = {"t": 123.0, "data": {"x": 1}}
    web._GOOGLE_HTTP_ACCOUNT.update(key="stale", at=123.0,
                                    info={"signed_in": True})

    cleared = client.post("/api/cache/clear")
    assert cleared.status_code == 200
    result = cleared.get_json()
    assert "arena-browser" in result["removed"]
    assert "google-browser" not in result["removed"]
    assert result["freed"] >= 2000
    assert (tmp_path / "google-browser").exists()
    assert not (tmp_path / "arena-browser").exists()
    assert (tmp_path / "chats" / "keep.json").read_text() == "keep me"
    # In-process caches must be invalidated with the files.
    assert web._update_cache == {}
    assert web._GOOGLE_HTTP_ACCOUNT["at"] == 0.0
    assert web._GOOGLE_HTTP_ACCOUNT["key"] is None


def test_the_browser_calls_the_real_cache_routes():
    """A stub that answers success without a request is worse than no button."""
    js = (ROOT / "arena" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "clearCache() { return this.request('/api/cache/clear'" in js
    assert "getCache() { return this.request('/api/cache'" in js
    assert "API.clearCache = API_clearCache" not in js
    assert "async function API_clearCache" not in js
    assert "detail: 'Cache cleared'" not in js
    # The settings section is real and uses the live endpoints.
    assert "function renderCachePanel" in js
    assert "API.clearCache()" in js and "API.getCache()" in js
    assert "settingsClearCacheBtn" in js
    assert "if (tab === 'cache') return renderCachePanel();" in js
    assert js.count("id: 'cache', label: 'Cache', icon: 'trash'") == 2
