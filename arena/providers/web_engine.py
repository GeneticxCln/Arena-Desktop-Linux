"""Reusable pure-Linux WebKitGTK engine for web-based AI providers.

Every web provider (DeepSeek, Claude, ChatGPT) runs inside a genuine, persistent
Linux WebKitGTK window sharing the app's WebKit profile storage
(`~/.arena-personal/webview`).

Architecture (mirroring ``arena/engine.py``):
- All JS executes through WebKit2GTK's native ``evaluate_javascript`` C API with
  exact UTF-8 byte lengths (zero JS ``eval()``, zero CSP ``EvalError``, zero
  UTF-8 truncation ``SyntaxError``).
- Same-origin ``fetch()`` + SSE streaming runs directly inside the signed-in
  page's JS context (``fetch_json`` / ``iter_fetch_stream``), working silently
  even when the GTK window is hidden on Wayland.
- For sites whose chat endpoint uses browser-side integrity/PoW (DeepSeek,
  ChatGPT), we NEVER compute or forge tokens: we drive the page's own composer
  (strictly scoped to the active composer container) and attach an
  observation-only ``window.fetch`` clone hook (same pattern as
  ``arena/engine.py:_DRIVE_HOOK_JS``) so we stream the genuine server SSE reply
  without ever misreading stale messages from older chats.
- Sign-in and human-verification checks are ALWAYS manual: the window is shown
  to the user whenever login or verification is needed.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator

from arena import diag
from arena.providers.base import Provider

LOG = logging.getLogger("arena.providers.web")

_WINDOW_FACTORY: Callable[[str, str, bool], object] | None = None


def set_window_factory(factory: Callable[[str, str, bool], object] | None) -> None:
    """Register the desktop WebView window creator from ``arena/desktop.py``."""
    global _WINDOW_FACTORY
    _WINDOW_FACTORY = factory


def get_window_factory() -> Callable[[str, str, bool], object] | None:
    return _WINDOW_FACTORY


_HIDDEN_RAF_SHIM_JS = r"""(() => {
  if (window.__apRafPatched) return;
  window.__apRafPatched = true;
  window.__apRafActive = false;
  const origRAF = window.requestAnimationFrame.bind(window);
  const origCAF = window.cancelAnimationFrame.bind(window);
  const timers = new Map();
  let nextId = 900000000;
  window.requestAnimationFrame = function(cb) {
    if (!document.hidden) return origRAF(cb);
    const id = ++nextId;
    const delay = window.__apRafActive ? 16 : 250;
    const t = setTimeout(() => {
      timers.delete(id);
      try { cb(performance.now()); } catch (e) {}
    }, delay);
    timers.set(id, t);
    return id;
  };
  window.cancelAnimationFrame = function(id) {
    if (timers.has(id)) {
      clearTimeout(timers.get(id));
      timers.delete(id);
      return;
    }
    return origCAF(id);
  };
})();"""

_OAUTH_HOST_HINTS = (
    "accounts.google.",
    "auth.openai.com",
    "appleid.apple.com",
    "github.com/login",
    "login.microsoftonline.com",
    "auth.qwen.ai",
)


def _norm_url(u: str) -> str:
    return (u or "").strip().rstrip("/")


_DATED_SUFFIX = re.compile(r"^-\d{6,8}(?:$|[-:])")


def same_model_id(a: str, b: str) -> bool:
    """True when two model ids name the same model, ignoring case and a date
    suffix (``claude-x-5-20260101`` matches ``claude-x-5``)."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    return longer.startswith(shorter) and bool(_DATED_SUFFIX.match(longer[len(shorter):]))


def _looks_like_human_check(title: str, body: str) -> bool:
    t = (title or "").lower()
    b = (body or "").lower()
    if "just a moment" in t or "attention required" in t:
        return True
    markers = (
        "verify you are human",
        "checking your browser",
        "cf-turnstile",
        "enable javascript and cookies to continue",
        "unusual traffic from your computer",
        "complete the security check",
        "human verification",
    )
    return any(m in b for m in markers)


class WebChatEngine(Provider):
    """Base WebKitGTK provider with same-origin fetch streaming + composer fallback."""

    kind = "webview"
    window_title: str = "Provider - Arena Desktop"
    cookie_urls: tuple[str, ...] = ()
    auth_cookie_names: tuple[str, ...] = ()
    login_url_hints: tuple[str, ...] = ("/login", "/signin", "/sign_in", "/auth")
    composer_selectors: tuple[str, ...] = ()
    send_selectors: tuple[str, ...] = ()
    stop_selectors: tuple[str, ...] = ()
    assistant_selectors: tuple[str, ...] = ()
    reasoning_selectors: tuple[str, ...] = ()
    conversation_url_hint: str = ""
    observed_fetch_patterns: tuple[str, ...] = ()
    prefer_enter_submit: bool = False
    api_nav_not_needed: bool = False
    default_models: tuple[dict, ...] = ()
    poll_interval: float = 0.30
    turn_timeout: float = 180.0

    def __init__(self) -> None:
        self.window = None
        self.visible: bool = False
        self.last_url: str = ""
        self.last_error: str = ""
        self._lock = threading.Lock()
        self._turn_lock = threading.Lock()
        self._signed_in_cache: bool | None = None
        self._signed_in_at: float = 0.0
        self._models_cache: dict | None = None
        self._models_at: float = 0.0
        self._models_probe_lock = threading.Lock()
        self._popup_windows: list = []
        self._webkit_configured_uid: str = ""

    def _configure_webkit_view(self, win) -> None:
        """Install OAuth popup support ('create' signal) and hidden-window rAF UserScript."""
        uid = getattr(win, "uid", None)
        if not uid or uid == self._webkit_configured_uid:
            return
        try:
            import gi  # type: ignore

            gi.require_version("Gtk", "3.0")
            from gi.repository import GLib, Gtk, WebKit2  # type: ignore
            from webview.platforms import gtk as gtk_backend  # type: ignore
        except Exception:
            return

        attempts = {"n": 0}

        def _attach() -> bool:
            view = gtk_backend.BrowserView.instances.get(uid)
            wv = getattr(view, "webview", None) if view is not None else None
            if wv is None:
                attempts["n"] += 1
                if attempts["n"] < 25:
                    GLib.timeout_add(150, _attach)
                return False
            if self._webkit_configured_uid == uid:
                return False
            self._webkit_configured_uid = uid

            # 1. Inject hidden-window rAF shim at DOCUMENT_START so Svelte/Vue/Lexical/React
            #    submit handlers never stall when this GTK window is hidden.
            try:
                ucm = wv.get_user_content_manager()
                if ucm is not None:
                    ucm.add_script(WebKit2.UserScript(
                        _HIDDEN_RAF_SHIM_JS,
                        WebKit2.UserContentInjectedFrames.ALL_FRAMES,
                        WebKit2.UserScriptInjectionTime.START,
                        None,
                        None,
                    ))
            except Exception as exc:
                LOG.debug("%s: could not install hidden rAF UserScript: %s", self.id, exc)

            # 2. Enable JS popup windows (e.g. Google/Apple/GitHub OAuth via window.open)
            #    sharing the exact same WebContext, cookies, and JS window.opener link.
            def _wire_popups(target_wv) -> None:
                try:
                    settings = target_wv.get_settings()
                    if settings is not None:
                        settings.set_javascript_can_open_windows_automatically(True)
                except Exception:
                    pass

                def _on_create(parent_wv, _nav_action):
                    # Only allow user-initiated OAuth popups while this provider window is visible,
                    # and cap concurrent popups to avoid unsolicited background windows.
                    if not self.visible or len(self._popup_windows) >= 2:
                        return None
                    try:
                        popup_wv = WebKit2.WebView.new_with_related_view(parent_wv)
                    except Exception:
                        return None
                    popup_win = Gtk.Window(title=f"{self.name} Sign in - Arena Desktop")
                    popup_win.set_default_size(540, 720)
                    popup_win.set_position(Gtk.WindowPosition.CENTER)
                    popup_win.add(popup_wv)
                    self._popup_windows.append(popup_win)

                    def _update_popup_title(p_wv):
                        try:
                            from urllib.parse import urlparse
                            uri = p_wv.get_uri() or ""
                            host = urlparse(uri).netloc if uri else ""
                            t = (p_wv.get_title() or "").strip()
                            prefix = f"[{host}] " if host else ""
                            label = t or f"{self.name} Sign in"
                            popup_win.set_title(f"{prefix}{label} - Arena Desktop")
                        except Exception:
                            pass

                    def _on_ready(p_wv):
                        _update_popup_title(p_wv)
                        popup_win.show_all()
                        popup_win.present()

                    def _on_title(p_wv, _pspec):
                        _update_popup_title(p_wv)

                    def _on_uri(p_wv, _pspec):
                        _update_popup_title(p_wv)

                    def _on_close(_p_wv):
                        try:
                            popup_win.destroy()
                        except Exception:
                            pass

                    def _on_destroy(_w):
                        try:
                            self._popup_windows.remove(popup_win)
                        except ValueError:
                            pass

                    popup_wv.connect("ready-to-show", _on_ready)
                    popup_wv.connect("notify::title", _on_title)
                    popup_wv.connect("notify::uri", _on_uri)
                    popup_wv.connect("close", _on_close)
                    popup_win.connect("destroy", _on_destroy)
                    _wire_popups(popup_wv)
                    return popup_wv

                target_wv.connect("create", _on_create)

            try:
                _wire_popups(wv)
            except Exception as exc:
                LOG.debug("%s: could not connect WebKit2 create popup handler: %s", self.id, exc)
            return False

        try:
            GLib.idle_add(_attach)
        except Exception:
            pass

    # -- window lifecycle ------------------------------------------------

    def has_desktop_runtime(self) -> bool:
        return self.window is not None or _WINDOW_FACTORY is not None

    def ensure_window(self, hidden: bool = True) -> bool:
        if self.window is not None:
            return True
        factory = _WINDOW_FACTORY
        if factory is None:
            return False
        with self._lock:
            if self.window is not None:
                return True
            try:
                win = factory(self.window_title, self.origin, hidden)
            except Exception as exc:
                self.last_error = f"Could not create {self.name} window: {exc}"
                LOG.exception("%s: window creation failed", self.id)
                return False
            if win is None:
                return False
            self.window = win
            self.visible = not hidden
            try:
                diag.make_window_calls_non_blocking(win)
            except Exception:
                pass
            try:
                win.events.closing += self._on_closing
                win.events.closed += self._on_closed
            except Exception:
                pass
            shown_evt = getattr(getattr(win, "events", None), "shown", None)
            wait_fn = getattr(shown_evt, "wait", None)
            if callable(wait_fn):
                try:
                    wait_fn(2.0)
                except Exception:
                    pass
            self._configure_webkit_view(win)
            return True

    def _close_popups(self) -> None:
        if not self._popup_windows:
            return
        wins = list(self._popup_windows)
        self._popup_windows.clear()
        try:
            from gi.repository import GLib  # type: ignore

            def _destroy_all():
                for w in wins:
                    try:
                        w.destroy()
                    except Exception:
                        pass
                return False

            GLib.idle_add(_destroy_all)
        except Exception:
            for w in wins:
                try:
                    w.destroy()
                except Exception:
                    pass

    def _on_closing(self, *_args):
        """Close-to-hide so the signed-in session stays alive in the background."""
        self.visible = False
        self._close_popups()
        if self.window is not None:
            diag.run_on_ui_nonblocking(self.window, self.window.hide, f"{self.id}-hide")
        return False

    def _on_closed(self, *_args):
        self.visible = False
        self._close_popups()
        self._webkit_configured_uid = ""
        self.window = None

    def show(self) -> bool:
        if not self.ensure_window(hidden=False):
            return False
        w = self.window
        self._configure_webkit_view(w)
        self.visible = True
        try:
            w.show()
        except Exception as exc:
            LOG.warning("%s: show() failed: %s", self.id, exc)
            return False
        try:
            restore = getattr(w, "restore", None)
            if callable(restore):
                restore()
        except Exception:
            pass
        native = getattr(w, "native", None)
        present = getattr(native, "present", None)
        if callable(present):
            diag.run_on_ui_nonblocking(w, present, f"{self.id}-present")
        return True

    def hide(self) -> bool:
        self.visible = False
        self._close_popups()
        if self.window is None:
            return False
        try:
            self.window.hide()
            return True
        except Exception:
            return False

    def load_url(self, url: str) -> bool:
        if not self.ensure_window(hidden=not self.visible):
            return False
        try:
            self.window.load_url(url)
            self.last_url = url
            return True
        except Exception as exc:
            LOG.warning("%s: load_url(%s) failed: %s", self.id, url, exc)
            return False

    def open_sign_in(self) -> dict:
        was_missing = self.window is None
        if not self.ensure_window(hidden=False):
            return {
                "ok": False,
                "error": f"The {self.name} window is only available inside the desktop app.",
            }
        self.show()
        if not was_missing:
            current = self.page_url()
            cur_low = (current or "").lower()
            in_oauth = any(h in cur_low for h in _OAUTH_HOST_HINTS)
            if not in_oauth and (not current or current == "about:blank" or self.origin.split("/")[2] not in current):
                self.load_url(self.origin)
        return {"ok": True, "open": True, "url": self.origin}

    def shutdown(self) -> None:
        self._close_popups()
        try:
            self.hide()
        except Exception:
            pass

    # -- JS & cookies (native WebKit2GTK with exact UTF-8 byte length) ---

    def _eval_js_webkit_direct(self, script: str, timeout: float = 6.0):
        """Run JS directly via WebKit2's ``evaluate_javascript`` C API.

        Avoids BOTH pywebview GTK bugs:
        1. ``Window.evaluate_js()`` wraps code in JS ``eval(...)`` which strict
           CSP sites (chatgpt.com, claude.ai) reject with ``EvalError``.
        2. ``BrowserView.evaluate_js()`` passes ``length=len(script)`` (Python
           character count) instead of UTF-8 byte length, truncating any script
           containing non-ASCII characters (emojis, ``·``, Cyrillic, etc.) and
           causing ``SyntaxError: Unexpected end of script``.
        """
        uid = getattr(self.window, "uid", None) if self.window is not None else None
        if not uid:
            return False, None
        try:
            from gi.repository import GLib  # type: ignore
            from webview.platforms import gtk as gtk_backend  # type: ignore
        except Exception:
            return False, None
        view = gtk_backend.BrowserView.instances.get(uid)
        webview_obj = getattr(view, "webview", None) if view is not None else None
        if webview_obj is None:
            return False, None

        box: dict = {}
        done = threading.Event()
        byte_len = len(script.encode("utf-8"))

        def _on_finish(wv, task):
            try:
                js_val = wv.evaluate_javascript_finish(task)
                box["value"] = view._convert_js_value(js_val)
            except BaseException as exc:
                box["error"] = exc
            finally:
                done.set()

        def _dispatch():
            try:
                if hasattr(webview_obj, "evaluate_javascript"):
                    webview_obj.evaluate_javascript(
                        script=script,
                        length=byte_len,
                        world_name=None,
                        source_uri=None,
                        cancellable=None,
                        callback=_on_finish,
                    )
                else:
                    # Do NOT call view.evaluate_js() on the GTK main thread (would self-deadlock).
                    box["fallback"] = True
                    done.set()
            except BaseException as exc:
                box["error"] = exc
                done.set()
            return False

        GLib.idle_add(_dispatch)
        if not done.wait(timeout):
            return True, None
        if box.get("fallback"):
            return False, None
        if "error" in box:
            LOG.debug("%s: webkit direct eval_js error: %s", self.id, box["error"])
            return True, None
        return True, box.get("value")

    def eval_js(self, script: str, timeout: float = 6.0):
        if self.window is None:
            return None
        handled, val = self._eval_js_webkit_direct(script, timeout=timeout)
        if not handled:
            box: dict = {}
            done = threading.Event()

            def work():
                try:
                    box["value"] = self.window.evaluate_js(script)
                except BaseException as exc:
                    box["error"] = exc
                finally:
                    done.set()

            threading.Thread(target=work, daemon=True, name=f"{self.id}-js").start()
            if not done.wait(timeout):
                return None
            if "error" in box:
                LOG.debug("%s: eval_js error: %s", self.id, box["error"])
                return None
            val = box.get("value")
        if isinstance(val, str):
            s = val.strip()
            if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
                try:
                    return json.loads(s)
                except ValueError:
                    pass
        return val

    def page_url(self) -> str:
        val = self.eval_js("window.location ? String(window.location.href || '') : ''", timeout=2.0)
        if isinstance(val, str) and val:
            self.last_url = val
            return val
        return self.last_url

    def read_cookie_names(self) -> set[str]:
        names: set[str] = set()
        if self.window is None:
            return names
        # Always prefer bounded WebKit2 CookieManager directly on GTK to avoid
        # pywebview.platforms.gtk.BrowserView.get_cookies() unbounded semaphore hangs.
        try:
            names.update(self._read_webkit_cookie_names())
        except Exception:
            pass
        if names or getattr(self.window, "uid", None):
            return names
        box: dict = {}
        done = threading.Event()

        def _fetch():
            try:
                box["raw"] = self.window.get_cookies()
            except Exception:
                box["raw"] = []
            finally:
                done.set()

        threading.Thread(target=_fetch, daemon=True, name=f"{self.id}-cookies").start()
        if done.wait(2.5):
            for item in box.get("raw") or []:
                if isinstance(item, dict):
                    n = item.get("name") or item.get("Name")
                    if n:
                        names.add(str(n))
                    else:
                        for k in item.keys():
                            names.add(str(k))
                elif hasattr(item, "keys"):
                    for k in item.keys():
                        names.add(str(k))
        return names

    def _read_webkit_cookie_names(self) -> set[str]:
        names: set[str] = set()
        uid = getattr(self.window, "uid", None) if self.window is not None else None
        if not uid:
            return names
        try:
            import gi  # type: ignore

            gi.require_version("Gtk", "3.0")
            from gi.repository import GLib  # type: ignore
            from webview.platforms import gtk as gtk_backend  # type: ignore
        except Exception:
            return names
        view = gtk_backend.BrowserView.instances.get(uid)
        wv = getattr(view, "webview", None) if view is not None else None
        if wv is None:
            return names
        try:
            ctx = wv.get_context()
            cm = ctx.get_cookie_manager() if ctx is not None else None
        except Exception:
            cm = None
        if cm is None:
            return names
        urls = self.cookie_urls or (self.origin,)
        for url in urls:
            done = threading.Event()
            box: dict = {}

            def _make(target_url, target_box, target_done):
                def _on_done(mgr, res, _ud=None):
                    try:
                        cookies = mgr.get_cookies_finish(res) or []
                        target_box["names"] = {
                            str(c.get_name())
                            for c in cookies
                            if getattr(c, "get_name", None) and c.get_name()
                        }
                    except Exception:
                        target_box["names"] = set()
                    finally:
                        target_done.set()

                def _start():
                    try:
                        cm.get_cookies(target_url, None, _on_done, None)
                    except Exception:
                        target_done.set()
                    return False

                return _start

            GLib.idle_add(_make(url, box, done))
            if done.wait(2.5) and box.get("names"):
                names.update(box["names"])
        return names

    # -- Same-origin async fetch & streaming inside signed-in page -------

    def fetch_json(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict | None = None,
        headers: dict | None = None,
        timeout: float = 8.0,
    ) -> dict:
        """Execute a same-origin fetch() inside the WebKitGTK page and return status+data."""
        rid = uuid.uuid4().hex
        script = f"""(() => {{
          window.__apFetches = window.__apFetches || {{}};
          const rid = {json.dumps(rid)};
          window.__apFetches[rid] = {{ done: false, status: 0, ok: false, text: '', data: null, error: '' }};
          (async () => {{
            try {{
              const opts = {{
                method: {json.dumps(method)},
                credentials: 'include',
                headers: Object.assign(
                  {json.dumps(headers or {})},
                  {json.dumps({'Content-Type': 'application/json'} if body is not None else {})}
                )
              }};
              const payload = {json.dumps(body)};
              if (payload !== null && payload !== undefined) opts.body = JSON.stringify(payload);
              const res = await fetch({json.dumps(url)}, opts);
              const text = await res.text();
              let data = null;
              try {{ data = JSON.parse(text); }} catch (e) {{}}
              window.__apFetches[rid] = {{
                done: true, status: res.status, ok: res.ok,
                text: text.slice(0, 65536), data, error: ''
              }};
            }} catch (e) {{
              window.__apFetches[rid] = {{
                done: true, status: 0, ok: false, text: '', data: null, error: String(e)
              }};
            }}
          }})();
          return 'started';
        }})()"""
        started = self.eval_js(script, timeout=3.0)
        if started != "started":
            return {"ok": False, "status": 0, "data": None, "text": "", "error": "eval_failed"}
        deadline = time.time() + timeout
        poll_js = f"""(() => {{
          const bag = window.__apFetches && window.__apFetches[{json.dumps(rid)}];
          if (!bag || !bag.done) return '';
          delete window.__apFetches[{json.dumps(rid)}];
          return JSON.stringify(bag);
        }})()"""
        while time.time() < deadline:
            time.sleep(0.12)
            res = self.eval_js(poll_js, timeout=2.5)
            if isinstance(res, dict) and res.get("done"):
                return res
        # Clean up timed-out fetch entry in page JS so __apFetches never leaks.
        self.eval_js(
            f"(() => {{ if (window.__apFetches) delete window.__apFetches[{json.dumps(rid)}]; return 1; }})()",
            timeout=1.5,
        )
        return {"ok": False, "status": 0, "data": None, "text": "", "error": "timeout"}

    def iter_fetch_stream(
        self,
        url: str,
        *,
        method: str = "POST",
        body: dict | None = None,
        headers: dict | None = None,
        timeout: float = 180.0,
        stop_event=None,
    ) -> Iterator[dict]:
        """Stream a same-origin fetch() response chunk-by-chunk from the hidden WebView."""
        rid = uuid.uuid4().hex
        script = f"""(() => {{
          window.__apStreams = window.__apStreams || {{}};
          const rid = {json.dumps(rid)};
          const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
          const st = {{
            status: 0, ok: false, statusReady: false, chunks: [], done: false, error: '',
            abort: () => {{ try {{ if (ctrl) ctrl.abort(); }} catch (e) {{}} }}
          }};
          window.__apStreams[rid] = st;
          (async () => {{
            try {{
              const opts = {{
                method: {json.dumps(method)},
                credentials: 'include',
                signal: ctrl ? ctrl.signal : undefined,
                headers: Object.assign(
                  {{ 'Accept': 'text/event-stream, application/json' }},
                  {json.dumps(headers or {})},
                  {json.dumps({'Content-Type': 'application/json'} if body is not None else {})}
                )
              }};
              const payload = {json.dumps(body)};
              if (payload !== null && payload !== undefined) opts.body = JSON.stringify(payload);
              const res = await fetch({json.dumps(url)}, opts);
              st.status = res.status;
              st.ok = res.ok;
              st.statusReady = true;
              if (res.body && res.body.getReader) {{
                const reader = res.body.getReader();
                const dec = new TextDecoder();
                for (;;) {{
                  const r = await reader.read();
                  if (r.done) break;
                  const txt = dec.decode(r.value, {{ stream: true }});
                  if (txt) st.chunks.push(txt);
                }}
              }} else {{
                const txt = await res.text();
                if (txt) st.chunks.push(txt);
              }}
              st.done = true;
            }} catch (e) {{
              st.error = String(e);
              st.done = true;
            }}
          }})();
          return 'started';
        }})()"""
        cleanup_js = f"""(() => {{
          const st = window.__apStreams && window.__apStreams[{json.dumps(rid)}];
          if (st) {{
            try {{ if (typeof st.abort === 'function') st.abort(); }} catch (e) {{}}
            delete window.__apStreams[{json.dumps(rid)}];
          }}
          return 1;
        }})()"""
        if self.eval_js(script, timeout=3.5) != "started":
            self.eval_js(cleanup_js, timeout=1.5)
            yield {"event": "error", "error": "eval_failed"}
            return

        cursor = 0
        last_activity = time.time()
        hard_deadline = last_activity + max(float(timeout) * 3.0, 900.0)
        emitted_status = False
        try:
            while (time.time() - last_activity) < timeout and time.time() < hard_deadline:
                if stop_event is not None and stop_event.is_set():
                    return
                time.sleep(0.08)
                drain_js = f"""(() => {{
                  const st = window.__apStreams && window.__apStreams[{json.dumps(rid)}];
                  if (!st) return JSON.stringify({{ missing: true }});
                  const cur = {int(cursor)};
                  for (let i = 0; i < cur && i < st.chunks.length; i++) st.chunks[i] = '';
                  const slice = st.chunks.slice(cur);
                  const nextCursor = st.chunks.length;
                  return JSON.stringify({{
                    statusReady: Boolean(st.statusReady),
                    status: st.status,
                    ok: st.ok,
                    chunks: slice,
                    nextCursor,
                    done: Boolean(st.done),
                    error: st.error || ''
                  }});
                }})()"""
                snap = self.eval_js(drain_js, timeout=3.0)
                if not isinstance(snap, dict):
                    continue
                if snap.get("missing"):
                    yield {"event": "error", "error": "stream_missing"}
                    return
                cursor = max(cursor, int(snap.get("nextCursor") or cursor))
                if snap.get("statusReady") and not emitted_status:
                    emitted_status = True
                    last_activity = time.time()
                    yield {"event": "status", "status": int(snap.get("status") or 0), "ok": bool(snap.get("ok"))}
                for chunk in snap.get("chunks") or []:
                    if chunk:
                        last_activity = time.time()
                        yield {"event": "chunk", "data": str(chunk)}
                if snap.get("error"):
                    yield {"event": "error", "error": str(snap["error"])}
                    return
                if snap.get("done"):
                    yield {"event": "end"}
                    return
            yield {"event": "error", "error": "stream_timeout"}
        finally:
            self.eval_js(cleanup_js, timeout=1.5)

    # -- Observation-only fetch stream hook (like arena/engine.py:_DRIVE_HOOK_JS)

    def _install_fetch_observer(self, patterns: tuple[str, ...]) -> bool:
        """Install an observation-only window.fetch clone hook to capture genuine SSE replies.

        Never modifies requests or headers and never computes/forges PoW tokens.
        """
        if not patterns:
            return False
        script = f"""(() => {{
          window.__apObsPatterns = {json.dumps(list(patterns))};
          window.__apObs = {{ active: false, status: 0, url: '', chunks: [], done: false, error: '', seq: (window.__apObs && window.__apObs.seq ? window.__apObs.seq + 1 : 1) }};
          if (window.__apObsHooked) return 'reset';
          window.__apObsHooked = true;
          const origFetch = window.fetch.bind(window);
          window.fetch = function(input, init) {{
            const url = typeof input === 'string' ? input : (input && input.url ? String(input.url) : '');
            const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
            const pats = window.__apObsPatterns || [];
            const matched = method === 'POST' && pats.some((p) => url.indexOf(p) !== -1);
            return origFetch(input, init).then((res) => {{
              if (matched) {{
                const ct = String((res.headers && res.headers.get && res.headers.get('content-type')) || '').toLowerCase();
                const isStream = ct.includes('event-stream') || ct.includes('grpc') || ct.includes('connect') || ct.includes('octet-stream');
                const cur = window.__apObs;
                // Do not let a non-streaming metadata POST overwrite an active streaming reply!
                if (cur && cur.active && !cur.done && cur.isStream && !isStream) {{
                  return res;
                }}
                const obs = {{
                  active: true,
                  isStream,
                  status: res.status,
                  url: url.slice(0, 200),
                  chunks: [],
                  done: false,
                  error: '',
                  seq: (cur && cur.seq ? cur.seq + 1 : 1)
                }};
                window.__apObs = obs;
                try {{
                  const cloned = res.clone();
                  if (cloned.body && cloned.body.getReader) {{
                    const reader = cloned.body.getReader();
                    const dec = new TextDecoder();
                    (async () => {{
                      try {{
                        for (;;) {{
                          const r = await reader.read();
                          if (r.done) break;
                          const txt = dec.decode(r.value, {{ stream: true }});
                          if (txt) obs.chunks.push(txt);
                        }}
                      }} catch (e) {{
                        obs.error = String(e);
                      }} finally {{
                        obs.done = true;
                      }}
                    }})();
                  }} else {{
                    cloned.text().then((t) => {{
                      if (t) obs.chunks.push(t);
                      obs.done = true;
                    }}).catch((e) => {{ obs.error = String(e); obs.done = true; }});
                  }}
                }} catch (e) {{
                  obs.error = String(e);
                  obs.done = true;
                }}
              }}
              return res;
            }});
          }};
          return 'installed';
        }})()"""
        return bool(self.eval_js(script, timeout=3.0))

    def _drain_observed_stream(self, cursor: int = 0, expected_seq: int = 0) -> dict:
        res = self.eval_js(
            f"""(() => {{
              const o = window.__apObs;
              if (!o) return JSON.stringify({{ active: false, chunks: [], done: false, seq: 0, nextCursor: 0 }});
              const seq = Number(o.seq || 0);
              const rawChunks = Array.isArray(o.chunks) ? o.chunks : [];
              const cur = ({int(expected_seq)} && seq !== {int(expected_seq)}) ? 0 : {int(cursor)};
              for (let i = 0; i < cur && i < rawChunks.length; i++) rawChunks[i] = '';
              return JSON.stringify({{
                active: Boolean(o.active),
                status: Number(o.status || 0),
                url: String(o.url || ''),
                seq,
                chunks: rawChunks.slice(cur),
                nextCursor: rawChunks.length,
                done: Boolean(o.done),
                error: String(o.error || '')
              }});
            }})()""",
            timeout=2.5,
        )
        return res if isinstance(res, dict) else {"active": False, "chunks": [], "done": False, "seq": expected_seq, "nextCursor": cursor}

    def _parse_observed_sse_chunk(self, raw_chunk: str, state: dict, *, flush: bool = False) -> list[dict]:
        """Override in subclasses to parse observed SSE lines into text/reasoning/meta events."""
        return []

    # -- status & page probing -------------------------------------------

    def probe_page(self) -> dict:
        """Inspect live DOM state: URL, title, composer presence, human check."""
        script = f"""(() => {{
          const sels = {json.dumps(list(self.composer_selectors))};
          let hasComposer = false;
          let matchedComp = '';
          for (const s of sels) {{
            const el = document.querySelector(s);
            if (!el || el.disabled || el.hidden || el.getAttribute('aria-hidden') === 'true') continue;
            if (el.style && (el.style.display === 'none' || el.style.visibility === 'hidden')) continue;
            hasComposer = true;
            matchedComp = s;
            break;
          }}
          const bodyText = (document.body && document.body.innerText || '').slice(0, 600);
          return JSON.stringify({{
            href: String(location.href || ''),
            title: String(document.title || ''),
            has_composer: hasComposer,
            composer_sel: matchedComp,
            body_sample: bodyText
          }});
        }})()"""
        res = self.eval_js(script, timeout=3.0)
        if not isinstance(res, dict):
            return {"href": "", "title": "", "has_composer": False, "body_sample": ""}
        if res.get("href"):
            self.last_url = str(res["href"])
        return res

    def _is_login_url(self, href: str) -> bool:
        low = (href or "").lower()
        return any(h in low for h in self.login_url_hints)

    def _is_non_chat_url(self, href: str) -> bool:
        """Override in subclasses to force navigating back to origin from tool/landing subpages."""
        return False

    def verify(self) -> dict:
        if self.window is None:
            return self.status()
        probe = self.probe_page()
        href = str(probe.get("href") or "")
        title = str(probe.get("title") or "")
        body = str(probe.get("body_sample") or "")
        if _looks_like_human_check(title, body):
            self._signed_in_cache = False
            self._signed_in_at = time.time()
            return {
                "available": self.has_desktop_runtime(),
                "signed_in": False,
                "open": self.visible,
                "window": True,
                "human_check": True,
                "detail": f"{self.name} wants a manual human check in its window.",
            }
        cookie_names = self.read_cookie_names()
        _anon_waf = {"HWWAFSESID", "__cf_bm", "cf_clearance", "ssxmod_itna", "cna"}
        effective_auth_cookies = set(self.auth_cookie_names or ()) - _anon_waf
        cookie_ok = bool(effective_auth_cookies and (cookie_names & effective_auth_cookies))
        dom_ok = bool(probe.get("has_composer")) and not self._is_login_url(href)
        signed = bool((dom_ok or cookie_ok) and not self._is_login_url(href))
        self._signed_in_cache = signed
        self._signed_in_at = time.time()
        return {
            "available": self.has_desktop_runtime(),
            "signed_in": signed,
            "open": self.visible,
            "window": True,
            "detail": (
                f"Signed in ({self.origin.split('/')[2]})"
                if signed
                else f"Open the {self.name} window to sign in."
            ),
        }

    def status(self) -> dict:
        avail = self.has_desktop_runtime()
        if self.window is None:
            return {
                "available": avail,
                "signed_in": False,
                "open": False,
                "window": False,
                "detail": (
                    f"Click Sign in to open {self.name}."
                    if avail
                    else "Desktop window unavailable."
                ),
            }
        if self._signed_in_cache is not None and (time.time() - self._signed_in_at) < 4.0:
            signed = self._signed_in_cache
        else:
            return self.verify()
        return {
            "available": avail,
            "signed_in": signed,
            "open": self.visible,
            "window": True,
            "detail": (
                f"Signed in ({self.origin.split('/')[2]})"
                if signed
                else f"Open the {self.name} window to sign in."
            ),
        }

    # -- model discovery & selection -------------------------------------

    def _fallback_models(self) -> list[dict]:
        return [dict(m) for m in self.default_models]

    def _probe_live_models(self) -> dict | None:
        """Override in subclasses to query live models from the signed-in session."""
        return None

    def _apply_model_selection(self, model_id: str) -> bool | None:
        """Override to switch the provider page to ``model_id``.

        Return True once the page shows that model, False when it could not be
        switched. None (the default) means no switch happened.
        """
        return None

    def _known_model_id(self, want: str) -> str | None:
        """The listed id ``want`` refers to, or None when a live list lacks it.

        Without a live list there is nothing to check against, so ``want`` passes.
        """
        cache = self._models_cache or {}
        ids = [str(m.get("id") or "") for m in cache.get("models") or []]
        if not ids or want.lower() == "default":
            return want
        return next((mid for mid in ids if same_model_id(want, mid)), None)

    def _mark_active_model(self, model_id: str) -> None:
        """Record a switch in the cached list and make the next list call re-check."""
        if not self._models_cache:
            return
        models = [{**m, "active": str(m.get("id")) == model_id} for m in self._models_cache.get("models") or []]
        self._models_cache = {**self._models_cache, "models": models, "active_model": model_id}
        self._models_at = 0.0

    def select_model(self, model_id: str) -> dict:
        """Immediately apply a model selection in the live provider window when open."""
        want = (model_id or "").strip()
        if not want or self.window is None:
            return {"ok": True, "applied": False, "model_id": want}
        known = self._known_model_id(want)
        if known is None:
            return {
                "ok": False,
                "applied": False,
                "code": "unknown_model",
                "error": f"{want} is not in your {self.name} model list. Refresh the models and pick again.",
                "model_id": want,
            }
        if not self._turn_lock.acquire(blocking=False):
            return {"ok": True, "applied": False, "busy": True, "model_id": want}
        try:
            applied = bool(self._apply_model_selection(want))
            if applied:
                self._mark_active_model(known)
            return {"ok": True, "applied": applied, "model_id": want}
        except Exception as exc:
            LOG.debug("provider %s select_model(%s) failed: %s", self.id, want, exc)
            return {"ok": False, "applied": False, "error": str(exc), "model_id": want}
        finally:
            self._turn_lock.release()

    def _target_url_for_model(self, conversation_url: str, model_id: str) -> str:
        return conversation_url or self.origin

    def list_models(self, *, force: bool = False) -> dict:
        """Models for the picker, tagged with where they came from.

        ``source`` is "live" (just read from the signed-in page), "cached" (the
        last live list, when the page cannot answer now) or "default" (the
        built-in placeholder, before any live list was read).

        One check runs at a time. A call made while a check runs waits for it
        and returns its list: the picker keeps only its newest request, so a
        quick placeholder answer would replace the real list.
        """
        if not force and self._models_cache and (time.time() - self._models_at) < 60.0:
            return dict(self._models_cache)
        asked_at = time.time()
        if not self._models_probe_lock.acquire(timeout=20.0):
            return self._cached_or_default_models()
        try:
            if self._models_cache and self._models_at >= asked_at:
                return dict(self._models_cache)
            return self._check_live_models(force=force)
        finally:
            self._models_probe_lock.release()

    def _check_live_models(self, *, force: bool = False) -> dict:
        """Read the live list from the provider page, or fall back to the last one."""
        if self.window is None and self.has_desktop_runtime():
            if self.ensure_window(hidden=True):
                self._wait_for_composer(timeout=6.0 if force else 3.5)
        if self.window is None:
            return self._cached_or_default_models()
        if self._is_login_url(self.page_url()):
            # The window shows the provider's sign-in page: there is no model
            # list to read, and the placeholder must not pass for one.
            return {**self._cached_or_default_models(), "signed_in": False}
        # Never touch the live provider DOM (e.g. clicking dropdowns / Escape) while a turn is running.
        if not self._turn_lock.acquire(blocking=False):
            return self._cached_or_default_models()
        try:
            live_res = self._probe_live_models()
        except Exception as exc:
            LOG.debug("provider %s live model probe failed: %s", self.id, exc)
            live_res = None
        finally:
            self._turn_lock.release()
        if not (isinstance(live_res, dict) and live_res.get("models")):
            return self._cached_or_default_models()
        now = time.time()
        out = {
            "ok": True,
            "provider": self.id,
            "live": True,
            "source": "live",
            "signed_in": True,
            "active_model": str(live_res.get("active_model") or ""),
            "models": list(live_res["models"]),
            "fetched_at": now,
        }
        self._models_cache = out
        self._models_at = now
        return dict(out)

    def _cached_or_default_models(self) -> dict:
        if self._models_cache:
            return {**self._models_cache, "live": False, "source": "cached"}
        fallback = self._fallback_models()
        return {
            "ok": True,
            "provider": self.id,
            "live": False,
            "source": "default",
            "active_model": str(fallback[0]["id"]) if fallback else "",
            "models": fallback,
        }

    # -- DOM-driven submission & streaming -------------------------------

    def _read_messages_snapshot(self) -> dict:
        script = f"""(() => {{
          const msgSels = {json.dumps(list(self.assistant_selectors))};
          const thinkSels = {json.dumps(list(self.reasoning_selectors))};
          const stopSels = {json.dumps(list(self.stop_selectors))};
          const isChromeOrInput = (n) => Boolean(
            n.closest(
              'form, fieldset, nav, aside, header, footer, textarea, input, ' +
              '[contenteditable="true"], .ProseMirror, ' +
              '[data-testid="user-message"], [data-message-author-role="user"]'
            )
          );
          const blockTags = new Set(['P', 'DIV', 'LI', 'PRE', 'BLOCKQUOTE', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'TR']);
          const extractText = (root) => {{
            if (!root) return '';
            const parts = [];
            const walk = (node) => {{
              if (!node) return;
              if (node.nodeType === 3) {{ parts.push(node.nodeValue || ''); return; }}
              if (node.nodeType !== 1) return;
              const tag = node.tagName;
              if (tag === 'BR') {{ parts.push('\\n'); return; }}
              if (tag === 'STYLE' || tag === 'SCRIPT' || tag === 'NOSCRIPT') return;
              const isBlock = blockTags.has(tag);
              if (isBlock && parts.length && !parts[parts.length - 1].endsWith('\\n')) parts.push('\\n');
              for (const child of node.childNodes) walk(child);
              if (isBlock && parts.length && !parts[parts.length - 1].endsWith('\\n')) parts.push('\\n');
            }};
            walk(root);
            return parts.join('').replace(/\\n{{3,}}/g, '\\n\\n').trim();
          }};
          let nodes = [];
          let matchedSel = '';
          for (const s of msgSels) {{
            try {{
              const found = Array.from(document.querySelectorAll(s)).filter((n) => {{
                if (isChromeOrInput(n)) return false;
                const t = (n.innerText || n.textContent || '').trim();
                return t.length > 0;
              }});
              if (found.length) {{ nodes = found; matchedSel = s; break; }}
            }} catch (e) {{}}
          }}
          if (!nodes.length) {{
            const fallback = Array.from(document.querySelectorAll('main [class*="markdown"], main [class*="prose"], article [class*="markdown"], [class*="ds-markdown"]')).filter((n) => {{
              if (isChromeOrInput(n)) return false;
              const t = (n.innerText || n.textContent || '').trim();
              return t.length > 0;
            }});
            if (fallback.length) {{ nodes = fallback; matchedSel = 'fallback-markdown'; }}
          }}
          let text = '';
          let reasoning = '';
          if (nodes.length) {{
            const last = nodes[nodes.length - 1];
            const clone = last.cloneNode(true);
            clone.querySelectorAll('button, svg, [contenteditable="true"], .ProseMirror').forEach((n) => n.remove());
            for (const ts of thinkSels) {{
              try {{
                clone.querySelectorAll(ts).forEach((n) => {{
                  const t = extractText(n);
                  if (t) reasoning += (reasoning ? '\\n' : '') + t;
                  n.remove();
                }});
              }} catch (e) {{}}
            }}
            text = extractText(clone);
          }}
          let streaming = false;
          for (const s of stopSels) {{
            try {{
              const btn = document.querySelector(s);
              if (btn && !btn.disabled && btn.getAttribute('aria-hidden') !== 'true') {{ streaming = true; break; }}
            }} catch (e) {{}}
          }}
          return JSON.stringify({{
            count: nodes.length,
            sel: matchedSel,
            text,
            reasoning,
            streaming,
            href: String(location.href || '')
          }});
        }})()"""
        res = self.eval_js(script, timeout=4.0)
        if isinstance(res, dict):
            return res
        return {"count": 0, "text": "", "reasoning": "", "streaming": False, "href": ""}

    def _submit_prompt_dom(self, full_prompt: str) -> dict:
        """Type into the composer and submit strictly inside the composer's own scope."""
        # Ensure hidden-window rAF shim is active even if the page loaded before UserScript attached.
        self.eval_js(_HIDDEN_RAF_SHIM_JS, timeout=1.5)
        script = f"""(() => {{
          const promptText = {json.dumps(full_prompt)};
          const compSels = {json.dumps(list(self.composer_selectors))};
          const sendSels = {json.dumps(list(self.send_selectors))};
          const preferEnter = {json.dumps(bool(self.prefer_enter_submit))};
          let box = null;
          let matchedComp = '';
          for (const s of compSels) {{
            const el = document.querySelector(s);
            if (!el || el.disabled || el.hidden || el.getAttribute('aria-hidden') === 'true') continue;
            if (el.style && (el.style.display === 'none' || el.style.visibility === 'hidden')) continue;
            box = el;
            matchedComp = s;
            break;
          }}
          if (!box) return JSON.stringify({{ ok: false, reason: 'no_composer' }});
          box.focus();
          let method = 'none';
          if (box.tagName === 'TEXTAREA' || box.tagName === 'INPUT') {{
            const proto = box.tagName === 'TEXTAREA'
              ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(box, promptText);
            else box.value = promptText;
            box.dispatchEvent(new InputEvent('input', {{ bubbles: true, data: promptText, inputType: 'insertText' }}));
            box.dispatchEvent(new Event('change', {{ bubbles: true }}));
            method = 'value_setter';
          }} else {{
            const isLexical = box.getAttribute('data-lexical-editor') === 'true' || Boolean(box.__lexicalEditor);
            try {{
              box.click();
              box.focus();
              const sel = window.getSelection();
              if (sel) {{
                const target = box.querySelector('p') || box;
                const range = document.createRange();
                range.selectNodeContents(target);
                if (isLexical) {{
                  // Clear any leftover draft inside Lexical before inserting fresh text
                  if ((box.innerText || box.textContent || '').trim()) {{
                    sel.removeAllRanges();
                    sel.addRange(range);
                    try {{ document.execCommand('selectAll', false, null); document.execCommand('delete', false, null); }} catch (e) {{}}
                  }}
                  const t2 = box.querySelector('p') || box;
                  const r2 = document.createRange();
                  r2.selectNodeContents(t2);
                  r2.collapse(false);
                  sel.removeAllRanges();
                  sel.addRange(r2);
                }} else {{
                  sel.removeAllRanges();
                  sel.addRange(range);
                }}
              }}
              if (document.execCommand('insertText', false, promptText) && (box.innerText || box.textContent || '').trim()) {{
                method = 'execCommand';
              }}
            }} catch (e) {{}}
            if (!(box.innerText || box.textContent || '').trim()) {{
              try {{
                const dt = new DataTransfer();
                dt.setData('text/plain', promptText);
                box.dispatchEvent(new ClipboardEvent('paste', {{
                  clipboardData: dt, bubbles: true, cancelable: true
                }}));
                if ((box.innerText || box.textContent || '').trim()) method = 'paste';
              }} catch (e) {{}}
            }}
            if (!(box.innerText || box.textContent || '').trim()) {{
              try {{
                const dt2 = new DataTransfer();
                dt2.setData('text/plain', promptText);
                box.dispatchEvent(new InputEvent('beforeinput', {{
                  bubbles: true, cancelable: true, data: promptText, dataTransfer: dt2, inputType: 'insertFromPaste'
                }}));
                if ((box.innerText || box.textContent || '').trim()) method = 'beforeinput_paste';
              }} catch (e) {{}}
            }}
            if (!(box.innerText || box.textContent || '').trim() && !isLexical) {{
              try {{
                box.innerHTML = '';
                for (const line of String(promptText).split('\\n')) {{
                  const p = document.createElement('p');
                  p.textContent = line;
                  box.appendChild(p);
                }}
                box.dispatchEvent(new InputEvent('beforeinput', {{
                  bubbles: true, cancelable: true, data: promptText, inputType: 'insertText'
                }}));
                box.dispatchEvent(new InputEvent('input', {{
                  bubbles: true, data: promptText, inputType: 'insertText'
                }}));
                method = 'dom_paragraphs';
              }} catch (e) {{
                box.textContent = promptText;
                box.dispatchEvent(new InputEvent('input', {{
                  bubbles: true, data: promptText, inputType: 'insertText'
                }}));
                method = 'textContent';
              }}
            }} else if (!isLexical) {{
              // Ensure React/ProseMirror listeners observe an input event even in hidden WebKitGTK.
              box.dispatchEvent(new InputEvent('input', {{
                bubbles: true, data: promptText, inputType: 'insertText'
              }}));
            }}
          }}
          // Search ONLY inside the composer's own form/wrapper so we never click sidebar buttons.
          const findScopedSend = () => {{
            const scopes = [];
            const form = box.closest ? box.closest('form') : null;
            if (form) scopes.push(form);
            let cur = box.parentElement;
            for (let depth = 0; depth < 6 && cur; depth++) {{
              if (cur.tagName === 'BODY' || cur.tagName === 'MAIN') break;
              scopes.push(cur);
              cur = cur.parentElement;
            }}
            for (const scope of scopes) {{
              for (const s of sendSels) {{
                try {{
                  const found = Array.from(scope.querySelectorAll(s)).filter((btn) => {{
                    if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') return false;
                    if (btn.classList && btn.classList.contains('disabled')) return false;
                    const txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                    // Never click mode toggles (DeepThink/Search/Think/Attach)
                    if (txt && /(deepthink|search|think|attach|voice|upload|new chat)/.test(txt)) return false;
                    return true;
                  }});
                  if (found.length) return found[found.length - 1];
                }} catch (e) {{}}
              }}
            }}
            return null;
          }};
          const fireEnter = () => {{
            box.focus();
            const opts = {{ key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }};
            box.dispatchEvent(new KeyboardEvent('keydown', opts));
            box.dispatchEvent(new KeyboardEvent('keypress', opts));
            box.dispatchEvent(new KeyboardEvent('keyup', opts));
          }};
          let clickedSend = false;
          const trySend = (attempt) => {{
            if (clickedSend || (window.__apObs && window.__apObs.active)) return true;
            const curVal = (box.value !== undefined ? String(box.value) : (box.innerText || box.textContent || '')).trim();
            if (!curVal) return true; // already submitted and cleared!
            if (preferEnter && attempt === 0) {{
              fireEnter();
              return false; // still check button on attempt 1 if Enter didn't clear the box
            }}
            const btn = findScopedSend();
            if (btn) {{
              clickedSend = true;
              btn.click();
              return true;
            }}
            if (attempt >= 1) {{
              clickedSend = true;
              fireEnter();
              return true;
            }}
            return false;
          }};
          const delays = preferEnter ? [140, 750, 1250, 1800] : [140, 420, 820, 1300];
          let idx = 0;
          const step = () => {{
            if (trySend(idx)) return;
            idx += 1;
            if (idx < delays.length) setTimeout(step, delays[idx] - delays[idx - 1]);
          }};
          setTimeout(step, delays[0]);
          const boxLen = (box.value !== undefined ? String(box.value) : (box.innerText || box.textContent || '')).trim().length;
          return JSON.stringify({{ ok: true, method, composer: matchedComp, box_len: boxLen }});
        }})()"""
        res = self.eval_js(script, timeout=5.0)
        return res if isinstance(res, dict) else {"ok": False, "reason": "eval_failed"}

    def _wait_for_composer(self, timeout: float = 15.0) -> dict:
        deadline = time.time() + timeout
        last = {"href": "", "title": "", "has_composer": False, "body_sample": ""}
        while time.time() < deadline:
            last = self.probe_page()
            if _looks_like_human_check(last.get("title", ""), last.get("body_sample", "")):
                return last
            if self._is_login_url(last.get("href", "")):
                return last
            if last.get("has_composer"):
                return last
            time.sleep(0.35)
        return last

    def _stream_turn_api(
        self,
        chat_id: str,
        full_prompt: str,
        *,
        conversation_url: str = "",
        model: str = "",
        stop_event=None,
    ) -> Iterator[dict] | None:
        """Optional direct same-origin API streaming hook (used by Claude).

        Return an iterator if handled directly via API, or ``None`` to fall back
        to composer + observed fetch streaming.
        """
        return None

    def _poll_conversation_api(self, href: str) -> dict | None:
        """Optional same-origin conversation GET fallback for hidden GTK windows.

        Returns ``{"count": int, "text": str, "reasoning": str, "streaming": bool}``
        when the provider exposes a signed-in conversation history endpoint.
        """
        return None

    def _wait_for_origin(
        self,
        origin_host: str,
        timeout: float = 10.0,
        *,
        avoid_stale_hint: bool = False,
        expected_url: str = "",
        previous_url: str = "",
    ) -> dict:
        deadline = time.time() + timeout
        last = {"href": "", "title": "", "has_composer": False, "body_sample": ""}
        while time.time() < deadline:
            last = self.probe_page()
            href = str(last.get("href") or "")
            if _looks_like_human_check(last.get("title", ""), last.get("body_sample", "")):
                return last
            if self._is_login_url(href):
                return last
            if origin_host in href:
                if avoid_stale_hint and self.conversation_url_hint and self.conversation_url_hint in href:
                    time.sleep(0.25)
                    continue
                if expected_url and previous_url and href == previous_url and expected_url != previous_url:
                    time.sleep(0.25)
                    continue
                return last
            time.sleep(0.25)
        return last

    def stream_turn(
        self,
        chat_id: str,
        message: str,
        history: list,
        *,
        memory_block: str = "",
        workspace: str = "",
        conversation_url: str = "",
        model: str = "",
        permission_mode: str = "",
        is_followup_round: bool = False,
        stop_event=None,
    ) -> Iterator[dict]:
        # Serialize concurrent turns on the same web provider so two tabs never
        # clobber self.window navigation or window.__apObs.
        if not self._turn_lock.acquire(timeout=2.0):
            yield {"type": "agent", "stage": f"Waiting for another {self.name} tab to finish..."}
            while not self._turn_lock.acquire(timeout=0.3):
                if stop_event is not None and stop_event.is_set():
                    return
        try:
            self.eval_js("window.__apRafActive = true; 1;", timeout=1.0)
            yield from self._stream_turn_locked(
                chat_id,
                message,
                history,
                memory_block=memory_block,
                workspace=workspace,
                conversation_url=conversation_url,
                model=model,
                permission_mode=permission_mode,
                is_followup_round=is_followup_round,
                stop_event=stop_event,
            )
        finally:
            try:
                self.eval_js("window.__apRafActive = false; 1;", timeout=1.0)
            except Exception:
                pass
            self._turn_lock.release()

    def _stream_turn_locked(
        self,
        chat_id: str,
        message: str,
        history: list,
        *,
        memory_block: str = "",
        workspace: str = "",
        conversation_url: str = "",
        model: str = "",
        permission_mode: str = "",
        is_followup_round: bool = False,
        stop_event=None,
    ) -> Iterator[dict]:
        if not self.ensure_window(hidden=True):
            yield {
                "type": "error",
                "code": "no_window",
                "message": f"The {self.name} WebView window is unavailable in this environment.",
            }
            return

        target_url = self._target_url_for_model(conversation_url, model)
        current = self.page_url()
        origin_host = self.origin.split("/")[2]
        stale_conversation = bool(
            not is_followup_round
            and not conversation_url
            and (
                (self.conversation_url_hint and self.conversation_url_hint in (current or ""))
                or self._is_non_chat_url(current or "")
            )
        )
        # API-first providers (Claude) execute same-origin fetch() from any page
        # on origin_host — reloading on conversation_url would race with fetch()
        # and wipe window.__apStreams mid-stream!
        if self.api_nav_not_needed and current and origin_host in current:
            need_nav = False
        else:
            need_nav = (
                not current
                or current == "about:blank"
                or origin_host not in current
                or stale_conversation
                or (conversation_url and _norm_url(conversation_url) != _norm_url(current))
                or (not is_followup_round and not conversation_url and _norm_url(target_url) != _norm_url(self.origin) and _norm_url(target_url) != _norm_url(current))
            )
        if need_nav:
            yield {"type": "agent", "stage": f"Opening {self.name}..."}
            self.load_url(target_url)

        full_prompt = (memory_block + message) if memory_block else message

        # 1. Direct same-origin API streaming FIRST (identical to arena/engine.py):
        #    never blocks on hidden-window DOM composer visibility!
        origin_probe = self._wait_for_origin(
            origin_host,
            timeout=10.0,
            avoid_stale_hint=bool(need_nav and stale_conversation),
            expected_url=target_url if need_nav else "",
            previous_url=current if need_nav else "",
        )
        if _looks_like_human_check(origin_probe.get("title", ""), origin_probe.get("body_sample", "")):
            self.show()
            yield {
                "type": "error",
                "code": "human_check",
                "message": (
                    f"{self.name} asked for a human check. Complete it in the {self.name} "
                    "window, then send your message again."
                ),
                "open_window": True,
            }
            return
        if not self._is_login_url(origin_probe.get("href", "")):
            api_iter = self._stream_turn_api(
                chat_id,
                full_prompt,
                conversation_url=conversation_url,
                model=model,
                stop_event=stop_event,
            )
            if api_iter is not None:
                self._signed_in_cache = True
                self._signed_in_at = time.time()
                yield from api_iter
                return

        # If API streaming was not used (DOM fallback), ensure stale conversations
        # or mismatched URLs are navigated before typing into the composer.
        if self.api_nav_not_needed and not need_nav:
            cur_after = self.page_url()
            dom_need_nav = bool(
                (not is_followup_round and not conversation_url and self.conversation_url_hint and self.conversation_url_hint in (cur_after or ""))
                or (conversation_url and conversation_url != cur_after)
            )
            if dom_need_nav:
                self.load_url(target_url)
                origin_probe = self._wait_for_origin(
                    origin_host,
                    timeout=10.0,
                    avoid_stale_hint=bool(not conversation_url),
                    expected_url=target_url,
                    previous_url=cur_after,
                )

        probe = origin_probe if origin_probe.get("has_composer") else self._wait_for_composer(timeout=12.0)
        if _looks_like_human_check(probe.get("title", ""), probe.get("body_sample", "")):
            self.show()
            yield {
                "type": "error",
                "code": "human_check",
                "message": (
                    f"{self.name} asked for a human check. Complete it in the {self.name} "
                    "window, then send your message again."
                ),
                "open_window": True,
            }
            return

        if not probe.get("has_composer") or self._is_login_url(probe.get("href", "")):
            self.show()
            yield {
                "type": "error",
                "code": "auth",
                "message": (
                    f"Sign in to {self.name} in the window that just opened, "
                    "then send your message again."
                ),
                "open_window": True,
            }
            return

        # 2. Composer-driven submission + observation-only fetch SSE stream capture
        if model:
            try:
                switched = bool(self._apply_model_selection(model))
            except Exception as exc:
                LOG.debug("provider %s apply_model(%s) failed: %s", self.id, model, exc)
                switched = False
            if not switched:
                yield {
                    "type": "agent",
                    "stage": f"Could not switch {self.name} to {model}; using the model its page has selected.",
                }

        if self.observed_fetch_patterns:
            self._install_fetch_observer(self.observed_fetch_patterns)

        # Allow any existing conversation messages to settle before snapshotting count
        if conversation_url:
            time.sleep(0.35)
        before = self._read_messages_snapshot()
        before_count = int(before.get("count") or 0)
        before_text = str(before.get("text") or "")
        before_api_count = 0
        if conversation_url:
            api_before = self._poll_conversation_api(conversation_url)
            if isinstance(api_before, dict):
                before_api_count = int(api_before.get("count") or 0)

        yield {"type": "agent", "stage": f"Sending to {self.name}..."}
        submitted = self._submit_prompt_dom(full_prompt)
        diag.note(
            f"provider {self.id} submit ok={submitted.get('ok')} "
            f"method={submitted.get('method', '')} comp={submitted.get('composer', '')} "
            f"box_len={submitted.get('box_len', 0)} before_count={before_count}"
        )
        if not submitted.get("ok"):
            self.show()
            yield {
                "type": "error",
                "code": "submit_failed",
                "message": f"Could not type into {self.name}. Check the {self.name} window and try again.",
                "open_window": True,
            }
            return

        self._signed_in_cache = True
        self._signed_in_at = time.time()
        yield {"type": "agent", "stage": f"{self.name} is writing..."}

        emitted_text = ""
        emitted_reasoning = ""
        saved_href = conversation_url
        started = time.time()
        last_growth_at = started
        hard_deadline = started + max(float(self.turn_timeout) * 3.0, 900.0)
        last_api_poll = 0.0
        stable_ticks = 0
        saw_activity = False
        obs_state: dict = {"buf": ""}
        obs_used = False
        dom_used = False
        obs_cursor = 0
        obs_seq = 0
        obs_text_acc = ""
        obs_reason_acc = ""

        def _handle_obs_event(ev: dict) -> dict | None:
            nonlocal obs_used, emitted_text, emitted_reasoning, last_growth_at, saved_href
            nonlocal obs_text_acc, obs_reason_acc
            et = ev.get("type")
            if et == "text" and ev.get("text"):
                piece = str(ev["text"])
                obs_used = True
                obs_text_acc += piece
                last_growth_at = time.time()
                if dom_used:
                    if obs_text_acc == emitted_text:
                        return None
                    if obs_text_acc.startswith(emitted_text):
                        delta = obs_text_acc[len(emitted_text):]
                        emitted_text = obs_text_acc
                        return {"type": "text", "text": delta} if delta else None
                    if emitted_text.startswith(obs_text_acc):
                        return None
                    emitted_text = obs_text_acc
                    return {"type": "text_replace", "text": obs_text_acc}
                emitted_text += piece
                return ev
            if et == "text_replace" and ev.get("text") is not None:
                obs_used = True
                new_t = str(ev["text"])
                obs_text_acc = new_t
                emitted_text = new_t
                last_growth_at = time.time()
                return {"type": "text_replace", "text": new_t}
            if et == "reasoning" and ev.get("text"):
                piece = str(ev["text"])
                obs_used = True
                obs_reason_acc += piece
                last_growth_at = time.time()
                if dom_used:
                    if obs_reason_acc == emitted_reasoning:
                        return None
                    if obs_reason_acc.startswith(emitted_reasoning):
                        delta = obs_reason_acc[len(emitted_reasoning):]
                        emitted_reasoning = obs_reason_acc
                        return {"type": "reasoning", "text": delta} if delta else None
                    if emitted_reasoning.startswith(obs_reason_acc):
                        return None
                    emitted_reasoning = obs_reason_acc
                    return {"type": "reasoning_replace", "text": obs_reason_acc}
                emitted_reasoning += piece
                return ev
            if et == "reasoning_replace" and ev.get("text") is not None:
                obs_used = True
                new_r = str(ev["text"])
                obs_reason_acc = new_r
                emitted_reasoning = new_r
                last_growth_at = time.time()
                return {"type": "reasoning_replace", "text": new_r}
            if et == "meta_update" and ev.get("external_url"):
                if ev["external_url"] != saved_href:
                    saved_href = str(ev["external_url"])
                    return ev
            return None

        while (time.time() - last_growth_at) < self.turn_timeout and time.time() < hard_deadline:
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(self.poll_interval)

            # Prefer observation-only SSE stream when captured from the page's own fetch()
            if self.observed_fetch_patterns:
                obs = self._drain_observed_stream(cursor=obs_cursor, expected_seq=obs_seq)
                if obs.get("active"):
                    saw_activity = True
                    new_seq = int(obs.get("seq") or 0)
                    if obs_seq and new_seq and new_seq != obs_seq:
                        obs_cursor = 0
                    if new_seq:
                        obs_seq = new_seq
                    obs_cursor = max(obs_cursor, int(obs.get("nextCursor") or obs_cursor))
                    for raw_chunk in obs.get("chunks") or []:
                        for ev in self._parse_observed_sse_chunk(str(raw_chunk), obs_state):
                            out_ev = _handle_obs_event(ev)
                            if out_ev is not None:
                                yield out_ev
                    if obs.get("done"):
                        for ev in self._parse_observed_sse_chunk("", obs_state, flush=True):
                            out_ev = _handle_obs_event(ev)
                            if out_ev is not None:
                                yield out_ev
                        if obs_used:
                            cur_href = self.page_url()
                            if (
                                cur_href
                                and cur_href != saved_href
                                and self.conversation_url_hint
                                and self.conversation_url_hint in cur_href
                            ):
                                yield {"type": "meta_update", "external_url": cur_href}
                            return
                        obs_status = int(obs.get("status") or 0)
                        if obs_status >= 400 and not emitted_text and not emitted_reasoning:
                            if obs_status in (401, 403):
                                self.show()
                            yield {
                                "type": "error",
                                "code": "auth" if obs_status in (401, 403) else "provider_http",
                                "message": f"{self.name} returned HTTP {obs_status}. Check the {self.name} window and try again.",
                                "open_window": True,
                            }
                            return
                    if obs_used:
                        continue

            snap = self._read_messages_snapshot()
            href = str(snap.get("href") or "")
            if (
                href
                and href != saved_href
                and self.conversation_url_hint
                and self.conversation_url_hint in href
            ):
                saved_href = href
                yield {"type": "meta_update", "external_url": href}

            count = int(snap.get("count") or 0)
            streaming = bool(snap.get("streaming"))
            cur_text = str(snap.get("text") or "")
            cur_reason = str(snap.get("reasoning") or "")
            using_api_snap = False

            # Hidden-window fallback: if the conversation URL exists on the server
            # but hidden GTK DOM hasn't rendered assistant nodes yet, poll the
            # signed-in conversation history API directly!
            if (
                href
                and self.conversation_url_hint
                and self.conversation_url_hint in href
                and (count <= before_count or not cur_text)
                and (time.time() - last_api_poll) >= 1.0
            ):
                last_api_poll = time.time()
                api_snap = self._poll_conversation_api(href)
                if isinstance(api_snap, dict) and int(api_snap.get("count") or 0) > before_api_count:
                    using_api_snap = True
                    count = int(api_snap["count"])
                    cur_text = str(api_snap.get("text") or "")
                    cur_reason = str(api_snap.get("reasoning") or "")
                    streaming = bool(api_snap.get("streaming"))

            new_turn = using_api_snap or count > before_count or (count == before_count and cur_text != before_text)
            if streaming or new_turn:
                saw_activity = True

            if saw_activity and new_turn:
                if cur_reason and len(cur_reason) > len(emitted_reasoning) and cur_reason.startswith(emitted_reasoning):
                    delta_r = cur_reason[len(emitted_reasoning):]
                    emitted_reasoning = cur_reason
                    dom_used = True
                    last_growth_at = time.time()
                    yield {"type": "reasoning", "text": delta_r}
                elif cur_reason and cur_reason != emitted_reasoning:
                    emitted_reasoning = cur_reason
                    dom_used = True
                    last_growth_at = time.time()
                    yield {"type": "reasoning_replace", "text": cur_reason}

                if cur_text and len(cur_text) > len(emitted_text) and cur_text.startswith(emitted_text):
                    delta = cur_text[len(emitted_text):]
                    emitted_text = cur_text
                    dom_used = True
                    last_growth_at = time.time()
                    stable_ticks = 0
                    yield {"type": "text", "text": delta}
                elif cur_text and cur_text != emitted_text:
                    emitted_text = cur_text
                    dom_used = True
                    last_growth_at = time.time()
                    stable_ticks = 0
                    yield {"type": "text_replace", "text": cur_text}
                elif not streaming and (emitted_text or emitted_reasoning):
                    stable_ticks += 1
                    if stable_ticks >= 3:
                        return
                elif streaming and (emitted_text or emitted_reasoning) and (time.time() - last_growth_at) > 45.0:
                    return
            elif (
                not emitted_text
                and not emitted_reasoning
                and (
                    (not saw_activity and (time.time() - started) > 25.0)
                    or (saw_activity and not streaming and (time.time() - started) > 20.0)
                    or (time.time() - started) > 40.0
                )
            ):
                diag.note(
                    f"provider_turn_timeout provider={self.id} sel={snap.get('sel', '')} "
                    f"count={count} before={before_count} streaming={streaming} "
                    f"saw_activity={saw_activity} href={href[:120]}"
                )
                probe2 = self.probe_page()
                if _looks_like_human_check(probe2.get("title", ""), probe2.get("body_sample", "")):
                    self.show()
                    yield {
                        "type": "error",
                        "code": "human_check",
                        "message": f"{self.name} requested a human check. Complete it in the {self.name} window.",
                        "open_window": True,
                    }
                    return
                yield {
                    "type": "error",
                    "code": "timeout",
                    "message": f"{self.name} did not start replying within 25s. Check the {self.name} window.",
                    "open_window": True,
                }
                return

        if not emitted_text and not emitted_reasoning:
            yield {
                "type": "error",
                "code": "timeout",
                "message": f"{self.name} did not finish within {int(self.turn_timeout)}s.",
                "open_window": True,
            }
