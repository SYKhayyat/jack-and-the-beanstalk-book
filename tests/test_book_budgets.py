"""What a book may spend, and who is told when it costs more than it should (C4).

Two operator knobs, both read off the same measured ledger:

* `REVIEWER_BUDGET_PERCENT` — past a share of the book's own fee, tier 2 is
  rested for that book. Tier 1 and the decision judge still look, so a picture
  is never passed unchecked, and the verdict says the reviewer did not run (it
  is the operator's budget, not an outage).
* `BOOK_COST_ALERT_CENTS` — a book whose spend passes the line writes one
  `book.cost_alert` line and is named on `/admin/status`, so the operator hears
  about a book that costs more than it sells for without reading the ledger.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.book_runner import BookRunner
from app.services.image_providers.base import ProviderError
from app.services.user_store import User
from app.sessions import SESSION_COOKIE
from tests.helpers import JACK_KEY, RecordingProvider, isolated_settings, make_photo_bytes
from tests.test_plate_import_and_edit import _approved_plates, _start_a_book
from tests.test_review_loop import (
    ABigPainter,
    ScriptedReviewer,
    artwork,
    grain,
    runner_with,
    said,
)

OPERATOR = "operator-sub"

#: A quarter per picture: a mock book is 22 calls, so $5.50 — over the $1 alert
#: the runner ships with, and more than the book sells for.
PICTURE_USD = 0.25


@contextmanager
def _site(tmp_path: Path, schema_template: Path, **overrides: Any) -> Iterator[TestClient]:
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(schema_template, storage / "app.sqlite3")
    settings = Settings(
        image_provider="mock",  # type: ignore[arg-type]
        app_env="test",
        storage_dir=storage,
        google_client_id="cid",
        google_client_secret="cs",
        session_secret="session-secret",
        operator_sub=OPERATOR,
        max_generations_per_minute=1000,
        **overrides,
    )
    with TestClient(create_app(settings, provider=RecordingProvider())) as client:
        _sign_in(client, OPERATOR)
        yield client


def _sign_in(client: TestClient, sub: str) -> None:
    client.app.state.container.user_store.upsert(  # type: ignore[attr-defined]
        User(sub=sub, name=sub, email=f"{sub}@example.com")
    )
    token = client.app.state.sessions.issue_google(  # type: ignore[attr-defined]
        sub=sub, name=sub, email=f"{sub}@example.com", ttl_seconds=3600
    )
    client.cookies.set(SESSION_COOKIE, token)


def _portrait(client: TestClient) -> str:
    response = client.post(
        "/api/cartoonify",
        files={"photo": ("me.png", make_photo_bytes(), "image/png")},
        data={"story": JACK_KEY, "photo_gender": "girl"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["generation_id"])


def _book(client: TestClient) -> str:
    started = client.post(
        "/api/book/start", data={"story": JACK_KEY, "generation_id": _portrait(client)}
    )
    assert started.status_code == 201, started.text
    book_id = str(started.json()["book_id"])
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if client.get(f"/api/book/{book_id}").json()["status"] in {"complete", "failed"}:
            return book_id
        time.sleep(0.05)
    raise AssertionError("the book never settled")


# --- the review budget ---------------------------------------------------------------


class _Ledger:
    """A cost ledger that reports one fixed spend for the book it is asked about."""

    def __init__(self, microusd: int) -> None:
        self.microusd = microusd
        self.reads = 0

    def book_costs_microusd(self, book_ids: list[str]) -> dict[str, int]:
        self.reads += 1
        return dict.fromkeys(book_ids, self.microusd)


def _budgeted(
    spend_microusd: int,
    reviewer: Any,
    *,
    percent: int = 100,
    price_cents: int = 99,
) -> BookRunner:
    runner = runner_with(reviewer)
    runner._costs = _Ledger(spend_microusd)
    runner._book_price_cents = price_cents
    runner._reviewer_budget_percent = percent
    return runner


def _judged(runner: BookRunner) -> Any:
    return asyncio.run(
        runner._judge(
            book_id="book",
            page_number=5,
            image=grain(),
            plate=None,
            artwork=artwork(plated=False),
            page_title="the kitchen",
            story_line="the mother sends Jack to market",
            cast_note="Only these people may be in the picture: the child.",
            star_in_picture=True,
            attempt=1,
            user="-",
        )
    )


def test_the_reviewer_is_rested_once_the_book_has_spent_its_budget() -> None:
    """At 100 % of the 99¢ fee, tier 2 is not paid for again on this book."""
    reviewer = ScriptedReviewer()  # no answers: being asked fails the test
    verdict = _judged(_budgeted(99 * 10_000, reviewer))

    assert reviewer.calls == 0
    assert verdict.passed  # tier 1 passed it, and its verdict is the one kept
    assert verdict.tiers_run == (1,)
    assert verdict.note == "the reviewer was rested on the book's review budget"
    # A budget is the operator's choice, not an outage: it must not read as one.
    assert verdict.tier2_failed is False


def test_under_the_budget_the_reviewer_is_asked_as_usual() -> None:
    reviewer = ScriptedReviewer(answers=[said(True, 90)])
    verdict = _judged(_budgeted(50 * 10_000, reviewer))

    assert reviewer.calls == 1
    assert verdict.judged_by == "reviewer"
    assert verdict.tiers_run == (1, 2)


def test_a_budget_of_zero_never_rests_the_reviewer() -> None:
    """0 is the default: money never silences tier 2 unless the operator asks."""
    reviewer = ScriptedReviewer(answers=[said(True, 90)])
    verdict = _judged(_budgeted(500 * 10_000, reviewer, percent=0))

    assert reviewer.calls == 1
    assert verdict.judged_by == "reviewer"


def test_the_budget_is_read_from_the_ledger_once_in_a_while(caplog: Any) -> None:
    """Not once per picture: the cache answers, and the rest is said once."""
    reviewer = ScriptedReviewer()
    runner = _budgeted(99 * 10_000, reviewer)
    with caplog.at_level(logging.WARNING, logger="app.services.book_runner"):
        for _ in range(3):
            _judged(runner)

    assert runner._costs is not None
    assert runner._costs.reads == 1
    rested = [r for r in caplog.records if "book.review_budget" in r.getMessage()]
    assert len(rested) == 1, [r.getMessage() for r in rested]


# --- the retry budget ----------------------------------------------------------------


class _Drawn:
    """The little of a GeneratedImage the loop reads after a draw."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.media_type = "image/png"
        self.model = "test"


def prompt_only(faults: Any, findings: str, mend: bool) -> str:
    """Every attempt asks for the same thing: this test is about the count."""
    return "draw it"


def _page_loop(runner: BookRunner) -> tuple[Any, list[str]]:
    """Run one page's attempt loop against a reviewer that never passes it."""
    sent: list[str] = []

    async def draw(prompt: str, mend: Any) -> Any:
        sent.append(prompt)
        return _Drawn(grain(seed=len(sent)))

    attempts = asyncio.run(
        runner._draw_until_pass(
            book_id="book",
            page_number=5,
            user="-",
            page_title="the kitchen",
            story_line="the mother sends Jack to market",
            cast_note="Only these people may be in the picture: the child.",
            star_in_picture=True,
            plate=None,
            artwork=artwork(plated=False),
            draw=draw,
            prompt_for=prompt_only,
        )
    )
    return attempts, sent


def _retry_budgeted(spend_microusd: int, *, percent: int = 100) -> BookRunner:
    runner = runner_with(
        ScriptedReviewer(answers=[said(False, 20, faults=("extra_person",))] * 6)
    )
    runner._costs = _Ledger(spend_microusd)
    runner._book_price_cents = 99
    runner._retry_budget_percent = percent
    return runner


def test_a_book_over_its_retry_budget_keeps_the_picture_it_has() -> None:
    """C4: past 100 % of the 99¢ fee, a page is drawn once and kept."""
    attempts, sent = _page_loop(_retry_budgeted(99 * 10_000))

    assert len(sent) == 1, "a page with no picture is still drawn once"
    assert attempts.best_effort is True
    assert attempts.verdicts[-1].get("retry_budget_stopped") is True


def test_under_the_retry_budget_a_failing_page_is_drawn_again() -> None:
    attempts, sent = _page_loop(_retry_budgeted(50 * 10_000))

    assert len(sent) == 3, "up to the page's own attempt ceiling, as before"
    assert not attempts.verdicts[-1].get("retry_budget_stopped")


def test_a_retry_budget_of_zero_never_stops_a_redraw() -> None:
    _attempts, sent = _page_loop(_retry_budgeted(500 * 10_000, percent=0))

    assert len(sent) == 3


def test_a_page_with_no_picture_is_drawn_even_over_the_retry_budget() -> None:
    """The ceiling stops a redraw, never the page's only picture.

    A first draw the provider's safety system blocked leaves nothing to keep.
    Stopping there would save a page with no art at all — a hole the PDF
    cannot embed — so the book would fail while assembling instead of
    finishing. The budget is about the money redraws cost, and a redraw needs
    a picture to have been drawn already.
    """
    runner = _retry_budgeted(99 * 10_000)
    sent: list[str] = []

    async def draw(prompt: str, mend: Any) -> Any:
        sent.append(prompt)
        if len(sent) == 1:
            raise ProviderError("moderation_blocked: the safety system refused it")
        return _Drawn(grain(seed=2))

    attempts = asyncio.run(
        runner._draw_until_pass(
            book_id="book",
            page_number=5,
            user="-",
            page_title="the kitchen",
            story_line="the mother sends Jack to market",
            cast_note="Only these people may be in the picture: the child.",
            star_in_picture=True,
            plate=None,
            artwork=artwork(plated=False),
            draw=draw,
            prompt_for=prompt_only,
        )
    )

    assert len(sent) == 2, "the page is still drawn past the budget when it has no picture"
    assert attempts.image, "a page must never be kept with no art"
    assert attempts.verdicts[0]["attempt"] == 2


def _book_status(client: TestClient, book_id: str) -> str:
    """Wait for a book to settle, and say how it ended."""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        payload = client.get(f"/api/book/{book_id}").json()
        if payload["status"] in {"complete", "failed"}:
            return str(payload["status"])
        time.sleep(0.05)
    raise AssertionError("the book never settled")


def test_a_book_over_its_retry_budget_still_finishes(
    tmp_path: Path, seeded_template: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Past the ceiling a book loses redraws, not itself: it ends complete.

    The mock's pages do not pass, so every page wants its three tries; with
    the ceiling at the fee and a quarter a picture, the book is over its
    budget before the first page is done. It must still assemble — a visitor
    who paid for a book gets the book, with the best pictures it could make.
    """
    caplog.set_level(logging.WARNING, logger="app.services.book_runner")
    # A real painter and a reviewer that rejects every picture, because a
    # placeholder provider never retries at all — the ceiling has to be
    # reachable for this test to mean anything. One percent of the fee is
    # about a cent, so the book is over it after its first picture.
    settings = isolated_settings(tmp_path, seeded_template).model_copy(
        update={"image_call_cost_usd": 0.01, "book_retry_budget_percent": 1}
    )
    app = create_app(
        settings,
        provider=ABigPainter(),
        reviewer=ScriptedReviewer(default=said(False, 20, ("text_in_image",))),
    )
    with TestClient(app) as client:
        _approved_plates(client.app.state.container.templates, tmp_path)  # type: ignore[attr-defined]
        book_id = _start_a_book(client)
        status = _book_status(client, book_id)

    assert status == "complete"
    # This book's own line, and only one of it. A book another test left
    # rendering in this worker can still log its own budget line here, which is
    # why the id is part of the filter rather than the count.
    stopped = [
        r.getMessage()
        for r in caplog.records
        if "book.retry_budget" in r.getMessage() and book_id in r.getMessage()
    ]
    assert len(stopped) == 1, stopped


def test_the_retry_budget_is_said_once_a_book(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="app.services.book_runner")
    runner = _retry_budgeted(99 * 10_000)
    for _ in range(3):
        _page_loop(runner)

    lines = [r.getMessage() for r in caplog.records if "book.retry_budget" in r.getMessage()]
    assert len(lines) == 1, lines


# --- the alert -----------------------------------------------------------------------


def test_a_book_over_the_alert_line_writes_one_log_line(
    tmp_path: Path, schema_template: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="app.services.book_runner")
    with _site(
        tmp_path,
        schema_template,
        image_call_cost_usd=PICTURE_USD,
        book_cost_alert_cents=100,
    ) as client:
        book_id = _book(client)

    alerts = [r.getMessage() for r in caplog.records if "book.cost_alert" in r.getMessage()]
    assert len(alerts) == 1, alerts
    assert book_id in alerts[0]
    assert "spend_usd=5.5000" in alerts[0]
    assert "alert_usd=1.00" in alerts[0]


def test_the_alert_can_be_turned_off(
    tmp_path: Path, schema_template: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="app.services.book_runner")
    with _site(
        tmp_path,
        schema_template,
        image_call_cost_usd=PICTURE_USD,
        book_cost_alert_cents=0,
    ) as client:
        _book(client)

    assert not [r for r in caplog.records if "book.cost_alert" in r.getMessage()]


def test_the_operator_status_page_names_the_books_over_the_line(
    tmp_path: Path, schema_template: Path
) -> None:
    with _site(
        tmp_path,
        schema_template,
        image_call_cost_usd=PICTURE_USD,
        book_cost_alert_cents=100,
    ) as client:
        book_id = _book(client)
        page = client.get("/admin/status").text

    assert "Books over $1.00" in page
    assert f'href="/admin/books/{book_id}"' in page
    assert "$5.5000 to make" in page


def test_a_book_under_the_line_is_not_named(tmp_path: Path, schema_template: Path) -> None:
    with _site(
        tmp_path,
        schema_template,
        image_call_cost_usd=0.01,  # 22 pictures: 22¢, under the $1 line
        book_cost_alert_cents=100,
    ) as client:
        _book(client)
        page = client.get("/admin/status").text

    assert "Books over $1.00" not in page
    assert "$0.2200" in page  # the spend is still shown in the books table
