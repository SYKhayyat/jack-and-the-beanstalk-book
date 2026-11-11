# Plan: stop the server stalling under a book render, and make a restart not lose the book

Written 2026-11-11 for Opus, from storybook-review Run 2 on the live site.
Read `to-do.md` (UI section, the two Run 2 items) and the memory notes
`render-restart-mid-render` and `progress-caption-resting-bug` for context.

## What happened (evidence, all from the live service)

Service: Render `jack-and-the-beanstalk-book` (`srv-dbdmqro473hc73cu3680`),
**free plan** (512 MB RAM, a fraction of a CPU), one instance, one uvicorn
process, Docker runtime, `healthCheckPath: /healthz`.

Book `f6ff1a3522bb4ed7bf6e262d5f7bb2e7` (Jack, one real child photo) was
submitted 15:12:50 UTC. All 21 pages went to `painting` at once
(`asyncio.gather` in `app/services/book_runner.py` ~line 794, one `run_page`
per page, gated by `ConcurrencyGate`, default `max_concurrent_generations=12`
in `app/config.py`).

From the Render logs (`render logs --resources srv-dbdmqro473hc73cu3680
--start 2026-11-11T15:12:00Z --end 2026-11-11T15:17:50Z -o text --confirm`):

- Normal: `/api/book/<id>` polls ~300 ms, `/healthz` ~1.5 ms.
- 15:14: `/healthz` 1-3 s. 15:16:04: **25 s**. 15:16:41: **22 s**.
  15:17:00: four checks at **~21 s** each. 15:17:14: a status poll **53 s**.
- Last log line 15:17:21. New process at 15:17:49 (alembic starts), no
  shutdown or traceback line. `reap_incomplete` marked the book failed
  ("The server restarted while this book was rendering").
- Not a deploy (live deploy 14:58:54 UTC). **Render's Events page shows
  nothing after 10:00 ET and no email was sent**, so there is no recorded OOM
  and no recorded failure. Do not assume out-of-memory; the evidence is a
  starved/blocked event loop, a restart, and no cause on Render's side.
- 14 of 21 page images (about $0.36) and 4 reviews were done and lost.

Working conclusion: the process was starved for tens of seconds while many
pages were mid-flight, so health checks timed out and the instance was
restarted. The kill reason itself is **unconfirmed**; the first task is to
find out where the time and memory go, not to guess.

## Goals

1. A book render must never make `/healthz` or the status poll slow. Keep
   `/healthz` p99 under ~100 ms for the whole render, on the free plan.
2. A restart (deploy, health-check kill, OOM, anything) must not lose
   finished work: a reaped book resumes from its saved pages, charging and
   paying for only the missing ones.
3. Say what the server is doing: the progress caption must be truthful (see
   the separate UI bug below).
4. Keep the 60-second-per-book target in sight (Run 1: ~17 min; Run 2 was
   ~5 min to 14/21 pages). Do not make it slower to make it safe; the
   concurrency cap trades against it, so measure.

## Phase 0: measure first (no behaviour change)

Do not fix blind. Reproduce locally and on the live service, in this order:

- Find what runs on the event loop thread during a render. Suspects, all to
  be confirmed with a profiler (py-spy `--gil`/`dump`, or `asyncio` debug
  mode with `loop.slow_callback_duration=0.1`, or a tiny loop-lag logger):
  image decode/encode and resize (`app/transcoding.py`, `app/services/images.py`,
  `app/services/pictures.py`, Pillow), base64 of ~1.5 MB images for the
  reviewer (`app/services/reviewer.py`, `image_review.py`), PDF assembly and
  Hebrew shaping (`app/services/pdf.py`, `typography.py`), DB writes of
  large JSON `status_payload` (8.5 KB truncated in the logs; check real size
  and how often `paint()` rewrites it per page), `BlobRef` writes (R2),
  logging of large strings, the status route rebuilding the payload.
- Find which of those are **sync work called directly inside a coroutine**
  (blocks the loop) versus work already behind `asyncio.to_thread` (does not
  block the loop but contends for the GIL, which on 0.1 CPU is the same
  symptom). `to_thread` uses the default executor, which has no cap and
  shares threads with the DB calls and the routes (sync routes also run in
  the threadpool, so `/healthz` itself can queue behind them if it is a sync
  `def`; check it).
- Measure peak RSS during a 21-page render: images held per page, number of
  reference images per call (2-3 plates/portraits per page, ~1.5 MB results),
  retries kept, review payloads, the full `BlobRef` bytes loaded into memory
  by `load_record` for every status poll. Free plan limit is 512 MB.
- Add one permanent, cheap **loop-lag log line** (every 5 s, log the lag
  when over 250 ms) and RSS in the existing structured logs, so the next
  Render log answers this question without guessing. Keep this.

Deliverable: a short table in this file of "what blocks the loop, for how
long, per page", with the measurement method.

### Phase 0 findings (2026-11-11)

Method: each step timed in isolation (median of 5, `time.perf_counter`) on a
1024x1536 page picture saved as PNG (3.5 MB), on an 8-core dev machine; the
render as a whole in `tests/test_render_stall.py` (21 pages, slow fake
provider, full-size noisy pictures) with the loop-lag watcher sampling every
50 ms. Render's free plan has ~0.1 CPU, so a CPU-bound step there takes about
ten times the wall time below, and every step running at once shares that one
tenth of a core.

| Step, per page | Where it ran before | Dev box | ~Free plan | Now |
| --- | --- | --- | --- | --- |
| Tier-1 check (`review_image`: decodes page + plate) | **in the coroutine**, every attempt | 230 ms | ~2.3 s | image pool |
| Reviewer copy of the page (`judge_jpeg`, cold; a PNG decode alone is 120 ms) | `to_thread`, default pool, per review | 215 ms | ~2 s | with the reviewer call, cached per bytes |
| Reviewer copies of plate + portrait | re-made for every review, default pool | 215 ms each | ~2 s each | made once per book (`ShrunkCache`) |
| Plate paste-back (retry attempt 2) | `to_thread`, default pool | not timed (one decode + composite + encode) | — | image pool |
| base64 of the judge copy / of a full PNG | with the reviewer call | 1 ms / 7 ms | 10 / 70 ms | unchanged |
| Thumbnail / display copies (240 / 800 / 1600 px) | sync route, default pool | 154 / 446 / 555 ms | 1.5 / 4.5 / 5.5 s | unchanged (sync route), made once and stored |
| Status payload, `json.dumps` (3.3 KB, 21 pages) | in the coroutine, per stage change | 0.2 ms | 2 ms | unchanged: not a cause |
| PDF embed copy (`_image_for_pdf`: JPEG with `optimize`+`progressive`) | PDF thread, per page | 297 ms, **GIL held 243 ms in one call** | ~2.7 s frozen | baseline JPEG: GIL never held over ~8 ms, ~5% bigger |

Holding the GIL is measured separately from wall time: a ticker thread times
its own 1 ms sleeps while the step runs on another thread, and its worst
overshoot is the longest the loop could have been frozen. Decode, `convert`,
`ImageStat`, the seam check and the downscales all stay under ~8 ms (Pillow
releases the GIL in chunks). The exception is a JPEG saved with `optimize=` or
`progressive=`: Pillow encodes the whole frame in one call that keeps the GIL,
107-270 ms on the dev box. That froze the loop for every page of every book's
PDF assembly, and on 0.1 CPU for seconds at a time. Both flags are gone
(`app/services/pdf.py`, `app/services/images.py`), and a test fails if either comes back.

What stalled the loop: up to sixteen pages in flight at once, each running
its tier-1 check — two ~3 MB picture decodes — *inside the coroutine*. On 0.1
CPU that is ~2 s of loop time per page, tens of seconds per wave of pages,
which is the 20-second unanswered health check of Run 2. The `to_thread`
steps (judge copies, paste-back, the model calls themselves) did not block the
loop directly but held the GIL on the one tenth of a core and queued on the
same default pool that serves the sync routes and DB calls, so the health
check (then a sync `def` with a DB call) waited behind them too.

After Phase 1 the stall test renders the whole book with no `loop.lag` record
at a 250 ms threshold, `/healthz` never slower than 0.5 s, and the poll (which
reads SQLite in the test, blocked by the runner's own status writes —
Postgres readers are not) under 5 s.

Memory (512 MB limit): a page in flight holds its drawn PNG (1.5-3.5 MB), and
while it is decoded ~4.7 MB of pixels; the reference plates and portrait are
held once per book. With `BOOK_PAGE_CONCURRENCY=6` and `IMAGE_WORK_THREADS=2`
that is ~25 MB of picture bytes at the peak, against ~16 pages' worth (~60 MB
plus decode spikes) before. `rss_mb` is now in every `loop.lag` warning and in
the `book.page` and `book.complete` lines, so the next live render shows the
real peak.

## Phase 1: stop the stall

Apply only what Phase 0 shows, in this order of likelihood:

1. Move every CPU- or bytes-heavy step off the event loop and **bound the
   pool**: a dedicated `ThreadPoolExecutor(max_workers=2-3)` (or process
   pool for pure-CPU Pillow work) for image work, separate from the default
   executor that serves DB and routes. Never let image work and the health
   check share an unbounded pool.
2. Cap page concurrency separately from `max_concurrent_generations`: a
   per-book page semaphore (try 4-6, measure; the model calls are I/O so
   this mostly costs latency, not CPU). Make it a setting, default chosen
   from the Phase 0 numbers, and re-time with `scripts/time_book.py`.
3. Release image bytes early: do not keep a page's full result, its
   reference images and its review payload alive after it is saved; hold
   `BlobRef`/keys, not bytes, in the runner's state; downscale reference
   images once per book (cache) instead of per call.
4. Make `/healthz` independent of everything: an `async def`, no DB, no
   locks, constant-time, so a busy render cannot queue it. Consider a
   second cheap liveness signal instead of the same route for readiness.
5. If the free plan cannot hold 21 pages at once even after 1-4, say so and
   recommend the paid Starter plan (more CPU and RAM) with numbers; this is
   a product/cost decision for the owner, not something to hide.

Done (2026-11-11): the image pool (`IMAGE_WORK_THREADS`, default 2), the
page semaphore (`BOOK_PAGE_CONCURRENCY`, default 6), the per-book shrunk
copies, an async database-free `/healthz`, and the stall test.

**Plan recommendation (for the owner).** The free plan can now hold a book
without stalling, but it is slow: on 0.1 CPU a page's image work is ~5-6 s
of CPU (the tier-1 check, three judge copies on the first review, a
paste-back on a retry), and the thumbnails a watching browser asks for add ~1.5 s each. Six
pages at once already saturate it, so the book's wall time is CPU-bound
rather than model-bound. Render Starter ($7/month: 512 MB, 0.5 CPU) gives
five times the CPU, so the image work stops being the bottleneck at the
same memory, and it does not spin down when idle (a cold start costs the
first visitor ~30-60 s on the free plan). Recommended once real customers
are making books; Standard ($25/month: 2 GB, 1 CPU) only if two or more
books are routinely made at the same time. Check Render's current prices
before deciding.

Tests: a render of a fake 21-page book with a deliberately slow image
provider while a second client polls `/healthz` and `/api/book/<id>`; assert
loop lag stays under a threshold. Run pytest in a shell that has **not**
sourced `.env` (it would hit Supabase).

## Phase 2: resume a reaped book

Today `reap_incomplete` (`app/services/db_book_store.py` ~line 745, called
from `app/main.py` ~line 167) marks PENDING/RENDERING books FAILED and tells
the user to "Start a new one" (they pay the fee again, on the next success).

- Pages already saved (`book_pages` rows, plus the cover and portrait) are
  kept. Add a resume path: on startup, instead of failing, mark the book
  `rendering` and re-enter the runner for only the missing pages
  (`only_missing` already exists in `book_runner.py` for edits, see
  `_mark_rendering(..., only_missing)`; reuse it rather than writing a
  second path). Guard against crash loops: count resumes per book (a
  `resume_count` column or in `settings`), stop after 2 and fail with the
  current message; never resume a book older than N minutes.
- Idempotent charging: the $0.99 is taken when the PDF is written; confirm a
  resumed book charges exactly once, and a failed/abandoned one never.
- A "Resume" button on the failed book page as the manual fallback (the
  failed screen already keeps the finished pages visible).
- Migration if needed (alembic, latest is 0013); update `reap_incomplete`
  tests; add tests for: restart mid-render resumes, second restart resumes,
  third restart fails, wallet charged once.
- Related cost leak: pages that were drawn but not yet reviewed/saved when
  the process died are paid for and lost; log them (`api_calls` already has
  them) so they show in the per-book cost.

Done (2026-11-11): migration 0015 adds `job_started_at` and `resume_count`;
at boot `BookRunner.recover` resumes each interrupted book as the job it was
(its pages, cover and requested fixes, from the status `job` note), skipping
finished pages, at most `BOOK_MAX_RESUMES` (2) times and only within
`BOOK_RESUME_WINDOW_MINUTES` (60) of the job's start; otherwise it fails
with "Finish this book" (the existing manual fallback, `retry`). A graceful
shutdown (every deploy) now pauses a rendering book instead of failing it, so
the next boot resumes it. The fee is charged once on completion (tested end
to end across a restart), and each resume logs `book.resumed ...
lost_pictures=N` from `api_calls`.

## Phase 3: truthful progress (separate, small)

Bug in `app/static/book-live.js` ~line 347: "0 of 21 pictures drawn. The
pencil is resting for a moment" appears while 20 pages are painting. The
"done" counter counts pages that passed review, and after 8 quiet polls
(`RESTING_AFTER_POLLS`, ~12 s) the caption falls back to "resting" although
`stage_counts` says pages are painting or reviewing. Fix: count pictures
drawn separately from pictures accepted, always prefer the `stage_counts`
sentence, and never show "resting" while any page is painting/reviewing/
being redrawn; show "Try N of 3" per page (Run 2 never saw it; Run 1 did).
Also: a thumbnail per page as it finishes, and a visible progress bar value
(was empty in every Run 2 reading).

Done (2026-11-11): the status payload carries `page_stages` (each page in
flight: stage and try) and `pictures_drawn`. The caption counts pages
finished and pictures drawn and names what is in flight; "resting" only
when nothing is. Each tile in flight shows its try ("2/3") and the spread
says "being checked, try 2 of 3"; finished tiles show a thumbnail. The bar
takes partial credit for pages in flight, never runs backwards during a job,
and stripes while work is in flight.

## Phase 4: verify on the live site (the owner, or the review skill)

- Deploy; run one Jack book with a new photo; watch `/healthz` durations in
  `render logs` stay small; the book completes; `/wallet` charges $0.99 once.
- Kill the instance mid-render on purpose (`render restart
  srv-dbdmqro473hc73cu3680`) and confirm the book resumes and finishes.
- Re-run `storybook-review` (Run 2 in `review-log.md` is waiting for exactly
  this; a browser agent can drive everything except card entry and the
  paid "Draw the portrait" / "Make the book" clicks, which the user does).

## Constraints from the owner (memory)

- Build items in their final shape; no interim slices or technical debt.
- Production product: no shortcuts that only work in dev.
- Be mindful of spend: every full live book is about $0.60-1.60 of image
  API; prefer the mock provider or local fakes for tests, and one real book
  per verification.
- Commit to main and push once checks are green; stage only your own hunks
  (other sessions share this checkout).
- Never print DATABASE_URL or other secrets; read-only queries against the
  live DB are fine via `.venv/bin/python` + sqlalchemy.

## Out of scope here

Pricing/average cost ($1.58 vs $0.99; see `review-log.md` Run 1 addendum),
the refund still "Waiting for the operator", and book 2 / sign-out checks:
those belong to the review run that resumes afterwards.
