"""Accounts and money: a ledger, a balance, and refunds.

Every account has a wallet. The wallet is a list of signed transactions, not a
stored balance: the balance is their sum. That is the only shape in which the
transaction-history page and the balance can never disagree, and it makes every
movement auditable — what it was, when, and which book or Stripe session caused
it.

The rules:

* **One picture book is one document, and one document costs 99¢.** The start
  endpoint checks the balance first, so nobody starts a long render they cannot
  pay for, and the fee is actually taken when the finished PDF is written — a
  book that never produces a document is never charged. A book and its fee are
  tied together by the book id in the transaction's `reference`, which also
  makes the charge idempotent: the same book cannot be charged twice.
* **A refund is requested, then processed.** Requesting moves the money out of
  the spendable balance immediately and records a pending request. Processing
  sends it back through Stripe, one funding payment at a time, oldest first,
  and marks the request done. If Stripe refuses, the money goes back into the
  wallet — a failed refund must cost the visitor nothing.
* **Funding is credited only from a paid Stripe session**, keyed by the session
  id, so a refreshed success page or a double-click cannot fund twice.

`WalletStore` has a filesystem implementation here and a database one in
`db_wallet_store.py`. `Wallet` is the business logic both share.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from app.services.payments import PaymentProvider

__all__ = [
    "CURRENCY",
    "DOCUMENT_PRICE_CENTS",
    "FUNDING_AMOUNTS_CENTS",
    "DuplicateTransaction",
    "FileWalletStore",
    "InsufficientFunds",
    "RefundRequest",
    "Wallet",
    "WalletError",
    "WalletStore",
    "WalletTransaction",
    "format_cents",
]

logger = logging.getLogger(__name__)

#: One picture book — cover, nineteen story pages, the About page and the PDF —
#: is the "document", and this is its price.
DOCUMENT_PRICE_CENTS = 99

#: What the top-up form offers. Closed set, like every other amount a visitor
#: can choose: no free-text amount reaches Stripe.
FUNDING_AMOUNTS_CENTS = (500, 1000, 2000)

CURRENCY = "usd"

# Transaction kinds. Kept as strings in storage so a ledger written today is
# readable forever, and so a human reading the JSON knows what it is.
KIND_FUNDING = "funding"
KIND_CHARGE = "charge"
KIND_REFUND = "refund"
KIND_REFUND_REVERSAL = "refund_reversal"

REFUND_REQUESTED = "requested"
REFUND_PROCESSING = "processing"
REFUND_PROCESSED = "processed"
REFUND_FAILED = "failed"


class WalletError(RuntimeError):
    """Something about this money movement is not allowed."""


class InsufficientFunds(WalletError):
    """The account cannot cover the charge."""


class DuplicateTransaction(WalletError):
    """This exact movement has already been recorded."""


@dataclass(frozen=True, slots=True)
class WalletTransaction:
    """One line of the ledger: money in (positive) or out (negative)."""

    id: str
    owner: str
    amount_cents: int
    kind: str
    description: str
    reference: str | None
    payment_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class RefundRequest:
    """A request to send money back, and what happened to it."""

    id: str
    owner: str
    amount_cents: int
    status: str
    provider_refund_ids: tuple[str, ...] = ()
    created_at: str = ""
    processed_at: str | None = None
    note: str = ""


class WalletStore:
    """The persistence contract: append to the ledger, read it back."""

    def balance_cents(self, owner: str) -> int:
        raise NotImplementedError

    def transactions(self, owner: str) -> list[WalletTransaction]:
        """Every transaction, oldest first (the order a ledger is read in)."""
        raise NotImplementedError

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
        """Append one movement.

        Raises:
            DuplicateTransaction: when `reference` was already used for this
                owner and kind. Callers treat that as "already applied".
        """
        raise NotImplementedError

    def refunds(self, owner: str) -> list[RefundRequest]:
        """This account's refund requests, newest first."""
        raise NotImplementedError

    def save_refund(self, request: RefundRequest) -> None:
        raise NotImplementedError

    def get_refund(self, refund_id: str) -> RefundRequest | None:
        raise NotImplementedError

    def claim_refund(self, refund_id: str) -> bool:
        """Move a `requested` refund to `processing`, exactly once.

        Returns whether this caller won the claim. That is what stops two
        clicks — or two requests — from sending the same money back twice.
        """
        raise NotImplementedError

    def finish_refund(
        self,
        refund_id: str,
        *,
        status: str,
        provider_refund_ids: Sequence[str],
        note: str,
    ) -> None:
        raise NotImplementedError


class Wallet:
    """The account's money rules, over any `WalletStore`.

    One process-level lock serialises the read-then-write pairs (check the
    balance, then debit it). A multi-process deployment would need the same
    guarantee in the store; Render runs one web process, and the wallet's
    amounts are small enough that this is the honest implementation for it.
    """

    def __init__(self, store: WalletStore, *, price_cents: int = DOCUMENT_PRICE_CENTS) -> None:
        self._store = store
        self.price_cents = price_cents
        # Re-entrant: `request_refund` holds the lock while calling the
        # refundable-amount helper, which takes it again.
        self._lock = threading.RLock()

    # --- reading ---------------------------------------------------------------

    def balance_cents(self, owner: str) -> int:
        return self._store.balance_cents(owner)

    def can_afford_document(self, owner: str) -> bool:
        return self.balance_cents(owner) >= self.price_cents

    def history(self, owner: str) -> list[WalletTransaction]:
        """The ledger, newest first, as the history page shows it."""
        return list(reversed(self._store.transactions(owner)))

    def refund_requests(self, owner: str) -> list[RefundRequest]:
        return self._store.refunds(owner)

    def refundable_cents(self, owner: str) -> int:
        """How much can genuinely go back to the card right now.

        Two caps, both real: the spendable balance (money already spent on a
        book cannot be refunded by asking again), and the part of the funding
        payments Stripe has not already refunded.
        """
        with self._lock:
            return min(
                self._store.balance_cents(owner),
                sum(remaining for _, remaining in self._funding_remaining(owner)),
            )

    # --- funding ---------------------------------------------------------------

    def fund(
        self,
        owner: str,
        *,
        amount_cents: int,
        session_id: str,
        payment_id: str,
        description: str,
    ) -> tuple[WalletTransaction, bool]:
        """Credit a paid Stripe session once.

        Returns the transaction and whether it was newly created. A duplicate
        is not an error here: it is the refreshed success page doing its job.
        """
        if amount_cents <= 0:
            raise WalletError("A top-up has to be a positive amount.")
        try:
            transaction = self._store.add(
                owner=owner,
                amount_cents=amount_cents,
                kind=KIND_FUNDING,
                description=description,
                reference=session_id,
                payment_id=payment_id,
            )
        except DuplicateTransaction:
            existing = next(
                (
                    entry
                    for entry in self._store.transactions(owner)
                    if entry.kind == KIND_FUNDING and entry.reference == session_id
                ),
                None,
            )
            if existing is None:  # pragma: no cover - the store said so, then lost it
                raise
            return existing, False
        logger.info(
            "wallet.funded owner=%s cents=%d session=%s", owner, amount_cents, session_id
        )
        return transaction, True

    # --- the document fee ------------------------------------------------------

    def charge_document(
        self, owner: str, *, book_id: str, title: str, require_funds: bool = True
    ) -> WalletTransaction:
        """Take the document fee for a book, once.

        Called when the finished PDF is written, so the fee is tied to a
        document that actually exists; a book that never finishes is never
        charged. The start endpoint checks the balance first so a visitor is
        warned before a long render, but that check is a courtesy: this call is
        what keeps the ledger true, and `require_funds=False` lets a render
        that was already paid for in the check finish even if the balance moved
        while it ran (the account then owes the difference, and the next book
        is refused).

        Raises:
            InsufficientFunds: when `require_funds` is true and the balance
                cannot cover one document.
        """
        reference = f"book:{book_id}"
        with self._lock:
            for entry in self._store.transactions(owner):
                if entry.kind == KIND_CHARGE and entry.reference == reference:
                    return entry
            if require_funds and self._store.balance_cents(owner) < self.price_cents:
                raise InsufficientFunds(
                    f"A book costs {format_cents(self.price_cents)} and the balance "
                    f"is {format_cents(self._store.balance_cents(owner))}."
                )
            transaction = self._store.add(
                owner=owner,
                amount_cents=-self.price_cents,
                kind=KIND_CHARGE,
                description=f'Book "{title}" — the finished picture book (PDF)',
                reference=reference,
            )
        logger.info(
            "wallet.charged owner=%s book=%s cents=%d", owner, book_id, self.price_cents
        )
        return transaction

    # --- refunds ---------------------------------------------------------------

    def request_refund(self, owner: str, *, amount_cents: int | None = None) -> RefundRequest:
        """Take `amount_cents` (or everything refundable) out of the balance.

        The debit happens here, with the request, so the same money cannot be
        requested twice while the request is waiting to be processed.
        """
        with self._lock:
            available = self.refundable_cents(owner)
            amount = available if amount_cents is None else amount_cents
            if amount <= 0:
                raise WalletError("There is nothing to refund yet.")
            if amount > available:
                raise WalletError(
                    f"At most {format_cents(available)} can be refunded right now."
                )
            request = RefundRequest(
                id=uuid.uuid4().hex,
                owner=owner,
                amount_cents=amount,
                status=REFUND_REQUESTED,
                created_at=_now(),
            )
            self._store.add(
                owner=owner,
                amount_cents=-amount,
                kind=KIND_REFUND,
                description=f"Refund requested — {format_cents(amount)} back to the card",
                reference=f"refund-request:{request.id}",
            )
            try:
                self._store.save_refund(request)
            except Exception:
                # The debit landed but the request did not. Put the money back
                # rather than leaving it spent on nothing; a second failure to
                # write is beyond saving here, and the log is the operator's
                # clue.
                self._store.add(
                    owner=owner,
                    amount_cents=amount,
                    kind=KIND_REFUND_REVERSAL,
                    description=(
                        "Refund could not be recorded — "
                        f"{format_cents(amount)} returned to your balance"
                    ),
                    reference=f"refund-reversal:{request.id}",
                )
                raise
        logger.info(
            "wallet.refund_requested owner=%s cents=%d id=%s", owner, amount, request.id
        )
        return request

    def process_refund(
        self, owner: str, refund_id: str, provider: PaymentProvider
    ) -> RefundRequest:
        """Send a requested refund through Stripe, one funding payment at a time."""
        request = self._store.get_refund(refund_id)
        if request is None or request.owner != owner:
            raise WalletError("No refund request with that id.")
        if request.status != REFUND_REQUESTED:
            return request
        if not self._store.claim_refund(refund_id):
            current = self._store.get_refund(refund_id)
            return current if current is not None else request

        refund_ids: list[str] = []
        outstanding = request.amount_cents
        try:
            with self._lock:
                remaining = self._funding_remaining(owner)
            for payment_id, available in remaining:
                if outstanding <= 0:
                    break
                portion = min(outstanding, available)
                if portion <= 0 or not payment_id:
                    continue
                refund = provider.refund(payment_intent=payment_id, amount_cents=portion)
                refund_ids.append(str(refund.id))
                outstanding -= int(refund.amount_cents)
            if outstanding > 0:
                raise WalletError("Stripe had no funding payment left to refund that amount.")
        except Exception as exc:
            # Only the part that never reached the card goes back. When the
            # first funding payment refunded and the second was refused,
            # reversing the whole request would credit the first half twice.
            unrecovered = max(0, outstanding)
            if unrecovered:
                self._store.add(
                    owner=owner,
                    amount_cents=unrecovered,
                    kind=KIND_REFUND_REVERSAL,
                    description=(
                        f"Refund could not be completed — "
                        f"{format_cents(unrecovered)} returned to your balance"
                    ),
                    reference=f"refund-reversal:{refund_id}",
                )
            self._store.finish_refund(
                refund_id,
                status=REFUND_FAILED,
                provider_refund_ids=refund_ids,
                note=_safe_note(exc),
            )
            logger.warning(
                "wallet.refund_failed owner=%s id=%s reason=%s",
                owner,
                refund_id,
                type(exc).__name__,
            )
            failed = self._store.get_refund(refund_id)
            return failed if failed is not None else request

        self._store.finish_refund(
            refund_id,
            status=REFUND_PROCESSED,
            provider_refund_ids=refund_ids,
            note="",
        )
        logger.info(
            "wallet.refunded owner=%s id=%s cents=%d payments=%s",
            owner,
            refund_id,
            request.amount_cents,
            ",".join(refund_ids) or "-",
        )
        processed = self._store.get_refund(refund_id)
        return processed if processed is not None else request

    # --- internals -------------------------------------------------------------

    def _funding_remaining(self, owner: str) -> list[tuple[str, int]]:
        """Each funding payment and what is left of it, oldest first.

        Only *processed* refund requests consume a funding payment: a request
        that is still pending has taken the money out of the wallet balance,
        but the card refund has not happened yet, and a failed one was put
        back. The order is first-in-first-out — the top-up that has been on
        the account longest goes back first, which is also how the Stripe
        dashboard presents it.
        """
        fundings = [
            (entry.payment_id or "", entry.amount_cents)
            for entry in self._store.transactions(owner)
            if entry.kind == KIND_FUNDING
        ]
        already_refunded = sum(
            request.amount_cents
            for request in self._store.refunds(owner)
            if request.status == REFUND_PROCESSED
        )
        remaining: list[tuple[str, int]] = []
        consumed = 0
        for payment_id, amount in fundings:
            taken = max(0, min(amount, already_refunded - consumed))
            consumed += taken
            remaining.append((payment_id, amount - taken))
        return remaining


def format_cents(cents: int) -> str:
    """`-99` → `-\u2212$0.99`; `500` → `$5.00`."""
    sign = "\u2212" if cents < 0 else ""
    whole, part = divmod(abs(cents), 100)
    return f"{sign}${whole}.{part:02d}"


class FileWalletStore(WalletStore):
    """One JSON file per account, under `<storage>/wallets`.

    The owner id is hashed into the filename rather than used directly: a
    Google `sub` is safe today, but the ledger is not the place to test that,
    and the payload keeps the real id for reading.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # --- the document ----------------------------------------------------------

    def _path(self, owner: str) -> Path:
        digest = hashlib.sha256(owner.encode()).hexdigest()[:32]
        return self.root / f"{digest}.json"

    def _read(self, owner: str) -> dict[str, object]:
        path = self._path(owner)
        if not path.is_file():
            return {"owner": owner, "transactions": [], "refunds": []}
        try:
            payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            # Silence here would turn a corrupt ledger into a zero balance,
            # and the next write would overwrite the history with it. Say so;
            # the operator can recover the file before any money moves again.
            logger.warning("wallet.ledger_unreadable path=%s", path.name)
            return {"owner": owner, "transactions": [], "refunds": []}
        payload.setdefault("transactions", [])
        payload.setdefault("refunds", [])
        return payload

    def _write(self, owner: str, payload: dict[str, object]) -> None:
        path = self._path(owner)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _transactions_of(payload: dict[str, object]) -> list[dict[str, object]]:
        raw = payload.get("transactions", [])
        return [dict(entry) for entry in cast("list[object]", raw) if isinstance(entry, dict)]

    @staticmethod
    def _refunds_of(payload: dict[str, object]) -> list[dict[str, object]]:
        raw = payload.get("refunds", [])
        return [dict(entry) for entry in cast("list[object]", raw) if isinstance(entry, dict)]

    # --- the contract ----------------------------------------------------------

    def balance_cents(self, owner: str) -> int:
        with self._lock:
            entries = self._transactions_of(self._read(owner))
        return sum(int(cast(int, entry.get("amount_cents", 0))) for entry in entries)

    def transactions(self, owner: str) -> list[WalletTransaction]:
        with self._lock:
            entries = self._transactions_of(self._read(owner))
        return [_transaction(entry) for entry in sorted(entries, key=_transaction_order)]

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
        with self._lock:
            payload = self._read(owner)
            entries = self._transactions_of(payload)
            if reference is not None and any(
                entry.get("reference") == reference and entry.get("kind") == kind
                for entry in entries
            ):
                raise DuplicateTransaction(reference)
            entry = {
                "id": uuid.uuid4().hex,
                "owner": owner,
                "amount_cents": amount_cents,
                "kind": kind,
                "description": description,
                "reference": reference,
                "payment_id": payment_id,
                "created_at": _now(),
            }
            entries.append(entry)
            payload["transactions"] = entries
            payload["owner"] = owner
            self._write(owner, payload)
        return _transaction(entry)

    def refunds(self, owner: str) -> list[RefundRequest]:
        with self._lock:
            entries = self._refunds_of(self._read(owner))
        return [_refund(entry) for entry in reversed(entries)]

    def save_refund(self, request: RefundRequest) -> None:
        with self._lock:
            payload = self._read(request.owner)
            entries = self._refunds_of(payload)
            entries.append(_refund_payload(request))
            payload["refunds"] = entries
            payload["owner"] = request.owner
            self._write(request.owner, payload)

    def get_refund(self, refund_id: str) -> RefundRequest | None:
        with self._lock:
            for _, payload in self._ledgers():
                for entry in self._refunds_of(payload):
                    if entry.get("id") == refund_id:
                        return _refund(entry)
        return None

    def claim_refund(self, refund_id: str) -> bool:
        with self._lock:
            for _, payload in self._ledgers():
                entries = self._refunds_of(payload)
                for entry in entries:
                    if entry.get("id") != refund_id:
                        continue
                    if entry.get("status") != REFUND_REQUESTED:
                        return False
                    entry["status"] = REFUND_PROCESSING
                    payload["refunds"] = entries
                    self._write(str(payload.get("owner", "")), payload)
                    return True
        return False

    def finish_refund(
        self,
        refund_id: str,
        *,
        status: str,
        provider_refund_ids: Sequence[str],
        note: str,
    ) -> None:
        with self._lock:
            for _, payload in self._ledgers():
                entries = self._refunds_of(payload)
                for entry in entries:
                    if entry.get("id") != refund_id:
                        continue
                    entry["status"] = status
                    entry["provider_refund_ids"] = list(provider_refund_ids)
                    entry["processed_at"] = _now()
                    entry["note"] = note
                    payload["refunds"] = entries
                    self._write(str(payload.get("owner", "")), payload)
                    return

    # --- internals -------------------------------------------------------------

    def _ledgers(self) -> Iterator[tuple[Path, dict[str, object]]]:
        """Every account file, parsed; a corrupt file is skipped and reported.

        Refund requests are looked up by id across accounts because the id is
        the only thing processing has. One helper keeps that scan — and its
        corruption handling — from being written three times.
        """
        for path in self.root.iterdir():
            if path.suffix != ".json" or not path.is_file():
                continue
            try:
                payload = cast(
                    "dict[str, object]", json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError):
                logger.warning("wallet.ledger_unreadable path=%s", path.name)
                continue
            yield path, payload


def _transaction(entry: dict[str, object]) -> WalletTransaction:
    reference = entry.get("reference")
    payment_id = entry.get("payment_id")
    return WalletTransaction(
        id=str(entry.get("id", "")),
        owner=str(entry.get("owner", "")),
        amount_cents=int(cast(int, entry.get("amount_cents", 0))),
        kind=str(entry.get("kind", "")),
        description=str(entry.get("description", "")),
        reference=str(reference) if reference else None,
        payment_id=str(payment_id) if payment_id else None,
        created_at=str(entry.get("created_at", "")),
    )


def _refund(entry: dict[str, object]) -> RefundRequest:
    raw_ids = entry.get("provider_refund_ids", [])
    ids = tuple(str(item) for item in cast("list[object]", raw_ids) if item)
    processed = entry.get("processed_at")
    return RefundRequest(
        id=str(entry.get("id", "")),
        owner=str(entry.get("owner", "")),
        amount_cents=int(cast(int, entry.get("amount_cents", 0))),
        status=str(entry.get("status", "")),
        provider_refund_ids=ids,
        created_at=str(entry.get("created_at", "")),
        processed_at=str(processed) if processed else None,
        note=str(entry.get("note", "")),
    )


def _refund_payload(request: RefundRequest) -> dict[str, object]:
    return {
        "id": request.id,
        "owner": request.owner,
        "amount_cents": request.amount_cents,
        "status": request.status,
        "provider_refund_ids": list(request.provider_refund_ids),
        "created_at": request.created_at,
        "processed_at": request.processed_at,
        "note": request.note,
    }


def _transaction_order(entry: dict[str, object]) -> tuple[str, str]:
    return (str(entry.get("created_at", "")), str(entry.get("id", "")))


def _safe_note(exc: BaseException) -> str:
    """A one-line, secret-free reason for the history page."""
    text = str(exc) or type(exc).__name__
    return text[:200]


def _now() -> str:
    return datetime.now(UTC).isoformat()
