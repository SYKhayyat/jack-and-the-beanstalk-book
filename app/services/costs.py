"""What a book costs to make: every billable model call, logged and summed.

The app charges $0.99 a book. Whether that covers the book depends on how many
calls it took — a page that passes first time is one image call and one review,
a page that needs three tries is three of each — so the cost is measured per
call, never estimated per book.

Three pieces:

* **A scope.** The code that knows *why* a call is made (the book runner knows
  the book, the page and the attempt; the portrait pass knows the draft) sets a
  `ContextVar` around it. `asyncio.to_thread` and `create_task` copy the
  context, so the scope reaches the worker thread that makes the call with no
  extra parameters threaded through the providers.
* **Metered wrappers.** `MeteredProvider` and `MeteredReviewer` sit around the
  real backends (wired once, in `build_container`) and write one `api_calls`
  row per call — success or failure, with the scope's book, page and attempt.
  Every call site is covered because there is only one provider object.
* **Prices.** One image's price follows the configured provider: Agnes and the
  mock are free, OpenAI is priced from its published per-image table for the
  model and quality. `IMAGE_CALL_COST_USD` overrides that, and a backend whose
  price is not known is stored as *unpriced* (null) — shown as such, never
  counted as free.

A failed call is recorded with a cost of zero: the providers here do not bill
a request that returned no image. It is still a row, so failures are visible.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy import insert as sa_insert
from sqlalchemy.engine import Engine

from app.config import ProviderName, Settings
from app.db import ApiCallRow, BookRow
from app.models.story import COVER_NUMBER
from app.services.decision_judge import Decision, DecisionJudge
from app.services.image_providers.base import GeneratedImage, ImageProvider, ImageRequest
from app.services.reviewer import ImageReviewer, ReviewAnswer

__all__ = [
    "CATEGORIES",
    "CATEGORY_LABELS",
    "CostLedger",
    "CostScope",
    "CostSummary",
    "MeteredDecisionJudge",
    "MeteredProvider",
    "MeteredReviewer",
    "cost_scope",
    "format_microusd",
    "image_call_price",
    "review_call_price",
]

logger = logging.getLogger(__name__)

#: OpenAI's published per-image prices (dollars), by (model, quality). A model
#: or quality not listed here is unpriced unless IMAGE_CALL_COST_USD says.
OPENAI_IMAGE_PRICES_USD: dict[tuple[str, str], float] = {
    ("gpt-image-2.5-flare", "low"): 0.00588,
}

#: Every category a call is filed under, in the order the cost page lists them.
CATEGORIES: tuple[str, ...] = (
    "portrait",
    "cover",
    "page",
    "retry",
    "review",
    "plate",
    "other",
)


#: How each category reads on the cost panel.
CATEGORY_LABELS: dict[str, str] = {
    "portrait": "Cartoon portraits",
    "cover": "Cover pictures",
    "page": "Story pictures (first try)",
    "retry": "Redraws after a failed check",
    "review": "Quality checks",
    "plate": "Template plates",
    "other": "Other calls",
}


def format_microusd(microusd: int) -> str:
    """`5880` → `$0.0059`. Four places, because one call costs less than a cent."""
    return f"${microusd / 1_000_000:.4f}"


@dataclass(frozen=True, slots=True)
class CostScope:
    """Why the calls made inside this scope are being made."""

    purpose: str = "other"
    book_id: str | None = None
    generation_id: str | None = None
    page_number: int | None = None
    attempt: int | None = None


_NO_SCOPE = CostScope()
_SCOPE: ContextVar[CostScope | None] = ContextVar("cost_scope", default=None)


@contextmanager
def cost_scope(**changes: object) -> Iterator[CostScope]:
    """Narrow the current scope (e.g. add the page and attempt to a book's)."""
    scope = replace(current_scope(), **changes)  # type: ignore[arg-type]
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def current_scope() -> CostScope:
    return _SCOPE.get() or _NO_SCOPE


# --- prices ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallPrice:
    """One backend's price per call (None: not known) and how it is named."""

    usd: float | None
    model: str


def image_call_price(settings: Settings) -> CallPrice:
    """What one image from the configured provider costs."""
    provider = settings.image_provider
    if provider == ProviderName.OPENAI:
        model = f"{settings.openai_model} ({settings.openai_quality})"
        listed = OPENAI_IMAGE_PRICES_USD.get((settings.openai_model, settings.openai_quality))
    elif provider == ProviderName.AGNES:
        # Agnes AI's free tier: rate limited, not billed.
        model, listed = settings.agnes_model, 0.0
    elif provider == ProviderName.GEMINI:
        model, listed = settings.gemini_model, None
    else:
        model, listed = "placeholder", 0.0
    if settings.image_call_cost_usd is not None:
        listed = settings.image_call_cost_usd
    return CallPrice(usd=listed, model=model)


def review_call_price(settings: Settings) -> CallPrice:
    """What one review call costs (None until REVIEW_CALL_COST_USD is set)."""
    return CallPrice(usd=settings.review_call_cost_usd, model=settings.reviewer_model)


def _micros(usd: float | None, count: int = 1) -> int | None:
    return None if usd is None else round(usd * 1_000_000) * count


# --- the ledger -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CategoryCost:
    category: str
    #: Average number of these calls in one finished book.
    calls_per_book: float
    #: Average spend on them in one finished book.
    cost_microusd: int


@dataclass(frozen=True, slots=True)
class CostSummary:
    """The deployment's cost of making books, for the cost panel."""

    #: Finished books whose calls were measured (books made before the ledger
    #: existed have no rows and are left out, not counted as free).
    books_measured: int
    average_book_microusd: int
    #: Average calls and spend per measured book, by category.
    per_book: tuple[CategoryCost, ...]
    #: Everything ever logged, including failed books, drafts and plates.
    total_calls: int
    total_microusd: int
    #: Spend no finished book carries: rejected portraits, books that failed
    #: or are still drawing, and plates.
    overhead_microusd: int
    failed_calls: int
    unpriced_calls: int


class CostLedger:
    """Writes and sums `api_calls`."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(
        self,
        *,
        kind: str,
        provider: str,
        model: str,
        images: int,
        ok: bool,
        cost_microusd: int | None,
        duration_ms: int,
        scope: CostScope | None = None,
    ) -> None:
        """Log one call. Never raises: a ledger hiccup must not cost a book."""
        scope = scope or current_scope()
        row = {
            "id": uuid4().hex,
            "kind": kind,
            "category": _category(kind, scope),
            "provider": provider[:32],
            "model": model[:128],
            "book_id": scope.book_id,
            "generation_id": scope.generation_id,
            "page_number": scope.page_number,
            "attempt": scope.attempt,
            "images": images,
            "ok": ok,
            "cost_microusd": cost_microusd if ok else 0,
            "duration_ms": duration_ms,
        }
        try:
            with self._engine.begin() as conn:
                conn.execute(sa_insert(ApiCallRow).values(**row))
        except Exception:
            logger.exception("cost.record_failed kind=%s book=%s", kind, scope.book_id or "-")
            return
        logger.info(
            "cost.call kind=%s category=%s provider=%s book=%s page=%s attempt=%s "
            "ok=%s microusd=%s",
            kind,
            row["category"],
            provider,
            scope.book_id or "-",
            scope.page_number if scope.page_number is not None else "-",
            scope.attempt or "-",
            ok,
            row["cost_microusd"] if row["cost_microusd"] is not None else "unpriced",
        )

    def adopt_drafts(self, book_id: str, generation_ids: list[str]) -> None:
        """File the portrait calls of the drafts a book is made from under it.

        A portrait is paid for before its book exists, so its calls are logged
        under the draft. When the book starts, those rows become the book's —
        the star's portrait and every cast portrait it uses. Never raises.
        """
        wanted = [generation for generation in generation_ids if generation]
        if not wanted:
            return
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    update(ApiCallRow)
                    .where(ApiCallRow.generation_id.in_(wanted), ApiCallRow.book_id.is_(None))
                    .values(book_id=book_id)
                )
        except Exception:
            logger.exception("cost.adopt_failed book=%s", book_id)

    def book_costs_microusd(self, book_ids: list[str]) -> dict[str, int]:
        """Each book's priced spend: its portraits, pictures, retries and reviews."""
        if not book_ids:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(
                    ApiCallRow.book_id,
                    func.coalesce(func.sum(ApiCallRow.cost_microusd), 0),
                )
                .where(ApiCallRow.book_id.in_(book_ids))
                .group_by(ApiCallRow.book_id)
            ).all()
        return {str(book_id): int(total or 0) for book_id, total in rows}

    def summary(self) -> CostSummary:
        """Average cost per finished book, and everything else logged."""
        with self._engine.connect() as conn:
            finished = set(
                conn.execute(select(BookRow.id).where(BookRow.status == "complete")).scalars()
            )
            rows = conn.execute(
                select(
                    ApiCallRow.book_id,
                    ApiCallRow.category,
                    ApiCallRow.ok,
                    ApiCallRow.cost_microusd,
                )
            ).all()
        spend: dict[str, int] = {}
        calls: dict[str, int] = {}
        money: dict[str, int] = {}
        for row in rows:
            if row.book_id not in finished:
                continue
            cost = row.cost_microusd or 0
            spend[row.book_id] = spend.get(row.book_id, 0) + cost
            calls[row.category] = calls.get(row.category, 0) + 1
            money[row.category] = money.get(row.category, 0) + cost
        measured = len(spend)
        per_book = tuple(
            CategoryCost(
                category=category,
                calls_per_book=calls[category] / measured,
                cost_microusd=round(money[category] / measured),
            )
            for category in CATEGORIES
            if category in calls
        )
        book_spend = sum(spend.values())
        total = sum(row.cost_microusd or 0 for row in rows)
        return CostSummary(
            books_measured=measured,
            average_book_microusd=round(book_spend / measured) if measured else 0,
            per_book=per_book,
            total_calls=len(rows),
            total_microusd=total,
            overhead_microusd=total - book_spend,
            failed_calls=sum(1 for row in rows if not row.ok),
            unpriced_calls=sum(1 for row in rows if row.ok and row.cost_microusd is None),
        )


def _category(kind: str, scope: CostScope) -> str:
    if kind == "review":
        return "review"
    if scope.purpose in ("portrait", "plate"):
        return scope.purpose
    if scope.purpose == "book":
        if scope.attempt is not None and scope.attempt > 1:
            return "retry"
        return "cover" if scope.page_number == COVER_NUMBER else "page"
    return "other"


# --- the metered backends ---------------------------------------------------------


class MeteredProvider(ImageProvider):
    """The configured image provider, with every call written to the ledger."""

    def __init__(self, inner: ImageProvider, ledger: CostLedger, price: CallPrice) -> None:
        self._inner = inner
        self._ledger = ledger
        self._price = price
        self.name = inner.name

    @property
    def inner(self) -> ImageProvider:
        return self._inner

    def generate(self, request: ImageRequest) -> GeneratedImage:
        started = time.monotonic()
        try:
            image = self._inner.generate(request)
        except BaseException:
            self._log(images=0, ok=False, started=started)
            raise
        self._log(images=1, ok=True, started=started)
        return image

    def generate_candidates(self, request: ImageRequest, count: int) -> list[GeneratedImage]:
        started = time.monotonic()
        try:
            images = self._inner.generate_candidates(request, count)
        except BaseException:
            self._log(images=0, ok=False, started=started)
            raise
        # Priced per image returned: one call with n=3 is three images' worth.
        self._log(images=len(images), ok=True, started=started)
        return images

    def health_check(self) -> tuple[bool, str]:
        return self._inner.health_check()

    @property
    def draws_placeholders(self) -> bool:
        return self._inner.draws_placeholders

    def _log(self, *, images: int, ok: bool, started: float) -> None:
        self._ledger.record(
            kind="image",
            provider=self.name,
            model=self._price.model,
            images=images,
            ok=ok,
            cost_microusd=_micros(self._price.usd, images),
            duration_ms=round((time.monotonic() - started) * 1000),
        )


class MeteredReviewer:
    """The configured reviewer, with every call written to the ledger.

    The stand-in reviewer (`name == "off"`) makes no call, so it is passed
    through unwrapped by `metered_reviewer` and logs nothing.
    """

    def __init__(self, inner: ImageReviewer, ledger: CostLedger, price: CallPrice) -> None:
        self._inner = inner
        self._ledger = ledger
        self._price = price
        self.name = inner.name

    def review(
        self, image: bytes, *, media_type: str, page_title: str, story_line: str, cast: str
    ) -> ReviewAnswer:
        started = time.monotonic()
        ok = False
        try:
            answer = self._inner.review(
                image,
                media_type=media_type,
                page_title=page_title,
                story_line=story_line,
                cast=cast,
            )
            ok = True
            return answer
        finally:
            self._ledger.record(
                kind="review",
                provider="reviewer",
                model=self._price.model,
                images=0,
                ok=ok,
                cost_microusd=_micros(self._price.usd),
                duration_ms=round((time.monotonic() - started) * 1000),
            )


class MeteredDecisionJudge:
    """The decision judge, with every call written to the ledger at its token price.

    Priced from the input tokens the call reports, since that is how the
    Decisions API bills (output is free); a failed call is logged at $0.
    """

    def __init__(
        self, inner: DecisionJudge, ledger: CostLedger, usd_per_million: float
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._usd_per_million = usd_per_million
        self.name = inner.name

    def decide(self, image: bytes, *, brief: str, cast: str) -> Decision:
        started = time.monotonic()
        decision: Decision | None = None
        try:
            decision = self._inner.decide(image, brief=brief, cast=cast)
            return decision
        finally:
            tokens = decision.input_tokens if decision is not None else 0
            self._ledger.record(
                kind="review",
                provider="decisions",
                model=decision.model if decision is not None else "",
                images=0,
                ok=decision is not None,
                cost_microusd=round(tokens * self._usd_per_million),
                duration_ms=round((time.monotonic() - started) * 1000),
            )


def metered_reviewer(
    reviewer: ImageReviewer, ledger: CostLedger, price: CallPrice
) -> ImageReviewer:
    if reviewer.name == "off":
        return reviewer
    return MeteredReviewer(reviewer, ledger, price)
