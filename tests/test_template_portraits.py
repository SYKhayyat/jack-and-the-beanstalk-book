"""Fixed portraits of supporting characters.

* **Ordering.** Portraits list newest first, and one character's list is the
  same rows filtered, never bytes.
* **Fallback.** A book drawn on a version that pins a character's portrait uses
  it when the visitor gave no photo; a visitor's own photo always wins; a book
  whose version has no portrait for someone leaves them out, as before.
* **No portrait on a page.** A cast member with no portrait and no photo is
  simply not an anchor; the prompt names only the images actually sent.
* **The cap.** A plate call carries at most `MAX_PLATE_ANCHORS` character
  anchors, the star's face first.
* **The importer.** Files are decoded before anything is written, the star and
  strangers are refused, and an approved portrait is replaced only on request.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image
from sqlalchemy import Engine

from app.books import SEEDS, StorySeed, picture_cast
from app.models.book import AboutMe
from app.scripts.import_portraits import ImportError_
from app.scripts.import_portraits import run as import_run
from app.services.book_runner import BookRunner
from app.services.book_store import CharacterReference
from app.services.db_book_store import DatabaseBookStore
from app.services.prompts import MAX_PLATE_ANCHORS, edit_reference_cast
from app.services.template_store import (
    COVER_PAGE,
    PAGE_NUMBERS,
    PlateStatus,
    Slot,
    TemplateStore,
)
from app.services.user_store import DatabaseUserStore, User

RED: StorySeed = next(seed for seed in SEEDS if seed.key == "little_red_riding_hood")
JACK: StorySeed = next(seed for seed in SEEDS if seed.key == "jack_and_the_beanstalk")
OPERATOR = "operator-sub"


def _png(colour: tuple[int, int, int] = (90, 120, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _write(directory: Path, name: str, data: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(data)


def _store(engine: Engine) -> TemplateStore:
    store = TemplateStore(engine)
    store.seed(SEEDS)
    return store


def _slots(cast_keys: list[str]) -> list[Slot]:
    width = 1 / max(len(cast_keys), 1)
    return [
        Slot(cast_key=key, rect=(index * width, 0.4, width, 0.5), scale=0.5, facing="toward")
        for index, key in enumerate(cast_keys)
    ]


def _approve_every_plate(store: TemplateStore, seed: StorySeed) -> None:
    """Make the story complete, so a version is written. Mirrors the plate tests."""
    for number in PAGE_NUMBERS:
        scene = seed.cover if number == COVER_PAGE else seed.pages[number - 1].scene
        cast = list(picture_cast(seed.star, scene.cast, star_in_picture=scene.star_in_picture))
        plate_id = store.add_plate(
            seed.key,
            number,
            image=f"plate {number}".encode(),
            media_type="image/png",
            prompt=f"empty scene {number}, no people, no text",
            slots=_slots(cast),
        )
        store.approve_plate(plate_id, operator=OPERATOR)


def _book_on_current_version(engine: Engine, store: TemplateStore) -> str:
    DatabaseUserStore(engine).upsert(User(sub="reader", email="r@example.com", name="R"))
    books = DatabaseBookStore(engine)
    book_id = "book-portrait"
    books.save_record(
        edition=store.edition_for_new_book(RED.key),
        book_id=book_id,
        user_id="reader",
        about=AboutMe.model_validate({"prompt_name": "Ada", "about_name": "Ada"}),
        generation_id=f"gen-{book_id}",
    )
    return book_id


class _Books:
    """Only what `_characters` reads: the visitor's own supporting faces."""

    def __init__(self, characters: dict[str, CharacterReference]) -> None:
        self._characters = characters

    def load_characters(self, book_id: str) -> dict[str, CharacterReference]:
        return dict(self._characters)


def _characters_for(
    store: TemplateStore, book_id: str, visitor: dict[str, CharacterReference]
) -> dict[str, CharacterReference]:
    stub = SimpleNamespace(_book_store=_Books(visitor), _templates=store)
    return BookRunner._characters(cast(Any, stub), book_id, visitor)


# --- ordering -----------------------------------------------------------------------


class TestOrdering:
    def test_portraits_list_newest_first(self, engine: Engine) -> None:
        store = _store(engine)
        first = store.add_portrait(
            RED.key, "wolf", image=_png((1, 1, 1)), media_type="image/png"
        )
        second = store.add_portrait(
            RED.key, "wolf", image=_png((2, 2, 2)), media_type="image/png"
        )
        listed = [p.id for p in store.portraits(RED.key, "wolf")]
        assert listed == [second, first]

    def test_one_character_is_the_same_rows_filtered(self, engine: Engine) -> None:
        store = _store(engine)
        store.add_portrait(RED.key, "wolf", image=_png(), media_type="image/png")
        store.add_portrait(RED.key, "mother", image=_png(), media_type="image/png")
        everyone = store.portraits(RED.key)
        wolf = store.portraits(RED.key, "wolf")
        assert {p.cast_key for p in everyone} == {"wolf", "mother"}
        assert [p.id for p in wolf] == [p.id for p in everyone if p.cast_key == "wolf"]

    def test_the_star_and_strangers_have_no_portrait(self, engine: Engine) -> None:
        store = _store(engine)
        with pytest.raises(Exception, match="star"):
            store.add_portrait(RED.key, "red", image=_png(), media_type="image/png")
        with pytest.raises(Exception, match="not in this story"):
            store.add_portrait(RED.key, "dragon", image=_png(), media_type="image/png")


# --- fallback -----------------------------------------------------------------------


class TestFallback:
    def test_a_book_uses_the_fixed_portrait_when_the_visitor_gave_no_photo(
        self, engine: Engine
    ) -> None:
        store = _store(engine)
        portrait = store.add_portrait(
            RED.key, "wolf", image=_png((9, 9, 9)), media_type="image/png"
        )
        store.approve_portrait(portrait, operator=OPERATOR)
        _approve_every_plate(store, RED)
        book_id = _book_on_current_version(engine, store)

        characters = _characters_for(store, book_id, {})

        assert set(characters) == {"wolf"}
        assert characters["wolf"].portrait == store.portrait_image(portrait)[0]
        assert characters["wolf"].photo == characters["wolf"].portrait

    def test_a_visitor_photo_always_wins_over_the_fixed_portrait(self, engine: Engine) -> None:
        store = _store(engine)
        portrait = store.add_portrait(
            RED.key, "wolf", image=_png((9, 9, 9)), media_type="image/png"
        )
        store.approve_portrait(portrait, operator=OPERATOR)
        _approve_every_plate(store, RED)
        book_id = _book_on_current_version(engine, store)
        mine = CharacterReference(
            portrait=b"visitor-portrait",
            portrait_media_type="image/png",
            photo=b"visitor-photo",
            photo_media_type="image/png",
        )

        characters = _characters_for(store, book_id, {"wolf": mine})

        assert characters["wolf"] is mine

    def test_a_book_whose_version_has_no_portrait_leaves_that_character_out(
        self, engine: Engine
    ) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)  # no portraits at all
        book_id = _book_on_current_version(engine, store)

        assert _characters_for(store, book_id, {}) == {}
        assert store.book_portraits(book_id) == {}

    def test_a_book_with_no_pinned_version_has_no_fixed_portraits(
        self, engine: Engine
    ) -> None:
        store = _store(engine)
        DatabaseUserStore(engine).upsert(User(sub="reader", email="r@example.com", name="R"))
        assert store.book_portraits("never-saved") == {}

    def test_a_portrait_approved_after_the_book_started_is_not_pinned(
        self, engine: Engine
    ) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        book_id = _book_on_current_version(engine, store)
        later = store.add_portrait(RED.key, "wolf", image=_png(), media_type="image/png")
        store.approve_portrait(later, operator=OPERATOR)

        assert "wolf" not in store.book_portraits(book_id)


# --- the anchor cap -----------------------------------------------------------------


class TestAnchorCap:
    def test_a_plate_carries_at_most_the_cap_of_character_anchors(self) -> None:
        slots = _slots(["wolf", "mother", "grandmother", "woodcutter"])
        supplied = {"wolf", "mother", "grandmother", "woodcutter"}

        keys = edit_reference_cast(slots, cast(Any, None), star="red", supplied=supplied)

        assert len(keys) <= MAX_PLATE_ANCHORS
        assert keys == ["wolf", "mother"]

    def test_the_star_is_kept_first_when_the_cap_bites(self) -> None:
        slots = _slots(["wolf", "red", "mother", "grandmother"])
        supplied = {"wolf", "mother", "grandmother"}

        keys = edit_reference_cast(slots, cast(Any, None), star="red", supplied=supplied)

        assert keys[0] == "red"
        assert len(keys) == MAX_PLATE_ANCHORS

    def test_a_cast_member_with_no_portrait_is_not_an_anchor(self) -> None:
        slots = _slots(["wolf", "mother"])

        keys = edit_reference_cast(slots, cast(Any, None), star="red", supplied={"mother"})

        assert keys == ["mother"]


# --- the importer -------------------------------------------------------------------


class TestImporter:
    def test_without_approve_files_are_stored_as_drafts(
        self, tmp_path: Path, engine: Engine
    ) -> None:
        store = _store(engine)
        directory = tmp_path / "portraits"
        _write(directory, "wolf.png", _png())
        _write(directory, "mother.png", _png())

        import_run(
            story=RED,
            store=store,
            directory=directory,
            operator="tester",
            approve=False,
            replace=False,
            out=io.StringIO(),
        )

        statuses = {p.cast_key: p.status for p in store.portraits(RED.key)}
        assert statuses == {"wolf": PlateStatus.DRAFT, "mother": PlateStatus.DRAFT}

    def test_approve_stores_and_approves_each_file(
        self, tmp_path: Path, engine: Engine
    ) -> None:
        store = _store(engine)
        directory = tmp_path / "portraits"
        _write(directory, "wolf.png", _png())

        result = import_run(
            story=RED,
            store=store,
            directory=directory,
            operator="tester",
            approve=True,
            replace=False,
            out=io.StringIO(),
        )

        assert result.approved == ["wolf"]
        assert [p.status for p in store.portraits(RED.key, "wolf")] == [PlateStatus.APPROVED]

    def test_the_star_file_is_refused_and_nothing_is_stored(
        self, tmp_path: Path, engine: Engine
    ) -> None:
        store = _store(engine)
        directory = tmp_path / "portraits"
        _write(directory, "wolf.png", _png())
        _write(directory, "red.png", _png())

        with pytest.raises(ImportError_, match="star"):
            import_run(
                story=RED,
                store=store,
                directory=directory,
                operator="tester",
                approve=True,
                replace=False,
                out=io.StringIO(),
            )
        assert store.portraits(RED.key) == []

    def test_a_stranger_is_refused(self, tmp_path: Path, engine: Engine) -> None:
        store = _store(engine)
        directory = tmp_path / "portraits"
        _write(directory, "dragon.png", _png())

        with pytest.raises(ImportError_, match="not a supporting character"):
            import_run(
                story=RED,
                store=store,
                directory=directory,
                operator="tester",
                approve=False,
                replace=False,
                out=io.StringIO(),
            )

    def test_a_broken_file_fails_before_any_file_is_stored(
        self, tmp_path: Path, engine: Engine
    ) -> None:
        store = _store(engine)
        directory = tmp_path / "portraits"
        _write(directory, "mother.png", _png())
        _write(directory, "wolf.png", b"not a picture at all")

        with pytest.raises(ImportError_, match=r"wolf\.png"):
            import_run(
                story=RED,
                store=store,
                directory=directory,
                operator="tester",
                approve=True,
                replace=False,
                out=io.StringIO(),
            )
        assert store.portraits(RED.key) == []

    def test_an_approved_portrait_is_not_replaced_without_replace(
        self, tmp_path: Path, engine: Engine
    ) -> None:
        store = _store(engine)
        directory = tmp_path / "first"
        _write(directory, "wolf.png", _png((1, 1, 1)))
        import_run(
            story=RED,
            store=store,
            directory=directory,
            operator="tester",
            approve=True,
            replace=False,
            out=io.StringIO(),
        )
        second = tmp_path / "second"
        _write(second, "wolf.png", _png((2, 2, 2)))

        with pytest.raises(ImportError_, match="already approved"):
            import_run(
                story=RED,
                store=store,
                directory=second,
                operator="tester",
                approve=True,
                replace=False,
                out=io.StringIO(),
            )
        assert len(store.portraits(RED.key, "wolf")) == 1

    def test_replace_approves_the_new_one_and_retires_the_old(
        self, tmp_path: Path, engine: Engine
    ) -> None:
        store = _store(engine)
        first = tmp_path / "first"
        _write(first, "wolf.png", _png((1, 1, 1)))
        import_run(
            story=RED,
            store=store,
            directory=first,
            operator="tester",
            approve=True,
            replace=False,
            out=io.StringIO(),
        )
        second = tmp_path / "second"
        _write(second, "wolf.png", _png((2, 2, 2)))

        import_run(
            story=RED,
            store=store,
            directory=second,
            operator="tester",
            approve=True,
            replace=True,
            out=io.StringIO(),
        )

        statuses = sorted(p.status for p in store.portraits(RED.key, "wolf"))
        assert statuses == [PlateStatus.APPROVED, PlateStatus.RETIRED]

    def test_replace_without_approve_is_refused(self, tmp_path: Path, engine: Engine) -> None:
        store = _store(engine)
        directory = tmp_path / "portraits"
        _write(directory, "wolf.png", _png())

        with pytest.raises(ImportError_, match="--replace needs --approve"):
            import_run(
                story=RED,
                store=store,
                directory=directory,
                operator="tester",
                approve=False,
                replace=True,
                out=io.StringIO(),
            )

    def test_jack_portraits_are_for_jacks_cast(self, tmp_path: Path, engine: Engine) -> None:
        store = _store(engine)
        directory = tmp_path / "portraits"
        _write(directory, "giant.png", _png())

        import_run(
            story=JACK,
            store=store,
            directory=directory,
            operator="tester",
            approve=False,
            replace=False,
            out=io.StringIO(),
        )

        assert [p.cast_key for p in store.portraits(JACK.key)] == ["giant"]
