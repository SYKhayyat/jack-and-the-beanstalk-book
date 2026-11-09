#!/usr/bin/env python3
"""Time a real book: how long it takes and what it costs in prompt characters.

T11 asks for numbers, not impressions: a whole book end to end, the spread of
per-page time, and what the plate edit pass actually saved. This drives the real
HTTP API against a running server, so it measures the thing a parent waits for,
not a unit of code.

    # free: no API key, no cost, no real pictures
    IMAGE_PROVIDER=mock uvicorn app.main:create_app --factory --port 8000
    python scripts/time_book.py --url http://localhost:8000

    # real: costs one portrait + 21 pages per book
    python scripts/time_book.py --url https://your-app.onrender.com \\
        --yes-i-am-using-a-paid-key

Two modes, because they answer different questions:

* `--books 1` — how long one book takes, and the p50/p95 of its pages.
* `--books 2 --concurrent` — whether two books at once take twice as long. If
  they do, the queue is serial; if they overlap, the wall clock says so.

Where the numbers come from: pages report themselves done in the status payload,
so per-page time is measured from poll observations (floor 0.5s, the poll
interval, so treat a page under that as "under the poll interval"). Attempts and
prompt size come from `/book-media/{id}/prompts.json`, which holds one entry per
attempt — that is the honest record of what was sent and how many tries it took.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import statistics
import sys
import time
from dataclasses import dataclass, field
from itertools import pairwise

try:
    import httpx
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit("Missing dependencies. Run: pip install -r requirements-dev.txt")

POLL_SECONDS = 0.5
#: What one image call costs at the cheap tier, for the estimate at the end.
COST_PER_CALL_USD = 0.00588


def a_photo(seed: int, size: int = 512) -> bytes:
    """A synthetic upload that is different every run, so nothing is cached."""
    image = Image.new("RGB", (size, size), "#ffffff")
    pixels = image.load()
    assert pixels is not None
    for x in range(0, size, 4):
        for y in range(size):
            shade = (x * 7 + y * 13 + seed * 31) % 256
            pixels[x, y] = (shade, (shade * 3) % 256, (shade * 11) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@dataclass
class Book:
    """One book's run: its wall clock, its pages and what the prompts say."""

    index: int
    started: float = 0.0
    finished: float = 0.0
    portrait_seconds: float = 0.0
    pages_done_at: dict[int, float] = field(default_factory=dict)
    assembling_seconds: float = 0.0
    status: str = ""
    error: str = ""
    book_id: str = ""
    prompts: list[dict[str, str]] = field(default_factory=list)

    @property
    def total_seconds(self) -> float:
        return self.finished - self.started

    @property
    def page_count(self) -> int:
        return len(self.pages_done_at)

    def prompts_by_page(self) -> dict[str, list[dict[str, str]]]:
        grouped: dict[str, list[dict[str, str]]] = {}
        for entry in self.prompts:
            grouped.setdefault(str(entry.get("page")), []).append(entry)
        return grouped


async def sign_in(client: httpx.AsyncClient, url: str, password: str) -> None:
    """Get past the shared-password gate, when there is one.

    An open deployment (or one behind Google) needs nothing here: the app either
    lets the request through or refuses it, and refusing is a result worth
    seeing rather than working around.
    """
    if not password:
        return
    response = await client.post(f"{url}/login", data={"password": password})
    if response.status_code >= 400:
        raise SystemExit(
            f"could not sign in with that password ({response.status_code}). "
            "A deployment behind Google needs a session cookie, not a password."
        )


async def cartoonify(client: httpx.AsyncClient, url: str, seed: int) -> str:
    """Make one portrait and return its generation id."""
    response = await client.post(
        f"{url}/api/cartoonify",
        files={"photo": ("me.png", a_photo(seed), "image/png")},
        data={"story": "jack_and_the_beanstalk"},
    )
    response.raise_for_status()
    return str(response.json()["generation_id"])


async def wait_for_portrait(client: httpx.AsyncClient, url: str, generation_id: str) -> None:
    """Poll until the cartoon exists, so the book's own timing starts clean.

    Readiness is the picture itself answering 200 on `/media/{id}/cartoon`, which
    is exactly what the browser waits for — not a status flag beside it.
    """
    for _ in range(1200):
        response = await client.get(f"{url}/media/{generation_id}/cartoon")
        if response.status_code == 200:
            return
        await asyncio.sleep(POLL_SECONDS)
    raise TimeoutError("the portrait never became ready")


async def run_book(client: httpx.AsyncClient, url: str, book: Book, child_name: str) -> None:
    """Start one book and follow it to the end, recording what it reports."""
    portrait_started = time.perf_counter()
    generation_id = await cartoonify(client, url, book.index)
    await wait_for_portrait(client, url, generation_id)
    book.portrait_seconds = time.perf_counter() - portrait_started

    book.started = time.perf_counter()
    response = await client.post(
        f"{url}/api/book/start",
        data={
            "story": "jack_and_the_beanstalk",
            "generation_id": generation_id,
            "child_name": child_name,
        },
    )
    response.raise_for_status()
    book.book_id = str(response.json()["book_id"])

    all_done_at: float | None = None
    for _ in range(7200):  # 30 minutes at the 0.5s poll
        payload = (await client.get(f"{url}/api/book/{book.book_id}")).json()
        book.status = str(payload.get("status"))
        if book.status in {"complete", "failed"}:
            break
        pages = payload.get("pages") or []
        for entry in pages:
            number = int(entry.get("number", 0))
            if entry.get("status") == "done" and number not in book.pages_done_at:
                book.pages_done_at[number] = time.perf_counter()
        if pages and all(entry.get("status") == "done" for entry in pages):
            all_done_at = all_done_at or time.perf_counter()
            book.assembling_seconds = time.perf_counter() - all_done_at
        await asyncio.sleep(POLL_SECONDS)

    book.finished = time.perf_counter()
    book.error = str(payload.get("error") or "")
    if book.status == "complete":
        book.assembling_seconds = max(0.0, book.finished - (all_done_at or book.finished))
        await load_prompts(client, url, book)


async def load_prompts(client: httpx.AsyncClient, url: str, book: Book) -> None:
    """The prompt record: one entry per attempt, so attempts and size are visible."""
    response = await client.get(f"{url}/book-media/{book.book_id}/prompts.json")
    if response.status_code != 200:
        return
    book.prompts = list(response.json().get("prompts") or [])


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def report(books: list[Book], args: argparse.Namespace) -> int:
    finished = [book for book in books if book.status == "complete"]
    print("\n── per book " + "─" * 59)
    for book in books:
        if book.status == "complete":
            print(
                f"  book {book.index}  {book.total_seconds:6.1f}s to complete"
                f"  ({book.page_count} pictures, {book.assembling_seconds:.1f}s"
                f" putting it together)"
            )
        else:
            print(f"  book {book.index}  FAILED after {book.total_seconds:.1f}s: {book.error}")

    if not finished:
        print("\nFAIL: no book completed, so there are no timings to report.")
        return 1

    # Per-page time from the poll observations. A page finished between two
    # polls reads as one interval, so this is an upper bound, not a floor.
    gaps: list[float] = []
    widest_group = 0
    for book in finished:
        ordered = sorted(book.pages_done_at.values())
        gaps.extend(later - earlier for earlier, later in pairwise(ordered))
        # How many pictures landed inside one poll: the direct evidence that the
        # pages are drawn in parallel rather than one after another.
        ticks: dict[float, int] = {}
        for moment in book.pages_done_at.values():
            key = round(moment / POLL_SECONDS)
            ticks[key] = ticks.get(key, 0) + 1
        widest_group = max([widest_group, *ticks.values()])
    if gaps:
        print("\n── a page " + "─" * 59)
        print(
            f"  p50 {percentile(gaps, 0.5):.2f}s   p95 {percentile(gaps, 0.95):.2f}s"
            f"   max {max(gaps):.2f}s   mean {statistics.fmean(gaps):.2f}s"
        )
        print(f"  (measured between polls every {POLL_SECONDS:g}s)")
        print(f"  up to {widest_group} pictures finished within one poll of each other")
        if percentile(gaps, 0.5) == 0:
            print("  a zero p50 means pictures landed together: this run is faster")
            print("  than the poll interval, so the spread is too coarse to read")

    totals = [book.total_seconds for book in finished]
    wall = max(book.finished for book in finished) - min(book.started for book in books)
    print("\n── the run " + "─" * 59)
    print(f"  {len(finished)} of {len(books)} books completed in {wall:.1f}s of wall clock")
    print(f"  one book: {statistics.fmean(totals):.1f}s")
    if len(finished) > 1 and args.concurrent:
        print(
            f"  {len(finished)} at once: {wall:.1f}s  "
            f"({wall / statistics.fmean(totals):.2f}x one book's time)"
        )

    attempts: dict[str, int] = {}
    characters = 0
    for book in finished:
        for page, entries in book.prompts_by_page().items():
            attempts[page] = len(entries)
            characters += sum(len(str(entry.get("prompt", ""))) for entry in entries)
    retried = {page: count for page, count in attempts.items() if count > 1}
    print("\n── what it sent " + "─" * 54)
    print(f"  prompt characters {characters:,} across {sum(attempts.values())} attempts")
    print(f"  retries           {len(retried)} page(s) needed more than one try")
    if retried:
        for page, count in sorted(retried.items()):
            print(f"    page {page}: {count} attempts")

    calls = len(finished) * (1 + sum(attempts.values()))
    if args.paid_key:
        print(f"\n  estimated spend: ${calls * COST_PER_CALL_USD:.2f} ({calls} image calls)")
    else:
        print("\n  cost: $0.00 — mock mode makes no API calls.")
    print("─" * 70)
    return 0 if len(finished) == len(books) else 1


async def run(args: argparse.Namespace) -> int:
    books = [Book(index=index) for index in range(args.books)]
    limit = asyncio.Semaphore(args.books if args.concurrent else 1)
    async with httpx.AsyncClient(timeout=args.timeout, follow_redirects=True) as client:
        await sign_in(client, args.url, args.password)

        async def one(book: Book) -> None:
            async with limit:
                await run_book(client, args.url, book, f"Reader {book.index + 1}")

        await asyncio.gather(*(one(book) for book in books))
    return report(books, args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--books", type=int, default=1, help="how many books to make")
    parser.add_argument(
        "--concurrent",
        action="store_true",
        help="make the books at the same time, to see whether one waits for another",
    )
    parser.add_argument(
        "--password",
        default="",
        help="the shared password, when the deployment asks for one",
    )
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument(
        "--yes-i-am-using-a-paid-key",
        dest="paid_key",
        action="store_true",
        help="acknowledge that a real provider will bill for these books",
    )
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse_args())))
