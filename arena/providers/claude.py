"""Claude web provider (pure WebKitGTK signed-in window, no CLI / no API key).

Uses claude.ai's same-origin signed-in REST + SSE API directly from inside the
hidden WebKitGTK window (the exact same pattern as ``arena/engine.py``), so
Claude streams silently in the background without ever needing its window
visible (except for manual sign-in or manual human verification).
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Iterator, Optional

from arena import diag
from arena.providers.web_engine import WebChatEngine


def _extract_claude_chat_uuid(url: str) -> str:
    m = re.search(r"/chat/([0-9a-fA-F-]{16,})", url or "")
    return m.group(1) if m else ""


def _humanize_claude_slug(slug: str) -> str:
    s = (slug or "").strip()
    if not s:
        return ""
    if not s.lower().startswith("claude-"):
        return s
    parts = s.split("-")
    # e.g. claude-sonnet-4-6-20260514 -> Claude Sonnet 4.6
    out = ["Claude"]
    nums = []
    for tok in parts[1:]:
        if len(tok) == 8 and tok.isdigit():
            continue
        if tok.isdigit():
            nums.append(tok)
        else:
            if nums:
                out.append(".".join(nums))
                nums = []
            out.append(tok.capitalize())
    if nums:
        out.append(".".join(nums))
    return " ".join(out)


class ClaudeProvider(WebChatEngine):
    id = "claude"
    name = "Claude"
    short_name = "Claude"
    badge = "\u25c8"
    badge_class = "is-claude"
    description = "claude.ai in a signed-in WebKitGTK window"
    origin = "https://claude.ai/new"
    window_title = "Claude - Arena Desktop"
    cookie_urls = ("https://claude.ai/",)
    auth_cookie_names = ("sessionKey", "lastActiveOrg")
    login_url_hints = ("/login", "/signup")
    conversation_url_hint = "/chat/"
    api_nav_not_needed = True

    # Never include bare "textarea" here: claude.ai has hidden textarea nodes
    # that are NOT the ProseMirror composer.
    composer_selectors = (
        'div.ProseMirror[contenteditable="true"]',
        '[contenteditable="true"][role="textbox"]',
        'fieldset [contenteditable="true"]',
    )
    send_selectors = (
        'button[aria-label="Send message"]',
        'button[aria-label="Send Message"]',
        'button[data-testid="send-button"]',
        'button[aria-label*="Send"]',
        'button[type="submit"]',
    )
    stop_selectors = (
        'button[aria-label="Stop response"]',
        'button[aria-label="Stop Response"]',
        'button[aria-label*="Stop generating"]',
        '[data-is-streaming="true"]',
    )
    assistant_selectors = (
        '[data-is-streaming] .font-claude-response',
        '[data-is-streaming] .standard-markdown',
        '[data-is-streaming] .progressive-markdown',
        '[data-is-streaming] [class*="markdown"]',
        ".font-claude-response",
        ".font-claude-message",
        '[data-testid="assistant-message"]',
        '[data-test-render-count] .standard-markdown',
        '[data-test-render-count] .progressive-markdown',
        "[data-is-streaming]",
    )
    reasoning_selectors = (
        '[data-testid="thinking-block"]',
        '[class*="thinking"]',
    )
    default_models = (
        {
            "id": "default",
            "name": "Claude (Account default)",
            "org": "Anthropic",
            "description": "Open Claude window & refresh to retrieve live account models",
            "capabilities": ["Text", "Code"],
        },
    )

    def __init__(self) -> None:
        super().__init__()
        self._org_id: str = ""
        self._model_label_to_slug: dict[str, str] = {}

    def _get_org_info(self) -> tuple[str, dict]:
        """Resolve the user's real Claude organization UUID from /api/organizations."""
        if self._org_id:
            return self._org_id, {}
        res = self.fetch_json("/api/organizations", timeout=6.0)
        data = res.get("data")
        if res.get("ok") and isinstance(data, list) and data:
            # Prefer an org with chat capability
            chosen = None
            for org in data:
                if not isinstance(org, dict):
                    continue
                caps = org.get("capabilities") or []
                if "chat" in caps or not chosen:
                    chosen = org
                    if "chat" in caps:
                        break
            if isinstance(chosen, dict) and chosen.get("uuid"):
                self._org_id = str(chosen["uuid"])
                return self._org_id, chosen
        return "", {}

    def _probe_live_models(self) -> Optional[dict]:
        """Retrieve live Claude models from signed-in session APIs + live DOM dropdown."""
        models: list[dict] = []
        seen: set[str] = set()

        def _add(mid: str, name: str, desc: str = "", active: bool = False) -> None:
            key = (mid or name).strip().lower()
            if not key or key in seen:
                return
            seen.add(key)
            low = f"{mid} {name}".lower()
            caps = ["Text", "Code"]
            if "opus" in low:
                caps.append("Reasoning")
            elif "haiku" in low:
                caps.append("Fast")
            models.append({
                "id": mid or name,
                "name": name or _humanize_claude_slug(mid),
                "org": "Anthropic",
                "description": desc or "Available in your claude.ai account",
                "capabilities": caps,
                "active": active,
            })

        # 1. Inspect claude.ai's signed-in bootstrap / account JSON for real model slugs
        for endpoint in ("/api/bootstrap", "/api/account"):
            resp = self.fetch_json(endpoint, timeout=4.5)
            data = resp.get("data")
            if not (resp.get("ok") and isinstance(data, (dict, list))):
                continue
            self._collect_claude_models_from_json(data, _add)

        # 2. Also read the live model selector button & dropdown in claude.ai's DOM (pure ASCII JS)
        script = r"""(() => {
          const isModelLabel = (t) => (
            /\b(Sonnet|Opus|Haiku)\b/i.test(t) || /\bClaude\s+\d/i.test(t)
          ) && !/\b(try|get|upgrade|new|sign|log|download|about|meet|pricing|pro|max plan)\b/i.test(t);
          const findTrigger = () => (
            document.querySelector('[data-testid="model-selector-dropdown"]')
            || Array.from(document.querySelectorAll('button')).find((b) => {
              const t = (b.innerText || b.textContent || '').trim();
              return t.length > 0 && t.length < 48 && isModelLabel(t);
            })
          );
          const trigger = findTrigger();
          const activeLabel = trigger ? (trigger.innerText || trigger.textContent || '').replace(/\s+/g, ' ').trim() : '';
          const out = [];
          const seen = new Set();
          const push = (label, desc, active) => {
            const clean = String(label || '').replace(/\s+/g, ' ').trim();
            if (!clean || clean.length > 60) return;
            if (!isModelLabel(clean)) return;
            const k = clean.toLowerCase();
            if (seen.has(k)) return;
            seen.add(k);
            out.push({ id: clean, name: clean, description: desc || '', active: Boolean(active) });
          };
          if (activeLabel) push(activeLabel, 'Currently active in claude.ai', true);
          let opened = false;
          if (trigger && trigger.getAttribute('aria-expanded') !== 'true') {
            try {
              trigger.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, button: 0, pointerType: 'mouse' }));
              trigger.click();
              opened = true;
            } catch (e) {}
          }
          return JSON.stringify({ active: activeLabel, opened, models: out });
        })()"""
        dom_res = self.eval_js(script, timeout=4.0)
        dom_active = ""
        dom_models = list(dom_res.get("models") or []) if isinstance(dom_res, dict) else []
        if isinstance(dom_res, dict):
            dom_active = str(dom_res.get("active") or "")
            if dom_res.get("opened"):
                time.sleep(0.3)
                scrape_script = r"""(() => {
                  const isModelLabel = (t) => (
                    /\b(Claude|Sonnet|Opus|Haiku)\b/i.test(t)
                    && !/\b(try|get|upgrade|new|sign|log|download|about|meet|pricing|pro|max plan)\b/i.test(t)
                  );
                  const activeLabel = (() => {
                    const tr = document.querySelector('[data-testid="model-selector-dropdown"]');
                    return tr ? (tr.innerText || tr.textContent || '').replace(/\s+/g, ' ').trim() : '';
                  })();
                  const out = [];
                  const items = Array.from(document.querySelectorAll(
                    '[role="menuitem"], [role="menuitemradio"], [role="option"], [data-radix-collection-item]'
                  ));
                  for (const item of items) {
                    const lines = (item.innerText || item.textContent || '').split('\n').map((s) => s.trim()).filter(Boolean);
                    if (!lines.length) continue;
                    const label = lines[0];
                    if (!isModelLabel(label)) continue;
                    const desc = lines.slice(1).join(' - ').slice(0, 100);
                    const isChecked = item.getAttribute('aria-checked') === 'true'
                      || item.getAttribute('data-state') === 'checked'
                      || (activeLabel && label.toLowerCase() === activeLabel.toLowerCase());
                    out.push({ id: label, name: label, description: desc, active: isChecked });
                  }
                  try {
                    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true }));
                  } catch (e) {}
                  return JSON.stringify({ models: out });
                })()"""
                scraped = self.eval_js(scrape_script, timeout=3.5)
                if isinstance(scraped, dict):
                    dom_models.extend(scraped.get("models") or [])
            for m in dom_models:
                name = str(m.get("name") or "").strip()
                if not name:
                    continue
                # Match ALL significant tokens (including minor version digits like 4.5 vs 4.6)
                matched_existing = False
                toks = [t for t in re.split(r"[\s\-_.]+", name.lower()) if t and t not in ("claude", "extended", "medium", "low", "high")]
                for existing in models:
                    ex_toks = set(re.split(r"[\s\-_.]+", f"{existing['id']} {existing['name']}".lower()))
                    if toks and all(t in ex_toks for t in toks):
                        if m.get("active"):
                            existing["active"] = True
                        matched_existing = True
                        break
                if not matched_existing:
                    _add(name, name, str(m.get("description") or ""), bool(m.get("active")))

        if not models:
            return None
        active_id = next((m["id"] for m in models if m.get("active")), "") or dom_active or models[0]["id"]
        return {
            "live": True,
            "active_model": active_id,
            "models": models,
        }

    def _collect_claude_models_from_json(self, obj, add_fn, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(obj, dict):
            # Look for model config dicts: {"model": "claude-...", "name": "..."} or {"id": "claude-..."}
            raw_slug = obj.get("model") or obj.get("model_id") or obj.get("slug") or obj.get("id")
            if isinstance(raw_slug, str) and re.match(r"^claude-[a-z0-9._-]+$", raw_slug.strip(), re.I):
                slug = raw_slug.strip()
                if not obj.get("hidden") and not obj.get("is_hidden"):
                    name = str(obj.get("display_name") or obj.get("name") or obj.get("title") or _humanize_claude_slug(slug))
                    desc = str(obj.get("description") or "")
                    self._model_label_to_slug[name.lower()] = slug
                    add_fn(slug, name, desc, bool(obj.get("is_default") or obj.get("default")))
            for k, v in obj.items():
                if isinstance(v, (dict, list)) and any(
                    hint in str(k).lower()
                    for hint in ("model", "config", "default", "account", "org", "membership", "settings", "feature")
                ):
                    self._collect_claude_models_from_json(v, add_fn, depth + 1)
                elif isinstance(v, str) and "model" in str(k).lower() and re.match(r"^claude-[a-z0-9._-]+$", v.strip(), re.I):
                    slug = v.strip()
                    add_fn(slug, _humanize_claude_slug(slug), "", "default" in str(k).lower())
        elif isinstance(obj, list):
            for item in obj[:60]:
                if isinstance(item, str) and re.match(r"^claude-[a-z0-9._-]+$", item.strip(), re.I):
                    slug = item.strip()
                    add_fn(slug, _humanize_claude_slug(slug), "", False)
                elif isinstance(item, (dict, list)):
                    self._collect_claude_models_from_json(item, add_fn, depth + 1)

    def _resolve_api_model(self, model_id: str) -> str:
        want = (model_id or "").strip()
        if not want or want.lower() == "default":
            return ""
        if want.lower().startswith("claude-"):
            return want

        def _lookup(label: str) -> str:
            low = label.lower()
            exact = self._model_label_to_slug.get(low, "")
            if exact:
                return exact
            # Token-normalized match so DOM labels like "Sonnet 4.5" match "claude-sonnet-4-5-..."
            toks = [t for t in re.split(r"[\s\-_.]+", low) if t and t not in ("claude", "extended", "medium", "low", "high")]
            if not toks:
                return ""
            for k, slug in self._model_label_to_slug.items():
                hay = set(re.split(r"[\s\-_.]+", f"{k} {slug}".lower()))
                if all(t in hay for t in toks):
                    return slug
            return ""

        slug = _lookup(want)
        if not slug and not self._model_label_to_slug:
            # Populate label->slug map lazily from bootstrap/account/org endpoints
            try:
                for endpoint in ("/api/bootstrap", "/api/account"):
                    resp = self.fetch_json(endpoint, timeout=4.0)
                    if resp.get("ok") and isinstance(resp.get("data"), (dict, list)):
                        self._collect_claude_models_from_json(resp["data"], lambda *_: None)
                _, org_obj = self._get_org_info()
                if org_obj:
                    self._collect_claude_models_from_json(org_obj, lambda *_: None)
                slug = _lookup(want)
            except Exception:
                pass
        return slug

    def _stream_turn_api(
        self,
        chat_id: str,
        full_prompt: str,
        *,
        conversation_url: str = "",
        model: str = "",
        stop_event=None,
    ) -> Optional[Iterator[dict]]:
        """Stream Claude directly via claude.ai's signed-in same-origin SSE API."""
        org_id, _ = self._get_org_info()
        if not org_id:
            return None

        conv_uuid = _extract_claude_chat_uuid(conversation_url)
        created_new = False
        if not conv_uuid:
            conv_uuid = str(uuid.uuid4())
            create_payload: dict = {"uuid": conv_uuid, "name": ""}
            api_model = self._resolve_api_model(model)
            if api_model:
                create_payload["model"] = api_model
            cr = self.fetch_json(
                f"/api/organizations/{org_id}/chat_conversations",
                method="POST",
                body=create_payload,
                timeout=8.0,
            )
            if not cr.get("ok"):
                if int(cr.get("status") or 0) in (401, 403, 404):
                    self._org_id = ""
                diag.note(f"claude create_conversation failed status={cr.get('status')} err={cr.get('error')}")
                return None
            if isinstance(cr.get("data"), dict) and cr["data"].get("uuid"):
                conv_uuid = str(cr["data"]["uuid"])
            created_new = True

        def _gen() -> Iterator[dict]:
            if created_new:
                yield {"type": "meta_update", "external_url": f"https://claude.ai/chat/{conv_uuid}"}
            yield {"type": "agent", "stage": "Claude is writing..."}
            payload: dict = {
                "prompt": full_prompt,
                "timezone": "UTC",
                "attachments": [],
                "files": [],
                "rendering_mode": "messages",
            }
            api_model = self._resolve_api_model(model)
            if api_model:
                payload["model"] = api_model
            buf = ""
            emitted_any = False
            status_code = 0
            for item in self.iter_fetch_stream(
                f"/api/organizations/{org_id}/chat_conversations/{conv_uuid}/completion",
                method="POST",
                body=payload,
                timeout=self.turn_timeout,
                stop_event=stop_event,
            ):
                ev_kind = item.get("event")
                if ev_kind == "status":
                    status_code = int(item.get("status") or 0)
                    diag.note(f"claude api completion status={status_code} model={api_model or 'default'}")
                    if status_code in (401, 403):
                        self._org_id = ""
                        self.show()
                        yield {
                            "type": "error",
                            "code": "auth",
                            "message": "Claude asked for verification or sign-in. Check the Claude window and try again.",
                            "open_window": True,
                        }
                        return
                    if status_code >= 400:
                        yield {
                            "type": "error",
                            "code": "claude_api",
                            "message": f"Claude returned HTTP {status_code}. Check the Claude window or try another model.",
                            "open_window": True,
                        }
                        return
                elif ev_kind == "chunk":
                    buf += str(item.get("data") or "")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        raw_json = line[5:].strip()
                        if not raw_json or raw_json == "[DONE]":
                            continue
                        try:
                            pkt = json.loads(raw_json)
                        except ValueError:
                            continue
                        if not isinstance(pkt, dict):
                            continue
                        ptype = pkt.get("type")
                        if ptype == "content_block_delta":
                            delta = pkt.get("delta") or {}
                            dtype = delta.get("type")
                            if dtype == "text_delta" and delta.get("text"):
                                emitted_any = True
                                yield {"type": "text", "text": str(delta["text"])}
                            elif dtype == "thinking_delta" and delta.get("thinking"):
                                emitted_any = True
                                yield {"type": "reasoning", "text": str(delta["thinking"])}
                        elif pkt.get("completion"):
                            emitted_any = True
                            yield {"type": "text", "text": str(pkt["completion"])}
                        elif ptype == "error":
                            err = pkt.get("error") or {}
                            msg = err.get("message") if isinstance(err, dict) else str(err)
                            yield {
                                "type": "error",
                                "code": "claude_api",
                                "message": msg or "Claude returned an error.",
                                "open_window": True,
                            }
                            return
                elif ev_kind == "error":
                    yield {
                        "type": "error",
                        "code": "claude_stream",
                        "message": f"Claude stream interrupted: {item.get('error')}",
                        "open_window": True,
                    }
                    return
                elif ev_kind == "end":
                    # Flush any trailing SSE line lacking a final newline
                    tail = buf.strip()
                    if tail.startswith("data:"):
                        raw_json = tail[5:].strip()
                        if raw_json and raw_json != "[DONE]":
                            try:
                                pkt = json.loads(raw_json)
                            except ValueError:
                                pkt = None
                            if isinstance(pkt, dict):
                                if pkt.get("type") == "content_block_delta":
                                    delta = pkt.get("delta") or {}
                                    if delta.get("type") == "text_delta" and delta.get("text"):
                                        emitted_any = True
                                        yield {"type": "text", "text": str(delta["text"])}
                                    elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                                        emitted_any = True
                                        yield {"type": "reasoning", "text": str(delta["thinking"])}
                                elif pkt.get("completion"):
                                    emitted_any = True
                                    yield {"type": "text", "text": str(pkt["completion"])}
                    if not emitted_any:
                        yield {
                            "type": "error",
                            "code": "claude_empty",
                            "message": (
                                f"Claude returned an empty response (HTTP {status_code or 200}). "
                                "Check the Claude window in case sign-in or verification is needed."
                            ),
                            "open_window": True,
                        }
                    return

        return _gen()

    def _apply_model_selection(self, model_id: str) -> Optional[bool]:
        want = (model_id or "").strip()
        if not want or want.lower() == "default":
            return True
        want_tokens = [tok for tok in re.split(r"[\s\-_/.]+", want.lower()) if tok and tok != "claude"]
        open_script = f"""(() => {{
          const wantFull = {json.dumps(want.lower())};
          const wantTokens = {json.dumps(want_tokens)};
          const trigger = (
            document.querySelector('[data-testid="model-selector-dropdown"]')
            || Array.from(document.querySelectorAll('button')).find((b) => {{
              const t = (b.innerText || b.textContent || '').trim();
              return t.length > 0 && t.length < 48 && /\\b(Sonnet|Opus|Haiku|Claude)\\b/i.test(t)
                && !/upgrade|new|sign/i.test(t);
            }})
          );
          if (!trigger) return JSON.stringify({{ ok: false, reason: 'no_trigger' }});
          const cur = (trigger.innerText || trigger.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          const curToks = cur.split(/[\\s\\-_/.]+/).filter(Boolean);
          if (cur === wantFull || (wantTokens.length && wantTokens.every((t) => curToks.includes(t)))) {{
            return JSON.stringify({{ ok: true, already: true }});
          }}
          trigger.dispatchEvent(new PointerEvent('pointerdown', {{ bubbles: true, cancelable: true, button: 0, pointerType: 'mouse' }}));
          trigger.click();
          return JSON.stringify({{ ok: true, opened: true }});
        }})()"""
        step1 = self.eval_js(open_script, timeout=3.0)
        if not isinstance(step1, dict) or not step1.get("ok"):
            return False
        if step1.get("already"):
            return True
        time.sleep(0.3)
        click_script = f"""(() => {{
          const wantFull = {json.dumps(want.lower())};
          const wantTokens = {json.dumps(want_tokens)};
          const items = Array.from(document.querySelectorAll(
            '[role="menuitem"], [role="menuitemradio"], [role="option"], [data-radix-collection-item]'
          ));
          for (const item of items) {{
            const txt = (item.innerText || item.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const toks = txt.split(/[\\s\\-_/.]+/).filter(Boolean);
            if (txt === wantFull || (wantTokens.length && wantTokens.every((t) => toks.includes(t)))) {{
              item.click();
              return JSON.stringify({{ ok: true, clicked: txt.slice(0, 60) }});
            }}
          }}
          document.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Escape', code: 'Escape', bubbles: true }}));
          return JSON.stringify({{ ok: false, reason: 'not_found' }});
        }})()"""
        res = self.eval_js(click_script, timeout=3.5)
        if isinstance(res, dict) and res.get("clicked"):
            time.sleep(0.25)
            return True
        return False
