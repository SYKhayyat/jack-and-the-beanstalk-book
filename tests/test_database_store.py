"""Database-mode persistence: same contract, different durability promise.

These tests run the `DatabaseBookStore` and `DatabaseUserStore` against
SQLite, which needs none of Postgres's infrastructure and answers the
questions that matter for the contract: does the data survive a rebuild of
the process (a **new engine** over the same file), do pages upsert, does
ownership hold, and does an orphaned mid-render book reap rather than poll
forever. The multi-account acceptance test at the end runs the real routes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.config import Settings
from app.container import build_container
from app.db import (
    Base,
    BookPageRow,
    BookRow,
    build_engine,
    normalise_database_url,
    persistence_mode,
)
from app.main import create_app
from app.models.book import AboutMe
from app.services.book_store import BookStatus, FileBookStore
from app.services.db_book_store import DatabaseBookStore
from app.services.user_store import DatabaseUserStore, FileUserStore, User

ABOUT = AboutMe.model_validate({"prompt_name": "Jack", "about_name": "Jack"})
USER = "109876543210987654321"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Any]:
    """A SQLite engine over a file, so the tables can be rebuilt on it."""
    e = build_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture
def store(engine: Any) -> DatabaseBookStore:
    return DatabaseBookStore(engine)


def _seed(store: DatabaseBookStore, *, user_id: str | None = USER) -> str:
    store.save_record(book_id="book-1", user_id=user_id, about=ABOUT, generation_id="gen-1")
    return "book-1"


# --- the round trip -------------------------------------------------------------


def test_a_book_round_trips(store: DatabaseBookStore) -> None:
    book_id = _seed(store)
    store.save_status(
        book_id,
        BookStatus.RENDERING,
        [{"number": 1, "status": "done", "url": None}],
    )
    store.save_portrait(book_id, b"PNG-PORTRAIT")
    store.save_page(book_id, 1, b"PNG-PAGE-1")
    store.save_page(book_id, 2, b"PNG-PAGE-2")
    store.save_pdf(book_id, b"%PDF-fake")
    store.append_prompt(book_id, {"page": "1", "key": "cupboard", "prompt": "p1"})
    store.append_prompt(book_id, {"page": "2", "key": "beans", "prompt": "p2"})

    record = store.load_record(book_id)
    assert record is not None
    assert record.about == ABOUT
    assert record.generation_id == "gen-1"
    assert record.created_at != ""
    assert record.user_id == USER

    status = store.load_status(book_id)
    assert status is not None
    assert status["status"] == BookStatus.RENDERING
    assert status["pages"] == [{"number": 1, "status": "done", "url": None}]

    assert store.read_portrait(book_id) == b"PNG-PORTRAIT"
    assert store.read_page(book_id, 2) == b"PNG-PAGE-2"
    assert store.read_page(book_id, 3) is None
    assert store.read_pdf(book_id) == b"%PDF-fake"

    prompts = store.load_prompts(book_id)
    assert prompts is not None and len(prompts) == 2
    served = json.loads(store.read_prompts_json(book_id))
    assert served == {"prompts": prompts}

    assert store.owner_of(book_id) == USER


def test_a_new_book_reads_as_pending_immediately(store: DatabaseBookStore) -> None:
    """The record and the poll document are written in the same transaction.

    The runner writes its first status only after the POST returns, so a poll
    landing in between must find a pending document, not NULL — which the
    route would report as `unknown_book`.
    """
    book_id = _seed(store)

    status = store.load_status(book_id)
    assert status is not None
    assert status["status"] == BookStatus.PENDING
    assert status["pages"] == []
    assert status["pdf_url"] is None


def test_a_row_without_a_payload_still_reads(store: DatabaseBookStore, engine: Any) -> None:
    """A row written before this fix answers in the poll's shape, not with 404."""
    book_id = _seed(store)
    with engine.begin() as conn:
        conn.execute(update(BookRow).where(BookRow.id == book_id).values(status_payload=None))

    status = store.load_status(book_id)
    assert status is not None
    assert status["status"] == BookStatus.PENDING
    assert status["pages"] == []


def test_re_saving_a_page_replaces_it(store: DatabaseBookStore, engine: Any) -> None:
    book_id = _seed(store)
    store.save_page(book_id, 1, b"first")
    store.save_page(book_id, 1, b"better")

    assert store.read_page(book_id, 1) == b"better"
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(BookPageRow)).scalar_one() == 1


def test_a_page_number_of_zero_is_refused(store: DatabaseBookStore) -> None:
    book_id = _seed(store)
    with pytest.raises(ValueError):
        store.save_page(book_id, 0, b"nope")


def test_an_unowned_book_says_so(store: DatabaseBookStore) -> None:
    book_id = _seed(store, user_id=None)
    assert store.owner_of(book_id) is None


def test_a_broken_book_record_absent(store: DatabaseBookStore) -> None:
    assert store.load_record("nobody") is None
    assert store.load_status("nobody") is None
    assert store.owner_of("nobody") is None
    assert store.read_prompts_json("nobody") is None


# --- the restart promise ---------------------------------------------------------


def test_data_survives_rebuilding_the_process(tmp_path: Path) -> None:
    """One engine is the process; the file is the database.

    Build a second engine over the same file and check the data is still
    there — that is exactly what a Render restart/redeploy does in database
    mode, and the whole reason the store exists.
    """
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    first = build_engine(url)
    Base.metadata.create_all(first)
    store = DatabaseBookStore(first)
    _seed(store)
    store.save_pdf("book-1", b"%PDF-1.4")
    store.save_page("book-1", 1, b"PNG")
    first.dispose()

    second = build_engine(url)
    try:
        reloaded = DatabaseBookStore(second)
        assert reloaded.read_pdf("book-1") == b"%PDF-1.4"
        assert reloaded.read_page("book-1", 1) == b"PNG"
        assert reloaded.owner_of("book-1") == USER
    finally:
        second.dispose()


def test_status_and_owner_come_from_one_read(store: DatabaseBookStore) -> None:
    """#12: the poll's authorisation and body are one round trip, not three."""
    book_id = _seed(store)
    pages = [{"number": 1, "status": "done", "url": "/book-media/book-1/pages/1"}]
    store.save_status(book_id, BookStatus.RENDERING, pages)

    payload, owner = store.load_status_and_owner(book_id)
    assert owner == USER
    assert payload is not None
    assert payload["status"] == BookStatus.RENDERING
    assert payload["pages"] == pages

    missing, nobody = store.load_status_and_owner("not-a-book")
    assert missing is None
    assert nobody is None


def test_expire_deletes_only_finished_old_books(store: DatabaseBookStore, engine: Any) -> None:
    """#10: retention in database mode, where the size limit is real."""
    week = 7 * 24 * 3600
    for book_id, status in (
        ("old-complete", BookStatus.COMPLETE),
        ("old-failed", BookStatus.FAILED),
        ("old-rendering", BookStatus.RENDERING),
        ("young", BookStatus.COMPLETE),
    ):
        store.save_record(book_id=book_id, user_id=USER, about=ABOUT, generation_id="gen")
        store.save_status(book_id, status, [])
    store.save_page("old-complete", 1, b"PNG-PAGE")

    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
    with engine.begin() as conn:
        conn.execute(
            update(BookRow)
            .where(BookRow.id.in_(("old-complete", "old-failed", "old-rendering")))
            .values(created_at=old)
        )

    assert store.expire(older_than_seconds=week) == 2

    assert store.load_status("old-complete") is None
    assert store.read_page("old-complete", 1) is None
    assert store.load_status("old-failed") is None
    # A book still being made, and one inside the window, both survive.
    assert store.load_status("old-rendering") is not None
    assert store.load_status("young") is not None
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(BookPageRow)).scalar_one() == 0


def test_expire_with_a_zero_window_deletes_nothing(store: DatabaseBookStore) -> None:
    _seed(store)
    store.save_status("book-1", BookStatus.COMPLETE, [])
    assert store.expire(older_than_seconds=0) == 0
    assert store.load_status("book-1") is not None


def test_find_started_matches_an_identical_live_request(store: DatabaseBookStore) -> None:
    """The idempotency lookup: same fingerprint, inside the window, not failed."""
    store.save_record(
        book_id="book-1",
        user_id=USER,
        about=ABOUT,
        generation_id="gen-1",
        fingerprint="fp-1",
    )
    now = datetime.now(UTC)

    assert store.find_started(fingerprint="fp-1", since=now - timedelta(minutes=5)) == "book-1"
    # A different request is a different book.
    assert store.find_started(fingerprint="fp-2", since=now - timedelta(minutes=5)) is None
    # The window is a window: a request older than it is not reused.
    assert store.find_started(fingerprint="fp-1", since=now + timedelta(minutes=5)) is None


def test_find_started_skips_a_failed_book(store: DatabaseBookStore) -> None:
    """After a failure the visitor asking again is asking for a new attempt."""
    store.save_record(
        book_id="book-1",
        user_id=USER,
        about=ABOUT,
        generation_id="gen-1",
        fingerprint="fp-1",
    )
    store.save_status("book-1", BookStatus.FAILED, [], error="boom")

    assert (
        store.find_started(fingerprint="fp-1", since=datetime.now(UTC) - timedelta(minutes=5))
        is None
    )


def test_find_started_ignores_a_record_without_a_fingerprint(
    store: DatabaseBookStore,
) -> None:
    """Rows written before this column existed can never match a request."""
    _seed(store)

    assert (
        store.find_started(fingerprint="fp-1", since=datetime.now(UTC) - timedelta(minutes=5))
        is None
    )


def test_a_mid_render_book_is_reaped_on_restart(store: DatabaseBookStore) -> None:
    book_id = _seed(store)
    store.save_page(book_id, 1, b"kept")
    pages = [{"number": 1, "status": "done", "url": "/book-media/book-1/pages/1"}]
    store.save_status(book_id, BookStatus.RENDERING, pages)
    store.save_record(book_id="book-2", user_id=None, about=ABOUT, generation_id="gen-2")
    store.save_status("book-2", BookStatus.COMPLETE, [])

    assert store.reap_incomplete() == 1
    status = store.load_status(book_id)
    assert status is not None
    assert status["status"] == BookStatus.FAILED
    assert "restarted" in str(status["error"])
    assert status["pages"] == pages  # the finished page stays visible
    assert store.read_page(book_id, 1) == b"kept"

    # The second reap has nothing left to do.
    assert store.reap_incomplete() == 0
    # A complete book is never touched.
    complete = store.load_status("book-2")
    assert complete is not None
    assert complete["status"] == BookStatus.COMPLETE


def test_a_book_orphaned_while_pending_is_reaped_on_restart(
    store: DatabaseBookStore,
) -> None:
    """`save_record` commits PENDING before the runner writes any status.

    A crash in that window used to leave a book that never rendered and never
    stopped polling as pending; the reaper now treats PENDING as orphaned too.
    """
    book_id = _seed(store)

    assert store.reap_incomplete() == 1
    status = store.load_status(book_id)
    assert status is not None
    assert status["status"] == BookStatus.FAILED
    assert "restarted" in str(status["error"])
    assert status["pages"] == []
    # Reaping is idempotent.
    assert store.reap_incomplete() == 0


def test_delete_removes_everything(store: DatabaseBookStore, engine: Any) -> None:
    book_id = _seed(store)
    store.save_page(book_id, 1, b"PNG")
    store.delete(book_id)
    assert store.load_status(book_id) is None
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(BookPageRow)).scalar_one() == 0


# --- the user store --------------------------------------------------------------


def test_a_person_round_trips(engine: Any) -> None:
    users = DatabaseUserStore(engine)
    first = users.upsert(
        User(sub=USER, email="someone@example.com", name="Someone", picture="p")
    )
    second = users.upsert(User(sub=USER, email="someone@example.com", name="New Name"))

    assert first.created_at == second.created_at  # the first sign-in stamps it
    assert second.name == "New Name"
    found = users.get(USER)
    assert found is not None and found.sub == USER
    assert users.get("nobody") is None


# --- the wiring ------------------------------------------------------------------


def test_the_container_builds_database_stores_when_a_url_is_set(
    tmp_path: Path,
) -> None:
    settings = Settings(
        image_provider="mock",  # type: ignore[arg-type]
        gemini_api_key="",
        app_env="test",
        storage_dir=tmp_path / "storage",
        database_url=f"sqlite:///{tmp_path / 'db.sqlite'}",
    )
    container = build_container(settings)
    assert isinstance(container.book_store, DatabaseBookStore)
    assert isinstance(container.user_store, DatabaseUserStore)


def test_the_container_stays_on_the_filesystem_without_a_url(
    tmp_path: Path,
) -> None:
    settings = Settings(
        image_provider="mock",  # type: ignore[arg-type]
        gemini_api_key="",
        app_env="test",
        storage_dir=tmp_path / "storage",
    )
    container = build_container(settings)
    assert isinstance(container.book_store, FileBookStore)
    assert isinstance(container.user_store, FileUserStore)


def test_an_engine_needs_a_url() -> None:
    with pytest.raises(ValueError):
        build_engine("")


def test_the_log_word_for_the_database_is_the_dialect(engine: Any) -> None:
    assert persistence_mode(engine) == "sqlite"


def test_a_pasted_postgres_url_names_the_installed_driver() -> None:
    """Bare `postgresql://` asks for psycopg2; the app ships psycopg3.

    Operators paste `postgres://` or `postgresql://` straight from Neon's
    dashboard; both are rewritten to the dialect of the driver that is
    actually installed, so the engine builds instead of dying on a missing
    psycopg2 import. (Building the engine connects to nothing.)
    """
    assert "postgresql+psycopg://" in normalise_database_url(
        "postgresql://user:pw@host.example/db"
    )
    assert "postgresql+psycopg://" in normalise_database_url(
        "postgres://user:pw@host.example/db"
    )
    # An explicitly pinned driver is left alone.
    assert (
        normalise_database_url("postgresql+psycopg2://user:pw@host.example/db")
        == "postgresql+psycopg2://user:pw@host.example/db"
    )


def test_both_stores_read_a_new_book_as_pending(tmp_path: Path, engine: Any) -> None:
    """The two stores answer the same document from the moment the book exists."""
    file_store = FileBookStore(tmp_path / "books")
    db_store = DatabaseBookStore(engine)
    for store in (file_store, db_store):
        store.save_record(book_id="book-1", user_id=USER, about=ABOUT, generation_id="gen-1")
        status = store.load_status("book-1")
        assert status is not None, store.__class__.__name__
        assert status["status"] == BookStatus.PENDING, store.__class__.__name__
        assert status["pages"] == [], store.__class__.__name__
        assert status["pdf_url"] is None, store.__class__.__name__


def test_file_and_database_stores_serve_the_same_status_shapes(
    tmp_path: Path, engine: Any
) -> None:
    """The contract: whichever store runs, the poll sees the same document."""
    file_store = FileBookStore(tmp_path / "books")
    db_store = DatabaseBookStore(engine)
    for s in (file_store, db_store):
        s.save_record(book_id="book-1", user_id=USER, about=ABOUT, generation_id="gen-1")
        s.save_status(
            "book-1", BookStatus.COMPLETE, [{"number": 1, "status": "done", "url": "u"}]
        )
        s.save_prompts("book-1", [{"page": "1", "key": "k", "prompt": "p"}])
        file_status = s.load_status("book-1")
        keys = set((file_status or {}).keys())
        assert keys == {"book_id", "status", "pages", "error", "pdf_url", "updated_at"}, (
            s.__class__.__name__
        )


# --- the acceptance flow, through the real routes --------------------------------


class _Google:
    """A Google that signs whoever the test says, so two accounts can run."""

    def __init__(self, sub: str) -> None:
        self._sub = sub

    def authorization_url(self, *, state: str, redirect_uri: str, prompt: str = "") -> str:
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    def exchange_code(self, code: str, *, redirect_uri: str) -> Any:
        from app.services.google_oauth import GoogleProfile

        return GoogleProfile(
            sub=self._sub,
            email=f"{self._sub}@example.com",
            name=self._sub,
            picture="",
            email_verified=True,
        )


def _db_google_settings(tmp_path: Path) -> Settings:
    return Settings(
        image_provider="mock",  # type: ignore[arg-type]
        gemini_api_key="",
        app_env="test",
        storage_dir=tmp_path / "storage",
        google_client_id="cid",
        google_client_secret="cs",
        google_redirect_uri="http://testserver/auth/callback",
        session_secret="session-secret",
        database_url=f"sqlite:///{tmp_path / 'acceptance.sqlite'}",
    )


def _app_with(tmp_path: Path, sub: str) -> TestClient:
    """A fresh process: new container, same database file."""
    settings = _db_google_settings(tmp_path)
    creator = build_engine(settings.database_url)
    Base.metadata.create_all(creator)
    creator.dispose()
    app = create_app(settings)
    app.state.oauth = _Google(sub)
    client = TestClient(app)
    # Closing the client does not close the container's engines; do it via a
    # teardown hook so no test finishes with an open SQLite file handle.
    orig_close = client.close

    def close() -> None:
        _dispose(app.state.container)
        orig_close()

    client.close = close
    return client


def _sign_in(client: TestClient) -> None:
    client.get("/auth/google/login?next=/", follow_redirects=False)
    state = client.app.state.sessions.read_state(  # type: ignore[attr-defined]
        client.cookies.get("jb_oauth_state")
    )
    assert state is not None
    response = client.get(f"/auth/callback?code=c&state={state[0]}", follow_redirects=False)
    assert response.status_code == 303


def _dispose(container: object) -> None:
    """Close the SQLite connections a test container opened (test hygiene)."""
    for name in ("book_store", "user_store"):
        store = getattr(container, name, None)
        engine = getattr(store, "_engine", None)
        if engine is not None:
            engine.dispose()


def _seed_book(settings: Settings, user_id: str) -> str:
    creator = build_engine(settings.database_url)
    Base.metadata.create_all(creator)
    creator.dispose()
    container = build_container(settings)
    store = container.book_store
    assert isinstance(store, DatabaseBookStore)
    store.save_record(
        book_id="owned-book", user_id=user_id, about=ABOUT, generation_id="gen-1"
    )
    store.save_pdf("owned-book", b"%PDF-owned")
    store.save_status("owned-book", BookStatus.COMPLETE, [])
    _dispose(container)
    return "owned-book"


def test_another_account_cannot_fetch_somebodys_book(tmp_path: Path) -> None:
    settings = _db_google_settings(tmp_path)
    book_id = _seed_book(settings, USER)

    owner = _app_with(tmp_path, sub=USER)
    _sign_in(owner)
    assert owner.get(f"/api/book/{book_id}").status_code == 200

    stranger = _app_with(tmp_path, sub="other-person")
    _sign_in(stranger)
    response = stranger.get(f"/api/book/{book_id}")
    pdf_fetch = stranger.get(f"/book-media/{book_id}/pdf")
    owner.close()
    stranger.close()
    assert response.status_code == 404
    # Same sentence as a book that does not exist at all: the id leaks nothing.
    assert response.json()["error"] == "unknown_book"
    assert pdf_fetch.status_code == 404


def test_the_same_person_sees_their_book_after_a_restart(tmp_path: Path) -> None:
    """The step's acceptance criterion, minus the real deployment."""
    settings = _db_google_settings(tmp_path)
    book_id = _seed_book(settings, USER)

    restarted = _app_with(tmp_path, sub=USER)
    _sign_in(restarted)
    status = restarted.get(f"/api/book/{book_id}")
    pdf = restarted.get(f"/book-media/{book_id}/pdf")
    restarted.close()
    assert status.status_code == 200
    assert status.json()["pdf_url"].endswith(f"/book-media/{book_id}/pdf")
    assert pdf.status_code == 200
    assert pdf.content == b"%PDF-owned"
