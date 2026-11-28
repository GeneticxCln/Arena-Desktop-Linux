"""Browser tools for agents: drive a private Chrome over the DevTools protocol.

The design follows vercel-labs/agent-browser (Apache-2.0); this is a fresh
Python implementation, no code copied. The loop an agent runs:

    open a page -> snapshot (accessibility tree, interactive elements numbered
    @e1..@eN) -> click / fill by ref -> snapshot again after the page changes

A ref maps to Chrome's ``backendDOMNodeId``. Refs are dropped when the main
frame navigates, so a stale ref fails with "take a new snapshot" instead of
acting on whatever now sits at that id.

Safety choices:

- Chrome runs with ``--remote-debugging-pipe``: the protocol travels over file
  descriptors 3 and 4 of the child, never a TCP port, so no other program on
  the computer can attach to the agent's browser.
- It uses its own profile directory (mode 0700), never the user's Chrome profile.
- Only http(s) URLs and ``about:blank`` are opened.
- One tab per chat, so chats cannot take over each other's page.
"""

from __future__ import annotations

import atexit
import base64
import fcntl
import fnmatch
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 25.0
SNAPSHOT_MAX_LINES = 400
TEXT_LIMIT = 6000

INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox", "listbox",
    "option", "menuitem", "menuitemcheckbox", "menuitemradio", "slider", "spinbutton",
    "switch", "tab", "treeitem",
})
CONTEXT_ROLES = frozenset({"heading", "dialog", "alert", "alertdialog"})
TEXT_ROLES = frozenset({"StaticText", "paragraph", "cell", "listitem"})
STATE_PROPS = ("checked", "selected", "expanded", "disabled", "pressed")

CHROME_NAMES = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "chrome")


class BrowserError(Exception):
    """A browser action failed; the message is written for the model to act on."""


def find_chrome() -> str | None:
    for name in CHROME_NAMES:
        path = shutil.which(name)
        if path:
            return path
    return None


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_HOST_PORT_RE = re.compile(r"^(\[[0-9a-fA-F:]+\]|[^:/\[\]]+)(:\d{1,5})?$")
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def _ip_is_private(ip) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
        return True
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified


def is_private_host(host: str, *, resolve_dns: bool = False) -> bool:
    """True for loopback, private, CGNAT, link-local, non-canonical IPv4 and local-only names."""
    name = (host or "").strip().lower().strip("[]")
    if not name:
        return False
    if name == "localhost" or name.endswith(_PRIVATE_SUFFIXES):
        return True
    try:
        return _ip_is_private(ipaddress.ip_address(name))
    except ValueError:
        pass
    # Catch non-canonical IPv4 forms accepted by browsers (e.g. 2130706433, 127.1, 0x7f000001, 0177.0.0.1).
    try:
        packed = socket.inet_aton(name)
        return _ip_is_private(ipaddress.IPv4Address(packed))
    except OSError:
        pass
    if resolve_dns:
        try:
            infos = socket.getaddrinfo(name, None)
        except OSError:
            return False
        for _fam, _type, _proto, _canon, sockaddr in infos:
            addr_text = str(sockaddr[0]).split("%", 1)[0]
            try:
                if _ip_is_private(ipaddress.ip_address(addr_text)):
                    return True
            except ValueError:
                continue
    return False


def url_host(url: str) -> str:
    return (urlparse(url).hostname or "") if url else ""


def check_url(url: str) -> str:
    """The URL to open, or BrowserError for anything that is not a web page.

    Without a scheme, ``example.com/x`` becomes https, while ``localhost:3000``
    and private addresses become http, which is what local dev servers speak.
    ``javascript:``, ``data:``, ``file:``, ``view-source:`` and the like are refused.
    """
    raw = (url or "").strip()
    if not raw:
        raise BrowserError("browser_open needs a URL.")
    if raw == "about:blank":
        return raw
    if not _SCHEME_RE.match(raw):
        head = raw.split("/", 1)[0]
        if not _HOST_PORT_RE.match(head):
            raise BrowserError(f"Only http and https pages can be opened, not {raw[:40]!r}.")
        host = head.rsplit(":", 1)[0] if not head.startswith("[") else head.split("]", 1)[0] + "]"
        raw = ("http://" if is_private_host(host) else "https://") + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise BrowserError(f"Only http and https pages can be opened, not {parsed.scheme}:.")
    if not parsed.netloc:
        raise BrowserError(f"{raw[:80]!r} has no host.")
    return raw


# -- snapshot ---------------------------------------------------------------

def _ax_value(node: dict, key: str) -> str:
    field = node.get(key)
    if isinstance(field, dict):
        return str(field.get("value") or "")
    return ""


def _ax_props(node: dict) -> dict:
    out = {}
    for prop in node.get("properties") or []:
        name = prop.get("name")
        value = (prop.get("value") or {}).get("value")
        if name:
            out[name] = value
    return out


def render_snapshot(nodes: list, title: str, url: str, full: bool = False) -> tuple[str, dict]:
    """Text snapshot of an accessibility tree, and the ref -> backendDOMNodeId map.

    Interactive elements get refs. Headings, dialogs and alerts are shown for
    context. ``full`` adds plain page text (paragraphs, list items, cells).
    """
    by_id = {str(n.get("nodeId")): n for n in nodes}
    child_ids = {c for n in nodes for c in (n.get("childIds") or [])}
    roots = [n for n in nodes if str(n.get("nodeId")) not in child_ids] or nodes[:1]
    lines = [f"Page: {title}", f"URL: {url}", ""]
    refs: dict[str, int] = {}
    seen: set[str] = set()

    def visit(node: dict, depth: int, in_dropdown: bool = False) -> None:
        node_id = str(node.get("nodeId"))
        if node_id in seen or len(lines) > SNAPSHOT_MAX_LINES + 3:
            return
        seen.add(node_id)
        shown = False
        if not node.get("ignored"):
            role = _ax_value(node, "role")
            name = " ".join(_ax_value(node, "name").split())
            backend = node.get("backendDOMNodeId")
            text = None
            if role == "option" and in_dropdown:
                # A closed dropdown's choices are picked with browser_select on the
                # dropdown itself; they cannot be clicked, so they get no ref.
                text = f'- option "{name[:120]}"' + (" selected" if _ax_props(node).get("selected") in (True, "true") else "")
            elif role in INTERACTIVE_ROLES and backend is not None:
                ref = f"e{len(refs) + 1}"
                refs[ref] = int(backend)
                text = f"@{ref} [{role}]" + (f' "{name[:120]}"' if name else "")
                props = _ax_props(node)
                for state in STATE_PROPS:
                    if props.get(state) in (True, "true", "mixed"):
                        text += f" {state}"
                if props.get("value") not in (None, ""):
                    text += f' value="{str(props["value"])[:80]}"'
            elif role in CONTEXT_ROLES and name:
                text = f'[{role}] "{name[:160]}"'
            elif full and role in TEXT_ROLES and name:
                text = name[:300]
            if text is not None:
                lines.append("  " * depth + text)
                shown = True
        # Only a combobox (a closed <select>) hides its options; a listbox's options are clickable.
        dropdown = in_dropdown or (_ax_value(node, "role") == "combobox" and not node.get("ignored"))
        for child_id in node.get("childIds") or []:
            child = by_id.get(str(child_id))
            if child is not None:
                visit(child, depth + (1 if shown else 0), dropdown)

    for root in roots:
        visit(root, 0)
    if len(lines) > SNAPSHOT_MAX_LINES + 3:
        lines = lines[: SNAPSHOT_MAX_LINES + 3]
        lines.append("[snapshot cut at %d lines; use browser_read for the rest]" % SNAPSHOT_MAX_LINES)
    if not refs:
        lines.append("(no interactive elements; the page may still be loading - try browser_wait)")
    return "\n".join(lines).rstrip() + "\n", refs


# -- keys -------------------------------------------------------------------

_NAMED_KEYS = {
    "Enter": ("Enter", 13, "\r"), "Tab": ("Tab", 9, ""), "Escape": ("Escape", 27, ""),
    "Backspace": ("Backspace", 8, ""), "Delete": ("Delete", 46, ""), "Space": ("Space", 32, " "),
    "ArrowUp": ("ArrowUp", 38, ""), "ArrowDown": ("ArrowDown", 40, ""),
    "ArrowLeft": ("ArrowLeft", 37, ""), "ArrowRight": ("ArrowRight", 39, ""),
    "Home": ("Home", 36, ""), "End": ("End", 35, ""), "PageUp": ("PageUp", 33, ""),
    "PageDown": ("PageDown", 34, ""),
}
_MODIFIERS = {"Alt": 1, "Control": 2, "Ctrl": 2, "Meta": 4, "Cmd": 4, "Shift": 8}


def key_events(combo: str) -> tuple[dict, dict]:
    """keyDown/keyUp parameters for ``Enter``, ``a`` or ``Control+a``."""
    parts = [p for p in (combo or "").split("+") if p]
    if not parts:
        raise BrowserError("browser_press needs a key, for example Enter or Control+a.")
    *mods, key = parts
    modifiers = 0
    for mod in mods:
        if mod not in _MODIFIERS:
            raise BrowserError(f"Unknown modifier {mod!r}; use Control, Shift, Alt or Meta.")
        modifiers |= _MODIFIERS[mod]
    if key in _NAMED_KEYS:
        name, code, text = _NAMED_KEYS[key]
    elif len(key) == 1:
        name, code, text = key, ord(key.upper()), key
    else:
        raise BrowserError(f"Unknown key {key!r}.")
    down = {"type": "keyDown", "key": name, "windowsVirtualKeyCode": code, "modifiers": modifiers}
    if text and not modifiers & (2 | 4):
        down["text"] = text
    up = {"type": "keyUp", "key": name, "windowsVirtualKeyCode": code, "modifiers": modifiers}
    return down, up


# -- transport --------------------------------------------------------------

class CdpPipe:
    """A DevTools-protocol connection over Chrome's --remote-debugging-pipe.

    Messages are JSON, each terminated by a NUL byte. One reader thread
    dispatches responses to waiting callers and events to ``on_event``.
    """

    def __init__(self, write_fd: int, read_fd: int, on_event: Callable[[str | None, str, dict], None]):
        self._write_fd = write_fd
        self._read_fd = read_fd
        self._on_event = on_event
        # _lock guards the reply table only; _write_lock serializes writes. They are
        # separate so a write blocked on a full pipe never stops replies being delivered.
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._waiting: dict[int, list] = {}
        self._closed = False
        threading.Thread(target=self._read_loop, name="agent-browser-cdp", daemon=True).start()

    def send(self, method: str, params: dict | None = None, session_id: str | None = None,
             timeout: float = 15.0) -> dict:
        done = threading.Event()
        slot: list = [done, None]
        with self._lock:
            if self._closed:
                raise BrowserError("The browser is closed.")
            self._next_id += 1
            msg_id = self._next_id
            self._waiting[msg_id] = slot
        message = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        data = json.dumps(message).encode("utf-8") + b"\0"
        try:
            with self._write_lock:
                if self._closed or self._write_fd < 0:
                    raise OSError("pipe closed")
                while data:
                    data = data[os.write(self._write_fd, data):]
        except OSError as exc:
            with self._lock:
                self._waiting.pop(msg_id, None)
            raise BrowserError(f"The browser connection failed: {exc}") from exc
        if not done.wait(timeout):
            with self._lock:
                self._waiting.pop(msg_id, None)
            raise BrowserError(f"The browser did not answer {method} within {timeout:.0f} s.")
        reply = slot[1] or {}
        if "error" in reply:
            raise BrowserError(f"{method} failed: {(reply['error'] or {}).get('message', reply['error'])}")
        return reply.get("result") or {}

    def _read_loop(self) -> None:
        buffer = b""
        while True:
            try:
                chunk = os.read(self._read_fd, 1 << 16)
            except OSError:
                chunk = b""
            if not chunk:
                break
            buffer += chunk
            while b"\0" in buffer:
                raw, buffer = buffer.split(b"\0", 1)
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if "id" in message:
                    with self._lock:
                        slot = self._waiting.pop(message["id"], None)
                    if slot is not None:
                        slot[1] = message
                        slot[0].set()
                elif "method" in message:
                    try:
                        self._on_event(message.get("sessionId"), message["method"], message.get("params") or {})
                    except Exception:
                        LOG.exception("browser event handler failed")
        with self._lock:
            self._closed = True
            waiting, self._waiting = self._waiting, {}
        for slot in waiting.values():
            slot[1] = {"error": {"message": "the browser exited"}}
            slot[0].set()

    def close(self) -> None:
        self._closed = True
        waiting = {}
        if self._lock.acquire(timeout=0.5):
            try:
                waiting, self._waiting = self._waiting, {}
            finally:
                self._lock.release()
        for slot in waiting.values():
            slot[1] = {"error": {"message": "the browser closed"}}
            slot[0].set()
        acquired_w = self._write_lock.acquire(timeout=0.5)
        try:
            wfd, self._write_fd = self._write_fd, -1
            if wfd >= 0:
                try:
                    os.close(wfd)
                except OSError:
                    pass
        finally:
            if acquired_w:
                self._write_lock.release()
        rfd, self._read_fd = self._read_fd, -1
        if rfd >= 0:
            try:
                os.close(rfd)
            except OSError:
                pass


# -- one chat's tab ---------------------------------------------------------

class Tab:
    """One chat's page: snapshot refs, navigation state and the actions on it."""

    def __init__(self, conn, target_id: str, session_id: str):
        self.conn = conn
        self.target_id = target_id
        self.session_id = session_id
        self.refs: dict[str, int] = {}
        self.lock = threading.RLock()
        self._loads = 0
        self._navigations = 0
        self._local_allowed = False
        self._load_event = threading.Condition()
        self.last_used = time.time()

    def _send(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        return self.conn.send(method, params, session_id=self.session_id, timeout=timeout)

    def on_event(self, method: str, params: dict) -> None:
        if method == "Page.frameNavigated" and not (params.get("frame") or {}).get("parentId"):
            with self._load_event:
                self.refs = {}
                self._navigations += 1
                self._load_event.notify_all()
        elif method == "Page.loadEventFired":
            with self._load_event:
                self._loads += 1
                self._load_event.notify_all()

    def _wait_for_load(self, loads_before: int, timeout: float) -> bool:
        deadline = time.time() + timeout
        with self._load_event:
            while self._loads <= loads_before:
                left = deadline - time.time()
                if left <= 0:
                    return False
                self._load_event.wait(left)
        return True

    def _eval(self, expression: str, timeout: float = 15.0):
        result = self._send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                  "awaitPromise": True}, timeout=timeout)
        if result.get("exceptionDetails"):
            raise BrowserError("The page script failed: "
                               + str((result["exceptionDetails"].get("exception") or {}).get("description", ""))[:200])
        return (result.get("result") or {}).get("value")

    def _guard_local_network(self, allowed: bool, href: str | None = None) -> None:
        """Stop a page from steering the browser onto this computer or the local network.

        Opening a local address is asked for explicitly (it never runs without a
        prompt outside Bypass). A public page that redirects there, or a click
        that lands there, is turned back to a blank page instead.
        """
        if allowed:
            return
        if href is None:
            href = str(self._eval("location.href") or "")
        if is_private_host(url_host(href), resolve_dns=True):
            self._send("Page.navigate", {"url": "about:blank"})
            self.refs = {}
            raise BrowserError(
                f"The page sent the browser to a local-network address ({url_host(href)}), so it was "
                "stopped. Open that address yourself with browser_open if the user wants it; it will ask first.")

    def _backend(self, ref: str) -> int:
        key = (ref or "").strip().lstrip("@")
        if not (key.startswith("e") and key[1:].isdigit()):
            raise BrowserError(f"{ref!r} is not a ref. Use a ref like @e3 from browser_snapshot.")
        backend = self.refs.get(key)
        if backend is None:
            raise BrowserError(f"@{key} is not on the current page (it changed or was never "
                               "listed). Take a new browser_snapshot and use its refs.")
        return backend

    def _call_on(self, backend: int, function: str, *args):
        node = self._send("DOM.resolveNode", {"backendNodeId": backend})
        object_id = (node.get("object") or {}).get("objectId")
        if not object_id:
            raise BrowserError("That element is gone. Take a new browser_snapshot.")
        result = self._send("Runtime.callFunctionOn", {
            "objectId": object_id, "functionDeclaration": function, "returnByValue": True,
            "arguments": [{"value": a} for a in args]})
        if result.get("exceptionDetails"):
            raise BrowserError("The page script failed on that element.")
        return (result.get("result") or {}).get("value")

    # actions ------------------------------------------------------------

    def open(self, url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        target = check_url(url)
        with self.lock:
            self.last_used = time.time()
            self._local_allowed = is_private_host(url_host(target), resolve_dns=True)
            before = self._loads
            result = self._send("Page.navigate", {"url": target}, timeout=timeout)
            if result.get("errorText"):
                raise BrowserError(f"Could not open {target}: {result['errorText']}")
            loaded = self._wait_for_load(before, timeout) if result.get("loaderId") else True
            title, href = self._eval("[document.title, location.href]") or ["", target]
            self._guard_local_network(self._local_allowed, str(href or ""))
            note = "" if loaded else " (still loading; use browser_wait before acting)"
            return f"Opened {href}{note}. Title: {title}"

    def snapshot(self, full: bool = False) -> str:
        with self.lock:
            self.last_used = time.time()
            navigations = self._navigations
            nodes = self._send("Accessibility.getFullAXTree", {}, timeout=20.0).get("nodes") or []
            title, href = self._eval("[document.title, location.href]") or ["", ""]
            self._guard_local_network(self._local_allowed, str(href or ""))
            text, refs = render_snapshot(nodes, title=title, url=href, full=full)
            with self._load_event:
                if self._navigations != navigations:
                    # The page navigated while the tree was read: its refs belong to the old page.
                    self.refs = {}
                    return text + "\n[The page changed while this snapshot was taken; its refs are void. Take another.]\n"
                self.refs = refs
            return text

    def click(self, ref: str) -> str:
        with self.lock:
            self.last_used = time.time()
            backend = self._backend(ref)
            try:
                self._send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend})
                quads = self._send("DOM.getContentQuads", {"backendNodeId": backend}).get("quads") or []
            except BrowserError as exc:
                raise BrowserError(f"Cannot click {ref}: {exc}. Take a new browser_snapshot.") from exc
            if not quads:
                raise BrowserError(f"{ref} is not visible on the page, so it cannot be clicked.")
            quad = quads[0]
            x = sum(quad[0::2]) / 4
            y = sum(quad[1::2]) / 4
            navigations, loads = self._navigations, self._loads
            for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
                params = {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1}
                if kind == "mouseMoved":
                    params = {"type": kind, "x": x, "y": y}
                self._send("Input.dispatchMouseEvent", params)
            time.sleep(0.15)
            if self._navigations != navigations:
                self._wait_for_load(loads, 10.0)
                self._guard_local_network(self._local_allowed)
                return f"Clicked {ref}; the page navigated, so take a new browser_snapshot."
            self._guard_local_network(self._local_allowed)
            return f"Clicked {ref}."

    def fill(self, ref: str, text: str) -> str:
        with self.lock:
            self.last_used = time.time()
            self._guard_local_network(self._local_allowed)
            backend = self._backend(ref)
            self._send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend})
            self._send("DOM.focus", {"backendNodeId": backend})
            cleared = self._call_on(backend, """function() {
                if ('value' in this && (this.tagName === 'INPUT' || this.tagName === 'TEXTAREA')) {
                    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(this), 'value').set;
                    setter.call(this, '');
                    this.dispatchEvent(new Event('input', { bubbles: true }));
                    return true;
                }
                if (this.isContentEditable) { document.execCommand('selectAll'); return true; }
                return false;
            }""")
            if not cleared:
                raise BrowserError(f"{ref} is not a text field.")
            if text:
                self._send("Input.insertText", {"text": str(text)})
            return f"Filled {ref}."

    def press(self, key: str) -> str:
        down, up = key_events(key)
        with self.lock:
            self.last_used = time.time()
            navigations, loads = self._navigations, self._loads
            self._send("Input.dispatchKeyEvent", down)
            self._send("Input.dispatchKeyEvent", up)
            time.sleep(0.1)
            if self._navigations != navigations:
                self._wait_for_load(loads, 10.0)
                self._guard_local_network(self._local_allowed)
                return f"Pressed {key}; the page navigated, so take a new browser_snapshot."
            self._guard_local_network(self._local_allowed)
            return f"Pressed {key}."

    def select(self, ref: str, value: str) -> str:
        with self.lock:
            self.last_used = time.time()
            self._guard_local_network(self._local_allowed)
            backend = self._backend(ref)
            chosen = self._call_on(backend, """function(want) {
                if (this.tagName !== 'SELECT') return null;
                const w = String(want).trim().toLowerCase();
                const opt = Array.from(this.options).find(o => o.value.toLowerCase() === w)
                    || Array.from(this.options).find(o => o.label.trim().toLowerCase() === w);
                if (!opt) return '';
                this.value = opt.value;
                this.dispatchEvent(new Event('input', { bubbles: true }));
                this.dispatchEvent(new Event('change', { bubbles: true }));
                return opt.label || opt.value;
            }""", str(value))
            if chosen is None:
                raise BrowserError(f"{ref} is not a select element; click it and pick the option instead.")
            if not chosen:
                raise BrowserError(f"{ref} has no option {value!r}.")
            return f"Selected {chosen!r} in {ref}."

    def wait(self, kind: str, value: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        kind = (kind or "").strip().lower()
        if kind not in ("text", "url", "ref"):
            raise BrowserError("browser_wait needs text, url or ref, for example: text Dashboard.")
        deadline = time.time() + max(0.5, min(float(timeout), 120.0))
        while True:
            with self.lock:
                self.last_used = time.time()
                self._guard_local_network(self._local_allowed)
                if kind == "text":
                    found = bool(self._eval(f"!!(document.body && document.body.innerText.includes({json.dumps(value)}))"))
                elif kind == "url":
                    found = fnmatch.fnmatch(str(self._eval("location.href") or ""), value)
                else:
                    try:
                        found = bool(self._send("DOM.getContentQuads",
                                                {"backendNodeId": self._backend(value)}).get("quads"))
                    except BrowserError:
                        found = False
            if found:
                return f"Done waiting: {kind} {value}."
            if time.time() >= deadline:
                raise BrowserError(f"Timed out waiting for {kind} {value!r}.")
            time.sleep(0.25)

    def read(self, ref: str | None = None) -> str:
        with self.lock:
            self.last_used = time.time()
            self._guard_local_network(self._local_allowed)
            if ref:
                text = self._call_on(self._backend(ref), "function() { return this.innerText || this.value || this.textContent || ''; }")
            else:
                text = self._eval("document.body ? document.body.innerText : ''")
            text = str(text or "")
            if len(text) > TEXT_LIMIT:
                text = text[:TEXT_LIMIT] + f"\n[cut at {TEXT_LIMIT} characters]"
            return text

    def screenshot(self, path: Path) -> Path:
        with self.lock:
            self.last_used = time.time()
            self._guard_local_network(self._local_allowed)
            data = self._send("Page.captureScreenshot", {"format": "png"}, timeout=30.0).get("data") or ""
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(data))
            return path

    def back(self, timeout: float = DEFAULT_TIMEOUT) -> str:
        with self.lock:
            self.last_used = time.time()
            history = self._send("Page.getNavigationHistory")
            index = history.get("currentIndex", 0)
            entries = history.get("entries") or []
            if index <= 0:
                raise BrowserError("There is no earlier page in this tab.")
            loads = self._loads
            self._send("Page.navigateToHistoryEntry", {"entryId": entries[index - 1]["id"]})
            self._wait_for_load(loads, timeout)
            self._guard_local_network(self._local_allowed)
            return f"Went back to {entries[index - 1].get('url', '')}."


# -- the browser ------------------------------------------------------------

class ChromeBrowser:
    """A private Chrome started on first use, with one tab per chat."""

    def __init__(self, chrome_path: str | None = None, profile_dir: Path | None = None,
                 headless: bool = True):
        self.chrome_path = chrome_path
        self.profile_dir = Path(profile_dir) if profile_dir else Path.home() / ".arena-personal" / "agent-browser" / "profile"
        self.headless = headless
        self.pid: int | None = None
        self.tabs: dict[str, Tab] = {}
        self._conn: CdpPipe | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> CdpPipe:
        if self._conn is not None and not self._conn._closed:
            return self._conn
        if self._conn is not None:
            # Chrome exited or crashed: forget its tabs (each chat gets a fresh one) and reap it.
            self._conn.close()
            self._conn = None
            self.tabs = {}
            self._reap(self.pid)
            self.pid = None
        chrome = self.chrome_path or find_chrome()
        if not chrome:
            raise BrowserError("Chrome or Chromium is not installed, so the browser tools are unavailable.")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.profile_dir, 0o700)
        self._clear_orphan()
        # Our ends stay in this process; the child's ends are moved to fds 3 and 4.
        cmd_read, cmd_write = os.pipe()
        resp_read, resp_write = os.pipe()
        # High fd numbers so the dup2 into 3 and 4 cannot clobber the other end.
        child_in = fcntl.fcntl(cmd_read, fcntl.F_DUPFD_CLOEXEC, 10)
        child_out = fcntl.fcntl(resp_write, fcntl.F_DUPFD_CLOEXEC, 10)
        os.close(cmd_read)
        os.close(resp_write)
        args = [chrome, "--remote-debugging-pipe", f"--user-data-dir={self.profile_dir}",
                "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
                "--disable-sync", "--password-store=basic", "about:blank"]
        if self.headless:
            args.insert(1, "--headless=new")
        devnull = os.open(os.devnull, os.O_RDWR)
        pid = None
        try:
            pid = os.posix_spawn(chrome, args, dict(os.environ), file_actions=[
                (os.POSIX_SPAWN_DUP2, devnull, 0), (os.POSIX_SPAWN_DUP2, devnull, 1),
                (os.POSIX_SPAWN_DUP2, devnull, 2),
                (os.POSIX_SPAWN_DUP2, child_in, 3), (os.POSIX_SPAWN_DUP2, child_out, 4),
            ], setsid=True)
        except Exception:
            for fd in (cmd_write, resp_read):
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        finally:
            for fd in (devnull, child_in, child_out):
                os.close(fd)
        conn = CdpPipe(cmd_write, resp_read, self._dispatch)
        try:
            conn.send("Target.setDiscoverTargets", {"discover": False}, timeout=20.0)
        except Exception:
            conn.close()
            self._reap(pid)
            raise
        self.pid = pid
        self._conn = conn
        LOG.info("agent browser started (pid %s, headless=%s)", self.pid, self.headless)
        return self._conn

    @staticmethod
    def _reap(pid: int | None) -> None:
        if not pid:
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    def _clear_orphan(self) -> None:
        """Stop a Chrome left running on this private profile by an Arena that crashed.

        Chrome hands a second launch on the same profile to the running one and
        exits, so the orphan would block every later start. The profile belongs
        to the agent alone, so a Chrome holding it is ours to stop.
        """
        lock = self.profile_dir / "SingletonLock"
        try:
            target = os.readlink(lock)
        except OSError:
            return
        pid_text = target.rsplit("-", 1)[-1]
        if not pid_text.isdigit():
            return
        pid = int(pid_text)
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            cmdline = b""
        if str(self.profile_dir).encode() in cmdline:
            LOG.warning("stopping an orphaned agent Chrome (pid %s) left on the profile", pid)
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(os.getpgid(pid), sig)
                except (ProcessLookupError, PermissionError):
                    break
                for _ in range(20):
                    if not Path(f"/proc/{pid}").exists():
                        break
                    time.sleep(0.1)
                else:
                    continue
                break
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                (self.profile_dir / name).unlink()
            except OSError:
                pass

    def _dispatch(self, session_id: str | None, method: str, params: dict) -> None:
        for tab in list(self.tabs.values()):
            if tab.session_id == session_id:
                tab.on_event(method, params)
                return

    def tab(self, key: str) -> Tab:
        with self._lock:
            existing = self.tabs.get(key)
            if existing is not None and existing.conn is self._conn and not self._conn._closed:
                return existing
            conn = self._ensure()
            target_id = conn.send("Target.createTarget", {"url": "about:blank"})["targetId"]
            session_id = conn.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
            tab = Tab(conn, target_id, session_id)
            self.tabs[key] = tab
            for domain in ("Page.enable", "DOM.enable", "Runtime.enable", "Accessibility.enable"):
                conn.send(domain, {}, session_id=session_id)
            return tab

    def close_tab(self, key: str) -> bool:
        with self._lock:
            tab = self.tabs.pop(key, None)
            if tab is None or self._conn is None:
                return False
            try:
                self._conn.send("Target.closeTarget", {"targetId": tab.target_id}, timeout=5.0)
            except BrowserError:
                pass
            return True

    def close_idle(self, idle_seconds: float = 600.0) -> None:
        cutoff = time.time() - idle_seconds
        for key, tab in list(self.tabs.items()):
            if tab.last_used >= cutoff:
                continue
            if not tab.lock.acquire(blocking=False):
                continue
            try:
                if tab.last_used < cutoff:
                    self.close_tab(key)
            finally:
                tab.lock.release()

    def shutdown(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
            self.tabs = {}
            pid, self.pid = self.pid, None
        if conn is not None:
            try:
                conn.send("Browser.close", {}, timeout=3.0)
            except BrowserError:
                pass
            conn.close()
        if pid:
            for _ in range(30):
                try:
                    done, _status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    break
                if done:
                    break
                time.sleep(0.1)
            else:
                try:
                    os.killpg(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass


_BROWSER: ChromeBrowser | None = None
_BROWSER_LOCK = threading.Lock()


def get_browser(headless: bool = True) -> ChromeBrowser:
    """The app-wide agent browser; restarted when the headless setting changes."""
    global _BROWSER
    with _BROWSER_LOCK:
        if _BROWSER is not None and _BROWSER.headless != headless:
            _BROWSER.shutdown()
            _BROWSER = None
        if _BROWSER is None:
            _BROWSER = ChromeBrowser(headless=headless)
        return _BROWSER


def shutdown() -> None:
    global _BROWSER
    with _BROWSER_LOCK:
        browser, _BROWSER = _BROWSER, None
    if browser is not None:
        browser.shutdown()


atexit.register(shutdown)
