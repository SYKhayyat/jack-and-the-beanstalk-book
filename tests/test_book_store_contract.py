"""One contract, two implementations, the same assertions.

The filesystem and database book stores are separate classes with the same
promise. Each has its own tests, but nothing was forcing them to answer the
same way — a change to one could quietly drift from the other until a
deployment chose the wrong mode. This suite runs the contract itself against
both, so a divergence fails here instead of in Postgres.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine

from app.db import Base, build_engine
from app.models import CartoonSettings, LikenessStrength
from app.models.book import AboutMe
from app.services.book_store import (
    BookRecord,
    BookStatus,
    BookStore,
    CharacterReference,
    FileBookStore,
)
from app.services.db_book_store import DatabaseBookStore
from app.services.user_store import DatabaseUserStore, User

USER = "contract-user-1"
ABOUT = AboutMe.model_validate({"prompt_name": "Jack", "about_name": "Jack", "about_age": 8})

#: Real image signatures, so the filesystem store's sniffing and the database
#: store's stored types agree on the same answer.
PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff\xe0"


@pytest.fixture(params=["filesystem", "database"])
def store(request: pytest.FixtureRequest, tmp_path) -> Iterator[BookStore]:
    if request.param == "filesystem":
        yield FileBookStore(tmp_path / "books")
        return
    engine: Engine = build_engine(f"sqlite:///{tmp_path / 'contract.sqlite'}")
    Base.metadata.create_all(engine)
    DatabaseUserStore(engine).upsert(
        User(sub=USER, email="contract@example.com", name="Contract")
    )
    try:
        yield DatabaseBookStore(engine)
    finally:
        engine.dispose()


def seed(store: BookStore, *, book_id: str = "book-1", user_id: str | None = USER) -> str:
    store.save_record(
        book_id=book_id,
        user_id=user_id,
        about=ABOUT,
        generation_id="gen-1",
        fingerprint="fingerprint-1",
        settings=CartoonSettings(likeness_strength=LikenessStrength.LOOSE),
    )
    return book_id


def test_a_new_book_polls_as_pending_with_no_pages(store: BookStore) -> None:
    book_id = seed(store)
    status = store.load_status(book_id)
    assert status is not None
    assert status["status"] == BookStatus.PENDING
    assert status["pages"] == []
    assert status["pdf_url"] is None


def test_a_record_round_trips_with_its_settings_and_owner(store: BookStore) -> None:
    book_id = seed(store)
    record = store.load_record(book_id)
    assert record is not None
    assert record.about == ABOUT
    assert record.generation_id == "gen-1"
    assert record.user_id == USER
    assert record.fingerprint == "fingerprint-1"
    assert record.settings.likeness_strength == LikenessStrength.LOOSE
    assert record.created_at != ""
    assert store.owner_of(book_id) == USER


def test_an_unowned_book_has_no_owner(store: BookStore) -> None:
    book_id = seed(store, user_id=None)
    assert store.owner_of(book_id) is None
    payload, owner = store.load_status_and_owner(book_id)
    assert payload is not None
    assert owner is None


def test_status_round_trips_with_its_extra_fields(store: BookStore) -> None:
    book_id = seed(store)
    pages: list[dict[str, object]] = [
        {"number": 0, "key": "cover", "status": "done", "url": "/cover"},
        {"number": 1, "key": "cupboard", "status": "pending", "url": None},
    ]
    store.save_status(
        book_id,
        BookStatus.RENDERING,
        pages,
        title="Jack's Book",
        extra={"phase": "pages", "total_pages": 20},
    )
    status = store.load_status(book_id)
    assert status is not None
    assert status["status"] == BookStatus.RENDERING
    assert status["pages"] == pages
    assert status["title"] == "Jack's Book"
    assert status["phase"] == "pages"
    assert status["total_pages"] == 20

    payload, owner = store.load_status_and_owner(book_id)
    assert payload == status
    assert owner == USER


def test_pages_round_trip_and_a_resave_replaces(store: BookStore) -> None:
    book_id = seed(store)
    store.save_page(book_id, 1, b"first")
    store.save_page(book_id, 1, b"better")
    store.save_page(book_id, 2, b"second")

    assert store.read_page(book_id, 1) == b"better"
    assert store.read_page(book_id, 2) == b"second"
    assert store.read_page(book_id, 3) is None


def test_a_page_number_of_zero_is_refused(store: BookStore) -> None:
    book_id = seed(store)
    with pytest.raises(ValueError):
        store.save_page(book_id, 0, b"nope")


def test_the_identity_artefacts_round_trip(store: BookStore) -> None:
    book_id = seed(store)
    store.save_portrait(book_id, b"portrait")
    store.save_source_photo(book_id, b"photo")
    store.save_cover(book_id, b"cover")

    assert store.read_portrait(book_id) == b"portrait"
    assert store.read_source_photo(book_id) == b"photo"
    assert store.read_cover(book_id) == b"cover"


def test_a_face_edit_replaces_portrait_photo_and_settings(store: BookStore) -> None:
    book_id = seed(store)
    store.update_identity(
        book_id,
        portrait=b"new portrait",
        photo=b"new photo",
        settings=CartoonSettings(likeness_strength=LikenessStrength.BALANCED),
    )
    record = store.load_record(book_id)
    assert record is not None
    assert store.read_portrait(book_id) == b"new portrait"
    assert store.read_source_photo(book_id) == b"new photo"
    assert record.settings.likeness_strength == LikenessStrength.BALANCED


def test_characters_round_trip_and_one_can_be_replaced(store: BookStore) -> None:
    book_id = seed(store)
    mother = CharacterReference(
        portrait=PNG + b"mother portrait",
        portrait_media_type="image/png",
        photo=JPEG + b"mother photo",
        photo_media_type="image/jpeg",
    )
    giant = CharacterReference(
        portrait=PNG + b"giant portrait",
        portrait_media_type="image/png",
        photo=JPEG + b"giant photo",
        photo_media_type="image/jpeg",
    )
    store.save_characters(book_id, {"mother": mother})
    store.save_character(book_id, "giant", giant)

    characters = store.load_characters(book_id)
    assert characters == {"mother": mother, "giant": giant}

    replacement = CharacterReference(
        portrait=PNG + b"new mother",
        portrait_media_type="image/png",
        photo=JPEG + b"new mother photo",
        photo_media_type="image/jpeg",
    )
    store.save_character(book_id, "mother", replacement)
    assert store.load_characters(book_id)["mother"] == replacement


def test_prompts_round_trip_and_are_served_wrapped(store: BookStore) -> None:
    book_id = seed(store)
    store.save_prompts(book_id, [{"page": "1", "key": "cupboard", "prompt": "p1"}])
    store.append_prompt(book_id, {"page": "2", "key": "beans", "prompt": "p2"})

    prompts = store.load_prompts(book_id)
    assert prompts == [
        {"page": "1", "key": "cupboard", "prompt": "p1"},
        {"page": "2", "key": "beans", "prompt": "p2"},
    ]
    assert json.loads(store.read_prompts_json(book_id)) == {"prompts": prompts}


def test_the_pdf_round_trips(store: BookStore) -> None:
    book_id = seed(store)
    store.save_pdf(book_id, b"%PDF-fake")
    assert store.read_pdf(book_id) == b"%PDF-fake"


def test_delete_removes_every_artefact(store: BookStore) -> None:
    book_id = seed(store)
    store.save_page(book_id, 1, b"page")
    store.save_pdf(book_id, b"%PDF")
    store.delete(book_id)

    assert store.load_record(book_id) is None
    assert store.load_status(book_id) is None
    assert store.owner_of(book_id) is None
    assert store.read_page(book_id, 1) is None
    assert store.read_pdf(book_id) is None


def test_list_for_owner_is_scoped_to_that_person(store: BookStore) -> None:
    seed(store, book_id="mine-1")
    seed(store, book_id="mine-2")
    seed(store, book_id="theirs", user_id="contract-user-2")

    summaries = store.list_for_owner(USER)
    assert {summary.book_id for summary in summaries} == {"mine-1", "mine-2"}
    assert all(summary.title for summary in summaries)
    assert store.list_for_owner("") == []


def test_find_started_answers_with_a_live_book_and_skips_a_failed_one(
    store: BookStore,
) -> None:
    book_id = seed(store)
    since = datetime.now(UTC) - timedelta(minutes=5)
    assert store.find_started(fingerprint="fingerprint-1", since=since) == book_id

    store.save_status(book_id, BookStatus.FAILED, [], error="stopped")
    assert store.find_started(fingerprint="fingerprint-1", since=since) is None
    assert store.find_started(fingerprint="another", since=since) is None


def test_a_restart_reaps_a_book_that_never_finished(store: BookStore) -> None:
    book_id = seed(store)
    assert store.reap_incomplete() >= 1
    status = store.load_status(book_id)
    assert status is not None
    assert status["status"] == BookStatus.FAILED


def test_urls_are_identical_in_both_modes(store: BookStore) -> None:
    book_id = seed(store)
    assert store.pdf_url(book_id) == f"/book-media/{book_id}/pdf"
    assert store.portrait_url(book_id) == f"/book-media/{book_id}/portrait"
    assert store.cover_url(book_id) == f"/book-media/{book_id}/cover"
    assert store.page_url(book_id, 3) == f"/book-media/{book_id}/pages/3"


def test_the_record_dataclass_stays_the_contract_shape(store: BookStore) -> None:
    """A cheap guard: where a record comes back, it comes back as a record."""
    record = store.load_record(seed(store))
    assert isinstance(record, BookRecord)
