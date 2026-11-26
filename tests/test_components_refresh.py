"""Regression tests for Linux component detection, cache refresh, and cloudflared install."""
import hashlib
import stat

from arena import components


def test_cloudflared_found_on_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    fake = tool_dir / "cloudflared"
    fake.write_bytes(b"fake")
    fake.chmod(0o755)
    monkeypatch.setattr(components, "BIN_DIR", bindir)
    monkeypatch.setenv("PATH", str(tool_dir))
    assert components.cloudflared_path() == fake
    entry = next(c for c in components.detect() if c["id"] == "cloudflared")
    assert entry["installed"] is True
    assert entry["path"] == str(fake)


def test_app_data_cloudflared_wins_over_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    own = bindir / "cloudflared"
    own.write_bytes(b"own")
    own.chmod(0o755)
    monkeypatch.setattr(components, "BIN_DIR", bindir)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    assert components.cloudflared_path() == own


def test_detect_refresh_drops_version_cache(tmp_path, monkeypatch):
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    node = tool_dir / "node"
    node.write_text("#!/bin/sh\necho v99.0.0\n")
    node.chmod(0o755)
    monkeypatch.setenv("PATH", str(tool_dir))
    components._version_cache.clear()
    components._version_cache["node"] = "v0-stale"
    stale = next(c for c in components.detect() if c["id"] == "node")
    assert stale["installed"] is True and stale["version"] == "v0-stale"
    fresh = next(c for c in components.detect(refresh=True) if c["id"] == "node")
    assert fresh["installed"] is True and fresh["version"] != "v0-stale"


def test_broken_app_copy_does_not_shadow_working_system_cloudflared(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    broken = bindir / "cloudflared"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(broken.stat().st_mode | stat.S_IEXEC)
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    good = tool_dir / "cloudflared"
    good.write_text("#!/bin/sh\necho 'cloudflared version 9.9.9'\n")
    good.chmod(good.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(components, "BIN_DIR", bindir)
    monkeypatch.setenv("PATH", str(tool_dir))
    components._version_cache.clear()
    assert components.cloudflared_path() == good


def test_linux_components_detect_python3_webkitgtk_and_download_cloudflared(tmp_path, monkeypatch):
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    py3 = tool_dir / "python3"
    py3.write_text("#!/bin/sh\necho 'Python 3.14.0'\n")
    py3.chmod(0o755)
    monkeypatch.setattr(components, "BIN_DIR", tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tool_dir))
    components._version_cache.clear()
    by_id = {c["id"]: c for c in components.detect(refresh=True)}
    assert set(by_id) == {"cloudflared", "node", "python", "webkitgtk"}
    assert by_id["python"]["installed"] is True
    assert by_id["cloudflared"]["install_kind"] == "download"
    assert by_id["webkitgtk"]["installed"] is True

    # Verify Linux cloudflared download checks SHA-256 and sets executable mode.
    payload = b"#!/bin/sh\necho 'cloudflared version 2026.10.6'\n"
    arch = components._host_architecture()
    monkeypatch.setitem(
        components.CLOUDFLARED_LINUX_ASSETS,
        arch,
        ("https://example.invalid/cloudflared-linux", hashlib.sha256(payload).hexdigest(), len(payload)),
    )
    monkeypatch.setattr(components, "_download", lambda url, dest, cid: dest.write_bytes(payload))
    components._install_cloudflared()
    installed = tmp_path / "bin" / "cloudflared"
    assert installed.read_bytes() == payload
    assert bool(installed.stat().st_mode & stat.S_IXUSR)
    assert components.install_status()["jobs"]["cloudflared"]["state"] == "done"
