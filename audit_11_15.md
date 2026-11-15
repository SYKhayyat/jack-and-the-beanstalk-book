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

---

# Part 2 — Run 3 review defects and the owner's findings (added 2026-11-15)

**Grade.** Run 3 has **no total**. The skill withholds a total when lines are unmeasured, and most browser lines were. The only scored shortfalls so far are Engineering -6 (7 min 09 s against 60 s) and UI -3 (113 s with no stage shown).

Book checked: `book-91dcd144…` downloaded 2026-11-15 07:24 (three identical files). Page numbers below are **PDF page numbers** (p1 cover, p2 copyright, p3 to p22 story, p23 The End, p24 About). The printed folio is one lower, so p3 prints "2". Where I verified a finding from the PDF it says **confirmed**. Where I could only look at 110 dpi or the owner saw it in the browser, it says **owner-reported**.

## A. Defects from the Run 3 browser pass

1. **Slow book (7 min 09 s) and 113 s of silence at the start.** Parts 1 to 3 above.
2. **Caption "the pencil is resting" at the start.** Same cause as 1.
3. **Reader shows 21 pages, PDF has 24. Confirmed.** 21 = cover + 20 story pages. The reader and share page leave out the copyright page, "The End" and "About Maya" (this is also what the owner saw).
4. **"Stop sharing" confirmed by the owner to work** (my agent could not click it). See B2 for what is still wrong with it.
5. **`/wallet` typed directly redirects home**; the header link works. Not investigated yet.
6. Unmeasured and still open: print page and Lulu quote, wallet top-up for a print, refund, sign-out/in, 400 px width, delete-account control, empty-balance message, console check.

## B. Owner-reported site problems (not reproduced by me; no browser was available for this part)

1. **Book text is white on white in the shared view, and on the in-browser reader.** Combined with A3, the reader looks unfinished. Where the colour comes from is not yet located (`app/templates/share.html` has no colour rules, so check the shared stylesheet and any dark-mode rule). Fix: set an explicit text colour on the text panel, and test the share page in light and dark mode.
2. **After "Stop sharing", the control that exposes the link is still shown**, so the owner can still click through to it. Fix: when sharing is off, hide the link and the copy button and show "Sharing is off" with one "Share again" button.
3. **The PDF and the reader are laid out differently.** The reader has no copyright, end or about pages (A3) and, per the owner, different formatting. Fix: render the reader from the same page list that builds the PDF.

## C. Print readiness. Checked against the PDF.

1. **Image resolution is about 81 ppi. Confirmed** (`pdfimages -list`: 1152 x 864 pixels placed at 81 ppi, JPEG). Print needs about 300 ppi. At this size the print edition will look soft. This is the biggest print risk. It comes from the 1152 x 864 generation size, so it needs the plate and render size decided before the Lulu edition is sold.
2. **Page count.** The interior is 23 pages (copyright, 20 story, The End, About) plus a cover. An odd interior is not printable as is. Lulu needs an even count (a multiple of 4 for saddle-stitch, and a minimum for hardcover). Needs a check against the chosen trim and binding in `app/services/` (print module), then add blank pages. A natural layout: blank back of the cover, copyright, 20 story, The End, About, then blanks to the required count.
3. **Page size is 720 x 771 pt**, not one of the Lulu trims by itself. Check the print edition crops or pads it to the chosen trim without cutting text.
4. **"The End" sits alone on a full page** and the story pages carry a folio that is one lower than the PDF page. This is fine for print, but the numbering should start from the first story page on purpose.

## D. Story and picture defects, page by page (from the latest PDF)

| Page | Owner said | Checked |
|---|---|---|
| 3 | Maya's left hand is warped | Not confirmed at 110 dpi. The hand on the basket is small and soft, with fingers not clearly drawn. Check at full size. |
| 5 | A hand is warped | The wolf's raised paw has a dark blob for a pad. Maya's hand on the basket handle is unclear. Partly confirmed. |
| 6 | They are right at the cottage; no shortcut possible | **Confirmed.** The cottage stands a few steps behind them. The story sends the wolf to run ahead to a cottage that looks about 20 metres away. The cottage must be shown far down the path. |
| 6 | A hand warped and a hand fades | Not confirmed at this resolution. Check at full size. |
| 7 | A hand likely missing | Not confirmed. The basket hangs from an arm with no hand visible. Check at full size. |
| 9 | Cottage has a bridge it did not have on 6 | **Confirmed.** A plank bridge over the stream appears on p9 and not on p6 or p8. This is the cottage "scene" differing between pages, as the wolf's route passes a stream. |
| 10 | No bulge for Grandmother's legs | **Confirmed.** The blanket lies flat. |
| 11 | Grandmother is not really sitting in bed | **Confirmed.** Her torso appears beside the bed edge and her lower body and legs are gone. |
| 12 | The wolf's legs make no bulge | **Confirmed.** The quilt is flat. |
| 13 | Same; the text says the wolf has a cap, the picture shows none; the cap comes back later | **Confirmed.** The p13 text says "someone was lying in the bed with a white cap on", the picture has no cap, and the cap is on again on p14 to p18. The scene brief (`little_red_riding_hood.toml`, page 12 text at line ~253 and its `must_include`) demands the cap, so the reviewer passed a picture that fails its own brief. |
| 14 | Half of Maya's head vanishes | **Confirmed.** The left side of her hood and head is a soft smear against the wall. |
| 18 | Maya's face vanishes | **Confirmed.** Her face is smeared and the mouth is lost. |
| 19 | Maya's hands morph into Grandmother's sleeves | Partly confirmed. The arms and hands in the hug are not cleanly separated, and Maya's arm and the sleeve merge. |
| 20 | Part of Maya's head vanishes | Partly confirmed. Her hood edge and head are faded. |
| 22 | Maya looks generic; the mother's head partly disappears | Maya looks generic: bigger eyes and a rounder, younger face than on the other pages. The mother's hair merges into the roof edge. Partly confirmed. |
| all | Hugs and hands are hard | Agreed. Every hug (p3, p19, p22) has a hand problem. |

**Wolf's clothing changes.** Confirmed in the pictures: the green coat and waistcoat stay, but the neck piece changes (brown neckerchief on p5 to p8, a red bow tie on p18), the cap and shawl come and go (absent on p13, present on p14 and p18), and the coat is open or closed from page to page.

**Likely cause of the "vanishing" heads.** The owner suspects repainting. Plausible, not proven. Attempt 2 is a fix-edit, which paints back over an area of the same picture. A model editing an image can soften or erase a region it was told to leave alone. To test: list each page's attempts from `api_calls` and see whether the smeared pages (p14, p18, p20) are the ones that went through a fix-edit. Page 1 took 3 attempts and page 17 took 2 in this book, so compare those first. If the smeared pages are the edited ones, change attempt 2 to a clean redraw with a corrected prompt, or mask the edit to the faulty region.

**Why does Maya always knock before going in?** It is the last sentence of the story text (`little_red_riding_hood.toml:399`): "always stayed on the path, and always knocked before going in." It is a moral that the story never sets up. The wolf is the one who knocks (p9), and Maya calls out and walks in (p13). It reads as a mistake, not as a lesson. Fix: change the line to something the story did show, for example "always stayed on the path, and never talked to strangers", or add a knock to p13.

**Standalone portrait has a beard, the story does not.** Owner-reported. On the About page the portrait is a cartoon of the photo with stubble. The story pages show a child-like Maya with no beard. Likeness is not constant between the portrait and the story, so the "same person on every page" line (8 points) is at risk. Fix: either generate the portrait with the same prompt and age as the plates, or tell the portrait prompt to keep the child's age and no facial hair. The portrait already feeds the reference, so also check it is not pulling the beard into the pictures.

## E. Additional findings from my own pass

1. **Wolf is a separate person from page to page in the pictures (see D).** The character, outfit and cap rules are not held across plates. The reviewer passes pictures that break their own `must_include` (p13).
2. **The reviewer misses bed bulges.** It passes flat beds on p10, p12 and p13, and Grandmother's missing body on p11. Add "a person in bed shows a body-shaped bulge under the quilt" to the review checklist and to the scene briefs.
3. **p19's text says the woodcutter opened the cupboard and Grandmother stepped out.** The picture shows a hug beside the bed and no cupboard action and no woodcutter. Text and picture do not match.
4. **The text on p17 says the wolf jumped off the bed and ran through the door.** The picture is the wolf already in the garden. Fine as a moment, but it is the third page in a row with the wolf in a different place.
5. **No text over faces anywhere.** Checked every story page: text sits in the cream band below the picture, so the 10-point line passes.
6. **Reader and PDF counts differ in the drawer too** (`app/templates/_books_drawer.html:18` shows "pages + cover" computed from `page_count - 1`). Check it agrees with the 24-page PDF.

## Order of work for Part 2

1. Print readiness (C1, C2), since selling a soft or odd-page print is the largest money risk.
2. The reader and share page (B1 to B3, A3), since they are what people see first.
3. Story and picture consistency (D, E1 to E3), starting with the check of whether fix-edits cause the vanishing heads.
4. Story text fixes (knock line, p6 cottage distance, p19 text).
5. Rerun the unmeasured Run 3 lines in a private window.
