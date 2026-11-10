"""The images-per-minute pacer and the OpenAI provider's 429 handling."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.image_providers.openai_provider import OpenAIImageProvider, _rate_limit_delay
from app.services.image_providers.pacer import ImagePacer


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_within_the_limit_never_waits() -> None:
    clock = Clock()
    pacer = ImagePacer(3, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        assert pacer.acquire() == 0
    assert clock.slept == []


def test_over_the_limit_waits_for_the_window_to_roll() -> None:
    clock = Clock()
    pacer = ImagePacer(2, clock=clock, sleep=clock.sleep)
    pacer.acquire()
    clock.now += 10
    pacer.acquire()
    waited = pacer.acquire()  # the first start ages out 50s from now
    assert waited == pytest.approx(50, abs=0.1)


def test_a_batch_waits_for_enough_room() -> None:
    clock = Clock()
    pacer = ImagePacer(4, clock=clock, sleep=clock.sleep)
    pacer.acquire(4)
    assert pacer.acquire(2) == pytest.approx(60, abs=0.1)


def test_a_batch_larger_than_the_limit_is_let_through() -> None:
    clock = Clock()
    pacer = ImagePacer(2, clock=clock, sleep=clock.sleep)
    assert pacer.acquire(5) == 0


def test_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ImagePacer(0)


class RateLimited(Exception):
    status_code = 429

    def __init__(self, retry_after: str | None = None, code: str | None = None) -> None:
        super().__init__("rate limited")
        self.code = code

        class Resp:
            headers = {"retry-after": retry_after} if retry_after else {}

        self.response = Resp()


def test_429_uses_retry_after_and_exponential_fallback() -> None:
    assert _rate_limit_delay(RateLimited("7"), 0) == 7
    assert _rate_limit_delay(RateLimited(), 0) == 5
    assert _rate_limit_delay(RateLimited(), 2) == 20


def test_out_of_quota_and_other_errors_are_not_retried() -> None:
    assert _rate_limit_delay(RateLimited(code="insufficient_quota"), 0) is None
    assert _rate_limit_delay(RuntimeError("boom"), 0) is None


class FlakyImages:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def edit(self, **_: Any) -> object:
        self.calls += 1
        if self.calls <= self.failures:
            raise RateLimited("1")
        return type("R", (), {"data": []})()


class Client:
    def __init__(self, images: FlakyImages) -> None:
        self.images = images


def _provider(images: FlakyImages, monkeypatch: pytest.MonkeyPatch) -> OpenAIImageProvider:
    monkeypatch.setattr("app.services.image_providers.openai_provider.time.sleep", lambda _: None)
    provider = OpenAIImageProvider(api_key="k")
    provider._client = Client(images)  # type: ignore[assignment]
    return provider


def test_a_429_is_resent_and_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_openai_provider import req

    images = FlakyImages(failures=2)
    provider = _provider(images, monkeypatch)
    provider._call(req(), n=1)
    assert images.calls == 3


def test_a_persistent_429_eventually_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.image_providers.base import ProviderError
    from tests.test_openai_provider import req

    images = FlakyImages(failures=99)
    provider = _provider(images, monkeypatch)
    with pytest.raises(ProviderError):
        provider._call(req(), n=1)
    assert images.calls == 4  # the first try plus three re-sends
