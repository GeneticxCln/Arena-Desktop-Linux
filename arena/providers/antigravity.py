"""Google Antigravity local runtime provider (auto-detects SDK and agy CLI)."""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import queue
import re
import select
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterator, Optional

from arena.providers.base import Provider

LOG = logging.getLogger("arena.providers.antigravity")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _find_agy_cli() -> Optional[str]:
    found = shutil.which("agy")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "agy",
        Path.home() / ".gemini" / "antigravity" / "bin" / "agy",
        Path("/usr/local/bin/agy"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _has_antigravity_sdk() -> bool:
    try:
        return importlib.util.find_spec("google.antigravity") is not None
    except Exception:
        return False


def _parse_agy_models_output(raw: str) -> list[dict]:
    """Parse `agy models` table output (`<id>  <Display Name>`)."""
    models: list[dict] = []
    seen: set[str] = set()
    cleaned = _ANSI_RE.sub("", raw or "")
    for raw_line in cleaned.splitlines():
        line = raw_line.split("\r")[-1].strip()
        if not line or line.lower().startswith("fetching") or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._:/-]{2,})\s{2,}(.+)$", line)
        if not m:
            continue
        mid, label = m.group(1).strip(), m.group(2).strip()
        if mid in seen:
            continue
        seen.add(mid)
        low = f"{mid} {label}".lower()
        org = "Google"
        if "claude" in low:
            org = "Anthropic"
        elif "gpt" in low:
            org = "OpenAI"
        caps = ["Agent", "Code"]
        if "flash" in low or "(low)" in low:
            caps.append("Fast")
        elif any(k in low for k in ("xhigh", "(high)", "opus", "orchestrator")):
            caps.append("Reasoning")
        models.append({
            "id": mid,
            "name": label,
            "org": org,
            "description": f"Local agy model ({mid})",
            "capabilities": caps,
        })
    return models


def _kill_proc_group(proc: subprocess.Popen) -> None:
    """Terminate a process group cleanly and always reap to avoid zombies."""
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=0.8)
            return
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=1.0)
        except Exception:
            pass


def _format_history_prefix(history: list) -> str:
    """Compact recent turns so follow-up messages keep conversation context."""
    if not history:
        return ""
    recent = history[-8:]
    lines = ["[Recent conversation context]"]
    total = 0
    for msg in recent:
        if not isinstance(msg, dict):
            continue
        role = "User" if msg.get("role") == "user" else "Assistant"
        txt = str(msg.get("text") or msg.get("content") or "").strip()
        if not txt:
            continue
        snippet = txt[:800]
        total += len(snippet)
        if total > 4000:
            break
        lines.append(f"{role}: {snippet}")
    if len(lines) <= 1:
        return ""
    lines.append("[Current message]\n")
    return "\n".join(lines)


class AntigravityProvider(Provider):
    """Runs local Google Antigravity agent turns inside the active workspace."""

    id = "antigravity"
    name = "Antigravity"
    short_name = "Antigravity"
    badge = "\u25b2"
    badge_class = "is-antigravity"
    description = "Local Google Antigravity agent (auto-detects google-antigravity SDK or agy CLI)"
    origin = "https://antigravity.google"
    kind = "local_agent"

    def __init__(self) -> None:
        self._procs: set[subprocess.Popen] = set()
        self._proc_lock = threading.Lock()
        self._models_cache: Optional[dict] = None
        self._models_at: float = 0.0

    def _backend(self) -> tuple[str, str]:
        if _has_antigravity_sdk():
            return "sdk", "google-antigravity SDK"
        cli = _find_agy_cli()
        if cli:
            return "cli", cli
        return "", ""

    def status(self) -> dict:
        kind, path = self._backend()
        if kind == "sdk":
            return {
                "available": True,
                "signed_in": True,
                "open": False,
                "window": False,
                "backend": "sdk",
                "detail": "Ready via google-antigravity Python SDK",
            }
        if kind == "cli":
            return {
                "available": True,
                "signed_in": True,
                "open": False,
                "window": False,
                "backend": "cli",
                "detail": f"Ready via local agy CLI ({path})",
            }
        return {
            "available": False,
            "signed_in": False,
            "open": False,
            "window": False,
            "backend": "",
            "detail": "Install `agy` CLI or `pip install google-antigravity` to enable Antigravity tabs.",
        }

    def list_models(self, *, force: bool = False) -> dict:
        now = time.time()
        if not force and self._models_cache and (now - self._models_at) < 300.0:
            return dict(self._models_cache)
        cli = _find_agy_cli()
        models: list[dict] = []
        live = False
        if cli:
            try:
                proc = subprocess.run(
                    [cli, "models"],
                    capture_output=True,
                    text=True,
                    timeout=12,
                )
                combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
                parsed = _parse_agy_models_output(combined)
                if parsed:
                    models = parsed
                    live = True
            except Exception as exc:
                LOG.debug("agy models failed: %s", exc)
        if not models:
            models = [
                {
                    "id": "",
                    "name": "Default Antigravity model",
                    "org": "Google",
                    "description": "Use the default model configured in your local Antigravity runtime",
                    "capabilities": ["Agent", "Code"],
                }
            ]
        out = {
            "ok": True,
            "provider": self.id,
            "live": live,
            "active_model": models[0]["id"] if models else "",
            "models": models,
        }
        self._models_cache = out
        self._models_at = now
        return dict(out)

    def open_sign_in(self) -> dict:
        st = self.status()
        if st.get("available"):
            return {"ok": True, "detail": st["detail"]}
        return {
            "ok": False,
            "error": (
                "Antigravity runtime not found on this machine. "
                "Install the Antigravity CLI (`agy`) or run `pip install google-antigravity`."
            ),
        }

    def _stream_via_sdk(
        self, prompt: str, workspace: str, model: str = "", stop_event=None
    ) -> Iterator[dict]:
        from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig  # type: ignore

        out_q: "queue.Queue[Optional[dict]]" = queue.Queue()
        loop_box: dict = {}

        async def _run():
            loop_box["loop"] = asyncio.get_running_loop()
            loop_box["task"] = asyncio.current_task()
            try:
                kwargs = {"capabilities": CapabilitiesConfig()}
                if workspace:
                    kwargs["working_directory"] = workspace
                if model:
                    kwargs["model"] = model
                try:
                    config = LocalAgentConfig(**kwargs)
                except TypeError:
                    kwargs.pop("model", None)
                    try:
                        config = LocalAgentConfig(**kwargs)
                    except TypeError:
                        config = LocalAgentConfig(capabilities=CapabilitiesConfig())
                async with Agent(config) as ag:
                    response = await ag.chat(prompt)
                    async for token in response:
                        if stop_event is not None and stop_event.is_set():
                            break
                        if token:
                            out_q.put({"type": "text", "text": str(token)})
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                out_q.put({
                    "type": "error",
                    "code": "antigravity_sdk",
                    "message": f"Antigravity SDK error: {exc}",
                })
            finally:
                out_q.put(None)

        thread = threading.Thread(
            target=lambda: asyncio.run(_run()),
            daemon=True,
            name="antigravity-sdk",
        )
        thread.start()
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    lp = loop_box.get("loop")
                    tk = loop_box.get("task")
                    if lp is not None and tk is not None:
                        try:
                            lp.call_soon_threadsafe(tk.cancel)
                        except Exception:
                            pass
                    return
                try:
                    item = out_q.get(timeout=0.20)
                except queue.Empty:
                    continue
                if item is None:
                    return
                yield item
        finally:
            # Always cancel any still-running SDK task on stop OR SSE client disconnect
            lp = loop_box.get("loop")
            tk = loop_box.get("task")
            if lp is not None and tk is not None:
                try:
                    lp.call_soon_threadsafe(tk.cancel)
                except Exception:
                    pass

    def _stream_via_cli(
        self,
        cli_path: str,
        prompt: str,
        workspace: str,
        model: str = "",
        permission_mode: str = "",
        stop_event=None,
    ) -> Iterator[dict]:
        cwd = workspace if (workspace and Path(workspace).is_dir()) else None
        cmd = [cli_path]
        if model:
            cmd.extend(["--model", model])
        pm = (permission_mode or "").strip()
        if pm == "plan":
            cmd.extend(["--mode", "plan"])
        elif pm == "acceptEdits":
            cmd.extend(["--mode", "accept-edits"])
        elif pm == "auto":
            cmd.extend(["--mode", "auto"])
        elif pm == "bypassPermissions":
            cmd.append("--dangerously-skip-permissions")
        # Guard against Linux MAX_ARG_STRLEN (128 KiB per argv entry) and leading '-' flag parsing
        safe_prompt = prompt
        if safe_prompt.startswith("-"):
            safe_prompt = " " + safe_prompt
        if len(safe_prompt.encode("utf-8")) > 110_000:
            safe_prompt = safe_prompt.encode("utf-8")[-105_000:].decode("utf-8", errors="ignore")
        cmd.extend(["-p", safe_prompt])
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            yield {
                "type": "error",
                "code": "antigravity_cli",
                "message": f"Could not start agy CLI: {exc}",
            }
            return
        with self._proc_lock:
            self._procs.add(proc)

        # Drain stderr concurrently so >64 KiB stderr never deadlocks the subprocess!
        stderr_chunks: list[bytes] = []

        def _drain_stderr() -> None:
            if proc.stderr is None:
                return
            try:
                while True:
                    chunk = proc.stderr.read(4096)
                    if not chunk:
                        break
                    if sum(len(c) for c in stderr_chunks) < 64 * 1024:
                        stderr_chunks.append(chunk)
            except Exception:
                pass

        err_thread = threading.Thread(target=_drain_stderr, daemon=True, name="agy-stderr")
        err_thread.start()

        try:
            import codecs
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            emitted = False
            assert proc.stdout is not None
            stdout_fd = proc.stdout.fileno()
            while True:
                if stop_event is not None and stop_event.is_set():
                    _kill_proc_group(proc)
                    return
                try:
                    rlist, _, _ = select.select([stdout_fd], [], [], 0.15)
                except (OSError, ValueError):
                    break
                if not rlist:
                    if proc.poll() is not None:
                        break
                    continue
                try:
                    raw = os.read(stdout_fd, 4096)
                except OSError:
                    break
                if not raw:
                    break
                text = decoder.decode(raw, final=False)
                if text:
                    emitted = True
                    yield {"type": "text", "text": text}
            tail = decoder.decode(b"", final=True)
            if tail:
                emitted = True
                yield {"type": "text", "text": tail}
            rc = proc.wait(timeout=10)
            err_thread.join(timeout=1.0)
            if rc != 0 and not emitted:
                err_text = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
                yield {
                    "type": "error",
                    "code": "antigravity_cli",
                    "message": err_text or f"agy exited with status {rc}.",
                }
        except Exception as exc:
            yield {
                "type": "error",
                "code": "antigravity_cli",
                "message": f"Antigravity CLI failed: {exc}",
            }
        finally:
            with self._proc_lock:
                self._procs.discard(proc)
            _kill_proc_group(proc)
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass

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
        kind, detail = self._backend()
        if not kind:
            yield {
                "type": "error",
                "code": "not_installed",
                "message": (
                    "Antigravity is not installed locally yet. Install the `agy` CLI "
                    "or `pip install google-antigravity`, then try again."
                ),
            }
            return
        hist_prefix = _format_history_prefix(history)
        full_prompt = f"{memory_block}{hist_prefix}{message}" if (memory_block or hist_prefix) else message
        stage_label = f"Antigravity ({model}) is thinking..." if model else "Antigravity is thinking..."
        yield {"type": "agent", "stage": stage_label}
        if kind == "sdk":
            yield from self._stream_via_sdk(full_prompt, workspace, model=model, stop_event=stop_event)
        else:
            yield from self._stream_via_cli(
                detail,
                full_prompt,
                workspace,
                model=model,
                permission_mode=permission_mode,
                stop_event=stop_event,
            )

    def shutdown(self) -> None:
        with self._proc_lock:
            procs = list(self._procs)
            self._procs.clear()
        for proc in procs:
            _kill_proc_group(proc)
