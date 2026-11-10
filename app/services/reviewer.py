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
* **A spent free quota is a rest, not an outage.** When Gemini says the day's
  free requests are used up, the reviewer is not asked again until the quota
  resets (`ReviewerQuotaSpent`); the decision judge decides alone meanwhile,
  without waiting out retries that cannot succeed.

The model and the key are the operator's, like every other backend: environment
only, never named on any visitor-facing surface.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, TypedDict

from app.prompt_data import prompt_text
from app.services.image_providers.base import ProviderError
from app.services.media import media_type_for

__all__ = [
    "FAULTS",
    "MINOR_FAULTS",
    "ImageReviewer",
    "OpenAIReviewer",
    "ReviewAnswer",
    "ReviewerQuotaSpent",
    "ReviewerUnavailable",
    "UnavailableReviewer",
    "Verdict",
    "build_reviewer",
    "parse_answer",
    "verdict_passes",
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

#: Faults that are recorded and steer a redraw but do not fail a picture that
#: otherwise scores well: a reviewer told to be strict lists "three cups, not
#: two" and "no cake in the basket", and a real run (2026-11-09) failed a good
#: book on exactly those. Everything else in `FAULTS` fails the picture.
MINOR_FAULTS: frozenset[str] = frozenset({"wrong_counts", "other"})


def verdict_passes(score: int, faults: tuple[str, ...]) -> bool:
    """The one pass rule: a good enough score and no serious fault."""
    return score >= PASS_SCORE and not any(fault not in MINOR_FAULTS for fault in faults)


#: A score below this fails even when the model lists no fault: "I cannot fault
#: it but it is not the moment" is a real answer and must not pass.
PASS_SCORE = 70

#: Waits before re-asking after a transient failure: the free tier's per-minute
#: quota (429) and "high demand" (503) both clear within seconds to a minute.
#: Bounded, and only for those: a malformed answer is never re-asked, because
#: that is how a reviewer quietly doubles its own cost. A failed call is not
#: billed, so these retries cost nothing; a real run lost 6 of 49 reviews to
#: them before this existed.
_TRANSIENT_BACKOFF_SECONDS: tuple[float, ...] = (3.0,)

#: HTTP statuses that mean "try again shortly", not "this request is wrong".
_TRANSIENT_CODES = frozenset({408, 429, 500, 502, 503, 504})


def _is_transient(exc: BaseException) -> bool:
    """Whether a reviewer error is the service being busy rather than refusing."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code in _TRANSIENT_CODES
    text = str(exc)
    return any(
        marker in text
        for marker in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED", "429", "503")
    )


#: Gemini names the quota it refused under; a per-day one will not clear by
#: retrying ("GenerateRequestsPerDayPerProjectPerModel-FreeTier").
_DAILY_QUOTA_MARKER = "PerDay"

#: Gemini's daily quotas reset at midnight Pacific time. Fixed at Pacific
#: standard time (UTC-8) so no time-zone database is needed: during daylight
#: time the reviewer is asked again an hour after the reset, never before it.
_QUOTA_RESET_UTC_HOUR = 8


def _is_daily_quota(exc: BaseException) -> bool:
    """Whether a reviewer error is the day's free requests being used up."""
    return _is_transient(exc) and _DAILY_QUOTA_MARKER in str(exc)


def next_quota_reset(now: datetime) -> datetime:
    """When a spent daily quota is next available: the coming 08:00 UTC."""
    reset = now.astimezone(UTC).replace(
        hour=_QUOTA_RESET_UTC_HOUR, minute=0, second=0, microsecond=0
    )
    return reset if reset > now else reset + timedelta(days=1)


#: Anything the model writes besides one JSON object is not an answer.
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class ReviewerUnavailable(ProviderError):
    """No usable reviewer is configured. A boot-time failure in production."""


class ReviewerQuotaSpent(ProviderError):
    """The reviewer's daily (free-tier) quota is used up until `until`.

    Not retried: the caller rests the reviewer until then and lets the
    decision judge decide alone.
    """

    def __init__(self, message: str, *, until: datetime) -> None:
        super().__init__(message)
        self.until = until


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
        judged_by: str = "",
        decision: dict[str, Any] | None = None,
    ) -> None:
        self.attempt = attempt
        #: "decision" or "reviewer": which check's verdict this is. "" when
        #: tier 2 did not run.
        self.judged_by = judged_by
        #: The decision model's answers, when it looked (`decision_judge.py`).
        self.decision = decision
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
            **({"judged_by": self.judged_by} if self.judged_by else {}),
            **({"decision": self.decision} if self.decision is not None else {}),
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
        self,
        image: bytes,
        *,
        media_type: str,
        page_title: str,
        story_line: str,
        cast: str,
        plate: bytes | None = None,
    ) -> ReviewAnswer:
        """Judge one picture against one page's checklist.

        Raises:
            ProviderError: the model was reached and failed, or answered
                unparseably. The caller records the failure and carries on.

        `plate` is the empty background the picture was painted into, when
        there is one. A reviewer that can use it also checks the picture kept it.
        """
        ...


class UnavailableReviewer:
    """The reviewer that is deliberately not there: local, test and mock mode.

    Distinguishable from a real one by `name`, so the runner can stamp verdicts
    `tiers_run: [1]` and say plainly in the log that tier 2 was off.
    """

    name = "off"

    def review(
        self,
        image: bytes,
        *,
        media_type: str,
        page_title: str,
        story_line: str,
        cast: str,
        plate: bytes | None = None,
    ) -> ReviewAnswer:
        del image, media_type, page_title, story_line, cast, plate  # nothing is looked at
        raise ReviewerUnavailable("no reviewer is configured")


class GeminiReviewer:
    """Tier 2 on a cheap Gemini vision model (T10's recommendation).

    Text-plus-image in, strict JSON out. A malformed answer is never re-asked
    (a second billable call is how a reviewer quietly doubles its own cost);
    a busy service (429, 503) is re-asked a bounded number of times, since a
    failed call is not billed and an outage otherwise passes a picture unjudged.
    """

    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.1-flash-lite",
        timeout_seconds: int = 20,
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
        self._sleep = time.sleep
        self._client = genai.Client(
            api_key=api_key.strip(),
            http_options=types.HttpOptions(timeout=timeout_seconds * 1000),
        )

    def review(
        self,
        image: bytes,
        *,
        media_type: str,
        page_title: str,
        story_line: str,
        cast: str,
        plate: bytes | None = None,
    ) -> ReviewAnswer:
        instruction = prompt_text()["review"]["instruction"].format(
            page_title=page_title, story_line=story_line, cast=cast
        )
        del plate  # Gemini is asked the one-picture question only
        contents: list[Any] = [
            self._types.Part.from_bytes(data=image, mime_type=media_type),
            self._types.Part.from_text(text=instruction),
        ]
        logger.info(
            "review.call model=%s page=%r image_bytes=%d", self._model, page_title, len(image)
        )
        for attempt, pause in enumerate((*_TRANSIENT_BACKOFF_SECONDS, None), start=1):
            try:
                response = self._client.models.generate_content(
                    model=self._model, contents=contents
                )
                break
            except Exception as exc:  # the SDK raises a wide range of types
                if _is_daily_quota(exc):
                    until = next_quota_reset(datetime.now(UTC))
                    raise ReviewerQuotaSpent(
                        f"the reviewer's daily quota is spent until {until:%Y-%m-%d %H:%M}Z",
                        until=until,
                    ) from None
                if pause is None or not _is_transient(exc):
                    raise ProviderError(f"the reviewer could not be reached: {exc}") from None
                logger.info(
                    "review.retry attempt=%d wait_s=%.0f error=%s",
                    attempt,
                    pause,
                    str(exc)[:120],
                )
                self._sleep(pause)
        text = getattr(response, "text", None) or ""
        return parse_answer(text)


class _ReasoningKwargs(TypedDict, total=False):
    reasoning_effort: Literal["none"]


class OpenAIReviewer:
    """Tier 2 on a cheap OpenAI vision model, shown the plate as well.

    The picture is sent first and, on a plate page, the empty plate second, with
    the plate-comparison rule from `[review]` added, so a repainted background,
    a moved prop or a stalk cut loose from its stump is a fault the reviewer can
    actually see. One call, one JSON answer, no re-ask of a malformed one.
    """

    name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-6-luna", timeout_seconds: int = 60):
        if not api_key.strip():
            raise ReviewerUnavailable(
                "OPENAI_API_KEY is not set, so pictures could not be reviewed."
            )
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - install extras
            raise ReviewerUnavailable("The 'openai' package is not installed.") from exc
        self._model = model
        self._client = openai.OpenAI(api_key=api_key.strip(), timeout=timeout_seconds)

    def _reasoning_kwargs(self) -> _ReasoningKwargs:
        """Reasoning effort for the Chat Completions call.

        The GPT-6 family bills reasoning tokens as output and, on Chat
        Completions, allows tool calling only at `none`. A reviewer answers a
        checklist; it does not need to think for long, so `none` is both the
        cheapest and the fastest setting. Older models (gpt-5-nano and below)
        have no `none` setting and would reject it, so they are left alone.
        """
        if self._model.startswith("gpt-6"):
            return {"reasoning_effort": "none"}
        return {}

    def review(
        self,
        image: bytes,
        *,
        media_type: str,
        page_title: str,
        story_line: str,
        cast: str,
        plate: bytes | None = None,
    ) -> ReviewAnswer:
        review = prompt_text()["review"]
        instruction = review["instruction"].format(
            page_title=page_title, story_line=story_line, cast=cast
        )
        content: list[dict[str, Any]] = [
            {"type": "text", "text": instruction},
            {"type": "image_url", "image_url": {"url": _data_url(image, media_type)}},
        ]
        if plate is not None:
            content.append({"type": "text", "text": review["plate_rule"]})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _data_url(plate, media_type_for(plate))},
                }
            )
        logger.info(
            "review.call model=%s page=%r image_bytes=%d plate=%s",
            self._model,
            page_title,
            len(image),
            plate is not None,
        )
        messages: list[Any] = [{"role": "user", "content": content}]
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format={"type": "json_object"},
                **self._reasoning_kwargs(),
            )
        except Exception as exc:  # the SDK raises a wide range of types
            if "insufficient_quota" in str(exc):
                until = next_quota_reset(datetime.now(UTC))
                raise ReviewerQuotaSpent(
                    "the reviewer's OpenAI quota is spent", until=until
                ) from None
            raise ProviderError(f"the reviewer could not be reached: {exc}") from None
        return parse_answer(response.choices[0].message.content or "")


def _data_url(image: bytes, media_type: str) -> str:
    return f"data:{media_type};base64," + base64.b64encode(image).decode("ascii")


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
    *,
    api_key: str,
    model: str,
    required: bool,
    timeout_seconds: int = 60,
    provider: str = "gemini",
) -> ImageReviewer:
    """The reviewer for this process, or the honest stand-in.

    Args:
        api_key: The reviewer's key.
        model: The vision model to use.
        required: True in production, where a missing key must fail at boot
            rather than quietly ship unreviewed pictures (T9.6).
        timeout_seconds: One review's budget.
        provider: "off", "gemini" or "openai"; `api_key` is that provider's key.
            Off is the stand-in whatever the key: no picture is sent anywhere.

    Raises:
        ReviewerUnavailable: no key and `required`.
    """
    if provider == "off":
        return UnavailableReviewer()
    if api_key.strip() and provider == "openai":
        return OpenAIReviewer(api_key, model=model, timeout_seconds=timeout_seconds)
    if api_key.strip():
        return GeminiReviewer(api_key, model=model, timeout_seconds=timeout_seconds)
    if required:
        raise ReviewerUnavailable(
            "The reviewer is required in production: set REVIEWER_API_KEY (or "
            "GEMINI_API_KEY), or run on the mock provider."
        )
    return UnavailableReviewer()
