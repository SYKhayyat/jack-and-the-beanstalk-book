"""Deleting an account, and the clean-up that makes deleted things really go (A8).

Three promises:

* deleting an account removes the person's books, saved child, portrait log
  and name, keeps the pseudonymous wallet ledger, and signs them out;
* nothing is torn down under a book still being drawn;
* a picture no row points at any more leaves the object store (the sweep),
  and a remembered child nobody uses is forgotten on its own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.blob_sweep import sweep_orphans
from app.blobs import MemoryBlobStore, set_blob_store
from app.db import (
    Base,
    BookRow,
    ChildProfileRow,
    PortraitAttemptRow,
    PrintOrderRow,
    UserRow,
    WalletTransactionRow,
)
from app.services.accounts import AccountBusy, delete_account
from app.services.child_profiles import ChildProfile, DatabaseChildProfileStore
from app.sessions import SESSION_COOKIE
from tests.helpers import JACK_KEY
from tests.test_child_profile import (
    OTHER,
    OWNER,
    _portrait,
    _remember,
    _sign_in,
    _site,
    _wait_for_book,
)


def _engine():  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine


def test_deleting_the_account_removes_what_the_person_made(
    tmp_path: Path, seeded_template: Path
) -> None:
    with _site(tmp_path, seeded_template) as (client, _provider):
        container = client.app.state.container  # type: ignore[attr-defined]
        _sign_in(client, OTHER)
        _remember(client, _portrait(client), name="Other")
        _sign_in(client, OWNER)
        generation = _portrait(client)
        _remember(client, generation)
        started = client.post(
            "/api/book/start", data={"story": JACK_KEY, "generation_id": generation}
        )
        assert started.status_code == 201, started.text
        book_id = started.json()["book_id"]
        assert _wait_for_book(client, book_id)["status"] == "complete"

        page = client.get("/privacy")
        assert page.status_code == 200
        assert "Delete my account" in page.text

        # The wrong word changes nothing.
        refused = client.post("/account/delete", data={"confirm": "yes"})
        assert refused.status_code == 400
        assert container.book_store.list_for_owner(OWNER)

        deleted = client.post("/account/delete", data={"confirm": " delete "})
        assert deleted.status_code == 200, deleted.text
        assert "Your account is deleted" in deleted.text
        assert SESSION_COOKIE in deleted.headers.get("set-cookie", "")

        assert container.book_store.load_record(book_id) is None
        assert container.child_profiles.get(OWNER) is None
        user = container.user_store.get(OWNER)
        assert user is not None and (user.email, user.name) == ("", "")
        with container.engine.connect() as conn:
            assert not conn.execute(
                select(func.count())
                .select_from(PortraitAttemptRow)
                .where(PortraitAttemptRow.user_id == OWNER)
            ).scalar_one()
        # Someone else's things are untouched.
        assert container.child_profiles.get(OTHER) is not None


def test_the_privacy_page_is_open_and_offers_no_deletion_without_a_person(
    tmp_path: Path, seeded_template: Path
) -> None:
    with _site(tmp_path, seeded_template) as (client, _provider):
        page = client.get("/privacy", follow_redirects=False)
        assert page.status_code == 200
        assert "What we keep" in page.text
        assert "Delete my account" not in page.text
        refused = client.post(
            "/account/delete", data={"confirm": "DELETE"}, follow_redirects=False
        )
        assert refused.status_code == 303
        assert refused.headers["location"].startswith("/login")


def test_a_book_being_drawn_blocks_the_deletion_and_nothing_changes() -> None:
    engine = _engine()
    with Session(engine) as session:
        session.add(UserRow(sub="u", email="u@example.com", name="U"))
        session.add(
            BookRow(id="b", user_id="u", status="rendering", generation_id="g", about={})
        )
        session.commit()
    with pytest.raises(AccountBusy):
        delete_account(engine, "u")
    with Session(engine) as session:
        assert session.get(BookRow, "b") is not None
        user = session.get(UserRow, "u")
        assert user is not None and user.email == "u@example.com"


def test_the_wallet_ledger_outlives_the_account() -> None:
    engine = _engine()
    with Session(engine) as session:
        session.add(UserRow(sub="u", email="u@example.com", name="U"))
        session.add(
            BookRow(id="b", user_id="u", status="complete", generation_id="g", about={})
        )
        session.add(
            WalletTransactionRow(
                id="t",
                user_id="u",
                kind="topup",
                amount_cents=500,
                reference="r",
                description="",
            )
        )
        session.commit()
    assert delete_account(engine, "u") == 1
    with Session(engine) as session:
        assert session.get(BookRow, "b") is None
        assert session.get(WalletTransactionRow, "t") is not None


def test_the_sweep_deletes_only_old_objects_no_row_points_at() -> None:
    store = MemoryBlobStore()
    set_blob_store(store)
    try:
        engine = _engine()
        with Session(engine) as session:
            session.add(
                BookRow(id="kept", status="complete", generation_id="g", about={}, pdf=b"kept")
            )
            session.add(
                BookRow(id="gone", status="complete", generation_id="g", about={}, pdf=b"gone")
            )
            session.commit()
        with Session(engine) as session:
            session.delete(session.get(BookRow, "gone"))
            session.commit()
        assert len(store.objects) == 2

        # Too young to sweep: it may be a write whose row is not committed yet.
        assert sweep_orphans(engine, store) == 0
        old = datetime.now(UTC) - timedelta(hours=2)
        store.written = dict.fromkeys(store.written, old)
        assert sweep_orphans(engine, store) == 1
        assert len(store.objects) == 1
        with Session(engine) as session:
            kept = session.get(BookRow, "kept")
            assert kept is not None and kept.pdf == b"kept"
    finally:
        set_blob_store(None)


def test_a_remembered_child_nobody_uses_is_forgotten_on_its_own() -> None:
    engine = _engine()
    with Session(engine) as session:
        session.add(UserRow(sub="u"))
        session.commit()
    profiles = DatabaseChildProfileStore(engine)
    profiles.save(ChildProfile(user_id="u", name="Maya", story_key=JACK_KEY, portrait=b"p"))
    assert profiles.expire(timedelta(days=1)) == 0
    with engine.begin() as conn:
        conn.execute(
            ChildProfileRow.__table__.update().values(
                updated_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
            )
        )
    profiles.touch("u")
    assert profiles.expire(timedelta(days=1)) == 0
    with engine.begin() as conn:
        conn.execute(
            ChildProfileRow.__table__.update().values(
                updated_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
            )
        )
    assert profiles.expire(timedelta(days=1)) == 1
    assert profiles.get("u") is None


def _print_order(state: str) -> PrintOrderRow:
    return PrintOrderRow(
        id=f"print_{state}",
        book_id="b",
        user_id="u",
        state=state,
        pdf_sha256="0" * 64,
        page_count=24,
        printing_cents=1614,
        shipping_cents=499,
        retail_cents=2999,
        interior=b"%PDF-interior",
        cover=b"%PDF-cover",
        address={"name": "U", "street1": "1 Road"},
        provider_order_id="42" if state == "submitted" else "",
    )


@pytest.mark.parametrize("state", ["placing", "unknown"])
def test_an_order_not_yet_settled_blocks_the_deletion(state: str) -> None:
    """A4: an order the printer may hold is resolved first, not torn down."""
    engine = _engine()
    with Session(engine) as session:
        session.add(UserRow(sub="u", email="u@example.com", name="U"))
        session.add(_print_order(state))
        session.commit()
    with pytest.raises(AccountBusy):
        delete_account(engine, "u")
    with Session(engine) as session:
        row = session.get(PrintOrderRow, f"print_{state}")
        assert row is not None and row.address is not None


def test_a_placed_order_is_kept_without_its_address_or_files() -> None:
    """A9: the sale's record stays; the person's address and pictures go."""
    engine = _engine()
    with Session(engine) as session:
        session.add(UserRow(sub="u", email="u@example.com", name="U"))
        session.add(_print_order("submitted"))
        session.add(_print_order("approved"))
        session.commit()
    delete_account(engine, "u")
    with Session(engine) as session:
        assert session.get(PrintOrderRow, "print_approved") is None
        kept = session.get(PrintOrderRow, "print_submitted")
        assert kept is not None
        assert (kept.address, kept.interior, kept.cover) == (None, None, None)
        assert (kept.provider_order_id, kept.retail_cents) == ("42", 2999)
