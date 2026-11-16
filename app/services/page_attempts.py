"""One page's attempt loop: draw, check, and draw again until it passes (T9.3).

Pulled out of `book_runner` (audit 2026-11-15, R5) so the loop reads on its own:
the runner owns the book, the gate and the money; this module owns the order of
one page's attempts and which one is kept.

Everything the loop needs from the runner comes in as `AttemptHooks`, already
bound to the book and the page, so nothing here knows about stores, ledgers or
providers, and a test can drive the loop with plain functions.

Two things stop a page early, both keeping the best picture it has:

- the book's **retry budget** (C4): money spent past a share of the price;
- the book's **deadline** (A1): a retry is only started when it can finish in
  the time the book has left, judged by how long this page's attempts took.

A first attempt always runs: both stops are about redraws, never a hole in the
book.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from time import monotonic
from typing import Any

from app.services.costs import cost_scope
from app.services.image_providers.base import GeneratedImage, ProviderError
from app.services.image_review import Tier1Review
from app.services.page_picture import PreparedPage
from app.services.reviewer import Verdict

__all__ = [
    "FIXABLE_FAULTS",
    "STAGE_PAINTING",
    "STAGE_RETRYING",
    "STAGE_REVIEWING",
    "AttemptHooks",
    "Attempts",
    "Box",
    "draw_until_pass",
]

#: The book runner's logger, not this module's: a page's lines are the job's
#: lines, and the operator's filters and the tests read them under one name.
logger = logging.getLogger("app.services.book_runner")

#: Faults that live in one part of a picture, so the second attempt sends that
#: picture back to be mended rather than drawing the page again from the plate:
#: a redraw rolls every part of it again to fix one hand. Not here, so redrawn:
#: a collage or a background redrawn into another place (the whole picture is
#: wrong), and tier 1's mechanical faults (nothing worth keeping came back).
FIXABLE_FAULTS = frozenset(
    {
        "wrong_moment",
        "missing_character",
        "extra_person",
        "text_in_image",
        "face_obscured",
        "frightening",
        "missing_item",
        "wrong_counts",
        "deformed",
        "likeness",
        "other",
    }
)

#: Which attempt may be a fix. Only the second: the third is always a clean
#: redraw from the plate, so a fix that made things worse is not built on, and
#: an edit is never made of an edit.
_FIX_ATTEMPT = 2

#: The named stages a job reports while it works (T12). A percentage says how
#: much is left; a stage says what is happening, which is the difference between
#: "45% for two minutes" looking stuck and looking like work.
STAGE_PAINTING = "painting"
STAGE_REVIEWING = "reviewing"
STAGE_RETRYING = "retrying"

Box = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class Attempts:
    """What one page's attempt loop produced (T9.3).

    `prompts` is per attempt, not just the winner's: the prompt record is meant
    to be what was actually sent, and a retry sends different text — the same
    sentence for every attempt would hide the very retry it documents.
    """

    image: bytes
    prompt: str
    prompts: tuple[str, ...]
    verdicts: tuple[dict[str, Any], ...]
    chosen: int
    best_effort: bool


@dataclass(frozen=True, slots=True)
class AttemptHooks:
    """The runner's side of one page's loop, bound to that book and page.

    Attributes:
        attempts: The most attempts the page may have.
        provider_name: Which backend draws, stamped on every attempt's record:
            books outlive a provider switch.
        draws_on_plate: The page is drawn onto a plate, so its figures are
            located and a mend of the previous attempt is possible.
        retry_budget_spent: Whether the book's money says no more redraws.
        locate_figures: Every person and animal on a drawing, for the paste.
        prepare: The plate pasted back and tier 1 run, on the image pool.
        judge: Both review tiers for one picture, as one verdict.
        deadline: The `monotonic()` time the book should be finished by; None
            when the book has no deadline.
    """

    attempts: int
    provider_name: str
    draws_on_plate: bool
    retry_budget_spent: Callable[[], Awaitable[bool]]
    locate_figures: Callable[[bytes, int], Awaitable[Sequence[Box] | None]]
    prepare: Callable[[bytes, Sequence[Box] | None], Awaitable[PreparedPage]]
    judge: Callable[[bytes, Tier1Review, int], Awaitable[Verdict]]
    deadline: float | None = None


async def draw_until_pass(
    hooks: AttemptHooks,
    *,
    book_id: str,
    page_number: int,
    user: str,
    draw: Callable[[str, GeneratedImage | None], Awaitable[GeneratedImage]],
    prompt_for: Callable[[Sequence[str], str, bool], str],
    on_stage: Callable[[str, int], Awaitable[None]] | None = None,
    requested: Sequence[str] = (),
) -> Attempts:
    """Draw one page up to `hooks.attempts` times and keep the best (T9.3).

    The loop is bounded and it stops the moment a review passes: a retry is
    a billable call, and "good enough" is the whole point. A retry carries
    the faults the reviewer named and its own description of what it saw and
    where, so the second attempt is aimed at what was wrong rather than
    being the same prompt with the dice rolled again.

    On a plate page the second attempt mends the first when everything
    wrong with it lives in part of the picture (`FIXABLE_FAULTS`): that
    picture goes back with what is wrong, instead of the page being drawn
    again. The third attempt is always a clean draw from the plate, so a
    mend is never mended. After every plate draw the plate is pasted back
    outside the cast's slots and the page's paint areas, so background drift
    is undone for free instead of failing the page.

    Args:
        draw: One billable attempt, given the prompt to send and the picture
            to mend (None to draw from the plate, or from scratch). The
            prompt is built here, once, so the text recorded is exactly the
            text sent.
        prompt_for: The prompt for the faults and findings to aim at, and
            whether it is a mend of the previous attempt.
        on_stage: Told "painting" or "reviewing" and which attempt it is, so
            the reader's progress says what is happening (T12). Optional:
            a caller with nowhere to report to passes nothing.
        requested: What the book's owner said was wrong when they asked
            for this redraw (`PageFault` names). Every attempt carries it,
            and each attempt's record keeps it, so the operator can see
            what was asked for next to what the reviewer found.

    Returns:
        The chosen image, the prompt that produced it, every attempt's prompt
        and verdict, the chosen attempt number, and whether it was a page
        that never passed (kept anyway, and marked best-effort).
    """
    verdicts: list[dict[str, Any]] = []
    prompts: list[str] = []
    best: tuple[int, int] | None = None  # (passed, score) of the kept attempt
    best_image = b""
    best_prompt = ""
    chosen = 1
    best_index = 0  # the chosen attempt's place in `verdicts` (a blocked one has none)
    asked = tuple(dict.fromkeys(requested))
    faults: tuple[str, ...] = asked
    findings = ""
    # The longest any attempt of this page took, start to verdict: what the
    # next one is expected to need of the book's time.
    longest = 0.0
    # The previous attempt's picture, when it was drawn (not blocked): only
    # that picture can be mended.
    previous: GeneratedImage | None = None
    for attempt in range(1, hooks.attempts + 1):
        if attempt > 1 and best is not None:
            stop = await _stop_reason(hooks, longest)
            if stop is not None:
                if verdicts:
                    verdicts[-1][stop] = True
                logger.info(
                    "book.retry_stopped user=%s book=%s page=%d attempt=%d why=%s",
                    user,
                    book_id,
                    page_number,
                    attempt,
                    stop,
                )
                break
        mend = (
            previous
            if (
                hooks.draws_on_plate
                and attempt == _FIX_ATTEMPT
                and previous is not None
                and faults
                and set(faults) <= FIXABLE_FAULTS
            )
            else None
        )
        kind = "fix" if mend is not None else "first" if attempt == 1 else "redraw"
        prompt = prompt_for(faults, findings, mend is not None)
        previous = None
        if on_stage is not None:
            await on_stage(STAGE_RETRYING if attempt > 1 else STAGE_PAINTING, attempt)
        began = monotonic()
        # Both billable calls of this attempt — the picture and its review —
        # are filed in the cost ledger under this book, page and attempt.
        with cost_scope(
            purpose="book", book_id=book_id, page_number=page_number, attempt=attempt
        ):
            try:
                drawn = await draw(prompt, mend)
            except ProviderError as exc:
                # The provider's safety system refused this picture. That is
                # a verdict on one drawing, not on the book: keep the best
                # earlier attempt, or ask again if there is none yet.
                if "moderation_blocked" not in str(exc):
                    raise
                logger.warning(
                    "book.page_blocked user=%s book=%s page=%d attempt=%d kept_earlier=%s",
                    user,
                    book_id,
                    page_number,
                    attempt,
                    best is not None,
                )
                longest = max(longest, monotonic() - began)
                if best is not None or attempt == hooks.attempts:
                    if best is None:
                        raise
                    break
                continue
            # Where every person and animal is on the drawing, so the
            # plate is never pasted over one, however pale it came out.
            figures = (
                await hooks.locate_figures(drawn.data, attempt) if hooks.draws_on_plate else ()
            )
            # One job on the image pool: the plate pasted back, tier 1 run
            # and the judge's copy made from a single decode (`page_picture`).
            prepared = await hooks.prepare(drawn.data, figures)
            drawn = replace(drawn, data=prepared.image, media_type=prepared.media_type)
            previous = drawn
            image = drawn.data
            if on_stage is not None:
                await on_stage(STAGE_REVIEWING, attempt)
            verdict = await hooks.judge(image, prepared.tier1, attempt)
        longest = max(longest, monotonic() - began)
        verdicts.append(
            {
                **verdict.as_dict(),
                # Which backend drew this attempt: books outlive a provider
                # switch, and "was this page an Agnes page" must be
                # answerable from the page itself.
                "provider": hooks.provider_name,
                "model": drawn.model,
                # A first draw, a mend of the previous attempt, or a redraw.
                "kind": kind,
                **({"plate_paste": prepared.paste} if prepared.paste else {}),
                **({"requested": list(asked)} if asked else {}),
            }
        )
        prompts.append(prompt)
        # Rank on the verdict, never on the attempt number: the earliest
        # attempt wins a tie, so a passing first try is never replaced by an
        # equally-good second that cost money for nothing.
        rank = (1 if verdict.passed else 0, verdict.score)
        if best is None or rank > best:
            best = rank
            best_image, best_prompt, chosen = image, prompt, attempt
            best_index = len(verdicts) - 1
        if verdict.passed:
            break
        faults = tuple(dict.fromkeys((*verdict.faults, *asked)))
        # The reviewer's own words go to the artist with the fault names:
        # "extra_person" says what kind of thing is wrong, "a second girl in
        # a red cloak at the far right" says which one to remove.
        findings = verdict.note if 2 in verdict.tiers_run else ""

    best_effort = not any(verdict["passed"] for verdict in verdicts)
    if best_effort:
        logger.warning(
            "book.best_effort user=%s book=%s page=%d attempts=%d faults=%s",
            user,
            book_id,
            page_number,
            len(verdicts),
            ",".join(verdicts[best_index].get("faults", [])) or "-",
        )
    return Attempts(
        image=best_image,
        prompt=best_prompt,
        prompts=tuple(prompts),
        verdicts=tuple(verdicts),
        chosen=chosen,
        best_effort=best_effort,
    )


async def _stop_reason(hooks: AttemptHooks, longest: float) -> str | None:
    """Why a page with a picture already in hand is not drawn again, if it is not.

    The deadline is asked first because it costs nothing to ask: the retry
    budget reads the ledger. A retry that would end past the deadline is not
    started (A1): the book's time is better spent finishing than polishing one
    page, and the best attempt so far is kept.
    """
    if hooks.deadline is not None and monotonic() + longest > hooks.deadline:
        return "deadline_stopped"
    if await hooks.retry_budget_spent():
        return "retry_budget_stopped"
    return None
