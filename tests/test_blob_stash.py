"""Pictures are encoded and uploaded before the row's transaction opens.

`BlobRef` would otherwise do the lossless encode and the upload while binding
the statement: many seconds holding a pooled connection, and on SQLite the
database's one write lock.
"""

from __future__ import annotations

import io
from collections.abc import Iterator

import pytest
from PIL import Image
from sqlalchemy.engine import Engine

from app.blobs import MemoryBlobStore, is_pointer, set_blob_store, stash
from app.models.book import AboutMe
from app.services.db_book_store import DatabaseBookStore
from tests.helpers import edition_in


def _png(shade: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 64), (shade, 40, 90)).save(out, format="PNG")
    return out.getvalue()


class WatchingStore(MemoryBlobStore):
    """Fails the test if a put happens while the engine has a connection out."""

    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self.engine = engine
        self.puts = 0

    def put(self, data: bytes) -> bytes:
        assert self.engine.pool.checkedout() == 0, "uploaded inside a transaction"  # type: ignore[attr-defined]
        self.puts += 1
        return super().put(data)


@pytest.fixture
def watching(engine: Engine) -> Iterator[WatchingStore]:
    store = WatchingStore(engine)
    set_blob_store(store)
    yield store
    set_blob_store(None)


def test_every_picture_write_uploads_outside_the_transaction(
    engine: Engine, watching: WatchingStore
) -> None:
    books = DatabaseBookStore(engine)
    books.save_record(
        edition=edition_in(books),
        book_id="stash-book",
        user_id=None,
        about=AboutMe(prompt_name="Jack", about_name="Jack"),
        generation_id="g",
    )
    books.save_portrait("stash-book", _png(1))
    books.save_source_photo("stash-book", _png(2))
    books.save_cover("stash-book", _png(3))
    books.save_page("stash-book", 1, _png(4))
    books.save_variant("stash-book", "page-1", 480, "sha", b"RIFFxxxxWEBPvariant")
    books.save_pdf("stash-book", b"%PDF-1.7 book")

    assert watching.puts == 6
    assert books.read_page("stash-book", 1) == watching.get(watching.put(_png(4)))
    assert books.read_pdf("stash-book") == b"%PDF-1.7 book"


def test_stash_passes_pointers_through_and_needs_no_store() -> None:
    data = _png(5)
    assert stash(data) == data
    memory = MemoryBlobStore()
    set_blob_store(memory)
    try:
        pointer = stash(data)
        assert is_pointer(pointer)
        assert stash(pointer) == pointer
        assert len(memory.objects) == 1
    finally:
        set_blob_store(None)
