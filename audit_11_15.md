# Audit 2026-11-15 — why a book takes minutes, not a minute

Found while running the storybook review on the live site (Red Riding Hood, book `91dcd144`, commit 53fb091). Owner's report: "my friends generate a whole book in under a minute", and the progress caption began with "the pencil is resting".

Evidence is from the Render logs, the `books` and `api_calls` tables (read-only) and a local timing test. Nothing here has been fixed yet.

## Timeline of the run

| Clock (UTC) | Event |
|---|---|
| 11:19:34 | portrait requested (took about 11 s) |
| 11:20:15 | book started (`books.job_started_at`) |
| 11:20:32 | portrait upload finished |
| 11:21:45 | hair-description review (a 2 s call) |
| 11:22:08 | **first picture call**, 113 s after the start |
| 11:22:08 to 11:25:49 | 21 pictures drawn, 6 at a time, 23.7 s average per call (max 32 s) |
| 11:26:34 | still retrying (page 1 needed 3 attempts, page 17 needed 2) |

Reviews averaged about 5 s each, with a 12 s maximum.

## 1. About 70 s of silence before the first picture: plate decoding

**What happens.** Before any page is drawn, `_artwork()` (`app/services/book_runner.py:2287`) calls `book_plates`, `book_premades` and `book_portraits`. Those images live in the object store as lossless WebP. Each read (`app/blobs.py:253`) downloads the file, decodes the WebP and re-encodes it as PNG (`_Codec.decode`, `app/blobs.py:147`).

**Why it is slow.**
- Plates are 2388 x 1791. Decoding one and writing a PNG took 1.4 to 1.8 s on an 8-core desktop (measured). Render's fractional CPU will be several times slower, so 21 plates plus premades plausibly take about 70 s. This matches the silent gap in the logs. It is not measured on Render.
- The blob cache (`_CACHE_BYTES`, 48 MB, `app/blobs.py:73`) cannot hold a book's plates: each decoded PNG is 4 to 5 MB, so 21 of them are about 100 MB and evict each other. Every book repeats all of the work.
- All 21 plates are loaded upfront, before page 1 starts, even though a page needs only its own plate.
- The OpenAI provider then shrinks each plate to 1024 px again inside the draw call (`openai_provider.py:355`, `_fit`), which is extra CPU on the same slow core.

**Why the caption said "resting".** During this gap no page is in flight. The rule from the 2026-11-11 fix says "resting" only when nothing is in flight, so it is doing what it was written to do. The bug is the silent stall.

**Fix.**
1. Add a reference copy at approval time: when a plate is approved, store a 1024 px copy (a PNG or high-quality JPEG). Drawing uses the copy. Only the PDF and the print edition use the full-resolution plate. This removes the decode gap and the per-call shrink (`_fit`).
2. Load plates lazily per page, so page 1 starts after one small read, not 21 large ones.
3. Cache the reference copies (they are small enough to fit) so a second book pays nothing.
4. Add a timing log around each phase of `_render` (`artwork.load`, `hair_seen`, `plan`) to confirm the cause on Render.
5. Show a real stage ("Getting the pictures ready") during setup, so no gap reads as resting.

## 2. Pictures start in groups of 6, and each takes 23 to 30 s

**What happens.** `BOOK_PAGE_CONCURRENCY` is 6 (`render.yaml:152`) and `MAX_CONCURRENT_GENERATIONS` is 8. These were turned down after the 2026-11-10 out-of-memory and 2026-11-11 event-loop incidents. The fixes that came afterwards (image pool, `asyncio.to_thread`, JPEG changes in `a6e4a20`) were never followed by a retry at higher values.

**Why it is slow.**
- 21 pictures at 6 at a time is 4 rounds. At about 25 s a picture that is 100 s or more, even before reviews and retries. Measured: first draw call 11:22:08, last first-attempt call about 11:25:49.
- Per-call time is mostly OpenAI itself. The one-reference portrait took 11 s. Pages send 2 to 3 references and a 4,000-character prompt and took 23.7 s on average.
- All work shares the default `to_thread` pool, which is sized from the CPU count (`min(32, cpus + 4)`). On a small instance that is a few threads shared between database calls, reviews and provider calls.

**Fix.**
1. Raise `BOOK_PAGE_CONCURRENCY` and `MAX_CONCURRENT_GENERATIONS` toward 21 in steps. Watch memory and the `loop.lag` warnings between steps. Check OpenAI's rate limits first. Use the 0.5 CPU Starter plan the render-stall plan already recommended.
2. Give provider calls and reviews their own thread pools, so database work cannot starve them.
3. Send smaller references: use the pre-shrunk plate copy from fix 1, and trim the prompt.
4. Pick the quality and size tier by measured speed against a fixed picture, not by guess.

## 3. A page's slot is held through its review and redraws

**What happens.** In `_render` the page task runs inside `async with in_flight` (`book_runner.py:1412`). That one slot is held for the draw, the review and every redraw. The image gate (`_on_worker_thread_with_slot`) is released after the draw, so the review does not block other draws directly. But the page limit of 6 is held, so a page that is being reviewed or redrawn blocks a fresh page from starting.

**Evidence.** Page 1 took 3 attempts (retries at 11:23:19 and 11:24:18) and page 17 took 2. Reviews took 5 s on average and up to 12 s. While those pages hold slots the later pages wait, so the drawing does not run flat out.

**Fix.**
1. Hold the page slot only for the draw. Run review and redraw after the slot is released, as separate queued work with their own limit.
2. Start the next page as soon as a draw finishes, so drawing stays at full concurrency while reviews catch up.
3. Review a finished picture while the next ones draw. The reviewer is called once per attempt, so this is an ordering change only.
4. Keep the retry ceiling and budget (`3eed617`, `ea66b7b`). The 3 attempts per page are part of the assignment.

## Order of work

1. Fix 1 (reference copies, lazy plate loading, timing logs). This is the largest cost and the cheapest to confirm.
2. Fix 3 (hold the slot only for the draw).
3. Fix 2 (raise concurrency in steps and measure).

Every one of these touches the render path, needs a manual Render deploy, and must be followed by a timed live book. The target is 60 s from upload to PDF, per the storybook-review skill. At 23 to 30 s per OpenAI picture, all 21 pictures must run at the same time for that target to be reachable.

## Not yet measured

- The plate decode time on Render's CPU (the 70 s is inferred from a desktop measurement and from the log gap).
- Whether OpenAI accepts 21 simultaneous image edits on this account without 429s.
- Memory at higher concurrency.
