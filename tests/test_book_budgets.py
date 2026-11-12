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
from app.services.user_store import User
from app.sessions import SESSION_COOKIE
from tests.helpers import JACK_KEY, RecordingProvider, make_photo_bytes
from tests.test_review_loop import ScriptedReviewer, artwork, grain, runner_with, said

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
