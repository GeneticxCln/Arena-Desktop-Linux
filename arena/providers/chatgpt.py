"""ChatGPT web provider (pure WebKitGTK signed-in window, no CLI / no API key).

Retrieves live account models via chatgpt.com's signed-in ``/backend-api/models``
endpoint and streams replies via an observation-only ``window.fetch`` SSE clone
hook on ``/backend-api/f/conversation`` (never forging Sentinel/PoW tokens and
never misreading stale messages from previous conversations).
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional
from urllib.parse import quote

from arena.providers.web_engine import WebChatEngine


class ChatGPTProvider(WebChatEngine):
    id = "chatgpt"
    name = "ChatGPT"
    short_name = "ChatGPT"
    badge = "\u25ce"
    badge_class = "is-chatgpt"
    description = "chatgpt.com in a signed-in WebKitGTK window"
    origin = "https://chatgpt.com/"
    window_title = "ChatGPT - Arena Desktop"
    cookie_urls = ("https://chatgpt.com/", "https://auth.openai.com/")
    auth_cookie_names = (
        "__Secure-next-auth.session-token",
        "__Secure-next-auth.session-token.0",
        "oai-did",
    )
    login_url_hints = ("/auth/login", "auth.openai.com")
    conversation_url_hint = "/c/"
    observed_fetch_patterns = (
        "/backend-api/f/conversation",
        "/backend-api/conversation",
    )
    prefer_enter_submit = False

    composer_selectors = (
        "#prompt-textarea",
        'div[contenteditable="true"][id="prompt-textarea"]',
        'textarea[data-id="root"]',
        '[contenteditable="true"][role="textbox"]',
    )
    send_selectors = (
        'button#composer-submit-button',
        'button[data-testid="send-button"]',
        'button[aria-label*="Send"]',
        'button[type="submit"]',
    )
    stop_selectors = (
        'button[data-testid="stop-button"]',
        'button[aria-label*="Stop"]',
    )
    assistant_selectors = (
        '[data-message-author-role="assistant"] .markdown',
        '[data-message-author-role="assistant"]',
        "article .markdown",
    )
    reasoning_selectors = (
        '[data-testid="reasoning-content"]',
        '[class*="thought"]',
    )
    default_models = (
        {
            "id": "auto",
            "name": "ChatGPT (Account default)",
            "org": "OpenAI",
            "description": "Open ChatGPT window & refresh to retrieve live account models",
            "capabilities": ["Text", "Code"],
        },
    )

    def _target_url_for_model(self, conversation_url: str, model_id: str) -> str:
        if conversation_url:
            return conversation_url
        slug = (model_id or "").strip()
        if not slug or slug in ("auto", "default"):
            return self.origin
        return f"https://chatgpt.com/?model={quote(slug, safe='-_.')}"

    def _probe_live_models(self) -> Optional[dict]:
        """Fetch live models from chatgpt.com's signed-in /backend-api/models."""
        kick = r"""(() => {
          window.__apCgModels = { done: false, models: [], active: '' };
          const switcher = document.querySelector('[data-testid="model-switcher-dropdown-button"]');
          if (switcher) {
            window.__apCgModels.active = (switcher.innerText || switcher.textContent || '').replace(/\s+/g, ' ').trim();
          }
          (async () => {
            try {
              const headers = {};
              try {
                const sResp = await fetch('/api/auth/session', { credentials: 'include' });
                if (sResp.ok) {
                  const sData = await sResp.json();
                  if (sData && sData.accessToken) headers['Authorization'] = 'Bearer ' + sData.accessToken;
                }
              } catch (e) {}
              const mResp = await fetch('/backend-api/models', { credentials: 'include', headers });
              if (mResp.ok) {
                const data = await mResp.json();
                const list = Array.isArray(data?.models) ? data.models : [];
                const defaultSlug = String(data?.default_model_slug || '');
                window.__apCgModels.default_slug = defaultSlug;
                window.__apCgModels.models = list
                  .filter((m) => m && m.slug && !m.is_hidden)
                  .map((m) => ({
                    id: String(m.slug),
                    name: String(m.title || m.slug),
                    org: 'OpenAI',
                    description: String(m.description || ''),
                    tags: Array.isArray(m.tags) ? m.tags : [],
                    active: Boolean(defaultSlug && String(m.slug) === defaultSlug),
                  }));
              }
            } catch (e) {}
            window.__apCgModels.done = true;
          })();
          return 'ok';
        })()"""
        self.eval_js(kick, timeout=3.0)
        deadline = time.time() + 4.0
        data = None
        while time.time() < deadline:
            time.sleep(0.20)
            polled = self.eval_js("JSON.stringify(window.__apCgModels || null)", timeout=2.0)
            if isinstance(polled, dict) and polled.get("done"):
                data = polled
                break
        if not isinstance(data, dict):
            return None
        raw_models = data.get("models") or []
        models = []
        seen = set()
        for m in raw_models:
            slug = str(m.get("id") or "").strip()
            name = str(m.get("name") or slug).strip()
            if not slug or slug in seen:
                continue
            seen.add(slug)
            caps = ["Text", "Code"]
            low = f"{slug} {name}".lower()
            if any(k in low for k in ("think", "reason", "o1", "o3", "o4")):
                caps.append("Reasoning")
            if "mini" in low or "4o" in low or "instant" in low:
                caps.append("Fast")
            models.append({
                "id": slug,
                "name": name,
                "org": "OpenAI",
                "description": str(m.get("description") or "Available in your chatgpt.com account"),
                "capabilities": caps,
                "active": bool(m.get("active")),
            })
        active_label = str(data.get("active") or "").strip()
        if not models and active_label:
            models.append({
                "id": "auto",
                "name": active_label,
                "org": "OpenAI",
                "description": "Currently active in chatgpt.com",
                "capabilities": ["Text", "Code"],
                "active": True,
            })
        if not models:
            return None
        active_id = next((m["id"] for m in models if m.get("active")), "") or models[0]["id"]
        return {
            "live": True,
            "active_model": active_id,
            "models": models,
        }

    def _apply_model_selection(self, model_id: str) -> Optional[bool]:
        want = (model_id or "").strip()
        if not want or want in ("auto", "default"):
            return True
        if "think" in want.lower():
            script = r"""(() => {
              const btn = Array.from(document.querySelectorAll('button')).find((b) => {
                const t = (b.innerText || b.textContent || '').trim().toLowerCase();
                return t === 'think' || t === 'reason';
              });
              if (btn && btn.getAttribute('aria-pressed') !== 'true') {
                btn.click();
                return JSON.stringify({ ok: true, toggled: 'think' });
              }
              return JSON.stringify({ ok: true, toggled: '' });
            })()"""
            self.eval_js(script, timeout=3.0)
            time.sleep(0.15)
            if want.lower() in ("think", "reason"):
                return True
        open_script = f"""(() => {{
          const want = {json.dumps(want.lower())};
          const btn = document.querySelector('[data-testid="model-switcher-dropdown-button"]');
          if (!btn) return JSON.stringify({{ ok: false }});
          const cur = (btn.innerText || btn.textContent || '').toLowerCase();
          if (cur.includes(want)) return JSON.stringify({{ ok: true, already: true }});
          btn.dispatchEvent(new PointerEvent('pointerdown', {{ bubbles: true, cancelable: true, button: 0, pointerType: 'mouse' }}));
          btn.click();
          return JSON.stringify({{ ok: true, opened: true }});
        }})()"""
        step1 = self.eval_js(open_script, timeout=3.0)
        if not isinstance(step1, dict) or not step1.get("ok"):
            return False
        if step1.get("already"):
            return True
        time.sleep(0.3)
        click_script = f"""(() => {{
          const want = {json.dumps(want.lower())};
          const items = Array.from(document.querySelectorAll('[role="menuitem"], [role="option"]'));
          for (const item of items) {{
            const txt = (item.innerText || item.textContent || '').toLowerCase();
            const testid = (item.getAttribute('data-testid') || '').toLowerCase();
            if (testid.includes(want) || txt.includes(want)) {{
              item.click();
              return JSON.stringify({{ ok: true, clicked: txt.slice(0, 60) }});
            }}
          }}
          document.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Escape', code: 'Escape', bubbles: true }}));
          return JSON.stringify({{ ok: false }});
        }})()"""
        res = self.eval_js(click_script, timeout=3.0)
        if isinstance(res, dict) and res.get("clicked"):
            time.sleep(0.2)
            return True
        return False

    def _parse_observed_sse_chunk(self, raw_chunk: str, state: dict, *, flush: bool = False) -> list[dict]:
        """Parse ChatGPT's live SSE response stream (observation-only)."""
        events: list[dict] = []
        buf = str(state.get("buf") or "") + (raw_chunk or "")
        if flush and buf.strip():
            buf += "\n"
        last_full = str(state.get("last_full") or "")
        cur_p = str(state.get("p") or "")
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

            conv_id = pkt.get("conversation_id")
            if isinstance(conv_id, str) and conv_id and not state.get("conv_emitted"):
                state["conv_emitted"] = True
                events.append({"type": "meta_update", "external_url": f"https://chatgpt.com/c/{conv_id}"})

            # Format A: ChatGPT v1 delta-encoding patches
            if isinstance(pkt.get("p"), str):
                cur_p = pkt["p"]
                state["p"] = cur_p
            op = str(pkt.get("o") or "").lower()
            val = pkt.get("v")
            is_content_path = (
                "/message/content/parts" in cur_p
                or "thoughts" in cur_p
                or "reasoning" in cur_p
            )
            if isinstance(val, str) and val and is_content_path and op in ("", "append"):
                if "thoughts" in cur_p or "reasoning" in cur_p:
                    events.append({"type": "reasoning", "text": val})
                else:
                    last_full += val
                    events.append({"type": "text", "text": val})
                continue
            if isinstance(val, list):
                for sub in val:
                    if not isinstance(sub, dict):
                        continue
                    sub_op = str(sub.get("o") or "").lower()
                    if sub_op in ("remove", "delete"):
                        continue
                    sp = str(sub.get("p") or cur_p)
                    sv = sub.get("v")
                    if isinstance(sv, str) and sv and ("/message/content/parts" in sp or "thoughts" in sp or "reasoning" in sp):
                        if "thoughts" in sp or "reasoning" in sp:
                            events.append({"type": "reasoning", "text": sv})
                        else:
                            last_full += sv
                            events.append({"type": "text", "text": sv})
                continue

            # Format B: Full message snapshots ({"message": {"author": {"role": "assistant"}, "content": {"parts": [...]}}})
            msg = pkt.get("message")
            if isinstance(msg, dict):
                role = ((msg.get("author") or {}).get("role") or "").lower()
                if role == "assistant":
                    content = msg.get("content") or {}
                    ctype = str(content.get("content_type") or "")
                    parts = content.get("parts")
                    if ctype == "text" and isinstance(parts, list):
                        full = "".join(str(p) for p in parts if isinstance(p, str))
                        if full and full.startswith(last_full) and len(full) > len(last_full):
                            delta = full[len(last_full):]
                            last_full = full
                            events.append({"type": "text", "text": delta})
                    elif ctype == "thoughts" and isinstance(content.get("thoughts"), list):
                        thought_text = "\n".join(
                            str(t.get("content") or "")
                            for t in content["thoughts"]
                            if isinstance(t, dict) and t.get("content")
                        ).strip()
                        prev_t = str(state.get("last_thought") or "")
                        if thought_text and thought_text.startswith(prev_t) and len(thought_text) > len(prev_t):
                            events.append({"type": "reasoning", "text": thought_text[len(prev_t):]})
                            state["last_thought"] = thought_text
        state["buf"] = buf
        state["last_full"] = last_full
        return events

    def _poll_conversation_api(self, href: str) -> Optional[dict]:
        """Read ChatGPT's signed-in /backend-api/conversation/<id> when hidden DOM lags."""
        m = re.search(r"/c/([0-9a-fA-F-]{16,})", href or "")
        if not m:
            return None
        conv_id = m.group(1)
        sess = self.fetch_json("/api/auth/session", timeout=3.0)
        token = ""
        if sess.get("ok") and isinstance(sess.get("data"), dict):
            token = str(sess["data"].get("accessToken") or "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        res = self.fetch_json(f"/backend-api/conversation/{conv_id}", headers=headers, timeout=4.0)
        if not res.get("ok") or not isinstance(res.get("data"), dict):
            return None
        mapping = res["data"].get("mapping")
        if not isinstance(mapping, dict):
            return None
        assistants = []
        for node in mapping.values():
            if not isinstance(node, dict):
                continue
            msg = node.get("message")
            if not isinstance(msg, dict):
                continue
            role = ((msg.get("author") or {}).get("role") or "").lower()
            if role != "assistant":
                continue
            content = msg.get("content") or {}
            parts = content.get("parts") or []
            txt = "".join(str(p) for p in parts if isinstance(p, str)).strip()
            if txt or msg.get("status") == "in_progress":
                assistants.append((float(msg.get("create_time") or 0.0), msg, txt))
        if not assistants:
            return {"count": 0, "text": "", "reasoning": "", "streaming": False}
        assistants.sort(key=lambda item: item[0])
        _, last_msg, last_txt = assistants[-1]
        streaming = str(last_msg.get("status") or "").lower() in ("in_progress", "pending")
        return {
            "count": len(assistants),
            "text": last_txt,
            "reasoning": "",
            "streaming": streaming,
        }
