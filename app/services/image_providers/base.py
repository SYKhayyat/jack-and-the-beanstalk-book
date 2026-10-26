"""The provider boundary.

Everything above this line (routes, pipeline, prompts) talks only to
`ImageProvider`. Swapping in a different image backend means writing one class
with a `generate` method and registering it in `registry.py`. Nothing else in
the app changes, and no provider is allowed to know about HTTP status codes,
file formats on disk, or scenes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

__all__ = [
    "GeneratedImage",
    "ImageProvider",
    "ImageRequest",
    "ProviderError",
    "ProviderUnavailable",
]


class ProviderError(RuntimeError):
    """The provider was reached but failed, or returned something unusable."""


class ProviderUnavailable(ProviderError):
    """The provider is not usable at all — missing key, unknown name, etc.

    Distinguished from `ProviderError` so the app can return a clear 503 with
    actionable advice instead of a generic 500.
    """


@dataclass(frozen=True, slots=True)
class ImageRequest:
    """One image generation call.

    Attributes:
        prompt: The full instruction, already assembled by the prompt service.
        reference_images: Images the model must use as reference. The first one
            is always the identity anchor; providers must not silently drop it.
        output_format: "png" or "jpeg".
        aspect_ratio: Passed through when the backend supports it natively.
        label: Short human tag used in logs, e.g. "cartoonify" or "scene".
    """

    prompt: str
    reference_images: tuple[bytes, ...] = field(default_factory=tuple)
    reference_media_types: tuple[str, ...] = field(default_factory=tuple)
    output_format: str = "png"
    aspect_ratio: str = "4:3"
    label: str = "generate"


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    """A single image returned by a provider."""

    data: bytes
    media_type: str = "image/png"
    model: str = "unknown"

    @property
    def extension(self) -> str:
        return "jpg" if self.media_type.endswith("jpeg") else "png"


class ImageProvider(ABC):
    """Contract every image backend must satisfy."""

    name: str

    @abstractmethod
    def generate(self, request: ImageRequest) -> GeneratedImage:
        """Generate one image, or raise `ProviderError`.

        Implementations must be stateless with respect to the request and must
        not mutate the input.
        """

    def generate_candidates(self, request: ImageRequest, count: int) -> list[GeneratedImage]:
        """Generate `count` alternatives, ideally in one API call.

        The default implementation makes a single call and returns one image,
        even if more were asked for: correctness over pretending. Providers that
        support an `n` parameter override this and return `count` images from
        one request.

        Deliberately not implemented as a loop over `generate`. Calling the API
        once per candidate would cost `count` times as much for the same
        picture, and looping around a billable call is exactly what this project
        forbids.
        """
        if count < 1:
            raise ValueError("count must be at least 1")
        return [self.generate(request)]

    def health_check(self) -> tuple[bool, str]:
        """Report whether this provider can currently be used.

        Returns `(ok, human_readable_reason)`. Default implementation assumes a
        constructed provider is usable; providers with credentials override it.
        """
        return True, "ok"

    @property
    def draws_placeholders(self) -> bool:
        """True when this provider returns stand-in pictures, not real drawings.

        The page says so in plain words when it is true — a visitor who gets
        placeholder art should know it. Which model and which quality are
        behind it stay in `/healthz`, for the operator.
        """
        return False
