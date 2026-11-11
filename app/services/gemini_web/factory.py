"""The process's one Gemini-in-Chrome client.

Every gemini_web backend (the image provider, the reviewer, the decision
judge) shares it: one Chrome tab does one job at a time, so they must queue on
the same client.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

from app.services.gemini_web.client import GeminiWeb, GeminiWebConfig

if TYPE_CHECKING:
    from app.config import Settings

__all__ = ["client_for"]


def client_for(settings: Settings) -> GeminiWeb:
    """The process's one Gemini-in-Chrome client for these settings."""
    return _client(
        GeminiWebConfig(
            account=settings.gemini_web_account.strip(),
            debug_port=settings.gemini_web_port,
            profile_dir=settings.gemini_web_profile_dir,
            chrome_binary=settings.gemini_web_chrome,
            limit_wait_seconds=float(settings.gemini_web_limit_wait_seconds),
        )
    )


@cache
def _client(config: GeminiWebConfig) -> GeminiWeb:
    return GeminiWeb(config)
