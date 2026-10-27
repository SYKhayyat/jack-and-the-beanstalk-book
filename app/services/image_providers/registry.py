"""Provider construction.

The single place in the app that knows which concrete provider classes exist.
Everything else receives a ready `ImageProvider` from `build_provider()`.
"""

from __future__ import annotations

import logging

from app.config import ProviderName, Settings
from app.services.image_providers.agnes_provider import AgnesImageProvider
from app.services.image_providers.base import ImageProvider, ProviderUnavailable
from app.services.image_providers.gemini import GeminiImageProvider
from app.services.image_providers.mock import MockImageProvider
from app.services.image_providers.openai_provider import OpenAIImageProvider

__all__ = ["KNOWN_PROVIDERS", "build_provider"]

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS: dict[ProviderName, type[ImageProvider]] = {
    ProviderName.MOCK: MockImageProvider,
    ProviderName.GEMINI: GeminiImageProvider,
    ProviderName.OPENAI: OpenAIImageProvider,
    ProviderName.AGNES: AgnesImageProvider,
}


def build_provider(settings: Settings) -> ImageProvider:
    """Build the provider named in settings.

    Raises `ProviderUnavailable` for an unknown name or a provider missing
    credentials, so the caller can answer 503 with something actionable.
    """
    provider_cls = KNOWN_PROVIDERS.get(settings.image_provider)
    if provider_cls is None:
        raise ProviderUnavailable(
            f"Unknown image provider {settings.image_provider!r}. "
            f"Known providers: {', '.join(sorted(p.value for p in KNOWN_PROVIDERS))}."
        )

    if provider_cls is GeminiImageProvider:
        return GeminiImageProvider(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            timeout_seconds=settings.generation_timeout_seconds,
        )
    if provider_cls is OpenAIImageProvider:
        return OpenAIImageProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            quality=settings.openai_quality,
            timeout_seconds=settings.generation_timeout_seconds,
        )
    if provider_cls is AgnesImageProvider:
        return AgnesImageProvider(
            api_key=settings.agnes_api_key,
            model=settings.agnes_model,
            base_url=settings.agnes_base_url,
            timeout_seconds=settings.generation_timeout_seconds,
            size=settings.agnes_size,
        )
    return provider_cls()
