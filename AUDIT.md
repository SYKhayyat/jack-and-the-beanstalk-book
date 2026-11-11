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
| P1 | 12 | Significant bug / cost / usability damage |
| P2 | 17 | Polish, edge cases, maintainability |
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

### B2 — P0: one failed page aborts a finished-but-uneven book and no PDF is produced
`asyncio.gather(..., return_exceptions=True)` collects every page, then `fatal = [...]` and `raise fatal[0]` (`app/services/book_runner.py:829–831`). A single provider timeout, moderation block that never recovers, or reviewer error therefore throws away the other 20 pages' progress from the user's point of view: no PDF, and the book is marked failed. Completed page rows are kept, and “Finish this book” can retry the missing pages, but the user has to discover and press it, and the render time/cost already spent is lost.

**Fix:** keep a page-level failure and assemble the book from the pages that exist (a clearly marked “couldn't draw this one” page), or retry provider errors once automatically before failing the whole job. The UI already has a per-page `failed` state and a retry action, so the backend should match it.

### B3 — P1: the wallet can go negative when two books are started at once
`_require_funds` runs at start (`app/routes/book.py:366`), but the actual charge happens when the PDF is written with `require_funds=False` (`app/services/book_document.py:169–186`). Two tabs (or two requests) with exactly $0.99 both pass the check; both books finish; the ledger ends at −$0.99. This is documented as “the account owes the difference”, but nothing ever collects it: the next book is refused until the user tops up, which is fine for an honest user and a loss for anyone who does not.

**Fix:** reserve/hold the fee atomically at start (a `KIND_HOLD` row or a `book:<id>` pending charge), release it when the job fails and apply it when the PDF is written; or refuse the second start if `balance − open_books × price < price`.

### B4 — P0 (cost): a $0 account can spend real money on portraits, and any buyer gets unlimited redraws
Two gaps combine into an open spending tap:

* `POST /api/cartoonify` (`app/routes/api.py:83`) has no funds check. It only admits against the per-IP rate limiter (`api.py:131`). A signed-in account with a zero balance can upload a photo and generate candidate portraits all day (1–3 images per call); those calls are billed to the operator and most will never lead to a book.
* Every edit after a book exists is free by design (`app/routes/book.py:422–570`): `action=character` with `character_key=star` redraws the whole book plus the cover, up to three attempts per page, and can be repeated indefinitely. `edit_book` has no funds check, no per-book edit budget, and the rate limiter is only a per-IP abuse brake.

With `MAX_GENERATIONS_PER_MINUTE=200` and roughly $0.024 per image, a determined account can spend tens of dollars an hour without ever paying $0.99.

**Fix:** require a wallet balance (or take a small hold) before a paid portrait, cap free edits per book (e.g. one page redraw and one cover redraw), and add a per-account/per-day generation budget separate from the per-IP rate limit. At minimum, alert the operator when one account crosses a cost threshold.

### B5 — P1: mock/demo mode is deterministically “broken” by the tier-1 collage check
Every page the mock provider draws gets `collage` from tier 1. Verified directly:

```text
review_image(mock_page) -> False 30 ('collage',)
```

and in the local logs every page of every demo book was reviewed as `faults=collage score=30 outcome=failed` (21 pages + cover), then redrawn three times and marked best-effort. The cause is structural: the mock draws full-width dark banners at the top and bottom (`app/services/image_providers/mock.py`, `_banner`), and tier 1 treats two full-width seams as a contact sheet (`app/services/image_review.py`, `COLLAGE_SEAMS = 2`).

**Impact:** demo books are 3× slower/noisier, show “Best of 3 tries” on every page, and the demo cannot be used to judge the normal happy path. With a real provider, the same false positive costs two extra page images every time a real page has two legitimate full-width seams (a horizon plus a table edge, a bed, a window frame).

**Fix:** skip tier 1/2 for `provider.draws_placeholders` (or add a `review=False` to `ImageRequest`), and make the mock banners not span the full width. Add a test that mock pages pass review. Longer term, make the seam detector ignore a small set of strong horizontal bands or require a grid of seams in both directions.

### B6 — P1: every page badge exposes internal quality-control language, and one string is ungrammatical
`app/static/book-live.js:447–501` writes the attempt record into the visitor's UI:

* `Try 1 of 3` / `Try 2 of 3` on a normal page.
* `Best of 1 tries` — observed in the local book page — is both plural-wrong and meaningless (one try is not “best of”).
* `3 of 3 tries — none passed every check, so the best one (try 3) is shown` on a best-effort page.
* `3×` in every page-map tile, even when the page passed on the first try.

The book is a gift for a child; “checks”, “tries” and “best effort” are an operator's vocabulary. They also make a perfectly good page sound defective.

**Fix:** hide the chip for `attempts === 1`; for retries say something human (“Redrawn once”) or move the whole record to the operator page; never show “Best of 1 tries”. Keep the detailed record in `/admin/books/{id}` where it belongs.

### B7 — P2: the page number and the printed folio disagree on the same page
The first story page is labelled `Page 1 of 20`, while its printed folio is `2` (`app/templates/book.html:32`, `app/static/book-live.js:90–93`). The cover is counted as page 1 in the PDF folio (`app/services/pdf.py:386–390`) but not in the reader's page counter, and the shelf copy says “20 pages and a cover”. Three numbering schemes are visible at once.

**Fix:** pick one: “Page 2 of 21” with the cover as 1, or folio `1` for the first story page. Apply the same rule to the PDF, the reader label and the page map.

### B8 — P1: a restart does not resume a book; it fails it, even when most pages are saved
`reap_incomplete()` (`app/services/db_book_store.py:745`) marks all `PENDING`/`RENDERING` books failed at boot. The message says the finished pages were kept, and “Finish this book” can retry the missing ones — but the user must click it, and the rendering time already paid for is not reused automatically. The Run 2 incident lost 14 of 21 pages and $0.36 of spend this way.

**Fix:** on boot, re-enqueue failed books that have a cover or at least one saved page and are still inside their retention window, or at least surface “resume” prominently on the failed book page. This turns a restart from a loss into a delay. It also needs the multi-worker caveat in B9.

### B9 — P1 (scaling): single-process assumptions are load-bearing
`reap_incomplete()` fails *all* pending/rendering books on every boot, including books another live worker may be rendering. `_start_lock`, `_active` jobs, `BookRunner._reviewer_rests_until`, the rate limiter and `Wallet._lock` are all in-memory. With more than one Render instance or Uvicorn worker, duplicate starts, double charges and cross-worker reaping are possible. The blueprint currently runs one free instance, so this is a latent scaling bug, not a live incident.

**Fix:** document/enforce a single instance, or move job state, locks and leases to the database (a lease/heartbeat column and a `SELECT ... FOR UPDATE` start transaction).

### B10 — P2: an About-page edit during a render can be silently overwritten
`action=about` updates the record and only rebuilds the PDF if the book is already complete (`app/routes/book.py:459–490`). A render in progress has already loaded the old `BookRecord` and assembles the PDF from that snapshot, so an About edit made mid-render is lost when the job finishes. The UI hides the edit strip while rendering, so this needs a direct API call, but it is a real data-loss path.

**Fix:** reject `about` edits (409) while an edit/render is active, or make the render re-read the record before assembly.

### B11 — P2: the PDF can silently truncate the About page while the web reader shows all of it
`_fit_text` shrinks the About body and, at the floor, drops lines and appends an ellipsis (`app/services/pdf.py:518–540`). The About form allows 1,000 characters of “details”, and the web About spread renders all of it. A family who writes a long dedication gets a different (shorter) book in the download.

**Fix:** warn and limit at the form (or add a dedicated “dedication” page), or paginate the About text across a second PDF page so the PDF matches the reader.

### B12 — P2: a demo/mock book's first paint is slow because variants are built on demand
On the first view of any newly completed book, every image URL is a `?w=1600&v=…` URL; the server creates and stores each WebP variant when first requested. A 21-page book therefore pays 42 encodes (800/1600) plus 21 thumbnails, one request at a time, with no background warm-up (`app/routes/book_media.py:227–260`). On a slow instance the page turns feel stalled even though the images are local.

**Fix:** generate the 800 and 240 variants immediately after `save_page`/`save_cover` (in the image pool), and keep the 1600 copy lazy; or make variant generation part of the book's “assembling” phase.

---

## 2. Display and UI

### U1 — P1: the attempt record is on the reader, not hidden
See B6. The chip sits next to “Page 1 of 20” and on every tile; on the cover it can read “Best of 1 tries”. This is the most visible “debug UI” in the product.

### U2 — P2: the progress screen shows reader tools before there is anything to read
While the cover is still being drawn, the bar already shows **Read to me**, **Sound off** and **Story time** (observed on the local book page), and the page map's tiles all say “being drawn…”. The cover has a title but no story, so “Read to me” has nothing useful to say and Story time opens an empty surface. Hide the reader toolbar until the first page is done, or label it for the cover.

### U3 — P2: price is missing from the signed-out shelf
`page_chrome` sets `price_label` only when `wallet_enabled`, which requires a signed-in owner (`app/routes/web.py:320–326`). The story card renders the price only from `price_label` (`app/templates/_story_card.html:37`). So the one page that has to sell the book — the public landing/shelf — never shows `$0.99`, even on a deployment where payments are live. A signed-in user sees it. Set `price_label` from `payments_enabled`, independent of the owner.

### U4 — P2: “Keep the photos and comics (zip)” still uses the old vocabulary
The app mostly says **portrait** / **storybook self**, but the download button says **comics** and the ZIP help refers to “comics” (`app/templates/book.html:68`, `app/routes/book_media.py` `_SOURCES_README`). The studio's internal `comic` wording also survives in a few places. Pick one word and use it everywhere.

### U5 — P2: admin plate review mixes “re-roll” and “paint”
The empty state says “Re-roll below”, the button says “Paint a new candidate”, and the panel title says “Paint this page again” (`app/templates/admin_plate_review.html`). Operator-facing, but it makes a simple action feel like three. Use “paint” consistently, or use “try another” consistently.

### U6 — P2: the wallet page repeats the header top-up control
The header shows a `+ $5` button on the wallet page, directly above an “Add money” card with the same $5 option (`app/templates/_layout.html:33–39`, `wallet.html`). Minor, but the wallet page should be the one place where amount choice lives; hide the header quick-add there (or turn it into a link to the Add money card).

### U7 — P2: the first view of a failed book does not explain “Finish this book”
On a failed book the primary recovery is a button labelled **Finish this book** with no sentence about what it will do, whether it costs anything, or how long it takes. The failure message mentions the error, not the recovery. Add one line: “This retries only the missing pictures; finished pages are kept.”

### U8 — P2: the “refund” button debits the wallet with one click and no confirmation
`POST /wallet/refund` immediately creates the request and reduces the spendable balance (`app/routes/wallet.py:323–345`). The page copy explains this, but the button is one click and there is no confirmation or amount choice; a parent who taps it by accident cannot cancel. Add a confirm step (the app already has `data-confirm` for destructive admin forms) or a small review screen.

### U9 — P2: dark-mode coverage is present, but check the paper metaphor
`prefers-color-scheme: dark` is implemented (style.css:74+), which is a real improvement. The book reader, however, is a deliberately cream “paper” surface; in dark mode the surrounding chrome goes dark while the page stays cream. That is defensible for a picture book, but the contrast between the two surfaces is now a design decision that should be intentional (and checked for the cream-on-dark outline). Low priority.

### U10 — P2: the studio can be jumped to the end without a star
The shared step header links **4 Make it** directly to `/stories/<story>/about` (`app/templates/_steps.html`). The About page correctly disables “Make the book” and shows “Go back to the faces”, but the user reaches a dead end. The header's step links should respect `firstUnfinished()` and land on the first step that still needs work (or show the About page in a read-only “answer the earlier steps first” state).

### U11 — P2: the reader shows a cover label where the page counter belongs
On the cover the bar says “Cover”; on page 1 it says “Page 1 of 20” plus the attempt chip. The page map's first item is also labelled “Cover”, while the PDF's cover is page 1. Covered by B7; listed here because it is the first thing a reader sees.

### U12 — P2: the sample-book dots are 8×8 px tap targets on a phone
Measured at 390×844 with touch emulation: `.dot-button` is 8×8 with no padding (`app/static/style.css:950–963`), and the six dots are the only way to jump to a specific sample book. The arrows beside them are 40×40, so the dots are the one control on the landing page that needs a fingertip rather than a finger. The header's ✦ menu button measures 36×36, just under the usual 44×44. Fix: keep the 8 px round mark but give the button a 44×44 transparent hit area (padding plus `background-clip: content-box`, or a pseudo-element), and raise the header button to 44 px.

---

## 3. Things that are not intuitive

### I1 — P1: the price is only revealed at the last step
The shelf and studio never show the fee; the About step shows “Your book is one fee of $0.99” only when `price_label` exists, and the wallet page is where the transaction history lives (`app/templates/about_page.html:58–65`). A visitor has already uploaded a photo and generated a portrait before learning the price. Put the price on the story card (publicly, see U3) and repeat the balance/fee on the photo step.

### I2 — P1: a zero-balance user can spend portrait money before being told they cannot buy a book
The first paid call (the portrait) does not check funds; the book start does. The About step then shows the 402 only after the user has invested time. Add a balance check to the portrait action (and a clear “you'll need $0.99 to finish” hint), or let the portrait be free but tiny/low-res until the book is paid for.

### I3 — P2: “tries”, “checks”, “best effort”, “plate”, “comic”
These are engineering words in a product for families. Use “redrawn”, “we had a look”, “the best version”, “the book's artwork”, “portrait”. The admin page can keep the technical vocabulary.

### I4 — P2: “Read it from the start” is the only action on a finished book
The completion card offers PDF, ZIP and prompts, but no “Make another”, “Share”, “Print” or “Read again” path (`app/templates/book.html:62–70`). The bookshelf is the only way back; a parent who just finished a book has no obvious next step. Add “Make another adventure” (with the same child, see ideas) and “Share”.

### I5 — P2: the wallet is a 404 in password/open mode
`wallet_visible` is `bool(owner)`, and password/open deployments have no owner (`app/routes/web.py:292–326`). `GET /wallet` therefore answers **404** (“There is no wallet without a signed-in account” — `app/routes/wallet.py:91–97`), verified in the local password run, and the footer's wallet link is hidden. That is consistent with “no identity, no wallet”, but a 404 is indistinguishable from a broken route; if this mode is meant for demos, document the absence so a grader does not think the wallet is broken.

### I6 — P2: “Books are kept for 7 days” is a promise with no visible clock
The footer says it, and the store enforces it (`main.py` sweeper, `db_book_store.expire`), but a book page does not show when it expires, and there is no “download before it goes” nudge near the end of the window. Show “available until <date>” on the finished book; it turns a limitation into a reason to act.

### I7 — P2: a failed page's redraw reasons are asked for after the fact, with no examples
The redraw dialog says “Tick what went wrong” and offers chips (`book.html:123–128`), but the chips are the reviewer's fault names (`PageFault`). A parent knows “her face looks wrong”, not `face_changed`. Use parent wording in the chips and map it to the closed set server-side.

---

## 4. Things that are slow

### S1 — P0: synchronous image work on the event loop
See B1. This is the one that turns a slow book into a lost book.

### S2 — P1: image variants are encoded on first view, serially
See B12. Measured 0.28 s (800 px) and 0.38 s (1600 px) per 1152×864 image on a development CPU; 21 pages × two widths = ~14 s of CPU before thumbnails and the first page are all warm, on top of the image download. On the free Render CPU this is much worse and happens while the reader is open. Precompute after save; keep the 1600 copy lazy.

### S3 — P1: the configured throughput cannot meet the “under 60 seconds” goal
`OPENAI_IMAGES_PER_MINUTE=18` (`render.yaml`) paces the provider, and a real book averages about 52 image calls (21 pages + cover + 1–2 redraws), so the pacer alone needs ~3 minutes. `MAX_CONCURRENT_GENERATIONS=8` on a 512 MB free instance is already a memory risk (the blueprint comment records an OOM at 8 before the pool change). The Run 1 live book took 16–17 minutes.

**Fix:** either make the product promise match reality (“a few minutes”), or raise the image-per-minute cap and run on a paid instance with enough memory; overlap review with the next paint; and keep the cover in the same `gather` (it already is).

### S4 — P2: the full status document is rewritten on every progress tick
`status_payload` includes each page's full story text (`app/services/book_document.py:127–160`), and `save_status` rewrites the whole JSON on every stage change; the browser polls it every 1.5 s (`app/static/book-live.js:16`). That is ~10–12 KB × dozens of writes per book, plus the poll payload, all over the network to Neon. Store story text once (book/story page rows) and keep the poll document to ids/status/URLs, or only update the changed page.

### S5 — P2: the wallet history is unbounded and filtered in Python
`Wallet.history` returns every transaction, `wallet_page` filters the list in memory (`app/services/wallet.py:204–206`, `app/routes/wallet.py:123–170`). A long-lived account accumulates rows; the page and the DB read grow without limit. Add `LIMIT/OFFSET` (the UI already has type filters) and paginate.

### S6 — P2: fonts are still 300 KB TTFs, revalidated every load
`@font-face` points at `Literata-Regular.ttf` (316 KB) and `Literata-Bold.ttf` (324 KB); `/static/fonts` is mounted `no-cache` (`app/main.py:288`, `app/static/style.css:8–18`). A browser must revalidate two large files on each visit. Convert to WOFF2 subsets (30–40 KB), fingerprint and cache immutably, and preload the bold face used for headings.

### S7 — P2: the PDF is built at the very end and served as one in-memory blob
`assemble` reads every page blob and builds the PDF after all pages are done (`book_runner.py:812`); the 4.7 MB result is then served by `book_pdf` as one `Response`. Fine at this scale, but a 21-page book's PDF is the last thing to appear and there is no progress for it. If books grow, stream or cache the PDF bytes in the row (they already live there) and show “putting the book together” progress (the UI already has the phase).

### S8 — P2: the test suite is slow enough to hide regressions
The full suite did not complete in 400 s here, while the app's own mock books are 3× slower than necessary because of B5. The suite is broad (65 test files) and worth keeping; parallelize the slow book tests and fix the mock review false positive so the full run stays under a CI-friendly budget.

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

---

## 6. Things that increase cost

### C1 — P0: the unit economics are negative
`review-log.md` (11 Nov 2026) measures **$1.60 and $1.56 per book against a $0.99 price** — about a $0.59 loss per book. The causes are 1–2 redraws per page (≈52 image calls instead of 22), a real price of ≈$0.024 per image versus the README's $0.00588, and review/decision calls. The cost panel now shows this to the operator, which is good; the price and retry policy have not caught up.

**Fix (in order):** make the first draft pass more often (shorter, better-targeted page prompts; plate edit where available), stop paying for a redraw that the reviewer/decision judge cannot justify, reprice to at least $1.99 (or cap pages/attempts), and add a per-book hard budget that stops retries when the spend exceeds a fraction of the price.

### C2 — P1: free, unlimited redraws after the sale
`edit_book` treats every image action as included (`book.py:422–570`): `character=star` redraws 21 pages + cover, repeatedly, for one $0.99 fee. This is generous to a fault and directly exploitable. Cap free edits (one cover + one page, or one “replace a face” per book), or charge the documented per-image cost.

### C3 — P0: portraits are not gated by the wallet
See B4. Zero-balance portraits are pure operator cost with no path to revenue.

### C4 — P1: no per-account or per-book budget
The only brakes are per-IP rate limits (`MAX_GENERATIONS_PER_MINUTE`, `OPENAI_IMAGES_PER_MINUTE`) and `MAX_CONCURRENT_GENERATIONS`; none of them knows what a book has spent or what an account has paid. A single account can open multiple books, generate retries, and redraw for hours. Add a cost ceiling per book (stop retrying at, say, 1.2× the price) and a daily account ceiling; surface both in the error copy.

### C5 — P2: tier-1 false positives cost two extra images per affected page
B5 proves the detector can fire on legitimate full-width bands. Every false positive is $0.048 + review cost, and on a 21-page book a systematic false positive is the difference between profit and loss. Calibrate on the plate set and add the `collage` fault to the reviewer's vocabulary (the review loop only knows the fault names in `parse_answer`; an unknown name is dropped).

### C6 — P2: review and decision calls are paid on every picture, including pages that never reach a book
The decision judge and reviewer run inside each page attempt; a failed book still pays for every review. That is acceptable for quality, but a failed page after three attempts has paid for three reviews. Consider one review on the best candidate instead of every candidate, or let the decision judge settle obvious passes without the reviewer (it already does) and measure the saved calls in the cost ledger.

### C7 — P2: the demo path can burn paid reviewer calls if misconfigured
With `IMAGE_PROVIDER=mock` and `REVIEWER_PROVIDER=openai`, tier 1 fails every mock page **before** tier 2 (so no review calls), which is lucky. The inverse (`IMAGE_PROVIDER=openai`, `REVIEWER_PROVIDER=off`) is supported and silently ships unreviewed pages (W9). The deployment should validate this combination at boot: paid images + reviewer off is either a deliberate cost saving or a mistake; require an explicit `ALLOW_UNREVIEWED=1`.

### C8 — P2: long prompts (if still ~18 K characters/page) multiply input tokens
UI.org measured ~17.8 K characters per page prompt; the prompt builder still assembles the identity block, cast sheet and boilerplate for every page (`app/services/prompts.py:445–480`). The output cost dominates with image models, but the input tokens are paid on every retry. Re-measure after the recent prompt cuts; if still large, put the page's moment first and trim the repeated cast block. (This audit did not re-measure the prompt lengths, so treat it as a to-verify item.)

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
