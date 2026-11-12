"""W7: the thresholds the operator hears about without opening the page.

`/admin/status` has the numbers; these are the few that are worth a log line at
boot and on the hourly sweep, and a card at the top of that page. Blunt and few
on purpose: an alert nobody believes is worse than none.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.engine import Engine

from app.main import check_alerts
from app.services.alerts import (
    REVIEW_FLOOR,
    REVIEW_SAMPLE,
    cost_alert,
    log_alerts,
    operator_alerts,
    review_alert,
)
from app.services.costs import format_microusd


def test_a_low_pass_rate_speaks_only_once_the_sample_means_something() -> None:
    """One shaky book is not news; two is a pattern (W7)."""
    assert review_alert(pictures=REVIEW_SAMPLE - 1, passed_first_try=0) is None

    alert = review_alert(pictures=REVIEW_SAMPLE, passed_first_try=1)
    assert alert is not None
    assert alert.code == "review_pass_rate"
    assert f"floor {REVIEW_FLOOR:.0%}" in alert.message

    assert review_alert(pictures=100, passed_first_try=90) is None


def test_a_book_that_costs_more_than_it_sells_speaks() -> None:
    """The price is the threshold, because that is the one that matters."""
    assert cost_alert(average_microusd=99 * 10_000, price_cents=99, books=3) is None

    alert = cost_alert(average_microusd=160 * 10_000, price_cents=99, books=3)
    assert alert is not None
    assert alert.code == "cost_per_book"
    assert format_microusd(160 * 10_000) in alert.message
    assert "$0.99" in alert.message

    # Nothing to average yet, and money off: no claim either way.
    assert cost_alert(average_microusd=160 * 10_000, price_cents=99, books=0) is None
    assert cost_alert(average_microusd=160 * 10_000, price_cents=0, books=3) is None


def test_a_fresh_deployment_is_quiet(engine: Engine) -> None:
    """No books, no reviews, no money: nothing to say."""
    assert operator_alerts(engine, price_cents=99, wallet=object()) == []


def test_the_log_gets_one_line_per_alert(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="app.services.alerts")
    log_alerts([review_alert(pictures=REVIEW_SAMPLE, passed_first_try=0)])

    lines = [r.getMessage() for r in caplog.records if "operator.alert" in r.getMessage()]
    assert len(lines) == 1
    assert "code=review_pass_rate" in lines[0]


def test_the_boot_and_the_sweep_run_the_same_thresholds(
    container: object, caplog: pytest.LogCaptureFixture
) -> None:
    """`check_alerts` is the one caller both paths use, so they cannot disagree."""
    caplog.set_level(logging.WARNING, logger="app.services.alerts")

    assert check_alerts(container) == 0  # type: ignore[arg-type]
    assert not [r for r in caplog.records if "operator.alert" in r.getMessage()]
