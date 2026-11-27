"""Freebuff web provider — freebuff.com/chat (pure WebKitGTK signed-in window, no CLI / no API key)."""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from urllib.parse import urlparse

from arena import diag
from arena.providers.web_engine import WebChatEngine, _looks_like_human_check

_MODEL_BUTTON_SELECTOR = (
    'button[aria-label*="model" i], [data-testid*="model"] button, [data-testid*="model"], '
    '[class*="model-selector"], [class*="model-picker"], [class*="model-trigger"]'
)

# freebuff.com's chat picks a reasoning tier, not a model. Which model each tier
# runs comes from the Freebuff app's own code (v0.2.38) for a normal account.
_STANDARD_TIERS = ("Instant", "Balanced", "Powerful")
_TIER_DETAILS = {
    "instant": "DeepSeek V4.1 Flash · low reasoning",
    "efficient": "A free fast model (DeepSeek V4.1 Flash · low reasoning when none)",
    "balanced": "Chat mode: Freebuff's chat model (m-61c26317cc). Otherwise DeepSeek V4.1 Flash · high reasoning",
    "powerful": "DeepSeek V4.1 Flash · max reasoning (Muse Spark 1.3 on limited access with 100+ credits)",
    "default": "This model · default reasoning",
    "max": "This model · max reasoning",
}
_TIER_CAPS = {
    "instant": ["Text", "Code", "Fast"],
    "efficient": ["Text", "Code", "Fast"],
    "balanced": ["Text", "Code", "Reasoning"],
    "powerful": ["Text", "Code", "Reasoning"],
    "max": ["Text", "Code", "Reasoning"],
}


def _tier_key(label: str) -> str:
    """The tier a label names ("Powerful Max" -> "powerful"), or "" for a model name."""
    words = (label or "").strip().lower().split()
    return words[0] if words and words[0] in _TIER_DETAILS else ""


# Finds menu entries by their tier name instead of guessing element types: each
# entry is the outermost element whose first text line is a tier name.
_TIER_SCAN_JS = r"""(() => { /* __freebuffTierScan */
  const tierRe = /^(instant|efficient|balanced|powerful|default|max)\b/i;
  const trigger = document.querySelector(%s);
  const best = new Map();
  for (const el of document.querySelectorAll('[role="menuitem"],[role="menuitemradio"],[role="option"],[data-radix-collection-item],li,button,[role="button"],div')) {
    if (trigger && (el === trigger || el.contains(trigger) || trigger.contains(el))) continue;
    const lines = (el.innerText || el.textContent || '').split('\n').map((t) => t.trim()).filter(Boolean);
    if (!lines.length || lines.length > 4 || !tierRe.test(lines[0])) continue;
    if (lines.slice(1).some((t) => tierRe.test(t))) continue;  // a container of several entries
    const key = lines[0].toLowerCase();
    const prev = best.get(key);
    if (!prev || lines.length > prev.lines) best.set(key, { name: lines[0], desc: lines.slice(1).join(' - ').slice(0, 100), lines: lines.length });
  }
  return JSON.stringify({ items: Array.from(best.values()).map(({ name, desc }) => ({ name, desc })) });
})()""" % json.dumps(_MODEL_BUTTON_SELECTOR)

_TIER_CLICK_JS = r"""(() => { /* __freebuffTierClick */
  const want = __WANT__;
  const wantKey = want.split(/\s+/)[0];
  const trigger = document.querySelector(%s);
  let hit = null;
  let keyHit = null;
  for (const el of document.querySelectorAll('[role="menuitem"],[role="menuitemradio"],[role="option"],[data-radix-collection-item],li,button,[role="button"],div')) {
    if (trigger && (el === trigger || el.contains(trigger) || trigger.contains(el))) continue;
    const first = ((el.innerText || el.textContent || '').split('\n').map((t) => t.trim()).filter(Boolean)[0] || '').toLowerCase();
    // Keep the innermost match: it is the clickable entry. A label that only
    // shares the tier word ("Powerful" for "Powerful Max") is the fallback.
    if (first === want) hit = el;
    else if (first.split(/\s+/)[0] === wantKey) keyHit = el;
  }
  hit = hit || keyHit;
  if (!hit) return JSON.stringify({ ok: false });
  hit.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, button: 0, pointerType: 'mouse' }));
  hit.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true, button: 0, pointerType: 'mouse' }));
  hit.click();
  return JSON.stringify({ ok: true });
})()""" % json.dumps(_MODEL_BUTTON_SELECTOR)


class FreebuffProvider(WebChatEngine):
    id = "freebuff"
    name = "Freebuff"
    short_name = "Freebuff"
    badge = "F"
    badge_class = "is-freebuff"
    description = "freebuff.com/chat in a signed-in WebKitGTK window (Freebuff Chat)"
    # Freebuff Web (/app) is the full workspace with code mode and the model
    # picker. /chat is a bare chat that sends signed-in users to the home page.
    origin = "https://freebuff.com/app"
    window_title = "Freebuff - Arena Desktop"
    cookie_urls = (
        "https://freebuff.com/",
        "https://www.freebuff.com/",
        "https://freebuff.com/chat",
        "https://freebuff.com/app",
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
        """Keep Freebuff tabs on /app or /chat (and their /<id> pages), not landing routes."""
        if not href:
            return False
        path = (urlparse(href).path or "/").rstrip("/") or "/"
        if any(path == base or path.startswith(base + "/") for base in ("/app", "/chat")):
            return False
        return True

    def _read_session_user(self) -> dict | None:
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

    def _shows_signed_out_page(self) -> bool:
        """Guests get the normal /chat page, so the URL alone cannot tell.

        Signed out means: a sign-in URL, or no session user while the guest
        "Sign in" button is shown (both, so one flaky check cannot hide a list).
        """
        if self._is_login_url(self.page_url()):
            return True
        has_user = self._read_session_user() is not None
        guest_cta = self._has_guest_signin_button()
        diag.note(f"freebuff sign-in check: session_user={'yes' if has_user else 'no'} guest_button={'yes' if guest_cta else 'no'}")
        return not has_user and guest_cta

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
              const btn = document.querySelector(%s);
              const label = btn ? (btn.innerText || btn.textContent || '').replace(/\s+/g, ' ').trim() : '';
              return JSON.stringify({ label });
            })()""" % json.dumps(_MODEL_BUTTON_SELECTOR),
            timeout=2.5,
        )
        return str(dom.get("label") or "").strip() if isinstance(dom, dict) else ""

    def _open_model_menu(self) -> bool:
        """Open the page's model menu: pointerdown for Radix-style menus, then click."""
        res = self.eval_js(
            """(() => {
              const btn = document.querySelector(%s);
              if (!btn) return JSON.stringify({ ok: false });
              btn.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, button: 0, pointerType: 'mouse' }));
              if (btn.getAttribute('aria-expanded') !== 'true') btn.click();
              return JSON.stringify({ ok: true });
            })()""" % json.dumps(_MODEL_BUTTON_SELECTOR),
            timeout=2.5,
        )
        return isinstance(res, dict) and bool(res.get("ok"))

    def _close_model_menu(self) -> None:
        self.eval_js(
            "(() => { document.dispatchEvent(new KeyboardEvent('keydown', "
            "{ key: 'Escape', code: 'Escape', bubbles: true })); return '1'; })()",
            timeout=1.5,
        )

    def _scan_tier_items(self) -> list[dict]:
        """Tier entries ({name, desc}) in the open model menu, found by their names."""
        res = self.eval_js(_TIER_SCAN_JS, timeout=3.0)
        items = res.get("items") if isinstance(res, dict) else None
        return [i for i in items or [] if isinstance(i, dict) and str(i.get("name") or "").strip()]

    def _probe_live_models(self) -> dict | None:
        """List the tiers freebuff.com's chat offers (Instant / Balanced / Powerful).

        The web chat has no model API (every /api/*models path answers 404) and
        its menu offers reasoning tiers, not model names, so the tiers are the list.
        """
        active_lbl = self._read_active_model_label()
        items: list[dict] = []
        if active_lbl and self._open_model_menu():
            time.sleep(0.32)
            items = self._scan_tier_items()
            self._close_model_menu()
        active_key = _tier_key(active_lbl)
        # Unreadable menu: offer the standard tiers, keeping the button's own
        # label ("Powerful Max") for the tier that is selected.
        tiers = items or (
            [{"name": active_lbl if _tier_key(n) == active_key else n, "desc": ""} for n in _STANDARD_TIERS]
            if active_key
            else []
        )
        # Host and first path segment only: later segments hold chat ids.
        path = "/".join((self.page_url() or "").split("?", 1)[0].split("#", 1)[0].split("/")[:4])
        diag.note(
            f"freebuff models probe: page={path or '-'} active={active_lbl or '-'} "
            f"menu_tiers={[i['name'] for i in items]} total={len(tiers) or (1 if active_lbl else 0)}"
        )
        if not tiers:
            if not active_lbl:
                return None
            # A named model on the button (not a tier): offer just that one.
            tiers = [{"name": active_lbl, "desc": "Currently active on freebuff.com/chat"}]
        models = []
        seen: set[str] = set()
        for tier in tiers:
            name = " ".join(str(tier.get("name") or "").split())
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            models.append({
                "id": name,
                "name": name,
                "org": "Freebuff",
                "description": (
                    str(tier.get("desc") or "").strip()
                    or _TIER_DETAILS.get(_tier_key(name), "Available on freebuff.com/chat")
                )[:120],
                "capabilities": _TIER_CAPS.get(_tier_key(name), ["Text", "Code"]),
                "active": False,
            })
        # The button's label marks the active entry: exact text first, else the same tier.
        active_id = next((m["id"] for m in models if active_lbl and m["id"].lower() == active_lbl.lower()), "")
        if not active_id and active_key:
            active_id = next((m["id"] for m in models if _tier_key(m["id"]) == active_key), "")
        models = [{**m, "active": m["id"] == active_id} for m in models]
        return {"live": True, "active_model": active_id, "models": models}

    def _apply_model_selection(self, model_id: str) -> bool:
        """Pick a tier (or model) in the page's menu by its exact name."""
        want = " ".join((model_id or "").split())
        if not want or want.lower() == "default":
            return True
        if self._read_active_model_label().lower() == want.lower():
            return True
        if not self._open_model_menu():
            return False
        time.sleep(0.32)
        res = self.eval_js(_TIER_CLICK_JS.replace("__WANT__", json.dumps(want.lower())), timeout=3.0)
        if isinstance(res, dict) and res.get("ok"):
            time.sleep(0.2)
            return True
        self._close_model_menu()
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
