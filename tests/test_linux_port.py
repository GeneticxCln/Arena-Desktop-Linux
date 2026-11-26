"""Linux port checks: backend selection, XDG deep links, Chromium cookies."""
import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from arena import auth, deep_links
from arena.web import app as appmod

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only code path")


@linux_only
def test_gui_backend_prefers_a_real_backend():
    from arena.desktop import gui_backend
    assert gui_backend() in ("gtk", "qt")


@linux_only
def test_linux_chromium_cookie_decrypts_cbc():
    key = appmod._linux_chromium_key()
    assert key and len(key) == 16
    value = b"arena-auth-prod-v1.0=cookie-value"
    pad = 16 - (len(value) % 16)
    padded = value + bytes([pad]) * pad
    iv = b" " * 16
    # Standard Linux Chromium layout: b"v10" + AES-128-CBC(key, iv=b" "*16, padded)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    assert appmod._decrypt_cookie_val(b"v10" + ct, key) == value.decode()
    # Chromium 130+ (DB v24+): 32-byte binary SHA-256 host-key digest prefix
    host_hash = bytes(range(32))
    prefixed = host_hash + value
    pad2 = 16 - (len(prefixed) % 16)
    enc2 = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct2 = enc2.update(prefixed + bytes([pad2]) * pad2) + enc2.finalize()
    assert appmod._decrypt_cookie_val(b"v11" + ct2, key) == value.decode()
    # Synthetic legacy layout with explicit space IV prefix still works
    assert appmod._decrypt_cookie_val(b"v10" + iv + ct, key) == value.decode()
    # Corrupted / truncated ciphertext fails closed
    assert appmod._decrypt_cookie_val(b"v10" + b"\x00" * 16, key) is None


def test_pyinstaller_spec_does_not_bundle_icon_themes():
    """The host's icon themes contain symlinks PyInstaller does not preserve.

    A partially copied theme makes GTK abort as soon as a file dialog asks for
    a missing icon, so the spec must disable the Gtk hook's icon collection.
    """
    spec = Path(__file__).resolve().parents[1] / "arena-cli.spec"
    tree = ast.parse(spec.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Analysis":
            for kw in node.keywords:
                if kw.arg == "hooksconfig":
                    config = ast.literal_eval(kw.value)
                    assert config.get("gi", {}).get("icons") == []
                    return
    raise AssertionError("Analysis(hooksconfig=...) not found in arena-cli.spec")


def test_google_window_diagnosis_uses_desktop_state():
    class FakeEngine:
        host = ""

        def __init__(self, **state):
            self._state = state

        def state(self):
            return self._state

    assert appmod._google_window_diagnosis(None)["state"] == "none"
    assert appmod._google_window_diagnosis(FakeEngine(attached=True))["state"] == "sleeping"
    opened = appmod._google_window_diagnosis(
        FakeEngine(attached=True, ready=True, host="google.com"))
    assert opened["state"] == "open" and "google.com" in opened["message"]
    failed = appmod._google_window_diagnosis(FakeEngine(attached=True, error="boom"))
    assert failed["state"] == "failed" and failed["message"] == "boom"


def test_desktop_exec_quoting():
    assert deep_links._desktop_exec_quote("/opt/Arena Desktop") == '"/opt/Arena Desktop"'
    assert deep_links._desktop_exec_quote('/tmp/a"b$c') == '"/tmp/a\\"b\\$c"'


@linux_only
def test_linux_browser_detection_is_registry_free(monkeypatch):
    monkeypatch.setattr(auth.shutil, "which", lambda _name: None)
    monkeypatch.setattr(auth.os.path, "isfile", lambda _p: False)
    assert auth.detect_installed_browsers() == {}


def test_google_engine_reads_a_plain_pywebview_window():
    """The Linux Google window has no WebView2 and no custom browser wrapper.

    Both the account reader and the jar export used to answer nothing for that
    window: ``evaluate_js`` was called with an unsupported ``timeout`` keyword
    and the only cookie reader required WebView2. Sign-in therefore worked in
    the window while the app never noticed it.
    """
    from arena.google_ai import GoogleEngine

    class FakeWindow:
        native = None
        is_browser = False

        def evaluate_js(self, script, callback=None):
            return json.dumps({"signed_in": True, "email": "user@example.com",
                               "name": "User", "has_signin_link": False})

        def get_cookies(self):
            return [
                SimpleNamespace(name="SID", value="sid-value", domain=".google.com"),
                SimpleNamespace(name="__Secure-1PSID", value="psid-value",
                                domain=".google.com"),
                SimpleNamespace(name="unrelated", value="ignored",
                                domain=".example.com"),
            ]

    engine = GoogleEngine()
    engine.window = FakeWindow()
    engine.loaded = True
    engine.visible = True
    engine.host = "www.google.com"

    info = engine.account()
    assert info["signed_in"] is True
    assert info["email"] == "user@example.com"

    jar = engine.cookies(tries=1)
    assert "SID=sid-value" in jar
    assert "__Secure-1PSID=psid-value" in jar
    assert "unrelated" not in jar
    assert engine.jar_signed_in() is True


def test_google_engine_accepts_a_json_mapping_from_pywebview():
    """pywebview parses JSON itself; the account reader must accept the dict."""
    from arena.google_ai import GoogleEngine

    class FakeWindow:
        native = None
        is_browser = False

        def evaluate_js(self, script, callback=None):
            return {"signed_in": True, "email": "x@y.z", "name": "X",
                    "has_signin_link": False}

    engine = GoogleEngine()
    engine.window = FakeWindow()
    engine.loaded = True
    engine.visible = True

    assert engine.account()["email"] == "x@y.z"


def test_google_clear_cookies_uses_pywebview_without_webview2():
    """A fresh sign-in on Linux must actually wipe the WebKit jar."""
    from arena.google_ai import GoogleEngine

    class FakeWindow:
        native = None
        is_browser = False
        cleared = False

        def clear_cookies(self):
            FakeWindow.cleared = True

    engine = GoogleEngine()
    engine.window = FakeWindow()

    assert engine.clear_cookies() is True
    assert FakeWindow.cleared is True


@linux_only
def test_frozen_linux_readiness_does_not_check_pythonnet(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    dep = appmod._check_dependencies()
    assert dep["ok"] is True
    assert "pywebview" in dep["detail"] and "Flask" in dep["detail"]
    assert "pythonnet" not in dep["detail"]


@linux_only
def test_linux_clipboard_uses_wayland_or_x11_tools(monkeypatch):
    calls = []
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(auth.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "wl-paste" else None)

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="arena-auth-prod-v1.0=abc\n")

    monkeypatch.setattr(auth.subprocess, "run", fake_run)
    assert auth.get_clipboard_text() == "arena-auth-prod-v1.0=abc"
    assert calls[0][0] == "/usr/bin/wl-paste"


@linux_only
def test_linux_notify_uses_notify_send(tmp_path, monkeypatch):
    from arena import desktop

    icon = tmp_path / "logo.png"
    icon.write_bytes(b"png")
    launched = []
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send" if name == "notify-send" else None)
    monkeypatch.setattr("subprocess.Popen", lambda args, **_kw: launched.append(args))
    assert desktop._linux_notify("Done", "Chat finished", icon) is True
    assert launched and launched[0][0] == "/usr/bin/notify-send"
    assert "-a" in launched[0] and "-i" in launched[0]


@linux_only
def test_linux_chromium_v11_keyring_password_derives_key(monkeypatch):
    monkeypatch.setattr(appmod, "_linux_chromium_keyring_password", lambda browser: b"my-keyring-secret")
    keys = appmod._linux_chromium_keys("chrome")
    assert len(keys) == 2
    assert keys[0] != keys[1]  # keyring-derived key first, fallback 'peanuts' second
    assert keys[1] == appmod._linux_chromium_key()


def test_dead_unpermissioned_terminal_and_files_routes_removed():
    client = appmod.app.test_client()
    assert client.post("/api/terminal", json={"command": "echo hi"}).status_code == 404
    assert client.post("/api/files/write", json={"path": "x.txt", "content": "x"}).status_code == 404
    assert client.get("/api/files").status_code == 404


def test_shutdown_runtime_stops_gateway_connectors_subagents_and_instance(tmp_path, monkeypatch):
    from arena import deep_links

    events = []
    fake_gw = SimpleNamespace(stop=lambda: events.append("gateway"))
    fake_connectors = SimpleNamespace(shutdown=lambda: events.append("connectors"))
    fake_subagents = SimpleNamespace(cancel_all=lambda: (events.append("subagents"), 1)[1])
    fake_srv = SimpleNamespace(
        shutdown=lambda: events.append("preview_shutdown"),
        server_close=lambda: events.append("preview_close"),
    )
    deep_links.write_instance(51234, "tok")
    assert deep_links.instance_path().exists()

    monkeypatch.setattr(appmod, "_shutdown_done", False)
    monkeypatch.setattr(appmod, "_GATEWAY", fake_gw)
    monkeypatch.setattr(appmod, "CONNECTORS", fake_connectors)
    monkeypatch.setattr(appmod, "_NATIVE_SUBAGENTS", fake_subagents)
    monkeypatch.setitem(appmod._preview, "server", fake_srv)
    monkeypatch.setitem(appmod._preview, "port", 51235)

    report = appmod.shutdown_runtime()
    assert report["gateway_stopped"] is True
    assert report["connectors_shutdown"] is True
    assert report["preview_stopped"] is True
    assert report["instance_removed"] is True
    assert not deep_links.instance_path().exists()
    assert events == ["subagents", "gateway", "connectors", "preview_shutdown", "preview_close"]
    # Idempotent on second call
    assert appmod.shutdown_runtime() == {"already": True}
