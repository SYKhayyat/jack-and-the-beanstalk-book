"""The operator's status screens (UI.org Q6): what is live, and is it working.

    /admin                         where the operator lands: the status page
    /admin/status                  backends, limits, review health, costs, recent
                                   books, recent model calls, recent portraits
    /admin/books/{id}              one book: every attempt of every picture, what
                                   drew it, both tiers' verdicts, the reviewer's
                                   words, what the owner asked for, the cast and
                                   the portrait history behind each face
    /admin/books/{id}/art/{n}      the picture on page n (0 is the cover)

The same gate as the plate screens: `OPERATOR_SUB` names the Google account(s)
who may come in — by OAuth subject id or account email, comma separated — and
everybody else gets the 404 an unknown URL gets. The overview also carries the
one destructive action: removing a book the visitor can no longer reach. This
is the only place the image provider's and the reviewer's names are printed; no
visitor surface carries them.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.config import ProviderName
from app.container import Container
from app.dependencies import ContainerDep
from app.routes.admin import admin_chrome, operator_or_404, render_admin
from app.routes.auth import CurrentSessionDep
from app.routes.book_access import no_store
from app.routes.wallet import cost_panel
from app.services.costs import image_call_price
from app.services.media import media_type_for
from app.services.operator_status import (
    book_detail,
    recent_books,
    recent_calls,
    review_health,
)
from app.services.wallet import (
    REFUND_FAILED,
    REFUND_PROCESSED,
    REFUND_PROCESSING,
    REFUND_REQUESTED,
    WalletError,
    format_cents,
)

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)

logger = logging.getLogger(__name__)

#: The window the headline review numbers cover. Long enough to hold a few days
#: of books, short enough that a fix shows up in the numbers the day after.
_HEALTH_WINDOW = timedelta(days=7)

_REFUND_LABELS = {
    REFUND_REQUESTED: "Waiting to be sent",
    REFUND_PROCESSING: "Sending",
    REFUND_PROCESSED: "Sent back to the card",
    REFUND_FAILED: "Failed — money returned to the wallet",
}

#: The only wording a `notice` query parameter may select on the admin page.
_NOTICES = {
    "refunded": "The refund was sent back to the card.",
    "refund_failed": "Stripe refused that refund; the money is back in the wallet.",
    "payments_off": "Payments are off, so there is no card to refund.",
    "refund_missing": "That refund request no longer exists.",
}


@router.get("", include_in_schema=False)
def admin_home(container: ContainerDep, session: CurrentSessionDep) -> RedirectResponse:
    operator_or_404(container, session)
    return RedirectResponse("/admin/status", status_code=303)


@router.get("/status", response_class=HTMLResponse)
async def status_page(
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    notice: str = "",
) -> HTMLResponse:
    """Everything the operator checks before trusting the live site."""
    operator_or_404(container, session)
    since = datetime.now(UTC).replace(tzinfo=None) - _HEALTH_WINDOW
    engine = container.engine
    week, ever, books, calls, portraits, refund_queue = await asyncio.gather(
        asyncio.to_thread(review_health, engine, since=since),
        asyncio.to_thread(review_health, engine),
        asyncio.to_thread(recent_books, engine),
        asyncio.to_thread(recent_calls, engine),
        asyncio.to_thread(container.portraits.recent, 30),
        asyncio.to_thread(container.wallet.all_refund_requests, limit=50),
    )
    refunds = sorted(
        refund_queue,
        key=lambda item: (item.status != REFUND_REQUESTED, item.created_at),
        reverse=True,
    )
    owners = sorted({item.owner for item in refunds})
    users = await asyncio.gather(
        *(asyncio.to_thread(container.user_store.get, owner) for owner in owners)
    )
    owner_labels = {
        owner: (user.email or user.name or owner) if user is not None else owner
        for owner, user in zip(owners, users, strict=True)
    }
    stories = await asyncio.to_thread(container.templates.stories)
    response = render_admin(
        request,
        "admin_status.html",
        {
            **admin_chrome(container, session, request, "/admin/status"),
            "backends": _backends(container),
            "stories": stories,
            "week": week,
            "ever": ever,
            "books": books,
            "calls": calls,
            "portraits": portraits,
            "refunds": [
                {
                    "id": item.id,
                    "owner": item.owner,
                    "owner_label": owner_labels[item.owner],
                    "amount_label": format_cents(item.amount_cents),
                    "status": item.status,
                    "status_label": _REFUND_LABELS.get(item.status, item.status),
                    "pending": item.status == REFUND_REQUESTED,
                    "created_at": item.created_at,
                    "note": item.note,
                }
                for item in refunds
            ],
            "notice": _NOTICES.get(notice, ""),
            "costs": cost_panel(container, container.wallet.price_cents),
        },
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.post("/refunds/{refund_id}/process")
async def process_refund(
    refund_id: str, container: ContainerDep, session: CurrentSessionDep
) -> RedirectResponse:
    """Send one queued refund back through Stripe, exactly once."""
    operator_or_404(container, session)
    if container.payments is None:
        return RedirectResponse("/admin/status?notice=payments_off#refunds", status_code=303)
    request_refund = await asyncio.to_thread(container.wallet.get_refund, refund_id)
    if request_refund is None:
        return RedirectResponse("/admin/status?notice=refund_missing#refunds", status_code=303)
    try:
        result = await asyncio.to_thread(
            container.wallet.process_refund,
            request_refund.owner,
            refund_id,
            container.payments,
        )
    except WalletError:
        logger.warning("admin.refund_failed id=%s", refund_id)
        return RedirectResponse("/admin/status?notice=refund_failed#refunds", status_code=303)
    notice = "refunded" if result.status == REFUND_PROCESSED else "refund_failed"
    return RedirectResponse(f"/admin/status?notice={notice}#refunds", status_code=303)


@router.get("/books/{book_id}", response_class=HTMLResponse)
async def book_page(
    request: Request, book_id: str, container: ContainerDep, session: CurrentSessionDep
) -> HTMLResponse:
    """One book, attempt by attempt."""
    operator_or_404(container, session)
    detail = await asyncio.to_thread(book_detail, container.engine, book_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Not Found")
    history = (
        await asyncio.to_thread(container.portraits.lineage, detail.generation_id)
        if detail.generation_id
        else []
    )
    cast_history = {
        cast.cast_key: await asyncio.to_thread(container.portraits.lineage, cast.generation_id)
        for cast in detail.cast
        if cast.generation_id
    }
    earlier: list[Any] = []
    if detail.user_id:
        earlier = await asyncio.to_thread(
            container.portraits.for_user,
            detail.user_id,
            story_key=detail.line.story_key,
            before=detail.line.created_at,
        )
    calls = await asyncio.to_thread(recent_calls, container.engine, book_id=book_id, limit=200)
    response = render_admin(
        request,
        "admin_book.html",
        {
            **admin_chrome(container, session, request, "/admin/status"),
            "book": detail,
            "portrait_history": history,
            "cast_history": cast_history,
            "studio_history": earlier,
            "calls": calls,
        },
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.post("/books/{book_id}/delete")
async def delete_book(
    book_id: str, container: ContainerDep, session: CurrentSessionDep
) -> RedirectResponse:
    """Remove a book and every picture in it.

    The visitor's own delete exists on the book page; this is the operator's,
    for the copy that cannot be reached any more (a failed book, an abandoned
    session, a test book left on the live site). It is deliberately behind the
    same identity gate as everything else here.
    """
    operator = operator_or_404(container, session)
    if container.book_store.load_record(book_id) is None:
        raise HTTPException(status_code=404, detail="Not Found")
    await asyncio.to_thread(container.book_store.delete, book_id)
    logger.info("admin.book_deleted book=%s operator=%s", book_id, operator)
    return RedirectResponse("/admin/status", status_code=303)


@router.get("/books/{book_id}/art/{number}")
async def book_art(
    book_id: str, number: int, container: ContainerDep, session: CurrentSessionDep
) -> Response:
    """The picture on one page of any book, for the operator's eyes only."""
    operator_or_404(container, session)
    store = container.book_store
    data = await asyncio.to_thread(
        store.read_cover if number == 0 else store.read_page,
        *((book_id,) if number == 0 else (book_id, number)),
    )
    if data is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return no_store(data, media_type=media_type_for(data))


def _resting_note(container: Container) -> str:
    """Why the reviewer is not being asked, while its daily quota is spent."""
    until = container.book_runner.reviewer_resting_until
    if until is None:
        return ""
    return (
        f" — RESTING until {until:%Y-%m-%d %H:%M} UTC (daily quota used up;"
        " the decision judge decides alone)"
    )


def _backends(container: Container) -> list[tuple[str, str]]:
    """What is live, as label/value rows. The only place the provider is named."""
    settings = container.settings
    provider = container.provider
    price = image_call_price(settings)
    reviewer = container.book_runner.reviewer_name
    ready, reason = provider.health_check()
    rows: list[tuple[str, str]] = [
        ("Environment", settings.app_env),
        (
            "Image provider",
            f"{provider.name} · {price.model}"
            + ("" if ready else f" — NOT READY: {reason}")
            + (" — placeholder art" if provider.draws_placeholders else ""),
        ),
        (
            "Image price",
            price.describe(),
        ),
        (
            "Reviewer (tier 2)",
            "OFF — pictures get tier 1 only"
            if reviewer == "off"
            else f"{reviewer} · {settings.reviewer_model}" + _resting_note(container),
        ),
        (
            "Decision judge (first look)",
            "OFF — every picture goes to the reviewer"
            if container.book_runner.decision_judge_name == "off"
            else (
                f"{container.book_runner.decision_judge_name}"
                f" · {settings.decision_judge_model}"
            ),
        ),
        ("Tries per picture", str(container.book_runner.page_attempts)),
        ("Concurrent image calls", str(container.gate.limit)),
        ("Image calls per client per minute", str(settings.max_generations_per_minute)),
        (
            "Payments",
            "Stripe on" if container.payments is not None else "OFF — books are free",
        ),
        ("Operator", settings.operator_sub or "—"),
    ]
    if settings.image_provider is ProviderName.MOCK:
        rows.append(("Note", "IMAGE_PROVIDER=mock: nothing real is drawn"))
    return rows
