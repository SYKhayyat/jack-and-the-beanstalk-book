#!/usr/bin/env python3
"""Load-test the app without spending anything.

Drives `POST /api/cartoonify` concurrently against any base URL and reports
throughput, latency percentiles and the status-code breakdown. Point it at a
local mock-mode instance for unlimited free load, or at a deployed URL for a
light sanity check.

    # terminal 1 — free, unlimited, no API key
    IMAGE_PROVIDER=mock uvicorn app.main:create_app --factory --port 8000

    # terminal 2
    python scripts/stress_test.py --url http://localhost:8000 --requests 200 \
        --concurrency 16

Against the real provider this costs roughly
`requests x $0.006` (one flare-low call per portrait), so run it deliberately,
not reflexively:

    python scripts/stress_test.py --url https://your-app.onrender.com \
        --requests 12 --concurrency 3 --yes-i-am-using-a-paid-key
"""

from __future__ import annotations

import argparse
import asyncio
import io
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

try:
    import httpx
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit("Missing dependencies. Run: pip install -r requirements-dev.txt")

COST_PER_CALL_USD = 0.00588
CALLS_PER_REQUEST = 1


def make_photo(seed: int, size: int = 512) -> bytes:
    """A distinct synthetic upload per worker, so nothing can be served from a
    cache and every request does real decode work."""
    image = Image.new("RGB", (size, size), "#ffffff")
    pixels = image.load()
    for x in range(0, size, 8):
        for y in range(size):
            shade = (x * 7 + y * 13 + seed * 31) % 256
            pixels[x, y] = (shade, (shade * 3) % 256, (shade * 11) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@dataclass
class Stats:
    latencies: list[float] = field(default_factory=list)
    statuses: Counter[int] = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    caption_mismatches: int = 0


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


async def one_request(
    client: httpx.AsyncClient,
    url: str,
    seed: int,
    intensity: int,
    stats: Stats,
) -> None:
    photo = make_photo(seed)
    form = {
        "cartoon_intensity": str(intensity),
        "cartoon_style": "western_cartoon",
        "likeness_strength": "strict",
    }
    files = {"photo": (f"stress-{seed}.png", photo, "image/png")}

    started = time.perf_counter()
    try:
        response = await client.post(f"{url}/api/cartoonify", data=form, files=files)
        elapsed = time.perf_counter() - started
        stats.latencies.append(elapsed)
        stats.statuses[response.status_code] += 1

        if response.status_code == 201:
            payload = response.json()
            if not payload["images"]["cartoon_url"].strip():
                stats.caption_mismatches += 1
        elif response.headers.get("content-type", "").startswith("application/json"):
            detail = response.json().get("detail", response.text)
            stats.errors.append(f"{response.status_code}: {str(detail)[:120]}")
    except httpx.HTTPError as exc:
        stats.latencies.append(time.perf_counter() - started)
        stats.statuses[0] += 1
        stats.errors.append(f"{type(exc).__name__}: {exc}")


async def run(args: argparse.Namespace) -> Stats:
    stats = Stats()
    limit = asyncio.Semaphore(args.concurrency)
    queue: asyncio.Queue[int] = asyncio.Queue()
    for index in range(args.requests):
        queue.put_nowait(index)

    async with httpx.AsyncClient(
        timeout=args.timeout, limits=httpx.Limits(max_connections=args.concurrency)
    ) as client:

        async def worker(worker_id: int) -> None:
            while True:
                try:
                    seed = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                async with limit:
                    await one_request(
                        client,
                        args.url,
                        seed + worker_id * 100_000,
                        1 + (seed % 10),
                        stats,
                    )

        started = time.perf_counter()
        await asyncio.gather(*(worker(i) for i in range(args.concurrency)))
        stats.wall_clock = time.perf_counter() - started  # type: ignore[attr-defined]

    return stats


def report(stats: Stats, args: argparse.Namespace) -> int:
    total = sum(stats.statuses.values())
    wall = getattr(stats, "wall_clock", 0.0) or 1e-9
    successes = stats.statuses.get(201, 0)

    print("\n── results " + "─" * 58)
    print(f"  requests       {total} in {wall:.1f}s  ({total / wall:.1f}/s)")
    print(f"  status codes   {dict(sorted(stats.statuses.items()))}")
    if stats.latencies:
        print(
            f"  latency        p50 {percentile(stats.latencies, 0.5):.3f}s"
            f"  p95 {percentile(stats.latencies, 0.95):.3f}s"
            f"  max {max(stats.latencies):.3f}s"
            f"  mean {statistics.fmean(stats.latencies):.3f}s"
        )
    print(f"  missing cartoon urls {stats.caption_mismatches}")

    if stats.errors:
        print("\n  error samples (unique):")
        for message in list(dict.fromkeys(stats.errors))[:5]:
            print(f"    {message}")

    if not args.paid_key:
        print("\n  cost: $0.00 — mock mode makes no API calls.")
    else:
        spend = successes * CALLS_PER_REQUEST * COST_PER_CALL_USD
        print(f"\n  estimated spend: ${spend:.2f} ({successes} generations)")

    print("─" * 70)
    if successes == total and total:
        print("PASS: every request succeeded.")
        return 0
    print(f"FAIL: {total - successes} of {total} requests did not return 201.")
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="per-request HTTP timeout; keep it under the server's generation timeout",
    )
    parser.add_argument(
        "--yes-i-am-using-a-paid-key",
        dest="paid_key",
        action="store_true",
        help="acknowledge that a real provider will bill for these requests",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.paid_key:
        estimate = args.requests * CALLS_PER_REQUEST * COST_PER_CALL_USD
        print(
            f"About {args.requests} generations against a paid provider: "
            f"~${estimate:.2f}. Proceeding."
        )
    print(f"Hammering {args.url} — {args.requests} requests, {args.concurrency} at a time")
    return report(asyncio.run(run(args)), args)


if __name__ == "__main__":
    raise SystemExit(main())
