"""A screen copy asked for many times at once is encoded once."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from app.routes import book_media
from app.services import pictures
from app.single_flight import KeyedLocks


class FakeStore:
    def __init__(self) -> None:
        self.variants: dict[tuple[str, str, int, str], bytes] = {}
        self.lock = threading.Lock()

    def read_variant(self, book_id: str, slot: str, width: int, sha: str) -> bytes | None:
        with self.lock:
            return self.variants.get((book_id, slot, width, sha))

    def read_picture(self, book_id: str, slot: str) -> bytes:
        return b"original"

    def save_variant(self, book_id: str, slot: str, width: int, sha: str, data: bytes) -> None:
        with self.lock:
            self.variants[(book_id, slot, width, sha)] = data


def test_concurrent_misses_encode_once_and_all_get_the_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    sha = hashlib.sha256(b"original").hexdigest()
    calls: list[int] = []

    def slow_encode(data: bytes, width: int) -> bytes:
        calls.append(width)
        time.sleep(0.05)
        return b"webp"

    monkeypatch.setattr(book_media, "encode_variant", slow_encode)
    store: Any = FakeStore()
    results: list[tuple[bytes, str]] = []
    start = threading.Barrier(6)

    def ask() -> None:
        start.wait()
        results.append(book_media._make_variant(store, "b", "page-1", 480, sha))

    threads = [threading.Thread(target=ask) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == [480]
    assert sorted(outcome for _, outcome in results) == ["encoded"] + ["stored"] * 5
    assert all(data == b"webp" for data, _ in results)
    assert len(pictures.variant_locks) == 0


def test_different_keys_do_not_wait_on_each_other() -> None:
    locks = KeyedLocks()
    inside = threading.Event()
    release = threading.Event()

    def hold_a() -> None:
        with locks.hold("a"):
            inside.set()
            release.wait(2)

    holder = threading.Thread(target=hold_a)
    holder.start()
    assert inside.wait(2)
    with locks.hold("b"):
        assert len(locks) == 2
    release.set()
    holder.join()
    assert len(locks) == 0


def test_a_failed_block_frees_its_key() -> None:
    locks = KeyedLocks()
    with pytest.raises(RuntimeError), locks.hold("a"):
        raise RuntimeError
    assert len(locks) == 0
