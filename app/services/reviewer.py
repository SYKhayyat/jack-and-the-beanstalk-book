"""Tier 2 of the review: a vision model looks at every finished picture (T9/T10).

Tier 1 (`image_review.py`) is free and mechanical: it can tell a blank frame from
a picture, and a plate handed back empty from an edit. It cannot tell whether the
picture shows *this page's moment*, whether a second person wandered in, or
whether a letter of text was drawn into the sky. Only a model that can see can
answer those, and they are the failures that reach a child's book.

So every image that passes tier 1 goes to a vision model with one page's
checklist and must come back as one strict JSON object. Three design decisions
matter more than the wording:

* **The reviewer never sees the child.** Not their photograph, not their name,
  not the About text. A reviewer shown "this should be Emma" is being asked to
  be polite about Emma. Identity is tier 1's business and the portrait's
  approval step; this tier judges the *scene*.
* **A verdict is only a review when both tiers ran.** A missing reviewer key
  fails at boot in production rather than silently shipping unreviewed pictures
  (T9.6). In local, test and mock mode tier 2 is off, every verdict is stamped
  `tiers_run: [1]`, and the log says so — never a pretend review.
* **A reviewer failure never fails the book.** One unparseable answer records
  tier 2 as failed for that image and the page continues (T10.2). A reviewer
  outage must not cost a parent a finished book.

The model and the key are the operator's, like every other backend: environment
only, never named on any visitor-facing surface.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.prompt_data import prompt_text
from app.services.image_providers.base import ProviderError

__all__ = [
    "FAULTS",
    "ImageReviewer",
    "ReviewAnswer",
    "ReviewerUnavailable",
    "UnavailableReviewer",
    "Verdict",
    "build_reviewer",
    "parse_answer",
]

logger = logging.getLogger(__name__)

#: Every fault a tier-2 verdict may name. A closed set, so a page's stored
#: verdict can be counted and displayed without a translation table.
FAULTS: tuple[str, ...] = (
    "wrong_moment",
    "missing_character",
    "extra_person",
    "text_in_image",
    "face_obscured",
    "frightening",
    "wrong_counts",
    "deformed",
    "collage",
    "other",
)

#: A score below this fails even when the model lists no fault: "I cannot fault
#: it but it is not the moment" is a real answer and must not pass.
PASS_SCORE = 70

#: Anything the model writes besides one JSON object is not an answer.
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class ReviewerUnavailable(ProviderError):
    """No usable reviewer is configured. A boot-time failure in production."""


class Verdict:
    """One image's combined verdict, in the shape the book store persists (T9).

    Tier 1 and tier 2 are recorded together because the stored list must answer
    "how many tiers ran" without the reader reconstructing it from a log.
    """

    def __init__(
        self,
        *,
        attempt: int,
        passed: bool,
        score: int,
        faults: tuple[str, ...] = (),
        tiers_run: tuple[int, ...] = (1,),
        note: str = "",
        tier2_failed: bool = False,
    ) -> None:
        self.attempt = attempt
        self.passed = passed
        self.score = score
        self.faults = faults
        self.tiers_run = tiers_run
        self.note = note
        self.tier2_failed = tier2_failed

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            # 0 when nothing looked at the picture — an unpinned book, which the
            # store does not ask for a verdict from either.
            "tier": max(self.tiers_run, default=0),
            "tiers_run": list(self.tiers_run),
            "passed": self.passed,
            "score": self.score,
            "faults": list(self.faults),
            "note": self.note,
            **({"tier2_failed": True} if self.tier2_failed else {}),
        }


@dataclass(frozen=True, slots=True)
class ReviewAnswer:
    """What the vision model said, before it is combined with tier 1."""

    passed: bool
    score: int
    faults: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""


class ImageReviewer(Protocol):
    """The tier-2 contract: one picture in, one structured answer out."""

    name: str

    def review(
        self, image: bytes, *, media_type: str, page_title: str, story_line: str, cast: str
    ) -> ReviewAnswer:
        """Judge one picture against one page's checklist.

        Raises:
            ProviderError: the model was reached and failed, or answered
                unparseably. The caller records the failure and carries on.
        """
        ...


class UnavailableReviewer:
    """The reviewer that is deliberately not there: local, test and mock mode.

    Distinguishable from a real one by `name`, so the runner can stamp verdicts
    `tiers_run: [1]` and say plainly in the log that tier 2 was off.
    """

    name = "off"

    def review(
        self, image: bytes, *, media_type: str, page_title: str, story_line: str, cast: str
    ) -> ReviewAnswer:
        del image, media_type, page_title, story_line, cast  # nothing is looked at
        raise ReviewerUnavailable("no reviewer is configured")


class GeminiReviewer:
    """Tier 2 on a cheap Gemini vision model (T10's recommendation).

    Text-plus-image in, strict JSON out, no retry loop: a second billable call
    because the first answer was malformed is how a reviewer quietly doubles its
    own cost.
    """

    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.1-flash-lite",
        timeout_seconds: int = 60,
    ):
        if not api_key.strip():
            raise ReviewerUnavailable(
                "REVIEWER_API_KEY (or GEMINI_API_KEY) is not set, so pictures "
                "could not be reviewed. Set one, or run with IMAGE_PROVIDER=mock."
            )
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - install extras
            raise ReviewerUnavailable(
                "The 'google-genai' package is not installed. Run: pip install google-genai"
            ) from exc
        self._model = model
        self._types = types
        self._client = genai.Client(
            api_key=api_key.strip(),
            http_options=types.HttpOptions(timeout=timeout_seconds * 1000),
        )

    def review(
        self, image: bytes, *, media_type: str, page_title: str, story_line: str, cast: str
    ) -> ReviewAnswer:
        instruction = prompt_text()["review"]["instruction"].format(
            page_title=page_title, story_line=story_line, cast=cast
        )
        contents: list[Any] = [
            self._types.Part.from_bytes(data=image, mime_type=media_type),
            self._types.Part.from_text(text=instruction),
        ]
        logger.info(
            "review.call model=%s page=%r image_bytes=%d", self._model, page_title, len(image)
        )
        try:
            response = self._client.models.generate_content(
                model=self._model, contents=contents
            )
        except Exception as exc:  # the SDK raises a wide range of types
            raise ProviderError(f"the reviewer could not be reached: {exc}") from None
        text = getattr(response, "text", None) or ""
        return parse_answer(text)


def parse_answer(text: str) -> ReviewAnswer:
    """Read one strict JSON object out of a model's answer.

    Lenient about *where* the JSON sits (a model that adds a sentence around it
    is not wrong, just chatty) and strict about its contents: an unknown fault
    name, a missing key or a non-boolean `passed` is a failed review, not a
    default pass.
    """
    match = _JSON_OBJECT.search(text)
    if match is None:
        raise ProviderError("the reviewer's answer had no JSON object in it")
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ProviderError(f"the reviewer's JSON did not parse: {exc}") from None
    if not isinstance(raw, dict):
        raise ProviderError("the reviewer's answer was not an object")

    passed = raw.get("passed")
    score = raw.get("score")
    if not isinstance(passed, bool) or not isinstance(score, (int, float)):
        raise ProviderError(
            "the reviewer's answer had no boolean `passed` and numeric `score`"
        )
    faults_raw = raw.get("faults", [])
    if not isinstance(faults_raw, list):
        raise ProviderError("the reviewer's `faults` was not a list")
    # A fault name outside the closed set is still a fault: it is stored as
    # "other" (so the verdict only names things the app can display) and the
    # model's own word is kept in the note. Dropping it would let a picture the
    # model objected to pass on its score alone.
    faults: list[str] = []
    unknown: list[str] = []
    for fault in (str(item) for item in faults_raw):
        if fault in FAULTS:
            name = fault
        else:
            name = "other"
            unknown.append(fault)
        if name not in faults:
            faults.append(name)
    note = str(raw.get("note", ""))
    if unknown:
        logger.info("review.unknown_faults names=%s", ",".join(unknown))
        note = f"[{', '.join(unknown)}] {note}".strip()
    note = note[:600]
    return ReviewAnswer(
        passed=bool(passed),
        score=int(max(0, min(100, float(score)))),
        faults=tuple(faults),
        note=note,
    )


def build_reviewer(
    *, api_key: str, model: str, required: bool, timeout_seconds: int = 60
) -> ImageReviewer:
    """The reviewer for this process, or the honest stand-in.

    Args:
        api_key: The reviewer's key.
        model: The vision model to use.
        required: True in production, where a missing key must fail at boot
            rather than quietly ship unreviewed pictures (T9.6).
        timeout_seconds: One review's budget.

    Raises:
        ReviewerUnavailable: no key and `required`.
    """
    if api_key.strip():
        return GeminiReviewer(api_key, model=model, timeout_seconds=timeout_seconds)
    if required:
        raise ReviewerUnavailable(
            "The reviewer is required in production: set REVIEWER_API_KEY (or "
            "GEMINI_API_KEY), or run on the mock provider."
        )
    return UnavailableReviewer()
