"""What the operator needs to see to trust the live site (UI.org Q6).

Everything here answers a question that used to need a database console:

* **Which backends are live?** The image provider and its model, the reviewer
  and its model, payments, the plate start gate and template version of each
  story, and the limits. The provider's name is operator data — no visitor
  surface may show it — so this is the one place it is printed.
* **Is every picture being reviewed?** Pictures reviewed, passed first time,
  redrawn, kept as best effort, and how often tier 2 could not run (an outage
  passes a picture on tier 1 alone, so it must be counted, never silent), plus
  the fault tally and any finished picture that has no verdict at all.
* **What happened to one book?** Every attempt of every page, with what drew
  it, both tiers' verdicts, the reviewer's own words and what the owner asked
  for; the cast and the portrait history behind each face.

Read-only, and it never loads an image or a PDF column: these queries run on
the production database from a page the operator reloads.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from app.db import (
    ApiCallRow,
    BookCharacterRow,
    BookPageRow,
    BookRow,
    UserRow,
)
from app.services.costs import format_microusd
from app.services.db_book_store import unreviewed_images

__all__ = [
    "ApiCall",
    "BookAttempt",
    "BookDetail",
    "BookLine",
    "PictureRecord",
    "ReviewHealth",
    "book_detail",
    "recent_books",
    "recent_calls",
    "review_health",
]


@dataclass(frozen=True, slots=True)
class ReviewHealth:
    """How the quality loop has been doing over a window of books."""

    books: int
    pictures: int
    reviewed: int
    passed_first_try: int
    redrawn: int
    best_effort: int
    #: Attempts on which tier 2 should have run and could not.
    tier2_unavailable: int
    #: Attempts tier 2 actually looked at.
    tier2_ran: int
    #: Attempts the decision model settled on its own (no reviewer call).
    settled_by_decision: int
    #: Attempts the reviewer's verdict decided.
    decided_by_reviewer: int
    faults: tuple[tuple[str, int], ...]
    #: (book, page) of finished pictures that must have a verdict and do not.
    unreviewed: tuple[tuple[str, int], ...]
    providers: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class ApiCall:
    created_at: datetime
    kind: str
    category: str
    provider: str
    model: str
    book_id: str | None
    page_number: int | None
    attempt: int | None
    ok: bool
    cost: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class BookLine:
    book_id: str
    title: str
    story_key: str
    status: str
    owner: str
    created_at: datetime
    plated: bool
    pictures: int
    attempts: int
    best_effort: int
    tier2_unavailable: int
    providers: str


@dataclass(frozen=True, slots=True)
class BookAttempt:
    attempt: int
    passed: bool
    score: int
    tiers: str
    faults: tuple[str, ...]
    note: str
    provider: str
    model: str
    requested: tuple[str, ...]
    tier2_unavailable: bool
    chosen: bool
    #: "decision" or "reviewer": whose verdict this was; "" before the cascade.
    judged_by: str = ""
    #: The decision model's "shows the moment" probability, when it looked.
    moment: float | None = None


@dataclass(frozen=True, slots=True)
class PictureRecord:
    #: 0 is the cover.
    number: int
    best_effort: bool
    attempts: tuple[BookAttempt, ...]

    @property
    def label(self) -> str:
        return "Cover" if self.number == 0 else f"Page {self.number}"


@dataclass(frozen=True, slots=True)
class CastLine:
    cast_key: str
    generation_id: str
    settings: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class BookDetail:
    line: BookLine
    user_id: str | None
    generation_id: str
    template_version_id: str | None
    review_required: bool
    settings: Mapping[str, Any]
    about: Mapping[str, Any]
    error: str
    pictures: tuple[PictureRecord, ...]
    cast: tuple[CastLine, ...] = field(default_factory=tuple)


# --- review health -----------------------------------------------------------------


def review_health(engine: Engine, *, since: datetime | None = None) -> ReviewHealth:
    """Tally every stored verdict of books made since `since` (all books if None)."""
    with engine.connect() as conn:
        book_query = select(BookRow.id, BookRow.cover_review, BookRow.cover_best_effort).where(
            BookRow.review_required.is_(True)
        )
        if since is not None:
            book_query = book_query.where(BookRow.created_at >= since)
        books = conn.execute(book_query).all()
        ids = [row.id for row in books]
        pages = (
            conn.execute(
                select(BookPageRow.review, BookPageRow.best_effort).where(
                    BookPageRow.book_id.in_(ids)
                )
            ).all()
            if ids
            else []
        )
        missing = unreviewed_images(engine)
    records: list[tuple[list[dict[str, Any]] | None, bool]] = [
        (row.cover_review, bool(row.cover_best_effort)) for row in books
    ]
    records += [(row.review, bool(row.best_effort)) for row in pages]
    faults: Counter[str] = Counter()
    providers: Counter[str] = Counter()
    pictures = reviewed = first = redrawn = kept = unavailable = ran = 0
    settled = by_reviewer = 0
    for review, best_effort in records:
        pictures += 1
        verdicts = list(review or [])
        if not verdicts:
            continue
        reviewed += 1
        if verdicts[0].get("passed"):
            first += 1
        if len(verdicts) > 1:
            redrawn += 1
        if best_effort:
            kept += 1
        for verdict in verdicts:
            faults.update(str(fault) for fault in verdict.get("faults", []))
            if verdict.get("tier2_failed"):
                unavailable += 1
            if 2 in verdict.get("tiers_run", []):
                ran += 1
            if verdict.get("judged_by") == "decision" and not verdict.get("note"):
                settled += 1
            elif verdict.get("judged_by") == "reviewer":
                by_reviewer += 1
            if verdict.get("provider"):
                providers[f"{verdict['provider']} · {verdict.get('model', '?')}"] += 1
    wanted = set(ids) if since is not None else None
    return ReviewHealth(
        books=len(books),
        pictures=pictures,
        reviewed=reviewed,
        passed_first_try=first,
        redrawn=redrawn,
        best_effort=kept,
        tier2_unavailable=unavailable,
        tier2_ran=ran,
        settled_by_decision=settled,
        decided_by_reviewer=by_reviewer,
        faults=tuple(faults.most_common()),
        unreviewed=tuple(item for item in missing if wanted is None or item[0] in wanted),
        providers=tuple(providers.most_common()),
    )


# --- the cost ledger -----------------------------------------------------------------


def recent_calls(
    engine: Engine, *, limit: int = 40, book_id: str | None = None
) -> list[ApiCall]:
    """The newest model calls, or one book's calls, newest first."""
    query = select(ApiCallRow).order_by(ApiCallRow.created_at.desc()).limit(limit)
    if book_id is not None:
        query = query.where(ApiCallRow.book_id == book_id)
    with engine.connect() as conn:
        rows = conn.execute(query).all()
    return [
        ApiCall(
            created_at=row.created_at,
            kind=row.kind,
            category=row.category,
            provider=row.provider,
            model=row.model,
            book_id=row.book_id,
            page_number=row.page_number,
            attempt=row.attempt,
            ok=bool(row.ok),
            cost=format_microusd(row.cost_microusd)
            if row.cost_microusd is not None
            else "unpriced",
            duration_ms=int(row.duration_ms),
        )
        for row in rows
    ]


# --- books ---------------------------------------------------------------------------

#: The book columns every operator listing reads: never a blob.
_BOOK_COLUMNS = (
    BookRow.id,
    BookRow.title,
    BookRow.story_key,
    BookRow.status,
    BookRow.user_id,
    BookRow.created_at,
    BookRow.template_version_id,
    BookRow.cover_review,
    BookRow.cover_best_effort,
)


def recent_books(engine: Engine, *, limit: int = 30) -> list[BookLine]:
    with engine.connect() as conn:
        books = conn.execute(
            select(*_BOOK_COLUMNS, UserRow.email)
            .outerjoin(UserRow, UserRow.sub == BookRow.user_id)
            .order_by(BookRow.created_at.desc())
            .limit(limit)
        ).all()
        ids = [row.id for row in books]
        pages = _page_reviews(conn, ids)
    return [_book_line(row, pages.get(row.id, [])) for row in books]


def book_detail(engine: Engine, book_id: str) -> BookDetail | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(
                *_BOOK_COLUMNS,
                UserRow.email,
                BookRow.generation_id,
                BookRow.review_required,
                BookRow.settings,
                BookRow.about,
                BookRow.error,
            )
            .outerjoin(UserRow, UserRow.sub == BookRow.user_id)
            .where(BookRow.id == book_id)
        ).one_or_none()
        if row is None:
            return None
        pages = _page_reviews(conn, [book_id]).get(book_id, [])
        cast_rows = conn.execute(
            select(
                BookCharacterRow.cast_key,
                BookCharacterRow.generation_id,
                BookCharacterRow.settings,
            ).where(BookCharacterRow.book_id == book_id)
        ).all()
    pictures = [_picture(0, row.cover_review, bool(row.cover_best_effort))]
    pictures += [_picture(number, review, best) for number, review, best in pages]
    return BookDetail(
        line=_book_line(row, pages),
        user_id=row.user_id,
        generation_id=row.generation_id,
        template_version_id=row.template_version_id,
        review_required=bool(row.review_required),
        settings=dict(row.settings or {}),
        about=dict(row.about or {}),
        error=row.error or "",
        pictures=tuple(picture for picture in pictures if picture.attempts or picture.number),
        cast=tuple(
            CastLine(
                cast_key=cast.cast_key,
                generation_id=cast.generation_id or "",
                settings=dict(cast.settings or {}),
            )
            for cast in cast_rows
        ),
    )


def _page_reviews(
    conn: Any, ids: Sequence[str]
) -> dict[str, list[tuple[int, list[dict[str, Any]] | None, bool]]]:
    found: dict[str, list[tuple[int, list[dict[str, Any]] | None, bool]]] = {}
    if not ids:
        return found
    rows = conn.execute(
        select(
            BookPageRow.book_id,
            BookPageRow.page_number,
            BookPageRow.review,
            BookPageRow.best_effort,
        )
        .where(BookPageRow.book_id.in_(ids))
        .order_by(BookPageRow.page_number)
    ).all()
    for row in rows:
        found.setdefault(row.book_id, []).append(
            (int(row.page_number), row.review, bool(row.best_effort))
        )
    return found


def _book_line(
    row: Any, pages: Iterable[tuple[int, list[dict[str, Any]] | None, bool]]
) -> BookLine:
    reviews = [(row.cover_review, bool(row.cover_best_effort))] + [
        (review, best) for _, review, best in pages
    ]
    pictures = sum(1 for _, review, _ in pages) + (1 if row.cover_review else 0)
    attempts = sum(len(review or []) for review, _ in reviews)
    best_effort = sum(1 for review, best in reviews if review and best)
    unavailable = sum(
        1 for review, _ in reviews for verdict in review or [] if verdict.get("tier2_failed")
    )
    providers = sorted(
        {
            str(verdict["provider"])
            for review, _ in reviews
            for verdict in review or []
            if verdict.get("provider")
        }
    )
    return BookLine(
        book_id=row.id,
        title=row.title or "",
        story_key=row.story_key,
        status=row.status,
        owner=row.email or (row.user_id or "—"),
        created_at=row.created_at,
        plated=row.template_version_id is not None,
        pictures=pictures,
        attempts=attempts,
        best_effort=best_effort,
        tier2_unavailable=unavailable,
        providers=", ".join(providers) or "—",
    )


def _picture(
    number: int, review: list[dict[str, Any]] | None, best_effort: bool
) -> PictureRecord:
    verdicts = list(review or [])
    passing = [v for v in verdicts if v.get("passed")]
    if passing:
        chosen = int(passing[0].get("attempt", 1))
    elif verdicts:
        chosen = int(
            max(verdicts, key=lambda v: (int(v.get("score", 0)), -int(v.get("attempt", 0))))[
                "attempt"
            ]
        )
    else:
        chosen = 0
    return PictureRecord(
        number=number,
        best_effort=best_effort,
        attempts=tuple(
            BookAttempt(
                attempt=int(verdict.get("attempt", index + 1)),
                passed=bool(verdict.get("passed")),
                score=int(verdict.get("score", 0)),
                tiers="+".join(str(tier) for tier in verdict.get("tiers_run", [])) or "none",
                faults=tuple(str(fault) for fault in verdict.get("faults", [])),
                note=str(verdict.get("note", "")),
                provider=str(verdict.get("provider", "")),
                model=str(verdict.get("model", "")),
                requested=tuple(str(item) for item in verdict.get("requested", [])),
                tier2_unavailable=bool(verdict.get("tier2_failed")),
                chosen=int(verdict.get("attempt", index + 1)) == chosen,
                judged_by=str(verdict.get("judged_by", "")),
                moment=_moment(verdict),
            )
            for index, verdict in enumerate(verdicts)
        ),
    )


def count_books(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(BookRow)).scalar_one())


def _moment(verdict: Mapping[str, Any]) -> float | None:
    decision = verdict.get("decision")
    if not isinstance(decision, Mapping):
        return None
    value = (decision.get("probabilities") or {}).get("shows_moment")
    return float(value) if isinstance(value, (int, float)) else None
