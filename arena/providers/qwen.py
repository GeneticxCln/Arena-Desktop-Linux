"""Qwen web provider (pure WebKitGTK signed-in window, no CLI / no API key)."""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from arena.providers.web_engine import WebChatEngine


def _match_active_label(models: list[dict], label: str) -> str:
    """Id of the listed model whose name or id equals the page's model button
    label, or "" when the label names none of them."""
    want = " ".join((label or "").split()).lower()
    if not want:
        return ""
    for m in models:
        if want in (str(m.get("name") or "").strip().lower(), str(m.get("id") or "").strip().lower()):
            return str(m["id"])
    return ""


class QwenProvider(WebChatEngine):
    id = "qwen"
    name = "Qwen"
    short_name = "Qwen"
    badge = "Q"
    badge_class = "is-qwen"
    description = "chat.qwen.ai in a signed-in WebKitGTK window (Qwen3 / Qwen Coder)"
    origin = "https://chat.qwen.ai/"
    window_title = "Qwen - Arena Desktop"
    cookie_urls = ("https://chat.qwen.ai/",)
    auth_cookie_names = ("token", "ssxmod_itna", "cna")
    login_url_hints = ("/auth", "/login", "/signin")
    conversation_url_hint = "/c/"
    observed_fetch_patterns = (
        "/api/v2/chat/completions",
        "/api/chat/completions",
    )
    prefer_enter_submit = True

    composer_selectors = (
        "textarea#chat-input",
        'textarea[placeholder*="Qwen"]',
        'textarea[placeholder*="Ask"]',
        '[contenteditable="true"][role="textbox"]',
        "textarea",
    )
    send_selectors = (
        "button#send-message-button",
        'button[aria-label*="Send"]',
        'button[type="submit"]',
    )
    stop_selectors = (
        'button[aria-label*="Stop"]',
        'button[id*="stop"]',
        '[class*="stop-btn"]',
    )
    assistant_selectors = (
        '[class*="response-message"] [class*="markdown"]',
        ".markdown-prose",
        '[class*="assistant"] [class*="markdown"]',
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
            "name": "Qwen (Account default)",
            "org": "Alibaba",
            "description": "Open Qwen window & refresh to retrieve live models from chat.qwen.ai",
            "capabilities": ["Text", "Code"],
        },
    )

    def _bearer_headers(self) -> dict:
        tok = self.eval_js(
            "(() => { try { return String(localStorage.getItem('token') || ''); } catch (e) { return ''; } })()",
            timeout=2.0,
        )
        return {"Authorization": f"Bearer {tok}"} if isinstance(tok, str) and tok else {}

    def _probe_live_models(self) -> Optional[dict]:
        """Retrieve live Qwen models from chat.qwen.ai/api/models + DOM selector."""
        headers = self._bearer_headers()
        models: list[dict] = []
        seen: set[str] = set()

        for endpoint in ("/api/models", "/api/v2/models"):
            res = self.fetch_json(endpoint, headers=headers, timeout=4.5)
            data = res.get("data")
            raw_list = data.get("data") if isinstance(data, dict) else (data if isinstance(data, list) else None)
            if not (res.get("ok") and isinstance(raw_list, list) and raw_list):
                continue
            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("id") or item.get("model") or "").strip()
                name = str(item.get("name") or item.get("title") or mid).strip()
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                low = f"{mid} {name}".lower()
                caps = ["Text", "Code"]
                if "coder" in low:
                    caps.append("Coding")
                if any(k in low for k in ("qwq", "think", "reason")):
                    caps.append("Reasoning")
                if "vl" in low or "omni" in low:
                    caps.append("Vision")
                info = item.get("info") if isinstance(item.get("info"), dict) else {}
                meta = info.get("meta") if isinstance(info.get("meta"), dict) else {}
                desc = str(meta.get("description") or item.get("description") or "Available on chat.qwen.ai")
                models.append({
                    "id": mid,
                    "name": name,
                    "org": "Alibaba",
                    "description": desc[:120],
                    "capabilities": caps,
                    "active": False,
                })
            if models:
                break

        # The model button on the page names the model that is selected right now.
        dom = self.eval_js(
            r"""(() => {
              const btn = Array.from(document.querySelectorAll('button')).find((b) => {
                const t = (b.innerText || b.textContent || '').trim();
                return t.length > 0 && t.length < 48 && /\b(Qwen|QwQ)\b/i.test(t);
              });
              const label = btn ? (btn.innerText || btn.textContent || '').replace(/\s+/g, ' ').trim() : '';
              return JSON.stringify({ label });
            })()""",
            timeout=3.0,
        )
        label = str(dom.get("label") or "").strip() if isinstance(dom, dict) else ""
        if label and not models:
            models.append({
                "id": label,
                "name": label,
                "org": "Alibaba",
                "description": "Currently active on chat.qwen.ai",
                "capabilities": ["Text", "Code"],
                "active": False,
            })

        if not models:
            return None
        active_id = _match_active_label(models, label)
        models = [{**m, "active": m["id"] == active_id} for m in models]
        return {
            "live": True,
            "active_model": active_id,
            "models": models,
        }

    def _apply_model_selection(self, model_id: str) -> bool:
        want = (model_id or "").strip()
        if not want or want.lower() == "default":
            return True
        tokens = [t for t in re.split(r"[\s\-_/]+", want.lower()) if t]
        script = f"""(() => {{
          const wantFull = {json.dumps(want.lower())};
          const toks = {json.dumps(tokens[:3])};
          const btn = Array.from(document.querySelectorAll('button')).find((b) => {{
            const t = (b.innerText || b.textContent || '').trim();
            return t.length > 0 && t.length < 48 && /\\b(Qwen|QwQ)\\b/i.test(t);
          }});
          if (!btn) return JSON.stringify({{ ok: false }});
          const cur = (btn.innerText || btn.textContent || '').toLowerCase();
          if (cur.includes(wantFull) || (toks.length && toks.every((t) => cur.includes(t)))) {{
            return JSON.stringify({{ ok: true, already: true }});
          }}
          btn.click();
          const items = Array.from(document.querySelectorAll('[role="menuitem"], [role="option"], [data-value]'));
          for (const item of items) {{
            const txt = ((item.innerText || item.textContent || '') + ' ' + (item.getAttribute('data-value') || '')).toLowerCase();
            if (txt.includes(wantFull) || (toks.length && toks.every((t) => txt.includes(t)))) {{
              item.click();
              return JSON.stringify({{ ok: true, clicked: txt.slice(0, 60) }});
            }}
          }}
          document.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Escape', code: 'Escape', bubbles: true }}));
          return JSON.stringify({{ ok: false }});
        }})()"""
        res = self.eval_js(script, timeout=3.5)
        if not (isinstance(res, dict) and res.get("ok")):
            return False
        if res.get("clicked"):
            time.sleep(0.2)
        return True

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
            choices = pkt.get("choices")
            if isinstance(choices, list) and choices:
                delta = (choices[0] or {}).get("delta") or {}
                phase = str(delta.get("phase") or "").lower()
                if delta.get("reasoning_content"):
                    events.append({"type": "reasoning", "text": str(delta["reasoning_content"])})
                content = delta.get("content")
                if isinstance(content, str) and content:
                    if phase == "think":
                        events.append({"type": "reasoning", "text": content})
                    else:
                        events.append({"type": "text", "text": content})
        state["buf"] = buf
        return events
