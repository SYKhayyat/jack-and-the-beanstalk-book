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

---

# Part 3 — Each audit item written up (added 2026-11-15)

Every item has the same shape: **What** (the symptom), **Evidence** (how I know), **Cause** (known, or marked *likely* or *unknown*), **Fix**, and **Done when** (a check you can run). IDs are for reference in commits. Items 1 to 3 are the speed causes from Part 1 (written up there, repeated here only as one-line pointers so the list is complete).

## Speed (details in Part 1)

- **S1. 113 s of silence before the first picture.** Plates are decoded and re-encoded for every book. Fix: 1024 px reference copies, lazy loading, timing logs. Done when the first picture call starts within 10 s of the book starting.
- **S2. Only 6 pictures drawn at once.** `BOOK_PAGE_CONCURRENCY=6`. Fix: raise in steps while watching memory and `loop.lag`. Done when 21 pictures start within 5 s of each other.
- **S3. A page slot is held through review and redraws.** Fix: hold it only for the draw. Done when a retry on one page does not delay the start of any other page.
- **S4. "The pencil is resting" shows at the start.** Same cause as S1. Fix: a real "Getting the pictures ready" stage. Done when the first caption names work that is happening.

**Status (2026-11-15).**
- **S1, done.** Each plate gets a 1536 px JPEG working copy (`template_plates.working_image`, migration 0023). The copy is made the first time a page draws on that plate, and every later book reads it back without decoding. A book now learns only its plates' ids and slots at the start; each page loads its own plate when it is drawn. The edge is 1536 rather than the 1024 suggested above because `paste_plate_back` needs the plate at least as large as the drawn picture, and the largest provider output is 1344. A test pins this. The new log line `book.setup ms_artwork= ms_hair= ms_plan=` shows where the time before page 1 goes. The live run (O1) checks the 10 s target.
- **S2, raised; the target is capped by OpenAI.** `BOOK_PAGE_CONCURRENCY` is now 8, matching the server gate, which is safe because of S3. OpenAI's limit of 20 images a minute (paced at 18) means 21 pictures can never all start within 5 s. The real ceiling is the per-minute rate, not this setting. O1 checks memory and `loop.lag` at 8.
- **S3, done.** The page slot is held only for the model call. Pasting the plate back, both review tiers and the wait before a redraw all run outside it. The semaphore is first come, first served, so a retry queues behind every page still waiting for its first draw. The pause switch is checked when a page takes its first draw slot. Test: `test_a_page_being_checked_does_not_keep_the_next_page_from_starting`. With one slot, the first review stays open until other pages have drawn.
- **S4, done.** A new job is in phase `preparing` until page 1 starts. The reader shows "Step 1 of 2 · Getting the pictures ready — the first page starts in a moment."

## Reader and sharing

### R1. The reader shows 21 pages; the PDF has 24
- **What.** The reader and the share page show the cover and the 20 story pages. The copyright page, "The End" and "About Maya" are missing.
- **Evidence.** PDF has 24 pages (`pdfinfo`): cover, copyright, 20 story, The End, About. The owner sees the same gap on the share page.
- **Cause.** *Likely* the reader builds its page list from the story pages only, not from the list that builds the PDF. Not yet located in the code.
- **Fix.** Build both from one ordered page list (kind: cover, copyright, story, end, about). Add a test that the reader's page count equals the PDF's.
- **Done when.** The reader, the share page and the PDF show the same pages in the same order.

### R2. Book text is white on white in the reader and on the shared view
- **What.** The story text cannot be read.
- **Evidence.** Owner-reported, in the browser and when sharing. Not reproduced by me.
- **Cause.** *Unknown.* `app/templates/share.html` sets no colours, so look at the shared stylesheet, the reader's text panel and any dark-mode rule that sets a light text colour over a light panel.
- **Fix.** Set an explicit text colour and panel colour on the reader's text band, and do not rely on inherited theme colours. Add a check that renders the share page in light and dark mode and compares the text and panel colours for contrast.
- **Done when.** Text is readable on the live share page in a private window, in both colour schemes.

### R3. After "Stop sharing" the link is still shown and clickable
- **What.** Stopping works (the link no longer opens), but the page still exposes the link so it can be clicked.
- **Evidence.** Owner-reported. My agent could not click the button.
- **Cause.** *Likely* the book page keeps showing the last share URL after the links are revoked.
- **Fix.** After a revoke, replace the link and copy button with "Sharing is off" and one "Share again" button. Reload shows the same state because it comes from the server.
- **Done when.** After stopping, no share URL is anywhere on the page, including after a reload.

### R4. The reader and the PDF are formatted differently
- **What.** The page layout (text band, margins, the front and back pages) differs between the reader and the PDF.
- **Evidence.** Owner-reported. The PDF has the text in a cream band under each picture.
- **Cause.** *Likely* two separate render paths (HTML and PDF).
- **Fix.** Fold into R1: one page list, one layout. Where HTML must differ, say so in a comment and keep the page content the same.
- **Done when.** Page n in the reader shows the same picture and the same text as page n in the PDF.
- **Status, R1 to R4 (1c21490).** Done. `book_outline.book_leaves` drives the PDF, the reader and the share page, so all three show the same pages in the same order: cover, copyright, 20 story pages, The End, About. The reader's text band has explicit colours. Sharing is an on/off flag (migration 0022), and the link is rebuilt from the book each time. Stopping sharing removes every link from the page. "Share again" makes a new one. Books shared before the migration show "never shared" until they are shared again.

### R5. `/wallet` typed directly redirects to the home page
- **What.** The header link works; typing the address does not.
- **Evidence.** Seen by the browser agent in Run 3.
- **Cause.** *Unknown.* Possibly a redirect for a signed-in user, or the wallet is a drawer and not a route.
- **Fix.** Decide whether `/wallet` should exist. If it should, route it. If not, remove the idea from anything that links to it.
- **Done when.** Typing the address does what the header link does.
- **Status.** Not reproduced. On the live site, typing `/wallet` while signed out goes to `/login?next=/wallet`, and while signed in it opens the wallet. The Run 3 agent was most likely signed out. O1 checks this again.

## Print

### P1. Pictures print at about 81 ppi
- **What.** Each picture is 1152 x 864 pixels placed at 81 ppi, JPEG.
- **Evidence.** `pdfimages -list` on the downloaded PDF.
- **Cause.** The generation size is 1152 x 864 (`gpt-image-2.5-flare`, quality low), and the PDF uses the same file.
- **Fix.** Decide the print size and generate or upscale to about 300 ppi for the print edition only. Keep the cheaper size for the screen PDF. Price the larger size before deciding, because cost follows the size.
- **Done when.** The print file's pictures are at least 250 ppi at the trim size.

### P2. The interior has an odd page count
- **What.** The interior is 23 pages (copyright, 20 story, The End, About) with the cover separate.
- **Evidence.** `pdfinfo`: 24 pages including the cover.
- **Cause.** The page list has no blank pages.
- **Fix.** Look up the page rules for each trim and binding in the print module, then pad with blank pages to the required count (a blank back of the cover, a blank before the copyright page, and trailing blanks). Never put a blank in the middle of the story.
- **Done when.** The print file passes the Lulu sandbox's own validation for each trim and binding.

### P3. The page size must match a Lulu trim
- **What.** The screen PDF is 720 x 771 pt.
- **Evidence.** `pdfinfo`.
- **Fix.** Check that the print edition builds a file at the chosen trim, with bleed, and that the text band never lands in the trim margin.
- **Done when.** The sandbox accepts the file at both trims.

### P4. Page numbers are one lower than the PDF page
- **What.** The first story page prints "2" and is PDF page 3.
- **Evidence.** The footer of p3 in the PDF.
- **Fix.** Pick one rule on purpose: number from the first story page, and leave the front and back pages unnumbered.
- **Done when.** The first story page prints "1" and the end pages print no number.
- **Status, P1 to P4 (1c21490).** Done.
  - P1: when a print order is approved, its pictures are upscaled to 300 ppi (Lanczos plus a light unsharp mask). The screen PDF stays as it is. This is the owner's decision. On the free plan's 0.1 CPU, approval may take tens of seconds.
  - P2 and P3: all four editions passed validation (NORMALIZED) in the Lulu sandbox. Lulu refuses perfect-bound books under 32 pages, so the paperback is padded to 32. Margin at 32 pages: printing $9.03, total $11.43, margin $6.56 (about 36.5%, was 46%).
  - P4: story page n prints "n". The cover, copyright, End and About pages are unnumbered.

## Pictures

### I1. The shortcut: the cottage is right behind them (p6)
- **What.** The wolf says the cottage is far, but it is a few steps away.
- **Evidence.** p6. The page 5 caption asks for a cottage "far between the trees", but p6 draws it close.
- **Cause.** The caption allows a cottage that is "visible", and the model draws it large and near.
- **Fix.** Say "very small and far down the path, at least a hundred trees away", and add "the cottage is small in the frame, no larger than the child's hand held at arm's length" to `must_include` and the review checklist.
- **Done when.** In a rendered book, the cottage on that page is a small shape far along the path.

### I2. The cottage changes between pages (the bridge on p9)
- **What.** p9 has a plank bridge over a stream that p6 and p8 do not.
- **Evidence.** p6, p8 and p9. The story text for p9 says "over the stream", so the bridge comes from the text.
- **Cause.** Each page is drawn on its own, with no fixed picture of the cottage and its surroundings.
- **Fix.** Add a short "the cottage and its path" description to the book's fixed settings, used on every page that shows it, and say where the stream is.
- **Done when.** The cottage and its surroundings agree across pages 6, 8, 9, 16 to 18 and 22.

### I3. No body under the quilt (p10, p12, p13)
- **What.** The bed is flat when someone is in it.
- **Evidence.** p10, p12 and p13.
- **Cause.** The scene briefs say "quilt up to his chest" but not that a body makes a shape. The reviewer does not check for it.
- **Fix.** Add "the quilt rises over a body: a visible hump for the knees and feet" to the briefs and the review checklist.
- **Done when.** The review fails a flat bed, and the next book shows a bulge.

### I4. Grandmother is not sitting in the bed (p11)
- **What.** Her torso is beside the bed edge and her lower body is gone.
- **Evidence.** p11.
- **Cause.** The brief asks for her to sit up "in bed", and the picture puts the bed at the edge of the frame so the model crops her.
- **Fix.** Put the bed fully in the frame with her in the middle of it, and check for the bed under her in the review.
- **Done when.** Her whole upper body is in the bed.

### I5. The wolf's cap and clothing change (p13 and others)
- **What.** The text says the wolf has a cap on p13 and the picture has none. It returns on p14. The neck piece changes between pages.
- **Evidence.** p13, p14, p18 and p5 to p8 against p18.
- **Cause.** The brief requires the cap, but the picture does not have it, and the reviewer passed it. The outfit is described in each caption separately.
- **Fix.** Make the wolf's outfit one fixed description per phase (his own coat, then the cap and shawl), used word for word. Make the cap a hard failure in the review of p13.
- **Done when.** Review fails a picture whose text names an item the picture lacks.

### I6. Heads and faces smear or vanish (p14, p18, p20, p22)
- **What.** Part of Maya's head or face is soft or missing. The mother's hair merges into the roof on p22.
- **Evidence.** p14 and p18 are clear. p20 and p22 are partial.
- **Cause.** *Likely* the fix-edit (attempt 2). Not proven.
- **Test first.** List the attempts per page from `api_calls` and see whether the smeared pages are the edited ones.
- **Fix.** If the test agrees, make attempt 2 a clean redraw with a corrected prompt, or limit the edit to the faulty region.
- **Done when.** A book with retries shows no smeared faces, and the review fails any face that is soft or cut.

### I7. Hands and hugs are warped (p3, p5, p6, p7, p19, p22)
- **What.** Hands are blobby, merged with sleeves, or missing. Hugs are worst.
- **Evidence.** p5, p19 and p22 are visible. The rest need a full-size check.
- **Cause.** Image models draw hands badly, and hugs make hands overlap.
- **Fix.** Put a hand check in the review (five fingers, each hand attached to an arm), reroll when it fails, and avoid layouts with overlapping hands where the story does not need them.
- **Done when.** Review fails a hand it cannot count, and a rerun of the same page fixes it.

### I8. Maya looks generic on p22 and the portrait has a beard
- **What.** Maya's face on p22 is rounder and younger than on other pages. The About portrait shows stubble. The story shows no beard.
- **Evidence.** p22 and p24. The beard is owner-reported.
- **Cause.** *Likely* the portrait prompt keeps the photo's adult features while the plates draw a child.
- **Fix.** Give the portrait prompt the same age and "no facial hair" as the plates, and compare it to a plate in the likeness review. Check the portrait is not feeding the beard into the pictures.
- **Done when.** The portrait and the story pictures look like the same person.

### I9. The reviewer lets bad pictures pass
- **What.** It passed items I3 to I7.
- **Evidence.** p10 to p14, p18, p19.
- **Cause.** The review checklist does not test these things.
- **Fix.** Add checks for: body shape under covers, the item the text names, hands, heads whole, setting consistent. Raise the severity of a missing named item.
- **Done when.** The same book fails review on the pages above.

## Story

### T1. "Always knocked before going in" is never set up (p22)
- **What.** The last sentence's moral does not come from the story.
- **Evidence.** `little_red_riding_hood.toml:399`. The wolf knocks on p9, and Maya calls out and walks in on p13.
- **Fix.** Change the line to something the story showed ("always stayed on the path, and never talked to strangers"), or add a knock to p13.
- **Done when.** Every lesson in the closing text matches something that happened.

### T2. Text and picture disagree (p13, p19)
- **What.** p13's text names a cap that is not there. p19's text has the woodcutter opening the cupboard, and the picture shows a hug and no woodcutter.
- **Evidence.** The text of p13 and p19 against their pictures.
- **Fix.** Make p19's picture show what the text says, or change the text to what the picture shows. Make the review compare the text to the picture.
- **Done when.** Each page's text is true of its picture.

## Other

### O1. Unmeasured Run 3 lines
Print page and Lulu quote, wallet top-up for a print, refund, sign-out/in, 400 px width, delete-account control, empty-balance message, console check, and the PDF likeness lines. Rerun in a private window in a browser that does not freeze, and write a Run 4 entry.

### O2. The $3.02 refund still reads "Waiting for the operator"
Check whether this is a real queue item or a stuck state.

**Status (2026-11-15): a real queue item, not stuck.** Supabase `wallet_refunds` holds one row, `619fee59…`: 302¢, status `requested`, created 2026-11-11 03:54, no Stripe refund ids. Refunds are sent by the operator by design, and this one is already raised as the `refunds_waiting` alert on /admin/status and in the operator mail. To clear it, press send on /admin/status. Nothing in the code needs changing.

## Suggested order

1. R1 to R4 (the reader and sharing, because people see these first), then P1 to P4 (print, because it costs money).
2. S1 to S4 (speed).
3. I9, I3, I5 (the review), then I6 (test the fix-edit cause), then I1, I2, I4, I7, I8.
4. T1 and T2.
5. O1 and O2.
