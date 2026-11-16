"""About how long a new book has left (audit 11-15, other implementations A9).

A fresh book drawn in one go keeps how long it took; the next one is told the
median of the last 30 of those, and the browser counts down from when it
started. Edits and other partial jobs are a different length of work, so they
are neither timed nor given an estimate.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base, BookRow
from app.services.db_book_store import DatabaseBookStore
from tests.test_logging import _make_portrait, _start_book, _wait_for_book


def _store_with(times: list[float | None]) -> DatabaseBookStore:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for n, seconds in enumerate(times):
            session.add(
                BookRow(
                    id=f"b{n}",
                    status="complete",
                    generation_id="g",
                    about={},
                    drawn_seconds=seconds,
                )
            )
        session.commit()
    return DatabaseBookStore(engine)


def test_the_typical_time_is_a_median_and_needs_three_books() -> None:
    assert _store_with([40.0, 50.0]).typical_drawn_seconds() is None
    assert _store_with([40.0, None, 50.0]).typical_drawn_seconds() is None
    assert _store_with([40.0, 300.0, 50.0]).typical_drawn_seconds() == 50.0


def _timed_writes(client: TestClient) -> list[dict[str, Any]]:
    """Every status extra the runner writes, as it writes it."""
    store = client.app.state.container.book_store  # type: ignore[attr-defined]
    seen: list[dict[str, Any]] = []
    real = store.save_status

    def save_status(*args: Any, **kwargs: Any) -> None:
        seen.append(dict(kwargs.get("extra") or {}))
        real(*args, **kwargs)

    store.save_status = save_status
    return seen


def _drawn_seconds(client: TestClient, book_id: str) -> float | None:
    engine = client.app.state.container.engine  # type: ignore[attr-defined]
    with engine.connect() as conn:
        return conn.execute(
            select(BookRow.drawn_seconds).where(BookRow.id == book_id)
        ).scalar_one()


def test_a_whole_book_is_timed_and_the_next_is_told_what_to_expect(
    client: TestClient,
) -> None:
    writes = _timed_writes(client)
    first = _start_book(client, _make_portrait(client))
    assert _wait_for_book(client, first)["status"] == "complete"

    took = _drawn_seconds(client, first)
    assert took is not None and took > 0
    assert not any("expected_seconds" in extra for extra in writes), "no history yet"

    store = client.app.state.container.book_store  # type: ignore[attr-defined]
    for other in ("x1", "x2"):
        with Session(client.app.state.container.engine) as session:  # type: ignore[attr-defined]
            session.add(
                BookRow(
                    id=other,
                    status="complete",
                    generation_id="g",
                    about={},
                    drawn_seconds=42.0,
                )
            )
            session.commit()
    expected = store.typical_drawn_seconds()
    assert expected == 42.0

    second = _start_book(client, _make_portrait(client))
    assert _wait_for_book(client, second)["status"] == "complete"
    timed = [extra for extra in writes if "expected_seconds" in extra]
    assert timed, "the second book's progress carried an estimate"
    assert {extra["expected_seconds"] for extra in timed} == {42}
    assert all(extra["job_started"] > 1_700_000_000 for extra in timed)


def test_an_edit_is_neither_timed_nor_given_an_estimate(client: TestClient) -> None:
    book = _start_book(client, _make_portrait(client))
    assert _wait_for_book(client, book)["status"] == "complete"
    store = client.app.state.container.book_store  # type: ignore[attr-defined]
    store.record_drawn_seconds(book, 1.0)
    writes = _timed_writes(client)

    response = client.post(
        f"/api/book/{book}/edit",
        data={"action": "page", "page_number": "2", "reason": "wrong_moment"},
    )
    assert response.status_code == 202, response.text
    assert _wait_for_book(client, book)["status"] == "complete"

    assert _drawn_seconds(client, book) == 1.0
    assert not any("expected_seconds" in extra for extra in writes)
