"""The wallet's rules, over the database ledger.

The wallet is the only place in the app that decides what money may move, so
these tests are about the guarantees rather than the plumbing: a session funds
once, a book is charged once, a refund is requested then processed (and cannot
be processed twice), and a failed refund costs the visitor nothing.
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine

from app.services.db_wallet_store import DatabaseWalletStore
from app.services.payments import PaidCheckout, PaymentError, PaymentProvider, Refund
from app.services.wallet import (
    DOCUMENT_PRICE_CENTS,
    FUNDING_AMOUNTS_CENTS,
    REFUND_FAILED,
    REFUND_PROCESSED,
    DuplicateTransaction,
    InsufficientFunds,
    RefundRequest,
    Wallet,
    WalletError,
    format_cents,
)


class FakeStripe(PaymentProvider):
    """Records refund calls, or refuses them on demand."""

    name = "fake"

    def __init__(self, *, fail: bool = False, fail_after: int | None = None) -> None:
        self.fail = fail
        self.fail_after = fail_after
        self.refunds: list[tuple[str, int]] = []

    def refund(self, *, payment_intent: str, amount_cents: int) -> Refund:
        self.refunds.append((payment_intent, amount_cents))
        if self.fail or (self.fail_after is not None and len(self.refunds) > self.fail_after):
            raise PaymentError("Stripe payment failed: card_declined")
        return Refund(
            id=f"re_{len(self.refunds)}", amount_cents=amount_cents, status="succeeded"
        )

    def create_checkout(self, **kwargs: object) -> object:  # pragma: no cover - unused
        raise NotImplementedError

    def read_checkout(self, session_id: str) -> PaidCheckout:  # pragma: no cover - unused
        raise NotImplementedError


@pytest.fixture
def wallet(engine: Engine) -> Wallet:
    return Wallet(DatabaseWalletStore(engine))


def fund(wallet: Wallet, owner: str, cents: int = 1000, session_id: str = "cs_1") -> None:
    wallet.fund(
        owner,
        amount_cents=cents,
        session_id=session_id,
        payment_id=f"pi_{session_id}",
        description="Top-up",
    )


def test_the_balance_is_the_sum_of_the_ledger(wallet: Wallet) -> None:
    assert wallet.balance_cents("me") == 0
    fund(wallet, "me", 500)
    fund(wallet, "me", 1000, session_id="cs_2")
    assert wallet.balance_cents("me") == 1500
    assert len(wallet.history("me")) == 2


def test_the_history_is_read_one_page_at_a_time(wallet: Wallet) -> None:
    """S5: a year-old ledger is not read in full to show one screen of it."""
    for index in range(60):
        fund(wallet, "me", 500, session_id=f"cs_{index}")
    first, total = wallet.history_page("me", limit=50, offset=0)
    assert total == 60 and len(first) == 50
    last, counted_again = wallet.history_page("me", limit=50, offset=50)
    assert counted_again == 60 and len(last) == 10
    # No row appears on both pages, and the newest row is first.
    assert {row.id for row in first}.isdisjoint({row.id for row in last})
    assert first[0].created_at >= last[-1].created_at
    # The filters narrow the count, not only the rows.
    assert wallet.history_page("me", kinds=("charge",)) == ([], 0)


def test_one_stripe_session_funds_once(wallet: Wallet) -> None:
    fund(wallet, "me", 1000)
    transaction, created = wallet.fund(
        "me",
        amount_cents=1000,
        session_id="cs_1",
        payment_id="pi_cs_1",
        description="Top-up",
    )
    assert created is False
    assert transaction.amount_cents == 1000
    assert wallet.balance_cents("me") == 1000


def test_a_refreshed_page_cannot_credit_a_second_time(wallet: Wallet) -> None:
    for _ in range(3):
        wallet.fund(
            "me",
            amount_cents=500,
            session_id="cs_same",
            payment_id="pi_same",
            description="Top-up",
        )
    assert wallet.balance_cents("me") == 500


def test_a_book_is_charged_once_however_often_its_pdf_is_rebuilt(wallet: Wallet) -> None:
    fund(wallet, "me", 1000)
    first = wallet.charge_document("me", book_id="book-1", title="My Book")
    again = wallet.charge_document("me", book_id="book-1", title="My Book")
    assert first.id == again.id
    assert wallet.balance_cents("me") == 1000 - DOCUMENT_PRICE_CENTS


def test_a_book_is_refused_when_the_wallet_cannot_pay(wallet: Wallet) -> None:
    with pytest.raises(InsufficientFunds):
        wallet.charge_document("me", book_id="book-1", title="My Book")
    assert wallet.balance_cents("me") == 0


def test_a_book_already_rendering_still_gets_its_pdf(wallet: Wallet) -> None:
    """The start check is a courtesy; the finished document is not thrown away."""
    wallet.charge_document("me", book_id="book-1", title="My Book", require_funds=False)
    assert wallet.balance_cents("me") == -DOCUMENT_PRICE_CENTS


def test_owners_do_not_share_a_wallet(wallet: Wallet) -> None:
    fund(wallet, "me", 1000)
    assert wallet.balance_cents("someone-else") == 0


def test_a_refund_is_requested_then_processed(wallet: Wallet) -> None:
    fund(wallet, "me", 1000)
    stripe = FakeStripe()
    request = wallet.request_refund("me")
    # The money leaves the balance with the request, so it cannot be asked for
    # twice while it is on its way.
    assert wallet.balance_cents("me") == 0
    assert request.status == "requested"

    processed = wallet.process_refund("me", request.id, stripe)
    assert processed.status == REFUND_PROCESSED
    assert stripe.refunds == [("pi_cs_1", 1000)]
    assert wallet.refundable_cents("me") == 0


def test_processing_the_same_refund_twice_sends_the_money_once(wallet: Wallet) -> None:
    fund(wallet, "me", 1000)
    stripe = FakeStripe()
    request = wallet.request_refund("me")
    wallet.process_refund("me", request.id, stripe)
    again = wallet.process_refund("me", request.id, stripe)
    assert again.status == REFUND_PROCESSED
    assert stripe.refunds == [("pi_cs_1", 1000)]


def test_a_failed_refund_puts_the_money_back(wallet: Wallet) -> None:
    fund(wallet, "me", 1000)
    request = wallet.request_refund("me")
    processed = wallet.process_refund("me", request.id, FakeStripe(fail=True))
    assert processed.status == REFUND_FAILED
    assert "card_declined" in processed.note
    assert wallet.balance_cents("me") == 1000


def test_a_partly_completed_refund_returns_only_the_unsent_part(wallet: Wallet) -> None:
    """One funding payment refunded, the next refused: no double credit."""
    fund(wallet, "me", 500)
    fund(wallet, "me", 500, session_id="cs_2")
    request = wallet.request_refund("me")
    failed = wallet.process_refund("me", request.id, FakeStripe(fail_after=1))

    assert failed.status == REFUND_FAILED
    assert len(failed.provider_refund_ids) == 1
    # The half that reached the card is not back in the wallet; the half that
    # never left is.
    assert wallet.balance_cents("me") == 500


def test_nothing_can_be_refunded_twice(wallet: Wallet) -> None:
    fund(wallet, "me", 1000)
    wallet.process_refund("me", wallet.request_refund("me").id, FakeStripe())
    with pytest.raises(WalletError):
        wallet.request_refund("me")


def test_a_refund_that_cannot_be_recorded_does_not_keep_the_money(engine: Engine) -> None:
    class BrokenStore(DatabaseWalletStore):
        def save_refund(self, request: RefundRequest) -> None:
            raise RuntimeError("the database is down")

    broken = Wallet(BrokenStore(engine))
    fund(broken, "me", 1000)
    with pytest.raises(RuntimeError):
        broken.request_refund("me")
    assert broken.balance_cents("me") == 1000


def test_a_book_that_was_paid_for_cannot_be_refunded_as_cash(wallet: Wallet) -> None:
    fund(wallet, "me", 1000)
    wallet.charge_document("me", book_id="book-1", title="My Book")
    assert wallet.refundable_cents("me") == 1000 - DOCUMENT_PRICE_CENTS


def test_a_refund_never_exceeds_what_stripe_can_send_back(wallet: Wallet) -> None:
    """A wall of credit does not make a card refund possible."""
    fund(wallet, "me", 500)
    fund(wallet, "me", 500, session_id="cs_2")
    wallet.process_refund("me", wallet.request_refund("me").id, FakeStripe())
    assert wallet.refundable_cents("me") == 0


def test_a_duplicate_ledger_reference_is_refused(engine: Engine) -> None:
    store = DatabaseWalletStore(engine)

    def add() -> None:
        store.add(
            owner="me",
            amount_cents=500,
            kind="funding",
            description="Top-up",
            reference="cs_1",
        )

    add()
    with pytest.raises(DuplicateTransaction):
        add()


def test_a_negative_top_up_is_refused(wallet: Wallet) -> None:
    with pytest.raises(WalletError):
        fund(wallet, "me", -100)


def test_a_stripe_key_wires_money_at_the_composition_root(settings) -> None:
    """The container is the only place that decides whether money is on."""
    from app.container import build_container
    from tests.helpers import RecordingProvider

    configured = settings.model_copy(
        update={"stripe_secret_key": "test-placeholder-not-a-real-key"}
    )
    container = build_container(configured, provider=RecordingProvider())
    assert container.payments is not None
    assert container.wallet.price_cents == DOCUMENT_PRICE_CENTS


def test_the_closed_set_of_top_up_amounts_is_what_the_page_offers() -> None:
    assert FUNDING_AMOUNTS_CENTS == (500, 1000, 2000)


@pytest.mark.parametrize(
    ("cents", "label"),
    [(0, "$0.00"), (99, "$0.99"), (500, "$5.00"), (-99, "\u2212$0.99")],
)
def test_money_is_written_the_way_a_person_reads_it(cents: int, label: str) -> None:
    assert format_cents(cents) == label
