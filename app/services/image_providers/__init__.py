"""Image provider boundary. See `base.py` for the contract and `registry.py`
for how a concrete provider is chosen."""

from app.services.image_providers.agnes_provider import AgnesImageProvider
from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
    ProviderError,
    ProviderUnavailable,
)
from app.services.image_providers.mock import MockImageProvider
from app.services.image_providers.openai_provider import OpenAIImageProvider
from app.services.image_providers.registry import build_provider

__all__ = [
    "AgnesImageProvider",
    "GeneratedImage",
    "ImageProvider",
    "ImageRequest",
    "MockImageProvider",
    "OpenAIImageProvider",
    "ProviderError",
    "ProviderUnavailable",
    "build_provider",
]
