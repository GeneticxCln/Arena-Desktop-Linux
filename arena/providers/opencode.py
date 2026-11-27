"""OpenCode local CLI provider (auto-detects local `opencode` CLI with live models & session resume)."""
from __future__ import annotations

import codecs
import json
import logging
import os
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

LOG = logging.getLogger("arena.providers.opencode")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SESSION_PREFIX = "opencode://session/"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,128}$")


def _find_opencode_cli() -> Optional[str]:
    found = shutil.which("opencode")
    if found:
        return found
    for candidate in (
        Path.home() / ".opencode" / "bin" / "opencode",
        Path.home() / ".local" / "bin" / "opencode",
        Path("/usr/local/bin/opencode"),
        Path("/usr/bin/opencode"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _extract_session_id(conversation_url: str) -> str:
    raw = str(conversation_url or "").strip()
    if raw.startswith(_SESSION_PREFIX):
        sid = raw[len(_SESSION_PREFIX):].strip()
        if _SESSION_ID_RE.match(sid):
            return sid
    return ""


def _infer_model_org(provider_prefix: str, model_slug: str) -> str:
    low = f"{provider_prefix} {model_slug}".lower()
    if "claude" in low or "anthropic" in low:
        return "Anthropic"
    if re.search(r"\b(gpt|openai|o[134])\b", low):
        return "OpenAI"
    if "gemini" in low or "gemma" in low or "google" in low:
        return "Google"
    if "deepseek" in low:
        return "DeepSeek"
    if "qwen" in low or "qwq" in low or "alibaba" in low:
        return "Alibaba"
    if "kimi" in low or "moonshot" in low:
        return "Kimi"
    if "glm" in low or "zhipu" in low or "zai" in low:
        return "Z.AI"
    if "grok" in low or "xai" in low:
        return "xAI"
    if "minimax" in low:
        return "MiniMax"
    if "llama" in low or "meta" in low:
        return "Meta"
    if "mistral" in low or "codestral" in low:
        return "Mistral"
    if "nemotron" in low or "nvidia" in low:
        return "Nvidia"
    p = (provider_prefix or "").strip().lower()
    if p == "ollama":
        return "Ollama"
    if p == "opencode":
        return "OpenCode"
    return provider_prefix.capitalize() if provider_prefix else "OpenCode"


def _load_opencode_config_models() -> dict[str, dict]:
    """Read custom provider model metadata from ~/.config/opencode/{opencode,config}.json."""
    out: dict[str, dict] = {}
    for cfg_path in (
        Path.home() / ".config" / "opencode" / "opencode.json",
        Path.home() / ".config" / "opencode" / "config.json",
    ):
        if not cfg_path.is_file():
            continue
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        provs = data.get("providers") or data.get("provider")
        if not isinstance(provs, dict):
            continue
        for prov_key, prov_cfg in provs.items():
            if not isinstance(prov_cfg, dict):
                continue
            p_name = str(prov_key or prov_cfg.get("name") or "").strip()
            mod_map = prov_cfg.get("models")
            if not p_name or not isinstance(mod_map, dict):
                continue
            for m_key, m_cfg in mod_map.items():
                if not isinstance(m_cfg, dict):
                    continue
                slug = str(m_cfg.get("modelID") or m_cfg.get("id") or m_key or "").strip()
                if not slug:
                    continue
                mid = f"{p_name}/{slug}"
                out[mid] = {
                    "name": str(m_cfg.get("name") or slug).strip(),
                    "capabilities": m_cfg.get("capabilities") if isinstance(m_cfg.get("capabilities"), dict) else {},
                    "limit": m_cfg.get("limit") if isinstance(m_cfg.get("limit"), dict) else {},
                }
    return out


def _parse_opencode_models_output(raw: str, config_meta: Optional[dict[str, dict]] = None) -> list[dict]:
    """Parse `opencode models` output (`provider/model-id`, one per line) + config metadata."""
    models: list[dict] = []
    seen: set[str] = set()
    cfg_map = config_meta if isinstance(config_meta, dict) else {}
    cleaned = _ANSI_RE.sub("", raw or "")

    def _add_model(prov_prefix: str, slug: str) -> None:
        mid = f"{prov_prefix}/{slug}"
        if mid in seen:
            return
        seen.add(mid)
        low = mid.lower()
        org = _infer_model_org(prov_prefix, slug)
        meta = cfg_map.get(mid) or {}
        caps = ["Agent", "Code"]
        if prov_prefix.lower() == "ollama":
            caps.append("Local")
        if any(k in low for k in ("flash", "mini", "fast", "lightning", "haiku")):
            caps.append("Fast")
        elif any(k in low for k in ("opus", "pro", "reason", "think", "r1", "max")):
            caps.append("Reasoning")
        meta_caps = meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
        in_mods = [str(x).lower() for x in (meta_caps.get("input") or [])] if isinstance(meta_caps.get("input"), list) else []
        if "image" in in_mods or any(k in low for k in ("-vl", "vision", "omni", "image")):
            if "Vision" not in caps:
                caps.append("Vision")
        short_slug = slug.split("/")[-1] if "/" in slug else slug
        cfg_name = str(meta.get("name") or "").strip()
        display_base = cfg_name if (cfg_name and cfg_name != slug) else short_slug
        label = f"{display_base} ({prov_prefix})"
        limit = meta.get("limit") if isinstance(meta.get("limit"), dict) else {}
        ctx = limit.get("context")
        ctx_str = f" · {int(ctx) // 1000}k ctx" if isinstance(ctx, (int, float)) and ctx >= 1000 else ""
        models.append({
            "id": mid,
            "name": label,
            "org": org,
            "group": prov_prefix,
            "description": f"Local opencode model ({mid}){ctx_str}",
            "capabilities": caps,
        })

    for raw_line in cleaned.splitlines():
        line = raw_line.split("\r")[-1].strip()
        if not line or "/" not in line or " " in line:
            continue
        if line.startswith(("-", "#", "[", "{", "http://", "https://")):
            continue
        prov_prefix, slug = line.split("/", 1)
        prov_prefix = prov_prefix.strip()
        slug = slug.strip()
        if not prov_prefix or not slug:
            continue
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", prov_prefix):
            continue
        _add_model(prov_prefix, slug)

    # Also include any custom models declared in ~/.config/opencode/opencode.json not yet listed
    for mid in cfg_map:
        if "/" in mid and mid not in seen:
            p, s = mid.split("/", 1)
            _add_model(p.strip(), s.strip())

    return models


def _kill_proc_group(proc: subprocess.Popen) -> None:
    """Terminate a process group cleanly and reap to avoid zombies."""
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
    """Compact recent turns when starting a fresh OpenCode session from an existing chat."""
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


def _parse_opencode_json_line(raw_line: str) -> tuple[str, list[dict]]:
    """Parse one NDJSON line from `opencode run --format json`.

    Returns ``(session_id, events)``.
    """
    s = _ANSI_RE.sub("", raw_line or "").strip()
    if not s or not s.startswith("{"):
        return "", []
    try:
        pkt = json.loads(s)
    except Exception:
        return "", []
    if not isinstance(pkt, dict):
        return "", []

    part = pkt.get("part") if isinstance(pkt.get("part"), dict) else {}
    sid = str(pkt.get("sessionID") or part.get("sessionID") or "").strip()
    if sid and not _SESSION_ID_RE.match(sid):
        sid = ""

    etype = str(pkt.get("type") or part.get("type") or "").strip().lower()
    events: list[dict] = []

    if etype in ("text", "text_delta", "text-delta"):
        txt = part.get("text") if isinstance(part.get("text"), str) else pkt.get("text") or pkt.get("delta")
        if isinstance(txt, str) and txt:
            events.append({"type": "text", "text": txt})
    elif etype in ("reasoning", "thinking", "reasoning_delta", "reasoning-delta"):
        txt = (
            part.get("text")
            if isinstance(part.get("text"), str)
            else part.get("reasoning") or pkt.get("text") or pkt.get("delta")
        )
        if isinstance(txt, str) and txt:
            events.append({"type": "reasoning", "text": txt})
    elif etype in ("tool_use", "tool", "tool-call", "tool_call") or str(part.get("type") or "").lower() == "tool":
        tool_name = str(part.get("tool") or part.get("name") or pkt.get("tool") or "tool").strip()
        state_obj = part.get("state") if isinstance(part.get("state"), dict) else {}
        title = str(state_obj.get("title") or part.get("title") or "").strip()
        inp = state_obj.get("input") if isinstance(state_obj.get("input"), dict) else {}
        detail = title or str(inp.get("command") or inp.get("filePath") or inp.get("path") or inp.get("pattern") or "").strip()
        summary = f"{tool_name}: {detail[:120]}" if detail else tool_name
        events.append({"type": "agent", "stage": f"OpenCode · {summary}"})
        events.append({"type": "tool", "tool": tool_name, "summary": summary})
    elif etype == "error":
        err_obj = pkt.get("error")
        if isinstance(err_obj, dict):
            data_obj = err_obj.get("data") if isinstance(err_obj.get("data"), dict) else {}
            msg = str(data_obj.get("message") or err_obj.get("message") or err_obj.get("name") or "").strip()
        else:
            msg = str(pkt.get("message") or err_obj or "").strip()
        events.append({
            "type": "error",
            "code": "opencode_cli",
            "message": msg or "OpenCode returned an error.",
        })

    return sid, events


class OpenCodeProvider(Provider):
    """Runs local OpenCode CLI (`opencode run --format json`) inside the active workspace."""

    id = "opencode"
    name = "OpenCode"
    short_name = "OpenCode"
    badge = "O"
    badge_class = "is-opencode"
    description = "Local OpenCode CLI runtime (supports all configured OpenCode & Ollama models)"
    origin = "https://opencode.ai"
    kind = "local_agent"

    def __init__(self) -> None:
        self._procs: set[subprocess.Popen] = set()
        self._proc_lock = threading.Lock()
        self._models_cache: Optional[dict] = None
        self._models_at: float = 0.0
        self._version_cache: tuple[str, str] = ("", "")

    def _read_version(self, cli_path: str) -> str:
        if self._version_cache[0] == cli_path and self._version_cache[1]:
            return self._version_cache[1]
        try:
            proc = subprocess.run(
                [cli_path, "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=4,
            )
            ver = (proc.stdout or proc.stderr or "").strip().splitlines()
            clean = _ANSI_RE.sub("", ver[0]).strip() if ver else ""
            if clean:
                self._version_cache = (cli_path, clean)
                return clean
        except Exception:
            pass
        return ""

    def status(self) -> dict:
        cli = _find_opencode_cli()
        if cli:
            ver = self._read_version(cli)
            ver_part = f"{ver} · " if ver else ""
            return {
                "available": True,
                "signed_in": True,
                "open": False,
                "window": False,
                "backend": "cli",
                "detail": f"Ready via local OpenCode CLI ({ver_part}{cli})",
            }
        return {
            "available": False,
            "signed_in": False,
            "open": False,
            "window": False,
            "backend": "",
            "detail": "Install the `opencode` CLI (https://opencode.ai) to enable OpenCode tabs.",
        }

    def list_models(self, *, force: bool = False) -> dict:
        now = time.time()
        if not force and self._models_cache and self._models_cache.get("live") and (now - self._models_at) < 300.0:
            return dict(self._models_cache)
        cli = _find_opencode_cli()
        cfg_meta = _load_opencode_config_models()
        models: list[dict] = []
        live = False
        if cli:
            try:
                proc = subprocess.run(
                    [cli, "models"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=12,
                )
                combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
                parsed = _parse_opencode_models_output(combined, config_meta=cfg_meta)
                if parsed:
                    models = parsed
                    live = True
            except Exception as exc:
                LOG.debug("opencode models failed: %s", exc)
        if not models and cfg_meta:
            models = _parse_opencode_models_output("", config_meta=cfg_meta)
            live = bool(models)
        if not models:
            models = [
                {
                    "id": "",
                    "name": "Default OpenCode model",
                    "org": "OpenCode",
                    "description": "Use the default model configured in your local OpenCode CLI",
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
        if live:
            self._models_cache = out
            self._models_at = now
        return dict(out)

    def open_sign_in(self) -> dict:
        st = self.status()
        if st.get("available"):
            return {"ok": True, "detail": st["detail"]}
        return {
            "ok": False,
            "error": "OpenCode CLI not found on this machine. Install `opencode` and run `opencode auth login`.",
        }

    def _run_cli_once(
        self,
        cli_path: str,
        prompt: str,
        workspace: str,
        *,
        model: str = "",
        session_id: str = "",
        permission_mode: str = "",
        stop_event=None,
    ) -> Iterator[dict]:
        cwd = workspace if (workspace and Path(workspace).is_dir()) else None
        cmd = [cli_path, "run", "--format", "json", "--thinking"]
        if session_id:
            cmd.extend(["--session", session_id])
        else:
            cmd.append("--archived")
        if model and model.lower() not in ("default", "auto"):
            cmd.extend(["--model", model])
        pm = (permission_mode or "").strip()
        if pm in ("auto", "acceptEdits", "bypassPermissions"):
            cmd.append("--auto")

        # Guard against Linux MAX_ARG_STRLEN (128 KiB per argv entry)
        safe_prompt = prompt
        if len(safe_prompt.encode("utf-8")) > 110_000:
            safe_prompt = safe_prompt.encode("utf-8")[-105_000:].decode("utf-8", errors="ignore")
        cmd.extend(["--", safe_prompt])

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            yield {
                "type": "error",
                "code": "opencode_cli",
                "message": f"Could not start opencode CLI: {exc}",
            }
            return

        with self._proc_lock:
            self._procs.add(proc)

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

        err_thread = threading.Thread(target=_drain_stderr, daemon=True, name="opencode-stderr")
        err_thread.start()

        try:
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            assert proc.stdout is not None
            stdout_fd = proc.stdout.fileno()
            started = time.time()
            last_output = started
            idle_limit = 180.0
            total_limit = 900.0
            timed_out_msg = ""
            line_buf = ""
            seen_sid = session_id
            emitted_text = False
            emitted_error = False
            saw_json = False
            need_step_sep = False
            last_text_ended_nl = True
            plain_fallback: list[str] = []

            def _process_line(line: str) -> Iterator[dict]:
                nonlocal seen_sid, emitted_text, emitted_error, saw_json, need_step_sep, last_text_ended_nl
                stripped = _ANSI_RE.sub("", line or "").strip()
                if stripped.startswith("{"):
                    try:
                        raw_pkt = json.loads(stripped)
                        if isinstance(raw_pkt, dict) and str(raw_pkt.get("type") or "").lower() == "step_start":
                            if emitted_text:
                                need_step_sep = True
                    except Exception:
                        pass
                sid, evs = _parse_opencode_json_line(line)
                if sid or evs or stripped.startswith("{"):
                    saw_json = True
                elif stripped:
                    plain_fallback.append(_ANSI_RE.sub("", line))
                if sid and sid != seen_sid:
                    seen_sid = sid
                    yield {"type": "meta_update", "external_url": f"{_SESSION_PREFIX}{sid}"}
                for ev in evs:
                    if ev.get("type") == "text":
                        txt = str(ev.get("text") or "")
                        if need_step_sep and not last_text_ended_nl and txt and not txt.startswith("\n"):
                            yield {"type": "text", "text": "\n\n"}
                        need_step_sep = False
                        emitted_text = True
                        if txt:
                            last_text_ended_nl = txt.endswith("\n")
                    elif ev.get("type") == "error":
                        emitted_error = True
                    yield ev

            while True:
                if stop_event is not None and stop_event.is_set():
                    _kill_proc_group(proc)
                    return
                now = time.time()
                if (now - last_output) > idle_limit:
                    timed_out_msg = f"opencode CLI produced no output for {int(idle_limit)}s and was stopped."
                    _kill_proc_group(proc)
                    break
                if (now - started) > total_limit:
                    timed_out_msg = f"opencode CLI exceeded the {int(total_limit)}s limit and was stopped."
                    _kill_proc_group(proc)
                    break
                try:
                    rlist, _, _ = select.select([stdout_fd], [], [], 0.15)
                except (OSError, ValueError):
                    break
                if not rlist:
                    if proc.poll() is not None:
                        # Drain any bytes written right before exit
                        while True:
                            try:
                                r2, _, _ = select.select([stdout_fd], [], [], 0)
                            except (OSError, ValueError):
                                break
                            if not r2:
                                break
                            try:
                                extra = os.read(stdout_fd, 4096)
                            except OSError:
                                break
                            if not extra:
                                break
                            line_buf += decoder.decode(extra, final=False)
                            while "\n" in line_buf:
                                one_line, line_buf = line_buf.split("\n", 1)
                                yield from _process_line(one_line)
                        break
                    continue
                try:
                    raw = os.read(stdout_fd, 4096)
                except OSError:
                    break
                if not raw:
                    break
                last_output = time.time()
                line_buf += decoder.decode(raw, final=False)
                while "\n" in line_buf:
                    one_line, line_buf = line_buf.split("\n", 1)
                    yield from _process_line(one_line)

            tail = decoder.decode(b"", final=True)
            if tail:
                line_buf += tail
            if line_buf.strip():
                yield from _process_line(line_buf)

            rc = proc.wait(timeout=10)
            err_thread.join(timeout=1.0)

            if not saw_json and plain_fallback and rc == 0 and not timed_out_msg:
                yield {"type": "text", "text": "\n".join(plain_fallback)}
                emitted_text = True

            if timed_out_msg:
                yield {
                    "type": "error",
                    "code": "opencode_timeout",
                    "message": timed_out_msg,
                }
            elif rc != 0 and not emitted_error:
                err_text = _ANSI_RE.sub("", b"".join(stderr_chunks).decode("utf-8", errors="replace")).strip()
                yield {
                    "type": "error",
                    "code": "opencode_cli",
                    "message": err_text or f"opencode exited with status {rc}.",
                }
            elif rc == 0 and not emitted_text and not emitted_error:
                err_text = _ANSI_RE.sub("", b"".join(stderr_chunks).decode("utf-8", errors="replace")).strip()
                if err_text:
                    yield {"type": "error", "code": "opencode_cli", "message": err_text}
        except Exception as exc:
            yield {
                "type": "error",
                "code": "opencode_cli",
                "message": f"OpenCode CLI failed: {exc}",
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
        cli = _find_opencode_cli()
        if not cli:
            yield {
                "type": "error",
                "code": "not_installed",
                "message": "OpenCode CLI is not installed locally yet. Install `opencode` (https://opencode.ai) and try again.",
            }
            return

        session_id = _extract_session_id(conversation_url)
        hist_prefix = "" if session_id else _format_history_prefix(history)
        plan_note = (
            "[Plan mode: do not modify files or execute destructive commands; propose a clear plan only.]\n"
            if (permission_mode or "").strip() == "plan"
            else ""
        )
        full_prompt = f"{plan_note}{memory_block}{hist_prefix}{message}" if (plan_note or memory_block or hist_prefix) else message
        stage_label = f"OpenCode ({model}) is thinking..." if model else "OpenCode is thinking..."
        yield {"type": "agent", "stage": stage_label}

        # If resuming a stale/deleted OpenCode session fails before emitting text, fall back cleanly to a fresh session.
        if session_id:
            got_output = False
            retry_fresh = False
            pending_err: Optional[dict] = None
            for ev in self._run_cli_once(
                cli,
                full_prompt,
                workspace,
                model=model,
                session_id=session_id,
                permission_mode=permission_mode,
                stop_event=stop_event,
            ):
                if ev.get("type") in ("text", "reasoning", "tool"):
                    got_output = True
                    yield ev
                elif ev.get("type") == "error" and not got_output and "session" in str(ev.get("message") or "").lower():
                    retry_fresh = True
                    pending_err = ev
                else:
                    yield ev
            if not retry_fresh:
                return
            if stop_event is not None and stop_event.is_set():
                return
            LOG.info("opencode session %s unavailable (%s); starting a fresh session", session_id, pending_err)
            hist_retry = _format_history_prefix(history)
            fresh_prompt = f"{plan_note}{memory_block}{hist_retry}{message}" if (plan_note or memory_block or hist_retry) else message
            yield from self._run_cli_once(
                cli,
                fresh_prompt,
                workspace,
                model=model,
                session_id="",
                permission_mode=permission_mode,
                stop_event=stop_event,
            )
            return

        yield from self._run_cli_once(
            cli,
            full_prompt,
            workspace,
            model=model,
            session_id="",
            permission_mode=permission_mode,
            stop_event=stop_event,
        )

    def shutdown(self) -> None:
        with self._proc_lock:
            procs = list(self._procs)
            self._procs.clear()
        for proc in procs:
            _kill_proc_group(proc)
