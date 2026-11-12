"""The picture-book rendering job.

Flow of one book, in story order:

    portrait (already approved) -> cover -> pages 1..N together -> PDF

The portrait was made and approved before the book started; the runner sends it
into **every** page alongside the original photo — the same double identity
anchor the portrait pipeline uses. That is the mechanism that keeps the
child the same child from page 1 to page 3: no page is ever generated without
both anchors. Both anchors are copied into the book so a later edit can re-use
them after the original draft has expired.

Pages render **concurrently**: one coroutine per page goes through
`asyncio.gather`, each taking its own lease on the shared concurrency gate, so
the provider sees no more parallel calls than the gate allows. The gate, not
this job, is what bounds the spend — and a page that outlives its deadline
hands its slot to the worker (the same slot-transfer pattern
`api._run_gated` uses) so the gate never believes the server is idle while a
billable call is still running.

The same job backs every edit: one page re-drawn, every page that casts a
replaced face, the cover alone, or the missing pages of a failed book. Only the
finished pages are re-read; the rest stay exactly as they were.

Status is written to the `BookStore` as each page moves pending -> making ->
done, so the browser can poll and show every page's state; a late failure still
leaves the finished pages visible.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from time import monotonic
from typing import Any, TypeVar, cast

from app.errors import BookBusy
from app.models.book import BookPage
from app.models.settings import CartoonSettings, HairColour, HairStyle
from app.models.story import StoryEdition
from app.prompt_data import prompt_text
from app.services.book_document import COVER_KEY, COVER_NUMBER, BookDocument
from app.services.book_notice import BookReadyNotice
from app.services.book_store import (
    INTERRUPTED_MESSAGE,
    BookRecord,
    BookStatus,
    BookStore,
    CharacterReference,
    ResumedBook,
)
from app.services.concurrency import ConcurrencyGate, GateTimeout, Slot
from app.services.costs import CostLedger, cost_scope
from app.services.decision_judge import COIN, Decision, DecisionJudge, Outcome
from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
    ProviderError,
)
from app.services.image_review import review_image
from app.services.media import media_type_for
from app.services.pictures import page_slot, warm_variants
from app.services.pipeline import UnknownGeneration
from app.services.plate_paste import paste_plate_back
from app.services.prompts import (
    build_book_page_prompt,
    build_cover_prompt,
    build_fix_edit_prompt,
    build_plate_edit_prompt,
    correction_for,
    edit_reference_cast,
    render_book_page,
    scene_for,
)
from app.services.reviewer import (
    ImageReviewer,
    ReviewAnswer,
    ReviewerQuotaSpent,
    UnavailableReviewer,
    Verdict,
    verdict_passes,
)
from app.services.storage import GenerationStore
from app.services.template_store import PlateArt, PremadeArt, TemplateStore
from app.services.vitals import rss_mb
from app.services.wallet import OutOfFunds, Wallet

__all__ = ["FIXABLE_FAULTS", "MAX_PAGE_ATTEMPTS", "BookRunner", "CharacterReference"]

#: How many times one page may be drawn before the best attempt is kept (T9.3).
#: Three, and no more: each attempt is a billable call, so the loop must converge
#: on "good enough" rather than search for perfect on a parent's money. A page
#: that never passes keeps its best attempt and is marked `best_effort`, because
#: a book with one imperfect picture is worth more than no book.
MAX_PAGE_ATTEMPTS = 3

#: How long a worker's claim on a book lasts, and how often it is renewed
#: (B9). The lease lives in the database, so a boot can tell a book whose
#: worker died from one another instance is still drawing. It only has to
#: outlive a heartbeat, not the whole render.
_JOB_LEASE_SECONDS = 120.0
_JOB_HEARTBEAT_SECONDS = 30.0


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

logger = logging.getLogger(__name__)

#: How long a reviewer that has just failed is left alone before it is asked again.
_REVIEWER_COOLDOWN = timedelta(minutes=2)

#: Micro-dollars in a cent: the ledger counts in the smallest unit, the two
#: operator knobs (a percentage of the price, an alert in cents) do not.
_MICROUSD_PER_CENT = 10_000

#: How often a book's measured spend is re-read while it renders. A verdict asks
#: for it up to three times a page and the answer barely moves; reading it every
#: time would put a query behind every picture (C4).
_SPEND_REFRESH_SECONDS = 10.0

_T = TypeVar("_T")

# How often a page re-asks the per-minute brake for one billable token while a
# minute is full. Small enough to resume promptly, large enough not to spin.
_PAGE_CHARGE_RETRY_SECONDS = 0.25

#: The phases a book job moves through, as the poll document reports them.
#: The distinction that matters to the reader: the pictures can all be done
#: while the PDF is still being put together, and saying so is the difference
#: between "your book is ready" and a progress bar that stops moving.
_PHASE_PAGES = "pages"
_PHASE_ASSEMBLING = "assembling"

#: The named stages a job reports while it works (T12). A percentage says how
#: much is left; a stage says what is happening, which is the difference between
#: "45% for two minutes" looking stuck and looking like work.
_STAGE_PAINTING = "painting"
_STAGE_REVIEWING = "reviewing"
_STAGE_RETRYING = "retrying"


@dataclass(frozen=True, slots=True)
class _Attempts:
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


def _verdict(attempts: _Attempts) -> dict[str, Any]:
    """The verdict arguments `save_page` and `save_cover` take with the art.

    One write, not two: a page whose art landed and whose verdict did not is a
    book that can never complete and cannot be finished either (B13).
    """
    return {
        "review": [dict(verdict) for verdict in attempts.verdicts],
        "attempt_count": len(attempts.verdicts),
        "chosen_attempt": attempts.chosen,
        "best_effort": attempts.best_effort,
    }


@dataclass(frozen=True, slots=True)
class _Drawn:
    """A finished page as the job keeps it: its formatted text and its size.

    Not the picture: that is saved the moment it is chosen, and holding all
    nineteen until the PDF was built is what filled a 512 MB server.
    """

    page: BookPage
    size: int


@dataclass(frozen=True, slots=True)
class _Artwork:
    """Everything one render needs, reloaded from the book itself."""

    about: object  # AboutMe, kept loose to avoid an import cycle at runtime
    settings: CartoonSettings
    portrait: bytes
    portrait_media_type: str
    photo: bytes
    photo_media_type: str
    characters: Mapping[str, CharacterReference]
    #: The book's pinned plates by page number (0 = cover). Empty when the book
    #: was made on no template version, in which case pages are composed from
    #: scratch exactly as they were before plates existed.
    plates: Mapping[int, PlateArt]
    # True when the photograph is missing and the portrait stands in for it,
    # which happens for books made before the photograph was stored.
    photo_is_portrait: bool = False
    #: The pre-made pictures the book's version pins, by page number: pages the
    #: star is not in, drawn once from their plate. Used only for a page whose
    #: cast the visitor gave no photo of.
    premades: Mapping[int, PremadeArt] = field(default_factory=dict)
    #: The cast keys the visitor gave a photo of (fixed portraits are not these).
    visitor_cast: frozenset[str] = frozenset()
    #: The reviewer's one sentence about the star's hair in the approved
    #: portrait, for a visitor who kept the photo's hair: said on every page so
    #: a hood or a cap does not get new hair under it. "" when not needed or
    #: when there was no reviewer to ask.
    hair_seen: str = ""


class BookRunner:
    """Renders books in the background, pages in parallel, through the gate."""

    def __init__(
        self,
        *,
        provider: ImageProvider,
        store: GenerationStore,
        book_store: BookStore,
        templates: TemplateStore,
        gate: ConcurrencyGate,
        page_timeout_seconds: int,
        wallet: Wallet | None = None,
        reviewer: ImageReviewer | None = None,
        decision_judge: DecisionJudge | None = None,
        page_attempts: int = MAX_PAGE_ATTEMPTS,
        page_concurrency: int = 6,
        image_threads: int = 2,
        costs: CostLedger | None = None,
        book_price_cents: int = 0,
        reviewer_budget_percent: int = 0,
        retry_budget_percent: int = 0,
        cost_alert_cents: int = 0,
        notice: BookReadyNotice | None = None,
    ) -> None:
        self._provider = provider
        # Read once per resume, to log what the restart threw away.
        self._costs = costs
        # How many of one book's pages are in flight at once (a semaphore per
        # job), and a bounded pool of our own for Pillow work, so that work
        # never runs on the event loop or queues the default pool the routes
        # and database calls share (Run 2, 2026-11-11).
        self._page_concurrency = max(1, page_concurrency)
        self._image_pool = ThreadPoolExecutor(
            max_workers=max(1, image_threads), thread_name_prefix="book-image"
        )
        # Tier 2 of the review. `None` means this runner was built without one
        # (tests that predate the loop); the stand-in is used instead so every
        # verdict is stamped `tiers_run: [1]` rather than claiming a review.
        self._reviewer: ImageReviewer = reviewer or UnavailableReviewer()
        # Set when the reviewer said its daily quota is used up: until then it
        # is not asked, and the decision judge decides alone.
        self._reviewer_rests_until: datetime | None = None
        # The confident first look (`decision_judge.py`); None when not configured.
        self._decision_judge = decision_judge
        # A placeholder provider cannot improve on a second try: there is no
        # model to steer, so a retry would be three identical placeholders and
        # three claimed attempts. One attempt, and a verdict that says so.
        self._page_attempts = 1 if provider.draws_placeholders else max(1, page_attempts)
        self._store = store
        self._book_store = book_store
        # Every job reads the book's own pinned edition: its pages, its words,
        # its bible. Nothing here knows which story it is drawing.
        self._templates = templates
        self._gate = gate
        self._page_timeout = page_timeout_seconds
        # None when payments are off: the book is still rendered, and nothing
        # is charged. An owned book made in that mode is simply free.
        self._document = BookDocument(book_store=book_store, wallet=wallet)
        self._tasks: set[asyncio.Task[None]] = set()
        self._active: dict[str, asyncio.Task[None]] = {}
        #: This process's name on the books it renders. The lease, not this id,
        #: is what keeps two workers off one book; the id is how the row says
        #: whose job it is (B9).
        self._owner = _worker_id()
        #: What the book is sold for, in cents: the review budget is a
        #: percentage of it (C4).
        self._book_price_cents = book_price_cents
        #: Percentage of the price the reviewer may spend before it is rested
        #: for that book; 0 never rests it (C4).
        self._reviewer_budget_percent = reviewer_budget_percent
        #: Percentage of the price after which a page is not drawn again: the
        #: best picture it has is kept and the book finishes (C4's retry
        #: ceiling, the other half of the same budget).
        self._retry_budget_percent = retry_budget_percent
        #: The measured spend, in cents, at which one book is reported to the
        #: operator; 0 turns the report off (C4).
        self._cost_alert_cents = cost_alert_cents
        #: The one message a book sends its owner when it is finished (W2);
        #: None when this deployment has no mail channel.
        self._notice = notice
        #: Each book's spend as last read from the ledger, and when; the books
        #: already reported, and those whose budget has already been said out
        #: loud. One line a book, not one per page.
        self._spend: dict[str, tuple[float, int]] = {}
        self._alerted: set[str] = set()
        self._budget_rested: set[str] = set()
        self._retry_rested: set[str] = set()
        self._heartbeats: dict[str, asyncio.Task[None]] = {}
        # Set by `close`: a job cancelled because the server is stopping is
        # paused for the next boot to resume, not failed.
        self._stopping = False

    async def _image_work(self, func: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        """Run CPU-heavy image work on the runner's own bounded pool."""
        return await asyncio.get_running_loop().run_in_executor(
            self._image_pool, partial(func, *args, **kwargs)
        )

    async def _book_spend(self, book_id: str, *, fresh: bool = False) -> int | None:
        """What this book has cost the operator so far, in micro-dollars.

        Read from the ledger rather than counted here: the portraits drawn
        before the book started, its pages, its retries and both tiers are
        already rows, and a book resumed after a restart must not begin its
        budget from zero. None when the ledger cannot be read — a ledger
        hiccup never rests the reviewer or reports a book.
        """
        cached = self._spend.get(book_id)
        now = monotonic()
        if not fresh and cached is not None and now - cached[0] < _SPEND_REFRESH_SECONDS:
            return cached[1]
        if self._costs is None:
            return cached[1] if cached is not None else None
        try:
            totals = await asyncio.to_thread(self._costs.book_costs_microusd, [book_id])
        except Exception:
            logger.warning("book.spend_unavailable book=%s", book_id, exc_info=True)
            return cached[1] if cached is not None else None
        total = totals.get(book_id, 0)
        self._spend[book_id] = (now, total)
        return total

    async def _review_budget_spent(self, book_id: str, user: str) -> bool:
        """True once this book has spent its review budget (C4).

        `REVIEWER_BUDGET_PERCENT` is a share of the book's own fee; past it,
        tier 2 is rested for the rest of this book and tier 1 and the decision
        judge decide. The book keeps every picture it has: the reviewer's
        words are what a tight budget gives up, never a picture already drawn.
        A verdict that ran without tier 2 says so in its note.
        """
        if self._reviewer_budget_percent <= 0 or self._book_price_cents <= 0:
            return False
        spend = await self._book_spend(book_id)
        if spend is None:
            return False
        budget = (
            self._book_price_cents * _MICROUSD_PER_CENT * self._reviewer_budget_percent // 100
        )
        if spend < budget:
            return False
        if book_id not in self._budget_rested:
            self._budget_rested.add(book_id)
            logger.warning(
                "book.review_budget user=%s book=%s spend_usd=%.4f budget_usd=%.4f "
                "outcome=rested",
                user,
                book_id,
                spend / 1_000_000,
                budget / 1_000_000,
            )
        return True

    async def _retry_budget_spent(self, book_id: str, user: str) -> bool:
        """True once this book may not pay for another drawing (C4).

        `BOOK_RETRY_BUDGET_PERCENT` is a share of the book's own fee, the same
        arithmetic as the reviewer's budget: past it, a page that did not pass
        is not drawn again — the best attempt it already has is kept, marked
        best-effort, and the book still finishes. A first draw is never
        stopped: a page with no picture at all would leave a hole in the book,
        and the ceiling is about retries, which are where the money goes (31 of
        a real book's 52 image calls). A ledger that cannot be read never stops
        a retry.
        """
        if self._retry_budget_percent <= 0 or self._book_price_cents <= 0:
            return False
        spend = await self._book_spend(book_id)
        if spend is None:
            return False
        budget = (
            self._book_price_cents * _MICROUSD_PER_CENT * self._retry_budget_percent // 100
        )
        if spend < budget:
            return False
        if book_id not in self._retry_rested:
            self._retry_rested.add(book_id)
            logger.warning(
                "book.retry_budget user=%s book=%s spend_usd=%.4f budget_usd=%.4f "
                "outcome=no_more_redraws",
                user,
                book_id,
                spend / 1_000_000,
                budget / 1_000_000,
            )
        return True

    async def _report_book_cost(self, book_id: str, user: str) -> None:
        """Tell the operator when a book cost more than the alert allows.

        Runs when a job ends however it ends: a book stopped half-drawn has
        spent money too. This deployment has no mail channel (W7), so the
        report is one `book.cost_alert` line the operator's log keeps, and
        `/admin/status` draws its banner from the same threshold. Never
        raises: the job's own ending matters more than the notice.
        """
        limit = self._cost_alert_cents * _MICROUSD_PER_CENT
        if limit <= 0 or book_id in self._alerted:
            return
        try:
            spend = await self._book_spend(book_id, fresh=True)
        except Exception:
            logger.warning("book.cost_alert_failed book=%s", book_id, exc_info=True)
            return
        if spend is None or spend < limit:
            return
        self._alerted.add(book_id)
        logger.error(
            "book.cost_alert user=%s book=%s spend_usd=%.4f alert_usd=%.2f",
            user,
            book_id,
            spend / 1_000_000,
            limit / 1_000_000,
        )

    def _forget_book_costs(self, book_id: str) -> None:
        """Drop one book's spend cache when its job ends: the ledger keeps it."""
        self._spend.pop(book_id, None)
        self._budget_rested.discard(book_id)
        self._retry_rested.discard(book_id)
        self._alerted.discard(book_id)

    async def _claim(self, book_id: str) -> None:
        """Take the book's job lease, or refuse because a live worker has it.

        The in-process `_active` check only knows this process. The lease is a
        conditional write in the database, so a second instance cannot start
        drawing the same book behind this one's back (B9).
        """
        claimed = await asyncio.to_thread(
            self._book_store.claim_job,
            book_id,
            self._owner,
            ttl_seconds=_JOB_LEASE_SECONDS,
        )
        if not claimed:
            raise BookBusy("This book is already being worked on. Wait for it to finish.")

    async def _keep_lease(self, book_id: str) -> None:
        """Renew the book's lease while its job runs; stop the job if it is lost.

        A False from the store means the book is no longer ours — the lease ran
        out and the boot resume (or another instance) took it — so the job is
        cancelled rather than paying for pictures nobody will keep.
        """
        while True:
            await asyncio.sleep(_JOB_HEARTBEAT_SECONDS)
            try:
                renewed = await asyncio.to_thread(
                    self._book_store.renew_job,
                    book_id,
                    self._owner,
                    ttl_seconds=_JOB_LEASE_SECONDS,
                )
            except Exception:
                logger.warning("book.lease_renew_failed id=%s", book_id, exc_info=True)
                continue
            if renewed:
                continue
            logger.warning("book.lease_lost id=%s owner=%s", book_id, self._owner)
            task = self._active.get(book_id)
            if task is not None and not task.done():
                task.cancel()
            return

    async def _warm_picture(self, book_id: str, slot: str) -> None:
        """Make a picture's usual screen copies while the book is still rendering.

        The full-size picture is what the PDF embeds, but a reader wants the
        240 px thumbnail and the 800 px page; made lazily, they would be 42
        encodes on the first view of a 21-page book, one at a time, while
        somebody waits (S2/B12). The 1600 px copy stays lazy. A failure here is
        logged and never fails the page: the picture is saved, and the media
        route can still make the copy on demand.
        """
        try:
            await self._image_work(warm_variants, self._book_store, book_id, slot)
        except Exception:
            logger.warning("book.warm_failed book=%s slot=%s", book_id, slot, exc_info=True)

    async def close(self) -> None:
        """Stop for a shutdown: pause every running book, then free the pool.

        A deploy stops the process gracefully, so the jobs are cancelled here
        rather than torn down with the loop. Each one is left as it was —
        rendering, its finished pages kept — and the next boot resumes it
        (`recover`). Called once, from the app's lifespan.
        """
        self._stopping = True
        for beat in list(self._heartbeats.values()):
            beat.cancel()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        # Each job's done callback releases its lease, so a redeploy hands its
        # books straight back to the next boot (B9).
        await asyncio.gather(*tasks, return_exceptions=True)
        self._image_pool.shutdown(wait=False, cancel_futures=True)

    @property
    def reviewer_name(self) -> str:
        """Which tier-2 reviewer judges pictures; "off" when none does."""
        return self._reviewer.name

    @property
    def reviewer_resting_until(self) -> datetime | None:
        """When a reviewer with a spent daily quota is next asked; None if not resting."""
        until = self._reviewer_rests_until
        if until is not None and datetime.now(UTC) >= until:
            self._reviewer_rests_until = until = None
        return until

    @property
    def decision_judge_name(self) -> str:
        """Which decision model looks first; "off" when none does."""
        return self._decision_judge.name if self._decision_judge is not None else "off"

    @property
    def page_attempts(self) -> int:
        """How many times one picture may be drawn before the best is kept."""
        return self._page_attempts

    async def start(
        self,
        record: BookRecord,
        characters: Mapping[str, CharacterReference] | None = None,
        page_charge: Callable[[], bool] | None = None,
    ) -> None:
        """Kick off rendering. Raises if the draft with the portrait is gone.

        The portrait, source photograph and character faces are copied into the
        book immediately: from here on the book owns its art, independent of
        draft expiry and cleanup, and it can be edited later.
        """
        draft = await asyncio.to_thread(self._store.load_draft, record.generation_id)
        if draft is None:
            raise UnknownGeneration(record.generation_id)
        await asyncio.to_thread(
            self._book_store.save_portrait, record.book_id, draft.cartoon_image
        )
        await asyncio.to_thread(
            self._book_store.save_source_photo, record.book_id, draft.photo_data
        )
        resolved_characters = dict(characters or {})
        await asyncio.to_thread(
            self._book_store.save_characters, record.book_id, resolved_characters
        )
        await self._claim(record.book_id)
        self._schedule(
            record.book_id,
            lambda: self._render(
                record.book_id,
                page_charge=page_charge,
                page_numbers=None,
                draw_cover=True,
                only_missing=False,
                requested_fixes={},
                # A new book's pictures are inside its own fee.
                picture_payment=None,
                resumed=False,
            ),
        )

    async def start_edit(
        self,
        book_id: str,
        *,
        page_numbers: set[int] | None,
        draw_cover: bool,
        only_missing: bool = False,
        requested_fixes: Mapping[int, Sequence[str]] | None = None,
        page_charge: Callable[[], bool] | None = None,
        picture_payment: Callable[[str], None] | None = None,
        resumed: bool = False,
    ) -> None:
        """Mark the edit as rendering, then schedule it; returns before any call.

        The status write happens before the response, so a browser polling
        immediately after the 202 can never see the *previous* `complete`
        document and conclude the edit already finished.

        The book's one job slot is reserved *before* that status write. If it
        were not, a second edit arriving while the first was still waiting on
        its status write would get its 409 only after rewriting the running
        job's poll document — resetting a `making` page to `pending`, or
        putting an assembling book back into the pages phase.

        `page_numbers=None` means every story page. `only_missing` leaves pages
        that already finished untouched (the retry path). `requested_fixes`
        maps a page to what its owner said was wrong with it — `PageFault`
        names, the same vocabulary the reviewer uses — and steers every attempt
        at that page, the first one included. `picture_payment` is asked for
        the price of each picture the job is about to draw, and raises
        `OutOfFunds` when the wallet cannot cover it: the job then stops with
        the pictures already made kept, rather than drawing on credit. `resumed`
        is a restart picking the job up again (`recover`): its window and resume
        count are kept, and finished pictures outside the job's own pages count
        as done.
        """
        self._ensure_idle(book_id)
        await self._claim(book_id)
        fixes = dict(requested_fixes or {})
        started = asyncio.Event()

        async def job() -> None:
            # Released by `finally` below, after the status write has either
            # succeeded or failed; the book is not touched before then.
            await started.wait()
            await self._render(
                book_id,
                page_charge=page_charge,
                page_numbers=page_numbers,
                draw_cover=draw_cover,
                only_missing=only_missing,
                requested_fixes=fixes,
                picture_payment=picture_payment,
                resumed=resumed,
            )

        try:
            task = self._schedule(book_id, job)
        except BaseException:
            # The lease was taken for a job that never started: give it back now
            # rather than leaving the book locked out until it expires.
            await asyncio.to_thread(self._book_store.release_job, book_id, self._owner)
            raise
        try:
            await asyncio.to_thread(
                partial(
                    self._mark_rendering,
                    book_id,
                    page_numbers,
                    draw_cover=draw_cover,
                    only_missing=only_missing,
                    requested_fixes=fixes,
                    resumed=resumed,
                )
            )
        except BaseException:
            task.cancel()
            raise
        finally:
            started.set()

    def _mark_rendering(
        self,
        book_id: str,
        page_numbers: set[int] | None,
        *,
        draw_cover: bool,
        only_missing: bool,
        requested_fixes: Mapping[int, Sequence[str]],
        resumed: bool,
    ) -> None:
        """Publish the edit's starting status before the job runs.

        A new job also starts the book's resume window here (`begin_job`); a
        resumed one keeps the window and the count it already has.
        """
        record = self._book_store.load_record(book_id)
        if record is None:
            return
        if not resumed:
            self._book_store.begin_job(book_id)
        edition = self._templates.book_edition(book_id)
        rendered_pages = _rendered(edition, record)
        progress, target, cover = self._plan(
            book_id,
            edition,
            page_numbers=page_numbers,
            draw_cover=draw_cover,
            only_missing=only_missing,
            resumed=resumed,
        )
        self._book_store.save_status(
            book_id,
            BookStatus.RENDERING,
            self._document.page_view(book_id, progress, rendered_pages),
            title=record.title,
            extra={
                "phase": _PHASE_PAGES,
                "total_pages": len(rendered_pages) + 1,
                "job": _job_note(target, cover, requested_fixes),
            },
        )

    def _plan(
        self,
        book_id: str,
        edition: StoryEdition,
        *,
        page_numbers: set[int] | None,
        draw_cover: bool,
        only_missing: bool,
        resumed: bool,
    ) -> tuple[dict[int, str], set[int], bool]:
        """Where a job starts: each page's progress, the pages to draw, the cover.

        A page is done when the stored status says so. A resumed job also
        counts every finished picture outside its own pages as done: those
        are not its to redraw, whatever an interrupted status write said. A
        picture the job is redrawing is not done until its new bytes land.
        """
        numbers = {page.number for page in edition.pages}
        done = {
            number
            for number, state in self._document.stored_progress(book_id).items()
            if state == "done"
        }
        target = set(numbers) if page_numbers is None else set(page_numbers)
        if resumed:
            job = target | ({COVER_NUMBER} if draw_cover else set())
            done |= self._book_store.saved_page_numbers(book_id) - job
        # A picture whose verdict never landed is not finished: the store refuses
        # to complete a book with one, so Finish and a resume draw it again
        # rather than leaving the book stuck with no way forward (B13).
        done -= self._book_store.unreviewed_pages(book_id)
        if only_missing:
            target -= done
            draw_cover = draw_cover and COVER_NUMBER not in done
        progress = {
            number: "pending" if number in target or number not in done else "done"
            for number in numbers
        }
        progress[COVER_NUMBER] = (
            "pending" if draw_cover or COVER_NUMBER not in done else "done"
        )
        return progress, target, draw_cover

    async def recover(self, *, max_resumes: int, window_minutes: int) -> list[str]:
        """Pick up the books a restart interrupted; called once, at boot.

        Each is rendered again as the job it was — the same pages, the same
        fixes its owner asked for — skipping the pages already finished. The
        wallet is charged when the book completes, once per book whatever the
        number of resumes (`BookDocument.assemble`). A book the store will not
        resume again is failed there, its owner told to finish it.

        Returns the ids of the books resumed.
        """
        books = await asyncio.to_thread(
            partial(
                self._book_store.resume_incomplete,
                owner=self._owner,
                ttl_seconds=_JOB_LEASE_SECONDS,
                max_resumes=max_resumes,
                window_seconds=window_minutes * 60,
            )
        )
        for book in books:
            try:
                await self._resume(book)
            except BookBusy:
                # Another instance holds it: not ours to fail, and not ours to
                # draw (B9).
                logger.warning("book.resume_skipped id=%s (another worker)", book.book_id)
                continue
            except Exception:
                logger.exception("book.resume_failed id=%s", book.book_id)
                await self._fail_off_loop(
                    book.book_id, INTERRUPTED_MESSAGE, user="-", elapsed_ms=0.0
                )
        return [book.book_id for book in books]

    async def _resume(self, book: ResumedBook) -> None:
        status = await asyncio.to_thread(self._book_store.load_status, book.book_id) or {}
        pages, cover, fixes = _read_job_note(status.get("job"))
        lost = await asyncio.to_thread(self._lost_pictures, book, pages, cover)
        logger.warning(
            "book.resumed id=%s resume=%d pages=%s cover=%s lost_pictures=%d",
            book.book_id,
            book.resume,
            "all" if pages is None else ",".join(str(number) for number in sorted(pages)),
            cover,
            lost,
        )
        await self.start_edit(
            book.book_id,
            page_numbers=pages,
            draw_cover=cover,
            only_missing=True,
            requested_fixes=fixes,
            resumed=True,
        )

    def _lost_pictures(self, book: ResumedBook, pages: set[int] | None, cover: bool) -> int:
        """Pictures the interrupted job paid for that never became a finished page."""
        if self._costs is None:
            return 0
        numbers = (
            {page.number for page in self._templates.book_edition(book.book_id).pages}
            if pages is None
            else set(pages)
        )
        if cover:
            numbers.add(COVER_NUMBER)
        done = {
            number
            for number, state in self._document.stored_progress(book.book_id).items()
            if state == "done"
        }
        return self._costs.pictures_drawn(
            book.book_id, pages=numbers - done, since=book.job_started_at
        )

    def _ensure_idle(self, book_id: str) -> None:
        """Refuse a second job for a book that already has one running.

        Synchronous on purpose: callers check and reserve without an `await`
        in between, so two requests cannot both pass the check before either
        has registered its task.
        """
        current = self._active.get(book_id)
        if current is not None and not current.done():
            raise BookBusy("This book is already being worked on. Wait for it to finish.")

    def _schedule(
        self, book_id: str, make_coro: Callable[[], Coroutine[Any, Any, None]]
    ) -> asyncio.Task[None]:
        """Run one job per book; refuse a second concurrent edit of it."""
        self._ensure_idle(book_id)
        task = asyncio.create_task(make_coro())
        self._active[book_id] = task
        self._tasks.add(task)
        # The claim is the store's; the heartbeat is what keeps it while a
        # twenty-page book is drawn (B9).
        self._heartbeats[book_id] = asyncio.create_task(self._keep_lease(book_id))

        def _finished(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            self._forget_book_costs(book_id)
            if self._active.get(book_id) is done:
                self._active.pop(book_id, None)
            beat = self._heartbeats.pop(book_id, None)
            if beat is not None:
                beat.cancel()
            # Released whoever ended the job — done, failed, cancelled — so the
            # next boot or instance may pick the book up.
            try:
                done.get_loop().run_in_executor(
                    None, self._book_store.release_job, book_id, self._owner
                )
            except RuntimeError:
                logger.warning("book.lease_release_failed id=%s", book_id, exc_info=True)

        task.add_done_callback(_finished)
        return task

    async def cancel(self, book_id: str) -> bool:
        """Stop the running job for `book_id`, and wait for it to unwind.

        Called when the book is about to be deleted: the job must not keep
        making billable calls, and its cancellation handler must not write a
        status into a book that is going away. Waiting for the task is what
        makes the second half true — a delete that raced the handler could
        find the file it was removing rewritten behind it. The wait is short:
        the cancellation path does one record read and one status write, and
        an in-flight provider call is abandoned, not awaited.

        Returns whether a job was actually cancelled.
        """
        task = self._active.pop(book_id, None)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # The job's own cancellation is expected. The request being
            # cancelled while it waits is not ours to swallow.
            if not task.cancelled():
                raise
        return True

    def rebuild_pdf(self, book_id: str) -> None:
        """Re-assemble the PDF from the pages already on disk (no model calls).

        Used by the cheap About edit: only the PDF text changes, so re-drawing
        an image would be waste. Raises if the book is not complete yet; the
        next normal render will pick the new text up.
        """
        record = self._book_store.load_record(book_id)
        if record is None:
            return
        edition = self._templates.book_edition(book_id)
        rendered_pages = _rendered(edition, record)
        self._document.assemble(
            record,
            rendered_pages,
            self._document.stored_progress(book_id),
            subtitle=edition.subtitle_for(record.about.prompt_name),
        )

    # --- the job ----------------------------------------------------------------

    async def _render(
        self,
        book_id: str,
        *,
        page_charge: Callable[[], bool] | None,
        page_numbers: set[int] | None,
        draw_cover: bool,
        only_missing: bool,
        requested_fixes: Mapping[int, Sequence[str]],
        picture_payment: Callable[[str], None] | None,
        resumed: bool,
    ) -> None:
        loaded = await asyncio.to_thread(self._book_store.load_record, book_id)
        if loaded is None:
            logger.warning("book.edit_missing id=%s", book_id)
            return
        # Bound to a name mypy can narrow: the page and cover closures below use
        # it, and a variable that a nested function captures keeps its union
        # type however it was checked outside.
        record = loaded
        user = record.user_id or "-"
        started = monotonic()
        # "Your book is ready" is a message about a book that has just been
        # finished. An edit of a book that was already complete redraws a
        # picture and must not send it again (W2).
        before = await asyncio.to_thread(self._book_store.load_status, book_id)
        announce = (before or {}).get("status") != BookStatus.COMPLETE
        try:
            artwork = await asyncio.to_thread(self._artwork, record)
            artwork = replace(
                artwork, hair_seen=await self._hair_seen(artwork, book_id=book_id, user=user)
            )
            edition = await asyncio.to_thread(self._templates.book_edition, book_id)
            pages = list(edition.pages)
            total = len(pages)
            prompt_name = record.about.prompt_name
            rendered_pages = tuple(
                render_book_page(page, artwork.settings, prompt_name=prompt_name)
                for page in pages
            )
            # The cover counts as one of the book's pages, drawn alongside them.
            progress, target, draw_cover = await asyncio.to_thread(
                partial(
                    self._plan,
                    book_id,
                    edition,
                    page_numbers=page_numbers,
                    draw_cover=draw_cover,
                    only_missing=only_missing,
                    resumed=resumed,
                )
            )
            # What this job is, kept in every status write: a restart resumes
            # exactly it, with the fixes its owner asked for (`recover`).
            job_note = _job_note(target, draw_cover, requested_fixes)
            status_lock = asyncio.Lock()
            # One book's pages in flight at once; the rest wait their turn
            # here, holding nothing, before any picture is loaded for them.
            in_flight = asyncio.Semaphore(self._page_concurrency)
            phase = _PHASE_PAGES
            costs: dict[int, tuple[str, int, int]] = {}
            # Which stage each page is in, by page number. Kept per page rather
            # than as one global value because the pages run in parallel: a
            # single "current page" would name one that had already finished by
            # the time the status was written, and the reader would be told about
            # work that is over (T12).
            stages: dict[int, str] = {}
            attempts_by_page: dict[int, int] = {}
            # Every attempt that came back as a picture, by (page, attempt):
            # pictures drawn runs ahead of pages accepted, and the reader is
            # shown both rather than a count that sits still while the
            # reviewer turns pictures down.
            drawn_pictures: set[tuple[int, int]] = set()

            def page_stages() -> dict[str, dict[str, object]]:
                """Each page still in flight: its stage and which try it is on."""
                if phase != _PHASE_PAGES:
                    return {}
                return {
                    str(number): {
                        "stage": name,
                        "attempt": attempts_by_page.get(number, 1),
                    }
                    for number, name in sorted(stages.items())
                    if progress.get(number) != "done"
                }

            def stage_now() -> dict[str, object]:
                """The most interesting page still in flight, as a stage triple."""
                if phase != _PHASE_PAGES:
                    # Nothing is being drawn any more: assembling the PDF is the
                    # whole story, and no page may still be named.
                    return {
                        "stage": phase,
                        "stage_page": None,
                        "stage_attempt": 1,
                        "stage_counts": {},
                    }
                # How many pages are in each stage right now. Pages run in
                # parallel, so one named page undersells the work: "4 being
                # drawn, 3 being checked" is what is actually happening.
                counts: dict[str, int] = {}
                for number, name in stages.items():
                    if progress.get(number) != "done":
                        counts[name] = counts.get(name, 0) + 1
                # Reviewing is more worth reporting than painting: it is the
                # stage a reader has never seen before, and the one that
                # explains a pause after a picture appears.
                for wanted in (_STAGE_REVIEWING, _STAGE_RETRYING, _STAGE_PAINTING):
                    for number in sorted(stages):
                        if stages[number] != wanted:
                            continue
                        if progress.get(number) == "done":
                            continue
                        return {
                            "stage": wanted,
                            "stage_page": number,
                            "stage_attempt": attempts_by_page.get(number, 1),
                            "stage_counts": counts,
                        }
                return {
                    "stage": _STAGE_PAINTING,
                    "stage_page": None,
                    "stage_attempt": 1,
                    "stage_counts": counts,
                }

            async def paint() -> None:
                async with status_lock:
                    await asyncio.to_thread(
                        self._book_store.save_status,
                        book_id,
                        BookStatus.RENDERING,
                        self._document.page_view(book_id, progress, rendered_pages),
                        None,
                        title=record.title,
                        extra={
                            "phase": phase,
                            "total_pages": total + 1,
                            **stage_now(),
                            "page_stages": page_stages(),
                            "pictures_drawn": len(drawn_pictures),
                            "page_attempts": self._page_attempts,
                            "job": job_note,
                        },
                    )

            async def set_stage(name: str, page_number: int, attempt: int = 1) -> None:
                stages[page_number] = name
                attempts_by_page[page_number] = attempt
                if name == _STAGE_REVIEWING:
                    # Reviewing starts the moment an attempt's picture is in.
                    drawn_pictures.add((page_number, attempt))
                await paint()

            async def mark(page_number: int, state: str) -> None:
                progress[page_number] = state
                if state in ("done", "failed"):
                    # A page that is finished or has given up is not in a stage
                    # any more, whatever it was doing a moment ago.
                    stages.pop(page_number, None)
                await paint()

            async def _render_page(page: BookPage) -> _Drawn:
                premade = _premade_for(page, artwork, requested_fixes)
                if premade is not None:
                    return await _use_premade(page, premade)
                # Paid for once per page, before any attempt: a retry the
                # reviewer causes is the app's own quality control and is part
                # of what the one charge buys.
                await _pay_for_picture(picture_payment, f"Page {page.number}")
                page_photos = [name for name in page.cast if name in artwork.characters]
                photo_only = frozenset(
                    ([""] if artwork.photo_is_portrait and page.star_in_picture else [])
                    + [
                        n
                        for n in page_photos
                        if artwork.characters[n].photo == artwork.characters[n].portrait
                    ]
                )
                plate = artwork.plates.get(page.number)

                def prompt_for(faults: Sequence[str], findings: str, fix: bool) -> str:
                    """The prompt for one attempt, aimed at what is known to be wrong.

                    `faults` is the owner's request (on a redraw) plus whatever
                    the review found in the previous attempt; `findings` is the
                    reviewer's own description of that attempt. `fix` asks for
                    the mend of that attempt rather than a draw from the plate.
                    """
                    if plate is not None and fix:
                        return build_fix_edit_prompt(
                            page,
                            plate.slots,
                            story=edition,
                            settings=artwork.settings,
                            prompt_name=prompt_name,
                            faults=faults,
                            findings=findings,
                            supplied_characters=tuple(artwork.characters),
                            hair_seen=artwork.hair_seen,
                        )
                    if plate is not None:
                        # The edit pass (T8): the approved plate is the world and
                        # the first reference, the cast is painted into its slots.
                        return build_plate_edit_prompt(
                            page,
                            plate.slots,
                            story=edition,
                            settings=artwork.settings,
                            prompt_name=prompt_name,
                            supplied_characters=tuple(artwork.characters),
                            retry_faults=faults,
                            retry_findings=findings,
                            hair_seen=artwork.hair_seen,
                        )
                    # No pinned plate: compose the page from scratch.
                    return build_book_page_prompt(
                        page,
                        artwork.settings,
                        story=edition,
                        prompt_name=prompt_name,
                        supplied_characters=tuple(artwork.characters),
                        character_photos=page_photos,
                        photo_only=photo_only,
                        correction=correction_for(faults, findings),
                        hair_seen=artwork.hair_seen,
                    )

                if plate is not None:
                    references, media_types = _plate_references(
                        artwork, plate, page, star=edition.star
                    )
                else:
                    references, media_types = _references(artwork, page)

                async def draw(prompt: str, base: GeneratedImage | None) -> GeneratedImage:
                    # Charged per attempt, which is what an attempt costs. The
                    # charge happens before the call, so a retry cannot slip past
                    # a balance that has run out.
                    await _await_page_charge(page_charge)

                    async def on_slot() -> None:
                        await mark(page.number, "making")

                    sent, sent_types = _with_base(references, media_types, base)
                    return await self._on_worker_thread_with_slot(
                        self._page_call(
                            sent, sent_types, prompt, artwork.settings, "book-page"
                        ),
                        on_slot=on_slot,
                    )

                attempts = await self._draw_until_pass(
                    book_id=book_id,
                    page_number=page.number,
                    user=user,
                    page_title=page.scene.title,
                    story_line=_picture_brief(page, artwork.settings),
                    cast_note=_cast_note(page, edition),
                    star_in_picture=page.star_in_picture,
                    plate=plate,
                    artwork=artwork,
                    draw=draw,
                    prompt_for=prompt_for,
                    paint_areas=page.paint_areas,
                    on_stage=lambda name, attempt: set_stage(name, page.number, attempt),
                    requested=tuple(requested_fixes.get(page.number, ())),
                )
                await asyncio.to_thread(
                    self._book_store.save_page,
                    book_id,
                    page.number,
                    attempts.image,
                    **_verdict(attempts),
                )
                await self._warm_picture(book_id, page_slot(page.number))
                await self._record_prompts(book_id, page.number, page.key or "", attempts)
                costs[page.number] = (
                    "edit" if plate is not None else "compose",
                    len(attempts.prompt),
                    len(references),
                )
                await mark(page.number, "done")
                # Only the size is kept: the picture is in the store, and the
                # PDF reads it back from there (BookDocument.assemble).
                return _Drawn(
                    page=render_book_page(page, artwork.settings, prompt_name=prompt_name),
                    size=len(attempts.image),
                )

            async def _use_premade(page: BookPage, premade: PremadeArt) -> _Drawn:
                """Put the page's pre-made picture on it: no image call, no charge.

                It was approved by an operator when it was made, so it is not
                reviewed again; the verdict says so, and names the picture used.
                """
                premade_attempts = _premade_attempts(premade)
                await asyncio.to_thread(
                    self._book_store.save_page,
                    book_id,
                    page.number,
                    premade.image,
                    **_verdict(premade_attempts),
                )
                await self._warm_picture(book_id, page_slot(page.number))
                await self._record_prompts(
                    book_id,
                    page.number,
                    page.key or "",
                    premade_attempts,
                )
                costs[page.number] = ("premade", 0, 0)
                await mark(page.number, "done")
                return _Drawn(
                    page=render_book_page(page, artwork.settings, prompt_name=prompt_name),
                    size=len(premade.image),
                )

            async def run_page(page: BookPage) -> _Drawn:
                async with in_flight:
                    return await _run_page(page)

            async def _run_page(page: BookPage) -> _Drawn:
                page_started = monotonic()
                try:
                    art = await _render_page(page)
                except BaseException as exc:
                    logger.warning(
                        "book.page user=%s book=%s page=%d/%d key=%s "
                        "ms=%.0f rss_mb=%.0f outcome=failed error=%s",
                        user,
                        book_id,
                        page.number,
                        total,
                        page.key,
                        (monotonic() - page_started) * 1000,
                        rss_mb(),
                        type(exc).__name__,
                    )
                    raise
                # `mode` and `prompt_chars` are the cost record: an edit pass
                # sends about a fifth of the text a from-scratch page sends, and
                # `refs` says how many images went with it. A real run's log is
                # what settles whether editing is cheaper, rather than anyone's
                # estimate — see "What the edit pass costs" in the README.
                mode, prompt_chars, references = costs.get(page.number, ("compose", 0, 0))
                logger.info(
                    "book.page user=%s book=%s page=%d/%d key=%s mode=%s "
                    "prompt_chars=%d refs=%d bytes=%d ms=%.0f rss_mb=%.0f outcome=done",
                    user,
                    book_id,
                    page.number,
                    total,
                    page.key,
                    mode,
                    prompt_chars,
                    references,
                    art.size,
                    (monotonic() - page_started) * 1000,
                    rss_mb(),
                )
                return art

            await paint()
            to_render = [page for page in pages if page.number in target]

            # The cover is drawn alongside the pages, not before them: no page
            # uses it as a reference, so waiting for it only added a whole
            # draw-and-check cycle to every book.
            # The cover takes its turn among the pages: it is as much work.
            async def run_cover() -> bytes:
                async with in_flight:
                    return await self._render_cover(
                        record,
                        edition,
                        artwork,
                        page_charge,
                        mark,
                        set_stage,
                        picture_payment=picture_payment,
                    )

            cover_job = [run_cover()] if draw_cover else []
            rendered: dict[int, BookPage] = {}
            if to_render or cover_job:
                results = await asyncio.gather(
                    *cover_job,
                    *(run_page(page) for page in to_render),
                    return_exceptions=True,
                )
                # A cover that could not be drawn still stops the job: it is
                # the face of the book, and "Finish this book" draws it later
                # from every page that was paid for. A story page is different
                # (B2): it is marked failed and the book is assembled with a
                # clearly marked page in its place, so one provider timeout or
                # moderation block never throws away twenty drawn pictures.
                for result in results[: len(cover_job)]:
                    if isinstance(result, BaseException):
                        raise result
                page_results = results[len(cover_job) :]
                for page, result in zip(to_render, page_results, strict=True):
                    if isinstance(result, BaseException):
                        if not isinstance(result, Exception):
                            raise result  # a cancellation must still stop the job
                        if isinstance(result, OutOfFunds):
                            # Money, not a bad picture: stop with what exists so
                            # the wallet can be topped up and the book finished.
                            raise result
                        if (
                            await asyncio.to_thread(
                                self._book_store.read_page, book_id, page.number
                            )
                            is not None
                        ):
                            # The picture landed before the failure (a prompt
                            # record write, say): keep it, never bill it twice.
                            rendered[page.number] = render_book_page(
                                page, artwork.settings, prompt_name=prompt_name
                            )
                            await mark(page.number, "done")
                            continue
                        # The page's own `book.page ... outcome=failed` line was
                        # already written by the run that raised.
                        await mark(page.number, "failed")
                        continue
                    drawn = cast("_Drawn", result)
                    rendered[drawn.page.number] = drawn.page

            # The pictures are done; the book is not. Say so, so the reader is
            # told the PDF is being put together instead of watching a
            # progress bar that has stopped moving.
            phase = _PHASE_ASSEMBLING
            await paint()
            # The About page and the title are written from the freshest
            # record, not the one this job started with: an About edit made
            # while the pictures were drawing used to be overwritten by the
            # job's own snapshot when the PDF was assembled (B10). Only the
            # words change here; the pictures are the ones this job drew.
            latest = await asyncio.to_thread(self._book_store.load_record, book_id)
            if latest is not None:
                record = latest
            pdf_bytes = await asyncio.to_thread(
                partial(
                    self._document.assemble,
                    record,
                    rendered_pages,
                    progress,
                    rendered,
                    subtitle=edition.subtitle_for(record.about.prompt_name),
                )
            )
            logger.info(
                "book.complete user=%s book=%s pages=%d pdf_bytes=%d ms=%.0f "
                "rss_mb=%.0f outcome=complete",
                user,
                book_id,
                total,
                pdf_bytes,
                (monotonic() - started) * 1000,
                rss_mb(),
            )
            if announce:
                await self._tell_the_parent(record, user=user)
        except asyncio.CancelledError:
            # A shutdown pauses the job: the book stays rendering, and the next
            # boot resumes it. A delete cancels the job and removes the book,
            # leaving nothing to write. Any other cancel fails the book, its
            # finished pages kept.
            if self._stopping:
                logger.warning(
                    "book.paused id=%s user=%s elapsed_ms=%.0f",
                    book_id,
                    user,
                    (monotonic() - started) * 1000,
                )
            elif await asyncio.to_thread(self._book_store.load_record, book_id) is not None:
                self._fail(
                    book_id,
                    "Rendering was cancelled before the book finished. The pages "
                    "already finished were kept — starting a new book is safe, the "
                    "same portrait is reused.",
                    user=user,
                    elapsed_ms=(monotonic() - started) * 1000,
                )
            raise
        except GateTimeout:
            await self._fail_off_loop(
                book_id,
                "The server was too busy to render this book's pages, so it "
                "stopped. The pages already finished were made and paid for — "
                "starting a new book is safe, the same portrait is reused.",
                user=user,
                elapsed_ms=(monotonic() - started) * 1000,
            )
        except TimeoutError:
            await self._fail_off_loop(
                book_id,
                "A page took too long to generate, so the book was stopped. The "
                "pages already finished were kept.",
                user=user,
                elapsed_ms=(monotonic() - started) * 1000,
            )
        except OutOfFunds as exc:
            # A redraw is a purchase: the wallet could not cover the next
            # picture, so nothing more was drawn and everything made so far is
            # kept. The book page shows this sentence and the way back.
            logger.warning("book.out_of_funds user=%s book=%s", user, book_id)
            await self._fail_off_loop(
                book_id,
                str(exc),
                user=user,
                elapsed_ms=(monotonic() - started) * 1000,
                code="out_of_funds",
            )
        except Exception as exc:  # keep the completed pages visible whatever happened
            logger.exception("book.failed user=%s book=%s", user, book_id)
            await self._fail_off_loop(
                book_id,
                f"The book stopped early: {exc}",
                user=user,
                elapsed_ms=(monotonic() - started) * 1000,
            )
        finally:
            # However the job ended, what it spent is already spent: the
            # operator hears about a book over the alert line here, once (C4).
            await self._report_book_cost(book_id, user)

    async def _tell_the_parent(self, record: BookRecord, *, user: str) -> None:
        """One mail a book, after its PDF is written and it says complete (W2).

        Never before: the message says "read it here", and a link to a book
        that is still assembling is a lie. Never a reason to fail a book
        either — the notice swallows its own errors, and this catches the
        rest, so a mail outage costs a warning line and nothing else.
        """
        if self._notice is None or not record.user_id:
            return
        try:
            await self._notice.send(record=record, user=record.user_id)
        except Exception:  # a mail outage must never fail a finished book
            logger.warning(
                "book.ready_mail_failed user=%s book=%s", user, record.book_id, exc_info=True
            )

    async def _fail_off_loop(
        self,
        book_id: str,
        message: str,
        *,
        user: str,
        elapsed_ms: float,
        code: str | None = None,
    ) -> None:
        """`_fail`, on a worker thread: it reads and writes the database.

        The cancellation path calls `_fail` directly instead: it runs while the
        server shuts down, when there is nothing left on the loop to starve.
        """
        await asyncio.to_thread(
            partial(self._fail, book_id, message, user=user, elapsed_ms=elapsed_ms, code=code)
        )

    async def _render_cover(
        self,
        record: BookRecord,
        edition: StoryEdition,
        artwork: _Artwork,
        page_charge: Callable[[], bool] | None,
        mark: Callable[[int, str], Awaitable[None]],
        set_stage: Callable[[str, int, int], Awaitable[None]],
        *,
        picture_payment: Callable[[str], None] | None = None,
    ) -> bytes:
        """Draw and persist the cover, alongside the pages.

        The cover goes through the same attempt loop as a page, and for the same
        reason: it is the one picture every reader sees first, and the page whose
        title band is easiest to ruin.
        """
        plate = artwork.plates.get(COVER_NUMBER)
        user = record.user_id or "-"

        async def on_stage(name: str, attempt: int) -> None:
            await set_stage(name, COVER_NUMBER, attempt)

        def prompt_for(faults: Sequence[str], findings: str, fix: bool) -> str:
            if plate is not None and fix:
                return build_fix_edit_prompt(
                    edition.cover,
                    plate.slots,
                    story=edition,
                    settings=artwork.settings,
                    prompt_name=record.about.prompt_name,
                    faults=faults,
                    findings=findings,
                    cover=True,
                    supplied_characters=tuple(artwork.characters),
                    hair_seen=artwork.hair_seen,
                )
            if plate is not None:
                return build_plate_edit_prompt(
                    edition.cover,
                    plate.slots,
                    story=edition,
                    settings=artwork.settings,
                    prompt_name=record.about.prompt_name,
                    cover=True,
                    supplied_characters=tuple(artwork.characters),
                    retry_faults=faults,
                    retry_findings=findings,
                    hair_seen=artwork.hair_seen,
                )
            return build_cover_prompt(
                artwork.settings,
                story=edition,
                prompt_name=record.about.prompt_name,
                correction=correction_for(faults, findings),
                hair_seen=artwork.hair_seen,
            )

        if plate is not None:
            references, media_types = _plate_references(
                artwork, plate, edition.cover, star=edition.star
            )
        else:
            references = [artwork.portrait]
            media_types = [artwork.portrait_media_type]
            if not artwork.photo_is_portrait:
                references.append(artwork.photo)
                media_types.append(artwork.photo_media_type)

        async def draw(prompt: str, base: GeneratedImage | None) -> GeneratedImage:
            await _await_page_charge(page_charge)
            await mark(COVER_NUMBER, "making")
            sent, sent_types = _with_base(references, media_types, base)
            return await self._on_worker_thread_with_slot(
                self._page_call(sent, sent_types, prompt, artwork.settings, "book-cover")
            )

        await _pay_for_picture(picture_payment, "The cover")
        attempts = await self._draw_until_pass(
            book_id=record.book_id,
            page_number=COVER_NUMBER,
            user=user,
            page_title=edition.cover.scene.title,
            story_line=_picture_brief(edition.cover, artwork.settings),
            cast_note=_cast_note(edition.cover, edition),
            star_in_picture=edition.cover.star_in_picture,
            plate=plate,
            artwork=artwork,
            draw=draw,
            prompt_for=prompt_for,
            paint_areas=edition.cover.paint_areas,
            on_stage=on_stage,
        )
        await asyncio.to_thread(
            self._book_store.save_cover,
            record.book_id,
            attempts.image,
            **_verdict(attempts),
        )
        await self._warm_picture(record.book_id, page_slot(COVER_NUMBER))
        await self._record_prompts(record.book_id, COVER_NUMBER, COVER_KEY, attempts)
        await mark(COVER_NUMBER, "done")
        return attempts.image

    async def _judge(
        self,
        *,
        book_id: str,
        page_number: int,
        image: bytes,
        plate: PlateArt | None,
        artwork: _Artwork,
        page_title: str,
        story_line: str,
        cast_note: str,
        star_in_picture: bool,
        attempt: int,
        user: str,
    ) -> Verdict:
        """Both tiers for one picture, combined into one verdict (T9, T10).

        Tier 1 runs first because it is free and catches the failures a model
        should not be paid to notice: a blank frame, the wrong shape, the plate
        handed straight back. Only an image that survives it is shown to tier 2.

        Every picture is judged, drawn onto a plate or composed from scratch:
        the plate only adds tier 1's two edit checks. A composed page is where
        a weak model goes most wrong, so it is the last one to wave through.

        Tier 2 is a cascade (see `decision_judge.py`): a decision model looks
        first and a picture it is confidently happy with passes there, with no
        reviewer call. Every other picture goes to the reviewer, whose verdict
        decides and whose words steer the redraw. When the reviewer cannot
        answer, the decision decides alone; when neither can, tier 1 stands.

        A tier-2 failure is *recorded*, not raised: an outage must never cost a
        parent a finished book (T10.2). Only when neither check could look does
        the verdict say tier 1 alone ran, so nothing mistakes it for a review.
        """
        # Off the event loop: ~0.35 s of Pillow on a dev machine, ten times
        # that on the free plan, and every request waited behind it.
        tier1 = await self._image_work(
            review_image,
            image,
            plate_bytes=plate.image if plate is not None else None,
            expected_ratio=_ratio(str(artwork.settings.aspect_ratio)),
        )
        verdict = Verdict(
            attempt=attempt,
            passed=tier1.passed,
            score=tier1.score,
            faults=tuple(tier1.faults),
        )
        if not tier1.passed:
            logger.warning(
                "book.review user=%s book=%s page=%d attempt=%d tier=1 "
                "faults=%s score=%d outcome=failed",
                user,
                book_id,
                page_number,
                attempt,
                ",".join(tier1.faults),
                tier1.score,
            )
        if not tier1.passed:
            return verdict

        # The approved portrait is what drew the child, so it is what the
        # child must look like; a page the child is not in has no likeness.
        portrait = artwork.portrait if star_in_picture else None
        decision = await self._decide(
            image,
            brief=story_line,
            cast=cast_note,
            portrait=portrait,
            book_id=book_id,
            page_number=page_number,
        )
        decided = decision.as_record() if decision is not None else None
        if decision is not None and decision.outcome is Outcome.ACCEPT:
            # Confidently good: no reviewer call is spent on it.
            return Verdict(
                attempt=attempt,
                passed=True,
                score=min(decision.score, tier1.score),
                tiers_run=(1, 2),
                judged_by="decision",
                decision=decided,
            )

        answer: ReviewAnswer | None = None
        # Asked before the call, not after: the point of the budget is not to
        # pay for it (C4). A verdict that ran without tier 2 says so below.
        rested_for_money = await self._review_budget_spent(book_id, user)
        if (
            self._reviewer.name != "off"
            and self.reviewer_resting_until is None
            and not rested_for_money
        ):
            try:
                answer = await asyncio.to_thread(
                    self._reviewer.review,
                    image,
                    media_type=media_type_for(image),
                    page_title=page_title,
                    story_line=story_line,
                    cast=cast_note,
                    plate=plate.image if plate is not None else None,
                    portrait=portrait,
                )
            except ReviewerQuotaSpent as exc:
                self._reviewer_rests_until = exc.until
                logger.warning(
                    "book.review user=%s book=%s page=%d attempt=%d tier=2 "
                    "outcome=quota_spent resting_until=%s",
                    user,
                    book_id,
                    page_number,
                    attempt,
                    exc.until.isoformat(),
                )
            except Exception as exc:
                # A reviewer that just failed is not asked again by the other
                # pages for a couple of minutes: each ask would only wait out
                # the same failure, and the decision judge decides meanwhile.
                self._reviewer_rests_until = datetime.now(UTC) + _REVIEWER_COOLDOWN
                logger.warning(
                    "book.review user=%s book=%s page=%d attempt=%d tier=2 "
                    "outcome=unavailable error=%s",
                    user,
                    book_id,
                    page_number,
                    attempt,
                    f"{type(exc).__name__}: {exc}",
                )

        if answer is None:
            if decision is None:
                # Neither vision check could look: tier 1 alone, and said so.
                # A budget that rested the reviewer is the operator's choice,
                # not an outage, so it is named differently and does not count
                # towards "the reviewer could not judge" (C4).
                return Verdict(
                    attempt=attempt,
                    passed=tier1.passed,
                    score=tier1.score,
                    faults=tuple(tier1.faults),
                    note=(
                        "the reviewer was rested on the book's review budget"
                        if rested_for_money
                        else "tier 2 did not run"
                    ),
                    tier2_failed=not rested_for_money,
                )
            # The reviewer is down, resting or not set: the decision makes the call on
            # its own, a confident rejection as it is, an unsure one at halfway.
            faults = decision.faults(COIN)
            passed = decision.outcome is not Outcome.REJECT and not faults
            verdict = Verdict(
                attempt=attempt,
                passed=passed,
                score=min(decision.score, tier1.score),
                faults=faults,
                tiers_run=(1, 2),
                judged_by="decision",
                decision=decided,
            )
            self._log_failed(verdict, user, book_id, page_number)
            return verdict

        # The reviewer decides whenever it answered, the decision's confident
        # rejections included: on real pages (2026-11-09) the decision model
        # rejected two pictures the reviewer rightly called perfect, and a
        # reviewer asked anyway costs nothing extra to believe. The code applies
        # the pass rule, not the model's own `passed`: a serious fault fails the
        # picture whatever the model concluded, and a minor one alone (three
        # cups, not two) does not fail a picture that scores well.
        faults = _confirmed_faults(answer.faults, decision)
        passed = verdict_passes(answer.score, faults)
        judged_by = "reviewer"
        # The checks' scores answer different questions, so the combined score
        # is the strictest: a page is only as good as its worst check.
        verdict = Verdict(
            attempt=attempt,
            passed=passed,
            score=min(answer.score, tier1.score),
            faults=faults,
            tiers_run=(1, 2),
            note=answer.note,
            judged_by=judged_by,
            decision=decided,
        )
        self._log_failed(verdict, user, book_id, page_number)
        return verdict

    async def _decide(
        self,
        image: bytes,
        *,
        brief: str,
        cast: str,
        portrait: bytes | None,
        book_id: str,
        page_number: int,
    ) -> Decision | None:
        """The decision model's look at one picture, or None if it could not answer."""
        if self._decision_judge is None:
            return None
        try:
            return await asyncio.to_thread(
                self._decision_judge.decide,
                image,
                brief=brief,
                cast=cast,
                portrait=portrait,
            )
        except Exception as exc:
            logger.warning(
                "book.decision book=%s page=%d outcome=unavailable error=%s",
                book_id,
                page_number,
                f"{type(exc).__name__}: {exc}",
            )
            return None

    @staticmethod
    def _log_failed(verdict: Verdict, user: str, book_id: str, page_number: int) -> None:
        if verdict.passed:
            return
        logger.warning(
            "book.review user=%s book=%s page=%d attempt=%d tier=2 by=%s "
            "faults=%s score=%d outcome=failed note=%r",
            user,
            book_id,
            page_number,
            verdict.attempt,
            verdict.judged_by,
            ",".join(verdict.faults) or "-",
            verdict.score,
            verdict.note,
        )

    async def _draw_until_pass(
        self,
        *,
        book_id: str,
        page_number: int,
        user: str,
        page_title: str,
        story_line: str,
        cast_note: str,
        star_in_picture: bool,
        plate: PlateArt | None,
        artwork: _Artwork,
        draw: Callable[[str, GeneratedImage | None], Awaitable[GeneratedImage]],
        prompt_for: Callable[[Sequence[str], str, bool], str],
        paint_areas: Sequence[tuple[float, float, float, float]] = (),
        on_stage: Callable[[str, int], Awaitable[None]] | None = None,
        requested: Sequence[str] = (),
    ) -> _Attempts:
        """Draw one page up to `MAX_PAGE_ATTEMPTS` times and keep the best (T9.3).

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
            paint_areas: Where the page's story puts something outside the
                slots, kept from the drawing when the plate is pasted back.
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
        best: tuple[int, int, bytes] | None = None  # (passed, score, ...) by rank
        best_image = b""
        best_prompt = ""
        chosen = 1
        best_index = 0  # the chosen attempt's place in `verdicts` (a blocked one has none)
        asked = tuple(dict.fromkeys(requested))
        faults: tuple[str, ...] = asked
        findings = ""
        # The previous attempt's picture, when it was drawn (not blocked): only
        # that picture can be mended.
        previous: GeneratedImage | None = None
        for attempt in range(1, self._page_attempts + 1):
            # The book's retry budget (C4): a page that did not pass is only
            # drawn again while the book can still afford it — and only when
            # there is a picture to keep. A first attempt the provider's safety
            # system blocked leaves nothing behind, so the page is still drawn
            # once: the ceiling is about redraws, never a hole in the book.
            if (
                attempt > 1
                and best is not None
                and await self._retry_budget_spent(book_id, user)
            ):
                if verdicts:
                    verdicts[-1]["retry_budget_stopped"] = True
                break
            mend = (
                previous
                if (
                    plate is not None
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
                await on_stage(_STAGE_RETRYING if attempt > 1 else _STAGE_PAINTING, attempt)
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
                    if best is not None or attempt == self._page_attempts:
                        if best is None:
                            raise
                        break
                    continue
                paste: dict[str, Any] = {}
                if plate is not None:
                    pasted = await self._image_work(
                        paste_plate_back,
                        drawn.data,
                        plate.image,
                        plate.slots,
                        paint_areas,
                        media_type=drawn.media_type,
                    )
                    drawn = replace(drawn, data=pasted.image, media_type=pasted.media_type)
                    paste = {
                        "pasted": pasted.pasted,
                        "restored": round(pasted.restored, 3),
                        "drift": round(pasted.drift, 1),
                    }
                previous = drawn
                image = drawn.data
                if on_stage is not None:
                    await on_stage(_STAGE_REVIEWING, attempt)
                verdict = await self._judge(
                    book_id=book_id,
                    page_number=page_number,
                    image=image,
                    plate=plate,
                    artwork=artwork,
                    page_title=page_title,
                    story_line=story_line,
                    cast_note=cast_note,
                    star_in_picture=star_in_picture,
                    attempt=attempt,
                    user=user,
                )
            verdicts.append(
                {
                    **verdict.as_dict(),
                    # Which backend drew this attempt: books outlive a provider
                    # switch, and "was this page an Agnes page" must be
                    # answerable from the page itself.
                    "provider": self._provider.name,
                    "model": drawn.model,
                    # A first draw, a mend of the previous attempt, or a redraw.
                    "kind": kind,
                    **({"plate_paste": paste} if paste else {}),
                    **({"requested": list(asked)} if asked else {}),
                }
            )
            prompts.append(prompt)
            # Rank on the verdict, never on the attempt number: the earliest
            # attempt wins a tie, so a passing first try is never replaced by an
            # equally-good second that cost money for nothing.
            rank = (1 if verdict.passed else 0, verdict.score)
            if best is None or rank > best[:2]:
                best = (rank[0], rank[1], image)
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
        return _Attempts(
            image=best_image,
            prompt=best_prompt,
            prompts=tuple(prompts),
            verdicts=tuple(verdicts),
            chosen=chosen,
            best_effort=best_effort,
        )

    async def _record_prompts(
        self, book_id: str, page_number: int, key: str, attempts: _Attempts
    ) -> None:
        """Write one page's prompt record, in attempt order (the prompt log).

        The verdicts went in with the picture (`_verdict` → `save_page`), so
        the log is what is left: *this text* asked for it. Written after the
        attempt, which is why a failed book still keeps what it had got to.
        """
        for index, verdict in enumerate(attempts.verdicts):
            sent = attempts.prompts[index] if index < len(attempts.prompts) else ""
            await asyncio.to_thread(
                self._book_store.append_prompt,
                book_id,
                {
                    "page": str(page_number),
                    "key": key,
                    "attempt": str(verdict["attempt"]),
                    "prompt": sent,
                },
            )

    async def _on_worker_thread_with_slot(
        self, work: Callable[[], _T], *, on_slot: Callable[[], Awaitable[None]] | None = None
    ) -> _T:
        """Blocking `work` on a worker thread while this task holds a gate slot.

        The page timeout binds the slot wait and the work together. If the work
        outlives it, ownership of the slot transfers to the worker: it is still
        running (and possibly still billing), and only it knows when it ends.
        """
        async with self._gate.lease(timeout=self._page_timeout) as slot:
            if on_slot is not None:
                await on_slot()
            hop = asyncio.create_task(asyncio.to_thread(work))
            try:
                return await asyncio.wait_for(asyncio.shield(hop), timeout=self._page_timeout)
            except BaseException:
                if not hop.done():
                    _hand_slot_to_worker(slot, hop)
                raise

    # --- one page ---------------------------------------------------------------

    def _page_call(
        self,
        references: Sequence[bytes],
        media_types: Sequence[str],
        prompt: str,
        settings: CartoonSettings,
        label: str,
    ) -> Callable[[], GeneratedImage]:
        """The blocking half of one render (not yet on a thread)."""

        def work() -> GeneratedImage:
            return self._provider.generate(
                ImageRequest(
                    prompt=prompt,
                    reference_images=tuple(references),
                    reference_media_types=tuple(media_types),
                    output_format="png",
                    aspect_ratio=str(settings.aspect_ratio),
                    label=label,
                )
            )

        return work

    # --- helpers ----------------------------------------------------------------

    def _artwork(self, record: BookRecord) -> _Artwork:
        portrait = self._book_store.read_portrait(record.book_id)
        if portrait is None:
            raise ValueError("This book's stored portrait is gone, so it cannot be redrawn.")
        photo = self._book_store.read_source_photo(record.book_id)
        # Books made before the photograph was stored keep their portrait only.
        # The portrait is the likeness the pages were drawn from, so it stands
        # in as the second anchor rather than the book becoming uneditable.
        # A face kept without a cartoon stores the photograph as the portrait too:
        # the same picture twice is likewise one reference, not two.
        if photo is None:
            photo = portrait
            photo_is_portrait = True
        else:
            photo_is_portrait = photo == portrait
        visitor = dict(self._book_store.load_characters(record.book_id))
        return _Artwork(
            about=record.about,
            settings=record.settings,
            portrait=portrait,
            portrait_media_type=media_type_for(portrait),
            photo=photo,
            photo_media_type=media_type_for(photo),
            photo_is_portrait=photo_is_portrait,
            characters=self._characters(record.book_id, visitor),
            plates=self._templates.book_plates(record.book_id),
            premades=self._templates.book_premades(record.book_id),
            visitor_cast=frozenset(visitor),
        )

    async def _hair_seen(self, artwork: _Artwork, *, book_id: str, user: str) -> str:
        """What the star's hair looks like in the portrait, when the prompt needs it.

        Asked once per render, and only when the visitor kept the photo's hair
        or colour: a picked hair is said from the pick, needing no look. Any
        failure leaves the pages to the portrait reference alone, as before.
        """
        settings = artwork.settings
        if (
            settings.hair_style is not HairStyle.AS_UPLOADED
            and settings.hair_color is not HairColour.AS_UPLOADED
        ):
            return ""
        if self._reviewer.name == "off" or self.reviewer_resting_until is not None:
            return ""
        try:
            with cost_scope(purpose="book", book_id=book_id):
                return await asyncio.to_thread(self._reviewer.describe_hair, artwork.portrait)
        except ReviewerQuotaSpent as exc:
            self._reviewer_rests_until = exc.until
            logger.warning(
                "book.hair user=%s book=%s outcome=quota_spent resting_until=%s",
                user,
                book_id,
                exc.until.isoformat(),
            )
        except Exception as exc:
            # Down for the hair is down for the reviews too: rest it, as _judge does.
            self._reviewer_rests_until = datetime.now(UTC) + _REVIEWER_COOLDOWN
            logger.warning(
                "book.hair user=%s book=%s outcome=unavailable error=%s",
                user,
                book_id,
                f"{type(exc).__name__}: {exc}",
            )
        return ""

    def _characters(
        self, book_id: str, visitor: Mapping[str, CharacterReference]
    ) -> dict[str, CharacterReference]:
        """The visitor's supporting faces, with the fixed portrait where they gave none.

        A visitor's own photo always wins. A character with no photo gets the
        approved portrait their book's version pins, drawn as a face of their
        own (portrait and photo are the same picture, so it is one reference).
        A version with no portrait for them leaves them out, as before.
        """
        characters = dict(visitor)
        for cast_key, fixed in self._templates.book_portraits(book_id).items():
            if cast_key in characters:
                continue
            characters[cast_key] = CharacterReference(
                portrait=fixed.image,
                portrait_media_type=fixed.media_type,
                photo=fixed.image,
                photo_media_type=fixed.media_type,
            )
        return characters

    def _fail(
        self,
        book_id: str,
        message: str,
        *,
        user: str = "-",
        elapsed_ms: float = 0.0,
        code: str | None = None,
    ) -> None:
        # The book may have been deleted while its job was unwinding. There is
        # nothing to report to and nowhere to write it: stay quiet.
        record = self._book_store.load_record(book_id)
        if record is None:
            return
        status = self._book_store.load_status(book_id) or {"pages": []}
        pages: list[dict[str, object]] = []
        for entry in cast("list[object]", status.get("pages", [])):
            if not isinstance(entry, dict):
                continue
            page_entry = dict(entry)
            if page_entry.get("status") != "done":
                page_entry["status"] = "pending"
                page_entry["url"] = None
            pages.append(page_entry)
        self._book_store.save_status(
            book_id,
            BookStatus.FAILED,
            pages,
            error=message,
            title=record.title,
            # A machine-readable "why": the reader shows the wallet warning for
            # `out_of_funds` and leaves every other failure to its own message.
            extra={"error_code": code} if code else None,
        )
        done = sum(1 for entry in pages if entry.get("status") == "done")
        logger.error(
            "book.failure user=%s book=%s pages_done=%d ms=%.0f outcome=failed message=%s",
            user,
            book_id,
            done,
            elapsed_ms,
            message,
        )


def _worker_id() -> str:
    """This process's name on the books it renders: `host:pid:random`.

    Unique per process, so a restart can never mistake its predecessor's claim
    for its own; the host and pid are there for an operator reading the row.
    """
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


#: The star as the reviewer hears of them: never by name (see `reviewer.py`).
_REVIEWER_STAR = "the child"


def _picture_brief(page: BookPage, settings: CartoonSettings) -> str:
    """What the picture must show, as the reviewer is told it.

    The page's caption — the picture this page is meant to be — not its story
    sentence. The story sentence narrates ("Mother was waiting by the lantern
    light") and names people who are not in the frame, which the reviewer then
    reports as missing. The child is "the child", never their name.
    """
    scene = scene_for(page, settings, prompt_name=_REVIEWER_STAR)
    return scene.caption or scene.title


def _cast_note(page: BookPage, edition: StoryEdition) -> str:
    """Who may appear on this page, said plainly, for the reviewer.

    The reviewer needs to know who *belongs* to catch someone who does not. It
    gets the cast's bible labels — the mother, the old man — and not the child's
    name, because a reviewer told "this is Emma's book" starts being polite
    about Emma instead of checking the picture. The star is described as "the
    child" for the same reason.

    The page's cast is who stands in its slots; a description can also name
    someone in the background (the wolf slipping away between the trees, the
    mother waiting at the gate). Those belong too, so the list defers to the
    description rather than calling them extra — a real run failed three good
    pages that way.
    """
    labels = [
        member.label for key in page.cast if (member := edition.cast.get(key)) is not None
    ]
    named = ", ".join(["the child", *labels] if page.star_in_picture else labels)
    review = prompt_text()["review"]
    key = "cast_note" if page.star_in_picture else "cast_note_absent"
    return str(review[key]).format(named=named)


def _confirmed_faults(faults: tuple[str, ...], decision: Decision | None) -> tuple[str, ...]:
    """The reviewer's faults, its likeness call kept only when the decision agrees.

    Run 1 (2026-11-11) had the reviewer flag likeness on 68 of 87 pictures that
    the decision judge scored at 0.1-0.2 on the same question, and every flag
    cost a redraw. A wrong child is the one fault a parent will not forgive, so
    it still fails a page, but only when both models see it. With no decision
    to ask (the judge is down or off) the reviewer's call stands alone.
    """
    if "likeness" not in faults or decision is None:
        return faults
    if float(decision.probabilities.get("likeness", 0.0)) >= COIN:
        return faults
    return tuple(fault for fault in faults if fault != "likeness")


def _ratio(aspect_ratio: str) -> float | None:
    """`"4:3"` as the number 1.333, for the reviewer's shape check."""
    width, _, height = aspect_ratio.partition(":")
    try:
        return float(width) / float(height)
    except (ValueError, ZeroDivisionError):
        return None


def _job_note(
    target: set[int], draw_cover: bool, requested_fixes: Mapping[int, Sequence[str]]
) -> dict[str, object]:
    """What a job is drawing, as kept in its status: enough to resume it."""
    return {
        "pages": sorted(target),
        "cover": draw_cover,
        "fixes": {
            str(number): list(faults) for number, faults in requested_fixes.items() if faults
        },
    }


def _read_job_note(
    note: object,
) -> tuple[set[int] | None, bool, dict[int, tuple[str, ...]]]:
    """A stored `_job_note` back as (pages, cover, fixes).

    A book interrupted before its first status write has no note: it was a
    new book, so the job is every page and the cover (pages None).
    """
    if not isinstance(note, Mapping):
        return None, True, {}
    raw_pages = note.get("pages")
    pages = (
        {int(number) for number in raw_pages if isinstance(number, int)}
        if isinstance(raw_pages, list)
        else None
    )
    fixes: dict[int, tuple[str, ...]] = {}
    raw_fixes = note.get("fixes")
    if isinstance(raw_fixes, Mapping):
        for key, faults in raw_fixes.items():
            if str(key).isdigit() and isinstance(faults, list):
                fixes[int(key)] = tuple(str(fault) for fault in faults)
    return pages, bool(note.get("cover", True)), fixes


def _rendered(edition: StoryEdition, record: BookRecord) -> tuple[BookPage, ...]:
    """The book's story pages with the star's name and pronouns filled in."""
    return tuple(
        render_book_page(page, record.settings, prompt_name=record.about.prompt_name)
        for page in edition.pages
    )


async def _pay_for_picture(payment: Callable[[str], None] | None, label: str) -> None:
    """Pay for the picture about to be drawn, or raise `OutOfFunds`.

    The callable is the route's: it knows the price and the wallet and raises
    `OutOfFunds` with the sentence the book page shows. This wrapper only gets
    it off the event loop, because the wallet reads and writes the database.
    """
    if payment is None:
        return
    await asyncio.to_thread(payment, label)


async def _await_page_charge(charge: Callable[[], bool] | None) -> None:
    """Wait until one billable page fits in this minute's allowance."""
    if charge is None:
        return
    while not charge():
        await asyncio.sleep(_PAGE_CHARGE_RETRY_SECONDS)


def _with_base(
    references: Sequence[bytes], media_types: Sequence[str], base: GeneratedImage | None
) -> tuple[list[bytes], list[str]]:
    """The references for one draw: as built, or with a picture to mend first.

    A fix sends the attempt being mended in place of the plate, as image 1,
    and keeps the cast's portraits after it; the fix prompt names it so.
    """
    if base is None:
        return list(references), list(media_types)
    return [base.data, *references[1:]], [base.media_type, *media_types[1:]]


def _plate_references(
    artwork: _Artwork,
    plate: PlateArt,
    page: BookPage,
    *,
    star: str,
) -> tuple[list[bytes], list[str]]:
    """The plate first, then one identity anchor per character, in slot order.

    The plate is reference 1 because the prompt names it as such: it is the
    picture that must survive. Each character then contributes its portrait —
    the star's is the book's approved portrait — and the plate holds the
    world, so the page needs fewer anchors than a from-scratch composition.
    Gemini takes three references, so a two-character page is already full.

    `star` is the edition's star key, never inferred from slot order: a page
    the star is not in has no star slot, and its first slot (the wolf) must
    not be sent the child's face. Its slots decide who is drawn, so the star's
    portrait goes only when a slot names them.
    """
    references = [plate.image]
    media_types = [plate.media_type]
    for cast_key in edit_reference_cast(
        plate.slots, page, star=star, supplied=artwork.characters
    ):
        if cast_key == star:
            references.append(artwork.portrait)
            media_types.append(artwork.portrait_media_type)
            continue
        character = artwork.characters[cast_key]
        references.append(character.portrait)
        media_types.append(character.portrait_media_type)
    return references, media_types


def _references(
    artwork: _Artwork,
    page: BookPage,
) -> tuple[list[bytes], list[str]]:
    """Exactly the reference images for one page, in the order the prompt names.

    A page the child is not in sends no image of the child.
    """
    references: list[bytes] = []
    media_types: list[str] = []
    if page.star_in_picture:
        references.append(artwork.portrait)
        media_types.append(artwork.portrait_media_type)
    if page.star_in_picture and not artwork.photo_is_portrait:
        # The same picture twice is a wasted reference slot.
        references.append(artwork.photo)
        media_types.append(artwork.photo_media_type)
    for name in page.cast:
        character = artwork.characters.get(name)
        if character is None:
            continue
        references.append(character.portrait)
        media_types.append(character.portrait_media_type)
        if character.photo != character.portrait:
            references.append(character.photo)
            media_types.append(character.photo_media_type)
    return references, media_types


def _hand_slot_to_worker(slot: Slot, hop: asyncio.Task[object]) -> None:
    """Give the slot to work that outlives the awaited request."""
    slot.transfer()
    hop.add_done_callback(lambda _finished: slot.release())


def _premade_for(
    page: BookPage, artwork: _Artwork, requested_fixes: Mapping[int, Sequence[str]]
) -> PremadeArt | None:
    """The page's pre-made picture, when this book may use it.

    Only for a page the star is not in, whose cast the visitor gave no photo of
    (a visitor's grandmother is theirs, not the stock one), and which the owner
    has not asked to be redrawn: a fix request is a request for a new picture.
    """
    premade = artwork.premades.get(page.number)
    if premade is None or page.star_in_picture:
        return None
    if requested_fixes.get(page.number):
        return None
    if any(name in artwork.visitor_cast for name in page.cast):
        return None
    return premade


def _premade_attempts(premade: PremadeArt) -> _Attempts:
    """A pre-made page's record: one attempt, passed, with nothing sent."""
    verdict: dict[str, Any] = {
        "attempt": 1,
        "tier": 0,
        "tiers_run": [],
        "passed": True,
        "score": 100,
        "faults": [],
        "note": "pre-made page, approved by an operator when it was made",
        "judged_by": "operator",
        "provider": "premade",
        "premade_id": premade.premade_id,
    }
    return _Attempts(
        image=premade.image,
        prompt="",
        prompts=("",),
        verdicts=(verdict,),
        chosen=1,
        best_effort=False,
    )
