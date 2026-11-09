"""Tier-1 image review: the free, deterministic checks every picture must pass.

T9 asks for two tiers of review before an image can reach a book. Tier 2 is a
vision model (not built yet — see `docs/book-template-plan.md`, T9/T10). This
module is **tier 1**, which runs on every image, costs nothing, needs no API
key, and cannot judge taste — only whether a picture is, mechanically, a usable
picture of the scene it was asked for.

That distinction is worth stating where the code lives: a tier-1 pass is not
"the page looks right", it is "this is not a blank frame, not the plate handed
back unchanged, not the wrong shape, and not a black square". It catches the
four failures that actually happen and are worth a re-roll, and it says plainly
that tier 2 did not run.

Two checks are specific to the edit pass (T8) and matter more than the rest:

* **`plate_unchanged`** — the model returned the plate with nobody painted in.
  Without this, a provider that silently ignores the cast would ship twenty-one
  empty rooms.
* **`background_redrawn`** — the picture is too different, across the whole
  frame, from the plate it was supposed to keep. T0 calibrates the real
  threshold with hand-made plates; this is the coarse guard that fails
  obviously-redrawn backgrounds today, and its constant is named so the
  calibration has an obvious home.

A verdict is the shape T9 specifies, stored as-is by the book store:
`{"attempt": 1, "tier": 1, "passed": bool, "score": int, "faults": [...]}`.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageChops, ImageStat

__all__ = [
    "BACKGROUND_DRIFT_LIMIT",
    "PLATE_UNCHANGED_LIMIT",
    "Tier1Review",
    "review_image",
]

#: Mean absolute difference (0-255, per channel) above which a page is judged to
#: have had its background redrawn rather than edited. A character painted into
#: a slot changes a small share of the frame; a re-imagined scene changes most
#: of it. T0 replaces this with an operator-calibrated number.
BACKGROUND_DRIFT_LIMIT = 34.0

#: Mean absolute difference below which the "edited" page is the plate itself.
#: Nobody stands in the frame.
PLATE_UNCHANGED_LIMIT = 1.5

#: A frame whose channel standard deviation is under this has nothing in it.
_FLAT_DEVIATION = 4.0

#: Mean brightness outside this band is a black or blown-out frame.
_DARK_MEAN = 12.0
_LIGHT_MEAN = 246.0


@dataclass(frozen=True, slots=True)
class Tier1Review:
    """One image's tier-1 verdict, in the shape the book store persists."""

    passed: bool
    score: int
    faults: tuple[str, ...] = field(default_factory=tuple)

    def as_verdict(self, *, attempt: int = 1) -> dict[str, Any]:
        """The stored verdict: T9's shape, with tier 2 honestly marked absent."""
        return {
            "attempt": attempt,
            "tier": 1,
            "tiers_run": [1],
            "passed": self.passed,
            "score": self.score,
            "faults": list(self.faults),
        }


def _open(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image.convert("RGB")


def _mean_absolute_difference(a: Image.Image, b: Image.Image) -> float:
    """Mean per-channel absolute difference between two frames, 0-255.

    Both frames are reduced to the same small size first. A painted-in
    character is a change of shape and colour in one region; comparing
    thumbnails measures the change that matters and ignores re-encoding noise,
    which a full-resolution comparison would count.
    """
    size = (64, 48)
    left = a.resize(size, Image.Resampling.BILINEAR)
    right = b.resize(size, Image.Resampling.BILINEAR)
    difference = ImageChops.difference(left, right)
    means = ImageStat.Stat(difference).mean
    return sum(means) / len(means)


def review_image(
    image_bytes: bytes,
    *,
    plate_bytes: bytes | None = None,
    expected_ratio: float | None = None,
) -> Tier1Review:
    """Run every free check on one picture and return its verdict.

    Args:
        image_bytes: The picture the provider returned.
        plate_bytes: For an edit pass, the plate it was asked to keep. Enables
            the two checks that make the edit pass meaningful.
        expected_ratio: Width / height the page should have, when it matters.
            A provider that ignores the aspect ratio gives a picture the PDF
            then crops, so a big mismatch is a fault, not a taste question.

    Never raises for a bad picture: an image that cannot be decoded is a
    failing verdict, not an exception, because the caller's job is to record
    the failure and move on.
    """
    faults: list[str] = []
    score = 100
    try:
        image = _open(image_bytes)
    except (OSError, ValueError):
        return Tier1Review(passed=False, score=0, faults=("undecodable",))

    if image.width < 256 or image.height < 256:
        faults.append("too_small")
        score -= 60

    if expected_ratio is not None and image.height:
        actual = image.width / image.height
        if abs(actual - expected_ratio) > 0.02 * max(expected_ratio, 1.0) + 0.02:
            faults.append("wrong_aspect")
            score -= 25

    stat = ImageStat.Stat(image)
    means = stat.mean
    deviations = stat.stddev
    brightness = sum(means) / len(means)
    if brightness < _DARK_MEAN:
        faults.append("all_dark")
        score -= 60
    elif brightness > _LIGHT_MEAN:
        faults.append("all_light")
        score -= 60
    if max(deviations) < _FLAT_DEVIATION:
        faults.append("blank")
        score -= 80

    if plate_bytes is not None:
        try:
            plate = _open(plate_bytes)
        except (OSError, ValueError):
            plate = None
        if plate is not None:
            drift = _mean_absolute_difference(image, plate)
            if drift <= PLATE_UNCHANGED_LIMIT:
                # The edit came back as the plate: no character in the frame.
                faults.append("plate_unchanged")
                score -= 90
            elif drift >= BACKGROUND_DRIFT_LIMIT:
                faults.append("background_redrawn")
                score -= 45

    return Tier1Review(passed=not faults, score=max(0, score), faults=tuple(faults))
