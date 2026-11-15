"""A plate's working copy, and a book reading each plate only when its page is drawn.

Audit 11-15 S1: every book used to decode all of its full-size plates before
page 1 could start.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from sqlalchemy import Engine, select

from app.db import TemplatePlateRow
from app.services.image_providers.openai_provider import _SIZE_BY_ASPECT
from app.services.template_store import (
    WORKING_EDGE,
    PinnedPlate,
    Slot,
    TemplateStore,
    UnknownTemplate,
    working_copy,
)

from .test_template_portraits import _approve_every_plate
from .test_template_premades import RED, _book, _store

SLOTS = (Slot(cast_key="red", rect=(0.1, 0.4, 0.3, 0.5), scale=0.5, facing="toward"),)


def _png(size: tuple[int, int] = (2388, 1791)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (90, 140, 70)).save(out, format="PNG")
    return out.getvalue()


def _add(store: TemplateStore, image: bytes) -> str:
    return store.add_plate(
        RED.key, 3, image=image, media_type="image/png", prompt="empty", slots=list(SLOTS)
    )


def _stored_copy(engine: Engine, plate_id: str) -> bytes | None:
    with engine.connect() as conn:
        return conn.execute(
            select(TemplatePlateRow.working_image).where(TemplatePlateRow.id == plate_id)
        ).scalar_one()


class TestWorkingCopy:
    def test_a_full_plate_is_shrunk_to_the_working_edge_as_jpeg(self) -> None:
        with Image.open(io.BytesIO(working_copy(_png()))) as copy:
            assert copy.format == "JPEG"
            assert max(copy.size) == WORKING_EDGE
            assert copy.size == (WORKING_EDGE, round(1791 * WORKING_EDGE / 2388))

    def test_a_small_plate_is_never_enlarged(self) -> None:
        with Image.open(io.BytesIO(working_copy(_png((800, 600))))) as copy:
            assert copy.size == (800, 600)

    def test_the_edge_covers_every_picture_a_provider_returns(self) -> None:
        # The copy is only ever shrunk to the drawn page, so it must not be smaller.
        longest = max(
            int(edge) for size in _SIZE_BY_ASPECT.values() for edge in size.split("x")
        )
        assert longest <= WORKING_EDGE


class TestDrawingPlate:
    def test_the_first_use_makes_and_keeps_the_copy(self, engine: Engine) -> None:
        store = _store(engine)
        plate_id = _add(store, _png())
        assert _stored_copy(engine, plate_id) is None

        art = store.drawing_plate(PinnedPlate(plate_id, SLOTS))

        assert art.media_type == "image/jpeg"
        assert art.slots == SLOTS
        assert _stored_copy(engine, plate_id) == art.image

    def test_a_later_use_reads_the_kept_copy(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(engine)
        plate_id = _add(store, _png())
        first = store.drawing_plate(PinnedPlate(plate_id, SLOTS))

        def no_second_shrink(_image: bytes) -> bytes:
            raise AssertionError("the copy is made once")

        monkeypatch.setattr("app.services.template_store.working_copy", no_second_shrink)
        assert store.drawing_plate(PinnedPlate(plate_id, SLOTS)).image == first.image

    def test_bytes_that_are_not_a_picture_are_drawn_from_as_they_are(
        self, engine: Engine
    ) -> None:
        store = _store(engine)
        plate_id = _add(store, b"not a picture")

        art = store.drawing_plate(PinnedPlate(plate_id, SLOTS))

        assert (art.image, art.media_type) == (b"not a picture", "image/png")
        assert _stored_copy(engine, plate_id) is None

    def test_an_unknown_plate_is_refused(self, engine: Engine) -> None:
        with pytest.raises(UnknownTemplate):
            _store(engine).drawing_plate(PinnedPlate("missing", SLOTS))


class TestBookPlates:
    def test_a_book_learns_its_plates_without_their_pictures(self, engine: Engine) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)

        plates = store.book_plates(_book(engine, store))

        assert set(plates) == set(range(len(RED.pages) + 1))
        assert all(isinstance(plate, PinnedPlate) for plate in plates.values())
        assert not any(hasattr(plate, "image") for plate in plates.values())
