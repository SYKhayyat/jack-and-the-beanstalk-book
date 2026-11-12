"""A book rendering must not stall the server (docs/plan-render-stall-and-resume.md).

Run 2 on the live site lost a book because the health check went unanswered
for twenty seconds while its pages were being made. A rendering book is now
image work on the runner's own small pool and model calls on threads; the
event loop only routes. This test renders a whole book with a slow provider
and full-size pictures — the real Pillow work: decode, downscale, re-encode
for every page and every review — and checks that the loop kept up the whole
time: the health check and the poll answered promptly throughout, and the
loop's own lag watcher (sampling far faster than in production) never recorded
a *stall*. It records lateness every time a thread holds the GIL through a
decode or a resize, which is the price of moving the work off the loop rather
than a symptom of B1, so the test draws the line at seconds.

Then it asks for every page's 1600 px copy at once, which is the one size the
reader makes for itself on first view (the 240 and 800 px copies are made as
each page is saved), under the same watch.
"""

from __future__ import annotations

import io
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main as main_module
from app.config import Settings
from app.main import create_app
from app.services.image_providers.base import GeneratedImage, ImageRequest
from app.services.vitals import watch_loop
from tests.helpers import RecordingProvider, make_photo_bytes

#: How late the loop may run before the watcher logs it; the production
#: watcher's own threshold.
LAG_LIMIT_SECONDS = 0.25
#: How late it may run before this test calls it a stall. A logged lag of a few
#: hundred milliseconds is Pillow holding the GIL while a thread decodes or
#: resizes a full-size picture, and an eight-worker suite makes those slower
#: still; the loop was waiting its turn, not blocked. What B1 was: 20-50 s with
#: every request behind it. The sharp end of the assertion is `/healthz`, which
#: may not pass HEALTH_LIMIT_SECONDS at any point.
LAG_STALL_SECONDS = 5.0
#: The slowest the health check may answer while the book renders: async and
#: database-free, it waits on nothing but the loop. A tenth of what Render's
#: check waited before it gave up.
HEALTH_LIMIT_SECONDS = 0.5
#: The slowest the poll may answer. It reads the database, and the test's
#: SQLite file makes a reader wait out each of the runner's status writes
#: (Postgres, in production, does not), so its bound is looser.
POLL_LIMIT_SECONDS = 5.0


def test_no_jpeg_is_encoded_in_one_gil_holding_call() -> None:
    """optimize=/progressive= make Pillow hold the GIL for the whole encode.

    At ~270 ms a page that froze the loop through every book's PDF assembly;
    a plain baseline encode works in chunks and lets the loop run.
    """
    from pathlib import Path

    offenders = [
        f"{path}:{number}"
        for path in Path("app").rglob("*.py")
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if "optimize=True" in line or "progressive=True" in line
    ]
    assert not offenders, offenders


def _picture() -> bytes:
    """A noisy 1024x1536 PNG: as costly to decode and shrink as a real page."""
    rng = random.Random(7)
    image = Image.frombytes("RGB", (1024, 1536), rng.randbytes(1024 * 1536 * 3))
    out = io.BytesIO()
    image.save(out, format="PNG", compress_level=1)
    return out.getvalue()


def _slowest_health(client: TestClient, samples: int = 20) -> float:
    """The worst round trip `/healthz` manages on this machine right now.

    Its only job is to answer; with nothing else running it takes a couple of
    milliseconds. Two test suites sharing eight cores is another matter, and
    the render must then be judged against what the host can do at all.
    """
    worst = 0.0
    for _ in range(samples):
        asked = time.monotonic()
        assert client.get("/healthz").status_code == 200
        worst = max(worst, time.monotonic() - asked)
        time.sleep(0.01)
    return worst


class _SlowProvider(RecordingProvider):
    """Takes a while per picture, like a real model, and returns a full-size one."""

    name = "slow"

    def __init__(self) -> None:
        super().__init__(_picture())

    def generate(self, request: ImageRequest) -> GeneratedImage:
        time.sleep(0.15)
        return super().generate(request)


def test_a_rendering_book_leaves_the_loop_free(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="app.services.vitals")
    monkeypatch.setattr(
        main_module,
        "watch_loop",
        partial(watch_loop, interval=0.05, threshold=LAG_LIMIT_SECONDS),
    )
    app = create_app(settings, provider=_SlowProvider())
    with TestClient(app) as client:
        # What the health check costs here with no book rendering: ~2 ms on an
        # idle machine, where the absolute bound below is what applies.
        baseline = _slowest_health(client)
        portrait = client.post(
            "/api/cartoonify",
            files={"photo": ("me.png", make_photo_bytes(), "image/png")},
            data={"story": "jack_and_the_beanstalk", "photo_gender": "boy"},
        )
        assert portrait.status_code == 201, portrait.text
        start = client.post(
            "/api/book/start",
            data={
                "story": "jack_and_the_beanstalk",
                "generation_id": portrait.json()["generation_id"],
            },
        )
        assert start.status_code == 201, start.text
        book_id = start.json()["book_id"]
        # The watch starts here. It has been running through app startup and
        # the studio's portrait step as well, and in a full parallel suite
        # (eight workers on eight cores, every one of them doing image work) a
        # single `loop.lag` was logged there — `ms=1313`, once, while `/healthz`
        # never passed 8 ms. Neither of those is the render stall this test is
        # about (AUDIT.md S8 files the observation), so the watch judges the
        # render and the copies, where a regression would show.
        caplog.clear()

        slowest = {"/healthz": 0.0, "poll": 0.0, "copies": 0.0, "healthz": 0}
        polls = 0
        status: dict = {}
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            for name, path in (("/healthz", "/healthz"), ("poll", f"/api/book/{book_id}")):
                asked = time.monotonic()
                response = client.get(path)
                slowest[name] = max(slowest[name], time.monotonic() - asked)
                assert response.status_code == 200, (path, response.text)
            status = response.json()
            polls += 1
            if status["status"] in {"complete", "failed"}:
                break
            time.sleep(0.05)

        # Now the reader's first large view: nothing is stored at 1600 px, so
        # every page is a decode and a LANCZOS resize of a full-size picture,
        # twenty-one of them asked for at once (B1).
        urls = [
            f"/book-media/{book_id}/pages/{page['number']}?w=1600"
            for page in status["pages"]
            if page["status"] == "done"
        ]
        assert len(urls) == 21, urls
        made = threading.Event()

        def watch_health() -> None:
            while not made.is_set():
                asked = time.monotonic()
                answered = client.get("/healthz")
                slowest["/healthz"] = max(slowest["/healthz"], time.monotonic() - asked)
                slowest["healthz"] += 1
                assert answered.status_code == 200, answered.text
                time.sleep(0.01)

        watcher = threading.Thread(target=watch_health)
        watcher.start()
        try:
            with ThreadPoolExecutor(max_workers=len(urls)) as pool:
                asked = time.monotonic()
                copies = list(pool.map(client.get, urls))
                slowest["copies"] = time.monotonic() - asked
        finally:
            made.set()
            watcher.join()

    assert status["status"] == "complete", status.get("error")
    assert len(status["pages"]) == 21  # the cover and twenty story pages
    assert polls > 10, "the book finished before the server was watched"
    # A quarter of a second is the budget on a machine that has spare CPU; on
    # one that does not, the render has to be no worse than the host already is.
    assert slowest["/healthz"] < max(HEALTH_LIMIT_SECONDS, baseline * 3), {
        "baseline": baseline,
        **slowest,
    }
    assert slowest["poll"] < POLL_LIMIT_SECONDS, slowest
    # The copies were really made (the loop was watched all the while):
    assert slowest["healthz"] > 5, slowest
    assert [(copy.status_code, copy.headers["content-type"]) for copy in copies] == [
        (200, "image/webp")
    ] * 21
    lags = [
        float(record.args[0])  # the watcher's own milliseconds, unformatted
        for record in caplog.records
        if record.args and "loop.lag" in record.getMessage()
    ]
    assert max(lags, default=0.0) < LAG_STALL_SECONDS * 1000, lags
