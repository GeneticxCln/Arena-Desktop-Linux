"""Native Linux PTY terminal manager for Arena Desktop's built-in terminal emulator.

Uses Python's standard library ``pty``, ``termios``, ``fcntl``, and ``select``
— zero external Python or system library dependencies. Each terminal session
spawns the user's real login shell (``$SHELL``, fallback ``/bin/bash`` or
``/bin/sh``) inside a dedicated pseudo-terminal (``TERM=xterm-256color``) with
its own process group so resize (``TIOCSWINSZ``), Ctrl+C, full-screen TUIs
(``vim``, ``htop``, ``btop``), and clean shutdown work reliably.
"""
from __future__ import annotations

import base64
import collections
import errno
import fcntl
import logging
import os
import pty
import queue
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Iterator, List, Optional

LOG = logging.getLogger("arena.terminal")

MAX_SESSIONS = 12
REPLAY_LIMIT_BYTES = 128 * 1024  # 128 KiB ring buffer per terminal tab
READ_CHUNK_BYTES = 8192
MAX_WRITE_BYTES = 64 * 1024  # cap single POST input payload to avoid worker stall


def default_shell() -> str:
    """Return the user's preferred interactive Linux shell."""
    env_shell = (os.environ.get("SHELL") or "").strip()
    if env_shell and os.path.isfile(env_shell) and os.access(env_shell, os.X_OK):
        return env_shell
    for candidate in ("/bin/bash", "/usr/bin/bash", "/bin/zsh", "/usr/bin/zsh", "/bin/sh"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("bash") or shutil.which("sh") or "/bin/sh"


def _set_pty_winsize(fd: int, cols: int, rows: int) -> None:
    cols = max(20, min(400, int(cols or 80)))
    rows = max(5, min(200, int(rows or 24)))
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


def _trim_utf8_tail(data: bytes, limit: int) -> bytes:
    """Truncate ``data`` to at most ``limit`` bytes without splitting a UTF-8 codepoint."""
    if len(data) <= limit:
        return data
    cut = limit
    # Back up over any UTF-8 continuation bytes (10xxxxxx) at the boundary, plus the lead byte.
    while cut > 0 and (data[cut] & 0xC0) == 0x80:
        cut -= 1
    if cut > 0 and data[cut] >= 0xC0:
        # Check if the multi-byte sequence starting at `cut` fits inside `limit`
        lead = data[cut]
        if (lead & 0xE0) == 0xC0:
            seq_len = 2
        elif (lead & 0xF0) == 0xE0:
            seq_len = 3
        elif (lead & 0xF8) == 0xF0:
            seq_len = 4
        else:
            seq_len = 1
        if cut + seq_len <= limit:
            return data[: cut + seq_len]
    return data[:cut]


def _trim_leading_utf8_cont(data: bytes) -> bytes:
    """Strip orphaned leading UTF-8 continuation bytes (0x80..0xBF) from a ring-buffer replay."""
    i = 0
    n = min(len(data), 4)
    while i < n and (data[i] & 0xC0) == 0x80:
        i += 1
    return data[i:] if i else data


def _queue_force_put(q: "queue.Queue[Optional[dict]]", item: Optional[dict]) -> None:
    """Enqueue a terminal event even if the subscriber queue is full by evicting one old chunk."""
    try:
        q.put_nowait(item)
        return
    except queue.Full:
        pass
    try:
        q.get_nowait()
    except queue.Empty:
        pass
    try:
        q.put_nowait(item)
    except queue.Full:
        pass


class PtySession:
    """One interactive Linux PTY session backed by a real shell subprocess."""

    def __init__(
        self,
        session_id: str,
        *,
        cwd: str = "",
        cols: int = 100,
        rows: int = 24,
        shell: str = "",
        title: str = "",
    ) -> None:
        self.id = session_id
        self.shell = shell or default_shell()
        self.shell_name = os.path.basename(self.shell) or "sh"
        resolved_cwd = str(Path.home())
        if cwd:
            try:
                p = Path(cwd).expanduser().resolve()
                if p.is_dir():
                    resolved_cwd = str(p)
            except OSError:
                pass
        self.cwd = resolved_cwd
        self.folder_name = Path(self.cwd).name or self.cwd
        self.title = title or f"{self.shell_name} · {self.folder_name}"
        self.cols = max(20, min(400, int(cols or 100)))
        self.rows = max(5, min(200, int(rows or 24)))
        self.created_at = time.time()
        self.exit_code: Optional[int] = None
        self.closed = False

        self._lock = threading.Lock()
        self._fd_lock = threading.Lock()
        self._replay: collections.deque[bytes] = collections.deque()
        self._replay_bytes = 0
        self._subscribers: set["queue.Queue[Optional[dict]]"] = set()
        self._reader_done = threading.Event()
        self._active_writers = 0
        self._writers_done = threading.Condition(self._fd_lock)

        master_fd: Optional[int] = None
        slave_fd: Optional[int] = None
        try:
            master_fd, slave_fd = pty.openpty()
            _set_pty_winsize(slave_fd, self.cols, self.rows)

            # Put master_fd in non-blocking mode so write() never hangs a Flask thread.
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env["COLORTERM"] = "truecolor"
            env["ARENA_TERMINAL"] = "1"
            env.setdefault("LANG", "C.UTF-8")

            sfd = slave_fd

            def _preexec() -> None:
                os.setsid()
                try:
                    fcntl.ioctl(sfd, termios.TIOCSCTTY, 0)
                except OSError:
                    pass

            self.proc = subprocess.Popen(
                [self.shell],
                cwd=self.cwd,
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=_preexec,
            )
        except Exception:
            if master_fd is not None:
                m_close, master_fd = master_fd, None
                try:
                    os.close(m_close)
                except OSError:
                    pass
            raise
        finally:
            if slave_fd is not None:
                s_close, slave_fd = slave_fd, None
                try:
                    os.close(s_close)
                except OSError:
                    pass

        self.master_fd: Optional[int] = master_fd
        _set_pty_winsize(master_fd, self.cols, self.rows)
        self._reader = threading.Thread(
            target=self._read_loop,
            daemon=True,
            name=f"pty-{self.id[:8]}",
        )
        self._reader.start()

    @property
    def alive(self) -> bool:
        return not self.closed and self.proc.poll() is None

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "shell": self.shell_name,
            "cwd": self.cwd,
            "folder": self.folder_name,
            "cols": self.cols,
            "rows": self.rows,
            "alive": self.alive,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
        }

    def _record_and_broadcast(self, chunk: bytes) -> None:
        """Atomically append to replay buffer AND enqueue to all live subscribers."""
        event = {
            "type": "data",
            "b64": base64.b64encode(chunk).decode("ascii"),
        }
        with self._lock:
            self._replay.append(chunk)
            self._replay_bytes += len(chunk)
            while self._replay_bytes > REPLAY_LIMIT_BYTES and self._replay:
                dropped = self._replay.popleft()
                self._replay_bytes -= len(dropped)
            subs = list(self._subscribers)
            for q in subs:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass

    def _broadcast_exit(self, code: int) -> None:
        event = {"type": "exit", "code": code}
        with self._lock:
            self.exit_code = code
            subs = list(self._subscribers)
            for q in subs:
                _queue_force_put(q, event)

    def _reap_process(self) -> int:
        """Ensure the child shell is reaped (no zombies) and return its exit code."""
        rc = self.proc.poll()
        if rc is not None:
            return rc
        try:
            return self.proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        if self.closed:
            try:
                pgid = os.getpgid(self.proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            try:
                return self.proc.wait(timeout=1.0)
            except Exception:
                pass
        else:
            # Child closed its PTY slave early but is still finishing; reap cleanly.
            try:
                return self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    self.proc.terminate()
                    return self.proc.wait(timeout=1.0)
                except Exception:
                    try:
                        self.proc.kill()
                        return self.proc.wait(timeout=1.0)
                    except Exception:
                        pass
        rc = self.proc.poll()
        return rc if rc is not None else -1

    def _read_loop(self) -> None:
        try:
            while not self.closed:
                with self._fd_lock:
                    fd = self.master_fd
                if fd is None:
                    break
                try:
                    rlist, _, _ = select.select([fd], [], [], 0.15)
                except (OSError, ValueError):
                    break
                if not rlist:
                    if self.proc.poll() is not None:
                        break
                    continue
                with self._fd_lock:
                    cur_fd = self.master_fd
                    if cur_fd is None:
                        break
                    try:
                        chunk = os.read(cur_fd, READ_CHUNK_BYTES)
                    except BlockingIOError:
                        continue
                    except OSError:
                        break
                if not chunk:
                    break
                self._record_and_broadcast(chunk)

            rc = self._reap_process()
            self._broadcast_exit(rc)
        finally:
            self._close_fd()
            self._reader_done.set()

    def _close_fd(self) -> None:
        with self._fd_lock:
            # Wait briefly for any in-flight write() select to finish before closing FD.
            deadline = time.time() + 0.5
            while self._active_writers > 0:
                rem = deadline - time.time()
                if rem <= 0:
                    break
                self._writers_done.wait(timeout=rem)
            fd = self.master_fd
            self.master_fd = None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def write(self, data: bytes, timeout: float = 2.0) -> bool:
        """Write input bytes to the non-blocking PTY without hanging Flask workers."""
        if self.closed or not data:
            return False
        if len(data) > MAX_WRITE_BYTES:
            data = _trim_utf8_tail(data, MAX_WRITE_BYTES)
            if not data:
                return False
        view = memoryview(data)
        total = 0
        deadline = time.time() + timeout
        with self._fd_lock:
            if self.master_fd is None or self.closed:
                return False
            self._active_writers += 1
        try:
            while total < len(view) and not self.closed:
                with self._fd_lock:
                    fd = self.master_fd
                    if fd is None:
                        return total == len(view)
                    try:
                        written = os.write(fd, view[total:])
                        if written <= 0:
                            break
                        total += written
                        continue
                    except BlockingIOError:
                        pass
                    except OSError as exc:
                        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                            pass
                        else:
                            return total == len(view)
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    with self._fd_lock:
                        fd_wait = self.master_fd
                    if fd_wait is None:
                        break
                    _, wlist, _ = select.select([], [fd_wait], [], min(0.1, remaining))
                    if not wlist:
                        continue
                except (OSError, ValueError):
                    break
            return total == len(view)
        finally:
            with self._fd_lock:
                self._active_writers = max(0, self._active_writers - 1)
                if self._active_writers == 0:
                    self._writers_done.notify_all()

    def resize(self, cols: int, rows: int) -> bool:
        self.cols = max(20, min(400, int(cols or self.cols)))
        self.rows = max(5, min(200, int(rows or self.rows)))
        with self._fd_lock:
            fd = self.master_fd
            if fd is None or self.closed:
                return False
            _set_pty_winsize(fd, self.cols, self.rows)
        try:
            if self.proc.poll() is None:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGWINCH)
        except Exception:
            pass
        return True

    def subscribe(self) -> Iterator[dict]:
        """Yield replay buffer first, then live PTY events until exit or disconnect."""
        q: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=2048)
        with self._lock:
            replay_bytes = _trim_leading_utf8_cont(b"".join(self._replay))
            already_exited = self.exit_code is not None
            if not already_exited and not self.closed:
                self._subscribers.add(q)
        try:
            if replay_bytes:
                yield {
                    "type": "data",
                    "b64": base64.b64encode(replay_bytes).decode("ascii"),
                    "replay": True,
                }
            if already_exited or self.closed:
                yield {"type": "exit", "code": self.exit_code if self.exit_code is not None else 0}
                return
            while not self.closed:
                try:
                    ev = q.get(timeout=15.0)
                except queue.Empty:
                    if self.exit_code is not None:
                        yield {"type": "exit", "code": self.exit_code}
                        return
                    yield {"type": "ping"}
                    continue
                if ev is None:
                    return
                yield ev
                if ev.get("type") == "exit":
                    return
        finally:
            with self._lock:
                self._subscribers.discard(q)

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
        try:
            if self.proc.poll() is None:
                pgid = os.getpgid(self.proc.pid)
                os.killpg(pgid, signal.SIGHUP)
                try:
                    self.proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    os.killpg(pgid, signal.SIGTERM)
                    try:
                        self.proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        os.killpg(pgid, signal.SIGKILL)
                        try:
                            self.proc.wait(timeout=1.0)
                        except subprocess.TimeoutExpired:
                            pass
        except Exception:
            try:
                if self.proc.poll() is None:
                    self.proc.kill()
                    self.proc.wait(timeout=1.0)
            except Exception:
                pass
        # Let _read_loop exit and close master_fd safely so select() never races with os.close()
        self._reader_done.wait(timeout=2.0)
        self._close_fd()
        with self._lock:
            subs = list(self._subscribers)
            for q in subs:
                _queue_force_put(q, None)


class TerminalManager:
    """Thread-safe registry of interactive PTY sessions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, PtySession] = {}
        self._pending: int = 0

    def list(self) -> List[dict]:
        with self._lock:
            return [s.to_public() for s in self._sessions.values()]

    def get(self, session_id: str) -> Optional[PtySession]:
        with self._lock:
            return self._sessions.get(str(session_id or ""))

    def create(
        self,
        *,
        cwd: str = "",
        cols: int = 100,
        rows: int = 24,
        shell: str = "",
        title: str = "",
    ) -> PtySession:
        to_close: list[PtySession] = []
        with self._lock:
            if len(self._sessions) + self._pending >= MAX_SESSIONS:
                dead = [sid for sid, s in self._sessions.items() if not s.alive]
                for sid in dead:
                    sess_dead = self._sessions.pop(sid, None)
                    if sess_dead is not None:
                        to_close.append(sess_dead)
            if len(self._sessions) + self._pending >= MAX_SESSIONS:
                raise RuntimeError(f"Maximum of {MAX_SESSIONS} open terminals reached.")
            self._pending += 1
        for dead_s in to_close:
            try:
                dead_s.close()
            except Exception:
                pass
        sid = uuid.uuid4().hex[:16]
        try:
            sess = PtySession(sid, cwd=cwd, cols=cols, rows=rows, shell=shell, title=title)
        except Exception:
            with self._lock:
                self._pending = max(0, self._pending - 1)
            raise
        with self._lock:
            self._pending = max(0, self._pending - 1)
            self._sessions[sid] = sess
        return sess

    def close(self, session_id: str) -> bool:
        with self._lock:
            sess = self._sessions.pop(str(session_id or ""), None)
        if sess is None:
            return False
        sess.close()
        return True

    def shutdown_all(self) -> int:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for sess in sessions:
            try:
                sess.close()
            except Exception:
                LOG.debug("terminal %s shutdown error", sess.id)
        return len(sessions)
