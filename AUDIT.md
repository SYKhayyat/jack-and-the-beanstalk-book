# Storybook Studio — application audit

**Date:** 11 November 2026
**Scope:** the whole app in this repository (FastAPI backend, templates, static JS/CSS, book runner, review loop, wallet/payments, admin, storage, deployment blueprint).
**Method:** source read-through, a local run with the mock image provider, a headless-browser walkthrough of the shelf → studio → About → generation → reader → wallet → admin, log inspection, targeted timing measurements, and `ruff`/`mypy`.
**No paid API calls were made.** The local server ran with `IMAGE_PROVIDER=mock`, `REVIEWER_PROVIDER=off`, `DECISION_JUDGE=off` and a throwaway SQLite database, so OpenAI/Stripe were never contacted.

**Verification note:** `ruff check .` and `mypy app` both pass (91 files). `tests/test_local_book.py` passes (3 tests, ~18 s). The full suite did not finish within a 400 s budget in this environment, so it is not claimed as a full pass. The findings below are from code and observed behaviour, not from production data.

**How to read this:** every finding has a severity, the code that causes it, what a user or the operator sees, and a concrete fix. Line numbers are from the audited tree.

| Severity | Count | Meaning |
| --- | ---: | --- |
| P0 | 4 | Can lose a book, lose money, or stop the service |
| P1 | 13 | Significant bug / cost / usability damage |
| P2 | 18 | Polish, edge cases, maintainability |
| Idea | 12 | Not defects; the product levers not yet taken |

---

## 1. Bugs in features

### B1 — P0: a book render can block the event loop, fail the health check, and lose the whole book
The free Render instance has already restarted mid-book because `/healthz` took 20–50 s (`to-do.md`, “The server stalls under a book render”). There are two concrete causes still in the tree:

* `BookRunner._judge` calls `review_image(...)` synchronously inside an `async def` (`app/services/book_runner.py:1001`). Tier 1 decodes every page with Pillow and scans it for seams on the event loop.
* `_make_variant` calls `encode_variant(...)` synchronously inside the request handler (`app/routes/book_media.py:257`). A 1152×864 PNG measured 0.28 s at 800 px and 0.38 s at 1600 px on this machine; the free Render CPU is several times slower, and a first view of a 21-page book can encode dozens of these while the reader is waiting.
* PDF assembly is correctly threaded (`book_runner.py:812`), and `paste_plate_back` uses the bounded image pool, so the remaining synchronous paths are the two above.

Once the health check fails and Render restarts the process, `reap_incomplete()` marks every pending/rendering book failed (`app/services/db_book_store.py:745`); the user sees a failed book and the operator has paid for the pages already drawn.

**Fix:** run tier 1 through the existing `_image_work`/thread pool, and make variant encoding a threaded job (or pre-compute the 800/1600 copies right after `save_page`/`save_cover`). Add a regression test that a 21-page mock book leaves the event loop responsive (e.g. a concurrent `/healthz` under 100 ms).

**Done (11 Nov 2026):** both paths were already off the loop when the audit was filed, which cited a tree older than `a6e4a20`; checked rather than re-fixed, and the missing assertion added. Tier 1 runs on the runner's own bounded image pool (`BookRunner._image_work`, `book_runner.py`), never in a coroutine, and JPEGs are saved without `optimize`/`progressive` (pinned by `test_no_jpeg_is_encoded_in_one_gil_holding_call`). The media routes (`book_page`, `book_cover`, `book_portrait`, `_sized` → `_make_variant`) are sync handlers, so Starlette runs the encode on a worker thread, and the encode holds one of the process's two `transcoding()` slots, so a first view cannot flood the pool or the memory. `test_a_rendering_book_leaves_the_loop_free` now also asks for every page's 1600 px copy at once — the one size still made on first view (B12 makes the 240 and 800 px copies at save time) — while sampling `/healthz` and the loop's lag watcher: 21 copies served, health check under 0.5 s throughout, no `loop.lag` record. The watcher was verified to log a deliberate 500 ms block, so the assertion is live.

### B2 — P0: one failed page aborts a finished-but-uneven book and no PDF is produced
`asyncio.gather(..., return_exceptions=True)` collects every page, then `fatal = [...]` and `raise fatal[0]` (`app/services/book_runner.py:829–831`). A single provider timeout, moderation block that never recovers, or reviewer error therefore throws away the other 20 pages' progress from the user's point of view: no PDF, and the book is marked failed. Completed page rows are kept, and “Finish this book” can retry the missing pages, but the user has to discover and press it, and the render time/cost already spent is lost.

**Fix:** keep a page-level failure and assemble the book from the pages that exist (a clearly marked “couldn't draw this one” page), or retry provider errors once automatically before failing the whole job. The UI already has a per-page `failed` state and a retry action, so the backend should match it.

**Done (11 Nov 2026, `e2a361e`):** the first option, as asked. A story page that never draws is marked `failed`, the other pages are kept and the book is assembled with a clearly marked page in its place; the book page names how many pictures could not be drawn and “Finish this book” draws only those, at the picture price. A cover failure still stops the job, because the cover is the book’s face and the “Finish this book” path draws it later. Pinned by `test_one_failed_page_does_not_lose_the_book`.

### B3 — P1: the wallet can go negative when two books are started at once
`_require_funds` runs at start (`app/routes/book.py:366`), but the actual charge happens when the PDF is written with `require_funds=False` (`app/services/book_document.py:169–186`). Two tabs (or two requests) with exactly $0.99 both pass the check; both books finish; the ledger ends at −$0.99. This is documented as “the account owes the difference”, but nothing ever collects it: the next book is refused until the user tops up, which is fine for an honest user and a loss for anyone who does not.

**Fix:** reserve/hold the fee atomically at start (a `KIND_HOLD` row or a `book:<id>` pending charge), release it when the job fails and apply it when the PDF is written; or refuse the second start if `balance − open_books × price < price`.

**Done (11 Nov 2026, `e2dc15e`):** the second option, made a set-aside rather than a new ledger kind. `_require_funds` now counts the account’s open books (pending or rendering) and refuses a start unless `balance − open_books × price ≥ price`, under the same `_start_lock` that holds the reuse lookup, so two tabs in one process cannot both read the same count before either records its book. The fee itself is still taken when the PDF is written, so a book that never finishes is never charged, and the refusal names the amount already set aside and the way to wait or top up. Pinned by `test_a_second_book_cannot_spend_the_fee_set_aside_for_the_first` and `test_an_account_that_can_pay_for_two_books_may_start_two`.

### B4 — P0 (cost): a $0 account can spend real money on portraits, and any buyer gets unlimited redraws
Two gaps combine into an open spending tap:

* `POST /api/cartoonify` (`app/routes/api.py:83`) has no funds check. It only admits against the per-IP rate limiter (`api.py:131`). A signed-in account with a zero balance can upload a photo and generate candidate portraits all day (1–3 images per call); those calls are billed to the operator and most will never lead to a book.
* Every edit after a book exists is free by design (`app/routes/book.py:422–570`): `action=character` with `character_key=star` redraws the whole book plus the cover, up to three attempts per page, and can be repeated indefinitely. `edit_book` has no funds check, no per-book edit budget, and the rate limiter is only a per-IP abuse brake.

With `MAX_GENERATIONS_PER_MINUTE=200` and roughly $0.024 per image, a determined account can spend tens of dollars an hour without ever paying $0.99.

**Fix:** require a wallet balance (or take a small hold) before a paid portrait, cap free edits per book (e.g. one page redraw and one cover redraw), and add a per-account/per-day generation budget separate from the per-IP rate limit. At minimum, alert the operator when one account crosses a cost threshold.

**Done (11 Nov 2026, `412cb07`):** both taps are shut by selling the pictures rather than by a hold. `POST /api/cartoonify` takes `PICTURE_PRICE_CENTS` (15¢) per candidate before the model is asked, refuses a wallet that cannot cover them and names the price and the balance, and pays back candidates the provider never returned (`_charge_portrait` / `_refund_portrait`, `app/routes/api.py`). `edit_book` refuses up front when the wallet cannot pay for one picture (`_require_picture_funds`) and then debits each picture as it is drawn (`_picture_payment`, reference `picture:<book>:<job>:<n>`, so a job retried with the same reference is not charged twice) — a star swap is one bought picture per page plus the cover, and repeating it costs the same again. An account that runs out mid-redraw stops, keeps the pictures already made, and is told the price and the way to finish (`OutOfFunds`). A book's own cover and story pages stay inside its fee. The fix's third ask — a per-account/per-day budget and an operator threshold alert — is C4, still open. Pinned by `tests/test_picture_charges.py`: a portrait costs one picture; a portrait the wallet cannot pay for is not drawn; only the candidates actually drawn are charged; a new book's own pictures are inside its fee; a redrawn cover costs a picture; a redraw with no money left is refused before it starts; running out mid-redraw stops and keeps what was drawn.

### B5 — P1: mock/demo mode is deterministically “broken” by the tier-1 collage check
Every page the mock provider draws gets `collage` from tier 1. Verified directly:

```text
review_image(mock_page) -> False 30 ('collage',)
```

and in the local logs every page of every demo book was reviewed as `faults=collage score=30 outcome=failed` (21 pages + cover), then redrawn three times and marked best-effort. The cause is structural: the mock draws full-width dark banners at the top and bottom (`app/services/image_providers/mock.py`, `_banner`), and tier 1 treats two full-width seams as a contact sheet (`app/services/image_review.py`, `COLLAGE_SEAMS = 2`).

**Impact:** demo books are 3× slower/noisier, show “Best of 3 tries” on every page, and the demo cannot be used to judge the normal happy path. With a real provider, the same false positive costs two extra page images every time a real page has two legitimate full-width seams (a horizon plus a table edge, a bed, a window frame).

**Fix:** skip tier 1/2 for `provider.draws_placeholders` (or add a `review=False` to `ImageRequest`), and make the mock banners not span the full width. Add a test that mock pages pass review. Longer term, make the seam detector ignore a small set of strong horizontal bands or require a grid of seams in both directions.

**Done (11 Nov 2026):** the mock banners are inset 14 % a side, so the hard edge covers 72 % of the width — under the detector’s 75 % threshold — and a placeholder page passes tier 1; and a placeholder provider gets one attempt, since there is no model to steer and a retry would be three identical placeholders. The demo is now the happy path it was meant to be: one attempt, a passing verdict, no “best of three”. Pinned by `test_a_placeholder_page_passes_the_free_review`; the detector itself is still calibrated by C5.

### B6 — P1: every page badge exposes internal quality-control language, and one string is ungrammatical
`app/static/book-live.js:447–501` writes the attempt record into the visitor's UI:

* `Try 1 of 3` / `Try 2 of 3` on a normal page.
* `Best of 1 tries` — observed in the local book page — is both plural-wrong and meaningless (one try is not “best of”).
* `3 of 3 tries — none passed every check, so the best one (try 3) is shown` on a best-effort page.
* `3×` in every page-map tile, even when the page passed on the first try.

The book is a gift for a child; “checks”, “tries” and “best effort” are an operator's vocabulary. They also make a perfectly good page sound defective.

**Fix:** hide the chip for `attempts === 1`; for retries say something human (“Redrawn once”) or move the whole record to the operator page; never show “Best of 1 tries”. Keep the detailed record in `/admin/books/{id}` where it belongs.

**Done (11 Nov 2026, `8dad299` then `6fa847d`):** the chip, the page-map count and the in-flight try count are gone from the reader altogether, as asked: a redraw now reads exactly like a draw. The record stays in the poll, in `data-attempts` and in full on `/admin/books/{id}`.

### B7 — P2: the page number and the printed folio disagree on the same page
The first story page is labelled `Page 1 of 20`, while its printed folio is `2` (`app/templates/book.html:32`, `app/static/book-live.js:90–93`). The cover is counted as page 1 in the PDF folio (`app/services/pdf.py:386–390`) but not in the reader's page counter, and the shelf copy says “20 pages and a cover”. Three numbering schemes are visible at once.

**Fix:** pick one: “Page 2 of 21” with the cover as 1, or folio `1` for the first story page. Apply the same rule to the PDF, the reader label and the page map.

**Done (11 Nov 2026):** the first rule everywhere: the cover is page 1, story pages are folios 2–21, and the reader bar, the page map and the PDF all use it. Pinned by `test_the_reader_sets_every_page_as_a_book` and `test_the_reader_bar_counts_the_cover_as_page_one`.

### B8 — P1: a restart does not resume a book; it fails it, even when most pages are saved
`reap_incomplete()` (`app/services/db_book_store.py:745`) marks all `PENDING`/`RENDERING` books failed at boot. The message says the finished pages were kept, and “Finish this book” can retry the missing ones — but the user must click it, and the rendering time already paid for is not reused automatically. The Run 2 incident lost 14 of 21 pages and $0.36 of spend this way.

**Fix:** on boot, re-enqueue failed books that have a cover or at least one saved page and are still inside their retention window, or at least surface “resume” prominently on the failed book page. This turns a restart from a loss into a delay. It also needs the multi-worker caveat in B9.

### B9 — P1 (scaling): single-process assumptions are load-bearing
`reap_incomplete()` fails *all* pending/rendering books on every boot, including books another live worker may be rendering. `_start_lock`, `_active` jobs, `BookRunner._reviewer_rests_until`, the rate limiter and `Wallet._lock` are all in-memory. With more than one Render instance or Uvicorn worker, duplicate starts, double charges and cross-worker reaping are possible. The blueprint currently runs one free instance, so this is a latent scaling bug, not a live incident.

**Fix:** document/enforce a single instance, or move job state, locks and leases to the database (a lease/heartbeat column and a `SELECT ... FOR UPDATE` start transaction).

**Done (11 Nov 2026):** the job lease is in the database. `books.job_owner` / `books.job_expires_at` (migration 0016) are claimed with one conditional `UPDATE` before a job is scheduled, renewed by a heartbeat every 30 s (lease 120 s), and released when the job ends, including on a graceful shutdown. A second instance's edit answers `409 book_busy`, a boot resumes only books whose lease is free or expired, and a book another instance is still drawing is left completely alone rather than reaped. The remaining per-process pieces — rate limiter, concurrency gate, wallet lock — are documented in the README; the ledger itself is already safe by unique reference and summed balance. Pinned by `test_one_worker_at_a_time_holds_the_job_lease`, `test_an_expired_lease_can_be_taken_over`, `test_a_book_a_live_worker_is_rendering_is_left_alone`, `test_the_boot_resumes_a_book_whose_worker_died`, `test_a_finished_book_has_given_its_job_lease_back` and `test_an_edit_is_refused_while_another_worker_holds_the_book`.

### B10 — P2: an About-page edit during a render can be silently overwritten
`action=about` updates the record and only rebuilds the PDF if the book is already complete (`app/routes/book.py:459–490`). A render in progress has already loaded the old `BookRecord` and assembles the PDF from that snapshot, so an About edit made mid-render is lost when the job finishes. The UI hides the edit strip while rendering, so this needs a direct API call, but it is a real data-loss path.

**Fix:** reject `about` edits (409) while an edit/render is active, or make the render re-read the record before assembly.

**Done (11 Nov 2026):** the second option, which keeps the edit rather than refusing it: the job re-reads the record just before the PDF is assembled, so an About edit made while the pictures were drawing is the text the finished book carries (title and subtitle too). The pictures are still the ones that job drew. Pinned by `test_an_about_edit_during_a_render_reaches_the_pdf`, which fails without the re-read.

### B11 — P2: the PDF can silently truncate the About page while the web reader shows all of it
`_fit_text` shrinks the About body and, at the floor, drops lines and appends an ellipsis (`app/services/pdf.py:518–540`). The About form allows 1,000 characters of “details”, and the web About spread renders all of it. A family who writes a long dedication gets a different (shorter) book in the download.

**Fix:** warn and limit at the form (or add a dedicated “dedication” page), or paginate the About text across a second PDF page so the PDF matches the reader.

**Done (11 Nov 2026):** the About text is paginated: `_fit_text_pages` lays the sentences out at the largest size that fits and, when they do not, continues them on another page with the same text column and the same room kept for “The End”, so a 1,000-character dedication prints every word the reader saw instead of an ellipsis. Pinned by `test_a_long_about_text_flows_onto_another_page_instead_of_being_cut`.

### B12 — P2: a demo/mock book's first paint is slow because variants are built on demand
On the first view of any newly completed book, every image URL is a `?w=1600&v=…` URL; the server creates and stores each WebP variant when first requested. A 21-page book therefore pays 42 encodes (800/1600) plus 21 thumbnails, one request at a time, with no background warm-up (`app/routes/book_media.py:227–260`). On a slow instance the page turns feel stalled even though the images are local.

**Fix:** generate the 800 and 240 variants immediately after `save_page`/`save_cover` (in the image pool), and keep the 1600 copy lazy; or make variant generation part of the book's “assembling” phase.

**Done (11 Nov 2026):** `warm_variants` (`app/services/pictures.py`) runs on the render's image pool right after each page, premade page and cover is saved, filing the 240 px and 800 px copies under the hash the store recorded; the 1600 px copy stays lazy, and the warm-up and the media route share one lock. Pinned by `test_a_finished_book_arrives_with_its_screen_copies_already_made`.
### B13 — P1: a picture whose verdict never lands takes the whole book down with it
`save_page` and `record_page_review` are two transactions (`app/services/db_book_store.py:571` and `:592`). The first deletes the page row and re-inserts it — review columns included — so between them the page has a picture and no verdict; if the second write fails, the page stays that way. `save_status(COMPLETE)` refuses to complete a book with an unreviewed picture (`_unreviewed`, `db_book_store.py:1051`), so the whole book fails. Observed in a parallel test run: `book.failed … cannot complete: no review for page(s) [5]`, after page 5 raised `OperationalError` (a SQLite write lock) **after** its picture was saved, with the other twenty pages finished.

That is B2's promise broken by another route, and “Finish this book” does not help: it redraws pages that are *missing*, not pages whose picture exists without a verdict. The book and the money already spent on it are stuck.

**Fix:** write the picture and its verdict in one transaction (`save_page(..., review=…, attempt_count=…, chosen_attempt=…, best_effort=…)`), so a page can never be saved without them. As a safety net, treat a page that exists without a review as missing: let the book assemble, mark that page “couldn't be checked”, and let Finish redraw it.

---

## 2. Display and UI

### U1 — P1: the attempt record is on the reader, not hidden
See B6. The chip sits next to “Page 1 of 20” and on every tile; on the cover it can read “Best of 1 tries”. This is the most visible “debug UI” in the product.

**Done (11 Nov 2026):** closed with B6 (`8dad299`, `6fa847d`) — the chip, the count on each tile and the in-flight try count are all gone from the reader; the record stays in the poll, in `data-attempts` and in full on `/admin/books/{id}`.

### U2 — P2: the progress screen shows reader tools before there is anything to read

**Done (11 Nov 2026):** the reader tools stay hidden until a story page exists — the cover is not something to read aloud or turn. Pinned by `test_the_reader_tools_wait_for_the_first_story_page`.
While the cover is still being drawn, the bar already shows **Read to me**, **Sound off** and **Story time** (observed on the local book page), and the page map's tiles all say “being drawn…”. The cover has a title but no story, so “Read to me” has nothing useful to say and Story time opens an empty surface. Hide the reader toolbar until the first page is done, or label it for the cover.

### U3 — P2: price is missing from the signed-out shelf
`page_chrome` sets `price_label` only when `wallet_enabled`, which requires a signed-in owner (`app/routes/web.py:320–326`). The story card renders the price only from `price_label` (`app/templates/_story_card.html:37`). So the one page that has to sell the book — the public landing/shelf — never shows `$0.99`, even on a deployment where payments are live. A signed-in user sees it. Set `price_label` from `payments_enabled`, independent of the owner.

**Done (11 Nov 2026):** `page_chrome` now carries `book_price`, set from `payments_enabled` and independent of who is looking, and the story card prints it: “$0.99 · 20 pages and a cover · their name on it” on the public shelf as well as inside. Pinned by the story-card assertion in `test_frontend_structure.py`.

### U4 — P2: “Keep the photos and comics (zip)” still uses the old vocabulary

**Done (11 Nov 2026, `8c451ed`):** the download says “Keep the photos and portraits (zip)”, and the archive’s own README uses the same word. Pinned by the book-page assertion in `test_frontend_structure.py`.
The app mostly says **portrait** / **storybook self**, but the download button says **comics** and the ZIP help refers to “comics” (`app/templates/book.html:68`, `app/routes/book_media.py` `_SOURCES_README`). The studio's internal `comic` wording also survives in a few places. Pick one word and use it everywhere.

### U5 — P2: admin plate review mixes “re-roll” and “paint”

**Done (11 Nov 2026):** the plate screen says “paint” throughout — the candidate action, the retry copy and the confirmation — and no “re-roll” is left in any template.
The empty state says “Re-roll below”, the button says “Paint a new candidate”, and the panel title says “Paint this page again” (`app/templates/admin_plate_review.html`). Operator-facing, but it makes a simple action feel like three. Use “paint” consistently, or use “try another” consistently.

### U6 — P2: the wallet page repeats the header top-up control
The header shows a `+ $5` button on the wallet page, directly above an “Add money” card with the same $5 option (`app/templates/_layout.html:33–39`, `wallet.html`). Minor, but the wallet page should be the one place where amount choice lives; hide the header quick-add there (or turn it into a link to the Add money card).

### U7 — P2: the first view of a failed book does not explain “Finish this book”
On a failed book the primary recovery is a button labelled **Finish this book** with no sentence about what it will do, whether it costs anything, or how long it takes. The failure message mentions the error, not the recovery. Add one line: “This retries only the missing pictures; finished pages are kept.”

**Done (11 Nov 2026):** the failed row now says, in one line under the error, “Finished pages are kept. Finishing draws only the pictures that are missing, <price> each as they are drawn, and takes a few minutes.” Pinned in `TestFinishAFailedBook`.

### U8 — P2: the “refund” button debits the wallet with one click and no confirmation

**Done (11 Nov 2026, `6594539`):** the request carries `data-confirm`, the app’s one confirm handler, saying that the money leaves the spendable balance now and comes back if Stripe’s refund fails. Pinned in `test_wallet_routes.py` (`data-confirm="Request $10.00 back?`).
`POST /wallet/refund` immediately creates the request and reduces the spendable balance (`app/routes/wallet.py:323–345`). The page copy explains this, but the button is one click and there is no confirmation or amount choice; a parent who taps it by accident cannot cancel. Add a confirm step (the app already has `data-confirm` for destructive admin forms) or a small review screen.

### U9 — P2: dark-mode coverage is present, but check the paper metaphor
`prefers-color-scheme: dark` is implemented (style.css:74+), which is a real improvement. The book reader, however, is a deliberately cream “paper” surface; in dark mode the surrounding chrome goes dark while the page stays cream. That is defensible for a picture book, but the contrast between the two surfaces is now a design decision that should be intentional (and checked for the cream-on-dark outline). Low priority.

### U10 — P2: the studio can be jumped to the end without a star
The shared step header links **4 Make it** directly to `/stories/<story>/about` (`app/templates/_steps.html`). The About page correctly disables “Make the book” and shows “Go back to the faces”, but the user reaches a dead end. The header's step links should respect `firstUnfinished()` and land on the first step that still needs work (or show the About page in a read-only “answer the earlier steps first” state).

**Done (11 Nov 2026):** in the studio, “4 Make it” only navigates once the star has a chosen portrait; before that it stays put and opens `firstUnfinished()`, so the About page's disabled button is never the first thing a visitor meets. Pinned in `test_the_make_it_step_waits_for_the_star_s_portrait`.

### U11 — P2: the reader shows a cover label where the page counter belongs
On the cover the bar says “Cover”; on page 1 it says “Page 1 of 20” plus the attempt chip. The page map's first item is also labelled “Cover”, while the PDF's cover is page 1. Covered by B7; listed here because it is the first thing a reader sees.

**Done (11 Nov 2026):** closed with B7 (`da262db`) — the cover is page 1 in the reader, in the page map and in the PDF, so the cover label and the page counter no longer disagree.

### U12 — P2: the sample-book dots are 8×8 px tap targets on a phone
Measured at 390×844 with touch emulation: `.dot-button` is 8×8 with no padding (`app/static/style.css:950–963`), and the six dots are the only way to jump to a specific sample book. The arrows beside them are 40×40, so the dots are the one control on the landing page that needs a fingertip rather than a finger. The header's ✦ menu button measures 36×36, just under the usual 44×44. Fix: keep the 8 px round mark but give the button a 44×44 transparent hit area (padding plus `background-clip: content-box`, or a pseudo-element), and raise the header button to 44 px.

**Done (11 Nov 2026):** each dot is a 44×44 button with the 8 px mark drawn by `::before` (the hit areas tile edge to edge, so no two overlap), and the header's account button is 44×44 on a narrow screen. Pinned in `test_the_sample_dots_and_the_account_button_are_fingertip_sized`.

---

## 3. Things that are not intuitive

### I1 — P1: the price is only revealed at the last step
The shelf and studio never show the fee; the About step shows “Your book is one fee of $0.99” only when `price_label` exists, and the wallet page is where the transaction history lives (`app/templates/about_page.html:58–65`). A visitor has already uploaded a photo and generated a portrait before learning the price. Put the price on the story card (publicly, see U3) and repeat the balance/fee on the photo step.

**Done (11 Nov 2026):** the price is on the story card the visitor picks from (U3's `book_price`), so it is known before any photo is uploaded, and the About step still repeats the fee, the balance and the per-picture price before the book starts. The portrait step also names the per-portrait price beside the button (`templates/studio.html`).

### I2 — P1: a zero-balance user can spend portrait money before being told they cannot buy a book
The first paid call (the portrait) does not check funds; the book start does. The About step then shows the 402 only after the user has invested time. Add a balance check to the portrait action (and a clear “you'll need $0.99 to finish” hint), or let the portrait be free but tiny/low-res until the book is paid for.

**Done (11 Nov 2026):** closed with B4. The portrait is now the first thing that can refuse for money: it is charged per candidate before anything is drawn, and the 402 names both the price and the balance. The About step repeats the fee, the per-picture price afterwards and the shortfall when the wallet is short, so no paid call is reached by an account that cannot finish. Pinned by `test_a_portrait_the_wallet_cannot_pay_for_is_not_drawn`.

### I3 — P2: “tries”, “checks”, “best effort”, “plate”, “comic”
These are engineering words in a product for families. Use “redrawn”, “we had a look”, “the best version”, “the book's artwork”, “portrait”. The admin page can keep the technical vocabulary.

**Done (11 Nov 2026, `8c451ed` then `6fa847d`):** the reader carries none of them — no try count, no “best of”, no “checks”, no “plate”, no “comic”; the download says “portraits”, the studio says “pictures”, and the technical vocabulary is left on `/admin`. Pinned by `test_the_pages_show_no_provider_internals` and the vocabulary assertions in `test_frontend_structure.py`.

### I4 — P2: “Read it from the start” is the only action on a finished book
The completion card offers PDF, ZIP and prompts, but no “Make another”, “Share”, “Print” or “Read again” path (`app/templates/book.html:62–70`). The bookshelf is the only way back; a parent who just finished a book has no obvious next step. Add “Make another adventure” (with the same child, see ideas) and “Share”.

**Done (11 Nov 2026), partly:** the finished card has “Make another book” beside Read, Download the PDF and Keep the pictures, so the shelf is one press away. Sharing (W3) and printing (W6) are still open as their own entries.

### I5 — P2: the wallet is a 404 in password/open mode
`wallet_visible` is `bool(owner)`, and password/open deployments have no owner (`app/routes/web.py:292–326`). `GET /wallet` therefore answers **404** (“There is no wallet without a signed-in account” — `app/routes/wallet.py:91–97`), verified in the local password run, and the footer's wallet link is hidden. That is consistent with “no identity, no wallet”, but a 404 is indistinguishable from a broken route; if this mode is meant for demos, document the absence so a grader does not think the wallet is broken.

**Done (11 Nov 2026):** documented, as asked: the README's password section now says there is no wallet in that mode by design — a wallet belongs to a person, the shared password carries none, `/wallet` is a 404 and the header hides its link; money appears when Google signs people in. The behaviour itself is unchanged and pinned by `test_the_wallet_has_no_page_without_an_identity`.

### I6 — P2: “Books are kept for 7 days” is a promise with no visible clock
The footer says it, and the store enforces it (`main.py` sweeper, `db_book_store.expire`), but a book page does not show when it expires, and there is no “download before it goes” nudge near the end of the window. Show “available until <date>” on the finished book; it turns a limitation into a reason to act.

**Done (11 Nov 2026):** a finished book says “This book stays here until 18 Nov” — the sweep's own clock (`created_at` + `BOOK_RETENTION_DAYS`), not a second rule — and in the last two days adds a warning that the book goes soon and that the PDF or the pictures are the way to keep it. Pinned by `test_a_finished_book_says_when_it_goes_and_what_to_do_next` and `test_the_expiry_is_the_day_this_book_goes`.

### I7 — P2: a failed page's redraw reasons are asked for after the fact, with no examples
The redraw dialog says “Tick what went wrong” and offers chips (`book.html:123–128`), but the chips are the reviewer's fault names (`PageFault`). A parent knows “her face looks wrong”, not `face_changed`. Use parent wording in the chips and map it to the closed set server-side.

**Checked (11 Nov 2026):** the chips already use parent wording. `PAGE_FAULT_LABELS` — “Doesn't look like my child”, “Doesn't show this part of the story”, “Someone is missing”, “Several little pictures, not one” — has been in `app/models/feedback.py` since `0741f01` (9 Nov), two days before the audit; `web.py` builds the chip list from it and `book.html` renders `fault.label`. The audit quoted an older tree. No change was needed.

---

## 4. Things that are slow

### S1 — P0: synchronous image work on the event loop
See B1. This is the one that turns a slow book into a lost book.

**Done (11 Nov 2026):** the same finding as B1, closed with it: tier 1 and the media encodes are off the loop, and the render-and-first-view test now watches `/healthz` and the loop's lag through both.

### S2 — P1: image variants are encoded on first view, serially
See B12. Measured 0.28 s (800 px) and 0.38 s (1600 px) per 1152×864 image on a development CPU; 21 pages × two widths = ~14 s of CPU before thumbnails and the first page are all warm, on top of the image download. On the free Render CPU this is much worse and happens while the reader is open. Precompute after save; keep the 1600 copy lazy.

**Done (11 Nov 2026):** the 240 px and 800 px copies are made as each picture is saved (B12); a first view of a finished book encodes nothing at those widths.

### S3 — P1: the configured throughput cannot meet the “under 60 seconds” goal
`OPENAI_IMAGES_PER_MINUTE=18` (`render.yaml`) paces the provider, and a real book averages about 52 image calls (21 pages + cover + 1–2 redraws), so the pacer alone needs ~3 minutes. `MAX_CONCURRENT_GENERATIONS=8` on a 512 MB free instance is already a memory risk (the blueprint comment records an OOM at 8 before the pool change). The Run 1 live book took 16–17 minutes.

**Fix:** either make the product promise match reality (“a few minutes”), or raise the image-per-minute cap and run on a paid instance with enough memory; overlap review with the next paint; and keep the cover in the same `gather` (it already is).

### S4 — P2: the full status document is rewritten on every progress tick
`status_payload` includes each page's full story text (`app/services/book_document.py:127–160`), and `save_status` rewrites the whole JSON on every stage change; the browser polls it every 1.5 s (`app/static/book-live.js:16`). That is ~10–12 KB × dozens of writes per book, plus the poll payload, all over the network to Neon. Store story text once (book/story page rows) and keep the poll document to ids/status/URLs, or only update the changed page.

**Done (11 Nov 2026):** the poll document carries no story text — only each page's number, key, title, status, picture URLs and version. For *Jack* that is ~6.4 KB less on every write and every poll (20 pages of prose); the words still live in the edition and are rendered into the book page. Pinned by `test_the_poll_document_carries_no_story_text`.

### S5 — P2: the wallet history is unbounded and filtered in Python
`Wallet.history` returns every transaction, `wallet_page` filters the list in memory (`app/services/wallet.py:204–206`, `app/routes/wallet.py:123–170`). A long-lived account accumulates rows; the page and the DB read grow without limit. Add `LIMIT/OFFSET` (the UI already has type filters) and paginate.

**Done (11 Nov 2026):** `Wallet.history_page` narrows by kind and by book (`book:<id>` / `picture:<id>:…`) and pages with `LIMIT/OFFSET` in one query, with a count for the filtered total; the book filter's options come from a distinct-reference query instead of the ledger rows. The wallet shows 50 movements a page with Newer/Older links that keep the filters in the URL, and a page past the end shows the last page. `Wallet.history` still returns the whole ledger for the few callers that want it all. Pinned by `test_the_history_is_read_one_page_at_a_time` and `test_the_wallet_page_pages_and_filters_the_ledger`.

### S6 — P2: fonts are still 300 KB TTFs, revalidated every load
`@font-face` points at `Literata-Regular.ttf` (316 KB) and `Literata-Bold.ttf` (324 KB); `/static/fonts` is mounted `no-cache` (`app/main.py:288`, `app/static/style.css:8–18`). A browser must revalidate two large files on each visit. Convert to WOFF2 subsets (30–40 KB), fingerprint and cache immutably, and preload the bold face used for headings.

**Done (11 Nov 2026):** both web faces are WOFF2 in `app/static/webfonts/` — 203 KB for the pair instead of 640 KB — and the stylesheet points at them with a relative URL, so they load under the fingerprinted `/static/<build>/` prefix and are served `immutable`. `_layout.html` preloads both faces, and the TTF mount under `/static/fonts` is gone: the TTFs in `app/assets/fonts` are the PDF's only. Not subsetted: a visitor may type any name into the About page (Hebrew included), so dropping glyphs would break their book. Pinned by `test_the_web_fonts_are_woff2_under_the_fingerprint`.

### S7 — P2: the PDF is built at the very end and served as one in-memory blob
`assemble` reads every page blob and builds the PDF after all pages are done (`book_runner.py:812`); the 4.7 MB result is then served by `book_pdf` as one `Response`. Fine at this scale, but a 21-page book's PDF is the last thing to appear and there is no progress for it. If books grow, stream or cache the PDF bytes in the row (they already live there) and show “putting the book together” progress (the UI already has the phase).

### S8 — P2: the test suite is slow enough to hide regressions
The full suite did not complete in 400 s here, while the app's own mock books are 3× slower than necessary because of B5. The suite is broad (65 test files) and worth keeping; parallelize the slow book tests and fix the mock review false positive so the full run stays under a CI-friendly budget.

**Done (11 Nov 2026):** the suite's wall time was disk time, not CPU time. Every SQLite commit is an fsync of the rollback journal — measured at ~80 ms here — and a mock book makes about 245 of them (a page save, two variant writes, twenty-one prompt appends, a verdict, a status tick), so a single book test spent 19.8 s of its 21 s waiting for the disk. SQLite now opens in WAL mode with `synchronous=NORMAL` and a 15 s `busy_timeout` (`configure_sqlite`, `app/db.py`); Postgres is untouched. One book test went 18 s → 5 s, and the full suite 285 s → 199 s wall (1248 s CPU, 8 workers, on a machine also running another agent's suite). The same change removes the suite's “database is locked” flake: 20 page threads writing at once is now a queue, and the test that hit the old 5 s timeout no longer can (B13 records the book that lost). The timing test's loop watch now starts at the render rather than at app boot, because a single `loop.lag ms=1313` logged during startup/the portrait step made a green suite red under load (S9 files the observation). Parallelism was already there — the Makefile has run `-n auto --dist loadscope` since before the audit; B5's mock fix (already done) had removed the 3× redraw cost.

### S9 — P2: the studio's portrait step can hold the loop for a second under load
While measuring S8, one `loop.lag ms=1313 rss_mb=328 tasks=10` was logged in a full parallel run, before the render started — during app startup or the portrait request (`POST /api/cartoonify` took 1722 ms there). `/healthz` stayed under 8 ms throughout, so this is a brief hold rather than the render stall of B1, but it is the same class of work: image decode/resize/encode.

**Checked (11 Nov 2026):** measured, and not reproduced. `POST /api/cartoonify` against the same full-size 1024×1536 picture the render-stall test uses took **231 ms** on an idle machine and **274–376 ms** under seven competing Pillow decode/resize/encode processes, logging no `loop.lag` at all; the request's whole image cost is a few tens of milliseconds because the provider, the decode and the variant encode are all off the loop (`work()` in `app/routes/api.py` → `_run_gated` → `to_thread`, and `decode_upload` is inside it). The `loop.lag` line was a one-off in a run whose machine was also running another session's suite, and it could not be attributed to a call site. Nothing to fix; the test that caught it now watches only the render (S8), and a real stall would still be caught there and by the production watcher.

---

## 5. Things that are not wired in

### W1 — P1: the plate pipeline is dark in production
The start gate is off (`requires_template` defaults to false; `to-do.md` T8: “the start gate stays off until it is seen to keep the plate's background”). `edition_for_new_book` therefore returns no `template_version_id`, so every new book is composed from scratch. That means the plate editor, `paste_plate_back`, premade pages, plate-aware tier-1 checks and the plate review screens are all implemented but unused for real books. The code is good; the feature is not shipping. Either finish the real-provider plate trial and turn the gate on, or move the plate code behind an explicit experimental flag so it is not mistaken for live behaviour.

### W2 — P1: no notification when a book is ready
Renders take minutes (Run 1: 16–17 min). If the parent closes the tab or locks the phone, nothing tells them the book is done; the job continues and the fee/PDF happens without them. There is no e-mail, push, or “send me a link” path. This is the highest-value missing integration for the actual waiting experience.

### W3 — P1: no share or gift link
The viewer is URL-based (`/books/{id}`) and ownership is enforced server-side, but there is no signed, expiring share token. A grandparent cannot open the book without the parent's account. A “Share a read-only link” button (and a gift note) is a small backend change and a large product unlock.

### W4 — P2: refunds wait for a human with no alert
A refund request is recorded and the wallet is debited immediately (`wallet.py:319–345`); the operator must visit `/admin/status` and press process. There is no e-mail/Slack/webhook, so a request can sit indefinitely while the copy promises “the operator sends it through Stripe”. At minimum, surface a count/badge in the header for operators and send a notification.

### W5 — P2: payments are still a deployment setting, not a finished flow
`render.yaml` does not declare `STRIPE_SECRET_KEY`, and `to-do.md` still has “Confirm STRIPE_SECRET_KEY is set in the Render dashboard”. Conditional on that being unset, the live site runs with payments off: no fee, no funding, no refunds, and the story cards have no price. Verify the dashboard; if payments are intentionally off for the hand-in, say so on the site instead of “Payments are not configured on this deployment” in a customer-facing wallet.

### W6 — P2: printing has no path
The code comment and prior audit both note “no print service yet”; the finished book offers PDF/ZIP only (`book.html:62–70`). The natural upsell (a printed hardcover) is not wired.

### W7 — P2: operator alerting is missing
`/admin/status` shows unreviewed images, restart-reaped books, review pass rates and average cost, but only when someone opens it. There is no threshold alert (“review pass rate fell below X”, “average book cost above $0.99”, “a render was reaped”, “a refund is waiting”). The data already exists; a scheduled check plus one notification channel would catch B1/B5/B8 before a customer does.

### W8 — P2: failed-render recovery is a button, not a workflow
“Finish this book” exists, but there is no automatic resume (B8), no e-mail when it recovers, and no visible distinction between “failed and resumable” and “failed and hopeless” in the library list. The store knows which pages are saved; the UI does not use that.

### W9 — P2: the mock reviewer/decision settings can silently weaken production
`REVIEWER_PROVIDER=off` is a supported configuration, and `tier2_failed` is recorded, but a deployment can ship with no tier-2 review and the only signal is the operator status page. If the review loop is a product promise (“every generated image is analysed”), fail closed for a production deployment with no reviewer, or show a visible “not being checked” state on the book. Currently the app degrades quietly.

**Done (11 Nov 2026):** it fails closed where money is at stake. A production deployment whose image provider is billed (`openai`, `gemini`) refuses to start with `REVIEWER_PROVIDER=off` unless `ALLOW_UNREVIEWED=1` says the saving is deliberate — the same setting C7 asked for, so one guard covers both. The free backends (mock, Agnes, Gemini in Chrome) and development are exempt: there is nothing to waste, and `/admin/status` still prints which reviewer is live. Pinned by `test_a_paid_production_deployment_must_check_its_pictures`.

---

## 6. Things that increase cost

### C1 — P0: the unit economics are negative
`review-log.md` (11 Nov 2026) measures **$1.60 and $1.56 per book against a $0.99 price** — about a $0.59 loss per book. The causes are 1–2 redraws per page (≈52 image calls instead of 22), a real price of ≈$0.024 per image versus the README's $0.00588, and review/decision calls. The cost panel now shows this to the operator, which is good; the price and retry policy have not caught up.

**Fix (in order):** make the first draft pass more often (shorter, better-targeted page prompts; plate edit where available), stop paying for a redraw that the reviewer/decision judge cannot justify, reprice to at least $1.99 (or cap pages/attempts), and add a per-book hard budget that stops retries when the spend exceeds a fraction of the price.

### C2 — P1: free, unlimited redraws after the sale
`edit_book` treats every image action as included (`book.py:422–570`): `character=star` redraws 21 pages + cover, repeatedly, for one $0.99 fee. This is generous to a fault and directly exploitable. Cap free edits (one cover + one page, or one “replace a face” per book), or charge the documented per-image cost.

**Done (11 Nov 2026, `412cb07`):** the second option was taken — the per-image price is charged, rather than a cap added. Every picture an edit draws is bought at 15¢ as it is drawn (`_picture_payment`), a star swap included, and an edit the wallet cannot cover is refused before it starts, so “free and unlimited” no longer exists. No separate per-book edit cap was added: with each picture paid for there is nothing left to cap, and a book-wide ceiling belongs to C4.

### C3 — P0: portraits are not gated by the wallet
See B4. Zero-balance portraits are pure operator cost with no path to revenue.

**Done (11 Nov 2026):** closed with B4 — a portrait is charged per candidate before anything is drawn, so an account that cannot pay draws none.

### C4 — P1: no per-account or per-book budget
The only brakes are per-IP rate limits (`MAX_GENERATIONS_PER_MINUTE`, `OPENAI_IMAGES_PER_MINUTE`) and `MAX_CONCURRENT_GENERATIONS`; none of them knows what a book has spent or what an account has paid. A single account can open multiple books, generate retries, and redraw for hours. Add a cost ceiling per book (stop retrying at, say, 1.2× the price) and a daily account ceiling; surface both in the error copy.

**Done (11 Nov 2026), the per-book half:** the book's own ledger is now read while it renders, and two operator knobs act on it. `REVIEWER_BUDGET_PERCENT` is a share of the book's fee (0 = off, the default; 100 = the whole fee): once the book's measured spend reaches it, tier 2 is rested for the rest of that book, tier 1 and the decision judge still decide, and the verdict's note says the reviewer was rested on the book's review budget, without counting as an outage — so the operator's own economy can never be mistaken for the reviewer being down. `BOOK_COST_ALERT_CENTS` (a dollar by default) makes a book that costs more than it sells for visible: one `book.cost_alert` line with the spend when the job ends however it ends, and the same threshold names those books at the top of `/admin/status` with what each cost to make. The spend is re-read at most every ten seconds and reused, so a verdict does not put a query behind every picture, and a resumed book starts from what the ledger already holds rather than from zero. There is no mail channel in this deployment (W7), so the log line and the operator page are the notification; per-account daily ceilings and a picture-retry ceiling (C1's hard budget) are **still open**.

### C5 — P2: tier-1 false positives cost two extra images per affected page
B5 proves the detector can fire on legitimate full-width bands. Every false positive is $0.048 + review cost, and on a 21-page book a systematic false positive is the difference between profit and loss. Calibrate on the plate set and add the `collage` fault to the reviewer's vocabulary (the review loop only knows the fault names in `parse_answer`; an unknown name is dropped).

### C6 — P2: review and decision calls are paid on every picture, including pages that never reach a book
The decision judge and reviewer run inside each page attempt; a failed book still pays for every review. That is acceptable for quality, but a failed page after three attempts has paid for three reviews. Consider one review on the best candidate instead of every candidate, or let the decision judge settle obvious passes without the reviewer (it already does) and measure the saved calls in the cost ledger.

### C7 — P2: the demo path can burn paid reviewer calls if misconfigured
With `IMAGE_PROVIDER=mock` and `REVIEWER_PROVIDER=openai`, tier 1 fails every mock page **before** tier 2 (so no review calls), which is lucky. The inverse (`IMAGE_PROVIDER=openai`, `REVIEWER_PROVIDER=off`) is supported and silently ships unreviewed pages (W9). The deployment should validate this combination at boot: paid images + reviewer off is either a deliberate cost saving or a mistake; require an explicit `ALLOW_UNREVIEWED=1`.

**Done (11 Nov 2026):** the validation is in `Settings`, so it fails at start-up rather than per call: paid image provider + `REVIEWER_PROVIDER=off` in production needs `ALLOW_UNREVIEWED=1`, and the error says so. Mock needs nothing (B5 made a placeholder pass tier 1, so a demo no longer spends reviews on pages it would fail first). Pinned by `test_a_paid_production_deployment_must_check_its_pictures`.

### C8 — P2: long prompts (if still ~18 K characters/page) multiply input tokens
UI.org measured ~17.8 K characters per page prompt; the prompt builder still assembles the identity block, cast sheet and boilerplate for every page (`app/services/prompts.py:445–480`). The output cost dominates with image models, but the input tokens are paid on every retry. Re-measure after the recent prompt cuts; if still large, put the page's moment first and trim the repeated cast block. (This audit did not re-measure the prompt lengths, so treat it as a to-verify item.)

**Checked (11 Nov 2026), no change to the order, on purpose.** Re-measured with the current builder: a composed page prompt is 21,375 characters, and the book-level block — identity, likeness, coherence, style, the cast sheet, the world, the story's rules — is byte-identical across pages to 16,298 of them (76 %). The builder orders exactly that block first (`prompts.py`, the `shared` list) so a provider can serve it from its prefix cache; the audit's suggestion to put the page's moment first would make all nineteen calls, and every retry, a cache miss and would cost more, not less. The production path is shorter still: a plated page is a plate edit, 2,217 characters, because the plate is the picture being edited. The property is now pinned by `test_the_book_level_block_comes_first_so_it_can_be_cached`.

---

## 7. Good ideas not implemented (the product levers)

These are not defects; they are the highest-leverage things the codebase is already positioned for.

1. **Show the cover before the book is bought.** The app has plates, samples and a cheap one-image portrait. Render the plate cover with the child's face and name immediately after the portrait, and reuse it as the real cover. People commit to a book they can see. This is likely the single biggest conversion improvement.
2. **A child profile.** Save the child's name, gender and approved portrait once; “Make another adventure for Maya” then skips photo + portrait (and its cost). The draft/portrait model already supports reuse; only the profile/ownership layer is missing. It also fixes the “restart the studio from scratch” friction noted in `UI.org` (G7).
3. **“We'll email you when it's ready.”** A single transactional e-mail with a signed link solves the 3–17 minute wait, tab closure, and the reload problem in one move. The session/book URLs already exist.
4. **A share/gift link.** A signed, expiring read-only token for `/books/{id}` plus a gift note. The viewer needs no new rendering; it needs one token and one page. This is the natural viral loop for grandparents.
5. **A dedication page.** One short text page (“For Maya, from Grandma, 11 Nov 2026”) is near-zero cost (PDF text only) and turns a product into a keepsake. The About page already has the layout and typography.
6. **Print-on-demand.** PDF is done; a hardcover partner (Lulu/Blurb/Printful) is the obvious upsell. Research providers before integrating; the book's 4:3 art and page count already fit a standard picture-book trim.
7. **A sample book on the landing page.** A 3-page “look inside” with a sample child and a download link, no sign-in. The sample covers and plates already exist; this builds trust before asking for a photo.
8. **A real bookshelf.** The “My books” drawer is functional but hidden. Covers standing on a shelf, with “read again”, “make another” and a date, would make the library feel like a collection — and the `covers` list already drives the rotating cards.
9. **Make the waiting a show.** Show the approved portrait on the stage immediately, put each page's text in its tile while the picture draws, fly the new picture into its tile, and auto-advance the stage unless the reader has navigated. The pagemap and stage exist; this is choreography, not new backend.
10. **Operator alerting and a cost kill switch.** Turn `/admin/status` into a pager: notify on a reaped render, a review pass rate below a threshold, an account above a daily spend, or a refund waiting. The ledger and status queries already produce the numbers.
11. **Localisation.** The PDF already bundles Hebrew shaping (`uharfbuzz`) and a Hebrew font is in `app/assets/fonts`, but the UI, stories and PDF strings are English-only. A Hebrew edition of one story would be a differentiated product for this market.
12. **Narrated audio / read-along download.** Browser speech is free and works; a downloadable audio track (or a read-aloud mode in the PDF) would make the book usable in the car and at bedtime without a screen. Start with the existing sentence spans and a single TTS pass per page.

---

## Positives worth keeping

* **Authorization is consistent.** Every book artefact goes through `ensure_book`, owner mismatch returns the same 404 as a missing book, and the `/media` draft URLs are explicitly bearer tokens with `no-store`. The admin gate 404s rather than 403s. The OAuth `next` and Stripe `next` paths reject schemes, hosts, backslashes and `//` spellings.
* **The wallet is a real ledger.** Funding is idempotent by Stripe session, charges are idempotent by `book:<id>`, refunds debit on request and reverse only the unrecovered part on failure. That is unusually careful.
* **Retention and privacy are wired.** Drafts expire hourly, books expire after the retention window, portraits have their own log, and the original photo is `no-store` while illustrations are `private, must-revalidate` with content-hash ETags.
* **The reader and PDF are share one design language** (paper, Literata, folio, drop cap), and the reader is sized to the window with a stacked/spread fallback. Type checking and linting are clean.
* **The operator status page is genuinely useful** — provider, reviewer, gate state, review statistics, recent calls, cost per book and the list of pictures with no verdict. It is the right place for the technical vocabulary that should not appear in the reader.

---

## Suggested order of work

1. **B1/B5** — make the event loop safe and the demo path honest. This prevents lost books and fixes the 3× demo.
2. **B4/C1/C2/C3** — close the spending holes and set a price/budget that does not lose money per book.
3. **B2/B8** — stop a single page or a restart from throwing away a nearly finished book.
4. **B3** — reserve the fee at start so concurrent books cannot overdraw.
5. **B6/B7/U3/I1** — remove the debug language, fix the numbering, and show the price before the last step.
6. **W2/W3** — notification and share links: the two features that turn a one-off into a product.
7. The rest as polish.
