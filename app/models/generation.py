"""Value objects passed between the pipeline, providers and routes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.models.settings import CartoonSettings


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """Everything produced by one pipeline run.

    Both intermediate and final images are kept: showing the user the
    cartoonified self-portrait next to the scene composite is the clearest way
    to see that the identity survived the scene transfer.
    """

    generation_id: str
    provider: str
    model: str
    scene_key: str
    caption: str
    settings: CartoonSettings
    cartoon_prompt: str
    scene_prompt: str
    cartoon_image: bytes
    scene_image: bytes
    candidate_count: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """A validated request: the raw photo, a scene, and the settings."""

    photo: bytes
    photo_filename: str
    photo_media_type: str
    scene_key: str
    settings: CartoonSettings


__all__ = ["GenerationRequest", "GenerationResult"]
