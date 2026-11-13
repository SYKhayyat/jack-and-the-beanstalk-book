"""The print-order store: one transaction per operation, over any SQL dialect."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.engine import Engine

from app.db import PrintOrderRow
from app.services.print.base import PrintOrder

__all__ = ["PrintOrderStore", "new_order_id"]

#: How long a `placing` claim is honoured before a retry may take it over.
#: Longer than any provider request (30 s) and shorter than a coffee break, so
#: a process that dies mid-order does not leave the order unplaceable.
_CLAIM_STALE_AFTER = timedelta(minutes=15)


def new_order_id() -> str:
    return f"print_{uuid.uuid4().hex[:16]}"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _row_to_order(row: Any) -> PrintOrder:
    return PrintOrder(
        id=row.id,
        book_id=row.book_id,
        user_id=row.user_id or "",
        state=row.state,
        pdf_sha256=row.pdf_sha256,
        page_count=row.page_count,
        printing_cents=row.printing_cents,
        shipping_cents=row.shipping_cents,
        retail_cents=row.retail_cents,
        currency=row.currency,
        provider=row.provider or "",
        provider_order_id=row.provider_order_id or "",
        address=dict(row.address or {}),
        error=row.error or "",
        approved_at=row.approved_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PrintOrderStore:
    """The print orders for books, keyed by the order id."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, order_id: str) -> PrintOrder | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(PrintOrderRow).where(PrintOrderRow.id == order_id)
            ).one_or_none()
        return _row_to_order(row) if row is not None else None

    def latest_for_book(self, book_id: str) -> PrintOrder | None:
        """The most recent order for a book, whatever state it is in."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(PrintOrderRow)
                .where(PrintOrderRow.book_id == book_id)
                .order_by(PrintOrderRow.created_at.desc())
                .limit(1)
            ).one_or_none()
        return _row_to_order(row) if row is not None else None

    def current_approval(self, book_id: str) -> PrintOrder | None:
        """The latest approval that has not been turned into a submitted order."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(PrintOrderRow)
                .where(
                    PrintOrderRow.book_id == book_id,
                    PrintOrderRow.state == "approved",
                )
                .order_by(PrintOrderRow.created_at.desc())
                .limit(1)
            ).one_or_none()
        return _row_to_order(row) if row is not None else None

    def create_approval(
        self,
        *,
        book_id: str,
        user_id: str,
        pdf_sha256: str,
        page_count: int,
        printing_cents: int,
        shipping_cents: int,
        retail_cents: int,
        currency: str,
        provider: str,
    ) -> PrintOrder:
        """Record that this exact PDF was reviewed.

        A previous unsubmitted approval for the same book is superseded: the
        owner has just looked at the new file, and two approvals for one book
        would make it ambiguous what was sent.
        """
        order_id = new_order_id()
        now = _now()
        with self._engine.begin() as conn:
            conn.execute(
                update(PrintOrderRow)
                .where(
                    PrintOrderRow.book_id == book_id,
                    PrintOrderRow.state == "approved",
                )
                .values(state="superseded", updated_at=now)
            )
            conn.execute(
                insert(PrintOrderRow).values(
                    id=order_id,
                    book_id=book_id,
                    user_id=user_id,
                    state="approved",
                    pdf_sha256=pdf_sha256,
                    page_count=page_count,
                    printing_cents=printing_cents,
                    shipping_cents=shipping_cents,
                    retail_cents=retail_cents,
                    currency=currency,
                    provider=provider,
                    provider_order_id="",
                    address=None,
                    error=None,
                    approved_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        created = self.get(order_id)
        if created is None:  # pragma: no cover - the insert just succeeded
            raise RuntimeError("the print approval was written but could not be read back")
        return created

    def claim(self, order_id: str) -> PrintOrder | None:
        """Move an approval to `placing`, once, and return the claimed row.

        The transition is one conditional UPDATE, so two requests that both
        saw the same approval cannot both go on to charge and submit: the
        second finds no claimable row and gets None. This is what keeps a
        repeated form submit from placing two print jobs for one charge.

        A claim older than `_CLAIM_STALE_AFTER` may be taken over, so a process
        that died between the claim and the provider call does not strand the
        order: the retry finds the charge already in the ledger (it is keyed by
        the order id) and submits the job once.
        """
        now = _now()
        stale = now - _CLAIM_STALE_AFTER
        with self._engine.begin() as conn:
            claimed = conn.execute(
                update(PrintOrderRow)
                .where(
                    PrintOrderRow.id == order_id,
                    or_(
                        PrintOrderRow.state == "approved",
                        and_(
                            PrintOrderRow.state == "placing",
                            PrintOrderRow.updated_at < stale,
                        ),
                    ),
                )
                .values(state="placing", updated_at=now)
            )
            if claimed.rowcount != 1:
                return None
        return self.get(order_id)

    def release(self, order_id: str) -> None:
        """Put a claimed order back to `approved` after a charge was refused."""
        with self._engine.begin() as conn:
            conn.execute(
                update(PrintOrderRow)
                .where(PrintOrderRow.id == order_id, PrintOrderRow.state == "placing")
                .values(state="approved", updated_at=_now())
            )

    def mark_submitted(
        self,
        order_id: str,
        *,
        provider: str,
        provider_order_id: str,
        address: dict[str, str],
    ) -> PrintOrder | None:
        with self._engine.begin() as conn:
            conn.execute(
                update(PrintOrderRow)
                .where(PrintOrderRow.id == order_id, PrintOrderRow.state == "placing")
                .values(
                    state="submitted",
                    provider=provider,
                    provider_order_id=provider_order_id,
                    address=address,
                    error=None,
                    updated_at=_now(),
                )
            )
        return self.get(order_id)

    def mark_failed(self, order_id: str, *, provider: str, error: str) -> PrintOrder | None:
        with self._engine.begin() as conn:
            conn.execute(
                update(PrintOrderRow)
                .where(PrintOrderRow.id == order_id, PrintOrderRow.state == "placing")
                .values(
                    state="failed",
                    provider=provider,
                    error=error,
                    updated_at=_now(),
                )
            )
        return self.get(order_id)
