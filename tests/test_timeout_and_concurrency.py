"""Wall-clock timeout and the concurrency gate.

These guard the two things that stop a stress test turning into a bill: a
generation that cannot run forever, and a burst that cannot fan out into
unbounded billable requests.
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.container import build_container
from app.main import create_app
from app.services.concurrency import ConcurrencyGate, GateTimeout
from app.services.image_providers.base import GeneratedImage, ImageProvider, ProviderError
from tests.conftest import generate_form
from tests.helpers import multipart, tiny_png_bytes


class SlowProvider(ImageProvider):
    """Sleeps long enough for a short timeout to fire.

    Kept only just longer than the timeout under test: a timed-out worker keeps
    running, and `time.sleep` in a thread cannot be interrupted, so a long delay
    would make every event loop wait for it at shutdown.
    """

    name = "slow"

    def __init__(self, delay: float = 1.5) -> None:
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def generate(self, request):
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        return GeneratedImage(data=tiny_png_bytes(), media_type="image/png", model="slow-v1")


def app_with(settings: Settings, provider: ImageProvider) -> TestClient:
    return TestClient(create_app(settings, provider=provider))


class TestGenerationTimeout:
    def test_slow_generation_returns_504(self, settings: Settings) -> None:
        tight = settings.model_copy(update={"generation_timeout_seconds": 1})
        client = app_with(tight, SlowProvider(delay=1.5))

        response = client.post("/api/generate", **multipart(generate_form()))
        assert response.status_code == 504
        assert response.json()["error"] == "generation_timeout"

    def test_timeout_message_tells_the_client_to_retry(self, settings: Settings) -> None:
        tight = settings.model_copy(update={"generation_timeout_seconds": 1})
        client = app_with(tight, SlowProvider(delay=1.5))

        detail = client.post("/api/generate", **multipart(generate_form())).json()["detail"]
        assert "try again" in detail.lower()

    def test_generation_within_the_budget_succeeds(self, settings: Settings) -> None:
        roomy = settings.model_copy(update={"generation_timeout_seconds": 30})
        client = app_with(roomy, SlowProvider(delay=0.2))

        assert client.post("/api/generate", **multipart(generate_form())).status_code == 201

    def test_a_timed_out_request_is_not_retried(self, settings: Settings) -> None:
        # The point of the ceiling: one user request, at most two provider calls,
        # and no automatic second attempt once the clock runs out.
        tight = settings.model_copy(update={"generation_timeout_seconds": 1})
        provider = SlowProvider(delay=1.5)
        client = app_with(tight, provider)

        client.post("/api/generate", **multipart(generate_form()))
        time.sleep(1.0)  # let any abandoned thread prove it did not re-request
        assert provider.calls <= 2


class TestConcurrencyGate:
    def test_limits_parallel_work(self) -> None:
        gate = ConcurrencyGate(2)
        peak = 0
        running = 0
        observed: list[int] = []
        lock = threading.Lock()

        async def worker() -> None:
            nonlocal peak, running
            async with gate.acquire():
                with lock:
                    running += 1
                    peak = max(peak, running)
                await asyncio.sleep(0.05)
                with lock:
                    running -= 1
                    observed.append(running)

        async def run_all() -> None:
            await asyncio.gather(*(worker() for _ in range(8)))

        asyncio.run(run_all())
        assert peak <= 2
        assert max(observed) <= 1  # every slot was released again

    def test_queueing_eventually_proceeds(self) -> None:
        gate = ConcurrencyGate(1)
        completed: list[int] = []

        async def worker(index: int) -> None:
            async with gate.acquire():
                await asyncio.sleep(0.01)
                completed.append(index)

        async def run_all() -> None:
            await asyncio.gather(*(worker(i) for i in range(5)))

        asyncio.run(run_all())
        assert sorted(completed) == [0, 1, 2, 3, 4]

    def test_waiting_past_the_timeout_raises_gate_timeout(self) -> None:
        gate = ConcurrencyGate(1)

        async def run() -> None:
            async with gate.acquire(), gate.acquire(timeout=0.05):
                pass

        with pytest.raises(GateTimeout, match="slots were busy"):
            asyncio.run(run())

    def test_limit_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            ConcurrencyGate(0)

    def test_survives_a_new_event_loop(self, settings: Settings) -> None:
        # Each TestClient context gets its own loop. A plain asyncio.Semaphore
        # would raise here; the gate must not.
        provider = SlowProvider(delay=0.05)
        client = create_app(settings, provider=provider)

        for _ in range(2):
            with TestClient(client) as one_shot:
                assert (
                    one_shot.post("/api/generate", **multipart(generate_form())).status_code
                    == 201
                )


class TestServerBusy:
    @pytest.fixture
    def saturated_app(self, settings: Settings):
        """One loop, one slot, a slow provider — i.e. a saturated server."""
        tight = settings.model_copy(
            update={"generation_timeout_seconds": 1, "max_concurrent_generations": 1}
        )
        provider = SlowProvider(delay=1.5)
        return create_app(tight, provider=provider), provider

    @staticmethod
    def _concurrently(app, count: int) -> list[httpx.Response]:
        """Fire `count` requests at once, on one event loop.

        An async client on a single loop, rather than threads on TestClient:
        that is how uvicorn runs in production, and it is the only way to get
        genuine contention. TestClient's blocking portal serialises calls, so
        requests would queue behind each other instead of at the gate.
        """

        async def run() -> list[httpx.Response]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                form = multipart(generate_form())
                return await asyncio.gather(
                    *(client.post("/api/generate", **form) for _ in range(count))
                )

        return asyncio.run(run())

    def test_excess_requests_get_429_not_a_billable_call(self, saturated_app) -> None:
        app, provider = saturated_app

        statuses = [r.status_code for r in self._concurrently(app, 3)]

        assert 429 in statuses
        # Only the one admitted request reached the provider: two pipeline
        # calls, not six. Rejected requests cost the user nothing.
        assert provider.calls == 2

    def test_provider_failure_releases_the_slot(self, settings: Settings) -> None:
        class FailingProvider(ImageProvider):
            name = "failing"

            def generate(self, request):
                raise ProviderError("upstream failure")

        tight = settings.model_copy(update={"max_concurrent_generations": 1})
        client = app_with(tight, FailingProvider())

        for _ in range(2):
            response = client.post("/api/generate", **multipart(generate_form()))
            assert response.status_code == 502

    def test_only_one_request_is_admitted(self, saturated_app) -> None:
        app, _ = saturated_app
        statuses = [r.status_code for r in self._concurrently(app, 3)]
        # The admitted request is held by the slow provider, so it times out;
        # the gate must not let a second one in behind it.
        assert statuses.count(429) == 2
        assert statuses.count(504) == 1

    def test_busy_response_suggests_backing_off(self, saturated_app) -> None:
        app, _ = saturated_app
        payloads = [r.json() for r in self._concurrently(app, 3)]

        busy = [item for item in payloads if item.get("error") == "server_busy"]
        assert busy
        assert "retry" in str(busy[0]["detail"]).lower()

    def test_a_timed_out_request_keeps_holding_its_slot(self, saturated_app) -> None:
        # Regression guard: releasing the slot at timeout time would tell the
        # next request the server was idle while it was still working, letting
        # two generations run against a limit of one.
        app, provider = saturated_app

        responses = self._concurrently(app, 2)
        assert {r.status_code for r in responses} == {504, 429}
        assert provider.calls == 2


class TestHealthReportsConcurrency:
    def test_healthz_exposes_the_limit(self, settings: Settings) -> None:
        bounded = settings.model_copy(update={"max_concurrent_generations": 3})
        payload = app_with(bounded, SlowProvider(delay=0.01)).get("/healthz").json()
        assert payload["provider"]["max_concurrent_generations"] == 3


class TestContainerWiring:
    def test_gate_limit_comes_from_settings(self, settings: Settings) -> None:
        bounded = settings.model_copy(update={"max_concurrent_generations": 7})
        assert build_container(bounded).gate.limit == 7
