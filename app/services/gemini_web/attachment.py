"""One picture handed to Gemini in Chrome."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Attachment"]


@dataclass(frozen=True, slots=True)
class Attachment:
    data: bytes
    media_type: str = "image/jpeg"

    @property
    def filename(self) -> str:
        return "image.png" if self.media_type.endswith("png") else "image.jpg"
