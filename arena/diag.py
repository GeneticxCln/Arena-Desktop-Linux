"""Diagnostics: make the app say out loud what it is doing, step by step.

A freeze that leaves a clean log cannot be diagnosed from the outside — there
is nothing to read. This module turns silence into evidence:

* every traced function writes ``->`` on entry, ``<-`` on exit with its cost,
  and ``!!`` if it raised;
* anything slower than ``SLOW_CALL`` is flagged, so a stuck call is visible even
  when it eventually returns;
* a background watchdog writes a full stack dump of every thread whenever the
  main thread (or any traced call) is blocked for too long;
* the same trace can be replayed for methods that were not decorated, by
  wrapping the window object itself.

Everything goes to ``desktop.log`` and, for the heaviest detail, to
``desktop-trace.log`` next to it. The cost is small: the code is inert unless
``ARENA_DIAG`` is on, and it is on by default in diagnostic builds.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import sys
import threading
import time
import traceback

LOG = logging.getLogger("arena.desktop")

_installed = False

# A call slower than this is reported as suspicious even if it finishes.
SLOW_CALL = 1.0
# The watchdog dumps stacks when the traced call below stays inside one function
# for longer than this; that is the signature of a freeze, not of slow work.
STUCK_CALL = 5.0

_T0 = time.time()
_lock = threading.Lock()
_inflight: dict = {}          # thread ident -> (label, started_at, stack)
_calls = [0]                # simple counter, for the "still alive" line


def enabled() -> bool:
    """Diagnostics are on unless explicitly switched off."""
    return os.environ.get("ARENA_DIAG", "1") not in ("0", "false", "off")


def _ms(t: float) -> str:
    return f"{(time.time() - t) * 1000:.0f}ms"


def _where(skip: int = 3) -> str:
    """``file:line in function`` of the caller, for pointing at the real code."""
    try:
        frame = sys._getframe(skip)
        return f"{os.path.basename(frame.f_code.co_filename)}:{frame.f_lineno} в {frame.f_code.co_name}"
    except Exception:
        return "?"


def note(message: str, level: int = logging.INFO) -> None:
    """A one-line breadcrumb, stamped with the time since start.

    Steps inside one function are traced with this: when the log simply stops,
    the last breadcrumb names the exact line the thread is sitting on, which a
    function-level trace can never show.
    """
    if not enabled():
        return
    LOG.log(level, "[diag %7.1fs] %s", time.time() - _T0, message)


def trace(func):
    """Log entry, exit, cost and exceptions of one function.

    Written to be cheap and safe: a diagnostic that itself throws would hide
    the very problem it is meant to expose.
    """
    label = f"{func.__module__.rsplit('.', 1)[-1]}.{func.__qualname__}"

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not enabled():
            return func(*args, **kwargs)
        started = time.time()
        ident = threading.get_ident()
        with _lock:
            _calls[0] += 1
            try:
                stack = "".join(traceback.format_stack(limit=6)[:-1])
            except Exception:
                stack = ""
            _inflight[ident] = (label, started, stack)
        LOG.info("-> %s", label)
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            LOG.warning("!! %s raised %s: %s (%s)",
                        label, type(exc).__name__, exc, _ms(started))
            raise
        finally:
            with _lock:
                _inflight.pop(ident, None)
        cost = time.time() - started
        if cost >= SLOW_CALL:
            LOG.warning("<- %s took %.2f s  (SLOW)", label, cost)
        else:
            LOG.info("<- %s %s", label, _ms(started))
        return result

    wrapper.__arena_traced__ = True
    return wrapper


def dump_threads(reason: str) -> None:
    """Write every thread's stack — the picture of a freeze at one moment."""
    try:
        names = {t.ident: t.name for t in threading.enumerate()}
        chunks = [f"\n========== THREAD DUMP: {reason} ==========\n"]
        for ident, frame in sys._current_frames().items():
            chunks.append(f"\n--- поток {names.get(ident, ident)} ---\n")
            chunks.extend(traceback.format_stack(frame))
        text = "".join(chunks)
        LOG.critical("thread dump: %s\n%s", reason, text)
        try:
            path = trace_path()
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(text)
                fh.write(f"\n(MainThread alive: {threading.main_thread().is_alive()}, "
                         f"calls so far: {_calls[0]})\n")
        except Exception:
            LOG.exception("could not write the thread dump file")
    except Exception:
        LOG.exception("thread dump itself failed")


# ---------------------------------------------------------------------------
# Non-blocking window calls
# ---------------------------------------------------------------------------
# pywebview wraps show/hide/resize/move/restore/load_url in ``_shown_call``,
# whose first statement is ``self.events.shown.wait(20)`` — a plain
# ``threading.Event.wait`` that holds the GIL for those 20 seconds. On a window
# that was never shown (the Google window before sign-in, the arena pane when
# docking is off) that single wait stopped every Python thread in the process:
# the log froze mid-line, the watchdog could not run, and the crash file stayed
# empty because nothing had crashed. This is the freeze the user kept hitting.
#
# Fixing each call site is not enough — any future call could reintroduce it —
# so the whole window gets wrapped: a gated method runs on a daemon thread and
# the caller is told "queued" instead of being parked. Reads that return a
# value (get_cookies, evaluate_js) are left alone: they are already bounded by
# the caller-side helpers, and wrapping them would change their contract.

_BLOCKING_WINDOW_CALLS = ("show", "hide", "resize", "move", "restore", "load_url")
# Hold a reference to each wrapped window: id() is reused after garbage
# collection, so a plain set of ids can wrongly report a new window as done.
_wrapped_windows: "list" = []


def _glib_idle_add(fn) -> bool:
    """Schedule ``fn`` on the active GTK/GLib main loop without blocking."""
    try:
        import gi  # type: ignore
        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib, Gtk  # type: ignore
        if Gtk.main_level() <= 0:
            return False

        def _once():
            try:
                fn()
            except Exception:
                LOG.info("[window] GLib UI callback failed", exc_info=True)
            return False  # GLib.SOURCE_REMOVE: run once

        GLib.idle_add(_once)
        return True
    except Exception:
        return False


def run_on_ui_nonblocking(window, action, name: str = "ui-call") -> bool:
    """Run ``action`` safely on the GTK UI thread without blocking.

    Touching GTK/WebKitGTK widgets from a background thread (Flask workers,
    engine load callbacks) can corrupt state or crash under Wayland. When the
    GTK main loop is running and the caller is off the main thread, dispatch
    via ``GLib.idle_add``; otherwise run directly.
    """
    if window is None:
        return False
    if threading.current_thread() is not threading.main_thread():
        if _glib_idle_add(action):
            return True
    try:
        action()
        return True
    except Exception:
        LOG.info("[window] %s failed", name, exc_info=True)
        return False


def make_window_calls_non_blocking(window) -> None:
    """Marshal pywebview window calls onto the GTK main thread via GLib.idle_add.

    ``pywebview.platforms.gtk`` calls ``show_all()``, ``move()``, and
    ``resize()`` directly on the calling thread instead of via ``GLib.idle_add``.
    Wrapping these methods guarantees thread safety on GTK/Wayland while always
    calling through to the real method (never silently dropping calls).
    """
    if not enabled() or window is None:
        return
    if any(win is window for win in _wrapped_windows):
        return
    if getattr(window, "is_browser", False) is True:
        LOG.info("[window] browser window: its own calls kept")
        return
    if hasattr(window, "_factory") and hasattr(window, "ensure"):
        LOG.info("[window] lazy window: wrapped after creation, not before")
        return
    _wrapped_windows.append(window)

    wrapped = []
    for name in _BLOCKING_WINDOW_CALLS:
        original = getattr(window, name, None)
        if not callable(original):
            continue
        window.__dict__["_original_" + name] = original

        def _make_wrapper(fn, method_name):
            def _wrapped(*args, **kwargs):
                if threading.current_thread() is not threading.main_thread():
                    if _glib_idle_add(lambda: fn(*args, **kwargs)):
                        return None
                return fn(*args, **kwargs)
            return _wrapped

        try:
            setattr(window, name, _make_wrapper(original, name))
            wrapped.append(name)
        except Exception:
            LOG.info("[window] could not replace %s", name)

    if wrapped:
        LOG.info("[window] window calls marshaled for GTK thread safety: %s",
                 ", ".join(sorted(wrapped)))


def _start_watchdog() -> None:
    """Report a stuck call by itself, without waiting for a user to notice."""

    def loop():
        dumped = set()  # (ident, started) pairs already reported
        while True:
            time.sleep(2.0)
            try:
                now = time.time()
                with _lock:
                    items = list(_inflight.items())
                for ident, (label, started, stack) in items:
                    age = now - started
                    if age < STUCK_CALL:
                        continue
                    key = (ident, started)
                    if key in dumped:
                        # Already reported. Speak up again only after a minute,
                        # so a permanent freeze cannot flood the log and push
                        # the interesting lines out of it.
                        if age < 60 or int(age) % 30:
                            continue
                        LOG.critical("still stuck after %.0f s: %s", age, label)
                        continue
                    dumped.add(key)
                    LOG.critical("=" * 70)
                    LOG.critical("STUCK_CALL %s — не возвращается уже %.0f с (поток %s)",
                                 label, age, ident)
                    LOG.critical("вызвано отсюда:\n%s", stack.rstrip())
                    dump_threads(f"{label} не возвращается уже {age:.0f} с (поток {ident})")
            except Exception:
                LOG.exception("diagnostic watchdog failed")

    threading.Thread(target=loop, daemon=True, name="diag-watchdog").start()


def _heartbeat() -> None:
    """Print what every live thread is doing, once every few seconds.

    A freeze is not always one blocked call: sometimes a thread spins in a loop
    that never logs anything, or waits on a lock nobody releases. A cheap
    periodic picture of all stacks catches exactly those cases, and it is the
    only way to see a deadlock that never enters a traced function.
    """
    while True:
        time.sleep(10.0)
        try:
            frames = sys._current_frames()
            lines = []
            for ident, frame in frames.items():
                name = next((t.name for t in threading.enumerate() if t.ident == ident), "?")
                where = []
                f = frame
                while f is not None and len(where) < 6:
                    where.append(f"{os.path.basename(f.f_code.co_filename)}:{f.f_lineno} "
                                 f"{f.f_code.co_name}")
                    f = f.f_back
                lines.append(f"  [{name}] " + " <- ".join(where))
            with _lock:
                pending = {k: (v[0], round(time.time() - v[1], 1)) for k, v in _inflight.items()}
            LOG.info("[heartbeat] потоков=%d, вызовов в работе=%s\n%s",
                     len(lines), pending or "нет", "\n".join(sorted(lines)))
        except Exception:
            LOG.exception("heartbeat failed")


def watch_object(obj, name: str, methods) -> None:
    """Trace ``obj.<method>`` for the listed names, whatever their source.

    Used for the WebView window: the freeze lives in calls that reach into
    WebView2, and those are the ones worth timing.
    """
    if not enabled() or obj is None:
        return
    for method in methods:
        original = getattr(obj, method, None)
        if original is None or getattr(original, "__arena_wrapped__", False):
            continue
        # Only real callables may be wrapped. ``on_top`` is a property whose
        # value is a bool; wrapping it replaces True with a function object and
        # pywebview then dies with 'function value cannot be converted to
        # System.Boolean' while building the window — the window never opens.
        if not callable(original):
            continue
        # A property that returns a callable would break the same way: writing
        # the wrapper back through the setter stores a function, not a value.
        if isinstance(getattr(type(obj), method, None), property):
            continue

        def make(fn, label):
            @functools.wraps(fn)
            def wrapper(*a, **kw):
                started = time.time()
                ident = threading.get_ident()
                with _lock:
                    _calls[0] += 1
                    try:
                        stack = "".join(traceback.format_stack(limit=6)[:-1])
                    except Exception:
                        stack = ""
                    _inflight[ident] = (label, started, stack)
                LOG.info("-> %s", label)
                try:
                    return fn(*a, **kw)
                except BaseException as exc:
                    LOG.warning("!! %s raised %s: %s", label, type(exc).__name__, exc)
                    raise
                finally:
                    with _lock:
                        _inflight.pop(ident, None)
                    cost = time.time() - started
                    if cost >= SLOW_CALL:
                        LOG.warning("<- %s took %.2f s  (SLOW)", label, cost)
                    else:
                        LOG.info("<- %s %s", label, _ms(started))

            wrapper.__arena_wrapped__ = True
            return wrapper

        try:
            setattr(obj, method, make(original, f"{name}.{method}"))
        except Exception:
            LOG.info("could not trace %s.%s", name, method)


def log_path():
    """The file the diagnostics sink writes to, or None before it exists.

    Used by the cache wipe: deleting the log that is being written right now
    silently stops the log for the rest of the run, which turns any later
    report into a blind one.
    """
    from arena.config import CONFIG_DIR
    return CONFIG_DIR / "desktop.log"


def trace_path():
    """The stack-dump file; the cache wipe must also keep it while in use."""
    from arena.config import CONFIG_DIR
    return CONFIG_DIR / "desktop-trace.log"


def _ensure_sink() -> None:
    """Guarantee diagnostics have somewhere to go, and that each line goes once.

    Two things are easy to get wrong here, and both silently ruin the log:

    * Python's logging writes a record through *every* handler on the chain from
      the emitting logger up to the root. Attaching a file handler to
      ``arena.desktop`` while ``logging.basicConfig()`` has already attached one
      to the root writes every line twice, but only one copy carries the calling
      thread — which is exactly the information a freeze diagnosis needs.
    * A logger inherits the root level, which is WARNING by default, so INFO
      trace lines are dropped before any handler sees them.

    So the handler goes on the root logger and only there: it is the one place
    that sees every record exactly once, and ``basicConfig()`` later becomes a
    no-op instead of adding a second sink.
    """
    try:
        from arena.config import CONFIG_DIR
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        target = os.path.normcase(os.path.abspath(str(CONFIG_DIR / "desktop.log")))

        root_logger = logging.getLogger()
        for handler in root_logger.handlers:
            if not isinstance(handler, logging.FileHandler):
                continue
            # Windows paths differ in case and separator style between the
            # config module and logging, so compare normalised absolute paths.
            if os.path.normcase(os.path.abspath(handler.baseFilename)) == target:
                handler.setLevel(logging.DEBUG)
                if root_logger.level > logging.DEBUG or not root_logger.level:
                    root_logger.setLevel(logging.DEBUG)
                return

        handler = logging.FileHandler(target, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s"))
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.DEBUG)
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        LOG.info("diagnostics sink ready: %s", target)
    except Exception:
        LOG.exception("could not attach the diagnostics file")


def install() -> None:
    """Turn diagnostics on: banner, watchdog, slow-call reporting."""
    global _installed
    if not enabled():
        LOG.info("diagnostics disabled (ARENA_DIAG=0)")
        return
    if _installed:
        return
    _installed = True
    _ensure_sink()
    LOG.info("=" * 70)
    LOG.info("DIAGNOSTICS ON — each call is logged with entry, exit and cost")
    LOG.info("python=%s frozen=%s argv=%s", sys.version.split()[0],
             getattr(sys, "frozen", False), sys.argv[1:])
    LOG.info("threads at start: %s", [t.name for t in threading.enumerate()])
    LOG.info("=" * 70)
    _start_watchdog()
    threading.Thread(target=_heartbeat, daemon=True, name="diag-heartbeat").start()
