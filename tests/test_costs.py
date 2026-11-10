"""What a book costs to make: every model call logged, priced and summed.

The claims, each one a thing the cost panel would get wrong without it:

* Every billable call of a book — its portrait, cover, pages, redraws and
  quality checks — lands in the ledger under that book, filed by what it was
  for, so a redraw is counted as a redraw and not hidden in the page count.
* The price of an image follows the configured provider: Agnes and the mock are
  free, OpenAI is priced from its published table, an override wins, and an
  unknown backend is *unpriced*, never silently free.
* A failed call is logged (as a failure, at no cost) and still raised.
* The operator status page shows the average cost of a finished book next to
  the $0.99 price; the visitor's wallet shows the price but none of the API
  spend.
* The attempt record reaches the reader: the poll and the book page carry how
  many tries each picture took.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import ApiCallRow
from app.main import create_app
from app.services.costs import (
    CallPrice,
    CostLedger,
    CostScope,
    MeteredProvider,
    TokenRates,
    cost_scope,
    image_call_price,
)
from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
    ProviderError,
    TokenUsage,
)
from tests.helpers import RecordingProvider, isolated_settings, make_photo_bytes
from tests.test_plate_import_and_edit import _approved_plates, _start_a_book
from tests.test_review_loop import PLAN_COUNT, ABigPainter, ScriptedReviewer, said
from tests.test_wallet_routes import SUB, FakeStripe, paid_session, wallet_app

IMAGE_USD = 0.01
REVIEW_USD = 0.001


def priced(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"image_call_cost_usd": IMAGE_USD, "review_call_cost_usd": REVIEW_USD}
    )


def wait_for(client: TestClient, book_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        payload = client.get(f"/api/book/{book_id}").json()
        if payload["status"] in {"complete", "failed"}:
            return dict(payload)
        time.sleep(0.05)
    raise AssertionError("the book never settled")


def calls_of(client: TestClient) -> list[ApiCallRow]:
    engine = client.app.state.container.engine  # type: ignore[attr-defined]
    with engine.connect() as conn:
        return list(conn.execute(select(ApiCallRow)).all())  # type: ignore[arg-type]


class TestABookIsCostedCallByCall:
    """A reviewed book with one redraw, through the real start route."""

    @pytest.fixture(scope="class")
    def book(self, tmp_path_factory: pytest.TempPathFactory, seeded_template: Path):
        """One costed book for the class: every test here only reads it."""
        tmp_path = tmp_path_factory.mktemp("costed")
        settings = isolated_settings(tmp_path, seeded_template)
        reviewer = ScriptedReviewer(
            answers=[said(False, 20, ("text_in_image",))], default=said(True, 95)
        )
        app = create_app(priced(settings), provider=ABigPainter(), reviewer=reviewer)
        with TestClient(app) as client:
            _approved_plates(client.app.state.container.templates, tmp_path)  # type: ignore[attr-defined]
            book_id = _start_a_book(client)
            payload = wait_for(client, book_id)
            assert payload["status"] == "complete", payload.get("error")
            yield client, book_id, payload, reviewer

    def test_every_call_is_filed_under_the_book_by_what_it_was_for(self, book) -> None:
        client, book_id, _, reviewer = book
        rows = calls_of(client)
        assert {row.book_id for row in rows} == {book_id}, "the portrait is adopted too"
        by_category: dict[str, int] = {}
        for row in rows:
            by_category[row.category] = by_category.get(row.category, 0) + 1
        # One portrait, one cover, the story pages, one redraw, and one review
        # per picture drawn (the redraw included).
        assert by_category == {
            "portrait": 1,
            "cover": 1,
            "page": PLAN_COUNT - 1,
            "retry": 1,
            "review": reviewer.calls,
        }
        assert reviewer.calls == PLAN_COUNT + 1
        retry = next(row for row in rows if row.category == "retry")
        assert retry.attempt == 2 and retry.page_number is not None

    def test_the_summary_averages_what_the_book_actually_cost(self, book) -> None:
        client, book_id, _, reviewer = book
        ledger: CostLedger = client.app.state.container.costs  # type: ignore[attr-defined]
        images = 1 + PLAN_COUNT + 1  # portrait, every picture, the redraw
        expected = round(images * IMAGE_USD * 1e6) + reviewer.calls * round(REVIEW_USD * 1e6)
        summary = ledger.summary()
        assert summary.books_measured == 1
        assert summary.average_book_microusd == expected
        assert ledger.book_costs_microusd([book_id]) == {book_id: expected}
        retry = next(item for item in summary.per_book if item.category == "retry")
        assert retry.calls_per_book == 1
        assert summary.unpriced_calls == 0

    def test_the_poll_says_how_many_tries_each_picture_took(self, book) -> None:
        client, book_id, payload, _ = book
        done = [page for page in payload["pages"] if page["status"] == "done"]
        assert all(page["attempts"] >= 1 for page in done)
        retried = [page for page in done if page["attempts"] == 2]
        assert len(retried) == 1
        assert retried[0]["chosen_attempt"] == 2
        assert retried[0]["best_effort"] is False

        html = client.get(f"/books/{book_id}").text
        assert 'data-attempts="2"' in html
        assert 'data-max-attempts="3"' in html


class TestPrices:
    def test_agnes_and_the_mock_are_free(self, settings: Settings) -> None:
        agnes = settings.model_copy(update={"image_provider": "agnes"})
        assert image_call_price(agnes).usd == 0.0
        assert image_call_price(settings).usd == 0.0

    def test_openai_is_priced_from_the_tokens_each_call_reports(
        self, settings: Settings
    ) -> None:
        openai = settings.model_copy(
            update={
                "image_provider": "openai",
                "openai_model": "gpt-image-2.5-flare",
                "openai_quality": "low",
            }
        )
        price = image_call_price(openai)
        assert price.usd is None
        assert price.model == "gpt-image-2.5-flare (low)"
        # 100 text in at $5/1M, 3,000 image in at $8/1M, 196 out at $30/1M.
        usage = TokenUsage(text_input_tokens=100, image_input_tokens=3000, output_tokens=196)
        assert price.microusd(1, usage) == 500 + 24_000 + 5_880
        # The reference images going in, not the picture out, are most of it.
        assert price.microusd(1, None) is None

    def test_quality_does_not_change_the_rate_only_the_tokens(
        self, settings: Settings
    ) -> None:
        high = settings.model_copy(
            update={"image_provider": "openai", "openai_quality": "high"}
        )
        assert (
            image_call_price(high).tokens
            == image_call_price(high.model_copy(update={"openai_quality": "low"})).tokens
        )

    def test_an_unknown_openai_model_is_unpriced_until_configured(
        self, settings: Settings
    ) -> None:
        unknown = settings.model_copy(
            update={"image_provider": "openai", "openai_model": "gpt-image-9"}
        )
        usage = TokenUsage(text_input_tokens=1, image_input_tokens=1, output_tokens=1)
        assert image_call_price(unknown).microusd(1, usage) is None
        overridden = unknown.model_copy(update={"image_call_cost_usd": 0.04})
        assert image_call_price(overridden).microusd(2, usage) == 80_000

    def test_a_blank_override_in_the_environment_means_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IMAGE_CALL_COST_USD", "")
        assert Settings(_env_file=None).image_call_cost_usd is None  # type: ignore[call-arg]


class _Failing(ImageProvider):
    name = "failing"

    def generate(self, request: ImageRequest) -> GeneratedImage:
        raise ProviderError("the model is down")


class TestTheMeter:
    def test_a_failed_call_is_logged_at_no_cost_and_still_raised(self, engine) -> None:
        ledger = CostLedger(engine)
        metered = MeteredProvider(
            _Failing(),
            ledger,
            image_call_price(Settings(_env_file=None)),  # type: ignore[call-arg]
        )
        with (
            cost_scope(purpose="book", book_id="b1", page_number=3, attempt=1),
            pytest.raises(ProviderError),
        ):
            metered.generate(ImageRequest(prompt="x"))
        with engine.connect() as conn:
            (row,) = conn.execute(select(ApiCallRow)).all()
        assert (row.ok, row.cost_microusd, row.category, row.book_id) == (
            False,
            0,
            "page",
            "b1",
        )

    def test_a_token_billed_call_stores_its_tokens_and_their_price(self, engine) -> None:
        usage = TokenUsage(text_input_tokens=200, image_input_tokens=2500, output_tokens=196)

        class _Billed(ImageProvider):
            name = "openai"

            def generate(self, request: ImageRequest) -> GeneratedImage:
                return GeneratedImage(data=b"png", usage=usage)

            def generate_candidates(
                self, request: ImageRequest, count: int
            ) -> list[GeneratedImage]:
                return [GeneratedImage(data=b"png", usage=usage) for _ in range(count)]

        price = CallPrice(usd=None, model="m", tokens=TokenRates(5.0, 8.0, 30.0))
        metered = MeteredProvider(_Billed(), CostLedger(engine), price)
        with cost_scope(purpose="book", book_id="b1", page_number=2, attempt=1):
            metered.generate(ImageRequest(prompt="x"))
            # Three candidates from one call are one bill, not three.
            metered.generate_candidates(ImageRequest(prompt="x"), 3)
        with engine.connect() as conn:
            rows = conn.execute(select(ApiCallRow)).all()
        expected = 200 * 5 + 2500 * 8 + 196 * 30
        assert [row.cost_microusd for row in rows] == [expected, expected]
        assert [
            (r.text_input_tokens, r.image_input_tokens, r.output_tokens) for r in rows
        ] == [
            (200, 2500, 196),
            (200, 2500, 196),
        ]

    def test_an_unpriced_call_is_counted_as_unpriced_not_free(self, engine) -> None:
        ledger = CostLedger(engine)
        ledger.record(
            kind="image",
            provider="gemini",
            model="m",
            images=1,
            ok=True,
            cost_microusd=None,
            duration_ms=1,
            scope=CostScope(purpose="plate"),
        )
        summary = ledger.summary()
        assert summary.unpriced_calls == 1
        assert summary.books_measured == 0


def test_the_operator_sees_the_cost_of_a_book_next_to_its_price(settings: Settings) -> None:
    stripe = FakeStripe()
    with wallet_app(priced(settings), RecordingProvider(), stripe, operator_sub=SUB) as (
        client,
        _,
    ):
        client.get(f"/wallet/return?session_id={paid_session(stripe, 1000)}")
        portrait = client.post(
            "/api/cartoonify",
            files={"photo": ("me.png", make_photo_bytes(), "image/png")},
            data={"story": "jack_and_the_beanstalk", "photo_gender": "boy"},
        )
        start = client.post(
            "/api/book/start",
            data={
                "story": "jack_and_the_beanstalk",
                "photo_gender": "boy",
                "generation_id": portrait.json()["generation_id"],
            },
        )
        assert start.status_code == 201, start.text
        assert wait_for(client, start.json()["book_id"])["status"] == "complete"

        ledger: CostLedger = client.app.state.container.costs  # type: ignore[attr-defined]
        average = ledger.summary().average_book_microusd
        assert average > 0
        figure = f"${average / 1e6:.4f}"

        wallet = client.get("/wallet").text
        assert "What a book costs to make" not in wallet
        assert "average API spend" not in wallet
        assert "This book cost" not in wallet

        page = client.get("/admin/status").text
        assert "What a book costs to make" in page
        assert "average API spend per finished book" in page
        assert figure in page
        assert "$0.99" in page
