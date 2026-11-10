"""The wallet: the one implementation of `WalletStore`.

The ledger is the database — Postgres in production, the local SQLite file
otherwise — so an account's balance survives the death of the process that
took the payment.

Two things the schema gives this store for free, and it relies on both:

* the balance is `SUM(amount_cents)`, so it is always the ledger's own answer;
* `(user_id, kind, reference)` is unique, so a duplicate funding credit or book
  charge raises `IntegrityError`, which becomes the contract's
  `DuplicateTransaction` — the idempotency the caller already understands.

Refund claiming is a single conditional `UPDATE ... WHERE status='requested'`,
so two clicks cannot both send the money back. That is the one place the
database does more than the file store can: a genuinely atomic claim.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy import insert as sa_insert
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from app.db import WalletRefundRow, WalletTransactionRow
from app.services.wallet import (
    REFUND_PROCESSING,
    REFUND_REQUESTED,
    DuplicateTransaction,
    RefundRequest,
    WalletStore,
    WalletTransaction,
)

__all__ = ["DatabaseWalletStore"]

logger = logging.getLogger(__name__)


class DatabaseWalletStore(WalletStore):
    """The ledger and refund requests, in Postgres (or SQLite in tests)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def balance_cents(self, owner: str) -> int:
        with self._engine.connect() as conn:
            total = conn.execute(
                select(func.coalesce(func.sum(WalletTransactionRow.amount_cents), 0)).where(
                    WalletTransactionRow.user_id == owner
                )
            ).scalar_one()
        return int(total or 0)

    def transactions(self, owner: str) -> list[WalletTransaction]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    select(WalletTransactionRow)
                    .where(WalletTransactionRow.user_id == owner)
                    .order_by(WalletTransactionRow.created_at, WalletTransactionRow.id)
                )
                .mappings()
                .all()
            )
        return [_transaction(row) for row in rows]

    def add(
        self,
        *,
        owner: str,
        amount_cents: int,
        kind: str,
        description: str,
        reference: str | None = None,
        payment_id: str | None = None,
    ) -> WalletTransaction:
        row = WalletTransactionRow(
            id=_new_id(),
            user_id=owner,
            amount_cents=amount_cents,
            kind=kind,
            description=description,
            reference=reference,
            payment_id=payment_id,
            created_at=_now(),
        )
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    sa_insert(WalletTransactionRow).values(
                        id=row.id,
                        user_id=row.user_id,
                        amount_cents=row.amount_cents,
                        kind=row.kind,
                        description=row.description,
                        reference=row.reference,
                        payment_id=row.payment_id,
                        created_at=row.created_at,
                    )
                )
        except IntegrityError as exc:
            raise DuplicateTransaction(reference or "") from exc
        return WalletTransaction(
            id=row.id,
            owner=row.user_id,
            amount_cents=row.amount_cents,
            kind=row.kind,
            description=row.description,
            reference=row.reference,
            payment_id=row.payment_id,
            created_at=row.created_at.isoformat(),
        )

    def refunds(self, owner: str) -> list[RefundRequest]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    select(WalletRefundRow)
                    .where(WalletRefundRow.user_id == owner)
                    .order_by(WalletRefundRow.created_at.desc(), WalletRefundRow.id.desc())
                )
                .mappings()
                .all()
            )
        return [_refund(row) for row in rows]

    def all_refunds(self, *, limit: int = 100) -> list[RefundRequest]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    select(WalletRefundRow)
                    .order_by(WalletRefundRow.created_at.desc(), WalletRefundRow.id.desc())
                    .limit(max(1, min(limit, 500)))
                )
                .mappings()
                .all()
            )
        return [_refund(row) for row in rows]

    def save_refund(self, request: RefundRequest) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa_insert(WalletRefundRow).values(
                    id=request.id,
                    user_id=request.owner,
                    amount_cents=request.amount_cents,
                    status=request.status,
                    provider_refund_ids=list(request.provider_refund_ids),
                    created_at=_now(),
                    processed_at=None,
                    note=request.note or None,
                )
            )

    def get_refund(self, refund_id: str) -> RefundRequest | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(select(WalletRefundRow).where(WalletRefundRow.id == refund_id))
                .mappings()
                .one_or_none()
            )
        return _refund(row) if row is not None else None

    def claim_refund(self, refund_id: str) -> bool:
        with self._engine.begin() as conn:
            claimed = conn.execute(
                update(WalletRefundRow)
                .where(
                    WalletRefundRow.id == refund_id,
                    WalletRefundRow.status == REFUND_REQUESTED,
                )
                .values(status=REFUND_PROCESSING)
            )
        return bool(claimed.rowcount)

    def finish_refund(
        self,
        refund_id: str,
        *,
        status: str,
        provider_refund_ids: Sequence[str],
        note: str,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(WalletRefundRow)
                .where(WalletRefundRow.id == refund_id)
                .values(
                    status=status,
                    provider_refund_ids=list(provider_refund_ids),
                    processed_at=_now(),
                    note=note or None,
                )
            )


def _transaction(row: RowMapping) -> WalletTransaction:
    created_at: datetime = row["created_at"]
    return WalletTransaction(
        id=str(row["id"]),
        owner=str(row["user_id"]),
        amount_cents=int(row["amount_cents"]),
        kind=str(row["kind"]),
        description=str(row["description"]),
        reference=row["reference"],
        payment_id=row["payment_id"],
        created_at=created_at.isoformat() if created_at else "",
    )


def _refund(row: RowMapping) -> RefundRequest:
    raw_ids: Sequence[object] = row["provider_refund_ids"] or ()
    created_at: datetime = row["created_at"]
    processed_at: datetime | None = row["processed_at"]
    return RefundRequest(
        id=str(row["id"]),
        owner=str(row["user_id"]),
        amount_cents=int(row["amount_cents"]),
        status=str(row["status"]),
        provider_refund_ids=tuple(str(item) for item in raw_ids),
        created_at=created_at.isoformat() if created_at else "",
        processed_at=processed_at.isoformat() if processed_at else None,
        note=row["note"] or "",
    )


def _new_id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    """Naive UTC, the shape every dialect stores in a plain DateTime column."""
    return datetime.now(UTC).replace(tzinfo=None)
