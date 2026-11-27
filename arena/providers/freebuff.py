"""Freebuff web provider — freebuff.com/chat (pure WebKitGTK signed-in window, no CLI / no API key)."""
from __future__ import annotations

import json
import re
import time
from typing import Iterator, Optional
from urllib.parse import urlparse

from arena.providers.web_engine import WebChatEngine, _looks_like_human_check


class FreebuffProvider(WebChatEngine):
    id = "freebuff"
    name = "Freebuff"
    short_name = "Freebuff"
    badge = "F"
    badge_class = "is-freebuff"
    description = "freebuff.com/chat in a signed-in WebKitGTK window (Freebuff Chat)"
    origin = "https://freebuff.com/chat"
    window_title = "Freebuff - Arena Desktop"
    cookie_urls = (
        "https://freebuff.com/",
        "https://www.freebuff.com/",
        "https://freebuff.com/chat",
    )
    auth_cookie_names = (
        "next-auth.session-token",
        "__Secure-next-auth.session-token",
        "authjs.session-token",
        "__Secure-authjs.session-token",
        "session",
        "freebuff_session",
    )
    login_url_hints = ("/login", "/signin", "/sign-in", "/auth/signin", "/api/auth")
    conversation_url_hint = "/chat/"
    observed_fetch_patterns = (
        "/api/chat",
        "/api/v1/chat",
        "/api/messages",
        "/api/threads",
        "/chat/completions",
        "/api/agent",
        "/api/run",
    )
    # Clicking button.send-key once data-empty="false" submits cleanly on freebuff.com/chat.
    prefer_enter_submit = False

    composer_selectors = (
        "div.composer-input[data-composer-input]",
        'div.composer-input[contenteditable="true"]',
        'div[role="textbox"][aria-label*="Freebuff" i]',
        '[contenteditable="true"][role="textbox"]',
        "textarea",
    )
    send_selectors = (
        "button.send-key:not([disabled])",
        'button[aria-label="Send message"]',
        'button[aria-label*="Send" i]',
        'button[type="submit"]',
    )
    stop_selectors = (
        "button.stop-key",
        'button[aria-label*="Stop" i]',
        'button[data-tooltip*="Stop" i]',
        '[class*="stop-btn"]',
        '[class*="stop-button"]',
    )
    assistant_selectors = (
        '[data-role="assistant"] [class*="markdown"]',
        '[data-role="assistant"] .prose',
        '[data-message-author-role="assistant"]',
        '[class*="assistant-message"] [class*="markdown"]',
        '[class*="assistant"] [class*="markdown"]',
        '[class*="assistant"] .prose',
        '[data-role="assistant"]',
        'main [class*="assistant"]:not([class*="user"])',
        'main [class*="markdown"]:not([class*="user"])',
        'main .prose:not([class*="user"])',
        'article [class*="markdown"]',
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
            "name": "Freebuff (Account default)",
            "org": "Freebuff",
            "description": "Open Freebuff window & refresh to retrieve live models from freebuff.com/chat",
            "capabilities": ["Text", "Code", "Reasoning"],
        },
    )

    def _is_non_chat_url(self, href: str) -> bool:
        """Keep Freebuff tabs on /chat or /chat/<id> rather than marketing/landing routes."""
        if not href:
            return False
        path = (urlparse(href).path or "/").rstrip("/") or "/"
        if path == "/chat" or path.startswith("/chat/"):
            return False
        return True

    def _read_session_user(self) -> Optional[dict]:
        """Query NextAuth /api/auth/session inside the WebKitGTK page."""
        res = self.fetch_json("/api/auth/session", timeout=3.0)
        if not (isinstance(res, dict) and res.get("ok")):
            return None
        data = res.get("data")
        if not isinstance(data, dict):
            return None
        user = data.get("user")
        if isinstance(user, dict) and (user.get("email") or user.get("name") or user.get("id")):
            return user
        if data.get("userId") or data.get("id") or data.get("email"):
            return data
        return None

    def _has_guest_signin_button(self) -> bool:
        """Return True when the top-right guest 'Sign in' / 'Sign up' CTA is rendered."""
        dom = self.eval_js(
            r"""(() => {
              const btns = Array.from(document.querySelectorAll('nav button, header button, button.btn'));
              const guest = btns.some((b) => {
                const t = (b.innerText || b.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
                return t === 'sign in' || t === 'sign up for free' || t === 'log in';
              });
              return JSON.stringify({ guest });
            })()""",
            timeout=2.0,
        )
        return bool(dom.get("guest")) if isinstance(dom, dict) else False

    def verify(self) -> dict:
        """Verify Freebuff authentication via /api/auth/session + auth cookies.

        Unlike login-gated providers, freebuff.com/chat renders its composer even for
        signed-out guests, so composer presence alone must not mark the account signed in.
        """
        if self.window is None:
            return self.status()
        probe = self.probe_page()
        href = str(probe.get("href") or "")
        title = str(probe.get("title") or "")
        body = str(probe.get("body_sample") or "")
        if _looks_like_human_check(title, body):
            self._signed_in_cache = False
            self._signed_in_at = time.time()
            return {
                "available": self.has_desktop_runtime(),
                "signed_in": False,
                "open": self.visible,
                "window": True,
                "human_check": True,
                "detail": f"{self.name} wants a manual human check in its window.",
            }

        user = self._read_session_user()
        cookie_names = self.read_cookie_names()
        effective_auth_cookies = set(self.auth_cookie_names or ())
        cookie_ok = bool(effective_auth_cookies and (cookie_names & effective_auth_cookies))
        is_login = self._is_login_url(href)
        guest_cta = self._has_guest_signin_button()
        signed = bool((user is not None or (cookie_ok and not guest_cta)) and not is_login)

        self._signed_in_cache = signed
        self._signed_in_at = time.time()
        label = ""
        if isinstance(user, dict):
            label = str(user.get("email") or user.get("name") or "").strip()
        detail = (
            f"Signed in (freebuff.com{f' · {label}' if label else ''})"
            if signed
            else "Open the Freebuff window and click Sign in to use your account."
        )
        return {
            "available": self.has_desktop_runtime(),
            "signed_in": signed,
            "open": self.visible,
            "window": True,
            "detail": detail,
        }

    def _stream_turn_locked(
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
        """Ensure the user's Freebuff account is signed in before submitting a chat turn."""
        if not self.ensure_window(hidden=True):
            yield {
                "type": "error",
                "code": "no_window",
                "message": f"The {self.name} WebView window is unavailable in this environment.",
            }
            return
        if not self._signed_in_cache:
            current = self.page_url()
            if not current or current == "about:blank" or "freebuff.com" not in current:
                self.load_url(conversation_url or self.origin)
                self._wait_for_origin("freebuff.com", timeout=10.0)
            v = self.verify()
            if v.get("human_check"):
                self.show()
                yield {
                    "type": "error",
                    "code": "human_check",
                    "message": (
                        f"{self.name} asked for a human check. Complete it in the {self.name} "
                        "window, then send your message again."
                    ),
                    "open_window": True,
                }
                return
            if not v.get("signed_in"):
                self.show()
                yield {
                    "type": "error",
                    "code": "auth",
                    "message": (
                        "Sign in to your Freebuff account in the window that just opened, "
                        "then send your message again."
                    ),
                    "open_window": True,
                }
                return

        yield from super()._stream_turn_locked(
            chat_id,
            message,
            history,
            memory_block=memory_block,
            workspace=workspace,
            conversation_url=conversation_url,
            model=model,
            permission_mode=permission_mode,
            is_followup_round=is_followup_round,
            stop_event=stop_event,
        )

    def _read_active_model_label(self) -> str:
        dom = self.eval_js(
            r"""(() => {
              const btn = document.querySelector(
                'button[aria-label*="model" i], [data-testid*="model"] button, [data-testid*="model"], ' +
                '[class*="model-selector"], [class*="model-picker"], [class*="model-trigger"]'
              );
              const label = btn ? (btn.innerText || btn.textContent || '').replace(/\s+/g, ' ').trim() : '';
              return JSON.stringify({ label });
            })()""",
            timeout=2.5,
        )
        return str(dom.get("label") or "").strip() if isinstance(dom, dict) else ""

    def _probe_live_models(self) -> Optional[dict]:
        """Query live Freebuff model catalog endpoints or the signed-in DOM model picker."""
        models: list[dict] = []
        seen: set[str] = set()
        active_lbl = self._read_active_model_label()
        active_low = active_lbl.lower()

        def _add(mid: str, name: str, desc: str, org: str, active: bool) -> None:
            clean_id = " ".join(str(mid or name or "").split()).strip()
            clean_name = " ".join(str(name or clean_id).split()).strip()
            if not clean_id or len(clean_name) > 64:
                return
            if re.search(r"\b(sign in|sign up|log in|log out|pricing|upgrade|download)\b", clean_name, re.I):
                return
            key = clean_id.lower()
            if key in seen:
                return
            seen.add(key)
            low = f"{clean_id} {clean_name}".lower()
            caps = ["Text", "Code"]
            if any(w in low for w in ("reason", "think", "pro", "r1", "claude", "opus", "sonnet", "gpt", "deepseek", "solar")):
                caps.append("Reasoning")
            if any(w in low for w in ("flash", "mini", "fast", "lightning", "haiku")):
                caps.append("Fast")
            is_active = active or bool(active_low and active_low in (clean_name.lower(), key))
            models.append({
                "id": clean_id,
                "name": clean_name,
                "org": org or "Freebuff",
                "description": (desc or "Available on freebuff.com/chat")[:120],
                "capabilities": caps,
                "active": is_active,
            })

        for endpoint in ("/api/v1/models", "/api/models", "/api/chat/models", "/api/v1/freebuff/models"):
            res = self.fetch_json(endpoint, timeout=3.5)
            data = res.get("data")
            raw_list = None
            if isinstance(data, dict):
                raw_list = data.get("models") or data.get("data") or data.get("items")
            elif isinstance(data, list):
                raw_list = data
            if not (res.get("ok") and isinstance(raw_list, list) and raw_list):
                continue
            for item in raw_list:
                if isinstance(item, str):
                    _add(item, item, "", "Freebuff", False)
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get("hidden") is True or item.get("disabled") is True:
                    continue
                mid = str(item.get("id") or item.get("model") or item.get("key") or item.get("slug") or "").strip()
                name = str(item.get("name") or item.get("displayName") or item.get("label") or mid).strip()
                org = str(item.get("provider") or item.get("org") or "Freebuff").strip()
                desc = str(item.get("description") or "").strip()
                _add(mid, name, desc, org, bool(item.get("default") or item.get("active")))
            if models:
                break

        if not models and active_lbl:
            _add(active_lbl, active_lbl, "Currently active on freebuff.com/chat", "Freebuff", True)
            opened = self.eval_js(
                """(() => {
                  const btn = document.querySelector(
                    'button[aria-label*="model" i], [data-testid*="model"] button, [data-testid*="model"], ' +
                    '[class*="model-selector"], [class*="model-picker"], [class*="model-trigger"]'
                  );
                  if (!btn) return JSON.stringify({ ok: false });
                  btn.click();
                  return JSON.stringify({ ok: true });
                })()""",
                timeout=2.5,
            )
            if isinstance(opened, dict) and opened.get("ok"):
                time.sleep(0.32)
                scraped = self.eval_js(
                    r"""(() => {
                      const out = [];
                      const nodes = Array.from(document.querySelectorAll(
                        '[role="menuitem"], [role="option"], [role="menuitemradio"], [data-radix-collection-item]'
                      ));
                      for (const el of nodes) {
                        const lines = (el.innerText || el.textContent || '').split('\n').map((s) => s.trim()).filter(Boolean);
                        if (lines.length && lines[0].length <= 56) {
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
                        _add(str(item.get("name") or ""), str(item.get("name") or ""), str(item.get("desc") or ""), "Freebuff", False)

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
        want = (model_id or "").strip()
        if not want or want.lower() == "default":
            return True
        cur = self._read_active_model_label().lower()
        if cur and (want.lower() == cur or want.lower() in cur):
            return True
        opened = self.eval_js(
            """(() => {
              const btn = document.querySelector(
                'button[aria-label*="model" i], [data-testid*="model"] button, [data-testid*="model"], ' +
                '[class*="model-selector"], [class*="model-picker"], [class*="model-trigger"]'
              );
              if (!btn) return JSON.stringify({ ok: false });
              btn.click();
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
              const nodes = Array.from(document.querySelectorAll(
                '[role="menuitem"], [role="option"], [role="menuitemradio"], [data-radix-collection-item]'
              ));
              for (const item of nodes) {{
                const txt = (item.innerText || item.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                if (txt === wantFull || txt.includes(wantFull) || (toks.length && toks.every((t) => txt.includes(t)))) {{
                  item.click();
                  return JSON.stringify({{ ok: true, clicked: txt.slice(0, 60) }});
                }}
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
        """Parse observed SSE / NDJSON / Vercel AI SDK data streams from freebuff.com/chat."""
        events: list[dict] = []
        buf = str(state.get("buf") or "") + (raw_chunk or "")
        if flush and buf.strip():
            buf += "\n"
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            s = line.strip()
            if not s or s.startswith(":") or s.startswith("event:"):
                continue

            # 1. Vercel AI SDK data stream protocol: 0:"text chunk" or g:"reasoning chunk"
            if len(s) >= 3 and s[1] == ":" and s[0] in ("0", "g"):
                try:
                    val = json.loads(s[2:])
                except Exception:
                    val = None
                if isinstance(val, str) and val:
                    events.append({"type": "text" if s[0] == "0" else "reasoning", "text": val})
                continue

            if s.startswith("data:"):
                s = s[5:].strip()
            if not s or s == "[DONE]":
                continue
            try:
                pkt = json.loads(s)
            except Exception:
                continue
            if not isinstance(pkt, dict):
                continue

            ptype = str(pkt.get("type") or "").lower()
            # 2. Vercel AI SDK UI stream / Codebuff event stream
            if ptype in ("text-delta", "text_delta", "text"):
                txt = pkt.get("delta") if isinstance(pkt.get("delta"), str) else pkt.get("textDelta") or pkt.get("text")
                if isinstance(txt, str) and txt:
                    events.append({"type": "text", "text": txt})
                continue
            if ptype in ("reasoning-delta", "reasoning_delta", "reasoning", "thinking"):
                txt = pkt.get("delta") if isinstance(pkt.get("delta"), str) else pkt.get("reasoningDelta") or pkt.get("text")
                if isinstance(txt, str) and txt:
                    events.append({"type": "reasoning", "text": txt})
                continue
            # 3. Anthropic-style content_block_delta
            if ptype == "content_block_delta" and isinstance(pkt.get("delta"), dict):
                d = pkt["delta"]
                if isinstance(d.get("thinking"), str) and d["thinking"]:
                    events.append({"type": "reasoning", "text": d["thinking"]})
                if isinstance(d.get("text"), str) and d["text"]:
                    events.append({"type": "text", "text": d["text"]})
                continue
            # 4. OpenAI-compatible choices[0].delta
            choices = pkt.get("choices")
            if isinstance(choices, list) and choices:
                delta = (choices[0] or {}).get("delta") or {}
                if isinstance(delta, dict):
                    rc = delta.get("reasoning_content") or delta.get("reasoning")
                    if isinstance(rc, str) and rc:
                        events.append({"type": "reasoning", "text": rc})
                    c = delta.get("content")
                    if isinstance(c, str) and c:
                        events.append({"type": "text", "text": c})
                continue
            # 5. Generic nested data / cumulative text fallback
            inner = pkt.get("data") if isinstance(pkt.get("data"), dict) else pkt
            if isinstance(inner, dict):
                if isinstance(inner.get("delta"), str) and inner["delta"]:
                    events.append({"type": "text", "text": inner["delta"]})
                elif isinstance(inner.get("text"), str) and inner["text"] and ptype not in ("error", "finish", "done", "status"):
                    events.append({"type": "text", "text": inner["text"]})
        state["buf"] = "" if flush else buf
        return events
