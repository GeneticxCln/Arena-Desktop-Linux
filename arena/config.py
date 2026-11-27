"""
Configuration, paths, and persistent settings for Arena CLI.
"""

import json
import os
import sys
import atexit
import threading
import uuid
from pathlib import Path
from typing import Dict, Any, Optional


def _config_dir() -> Path:
    """Portable mode: a portable.txt next to the EXE (or the repo root when
    running from source) keeps all data in ./data beside it; otherwise the
    per-user folder is used, as always."""
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(__file__).resolve().parent.parent)
    for root in roots:
        if (root / "portable.txt").exists():
            return root / "data"
    return Path.home() / ".arena-personal"


# Keep this personal fork's credentials and chats separate from the upstream app.
CONFIG_DIR = _config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
COOKIE_FILE = CONFIG_DIR / "cookies.txt"
HIST_FILE = CONFIG_DIR / "history.txt"
PID_FILE = CONFIG_DIR / "arena.pid"
CHATS_DIR = CONFIG_DIR / "chats"

BASE_URL = "https://arena.ai"
API_PREFIX = "/nextjs-api"


class UserAbort(Exception):
    """Raised when user presses Esc or Ctrl-C to cancel/go back."""
    pass


def ensure_config_dir() -> Path:
    """Ensure the per-user config folder exists with owner-only permissions."""
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(CONFIG_DIR, 0o700)
        except OSError:
            pass
    return CONFIG_DIR


def restrict_permissions(path: Path, mode: int = 0o600) -> None:
    """Credential files are owner-only on POSIX (DPAPI is Windows-only)."""
    if os.name == "posix":
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Atomically write text to *path* with *mode* applied at file creation (no 0644 TOCTOU)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(tmp), flags, mode)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        restrict_permissions(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


_CONFIG_LOCK = threading.RLock()


def _cfg_lock():
    return _CONFIG_LOCK


def load_config() -> Dict[str, Any]:
    """Load configuration from disk safely under a process lock."""
    with _cfg_lock():
        ensure_config_dir()
        if not CONFIG_FILE.exists():
            return {}
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}


def save_config(cfg: Dict[str, Any]) -> None:
    """Atomically save configuration to disk (temp file + os.replace) under a lock."""
    with _cfg_lock():
        ensure_config_dir()
        payload = json.dumps(cfg, indent=2)
        atomic_write_text(CONFIG_FILE, payload, mode=0o600)


def update_config(mutator) -> Dict[str, Any]:
    """Atomic read-modify-write helper for config.json."""
    with _cfg_lock():
        cfg = load_config()
        mutator(cfg)
        save_config(cfg)
        return cfg


def get_cookie() -> Optional[str]:
    """Retrieve the stored cookie, migrating legacy plaintext on Windows."""
    try:
        if COOKIE_FILE.exists():
            stored = COOKIE_FILE.read_text(encoding="utf-8").strip()
            if not stored:
                return None
            from arena import crypto
            val = crypto.decode_cookie(stored)
            if not val:
                return None
            # One-way, opportunistic migration: a readable plaintext file is
            # rewritten in protected form the first time it is loaded.
            if crypto.protection_available() and not crypto.is_protected(stored):
                try:
                    save_cookie(val)
                except Exception:
                    pass
            return val
    except Exception:
        pass
    return None


def save_cookie(val: str) -> None:
    """Save a cookie string (protected for the current user, owner-only 0600)."""
    ensure_config_dir()
    from arena import crypto
    atomic_write_text(COOKIE_FILE, crypto.protect_text(val.strip()), mode=0o600)


def clear_cookie() -> None:
    """Remove saved cookie file."""
    try:
        COOKIE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def write_pid() -> None:
    """Record current process PID."""
    ensure_config_dir()
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def cleanup_pid() -> None:
    """Remove PID file on clean exit only if it belongs to this process."""
    try:
        if PID_FILE.exists():
            raw = PID_FILE.read_text(encoding="utf-8").strip()
            if raw == str(os.getpid()):
                PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


atexit.register(cleanup_pid)


DEFAULT_PERSONA = (
    "You are a helpful AI coding assistant powered by Arena.ai. "
    "You write clean, efficient code and explain your reasoning clearly. "
    "When editing files, you use the available tools (read_file, write_file, edit_file). "
    "Be concise and direct."
)

DEFAULT_SETTINGS = {
    "persona": DEFAULT_PERSONA,
    "theme": "arena",  # arena | dark | light
    "show_reasoning": False,
    "auto_copy": True,
    "max_history": 20,
    "cdp_port": 9222,
}


def get_settings() -> Dict[str, Any]:
    """Get merged settings (defaults + user overrides)."""
    cfg = load_config()
    settings = dict(DEFAULT_SETTINGS)
    if "settings" in cfg:
        settings.update(cfg["settings"])
    return settings


def save_settings(settings: Dict[str, Any]) -> None:
    """Save settings to config atomically."""
    update_config(lambda cfg: cfg.__setitem__("settings", settings))

