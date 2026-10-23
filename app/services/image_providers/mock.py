"""A provider that never touches the network.

Purpose: let the entire app — UI, routes, pipeline, storage, tests — run with no
API key and no network. It renders a placeholder locally with Pillow so the
output is a real image a browser can display, clearly marked as a mock so nobody
mistakes it for a real result.

It is deterministic: the same request always produces the same bytes.
"""

from __future__ import annotations

import hashlib
import io
import textwrap

from PIL import Image, ImageDraw, ImageFont

from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
)

__all__ = ["MockImageProvider"]

_CANVAS = (1024, 768)
_BANNER_HEIGHT = 52
_BOTTOM_BANNER_TOP = _CANVAS[1] - _BANNER_HEIGHT
_PALETTE = [(36, 62, 104), (92, 46, 84), (28, 84, 72), (104, 66, 32), (58, 40, 96)]
_SCENE_LINE = "SCENE TO DRAW"


class MockImageProvider(ImageProvider):
    """Renders a labelled placeholder derived from the prompt."""

    name = "mock"

    def generate(self, request: ImageRequest) -> GeneratedImage:
        seed = hashlib.sha256((request.label + "|" + request.prompt).encode("utf-8")).digest()
        primary = _PALETTE[seed[0] % len(_PALETTE)]
        secondary = _PALETTE[seed[1] % len(_PALETTE)]

        image = Image.new("RGB", _CANVAS, primary)
        draw = ImageDraw.Draw(image, "RGBA")

        # Soft diagonal wash so the two pipeline steps look visually distinct.
        for step in range(_CANVAS[1]):
            ratio = step / _CANVAS[1]
            colour = tuple(
                int(primary[i] + (secondary[i] - primary[i]) * ratio) for i in range(3)
            )
            draw.line([(0, step), (_CANVAS[0], step)], fill=colour)

        self._draw_figure(draw, seed)
        _banner(draw, "MOCK PROVIDER - NOT A REAL GENERATION", 0, _BANNER_HEIGHT, primary)
        self._draw_caption(draw, request.prompt)
        _banner(
            draw,
            f"STEP: {request.label}",
            _BOTTOM_BANNER_TOP,
            _BANNER_HEIGHT,
            primary,
        )

        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return GeneratedImage(data=buffer.getvalue(), media_type="image/png", model="mock-v1")

    @staticmethod
    def _draw_figure(draw: ImageDraw.ImageDraw, seed: bytes) -> None:
        """A stylised head-and-shoulders silhouette, seeded by the prompt hash."""
        cx = _CANVAS[0] // 2
        cy = int(_CANVAS[1] * 0.4)
        radius = int(_CANVAS[0] * (0.12 + (seed[2] % 20) / 1000))
        skin = (232 - seed[3] % 60, 190 - seed[4] % 50, 165 - seed[5] % 40)

        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=skin)
        draw.ellipse(
            [cx - radius * 2, cy + radius, cx + radius * 2, cy + radius * 4],
            fill=(245, 245, 245, 220),
        )
        eye_dx, eye_y = radius // 3, cy - radius // 4
        eye_r = max(3, radius // 9)
        for sign in (-1, 1):
            ex = cx + sign * eye_dx
            draw.ellipse(
                [ex - eye_r, eye_y - eye_r, ex + eye_r, eye_y + eye_r], fill=(30, 30, 40)
            )

    @staticmethod
    def _draw_caption(draw: ImageDraw.ImageDraw, prompt: str) -> None:
        """Print the scene specification, so mock mode still demonstrates intent."""
        font = _font(20)
        caption = _extract_caption(prompt)
        lines = textwrap.wrap(caption, width=62)[:6]

        top = _BOTTOM_BANNER_TOP - 28 - len(lines) * 28
        draw.rounded_rectangle(
            [60, top - 14, _CANVAS[0] - 60, _BOTTOM_BANNER_TOP - 14],
            radius=18,
            fill=(0, 0, 0, 140),
        )
        for index, line in enumerate(lines):
            draw.text((84, top + index * 28), line, fill=(255, 255, 255, 255), font=font)


def _extract_caption(prompt: str) -> str:
    """Pull the scene caption out of a prompt, falling back to its first sentence.

    The scene pass labels the specification explicitly; the cartoon pass has no
    scene, so its opening line is the most informative thing available.
    """
    for line in prompt.splitlines():
        if line.startswith(_SCENE_LINE):
            return line.split(":", 1)[1].strip()
    return prompt.strip().split(". ")[0].strip().rstrip(".") or prompt[:90]


def _banner(
    draw: ImageDraw.ImageDraw, text: str, top: int, height: int, base: tuple[int, ...]
) -> None:
    draw.rectangle([0, top, _CANVAS[0], top + height], fill=(*base, 235))
    draw.text(
        (18, top + height // 2 - 12),
        text,
        fill=(255, 225, 150, 255),
        font=_font(20),
    )


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """A scalable font when Pillow can provide one, else its bitmap default."""
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - Pillow < 10.1
        return ImageFont.load_default()
