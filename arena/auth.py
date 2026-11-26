"""
Authentication and cookie management for Arena CLI (Linux).
Supports Brave, Edge, Chrome, Chromium, Firefox, Opera, Vivaldi,
Wayland/X11 clipboard detection, and smart cookie parsing.
"""

import base64
import json
import os
import re
import shutil
import subprocess
from typing import Dict, Optional, Tuple, Any

from arena.console import con


def detect_installed_browsers() -> Dict[str, str]:
    """Detect installed browsers on Linux.

    Returns dict mapping friendly name -> executable path.
    """
    candidates = {
        "Brave": [
            "/usr/bin/brave-browser",
            "/usr/bin/brave",
            "/opt/brave.com/brave/brave",
        ],
        "Microsoft Edge": [
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
        ],
        "Google Chrome": [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/opt/google/chrome/chrome",
        ],
        "Chromium": [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ],
        "Mozilla Firefox": [
            "/usr/bin/firefox",
            "/snap/bin/firefox",
        ],
        "Opera": [
            "/usr/bin/opera",
            "/usr/lib/x86_64-linux-gnu/opera/opera",
        ],
        "Vivaldi": [
            "/usr/bin/vivaldi",
            "/opt/vivaldi/vivaldi",
        ],
    }

    found: Dict[str, str] = {}
    for name, paths in candidates.items():
        for p in paths:
            if os.path.isfile(p):
                found[name] = p
                break

    for name, exe in (("Brave", "brave-browser"), ("Microsoft Edge", "microsoft-edge"),
                      ("Google Chrome", "google-chrome"), ("Chromium", "chromium"),
                      ("Mozilla Firefox", "firefox"), ("Opera", "opera"), ("Vivaldi", "vivaldi")):
        if name not in found:
            path = shutil.which(exe)
            if path:
                found[name] = path

    return found


def parse_cookie_input(raw: str) -> Tuple[str, Dict[str, str], Optional[Dict[str, Any]]]:
    """Parse cookie string from any input format:
    1. Raw cookie header: 'name=val; name2=val2'
    2. DevTools Application tab copy (tab-separated lines)
    3. JSON dictionary: '{"name": "value"}'

    Returns:
      (normalized_cookie_header, cookie_dict, decoded_user_info)
    """
    raw = raw.strip()
    cookies: Dict[str, str] = {}

    lines = raw.splitlines()
    if any("\t" in line for line in lines):
        for line in lines:
            parts = line.split("\t")
            if len(parts) >= 2:
                name = parts[0].strip()
                val = parts[1].strip()
                if name and val:
                    cookies[name] = val

    elif raw.startswith("{") and raw.endswith("}"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                cookies = {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass

    if not cookies:
        items = re.split(r"[;\r\n]+", raw)
        for item in items:
            item = item.strip()
            if "=" in item:
                k, v = item.split("=", 1)
                cookies[k.strip()] = v.strip()
            elif item.startswith("base64-"):
                cookies["arena-auth-prod-v1.0"] = item

    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    user_info = None
    v0 = cookies.get("arena-auth-prod-v1.0", "")
    v1 = cookies.get("arena-auth-prod-v1.1", "")
    if v0:
        try:
            combined = v0.replace("base64-", "") + v1
            combined += "=" * ((4 - len(combined) % 4) % 4)
            decoded_text = base64.b64decode(combined).decode("utf-8", errors="ignore")
            session_data = json.loads(decoded_text)
            user_obj = session_data.get("user", {})
            meta = user_obj.get("user_metadata", {})
            user_info = {
                "email": user_obj.get("email") or session_data.get("email"),
                "id": user_obj.get("id"),
                "full_name": meta.get("full_name") or meta.get("name"),
                "avatar_url": meta.get("avatar_url") or meta.get("picture"),
                "expires_at": session_data.get("expires_at"),
                "access_token": session_data.get("access_token"),
            }
        except Exception:
            pass

    return cookie_str, cookies, user_info


def get_clipboard_text() -> str:
    """Safely read text from the Linux clipboard (Wayland or X11)."""
    cmds = []
    if os.environ.get("WAYLAND_DISPLAY"):
        cmds.append(["wl-paste", "--no-newline"])
    cmds.extend([
        ["xclip", "-o", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--output"],
        ["wl-paste", "--no-newline"],
    ])
    seen = set()
    for cmd in cmds:
        key = tuple(cmd)
        if key in seen:
            continue
        seen.add(key)
        exe = shutil.which(cmd[0])
        if not exe:
            continue
        try:
            res = subprocess.run(
                [exe, *cmd[1:]],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if res.returncode == 0 and res.stdout:
                return res.stdout.strip()
        except Exception:
            continue
    try:
        import pyperclip
        return str(pyperclip.paste() or "").strip()
    except Exception:
        return ""


def check_clipboard_for_cookies() -> Optional[Tuple[str, Optional[Dict[str, Any]]]]:
    """Inspect clipboard for valid Arena cookies."""
    text = get_clipboard_text()
    if not text:
        return None
    if "arena-auth-prod" in text or "base64-eyJh" in text or "arena.ai" in text:
        header, c_dict, user_info = parse_cookie_input(text)
        if "arena-auth-prod-v1.0" in c_dict or any("auth" in k for k in c_dict):
            return header, user_info
    return None


def launch_browser_cdp(browser_path: str, port: int = 9222) -> Optional[subprocess.Popen]:
    """Launch a Chromium-family browser with remote debugging enabled."""
    from arena.config import CONFIG_DIR
    profile_dir = CONFIG_DIR / "cdp_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        browser_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={str(profile_dir)}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "https://arena.ai",
    ]
    try:
        return subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        con.print(f"[red]Failed to launch {browser_path}: {e}[/red]")
        return None
