"""Pluggable multi-provider registry for Arena Desktop tabs."""
from __future__ import annotations

from typing import Optional

from arena.providers.antigravity import AntigravityProvider
from arena.providers.base import Provider
from arena.providers.chatgpt import ChatGPTProvider
from arena.providers.claude import ClaudeProvider
from arena.providers.deepseek import DeepSeekProvider
from arena.providers.web_engine import get_window_factory, set_window_factory

PROVIDERS: dict[str, Provider] = {
    "deepseek": DeepSeekProvider(),
    "claude": ClaudeProvider(),
    "chatgpt": ChatGPTProvider(),
    "antigravity": AntigravityProvider(),
}

EXTRA_PROVIDER_IDS: tuple[str, ...] = tuple(PROVIDERS.keys())
ALL_TRACK_IDS: tuple[str, ...] = ("arena", "google", *EXTRA_PROVIDER_IDS)


def get_provider(provider_id: str) -> Optional[Provider]:
    return PROVIDERS.get(str(provider_id or "").strip().lower())


def list_providers() -> list[dict]:
    return [PROVIDERS[pid].to_public() for pid in EXTRA_PROVIDER_IDS]


def shutdown_all() -> None:
    for provider in PROVIDERS.values():
        try:
            provider.shutdown()
        except Exception:
            pass


__all__ = [
    "ALL_TRACK_IDS",
    "EXTRA_PROVIDER_IDS",
    "PROVIDERS",
    "Provider",
    "get_provider",
    "get_window_factory",
    "list_providers",
    "set_window_factory",
    "shutdown_all",
]
