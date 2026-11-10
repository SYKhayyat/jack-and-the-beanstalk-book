"""The blob layer: pointers in the database, bytes in the store, nothing lossy."""

from __future__ import annotations

import io
from collections.abc import Iterator

import pytest
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.blobs import MemoryBlobStore, is_pointer, set_blob_store
from app.db import Base, BookPageRow, BookRow


@pytest.fixture
def store() -> Iterator[MemoryBlobStore]:
    memory = MemoryBlobStore()
    set_blob_store(memory)
    yield memory
    set_blob_store(None)


def _png(color: tuple[int, int, int], size: int = 96, alpha: bool = False) -> bytes:
    image = Image.new(
        "RGBA" if alpha else "RGB", (size, size), (*color, 128) if alpha else color
    )
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _pixels(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        return image.tobytes()


def test_png_round_trips_pixel_identical_and_is_stored_as_webp(store: MemoryBlobStore) -> None:
    data = _png((10, 200, 30), alpha=True)
    pointer = store.put(data)
    assert is_pointer(pointer)
    (key,) = store.objects
    assert key.endswith(".webp") and len(store.objects[key]) < len(data)
    back = store.get(pointer)
    assert back.startswith(b"\x89PNG") and _pixels(back) == _pixels(data)


def test_identical_content_is_one_object(store: MemoryBlobStore) -> None:
    data = _png((1, 2, 3))
    assert store.put(data) == store.put(data)
    assert len(store.objects) == 1


def test_non_png_is_stored_untouched(store: MemoryBlobStore) -> None:
    for data in (b"%PDF-1.7 whatever", b"\xff\xd8\xff\xe0jpegish", b"RIFFxxxxWEBPyyyy"):
        assert store.get(store.put(data)) == data
    assert all(key.endswith(".bin") for key in store.objects)


def test_columns_hold_pointers_but_stores_still_see_bytes(store: MemoryBlobStore) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    data = _png((5, 5, 5))
    with Session(engine) as session:
        session.add(BookRow(id="b", status="done", generation_id="g", about={}, cover=data))
        session.flush()
        session.add(
            BookPageRow(book_id="b", page_number=1, image=data, media_type="image/png")
        )
        session.commit()
    with engine.connect() as conn:
        raw = conn.execute(text("SELECT cover FROM books")).scalar_one()
    assert is_pointer(bytes(raw)) and len(raw) < 100
    with Session(engine) as session:
        book = session.get(BookRow, "b")
        assert book is not None and _pixels(book.cover or b"") == _pixels(data)


def test_inline_blobs_still_read_when_a_store_is_installed(store: MemoryBlobStore) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    set_blob_store(None)
    with Session(engine) as session:
        session.add(
            BookRow(id="b", status="done", generation_id="g", about={}, pdf=b"%PDF-old")
        )
        session.commit()
    set_blob_store(store)
    with Session(engine) as session:
        book = session.get(BookRow, "b")
        assert book is not None and book.pdf == b"%PDF-old"


def test_a_pointer_without_a_store_fails_loudly(store: MemoryBlobStore) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(BookRow(id="b", status="done", generation_id="g", about={}, pdf=b"%PDF-x"))
        session.commit()
    set_blob_store(None)
    with Session(engine) as session, pytest.raises(RuntimeError, match="R2"):
        session.get(BookRow, "b")


def test_migrate_moves_inline_blobs_and_is_idempotent(store: MemoryBlobStore) -> None:
    from app.scripts.migrate_blobs import migrate

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    set_blob_store(None)
    data = _png((9, 9, 9))
    with Session(engine) as session:
        session.add(BookRow(id="b", status="done", generation_id="g", about={}, cover=data))
        session.commit()
    set_blob_store(store)
    assert migrate(engine, store)[0] == 1
    assert migrate(engine, store)[0] == 0
    with engine.connect() as conn:
        assert is_pointer(bytes(conn.execute(text("SELECT cover FROM books")).scalar_one()))
    with Session(engine) as session:
        book = session.get(BookRow, "b")
        assert book is not None and _pixels(book.cover or b"") == _pixels(data)
