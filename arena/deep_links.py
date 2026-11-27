"""Safe ``arenadesktop://`` links and running-instance handoff (Linux XDG).

Supported links intentionally do very little:

- ``arenadesktop://open``  - show Arena Desktop
- ``arenadesktop://new``   - open a new, empty chat
- ``arenadesktop://ask?q=...`` - put text in a new chat's composer
- ``arenadesktop://chat?id=<uuid>`` - open an existing local chat

An ``ask`` link never sends the message.  Any application can invoke a custom
XDG URI scheme, so following a link must not spend Arena usage or start tool
work without the user pressing Send in Arena Desktop.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from arena.config import CONFIG_DIR

SCHEME = "arenadesktop"
ACTIONS = {"open", "new", "ask", "chat"}
MAX_ASK_CHARS = 10000
_INSTANCE_FILE = CONFIG_DIR / "instance.json"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def parse_deep_link(raw: Any) -> Dict[str, Any]:
    """Validate and normalize one deep link, or raise ``ValueError``."""
    if isinstance(raw, dict):
        action = str(raw.get("action") or "").strip().lower()
        params = {str(k): [str(v)] for k, v in raw.items() if k != "action"}
    else:
        text = str(raw or "").strip()
        if not text or len(text) > MAX_ASK_CHARS + 4096:
            raise ValueError("invalid Arena Desktop link")
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme.lower() != SCHEME:
            raise ValueError("invalid Arena Desktop link scheme")
        action = (parsed.netloc or parsed.path.lstrip("/").split("/", 1)[0]).lower()
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if action == "chat" and parsed.netloc.lower() == "chat" and parsed.path:
            candidate = parsed.path.strip("/")
            if candidate and "id" not in params:
                params["id"] = [candidate]
    if action not in ACTIONS:
        raise ValueError("unsupported Arena Desktop link")

    def one(*names: str) -> str:
        for name in names:
            values = params.get(name)
            if values:
                return str(values[0])
        return ""

    if action in ("open", "new"):
        return {"action": action}
    if action == "ask":
        text = one("q", "text", "prompt").strip()
        if not text:
            raise ValueError("ask link needs q=<text>")
        if len(text) > MAX_ASK_CHARS:
            raise ValueError("ask link text is too long")
        return {"action": "ask", "text": text}

    chat_id = one("id", "chat_id", "chatId").strip()
    if not _UUID_RE.match(chat_id):
        raise ValueError("chat link needs a valid chat id")
    result: Dict[str, Any] = {"action": "chat", "chat_id": chat_id}
    msg = one("msg", "msg_index", "message").strip()
    if msg:
        try:
            index = int(msg)
        except ValueError as exc:
            raise ValueError("chat link message index is invalid") from exc
        if index < 0 or index > 1000000:
            raise ValueError("chat link message index is invalid")
        result["msg_index"] = index
    return result


def find_deep_link_arg(argv: Iterable[str]) -> Optional[str]:
    for value in argv:
        if isinstance(value, str) and value.lower().startswith(f"{SCHEME}://"):
            return value
    return None


def instance_path() -> Path:
    return _INSTANCE_FILE


def new_instance_token() -> str:
    return secrets.token_urlsafe(32)


def write_instance(port: int, token: str) -> None:
    """Publish this process as the deep-link target (atomic replace)."""
    _INSTANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "port": int(port),
        "pid": os.getpid(),
        "token": str(token),
        "executable": sys.executable,
    }
    tmp = _INSTANCE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, _INSTANCE_FILE)


def read_instance() -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(_INSTANCE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        data["port"] = int(data.get("port") or 0)
        data["pid"] = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if not (0 < data["port"] < 65536 and data["pid"] > 0 and data.get("token")):
        return None
    return data


def pid_alive(pid: int) -> bool:
    if not pid or pid == os.getpid():
        return pid == os.getpid()
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def forward_deep_link(raw: str) -> bool:
    """Send a validated link to the already-running app, if there is one."""
    try:
        payload = parse_deep_link(raw)
    except ValueError:
        return False
    instance = read_instance()
    if not instance or not pid_alive(int(instance["pid"])):
        return False
    try:
        import requests

        response = requests.post(
            f"http://127.0.0.1:{instance['port']}/api/deep-link",
            json={"link": payload},
            headers={"X-Arena-Instance-Token": str(instance["token"])},
            timeout=3,
        )
        return bool(response.ok and (response.json() or {}).get("ok"))
    except Exception:
        return False


def _desktop_exec_quote(path: str) -> str:
    """Quote one Exec argument as the Desktop Entry spec requires."""
    escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def register_protocol(executable: Optional[str] = None) -> bool:
    """Register ``arenadesktop://`` for the current Linux user via XDG desktop entry.

    Source runs do not register anything; only frozen binaries do.
    """
    if not getattr(sys, "frozen", False):
        return False
    return _register_linux_protocol(executable)


def _register_linux_protocol(executable: Optional[str] = None) -> bool:
    # For AppImages, sys.executable lives inside ephemeral /tmp/.mount_*; use $APPIMAGE instead.
    appimage = (os.environ.get("APPIMAGE") or "").strip()
    raw_exe = executable or appimage or sys.executable
    exe = str(Path(raw_exe).resolve())
    try:
        data_home = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        apps = data_home / "applications"
        apps.mkdir(parents=True, exist_ok=True)
        desktop = apps / "arena-desktop.desktop"
        # Respect an already-installed system package entry (/usr/share/applications) or valid user entry.
        sys_desktop = Path("/usr/share/applications/arena-desktop.desktop")
        if executable is None and sys_desktop.is_file() and not desktop.exists():
            return True
        if executable is None and desktop.is_file():
            try:
                existing_text = desktop.read_text(encoding="utf-8")
                if "/tmp/.mount_" not in existing_text and f"x-scheme-handler/{SCHEME}" in existing_text:
                    return True
            except OSError:
                pass
        if "/tmp/.mount_" in exe and not executable:
            return False
        desktop.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Arena Desktop\n"
            "Comment=Unofficial desktop client for arena.ai\n"
            f"Exec={_desktop_exec_quote(exe)} %u\n"
            "Icon=arena-desktop\n"
            "Terminal=false\n"
            f"MimeType=x-scheme-handler/{SCHEME};\n"
            "Categories=Network;Utility;\n"
            "StartupWMClass=Arena Desktop\n",
            encoding="utf-8",
        )
        for args in (
            ["xdg-mime", "default", desktop.name, f"x-scheme-handler/{SCHEME}"],
            ["update-desktop-database", str(apps)],
        ):
            tool = shutil.which(args[0])
            if tool:
                try:
                    subprocess.run([tool, *args[1:]], check=False, timeout=20,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
        return True
    except Exception:
        return False
