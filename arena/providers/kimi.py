"""Kimi web provider — European/International kimi.ai (pure WebKitGTK, no CLI / no API key)."""
from __future__ import annotations

import json
import re
import time
from typing import Optional
from urllib.parse import urlparse

from arena.providers.web_engine import WebChatEngine


class KimiProvider(WebChatEngine):
    id = "kimi"
    name = "Kimi"
    short_name = "Kimi"
    badge = "K"
    badge_class = "is-kimi"
    description = "kimi.ai/en in a signed-in WebKitGTK window (Kimi K3 / Moonshot AI)"
    origin = "https://www.kimi.ai/en"
    window_title = "Kimi - Arena Desktop"
    cookie_urls = (
        "https://www.kimi.ai/",
        "https://kimi.ai/",
        "https://www.kimi.com/",
        "https://kimi.com/",
    )
    auth_cookie_names = ("kimi-auth", "refresh_token", "access_token")
    login_url_hints = ("/login", "/signin", "/auth")
    conversation_url_hint = "/chat/"
    observed_fetch_patterns = (
        "/completion/stream",
        "/apiv2/kimi.gateway.chat",
        "/chat/completions",
    )
    # Clicking .send-button-container once Lexical enables it is cleaner than synthetic Enter.
    prefer_enter_submit = False

    composer_selectors = (
        'div.chat-input-editor[contenteditable="true"]',
        '[contenteditable="true"][data-lexical-editor="true"]',
        '[contenteditable="true"][role="textbox"]',
        "textarea",
    )
    send_selectors = (
        ".send-button-container:not(.disabled)",
        'button[aria-label*="Send" i]',
        'button[type="submit"]',
        '[class*="send-button"]:not(.disabled)',
    )
    stop_selectors = (
        'button[aria-label*="Stop" i]',
        '[class*="stop-button"]',
        '[class*="stop-btn"]',
    )
    assistant_selectors = (
        '[class*="segment-assistant"] [class*="markdown"]',
        '[class*="chat-content-item-assistant"] [class*="markdown"]',
        '[class*="segment-assistant"] .segment-content-box',
        '[class*="assistant"] [class*="markdown"]',
        ".markdown-container",
        '[class*="markdown-body"]',
        '[data-role="assistant"]',
    )
    reasoning_selectors = (
        '[class*="think"]',
        '[class*="reasoning"]',
        '[class*="cot"]',
    )
    default_models = (
        {
            "id": "default",
            "name": "Kimi (Account default)",
            "org": "Moonshot AI",
            "description": "Open Kimi window & refresh to retrieve live models from kimi.ai",
            "capabilities": ["Text", "Code"],
        },
    )

    def _is_non_chat_url(self, href: str) -> bool:
        """Navigate back to /en if Kimi is sitting on a side tool page like /make3d or /slides."""
        if not href:
            return False
        path = (urlparse(href).path or "/").rstrip("/") or "/"
        if path in ("/", "/en", "/zh") or path.startswith("/chat/"):
            return False
        return True

    def _read_active_model_label(self) -> str:
        dom = self.eval_js(
            r"""(() => {
              const el = document.querySelector('.current-model, .chat-editor-action .kimi-menu-trigger-wrapper');
              const label = el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '';
              return JSON.stringify({ label });
            })()""",
            timeout=2.5,
        )
        return str(dom.get("label") or "").strip() if isinstance(dom, dict) else ""

    def _probe_live_models(self) -> Optional[dict]:
        """Inspect Kimi EU's live model/effort selector (.current-model) inside the signed-in window."""
        active_lbl = self._read_active_model_label()
        models: list[dict] = []
        seen: set[str] = set()

        def _add(label: str, desc: str, active: bool) -> None:
            clean = " ".join(str(label or "").split()).strip()
            if not clean or len(clean) > 48:
                return
            if re.search(r"\b(sign|log|download|app|upgrade|about|new chat|plugins|project)\b", clean, re.I):
                return
            key = clean.lower()
            if key in seen:
                return
            seen.add(key)
            caps = ["Text", "Code"]
            if any(k in key for k in ("think", "research", "high", "pro", "k3", "k2", "k1.5")):
                caps.append("Reasoning")
            models.append({
                "id": clean,
                "name": clean,
                "org": "Moonshot AI",
                "description": (desc or "Available on kimi.ai")[:120],
                "capabilities": caps,
                "active": active,
            })

        if active_lbl:
            _add(active_lbl, "Currently active on kimi.ai", True)

        # Open Kimi's model dropdown asynchronously to discover all available models
        opened = self.eval_js(
            """(() => {
              const el = document.querySelector('.current-model, .chat-editor-action .kimi-menu-trigger-wrapper');
              if (!el) return JSON.stringify({ ok: false });
              el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
              el.click();
              return JSON.stringify({ ok: true });
            })()""",
            timeout=2.5,
        )
        if isinstance(opened, dict) and opened.get("ok"):
            time.sleep(0.32)
            scraped = self.eval_js(
                r"""(() => {
                  const out = [];
                  const items = Array.from(document.querySelectorAll(
                    '[role="menuitem"], [role="option"], [class*="model-item"], [class*="kimi-menu"] li, [class*="popover"] [role="button"]'
                  ));
                  for (const item of items) {
                    const lines = (item.innerText || item.textContent || '').split('\n').map((s) => s.trim()).filter(Boolean);
                    if (lines.length && /\b(K\d|Kimi|Thinking|High|Medium|Fast|Pro|Research)\b/i.test(lines[0])) {
                      out.push({ name: lines[0], desc: lines.slice(1).join(' - ').slice(0, 100) });
                    }
                  }
                  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true }));
                  return JSON.stringify({ items: out });
                })()""",
                timeout=3.0,
            )
            if isinstance(scraped, dict):
                for item in scraped.get("items") or []:
                    _add(str(item.get("name") or ""), str(item.get("desc") or ""), False)

        if not models:
            return None
        active_id = next((m["id"] for m in models if m.get("active")), models[0]["id"])
        return {
            "live": True,
            "active_model": active_id,
            "models": models,
        }

    def _apply_model_selection(self, model_id: str) -> bool:
        want = (model_id or "").strip()
        if not want or want.lower() == "default":
            return True
        cur = self._read_active_model_label().strip().lower()
        if cur and want.lower() == cur:
            return True
        opened = self.eval_js(
            """(() => {
              const el = document.querySelector('.current-model, .chat-editor-action .kimi-menu-trigger-wrapper');
              if (!el) return JSON.stringify({ ok: false });
              el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
              el.click();
              return JSON.stringify({ ok: true });
            })()""",
            timeout=2.5,
        )
        if not (isinstance(opened, dict) and opened.get("ok")):
            return False
        time.sleep(0.32)
        toks = [t for t in re.split(r"[\s\-_/]+", want.lower()) if t]
        res = self.eval_js(
            f"""(() => {{
              const wantFull = {json.dumps(want.lower())};
              const toks = {json.dumps(toks[:3])};
              const items = Array.from(document.querySelectorAll(
                '[role="menuitem"], [role="option"], [class*="model-item"], [class*="kimi-menu"] li, [class*="popover"] [role="button"]'
              ));
              const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              let fallback = null;
              for (const item of items) {{
                const raw = String(item.innerText || item.textContent || '');
                const firstLine = norm(raw.split('\\n')[0] || '');
                const txt = norm(raw);
                if (firstLine === wantFull || txt === wantFull) {{
                  item.click();
                  return JSON.stringify({{ ok: true, clicked: txt.slice(0, 60) }});
                }}
                if (!fallback && (txt.includes(wantFull) || (toks.length && toks.every((t) => txt.includes(t))))) {{
                  fallback = item;
                }}
              }}
              if (fallback) {{
                const txt = norm(fallback.innerText || fallback.textContent || '');
                fallback.click();
                return JSON.stringify({{ ok: true, clicked: txt.slice(0, 60) }});
              }}
              document.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Escape', code: 'Escape', bubbles: true }}));
              return JSON.stringify({{ ok: false }});
            }})()""",
            timeout=3.0,
        )
        if isinstance(res, dict) and res.get("clicked"):
            time.sleep(0.2)
            return True
        time.sleep(0.15)
        return False

    def _parse_observed_sse_chunk(self, raw_chunk: str, state: dict, *, flush: bool = False) -> list[dict]:
        events: list[dict] = []
        buf = str(state.get("buf") or "") + (raw_chunk or "")
        if flush and buf.strip():
            buf += "\n"

        def _handle_pkt(pkt: dict) -> None:
            ev_name = str(pkt.get("event") or "").lower()
            # Kimi native SSE: {"event": "cmpl", "text": "..."} or {"event": "k1", "text": "..."}
            if ev_name == "cmpl" and isinstance(pkt.get("text"), str) and pkt["text"]:
                events.append({"type": "text", "text": pkt["text"]})
                return
            if ev_name in ("k1", "think", "reasoning") and isinstance(pkt.get("text"), str) and pkt["text"]:
                events.append({"type": "reasoning", "text": pkt["text"]})
                return
            # Kimi EU Connect/gRPC-web JSON: {"block": {"text": {"content": "..."}} | {"think": {"content": "..."}}}
            block = pkt.get("block")
            if isinstance(block, dict):
                think_blk = block.get("think") or block.get("reasoning")
                if isinstance(think_blk, dict) and isinstance(think_blk.get("content"), str) and think_blk["content"]:
                    events.append({"type": "reasoning", "text": think_blk["content"]})
                text_blk = block.get("text")
                if isinstance(text_blk, dict) and isinstance(text_blk.get("content"), str) and text_blk["content"]:
                    events.append({"type": "text", "text": text_blk["content"]})
                if think_blk or text_blk:
                    return
            # OpenAI-compatible delta fallback
            choices = pkt.get("choices")
            if isinstance(choices, list) and choices:
                delta = (choices[0] or {}).get("delta") or {}
                if delta.get("reasoning_content"):
                    events.append({"type": "reasoning", "text": str(delta["reasoning_content"])})
                if delta.get("content"):
                    events.append({"type": "text", "text": str(delta["content"])})

        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            if line.startswith("data:"):
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    pkt = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(pkt, dict):
                    _handle_pkt(pkt)
            elif "{" in line:
                # Connect/gRPC-web frames may contain non-SSE JSON lines (possibly with binary prefix)
                decoder = json.JSONDecoder()
                pos = line.find("{")
                while pos != -1 and pos < len(line):
                    try:
                        pkt, end = decoder.raw_decode(line, pos)
                    except ValueError:
                        pos = line.find("{", pos + 1)
                        continue
                    if isinstance(pkt, dict):
                        _handle_pkt(pkt)
                    pos = line.find("{", end)
        # Also parse Connect/gRPC-web JSON frames incrementally when they lack trailing newlines.
        # Distinguish binary 0x7b ('{') prefix bytes (syntax error before EOF) from legitimately
        # split/truncated JSON frames (unterminated string or error at EOF, wait for next chunk).
        if buf and "\n" not in buf and "{" in buf and not buf.lstrip().startswith("data:"):
            decoder = json.JSONDecoder()
            pos = buf.find("{")
            consumed = 0
            while pos != -1 and pos < len(buf):
                try:
                    pkt, end = decoder.raw_decode(buf, pos)
                except json.JSONDecodeError as err:
                    is_truncated = (
                        err.pos >= len(buf)
                        or err.msg.startswith("Unterminated")
                    )
                    if not is_truncated:
                        next_pos = buf.find("{", pos + 1)
                        if next_pos != -1:
                            pos = next_pos
                            continue
                    break
                except ValueError:
                    break
                if isinstance(pkt, dict):
                    _handle_pkt(pkt)
                consumed = end
                pos = buf.find("{", end)
            if consumed:
                buf = buf[consumed:]
            elif len(buf) > 262144:
                # Safety valve against malformed non-closing garbage frames.
                buf = ""
        state["buf"] = buf
        return events
