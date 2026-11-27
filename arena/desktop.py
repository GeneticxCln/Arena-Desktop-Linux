"""
Arena Desktop (Linux): a native window (pywebview + WebKitGTK 4.1, or
QtWebEngine fallback) over the local Flask UI, plus a hidden arena.ai window
(the engine) that sends chats from arena's own page. Sign-in and human checks
open the engine window in front of the app.
"""

import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

LOG = logging.getLogger("arena.desktop")
_EXIT_LOCK = threading.Lock()
_EXITING = False


def _shutdown_and_exit(code: int = 0) -> None:
    """Stop MCP servers, Gateway tunnels, sub-agents, PTYs, and flush WebKit cleanly before exit."""
    global _EXITING
    with _EXIT_LOCK:
        if _EXITING:
            return
        _EXITING = True
    try:
        from arena.web import app as web_app
        web_app.shutdown_runtime()
    except Exception:
        LOG.exception("runtime shutdown failed")
    # Ask GTK to exit its loop briefly so WebKitNetworkProcess can flush cookies/storage.
    try:
        from gi.repository import GLib, Gio  # type: ignore

        def _quit_app():
            try:
                app = Gio.Application.get_default()
                if app is not None:
                    app.quit()
            except Exception:
                pass
            return False

        GLib.idle_add(_quit_app)
        time.sleep(0.25)
    except Exception:
        pass
    os._exit(code)


def _schedule_signal_shutdown(code: int = 0) -> None:
    """Async-signal-safe trigger: never acquire Python/logging/GTK locks inside a signal handler."""
    threading.Thread(
        target=_shutdown_and_exit,
        args=(code,),
        daemon=True,
        name="signal-shutdown",
    ).start()


def gui_backend():
    """pywebview GUI backend on Linux: prefers WebKitGTK, falls back to QtWebEngine."""
    try:
        import webview.platforms.gtk  # noqa: F401
        return "gtk"
    except Exception:
        LOG.info("WebKitGTK backend unavailable, trying Qt")
    try:
        import webview.platforms.qt  # noqa: F401
        return "qt"
    except Exception:
        LOG.exception("no usable pywebview backend (install WebKit2GTK or QtWebEngine)")
        return None


def window_icon(base: Path) -> Path:
    """PNG window icon for GTK/Qt."""
    return base / "assets" / "logo.png"


def _linux_backend_missing() -> None:
    """Explain a missing WebKitGTK/QtWebEngine stack instead of dying silently
    in a windowless build."""
    message = (
        "Arena Desktop could not start a browser window.\n\n"
        "Install WebKitGTK 4.1 and PyGObject, then start the app again:\n"
        "  Arch/CachyOS:   sudo pacman -S webkit2gtk-4.1 python-gobject\n"
        "  Debian/Ubuntu:  sudo apt install python3-gi gir1.2-webkit2-4.1 gir1.2-gtk-3.0\n"
        "  Fedora:         sudo dnf install python3-gobject gtk3 webkit2gtk4.1"
    )
    LOG.error(message)
    for cmd in (
        ["zenity", "--error", "--title", "Arena Desktop", "--text", message],
        ["kdialog", "--title", "Arena Desktop", "--error", message],
        ["notify-send", "--urgency=critical", "Arena Desktop", message],
    ):
        tool = shutil.which(cmd[0])
        if not tool:
            continue
        try:
            subprocess.run([tool, *cmd[1:]], check=False, timeout=30)
            return
        except Exception:
            continue
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_flask(port: int):
    from arena.web import app as web_app
    web_app._start_automation_scheduler()
    web_app.app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)


def wait_port(port: int, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def _flag_value(name):
    """Value after a --flag in argv, or None (flags: --profile, --mode, --background)."""
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--"):
            return sys.argv[i + 1]
    return None


def _switch_profile(port, wanted):
    """--profile <id|email>: activate that account on the local server before the UI loads."""
    try:
        import requests
        from arena.web import app as web_app
        headers = {"X-Arena-Token": web_app._API_TOKEN}
        data = requests.get(f"http://127.0.0.1:{port}/api/accounts", headers=headers, timeout=5).json()
        items = data.get("accounts") if isinstance(data, dict) else data
        for acc in items or []:
            if str(acc.get("id")) == wanted or str(acc.get("email") or "").lower() == wanted.lower():
                if acc.get("current"):
                    return
                requests.post(f"http://127.0.0.1:{port}/api/accounts/switch",
                              json={"id": acc.get("id"), "account_id": acc.get("id")}, headers=headers, timeout=15)
                return
        LOG.info("profile %r not found", wanted)
    except Exception:
        LOG.exception("profile switch failed")


_QUICK_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Quick ask</title>
<style>
 body{margin:0;background:#14171a;color:#e9f2ea;font-family:system-ui,sans-serif}
 .wrap{padding:14px}
 h1{font-size:13px;margin:0 0 8px;font-weight:650}
 textarea{width:100%;box-sizing:border-box;height:74px;resize:none;border-radius:8px;border:1px solid #3b4140;background:#1d2221;color:#e9f2ea;padding:8px;font:inherit}
 .row{display:flex;justify-content:flex-end;gap:8px;margin-top:10px}
 button{border-radius:7px;border:1px solid #8dd39a;background:#b8e5bf;color:#17251a;padding:6px 14px;font-weight:650;cursor:pointer}
 button.q{background:#262a2b;color:#cfd8d2;border-color:#3b4140}
</style></head><body><div class="wrap">
<h1>Quick ask \u2014 starts a new chat</h1>
<textarea id="t" placeholder="Ask something\u2026 (Enter to send, Shift+Enter for a new line)"></textarea>
<div class="row"><button class="q" id="cancel">Cancel</button><button id="send">Send</button></div>
</div><script>
 const t=document.getElementById('t'); t.focus();
 const go=()=>{ const v=t.value.trim(); if(!v) return; window.pywebview.api.submit(v); };
 document.getElementById('send').onclick=go;
 document.getElementById('cancel').onclick=()=>window.pywebview.api.submit('');
 t.addEventListener('keydown',e=>{ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();go();} if(e.key==='Escape') window.pywebview.api.submit(''); });
</script></body></html>"""


class _QuickAskApi:
    """js_api of the quick-ask window; submit('') just closes it."""

    def __init__(self, tray):
        self._tray = tray
        self._window = None

    def submit(self, text):
        self._tray._quick_submit(text)


def _linux_notify(title: str, text: str, icon_path=None) -> bool:
    """Send a native freedesktop desktop notification via notify-send."""
    tool = shutil.which("notify-send")
    if not tool:
        return False
    args = [tool, "-a", "Arena Desktop"]
    if icon_path and Path(icon_path).is_file():
        args.extend(["-i", str(icon_path)])
    args.extend([str(title or "Arena Desktop"), str(text or "")])
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except Exception:
        LOG.exception("notify-send failed")
        return False


class Tray:
    """Optional Linux system-tray icon (AppIndicator/GTK, best effort).

    With the tray on, closing the window hides it instead of quitting;
    Quit lives in the tray menu.
    """

    def __init__(self, window, icon_path):
        self.window = window
        self.icon_path = icon_path
        self.icon = None
        self.visible = True
        self.quitting = False
        self.quick_window = None

    def show_window(self):
        self.visible = True
        try:
            self.window.show()
            self.window.restore()
        except Exception:
            pass

    def hide_window(self):
        self.visible = False
        try:
            self.window.hide()
        except Exception:
            pass

    def toggle(self):
        self.hide_window() if self.visible else self.show_window()

    def notify(self, title, text, kind="done"):
        # Questions always toast (the agent is blocked on the user);
        # finished generations only when the window is tucked away.
        if kind == "done" and self.visible:
            return
        try:
            if self.icon:
                self.icon.notify(text, title)
                return
        except Exception:
            LOG.exception("tray notify failed")
        _linux_notify(title, text, self.icon_path)

    def open_quick_ask(self):
        try:
            import webview
            if self.quick_window is not None:
                try:
                    self.quick_window.show()
                    self.quick_window.restore()
                    return
                except Exception:
                    self.quick_window = None
            api = _QuickAskApi(self)
            self.quick_window = webview.create_window(
                "Quick ask - Arena Desktop", html=_QUICK_HTML, js_api=api,
                width=560, height=200, resizable=False, on_top=True)
            api._window = self.quick_window
        except Exception:
            LOG.exception("quick ask unavailable")
            self.show_window()

    def _quick_submit(self, text):
        text = (text or "").strip()
        if self.quick_window is not None:
            try:
                self.quick_window.destroy()
            except Exception:
                pass
            self.quick_window = None
        if not text:
            return
        self.show_window()
        try:
            self.window.evaluate_js(
                "window.__quickAsk && window.__quickAsk(" + json.dumps(text) + ")")
        except Exception:
            LOG.exception("quick ask handoff failed")

    def on_closing(self):
        if self.quitting:
            return True
        self.hide_window()
        return False  # cancel the close; the app lives in the tray

    def quit(self):
        self.quitting = True
        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass
        _shutdown_and_exit(0)

    def start(self) -> bool:
        try:
            import pystray
            from PIL import Image
            image = Image.open(self.icon_path)
        except Exception:
            LOG.exception("tray unavailable (pystray/Pillow/icon)")
            return False
        menu = pystray.Menu(
            pystray.MenuItem("Open Arena Desktop", lambda *_: self.show_window(), default=True),
            pystray.MenuItem("Quick ask...", lambda *_: self.open_quick_ask()),
            pystray.MenuItem("Hide", lambda *_: self.hide_window()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda *_: self.quit()),
        )
        self.icon = pystray.Icon("Arena Desktop", image, "Arena Desktop", menu)
        try:
            from arena.web import app as _webapp
            _webapp.NOTIFY_HOOK = lambda title, text, kind="done": self.notify(title, text, kind)
        except Exception:
            LOG.exception("notify hook wiring failed")
        threading.Thread(target=self._run_icon, daemon=True).start()
        return True

    def _run_icon(self):
        try:
            run_detached = getattr(self.icon, "run_detached", None)
            if run_detached:
                run_detached()
            else:
                self.icon.run()
        except Exception:
            LOG.exception("tray icon stopped")


def _dialog(name: str):
    import webview
    fd = getattr(webview, "FileDialog", None)
    if fd is not None:
        return getattr(fd, name)
    return getattr(webview, f"{name}_DIALOG")


class JsBridge:
    """js_api of the main window (local UI only). pywebview exposes every public attribute to JS
    and walks it recursively, so the window reference must stay private."""

    def __init__(self):
        self._window = None

    def open_file_dialog(self, file_types=None):
        ft = tuple(file_types or ()) + ("All files (*.*)",)
        result = self._window.create_file_dialog(_dialog("OPEN"), allow_multiple=True, file_types=ft)
        return list(result or [])

    def save_file_dialog(self, filename=""):
        result = self._window.create_file_dialog(_dialog("SAVE"), save_filename=filename)
        if isinstance(result, (list, tuple)):
            result = result[0] if result else None
        return result or None

    def choose_folder(self):
        result = self._window.create_file_dialog(_dialog("FOLDER"))
        return (result[0] if result else None) if isinstance(result, (list, tuple)) else result


def main():
    from arena import deep_links

    deep_link_arg = deep_links.find_deep_link_arg(sys.argv[1:])
    if deep_link_arg:
        try:
            deep_links.parse_deep_link(deep_link_arg)
        except ValueError:
            deep_link_arg = "arenadesktop://open"
        if deep_links.forward_deep_link(deep_link_arg):
            return

    import webview
    from arena import contracts, diag, google_ai
    from arena.config import CONFIG_DIR
    from arena.engine import ENGINE
    from arena.web import app as web_app

    contracts.load_overrides()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(CONFIG_DIR / "desktop.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    try:
        diag.install()
    except Exception:
        LOG.exception("could not start diagnostics")
    if deep_links.register_protocol():
        LOG.info("registered arenadesktop:// for this user")
    instance_token = deep_links.new_instance_token()
    web_app.INSTANCE_TOKEN = instance_token
    boot_nonce = deep_links.new_instance_token()
    web_app._BOOT_NONCE = boot_nonce

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_a: _schedule_signal_shutdown(0))
        except Exception:
            pass

    port = int(os.environ.get("ARENA_PORT") or 0) or find_free_port()
    threading.Thread(target=start_flask, args=(port,), daemon=True).start()
    wait_port(port)
    try:
        deep_links.write_instance(port, instance_token)
    except Exception:
        LOG.exception("could not publish the deep-link instance file")
    profile = _flag_value("--profile")
    if profile:
        _switch_profile(port, profile)

    base = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    ico = window_icon(base)

    backend = gui_backend()
    if backend is None:
        _linux_backend_missing()
        os._exit(1)
    webview.settings["ALLOW_DOWNLOADS"] = True
    from arena.config import load_config as _load_cfg
    cfg = _load_cfg()
    background = "--background" in sys.argv
    start_url = f"http://127.0.0.1:{port}/"
    mode_flag = _flag_value("--mode")
    query = [("boot", boot_nonce)]
    if mode_flag:
        query.append(("mode", mode_flag))
    if deep_link_arg:
        query.append(("deeplink", deep_link_arg))
    if query:
        start_url += "?" + urllib.parse.urlencode(query)
    bridge = JsBridge()
    main_win = webview.create_window("Arena Desktop", start_url, width=1280, height=840,
                                     min_size=(860, 600), background_color="#242422", text_select=True,
                                     js_api=bridge, hidden=background)
    bridge._window = main_win
    engine_win = webview.create_window("arena.ai - Arena Desktop", contracts.ORIGIN, width=1000, height=760, x=120, y=120,
                                       hidden=True, background_color="#242422", js_api=ENGINE.bridge())
    ENGINE.attach(engine_win, main_win)
    ENGINE.dock = None
    google_win = webview.create_window("Google AI mode - Arena Desktop", "about:blank", width=1100, height=760,
                                       x=140, y=140, hidden=True, background_color="#242422")
    google_ai.ENGINE.attach(google_win, main_win)
    web_app.GOOGLE_ENGINE = google_ai.ENGINE
    for _win_to_wrap, _wrap_lbl in ((main_win, "main-window"), (engine_win, "arena-window"), (google_win, "google-window")):
        try:
            diag.make_window_calls_non_blocking(_win_to_wrap)
        except Exception:
            LOG.exception("could not wrap %s for GTK thread safety", _wrap_lbl)
    from arena import providers as _providers

    def _create_provider_window(title: str, url: str, hidden: bool = True):
        win = webview.create_window(
            title, url, width=1120, height=780, x=150, y=130,
            hidden=hidden, background_color="#18191c",
        )
        try:
            diag.watch_object(win, title, ["show", "hide", "restore", "load_url", "evaluate_js", "get_cookies"])
        except Exception:
            pass
        return win

    _providers.set_window_factory(_create_provider_window)
    for win, label in ((main_win, "main-window"), (engine_win, "arena-window"),
                       (google_win, "google-window")):
        try:
            diag.watch_object(win, label, ["show", "hide", "restore", "resize",
                                           "move", "load_url", "evaluate_js",
                                           "get_cookies"])
        except Exception:
            LOG.exception("could not trace %s", label)
    main_win.events.closed += lambda *a: _shutdown_and_exit(0)
    tray = None
    tray_started = False
    if cfg.get("tray_enabled") or background:
        tray = Tray(main_win, base / "assets" / "logo.png")
        tray.visible = not background
        if tray.start():
            tray_started = True
            main_win.events.closing += tray.on_closing
        elif background:
            # main_win was created hidden; show it once webview.start() marks it ready.
            try:
                main_win.events.shown += lambda *_a: main_win.show()
            except Exception:
                main_win.show()
    if not tray_started:
        icon_png = base / "assets" / "logo.png"
        web_app.NOTIFY_HOOK = lambda title, text, kind="done": _linux_notify(title, text, icon_png)

    def handle_deep_link(payload):
        try:
            if tray is not None:
                tray.show_window()
            else:
                main_win.show()
                main_win.restore()
            main_win.evaluate_js(
                "window.__arenaDeepLink && window.__arenaDeepLink(" + json.dumps(payload) + ")")
            return True
        except Exception:
            LOG.exception("deep link window handoff failed")
            return False

    web_app.DEEP_LINK_HOOK = handle_deep_link

    storage = CONFIG_DIR / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    webview.start(gui=backend, private_mode=False, storage_path=str(storage),
                  icon=str(ico) if ico.exists() else None, debug=bool(os.environ.get("ARENA_DEBUG")))
    _shutdown_and_exit(0)


if __name__ == "__main__":
    main()
