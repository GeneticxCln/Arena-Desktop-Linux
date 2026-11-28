"""
Permission modes for local actions, modelled on Claude Code.

default            ask before any change or command (reads are free)
acceptEdits        create and edit files freely; ask before deletes and commands
auto               work freely inside the project folder; ask for risky commands
                   and for anything outside it
plan               read-only: the model may look but never change anything
bypassPermissions  allow everything except catastrophic commands
"""

import re
import shlex
import threading
import time
import uuid

MODES: dict[str, str] = {
    "default": "Ask before edits",
    "acceptEdits": "Accept edits",
    "auto": "Auto",
    "plan": "Plan mode",
    "bypassPermissions": "Bypass permissions",
}

KIND = {
    "read_file": "read",
    "ls": "read",
    "search": "read",
    "write_file": "edit",
    "edit_file": "edit",
    "mkdir": "edit",
    "write_project": "edit",
    "delete": "delete",
    "run_cmd": "shell",
    "use_skill": "read",
    "mcp_describe": "read",
    # Browser tools: looking is a read; acting on a page is its own kind.
    "browser_snapshot": "read",
    "browser_read": "read",
    "browser_wait": "read",
    "browser_screenshot": "read",
    "browser_open": "browser",
    "browser_click": "browser",
    "browser_fill": "browser",
    "browser_press": "browser",
    "browser_select": "browser",
    "browser_back": "browser",
    "browser_close": "browser",
    # Writes an MP4 into the project, like a file edit.
    "render_video": "edit",
}

SAFE_CMD = re.compile(
    r"^\s*(dir|ls|pwd|echo|type|cat|where|which|tree|git\s+(status|log|diff|show|branch)|"
    r"(python|py)(\s+-m)?\s+(pytest|unittest|--version)|node\s+(--version|-v)|"
    r"npm\s+(test|ls|-v|--version)|npm\s+run\s+(test|build|lint))\b",
    re.IGNORECASE,
)
# Block newlines, NULs, shell metacharacters, dangerous flags/paths on otherwise-read commands
SHELL_META = re.compile(
    r"[\r\n\x00;&|<>`$]|\b(del|erase|rm|rmdir|rd|format|curl|wget|iwr|invoke-webrequest|"
    r"powershell|pwsh|reg|shutdown|move|mv)\b",
    re.IGNORECASE,
)
_UNSAFE_SAFE_CMD_ARGS = re.compile(
    r"(--output\b|--ext-diff\b|--textconv\b|--no-index\b|"
    r"\bgit\s+branch\b.*\s(?:-[a-zA-Z]*[dDmMcCf]\b|--delete\b|--move\b|--copy\b|--force\b)|"
    r"(?:^|\s)(?:~|\.\./|/etc/|/proc/|/root/|/home/|/var/|/usr/|\.ssh\b))",
    re.IGNORECASE,
)
CATASTROPHIC = re.compile(
    r"(\brm\s+(?:-[a-zA-Z]+\s+)*(?:-[a-zA-Z]*r[a-zA-Z]*|--recursive)(?:\s+-[a-zA-Z]+)*\s+"
    r"(?:--no-preserve-root\s+)?(?:/\*?|~/?|\*|\$HOME\b|\$\{HOME\}|[a-z]:[\\/]?)(?:\s|$)|"
    r"\bformat\s+[a-z]:|"
    r"\b(rd|rmdir)\s+/s\s+(/q\s+)?[a-z]:[\\/]?\s*$|\bdel\s+(/[a-z]\s+)*[a-z]:[\\/]\*|\bmkfs\b|"
    r"\bshutdown\b|:\(\)\s*\{|\bdiskpart\b|\bbcdedit\b|\bvssadmin\s+delete\b|\bcipher\s+/w)",
    re.IGNORECASE,
)


def normalize(mode: str | None) -> str:
    return mode if mode in MODES else "default"


def _is_safe_auto_cmd(command: str) -> bool:
    cmd = command or ""
    if not SAFE_CMD.match(cmd):
        return False
    if SHELL_META.search(cmd):
        return False
    if _UNSAFE_SAFE_CMD_ARGS.search(cmd):
        return False
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        return False
    if not tokens:
        return False
    for tok in tokens[1:]:
        low = tok.lower()
        # Reject absolute paths, home-relative paths, parent traversal, and .ssh anywhere in args
        val = tok.split("=", 1)[1] if (tok.startswith("-") and "=" in tok) else tok
        if val.startswith(("/", "~")) or re.match(r"^[a-zA-Z]:[\\/]", val):
            return False
        parts = [p for p in re.split(r"[/\\]", val) if p]
        if ".." in parts or ".ssh" in [p.lower() for p in parts]:
            return False
        if low in ("--no-index", "--output", "--ext-diff", "--textconv"):
            return False
    return True


def decide(mode: str, tool: str, inside: bool = True, command: str = "") -> str:
    """Return 'allow', 'ask' or 'deny' for one action."""
    mode = normalize(mode)
    kind = KIND.get(tool, "shell")
    if kind == "shell" and CATASTROPHIC.search(command or ""):
        return "deny"
    if kind == "read":
        return "allow" if inside or mode == "bypassPermissions" else "ask"
    if mode == "plan":
        return "deny"
    if mode == "bypassPermissions":
        return "allow"
    if not inside:
        return "ask"
    if kind == "edit":
        return "allow" if mode in ("acceptEdits", "auto") else "ask"
    if kind == "browser":
        return "allow" if mode in ("acceptEdits", "auto") else "ask"
    if kind == "delete":
        return "allow" if mode == "auto" else "ask"
    if mode == "auto" and _is_safe_auto_cmd(command or ""):
        return "allow"
    return "ask"


class PermissionBroker:
    """Holds approval requests until the user answers them in the UI."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}
        # Scoped per chat_id: {chat_id: set(kinds)} so "Allow in this chat" never leaks globally or to gateway.
        self._always: dict[str, set] = {}

    def check(
        self,
        mode: str,
        tool: str,
        inside: bool = True,
        command: str = "",
        *,
        chat_id: str | None = None,
        gateway: bool = False,
    ) -> str:
        verdict = decide(mode, tool, inside, command)
        if verdict == "ask" and inside and not gateway:
            scope = str(chat_id or "")
            kind = KIND.get(tool, "shell")
            with self._lock:
                if kind in self._always.get(scope, ()):
                    return "allow"
        return verdict

    def ask(self, info: dict) -> str:
        pid = uuid.uuid4().hex[:12]
        with self._lock:
            self._pending[pid] = {
                "event": threading.Event(),
                "decision": None,
                "info": dict(info, id=pid, created=time.time()),
            }
        return pid

    def resolve(self, pid: str, decision: str) -> bool:
        if decision not in ("allow", "always", "deny"):
            return False
        with self._lock:
            item = self._pending.get(pid)
            if not item or item["decision"]:
                return False
            item["decision"] = decision
            if decision == "always" and not item["info"].get("gateway"):
                scope = str(item["info"].get("chat_id") or "")
                kind = KIND.get(item["info"].get("tool"), "shell")
                self._always.setdefault(scope, set()).add(kind)
            item["event"].set()
        return True

    def wait(self, pid: str, timeout: float | None = None) -> str | None:
        """'allow' or 'deny' once answered; None if still waiting after timeout."""
        item = self._pending.get(pid)
        if not item:
            return "deny"
        if not item["event"].wait(timeout):
            return None
        with self._lock:
            self._pending.pop(pid, None)
        return "deny" if item["decision"] == "deny" else "allow"

    def cancel(self, pid: str) -> bool:
        """Resolve and remove a pending request (e.g. after timeout or disconnect)."""
        with self._lock:
            item = self._pending.pop(pid, None)
            if not item:
                return False
            if not item["decision"]:
                item["decision"] = "deny"
            item["event"].set()
        return True

    def pending(self) -> list[dict]:
        with self._lock:
            return [p["info"] for p in self._pending.values() if not p["decision"]]

    def cancel_all(self) -> int:
        ids = [info["id"] for info in self.pending()]
        for pid in ids:
            self.cancel(pid)
        return len(ids)

    def reset_always(self, chat_id: str | None = None):
        with self._lock:
            if chat_id is None:
                self._always.clear()
            else:
                self._always.pop(str(chat_id), None)
