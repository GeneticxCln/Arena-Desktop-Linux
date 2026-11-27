"""MiniMax web provider — European/International agent.minimax.io (pure WebKitGTK, no CLI / no API key)."""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from arena.providers.web_engine import WebChatEngine


class MiniMaxProvider(WebChatEngine):
    id = "minimax"
    name = "MiniMax"
    short_name = "MiniMax"
    badge = "M"
    badge_class = "is-minimax"
    description = "agent.minimax.io in a signed-in WebKitGTK window (MiniMax Code / M3.1)"
    origin = "https://agent.minimax.io/"
    window_title = "MiniMax - Arena Desktop"
    cookie_urls = (
        "https://agent.minimax.io/",
        "https://minimax.io/",
        "https://www.minimax.io/",
    )
    auth_cookie_names = ("HERTZ-SESSION", "token", "_token", "mm_token", "session")
    login_url_hints = ("/login", "/signin", "/auth")
    conversation_url_hint = "/chat/"
    observed_fetch_patterns = (
        "/v1/api/chat/msg",
        "/api/chat/completions",
        "/chat/send",
        "/agent/chat",
    )
    # Clicking [role="button"][aria-label="Send message"] submits cleanly on TipTap/React.
    prefer_enter_submit = False

    composer_selectors = (
        'div.tiptap.ProseMirror[contenteditable="true"]',
        '.ProseMirror[contenteditable="true"]',
        '[contenteditable="true"][aria-label*="Enter message" i]',
        '[contenteditable="true"][role="textbox"]',
        "textarea#chat-input",
        "textarea",
    )
    send_selectors = (
        '[role="button"][aria-label*="Send" i]',
        'button[aria-label*="Send" i]',
        'button[type="submit"]',
        '[class*="send-btn"]',
        '[class*="send-button"]',
    )
    stop_selectors = (
        '[role="button"][aria-label*="Stop" i]',
        'button[aria-label*="Stop" i]',
        '[class*="stop-btn"]',
        '[class*="stop-button"]',
    )
    assistant_selectors = (
        '[class*="assistant"] [class*="markdown"]',
        '[class*="message-content"] [class*="markdown"]',
        ".markdown-body",
        ".prose",
        '[data-role="assistant"]',
        "article .markdown",
    )
    reasoning_selectors = (
        '[class*="thinking"]',
        '[class*="reasoning"]',
        '[class*="thought"]',
    )
    default_models = (
        {
            "id": "default",
            "name": "MiniMax (Account default)",
            "org": "MiniMax",
            "description": "Open MiniMax window & refresh to retrieve live models from agent.minimax.io",
            "capabilities": ["Text", "Code"],
        },
    )

    def _dismiss_onboarding_modal(self) -> None:
        """Close the 'New MiniMax Code Update' onboarding popup if it covers the composer."""
        self.eval_js(
            """(() => {
              const modals = Array.from(document.querySelectorAll('[role="dialog"], .ant-modal, [class*="modal"]'));
              for (const m of modals) {
                const txt = (m.innerText || '').toLowerCase();
                if (!/update|welcome|what.?s new|try it now/.test(txt)) continue;
                const btn = m.querySelector('button[aria-label="Close"]');
                if (btn && btn.offsetParent !== null) { btn.click(); return 1; }
              }
              return 0;
            })()""",
            timeout=1.5,
        )

    def _submit_prompt_dom(self, full_prompt: str) -> dict:
        self._dismiss_onboarding_modal()
        return super()._submit_prompt_dom(full_prompt)

    def _read_active_model_label(self) -> str:
        dom = self.eval_js(
            r"""(() => {
              const btn = document.querySelector('button.mavis-model-selector-trigger')
                || Array.from(document.querySelectorAll('button, div[role="button"]')).find((b) => {
                  const t = (b.innerText || b.textContent || '').trim();
                  return t.length > 0 && t.length < 44 && /\b(M\d|MiniMax)\b/i.test(t) && !/update|try|download|sign/i.test(t);
                });
              const label = btn ? (btn.innerText || btn.textContent || '').replace(/\s+/g, ' ').trim() : '';
              return JSON.stringify({ label });
            })()""",
            timeout=2.5,
        )
        return str(dom.get("label") or "").strip() if isinstance(dom, dict) else ""

    def _probe_live_models(self) -> Optional[dict]:
        """Inspect MiniMax's live model API / .mavis-model-selector-trigger inside agent.minimax.io."""
        self._dismiss_onboarding_modal()
        models: list[dict] = []
        seen: set[str] = set()

        def _add(mid: str, name: str, desc: str, active: bool) -> None:
            clean = " ".join(str(name or mid or "").split()).strip()
            clean_id = " ".join(str(mid or clean).split()).strip()
            if not clean or len(clean) > 56:
                return
            if re.search(r"\b(sign|log|download|upgrade|about|new task|pricing|try it)\b", clean, re.I):
                return
            k = clean_id.lower()
            if k in seen:
                return
            seen.add(k)
            low = f"{clean_id} {clean}".lower()
            caps = ["Text", "Code"]
            if any(w in low for w in ("m3", "m2", "m1", "reason", "think", "pro", "high")):
                caps.append("Reasoning")
            if "flash" in low or "lightning" in low:
                caps.append("Fast")
            models.append({
                "id": clean_id,
                "name": clean,
                "org": "MiniMax",
                "description": (desc or "Available on agent.minimax.io")[:120],
                "capabilities": caps,
                "active": active,
            })

        for endpoint in ("/api/models", "/v1/api/chat/models"):
            res = self.fetch_json(endpoint, timeout=3.5)
            data = res.get("data")
            raw_list = data.get("data") if isinstance(data, dict) else (data if isinstance(data, list) else None)
            if not (res.get("ok") and isinstance(raw_list, list) and raw_list):
                continue
            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("id") or item.get("model") or item.get("model_id") or "").strip()
                name = str(item.get("name") or item.get("title") or mid).strip()
                _add(mid, name, str(item.get("description") or ""), len(models) == 0)
            if models:
                break

        if not models:
            active_lbl = self._read_active_model_label()
            if active_lbl:
                _add(active_lbl, active_lbl, "Currently active on agent.minimax.io", True)
            opened = self.eval_js(
                """(() => {
                  const btn = document.querySelector('button.mavis-model-selector-trigger');
                  if (!btn) return JSON.stringify({ ok: false });
                  btn.click();
                  return JSON.stringify({ ok: true });
                })()""",
                timeout=2.5,
            )
            if isinstance(opened, dict) and opened.get("ok"):
                time.sleep(0.35)
                scraped = self.eval_js(
                    r"""(() => {
                      const out = [];
                      const nodes = Array.from(document.querySelectorAll(
                        '[role="menuitem"], [role="option"], .ant-popover [role="button"], .ant-dropdown-menu-item, body > div:last-child button, body > div:last-child [class*="cursor-pointer"]'
                      ));
                      for (const el of nodes) {
                        const lines = (el.innerText || el.textContent || '').split('\n').map((s) => s.trim()).filter(Boolean);
                        if (lines.length && /\b(M\d|MiniMax|Flash|Pro|High|Default)\b/i.test(lines[0])) {
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
                        _add(str(item.get("name") or ""), str(item.get("name") or ""), str(item.get("desc") or ""), False)

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
              const btn = document.querySelector('button.mavis-model-selector-trigger');
              if (!btn) return JSON.stringify({ ok: false });
              btn.click();
              return JSON.stringify({ ok: true });
            })()""",
            timeout=2.5,
        )
        if not (isinstance(opened, dict) and opened.get("ok")):
            return False
        time.sleep(0.35)
        toks = [t for t in re.split(r"[\s\-_/]+", want.lower()) if t]
        res = self.eval_js(
            f"""(() => {{
              const wantFull = {json.dumps(want.lower())};
              const toks = {json.dumps(toks[:3])};
              const nodes = Array.from(document.querySelectorAll(
                '[role="menuitem"], [role="option"], .ant-popover [role="button"], .ant-dropdown-menu-item, body > div:last-child button, body > div:last-child [class*="cursor-pointer"]'
              ));
              const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              let fallback = null;
              for (const item of nodes) {{
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
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                pkt = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(pkt, dict):
                continue

            # Format A: OpenAI-compatible choices delta
            choices = pkt.get("choices")
            if isinstance(choices, list) and choices:
                delta = (choices[0] or {}).get("delta") or {}
                if delta.get("reasoning_content"):
                    events.append({"type": "reasoning", "text": str(delta["reasoning_content"])})
                if delta.get("content"):
                    events.append({"type": "text", "text": str(delta["content"])})
                continue

            # Format B: MiniMax native web chat SSE payload
            data = pkt.get("data") if isinstance(pkt.get("data"), dict) else pkt
            if isinstance(data.get("reasoning_content"), str) and data["reasoning_content"]:
                events.append({"type": "reasoning", "text": data["reasoning_content"]})
            for key in ("delta", "text", "reply", "content"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    if key == "reply":
                        prev = str(state.get("last_full") or "")
                        if val.startswith(prev):
                            diff = val[len(prev):]
                            state["last_full"] = val
                            if diff:
                                events.append({"type": "text", "text": diff})
                        elif val != prev:
                            state["last_full"] = val
                            events.append({"type": "text_replace", "text": val})
                    else:
                        events.append({"type": "text", "text": val})
                    break
        state["buf"] = buf
        return events
