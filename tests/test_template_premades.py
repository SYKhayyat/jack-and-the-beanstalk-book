"""Pre-made pages: finished pictures for the pages the star is not in.

* **Store.** Only a page without the star can have one, and only once it has an
  approved plate, which the picture then belongs to. Approving replaces the
  page's previous one.
* **Version pinning.** A version pins an approved pre-made page beside its own
  plate; a new plate for the page leaves it out; a book keeps what it began on.
* **The runner.** A book uses the picture only when the visitor gave no photo of
  anyone on the page and the owner did not ask for that page to be redrawn; the
  record says it was pre-made and nothing was sent.
* **The importer.** Every file and page is checked before anything is written.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import Engine

from app.books import SEEDS, StorySeed
from app.models.book import AboutMe
from app.models.settings import CartoonSettings
from app.scripts.import_premades import ImportError_
from app.scripts.import_premades import run as import_run
from app.services.book_runner import _Artwork, _premade_attempts, _premade_for
from app.services.db_book_store import DatabaseBookStore
from app.services.template_store import (
    PlateNotApprovable,
    PlateStatus,
    PremadeArt,
    TemplateError,
    TemplateStore,
)
from app.services.user_store import DatabaseUserStore, User

from .helpers import edition_for
from .test_template_portraits import _approve_every_plate, _slots

RED: StorySeed = next(seed for seed in SEEDS if seed.key == "little_red_riding_hood")
OPERATOR = "operator-sub"
CUPBOARD = 9  # the-cupboard: grandmother and wolf, no Red


def _jpeg(colour: tuple[int, int, int] = (120, 90, 60)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def _store(engine: Engine) -> TemplateStore:
    store = TemplateStore(engine)
    store.seed(SEEDS)
    return store


def _book(engine: Engine, store: TemplateStore, book_id: str = "book-premade") -> str:
    DatabaseUserStore(engine).upsert(User(sub="reader", email="r@example.com", name="R"))
    DatabaseBookStore(engine).save_record(
        edition=store.edition_for_new_book(RED.key),
        book_id=book_id,
        user_id="reader",
        about=AboutMe.model_validate({"prompt_name": "Ada", "about_name": "Ada"}),
        generation_id=f"gen-{book_id}",
    )
    return book_id


def _approved_premade(store: TemplateStore, page: int = CUPBOARD) -> str:
    premade = store.add_premade(RED.key, page, image=_jpeg(), media_type="image/jpeg")
    store.approve_premade(premade, operator=OPERATOR)
    return premade


def _red_page(number: int):
    return next(p for p in edition_for("little_red_riding_hood").pages if p.number == number)


def _artwork(
    premades: dict[int, PremadeArt], visitor: frozenset[str] = frozenset()
) -> _Artwork:
    return _Artwork(
        about=None,
        settings=CartoonSettings(),
        portrait=b"star-portrait",
        portrait_media_type="image/png",
        photo=b"star-photo",
        photo_media_type="image/png",
        characters={},
        plates={},
        premades=premades,
        visitor_cast=visitor,
    )


def _art(number: int = CUPBOARD) -> PremadeArt:
    return PremadeArt(
        page_number=number, premade_id="p" * 32, image=_jpeg(), media_type="image/jpeg"
    )


# --- the store ------------------------------------------------------------------------


class TestStore:
    def test_a_page_with_the_star_has_no_premade(self, engine: Engine) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        with pytest.raises(TemplateError, match="shows the star"):
            store.add_premade(RED.key, 1, image=_jpeg(), media_type="image/jpeg")

    def test_a_page_needs_an_approved_plate_first(self, engine: Engine) -> None:
        store = _store(engine)
        with pytest.raises(TemplateError, match="no approved plate"):
            store.add_premade(RED.key, CUPBOARD, image=_jpeg(), media_type="image/jpeg")

    def test_approving_replaces_the_previous_one(self, engine: Engine) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        first = _approved_premade(store)
        second = _approved_premade(store)
        status = {p.id: p.status for p in store.premades(RED.key, CUPBOARD)}
        assert status == {first: PlateStatus.RETIRED, second: PlateStatus.APPROVED}

    def test_a_premade_from_an_old_plate_cannot_be_approved(self, engine: Engine) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        premade = store.add_premade(RED.key, CUPBOARD, image=_jpeg(), media_type="image/jpeg")
        cast = ["grandmother", "wolf"]
        newer = store.add_plate(
            RED.key,
            CUPBOARD,
            image=b"newer",
            media_type="image/png",
            prompt="empty",
            slots=_slots(cast),
        )
        store.approve_plate(newer, operator=OPERATOR)
        with pytest.raises(PlateNotApprovable, match="different plate"):
            store.approve_premade(premade, operator=OPERATOR)

    def test_a_plate_no_version_names_is_deleted_with_its_premades(
        self, engine: Engine
    ) -> None:
        store = _store(engine)
        plate = store.add_plate(
            RED.key,
            CUPBOARD,
            image=b"only",
            media_type="image/png",
            prompt="empty",
            slots=_slots(["grandmother", "wolf"]),
        )
        store.approve_plate(plate, operator=OPERATOR)  # story incomplete: no version
        store.add_premade(RED.key, CUPBOARD, image=_jpeg(), media_type="image/jpeg")
        store.delete_plate(plate)
        assert store.premades(RED.key) == []


# --- version pinning ------------------------------------------------------------------


class TestPinning:
    def test_a_book_on_a_version_with_the_premade_gets_it(self, engine: Engine) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        premade = _approved_premade(store)
        book_id = _book(engine, store)

        pinned = store.book_premades(book_id)

        assert set(pinned) == {CUPBOARD}
        assert pinned[CUPBOARD].premade_id == premade
        assert pinned[CUPBOARD].image == store.premade_image(premade)[0]

    def test_approving_writes_a_new_version(self, engine: Engine) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        before = store.current_version(RED.key)
        _approved_premade(store)
        after = store.current_version(RED.key)
        assert before is not None and after is not None
        assert after.number == before.number + 1

    def test_a_premade_approved_after_the_book_started_is_not_pinned(
        self, engine: Engine
    ) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        book_id = _book(engine, store)
        _approved_premade(store)
        assert store.book_premades(book_id) == {}

    def test_a_new_plate_leaves_the_premade_out(self, engine: Engine) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        _approved_premade(store)
        newer = store.add_plate(
            RED.key,
            CUPBOARD,
            image=b"newer",
            media_type="image/png",
            prompt="empty",
            slots=_slots(["grandmother", "wolf"]),
        )
        store.approve_plate(newer, operator=OPERATOR)
        book_id = _book(engine, store)
        assert store.book_premades(book_id) == {}

    def test_retiring_takes_it_out_of_later_versions(self, engine: Engine) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        premade = _approved_premade(store)
        early = _book(engine, store, "book-early")
        store.retire_premade(premade)
        late = _book(engine, store, "book-late")
        assert set(store.book_premades(early)) == {CUPBOARD}
        assert store.book_premades(late) == {}

    def test_a_book_with_no_version_has_none(self, engine: Engine) -> None:
        assert _store(engine).book_premades("never-saved") == {}


# --- the runner -----------------------------------------------------------------------


class TestRunner:
    def test_a_starless_page_with_no_visitor_photo_uses_it(self) -> None:
        art = _art()
        assert _premade_for(_red_page(CUPBOARD), _artwork({CUPBOARD: art}), {}) is art

    def test_a_visitor_photo_of_anyone_on_the_page_draws_it_instead(self) -> None:
        artwork = _artwork({CUPBOARD: _art()}, visitor=frozenset({"grandmother"}))
        assert _premade_for(_red_page(CUPBOARD), artwork, {}) is None

    def test_a_photo_of_someone_not_on_the_page_does_not_matter(self) -> None:
        artwork = _artwork({CUPBOARD: _art()}, visitor=frozenset({"mother"}))
        assert _premade_for(_red_page(CUPBOARD), artwork, {}) is not None

    def test_a_requested_fix_draws_the_page_again(self) -> None:
        artwork = _artwork({CUPBOARD: _art()})
        assert _premade_for(_red_page(CUPBOARD), artwork, {CUPBOARD: ["other"]}) is None

    def test_a_page_with_the_star_never_uses_one(self) -> None:
        page = _red_page(1)
        assert page.star_in_picture
        assert _premade_for(page, _artwork({1: _art(1)}), {}) is None

    def test_the_record_says_premade_and_sent_nothing(self) -> None:
        art = _art()
        attempts = _premade_attempts(art)
        assert attempts.image == art.image
        assert attempts.prompts == ("",)
        assert attempts.chosen == 1 and not attempts.best_effort
        (verdict,) = attempts.verdicts
        assert verdict["passed"] and verdict["provider"] == "premade"
        assert verdict["premade_id"] == art.premade_id


# --- the importer ---------------------------------------------------------------------


class TestImporter:
    def test_imports_and_approves_by_number_and_key(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        (tmp_path / "7.jpg").write_bytes(_jpeg())
        (tmp_path / "the-cupboard.jpg").write_bytes(_jpeg((1, 2, 3)))
        result = import_run(
            story=RED,
            store=store,
            directory=tmp_path,
            operator=OPERATOR,
            approve=True,
            replace=False,
            out=io.StringIO(),
        )
        assert sorted(result.approved) == [7, CUPBOARD]
        assert set(store.book_premades(_book(engine, store))) == {7, CUPBOARD}

    def test_a_star_page_fails_before_anything_is_written(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        (tmp_path / "1.jpg").write_bytes(_jpeg())
        (tmp_path / "9.jpg").write_bytes(_jpeg())
        with pytest.raises(ImportError_, match="shows the star"):
            import_run(
                story=RED,
                store=store,
                directory=tmp_path,
                operator=OPERATOR,
                approve=True,
                replace=False,
                out=io.StringIO(),
            )
        assert store.premades(RED.key) == []

    def test_a_broken_file_fails_before_anything_is_written(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        (tmp_path / "7.jpg").write_bytes(_jpeg())
        (tmp_path / "8.jpg").write_bytes(b"not a picture")
        with pytest.raises(ImportError_, match="not a usable picture"):
            import_run(
                story=RED,
                store=store,
                directory=tmp_path,
                operator=OPERATOR,
                approve=False,
                replace=False,
                out=io.StringIO(),
            )
        assert store.premades(RED.key) == []

    def test_an_approved_page_is_replaced_only_on_request(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        _approved_premade(store)
        (tmp_path / "9.jpg").write_bytes(_jpeg())
        kwargs = {"story": RED, "store": store, "directory": tmp_path, "operator": OPERATOR}
        with pytest.raises(ImportError_, match="already approved"):
            import_run(**kwargs, approve=True, replace=False, out=io.StringIO())
        with pytest.raises(ImportError_, match="needs --approve"):
            import_run(**kwargs, approve=False, replace=True, out=io.StringIO())
        result = import_run(**kwargs, approve=True, replace=True, out=io.StringIO())
        assert result.approved == [CUPBOARD]
