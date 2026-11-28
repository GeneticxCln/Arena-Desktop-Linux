"""Common contract for pluggable Arena Desktop tab providers."""
from __future__ import annotations

from typing import Iterator


class Provider:
    """Base interface implemented by every extra tab provider."""

    id: str = ""
    name: str = ""
    short_name: str = ""
    badge: str = "*"
    badge_class: str = ""
    description: str = ""
    origin: str = ""
    kind: str = "webview"  # "webview" | "local_agent"

    def status(self) -> dict:
        return {
            "available": False,
            "signed_in": False,
            "open": False,
            "window": False,
            "detail": "",
        }

    def to_public(self) -> dict:
        st = self.status()
        return {
            "id": self.id,
            "name": self.name,
            "short_name": self.short_name or self.name,
            "badge": self.badge,
            "badge_class": self.badge_class or f"is-{self.id}",
            "description": self.description,
            "origin": self.origin,
            "kind": self.kind,
            **st,
        }

    def open_sign_in(self) -> dict:
        return {"ok": False, "error": f"{self.name} does not use a sign-in window."}

    def show(self) -> bool:
        return False

    def hide(self) -> bool:
        return False

    def verify(self) -> dict:
        return self.status()

    def list_models(self, *, force: bool = False) -> dict:
        return {
            "ok": True,
            "provider": self.id,
            "live": False,
            "active_model": "",
            "models": [],
        }

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
        yield {
            "type": "error",
            "code": "unsupported",
            "message": f"{self.name} is not implemented.",
        }

    def shutdown(self) -> None:
        return None
