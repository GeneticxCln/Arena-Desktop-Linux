"""Z.AI / GLM web provider (pure WebKitGTK signed-in window, no CLI / no API key)."""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from arena.providers.web_engine import WebChatEngine


class ZaiProvider(WebChatEngine):
    id = "zai"
    name = "Z.AI / GLM"
    short_name = "Z.AI"
    badge = "Z"
    badge_class = "is-zai"
    description = "chat.z.ai in a signed-in WebKitGTK window (GLM coding & reasoning)"
    origin = "https://chat.z.ai/"
    window_title = "Z.AI / GLM - Arena Desktop"
    cookie_urls = ("https://chat.z.ai/",)
    auth_cookie_names = ("token", "chatglm_token")
    login_url_hints = ("/auth", "/login", "/signin")
    conversation_url_hint = "/c/"
    observed_fetch_patterns = (
        "/api/v2/chat/completions",
        "/api/chat/completions",
    )
    # Clicking #send-message-button directly submits cleanly on chat.z.ai (Svelte)
    # without risking Enter being swallowed by focus/dropdown state.
    prefer_enter_submit = False

    composer_selectors = (
        "textarea#chat-input",
        'textarea[placeholder*="Send a Message"]',
        'textarea[placeholder*="Ask"]',
        'textarea[placeholder*="Message"]',
        '[contenteditable="true"][role="textbox"]',
        "textarea",
    )
    send_selectors = (
        "button#send-message-button",
        'button[aria-label*="Send Message" i]',
        'button[aria-label*="Send" i]',
        'button[type="submit"]',
    )
    stop_selectors = (
        'button[aria-label*="Stop" i]',
        'button[id*="stop"]',
        '[class*="stop-btn"]',
    )
    # Exclude .chat-user (Z.AI puts .markdown-prose on user messages too!)
    assistant_selectors = (
        ".chat-assistant .markdown-prose",
        ".chat-assistant",
        "#response-content-container",
        ".markdown-prose:not(.chat-user)",
        '[class*="response-content"] [class*="markdown"]',
        '[data-role="assistant"]',
    )
    reasoning_selectors = (
        '[class*="thinking"]',
        '[class*="reasoning"]',
        '[class*="thought"]',
        "details",
    )
    default_models = (
        {
            "id": "default",
            "name": "GLM (Account default)",
            "org": "Z.AI",
            "description": "Open Z.AI window & refresh to retrieve live GLM models from chat.z.ai",
            "capabilities": ["Text", "Code"],
        },
    )

    def __init__(self) -> None:
        super().__init__()
        self._model_id_to_name: dict[str, str] = {}

    def _bearer_headers(self) -> dict:
        tok = self.eval_js(
            "(() => { try { return String(localStorage.getItem('token') || ''); } catch (e) { return ''; } })()",
            timeout=2.0,
        )
        return {"Authorization": f"Bearer {tok}"} if isinstance(tok, str) and tok else {}

    def _read_active_button_label(self) -> str:
        dom = self.eval_js(
            r"""(() => {
              const btn = document.querySelector('button.modelSelectorButton, button[aria-label="Select a model"]')
                || Array.from(document.querySelectorAll('button')).find((b) => {
                  const t = (b.innerText || b.textContent || '').trim();
                  return t.length > 0 && t.length < 48 && /\b(GLM|Z\.AI)\b/i.test(t);
                });
              const label = btn ? (btn.innerText || btn.textContent || '').replace(/\s+/g, ' ').trim() : '';
              return JSON.stringify({ label });
            })()""",
            timeout=2.5,
        )
        return str(dom.get("label") or "").strip() if isinstance(dom, dict) else ""

    @staticmethod
    def _normalize_model_label(label: str) -> str:
        """Normalize e.g. 'GLM-5.3-Flash Max' -> 'glm-5.3-flash' for exact model matching."""
        cleaned = " ".join(str(label or "").split()).strip().lower()
        cleaned = re.sub(r"\s+(max|high|medium|low|default|auto|pro\s+plan)$", "", cleaned)
        return cleaned.strip()

    def _probe_live_models(self) -> Optional[dict]:
        """Retrieve live GLM models from chat.z.ai/api/models + active model button."""
        headers = self._bearer_headers()
        raw_btn_label = self._read_active_button_label()
        cur_norm = self._normalize_model_label(raw_btn_label)
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
                info = item.get("info") if isinstance(item.get("info"), dict) else {}
                meta = info.get("meta") if isinstance(info.get("meta"), dict) else {}
                if meta.get("hidden") is True:
                    continue
                mid = str(item.get("id") or item.get("model") or "").strip()
                name = str(item.get("name") or item.get("title") or mid).strip()
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                low = f"{mid} {name}".lower()
                caps = ["Text", "Code"]
                meta_caps = meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
                if meta_caps.get("think") or any(k in low for k in ("z1", "think", "reason")):
                    caps.append("Reasoning")
                if meta_caps.get("vision"):
                    caps.append("Vision")
                if "air" in low or "flash" in low:
                    caps.append("Fast")
                desc_obj = (meta.get("info") or {}).get("desc") if isinstance(meta.get("info"), dict) else None
                if isinstance(desc_obj, dict):
                    desc = str(desc_obj.get("en") or desc_obj.get("cn") or "")
                else:
                    desc = str(meta.get("description") or item.get("description") or "")
                if not desc:
                    desc = "Available on chat.z.ai"
                # Exact normalized comparison so "glm-5.3" never falsely matches "glm-5.3-flash"!
                self._model_id_to_name[mid.lower()] = self._normalize_model_label(name)
                is_active = bool(cur_norm and cur_norm in (self._normalize_model_label(name), mid.lower()))
                models.append({
                    "id": mid,
                    "name": name,
                    "org": "Z.AI",
                    "description": desc[:120],
                    "capabilities": caps,
                    "active": is_active,
                })
            if models:
                break

        if not models and raw_btn_label:
            models.append({
                "id": raw_btn_label,
                "name": raw_btn_label,
                "org": "Z.AI",
                "description": "Currently active on chat.z.ai",
                "capabilities": ["Text", "Code"],
                "active": True,
            })

        if not models:
            return None
        if not any(m.get("active") for m in models):
            models[0]["active"] = True
        active_id = next((m["id"] for m in models if m.get("active")), models[0]["id"])
        return {
            "live": True,
            "active_model": active_id,
            "models": models,
        }

    def _apply_model_selection(self, model_id: str) -> Optional[bool]:
        """Select a GLM model on chat.z.ai via Bits-UI pointerdown + exact data-value click."""
        want = (model_id or "").strip()
        if not want or want.lower() == "default":
            return True
        want_low = want.lower()
        want_name = self._model_id_to_name.get(want_low, "")
        if not want_name:
            for m in (self._models_cache or {}).get("models") or []:
                if str(m.get("id") or "").strip().lower() == want_low:
                    want_name = self._normalize_model_label(str(m.get("name") or ""))
                    break
        cur_norm = self._normalize_model_label(self._read_active_button_label())
        if cur_norm and cur_norm in (want_low, want_name):
            return True

        # Step 1: Open Bits-UI / Svelte dropdown with pointerdown ONLY (btn.click() can toggle it shut).
        opened = self.eval_js(
            """(() => {
              const btn = document.querySelector('button.modelSelectorButton, button[aria-label="Select a model"]');
              if (!btn) return JSON.stringify({ ok: false });
              btn.dispatchEvent(new PointerEvent('pointerdown', {
                bubbles: true, cancelable: true, button: 0, pointerType: 'mouse'
              }));
              return JSON.stringify({ ok: true });
            })()""",
            timeout=2.5,
        )
        if not (isinstance(opened, dict) and opened.get("ok")):
            return False
        time.sleep(0.32)

        # Step 2: Click exact matching button[data-value] (never loose substring match), else Escape cleanly.
        res = self.eval_js(
            f"""(() => {{
              const wantId = {json.dumps(want_low)};
              const wantName = {json.dumps(want_name)};
              const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase()
                .replace(/\\s+(max|high|medium|low|default|auto|pro\\s+plan)$/, '').trim();
              const items = Array.from(document.querySelectorAll(
                'button[data-value], [role="menuitem"], [role="option"], [data-bits-menu-item]'
              ));
              const clickItem = (el) => {{
                el.dispatchEvent(new PointerEvent('pointerdown', {{ bubbles: true, cancelable: true, button: 0, pointerType: 'mouse' }}));
                el.dispatchEvent(new PointerEvent('pointerup', {{ bubbles: true, cancelable: true, button: 0, pointerType: 'mouse' }}));
                el.click();
              }};
              // Exact data-value match first
              let chosen = items.find((el) => String(el.getAttribute('data-value') || '').trim().toLowerCase() === wantId);
              // Exact normalized label match second
              if (!chosen && wantName) {{
                chosen = items.find((el) => norm(el.innerText || el.textContent || '') === wantName);
              }}
              if (!chosen) {{
                chosen = items.find((el) => norm(el.innerText || el.textContent || '') === wantId);
              }}
              if (chosen && !chosen.disabled && chosen.getAttribute('aria-disabled') !== 'true') {{
                clickItem(chosen);
                return JSON.stringify({{ ok: true, clicked: chosen.getAttribute('data-value') || norm(chosen.innerText) }});
              }}
              document.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Escape', code: 'Escape', bubbles: true }}));
              return JSON.stringify({{ ok: false, disabled: Boolean(chosen && chosen.disabled) }});
            }})()""",
            timeout=3.0,
        )
        if isinstance(res, dict) and res.get("clicked"):
            time.sleep(0.25)
            return True
        time.sleep(0.15)
        return False

    def _poll_conversation_api(self, href: str) -> Optional[dict]:
        """Read Z.AI's signed-in /api/v1/chats/<uuid> history when hidden DOM lags."""
        m = re.search(r"/c/([0-9a-fA-F-]{16,})", href or "")
        if not m:
            return None
        chat_uuid = m.group(1)
        res = self.fetch_json(f"/api/v1/chats/{chat_uuid}", headers=self._bearer_headers(), timeout=4.0)
        if not res.get("ok") or not isinstance(res.get("data"), dict):
            return None
        chat_obj = res["data"].get("chat") if isinstance(res["data"].get("chat"), dict) else res["data"]
        hist = chat_obj.get("history") if isinstance(chat_obj.get("history"), dict) else {}
        msgs_map = hist.get("messages") if isinstance(hist.get("messages"), dict) else {}
        assistants = [
            msg for msg in msgs_map.values()
            if isinstance(msg, dict) and str(msg.get("role") or "").lower() == "assistant"
        ]
        if not assistants:
            return {"count": 0, "text": "", "reasoning": "", "streaming": False}
        assistants.sort(key=lambda x: float(x.get("timestamp") or 0))
        last = assistants[-1]
        raw_content = str(last.get("content") or "")
        reasoning = ""
        text = raw_content
        # Strip Z.AI <details type="reasoning">...</details> into separate reasoning
        think_match = re.search(r"<details[^>]*>(.*?)</details>", raw_content, flags=re.S | re.I)
        if think_match:
            inner = re.sub(r"<summary[^>]*>.*?</summary>", "", think_match.group(1), flags=re.S | re.I)
            inner = re.sub(r"<[^>]+>", "", inner).strip()
            reasoning = inner
            text = (raw_content[:think_match.start()] + raw_content[think_match.end():]).strip()
        done_flag = last.get("done")
        streaming = (done_flag is False) or (not text and not reasoning)
        return {
            "count": len(assistants),
            "text": text,
            "reasoning": reasoning,
            "streaming": streaming,
        }

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

            # Format A: Z.AI envelope {"type": "chat:completion", "data": {"delta_content": "...", "phase": "thinking"|"answer"}}
            inner = pkt.get("data")
            if isinstance(inner, dict) and isinstance(inner.get("delta_content"), str) and inner["delta_content"]:
                phase = str(inner.get("phase") or "").lower()
                if phase == "thinking":
                    events.append({"type": "reasoning", "text": inner["delta_content"]})
                elif phase != "other":
                    events.append({"type": "text", "text": inner["delta_content"]})
                continue

            # Format B: OpenAI-compatible choices delta
            choices = pkt.get("choices")
            if isinstance(choices, list) and choices:
                delta = (choices[0] or {}).get("delta") or {}
                if delta.get("reasoning_content"):
                    events.append({"type": "reasoning", "text": str(delta["reasoning_content"])})
                if delta.get("content"):
                    events.append({"type": "text", "text": str(delta["content"])})
        state["buf"] = buf
        return events
