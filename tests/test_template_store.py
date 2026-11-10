"""T4: stories as data, template versions, and the version every book pins.

Each test names the acceptance criterion it holds (`docs/book-template-plan.md`
T4). They run on SQLite with foreign keys enforced, and on Postgres too when
`TEST_POSTGRES_URL` points at a throwaway database.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.books import SEEDS, SeedError, StorySeed, load_seed, seed_paths
from app.db import (
    Base,
    BookRow,
    StoryCharacterRow,
    StoryPageRow,
    build_engine,
    enforce_sqlite_foreign_keys,
)
from app.models.book import AboutMe
from app.services.book_store import BookStatus
from app.services.db_book_store import (
    DatabaseBookStore,
    UnreviewedImages,
    unreviewed_images,
)
from app.services.template_store import (
    COVER_PAGE,
    PAGE_NUMBERS,
    NoTemplateVersion,
    PlateNotApprovable,
    PlateStatus,
    Slot,
    TemplateError,
    TemplateInUse,
    TemplateStore,
)
from app.services.user_store import DatabaseUserStore, User

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")
RED = next(seed for seed in SEEDS if seed.key == "little_red_riding_hood")
OPERATOR = "operator-sub"
VERDICT = [{"attempt": 1, "tier": 2, "passed": True, "faults": []}]


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres",
            marks=pytest.mark.skipif(
                not POSTGRES_URL, reason="set TEST_POSTGRES_URL to run against Postgres"
            ),
        ),
    ]
)
def engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Engine]:
    if request.param == "sqlite":
        built = enforce_sqlite_foreign_keys(build_engine(f"sqlite:///{tmp_path / 't.sqlite'}"))
    else:
        built = build_engine(POSTGRES_URL)
        with built.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        Base.metadata.drop_all(built)
    Base.metadata.create_all(built)
    try:
        yield built
    finally:
        if request.param == "postgres":
            Base.metadata.drop_all(built)
        built.dispose()


@pytest.fixture
def templates(engine: Engine) -> TemplateStore:
    store = TemplateStore(engine)
    store.seed([RED])
    return store


@pytest.fixture
def books(engine: Engine) -> DatabaseBookStore:
    DatabaseUserStore(engine).upsert(User(sub="reader", email="r@example.com", name="R"))
    return DatabaseBookStore(engine)


# --- helpers --------------------------------------------------------------------


def cast_on(seed: StorySeed, page_number: int) -> list[str]:
    listed = (
        seed.cover.cast
        if page_number == COVER_PAGE
        else seed.pages[page_number - 1].scene.cast
    )
    return list(dict.fromkeys([seed.star, *listed]))


def slots_for(cast: list[str]) -> list[Slot]:
    width = 1 / max(len(cast), 1)
    return [
        Slot(cast_key=key, rect=(index * width, 0.4, width, 0.5), scale=0.5, facing="toward")
        for index, key in enumerate(cast)
    ]


def add_plate(
    store: TemplateStore,
    page_number: int,
    *,
    seed: StorySeed = RED,
    slots: list[Slot] | None = None,
) -> str:
    return store.add_plate(
        seed.key,
        page_number,
        image=f"plate {page_number}".encode(),
        media_type="image/png",
        prompt=f"empty scene {page_number}, no people, no text",
        slots=slots if slots is not None else slots_for(cast_on(seed, page_number)),
    )


def approve_all(store: TemplateStore, seed: StorySeed = RED) -> dict[int, str]:
    plates = {number: add_plate(store, number, seed=seed) for number in PAGE_NUMBERS}
    for plate_id in plates.values():
        store.approve_plate(plate_id, operator=OPERATOR)
    return plates


def new_book(engine: Engine, books: DatabaseBookStore, book_id: str = "book-1") -> str:
    """Start a Red book the way the start route does: pinned to what it starts on."""
    books.save_record(
        edition=TemplateStore(engine).edition_for_new_book(RED.key),
        book_id=book_id,
        user_id="reader",
        about=AboutMe.model_validate({"prompt_name": "Ada", "about_name": "Ada"}),
        generation_id=f"gen-{book_id}",
    )
    return book_id


def pinned(engine: Engine, book_id: str) -> str:
    """The template version a saved book records."""
    with engine.connect() as conn:
        value = conn.execute(
            select(BookRow.template_version_id).where(BookRow.id == book_id)
        ).scalar_one()
    assert value is not None, "the book was not pinned to a version"
    return str(value)


def count(engine: Engine, table: Any) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


def with_page_prose(seed: StorySeed, number: int, prose: str) -> StorySeed:
    pages = list(seed.pages)
    pages[number - 1] = dataclasses.replace(pages[number - 1], story=prose)
    return dataclasses.replace(seed, pages=tuple(pages))


# --- T4.1: a book keeps the version it started with --------------------------------


class TestPinning:
    def test_no_version_exists_until_every_page_has_an_approved_plate(
        self, templates: TemplateStore
    ) -> None:
        plates = {number: add_plate(templates, number) for number in PAGE_NUMBERS}
        for number in PAGE_NUMBERS[:-1]:
            assert templates.approve_plate(plates[number], operator=OPERATOR) is None
        version_id = templates.approve_plate(plates[PAGE_NUMBERS[-1]], operator=OPERATOR)

        version = templates.load_version(version_id or "")
        assert version.number == 1
        assert [page.page_number for page in version.pages] == list(PAGE_NUMBERS)
        assert set(version.characters) == set(RED.cast)

    def test_with_the_gate_on_a_book_cannot_start_before_a_version_exists(
        self, templates: TemplateStore
    ) -> None:
        templates.set_requires_template(RED.key, True)
        with pytest.raises(NoTemplateVersion, match="can't be started yet"):
            templates.edition_for_new_book(RED.key)

    def test_with_the_gate_off_a_book_starts_on_the_latest_words_without_a_version(
        self, engine: Engine, templates: TemplateStore, books: DatabaseBookStore
    ) -> None:
        book_id = new_book(engine, books)
        with engine.connect() as conn:
            version = conn.execute(
                select(BookRow.template_version_id).where(BookRow.id == book_id)
            ).scalar_one()
        assert version is None
        edition = templates.book_edition(book_id)
        assert edition.template_version_id is None
        assert [page.story for page in edition.pages] == [page.story for page in RED.pages]

    def test_a_newer_plate_makes_a_new_version_and_the_book_keeps_the_old(
        self, engine: Engine, templates: TemplateStore, books: DatabaseBookStore
    ) -> None:
        plates = approve_all(templates)
        book_id = new_book(engine, books)
        first = pinned(engine, book_id)
        before = templates.load_version(first)

        replacement = add_plate(templates, 5)
        second = templates.approve_plate(replacement, operator=OPERATOR)

        assert second != first
        assert templates.load_version(second or "").page(5).plate_id == replacement
        assert pinned(engine, book_id) == first
        assert templates.load_version(first) == before
        assert templates.load_version(first).page(5).plate_id == plates[5]
        assert templates.plate_image(plates[5])[0] == b"plate 5"

    def test_editing_a_pinned_look_makes_a_new_version_and_old_books_keep_the_old_look(
        self, engine: Engine, templates: TemplateStore, books: DatabaseBookStore
    ) -> None:
        approve_all(templates)
        first = pinned(engine, new_book(engine, books))

        second = templates.revise_character(RED.key, "wolf", look="A friendly grey wolf.")

        assert second is not None and second != first
        assert (
            templates.load_version(second).characters["wolf"].look == "A friendly grey wolf."
        )
        assert templates.load_version(first).characters["wolf"].look == RED.cast["wolf"].look

    def test_a_changed_seed_makes_a_new_version_and_old_books_keep_the_old_words(
        self, engine: Engine, templates: TemplateStore, books: DatabaseBookStore
    ) -> None:
        approve_all(templates)
        first = pinned(engine, new_book(engine, books))
        new_prose = RED.pages[2].story + " The path was quiet."

        report = templates.seed([with_page_prose(RED, 3, new_prose)])

        assert report.page_revisions == 1
        assert report.versions and report.versions[0] != first
        assert templates.load_version(report.versions[0]).page(3).prose == new_prose
        assert templates.load_version(first).page(3).prose == RED.pages[2].story

    def test_a_book_is_pinned_once(
        self, engine: Engine, templates: TemplateStore, books: DatabaseBookStore
    ) -> None:
        approve_all(templates)
        book_id = new_book(engine, books)
        first = pinned(engine, book_id)
        words = templates.book_edition(book_id)
        templates.approve_plate(add_plate(templates, 1), operator=OPERATOR)
        templates.seed([with_page_prose(RED, 3, RED.pages[2].story + " The path was quiet.")])
        assert pinned(engine, book_id) == first
        assert templates.book_edition(book_id) == words
        # One version for the new plate, one for the changed words.
        assert templates.current_version(RED.key).number == 3  # type: ignore[union-attr]


# --- T4.2: in use means retire, never delete ------------------------------------------


class TestDeletion:
    def test_a_version_a_book_pins_cannot_be_deleted(
        self, engine: Engine, templates: TemplateStore, books: DatabaseBookStore
    ) -> None:
        approve_all(templates)
        version_id = pinned(engine, new_book(engine, books))

        with pytest.raises(TemplateInUse, match="1 book"):
            templates.delete_version(version_id)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text("DELETE FROM template_versions WHERE id = :id"), {"id": version_id}
            )

    def test_a_plate_in_any_version_cannot_be_deleted_but_can_be_retired(
        self, engine: Engine, templates: TemplateStore
    ) -> None:
        plates = approve_all(templates)
        version_id = templates.current_version(RED.key).id  # type: ignore[union-attr]
        before = templates.load_version(version_id)

        with pytest.raises(TemplateInUse, match="retire"):
            templates.delete_plate(plates[4])
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("DELETE FROM template_plates WHERE id = :id"), {"id": plates[4]})

        templates.retire_plate(plates[4])
        assert templates.plate_status(plates[4]) == PlateStatus.RETIRED
        assert templates.load_version(version_id) == before

    def test_an_unused_draft_and_an_unpinned_version_can_be_deleted(
        self, engine: Engine, templates: TemplateStore, books: DatabaseBookStore
    ) -> None:
        approve_all(templates)
        first = pinned(engine, new_book(engine, books))
        second = templates.approve_plate(add_plate(templates, 2), operator=OPERATOR)
        draft = add_plate(templates, 7)

        templates.delete_plate(draft)
        templates.delete_version(second or "")

        assert templates.current_version(RED.key).id == first  # type: ignore[union-attr]
        with pytest.raises(TemplateError):
            templates.plate_status(draft)


# --- T4.3: the bible is the only source of a look -----------------------------------


class TestOneSourceOfLooks:
    def test_story_pages_has_nowhere_to_restate_a_look(self, engine: Engine) -> None:
        columns = {column["name"] for column in inspect(engine).get_columns("story_pages")}
        assert not {"look", "looks", "description", "appearance"} & columns
        assert "look" in {c["name"] for c in inspect(engine).get_columns("story_characters")}

    def test_a_version_page_carries_cast_keys_and_the_bible_carries_looks(
        self, templates: TemplateStore
    ) -> None:
        approve_all(templates)
        version = templates.current_version(RED.key)
        assert version is not None
        page = version.page(10)
        assert not hasattr(page, "look")
        assert set(page.cast_keys) <= set(version.characters)
        for character in version.characters.values():
            assert character.look not in str(page.scene)

    def test_a_seed_page_that_restates_a_look_is_refused_at_boot(self) -> None:
        import tomllib

        path = next(p for p in seed_paths() if p.stem == RED.key)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        wolf_sentence = data["cast"]["wolf"]["look"].split(". ")[1]
        data["pages"][3]["environment"] += ". " + wolf_sentence

        from app.books import seed_from_mapping

        with pytest.raises(SeedError, match="restates 'wolf'"):
            seed_from_mapping(data, path.name)
        assert load_seed(path) == RED


# --- T4.4: seeding is idempotent and never rewrites what was pinned -----------------


class TestSeeding:
    def test_seeding_twice_writes_nothing_the_second_time(
        self, engine: Engine, templates: TemplateStore
    ) -> None:
        pages, characters = count(engine, StoryPageRow), count(engine, StoryCharacterRow)
        assert pages == len(PAGE_NUMBERS)
        assert characters == len(RED.cast)

        report = templates.seed([RED])

        assert not report.changed
        assert (count(engine, StoryPageRow), count(engine, StoryCharacterRow)) == (
            pages,
            characters,
        )

    def test_every_seeded_row_is_stamped_with_its_seed_hash(
        self, engine: Engine, templates: TemplateStore
    ) -> None:
        with engine.connect() as conn:
            hashes = conn.execute(select(StoryPageRow.seed_hash)).scalars().all()
            hashes += conn.execute(select(StoryCharacterRow.seed_hash)).scalars().all()
        assert hashes and all(value and len(value) == 64 for value in hashes)

    def test_a_changed_seed_adds_a_revision_and_leaves_the_old_row_alone(
        self, engine: Engine, templates: TemplateStore
    ) -> None:
        new_prose = RED.pages[0].story + " The sun was warm."
        templates.seed([with_page_prose(RED, 1, new_prose)])
        with engine.connect() as conn:
            rows = conn.execute(
                select(StoryPageRow.revision, StoryPageRow.prose)
                .where(StoryPageRow.page_number == 1)
                .order_by(StoryPageRow.revision)
            ).all()
        assert [tuple(row) for row in rows] == [(1, RED.pages[0].story), (2, new_prose)]

    def test_an_operator_look_survives_a_reseed_of_an_unchanged_file(
        self, engine: Engine, templates: TemplateStore
    ) -> None:
        approve_all(templates)
        edited = templates.revise_character(RED.key, "mother", look="Mother in a blue shawl.")

        report = templates.seed([RED])

        assert not report.changed
        version = templates.current_version(RED.key)
        assert version is not None and version.id == edited
        assert version.characters["mother"].look == "Mother in a blue shawl."

    def test_a_non_photoable_character_cannot_be_given_a_reference_image(
        self, templates: TemplateStore
    ) -> None:
        with pytest.raises(TemplateError, match="cannot have a reference image"):
            templates.revise_character(RED.key, "wolf", reference_image=b"png")


# --- T4.5: no pinned book completes with an unreviewed image ------------------------


class TestReviews:
    def make_pinned_book(
        self, engine: Engine, templates: TemplateStore, books: DatabaseBookStore
    ) -> str:
        approve_all(templates)
        book_id = new_book(engine, books)
        pinned(engine, book_id)
        books.save_cover(book_id, b"cover art")
        for number in (1, 2):
            books.save_page(book_id, number, f"page {number}".encode())
        return book_id

    def review_everything(self, books: DatabaseBookStore, book_id: str) -> None:
        books.record_cover_review(book_id, VERDICT)
        for number in (1, 2):
            books.record_page_review(
                book_id, number, review=VERDICT, attempt_count=1, chosen_attempt=1
            )

    def test_a_pinned_book_cannot_complete_until_every_image_is_reviewed(
        self, engine: Engine, templates: TemplateStore, books: DatabaseBookStore
    ) -> None:
        book_id = self.make_pinned_book(engine, templates, books)

        with pytest.raises(UnreviewedImages, match=r"\[0, 1, 2\]"):
            books.save_status(book_id, BookStatus.COMPLETE, [])

        self.review_everything(books, book_id)
        books.save_status(book_id, BookStatus.COMPLETE, [])
        assert unreviewed_images(engine) == []

    def test_redrawing_a_page_or_cover_clears_its_review(
        self, engine: Engine, templates: TemplateStore, books: DatabaseBookStore
    ) -> None:
        book_id = self.make_pinned_book(engine, templates, books)
        self.review_everything(books, book_id)

        books.save_page(book_id, 2, b"redrawn")
        books.save_cover(book_id, b"new cover")

        with pytest.raises(UnreviewedImages, match=r"\[0, 2\]"):
            books.save_status(book_id, BookStatus.COMPLETE, [])

    def test_the_proof_query_names_any_unreviewed_image_in_a_completed_book(
        self, engine: Engine, templates: TemplateStore, books: DatabaseBookStore
    ) -> None:
        book_id = self.make_pinned_book(engine, templates, books)
        books.record_page_review(book_id, 1, review=VERDICT, attempt_count=2, chosen_attempt=2)
        with engine.begin() as conn:  # a write that bypassed the store
            conn.execute(
                update(BookRow).where(BookRow.id == book_id).values(status=BookStatus.COMPLETE)
            )
        assert unreviewed_images(engine) == [(book_id, 0), (book_id, 2)]

    def test_a_page_records_its_attempts_and_hash(
        self, engine: Engine, templates: TemplateStore, books: DatabaseBookStore
    ) -> None:
        book_id = self.make_pinned_book(engine, templates, books)
        books.record_page_review(
            book_id, 1, review=VERDICT * 3, attempt_count=3, chosen_attempt=2, best_effort=True
        )
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT attempt_count, chosen_attempt, best_effort, image_sha256 "
                    "FROM book_pages WHERE book_id = :id AND page_number = 1"
                ),
                {"id": book_id},
            ).one()
        assert (row.attempt_count, row.chosen_attempt, bool(row.best_effort)) == (3, 2, True)
        assert row.image_sha256 == __import__("hashlib").sha256(b"page 1").hexdigest()
        with pytest.raises(ValueError):
            books.record_page_review(
                book_id, 1, review=VERDICT, attempt_count=1, chosen_attempt=2
            )

    def test_a_book_without_plates_cannot_complete_unreviewed_either(
        self, engine: Engine, templates: TemplateStore, books: DatabaseBookStore
    ) -> None:
        """Every picture is reviewed now, plated or composed from scratch."""
        book_id = new_book(engine, books, "composed")
        books.save_page(book_id, 1, b"art")
        with pytest.raises(UnreviewedImages):
            books.save_status(book_id, BookStatus.COMPLETE, [])

    def test_a_book_from_before_every_picture_was_reviewed_still_completes(
        self, engine: Engine, templates: TemplateStore, books: DatabaseBookStore
    ) -> None:
        """Nothing looked at an old book's pictures; asking would block its edits."""
        book_id = new_book(engine, books, "legacy")
        with engine.begin() as conn:
            conn.execute(
                update(BookRow).where(BookRow.id == book_id).values(review_required=False)
            )
        books.save_page(book_id, 1, b"old art")
        books.save_status(book_id, BookStatus.COMPLETE, [])
        assert unreviewed_images(engine) == []


# --- plate approval (the contract T5/T6 build on) -----------------------------------


class TestApproval:
    def test_approve_is_idempotent_and_records_operator_time_and_prompt_hash(
        self, engine: Engine, templates: TemplateStore
    ) -> None:
        plate_id = add_plate(templates, 3)
        templates.approve_plate(plate_id, operator=OPERATOR)
        query = text(
            "SELECT approved_by, approved_at, prompt_hash, status FROM template_plates "
            "WHERE id = :id"
        )
        with engine.connect() as conn:
            first = conn.execute(query, {"id": plate_id}).one()

        templates.approve_plate(plate_id, operator="someone-else")

        with engine.connect() as conn:
            assert conn.execute(query, {"id": plate_id}).one() == first
        assert first.approved_by == OPERATOR and first.approved_at is not None
        assert first.status == PlateStatus.APPROVED and len(first.prompt_hash) == 64

    @pytest.mark.parametrize(
        "slots",
        [
            pytest.param([], id="no-slots"),
            pytest.param(slots_for(["wolf"]), id="star-missing"),
            pytest.param(slots_for(["red", "wolf", "wolf"]), id="duplicate"),
            pytest.param(slots_for(["red", "wolf", "mother"]), id="extra"),
        ],
    )
    def test_a_plate_needs_exactly_one_slot_per_character_on_its_page(
        self, templates: TemplateStore, slots: list[Slot]
    ) -> None:
        assert cast_on(RED, 4) == ["red", "wolf"]
        plate_id = add_plate(templates, 4, slots=slots)
        with pytest.raises(PlateNotApprovable, match="exactly one slot"):
            templates.approve_plate(plate_id, operator=OPERATOR)
        assert templates.plate_status(plate_id) == PlateStatus.DRAFT

    def test_approving_a_replacement_retires_the_previous_plate(
        self, templates: TemplateStore
    ) -> None:
        first = add_plate(templates, 6)
        templates.approve_plate(first, operator=OPERATOR)
        second = add_plate(templates, 6)
        templates.approve_plate(second, operator=OPERATOR)
        assert templates.plate_status(first) == PlateStatus.RETIRED
        assert templates.plate_status(second) == PlateStatus.APPROVED
        with pytest.raises(PlateNotApprovable, match="retired"):
            templates.approve_plate(first, operator=OPERATOR)

    @pytest.mark.parametrize(
        "slot",
        [
            Slot(cast_key="red", rect=(0.8, 0.0, 0.5, 0.5), scale=0.5, facing="left"),
            Slot(cast_key="red", rect=(0.0, 0.0, 0.5, 0.5), scale=0.0, facing="left"),
            Slot(cast_key="red", rect=(0.0, 0.0, 0.5, 0.5), scale=0.5, facing="up"),
        ],
        ids=["outside-the-art", "zero-scale", "unknown-facing"],
    )
    def test_a_malformed_slot_is_refused_when_the_plate_is_added(
        self, templates: TemplateStore, slot: Slot
    ) -> None:
        with pytest.raises(TemplateError):
            add_plate(templates, 2, slots=[slot])

    def test_a_plate_for_an_unknown_story_or_page_is_refused(
        self, templates: TemplateStore
    ) -> None:
        with pytest.raises(TemplateError, match="seed it first"):
            templates.add_plate(
                "no_such_story", 1, image=b"x", media_type="image/png", prompt="p", slots=[]
            )
        with pytest.raises(TemplateError, match=r"not 0\.\.20"):
            add_plate(templates, 21, slots=[])

    def test_a_plate_whose_page_cast_changed_blocks_the_next_version(
        self, engine: Engine, templates: TemplateStore, books: DatabaseBookStore
    ) -> None:
        """A seed that adds a character to a page can't pin a plate with no slot for them."""
        approve_all(templates)
        first = templates.current_version(RED.key)
        pages = list(RED.pages)
        scene = dataclasses.replace(pages[0].scene, cast=("mother", "grandmother"))
        pages[0] = dataclasses.replace(pages[0], scene=scene)

        report = templates.seed([dataclasses.replace(RED, pages=tuple(pages))])

        assert report.page_revisions == 1
        assert templates.current_version(RED.key) == first
