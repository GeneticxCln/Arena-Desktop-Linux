"""Optional external components on Linux: detection + guided installation.

The app itself is a single Linux ELF binary and needs none of these for normal
chats. They unlock specific features and are installed only on explicit request:

- cloudflared  - Agent Gateway (temporary Cloudflare Quick Tunnel).
                 Official Linux static binary (x86_64 / arm64), version +
                 SHA-256 pinned below, verified after download, kept in
                 ~/.arena-personal/bin with mode 0755.
- Node.js      - local MCP servers started with npx.
- Python       - local MCP servers run as Python scripts.
- WebKitGTK    - system WebKit2GTK 4.1 runtime powering the app window.

Nothing here ever downloads or runs anything silently.
"""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from arena.config import CONFIG_DIR

BIN_DIR = CONFIG_DIR / "bin"

CLOUDFLARED_RELEASES_URL = "https://github.com/cloudflare/cloudflared/releases/latest"
CLOUDFLARED_LINUX_VERSION = "2026.10.6"
CLOUDFLARED_LINUX_ASSETS = {
    "x64": (
        f"https://github.com/cloudflare/cloudflared/releases/download/"
        f"{CLOUDFLARED_LINUX_VERSION}/cloudflared-linux-amd64",
        "1d356dac769d0dbf62ebe588c4034824bbff98d5790c2f844cc99e0e2f57462d",
        39 * 1048576,
    ),
    "arm64": (
        f"https://github.com/cloudflare/cloudflared/releases/download/"
        f"{CLOUDFLARED_LINUX_VERSION}/cloudflared-linux-arm64",
        "0ae0e179475b2c77651a718d2b376e0151f4fca5954702a84f02843a4d5fcac0",
        38 * 1048576,
    ),
}

NODE_DOWNLOAD_URL = "https://nodejs.org/en/download"
PYTHON_DOWNLOAD_URL = "https://www.python.org/downloads/"

_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}
_version_cache: Dict[str, Any] = {}


def _host_architecture() -> str:
    raw = (platform.machine() or "").lower()
    if raw in ("arm64", "aarch64"):
        return "arm64"
    if raw in ("amd64", "x86_64", "x64"):
        return "x64"
    if raw in ("x86", "i386", "i686"):
        return "x86"
    return raw or "unknown"


def _cloudflared_asset() -> Optional[tuple[str, str, int, str]]:
    """Return (url, sha256, approx_size, filename) for this Linux architecture, or None."""
    entry = CLOUDFLARED_LINUX_ASSETS.get(_host_architecture())
    if not entry:
        return None
    url, sha, size = entry
    return (url, sha, size, "cloudflared")


def _search_path() -> Optional[str]:
    return os.environ.get("PATH") or None


def _which(name: str) -> Optional[str]:
    return shutil.which(name, path=_search_path())


def cloudflared_path() -> Optional[Path]:
    candidates: List[Path] = []
    candidate = BIN_DIR / "cloudflared"
    try:
        if candidate.is_file() and candidate.stat().st_size > 0:
            candidates.append(candidate)
    except OSError:
        pass
    found = _which("cloudflared")
    if found and Path(found) not in candidates:
        candidates.append(Path(found))
    if not candidates:
        return None
    for path in candidates:
        if _run_version([str(path), "--version"], f"cloudflared:{path}"):
            return path
    return candidates[0]


def _run_version(args: List[str], cache_key: str) -> Optional[str]:
    if cache_key in _version_cache:
        return _version_cache[cache_key]
    version = None
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=8)
        text = (proc.stdout or proc.stderr or "").strip().splitlines()
        if text:
            version = text[0].strip()[:80]
    except Exception:
        version = None
    _version_cache[cache_key] = version
    return version


def _webkitgtk_version() -> Optional[str]:
    if "webkitgtk" in _version_cache:
        return _version_cache["webkitgtk"]
    version = None
    try:
        import gi
        gi.require_version("WebKit2", "4.1")
        from gi.repository import WebKit2
        version = f"{WebKit2.get_major_version()}.{WebKit2.get_minor_version()}.{WebKit2.get_micro_version()}"
    except Exception:
        version = None
    _version_cache["webkitgtk"] = version
    return version


def detect(refresh: bool = False) -> List[Dict[str, Any]]:
    """Current status of every known Linux component."""
    if refresh:
        _version_cache.clear()
    cf = cloudflared_path()
    cf_version = _run_version([str(cf), "--version"], f"cloudflared:{cf}") if cf else None
    node = _which("node")
    python = _which("python3") or _which("python")
    webkitgtk = _webkitgtk_version()
    host_arch = _host_architecture()
    cf_asset = _cloudflared_asset()
    if cf_asset is not None:
        cloudflared_note = ("Single static binary downloaded into ~/.arena-personal/bin with "
                            "SHA-256 verification (no root needed), or install via your distro package manager.")
    else:
        cloudflared_note = ("Install cloudflared via your distribution package manager or download "
                            "the official Linux binary from Cloudflare releases, then click Check again.")
    return [
        {
            "id": "cloudflared", "name": "cloudflared (Cloudflare Tunnel)",
            "purpose": "Agent Gateway: the temporary secure tunnel that lets the Arena "
                       "Agent reach MCP servers on this computer.",
            "installed": cf is not None,
            "version": (cf_version or "").replace("cloudflared version ", "") or None,
            "path": str(cf) if cf else None,
            "install_kind": "download" if cf_asset is not None else "link",
            "size_mb": round((cf_asset[2] if cf_asset else 39 * 1048576) / 1048576),
            "source": "github.com/cloudflare/cloudflared (official release, SHA-256 verified)"
                      if cf_asset is not None else "github.com/cloudflare/cloudflared (official releases)",
            "host_architecture": host_arch,
            "binary_architecture": host_arch,
            "note": cloudflared_note,
        },
        {
            "id": "node", "name": "Node.js",
            "purpose": "Local MCP servers installed with npx (most presets).",
            "installed": node is not None,
            "version": _run_version([node, "--version"], "node") if node else None,
            "path": node,
            "install_kind": "link",
            "source": "nodejs.org (or your distro package manager)",
            "note": "Only needed if you add npx-based MCP servers.",
        },
        {
            "id": "python", "name": "Python",
            "purpose": "Local MCP servers that run as Python scripts.",
            "installed": python is not None,
            "version": _run_version([python, "--version"], "python") if python else None,
            "path": python,
            "install_kind": "link",
            "source": "python.org (or your distro package manager)",
            "note": "The app carries its own Python internally; user MCP scripts "
                    "use the system one.",
        },
        {
            "id": "webkitgtk", "name": "WebKitGTK 4.1",
            "purpose": "Renders the app window and the arena.ai session window.",
            "installed": webkitgtk is not None,
            "version": webkitgtk,
            "path": None,
            "install_kind": "unsupported",
            "source": "System package (webkit2gtk-4.1 / gir1.2-webkit2-4.1)",
            "note": "Provided by your Linux distribution package manager.",
        },
    ]


def install_status() -> Dict[str, Any]:
    with _lock:
        return {"jobs": {cid: dict(job) for cid, job in _jobs.items()}}


def _set_job(cid: str, **updates: Any) -> None:
    with _lock:
        job = _jobs.setdefault(cid, {"id": cid, "state": "idle", "progress": 0, "error": None})
        job.update(updates)


def _download(url: str, dest: Path, cid: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "ArenaDesktop-Components"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total:
                    _set_job(cid, progress=min(99, int(done * 100 / total)))


def _install_cloudflared() -> None:
    cid = "cloudflared"
    try:
        asset = _cloudflared_asset()
        if asset is None:
            _set_job(cid, state="error", progress=0,
                     error="Automatic cloudflared download is not available for this architecture.")
            return
        url, expected_sha, _size, filename = asset
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        tmp = BIN_DIR / "cloudflared.download"
        _download(url, tmp, cid)
        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
        if digest != expected_sha:
            tmp.unlink(missing_ok=True)
            _set_job(cid, state="error", progress=0,
                     error="Downloaded file failed SHA-256 verification and was deleted.")
            return
        target = BIN_DIR / filename
        os.replace(tmp, target)
        os.chmod(target, 0o755)
        for key in [k for k in _version_cache if k.startswith("cloudflared")]:
            _version_cache.pop(key, None)
        _set_job(cid, state="done", progress=100, error=None)
    except Exception as e:
        _set_job(cid, state="error", progress=0, error=f"{type(e).__name__}: {e}")


def start_install(cid: str) -> Dict[str, Any]:
    """Kick off installation; returns immediately with the job + optional URL action."""
    components = {c["id"]: c for c in detect()}
    comp = components.get(cid)
    if comp is None:
        return {"ok": False, "error": f"Unknown component: {cid}"}
    if comp["installed"]:
        return {"ok": True, "already": True}
    kind = comp["install_kind"]
    if kind == "link":
        if cid == "cloudflared":
            url = CLOUDFLARED_RELEASES_URL
        elif cid == "node":
            url = NODE_DOWNLOAD_URL
        else:
            url = PYTHON_DOWNLOAD_URL
        return {"ok": True, "open_url": url}
    if kind == "unsupported":
        return {"ok": False, "error": "Install this system component via your Linux package manager."}
    with _lock:
        current = _jobs.get(cid) or {}
        if current.get("state") == "downloading":
            return {"ok": True, "already": True}
        _jobs[cid] = {"state": "downloading", "progress": 0, "error": None}
    if cid == "cloudflared":
        threading.Thread(target=_install_cloudflared, daemon=True).start()
    else:
        _set_job(cid, state="idle", progress=0, error=None)
        return {"ok": False, "error": f"No installer for: {cid}"}
    return {"ok": True}
