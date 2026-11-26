"""DeepSeek web provider (pure WebKitGTK signed-in window, no CLI / no API key).

Uses composer-scoped submission (Enter + scoped Send button — never clicking
sidebar buttons) paired with an observation-only window.fetch SSE clone hook on
``/api/v0/chat/completion`` (identical pattern to ``arena/engine.py:_DRIVE_HOOK_JS``)
so DeepSeek's own frontend handles its legitimate browser session while Arena
Desktop streams real reasoning + answer tokens live, even while hidden.
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from arena.providers.web_engine import WebChatEngine


class DeepSeekProvider(WebChatEngine):
    id = "deepseek"
    name = "DeepSeek"
    short_name = "DeepSeek"
    badge = "\u25c7"
    badge_class = "is-deepseek"
    description = "chat.deepseek.com in a signed-in WebKitGTK window (DeepThink + Search)"
    origin = "https://chat.deepseek.com/"
    window_title = "DeepSeek - Arena Desktop"
    cookie_urls = ("https://chat.deepseek.com/",)
    auth_cookie_names = ("ds_session_id", "HWWAFSESID", "userToken")
    login_url_hints = ("/sign_in", "/login")
    conversation_url_hint = "/a/chat/s/"
    observed_fetch_patterns = ("/api/v0/chat/completion",)
    prefer_enter_submit = True

    composer_selectors = (
        "textarea#chat-input",
        'textarea[placeholder*="DeepSeek"]',
        "textarea",
    )
    # Scoped strictly inside the composer container by WebChatEngine._submit_prompt_dom
    send_selectors = (
        'button[aria-label*="Send"]',
        'button[type="submit"]',
        'div[role="button"][aria-disabled="false"]:has(svg)',
    )
    stop_selectors = (
        'button[aria-label*="Stop"]',
        '[class*="stop-btn"]',
    )
    assistant_selectors = (
        ".ds-markdown",
        ".ds-markdown-paragraph",
        '[class*="ds-markdown"]',
        '[class*="ds-message"] [class*="markdown"]',
        '[class*="markdown"]',
        '[data-role="assistant"]',
    )
    reasoning_selectors = (
        ".ds-think-content",
        '[class*="think-content"]',
        '[class*="reasoning"]',
    )
    # Honest mode defaults (never hardcode outdated version numbers like "V3")
    default_models = (
        {
            "id": "deepseek-default",
            "name": "DeepSeek (Default)",
            "org": "DeepSeek",
            "description": "Current default chat model on chat.deepseek.com",
            "capabilities": ["Text", "Code", "Fast"],
        },
        {
            "id": "deepseek-think",
            "name": "DeepSeek · DeepThink",
            "org": "DeepSeek",
            "description": "Enables DeepThink reasoning mode on chat.deepseek.com",
            "capabilities": ["Text", "Code", "Reasoning"],
        },
        {
            "id": "deepseek-search",
            "name": "DeepSeek · Search",
            "org": "DeepSeek",
            "description": "Enables live web search on chat.deepseek.com",
            "capabilities": ["Text", "Search"],
        },
        {
            "id": "deepseek-think-search",
            "name": "DeepSeek · DeepThink + Search",
            "org": "DeepSeek",
            "description": "Enables both DeepThink reasoning and web search",
            "capabilities": ["Text", "Reasoning", "Search"],
        },
    )

    def _probe_live_models(self) -> Optional[dict]:
        """Inspect DeepSeek's live session API + live composer mode pills."""
        script = r"""(() => {
          const nodes = Array.from(document.querySelectorAll('button, div[role="button"]'));
          const findPill = (re) => nodes.find((el) => {
            const t = (el.innerText || el.textContent || '').trim();
            return t.length > 0 && t.length < 36 && re.test(t);
          });
          const thinkBtn = findPill(/deepthink/i);
          const searchBtn = findPill(/\bsearch\b/i);
          const isOn = (el) => {
            if (!el) return false;
            if (el.getAttribute('aria-pressed') === 'true') return true;
            const cls = String(el.className || '').toLowerCase();
            if (cls.includes('active') || cls.includes('selected') || cls.includes('checked')) return true;
            try {
              const bg = getComputedStyle(el).backgroundColor || '';
              const m = bg.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
              if (m && Number(m[3]) > Number(m[1]) + 25) return true;
            } catch (e) {}
            return false;
          };
          // Also check if DeepSeek exposes a model name in its page/composer/localStorage
          let modelHint = '';
          try {
            const raw = localStorage.getItem('userToken') || '';
            if (raw) modelHint = 'signed_in';
          } catch (e) {}
          return JSON.stringify({
            has_think: Boolean(thinkBtn),
            think_on: isOn(thinkBtn),
            think_label: thinkBtn ? (thinkBtn.innerText || '').replace(/\s+/g, ' ').trim() : '',
            has_search: Boolean(searchBtn),
            search_on: isOn(searchBtn),
            search_label: searchBtn ? (searchBtn.innerText || '').replace(/\s+/g, ' ').trim() : '',
            signed_hint: modelHint,
          });
        })()"""
        res = self.eval_js(script, timeout=3.5)
        if not isinstance(res, dict) or not (res.get("has_think") or res.get("has_search")):
            return None
        think_label = str(res.get("think_label") or "DeepThink").strip()
        search_label = str(res.get("search_label") or "Search").strip()
        think_on = bool(res.get("think_on"))
        search_on = bool(res.get("search_on"))

        if think_on and search_on:
            active_id = "deepseek-think-search"
        elif think_on:
            active_id = "deepseek-think"
        elif search_on:
            active_id = "deepseek-search"
        else:
            active_id = "deepseek-default"

        models = [
            {
                "id": "deepseek-default",
                "name": "DeepSeek (Current default)",
                "org": "DeepSeek",
                "description": f"Live on chat.deepseek.com ({think_label} off, {search_label} off)",
                "capabilities": ["Text", "Code", "Fast"],
                "active": active_id == "deepseek-default",
            },
            {
                "id": "deepseek-think",
                "name": f"DeepSeek - {think_label}",
                "org": "DeepSeek",
                "description": f"Live mode on chat.deepseek.com ({think_label} on)",
                "capabilities": ["Text", "Code", "Reasoning"],
                "active": active_id == "deepseek-think",
            },
            {
                "id": "deepseek-search",
                "name": f"DeepSeek - {search_label}",
                "org": "DeepSeek",
                "description": f"Live mode on chat.deepseek.com ({search_label} on)",
                "capabilities": ["Text", "Search"],
                "active": active_id == "deepseek-search",
            },
            {
                "id": "deepseek-think-search",
                "name": f"DeepSeek - {think_label} + {search_label}",
                "org": "DeepSeek",
                "description": f"Live mode on chat.deepseek.com ({think_label} + {search_label})",
                "capabilities": ["Text", "Reasoning", "Search"],
                "active": active_id == "deepseek-think-search",
            },
        ]
        return {
            "live": True,
            "active_model": active_id,
            "models": models,
        }

    def _apply_model_selection(self, model_id: str) -> None:
        want = (model_id or "").strip().lower()
        if not want:
            return
        want_think = "think" in want or "reasoner" in want or "r1" in want
        want_search = "search" in want
        script = f"""(() => {{
          const wantThink = {json.dumps(want_think)};
          const wantSearch = {json.dumps(want_search)};
          const nodes = Array.from(document.querySelectorAll('button, div[role="button"]'));
          const findPill = (re) => nodes.find((el) => {{
            const t = (el.innerText || el.textContent || '').trim();
            return t.length > 0 && t.length < 36 && re.test(t);
          }});
          const isOn = (el) => {{
            if (!el) return false;
            if (el.getAttribute('aria-pressed') === 'true') return true;
            const cls = String(el.className || '').toLowerCase();
            if (cls.includes('active') || cls.includes('selected') || cls.includes('checked')) return true;
            try {{
              const bg = getComputedStyle(el).backgroundColor || '';
              const m = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
              if (m && Number(m[3]) > Number(m[1]) + 25) return true;
            }} catch (e) {{}}
            return false;
          }};
          const changed = [];
          const thinkBtn = findPill(/deepthink/i);
          if (thinkBtn && isOn(thinkBtn) !== wantThink) {{ thinkBtn.click(); changed.push('think'); }}
          const searchBtn = findPill(/\\bsearch\\b/i);
          if (searchBtn && isOn(searchBtn) !== wantSearch) {{ searchBtn.click(); changed.push('search'); }}
          return JSON.stringify({{ ok: true, changed }});
        }})()"""
        res = self.eval_js(script, timeout=3.0)
        if isinstance(res, dict) and res.get("changed"):
            time.sleep(0.15)

    def _parse_observed_sse_chunk(self, raw_chunk: str, state: dict) -> list[dict]:
        """Parse DeepSeek's live SSE response stream (observation-only)."""
        events: list[dict] = []
        buf = str(state.get("buf") or "") + (raw_chunk or "")
        cur_path = str(state.get("path") or "")
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

            # Format A (OpenAI-compatible choices delta)
            choices = pkt.get("choices")
            if isinstance(choices, list) and choices:
                delta = (choices[0] or {}).get("delta") or {}
                if delta.get("reasoning_content"):
                    events.append({"type": "reasoning", "text": str(delta["reasoning_content"])})
                if delta.get("content"):
                    events.append({"type": "text", "text": str(delta["content"])})
                continue

            # Format B (DeepSeek patch stream: {"p": "response/content", "o": "APPEND", "v": "..."})
            if isinstance(pkt.get("p"), str):
                cur_path = pkt["p"]
                state["path"] = cur_path
            val = pkt.get("v")
            if isinstance(val, str) and val:
                if "thinking" in cur_path or "reason" in cur_path:
                    events.append({"type": "reasoning", "text": val})
                elif "content" in cur_path or not cur_path:
                    events.append({"type": "text", "text": val})
            elif isinstance(val, list):
                for item in val:
                    if not isinstance(item, dict):
                        continue
                    itype = str(item.get("type") or "").upper()
                    icontent = item.get("content")
                    if isinstance(icontent, str) and icontent:
                        if "THINK" in itype:
                            events.append({"type": "reasoning", "text": icontent})
                        elif "RESPONSE" in itype or "TEXT" in itype or not itype:
                            events.append({"type": "text", "text": icontent})
        state["buf"] = buf
        return events

    def _poll_conversation_api(self, href: str) -> Optional[dict]:
        """Read DeepSeek's signed-in /api/v0/chat/history_messages when hidden DOM lags."""
        m = re.search(r"/a/chat/s/([0-9a-fA-F-]{16,})", href or "")
        if not m:
            return None
        session_id = m.group(1)
        token_val = self.eval_js(
            """(() => {
              try {
                const raw = localStorage.getItem('userToken') || '';
                if (!raw) return '';
                try { const p = JSON.parse(raw); return String(p.value || p.token || raw); } catch (e) { return raw; }
              } catch (e) { return ''; }
            })()""",
            timeout=2.0,
        )
        headers = {"Authorization": f"Bearer {token_val}"} if isinstance(token_val, str) and token_val else {}
        res = self.fetch_json(
            f"/api/v0/chat/history_messages?chat_session_id={session_id}",
            headers=headers,
            timeout=4.0,
        )
        if not res.get("ok") or not isinstance(res.get("data"), dict):
            return None
        biz = ((res["data"].get("data") or {}).get("biz_data") or {})
        msgs = biz.get("chat_messages")
        if not isinstance(msgs, list):
            return None
        assistants = [msg for msg in msgs if isinstance(msg, dict) and str(msg.get("role") or "").upper() == "ASSISTANT"]
        if not assistants:
            return {"count": 0, "text": "", "reasoning": "", "streaming": False}
        last = assistants[-1]
        text = str(last.get("content") or "")
        reasoning = str(last.get("thinking_content") or "")
        # DeepSeek v2 fragments list fallback
        if not text and isinstance(last.get("fragments"), list):
            t_parts, r_parts = [], []
            for frag in last["fragments"]:
                if not isinstance(frag, dict):
                    continue
                ftype = str(frag.get("type") or "").upper()
                fcontent = str(frag.get("content") or "")
                if not fcontent:
                    continue
                if "THINK" in ftype:
                    r_parts.append(fcontent)
                else:
                    t_parts.append(fcontent)
            text = "\n".join(t_parts).strip()
            if not reasoning:
                reasoning = "\n".join(r_parts).strip()
        status = str(last.get("status") or "").upper()
        streaming = status in ("WIP", "PENDING", "STREAMING", "IN_PROGRESS") or (not text and not reasoning)
        return {
            "count": len(assistants),
            "text": text,
            "reasoning": reasoning,
            "streaming": streaming,
        }
