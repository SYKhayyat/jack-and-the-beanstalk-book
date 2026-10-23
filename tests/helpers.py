"""Plain test helpers, kept out of conftest so tests can import them normally."""

from __future__ import annotations

import io
import os

from PIL import Image

from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
)


def make_photo_bytes(size: tuple[int, int] = (640, 480), colour: str = "#3f6f8f") -> bytes:
    """A tiny but genuine PNG, standing in for a user's upload."""
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def make_noisy_photo_bytes(size: tuple[int, int] = (1200, 1200)) -> bytes:
    """A large PNG that resists compression, for exercising upload limits.

    A flat-colour image compresses to a few kilobytes, so repeating one cannot
    stand in for a file that genuinely exceeds a size cap.
    """
    image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def tiny_png_bytes(size: tuple[int, int] = (8, 8), colour: str = "#2f7d4f") -> bytes:
    """The smallest valid PNG, used as canned provider output."""
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


class RecordingProvider(ImageProvider):
    """Records every request so tests can assert on prompts and references."""

    name = "recording"

    def __init__(self, image: bytes | None = None) -> None:
        self.requests: list[ImageRequest] = []
        self._image = image or tiny_png_bytes()

    def generate(self, request: ImageRequest) -> GeneratedImage:
        self.requests.append(request)
        return GeneratedImage(data=self._image, media_type="image/png", model="recording-v1")


class StubProvider(ImageProvider):
    """Returns a fixed, possibly degenerate, result. For failure-path tests."""

    name = "stub"

    def __init__(self, data: bytes, model: str = "stub-v1") -> None:
        self._data = data
        self._model = model
        self.calls = 0

    def generate(self, request: ImageRequest) -> GeneratedImage:
        self.calls += 1
        return GeneratedImage(data=self._data, media_type="image/png", model=self._model)


def multipart(form: dict[str, object]) -> dict[str, dict[str, object]]:
    """Split a form spec into httpx's `data` and `files` keyword arguments.

    A tuple value means "a file"; everything else is a plain form field. Without
    this split httpx would send every value as a file part, and FastAPI would
    reject the scalar fields.
    """
    files = {key: value for key, value in form.items() if isinstance(value, tuple)}
    data = {key: value for key, value in form.items() if not isinstance(value, tuple)}
    return {"files": files, "data": data}
