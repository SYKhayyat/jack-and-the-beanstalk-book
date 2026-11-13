# Review log

Every run of the `storybook-review` skill against the live site is appended
here, newest last. Earlier runs are never edited or removed: the trajectory is
the record.

Each entry: date, commit, the photo used (new or development), the total, the
top Fix-first item, and what was changed in response.

<!-- Runs are appended below this line. -->

## Run 1 — 2026-11-10 — reviewer: self — browser: Claude in Chrome (host: jack-and-the-beanstalk-book.onrender.com, the hand-in URL) — evaluated by: Claude Sonnet 5.5
Total 66 (provisional: unmeasured lines scored 0; first run, no previous total)
PDF 25/40 · UI 16/20 · Money 17/20 · Engineering 8/20
Commit 7061992. Photo: a new one, not used in development (a girl, outdoors, smiling).
Book 1 (Jack): submit ~22:18:10 → PDF ready 22:35:07 (~17 min). Book 2 (Red Riding Hood): submit ~22:37:45 → ready ~22:53 (~16 min). Target was under 60 s.
Balance: 0.00 → refused at $0.00 (clear message, no charge) → fund 5.00 (22:17:54) → book 1 4.01 → book 2 3.02 → refund requested 0.00 (history row, status "Waiting for the operator").
Avg cost per book: not shown on /wallet when checked, against a price of $0.99.
Not measured: book 2 PDF (the download click saved no file, so pages, layout match against book 1 and text placement are unchecked); sign out / sign back in / earlier PDF; 400px wizard (window only shrank to 515px; wallet had no horizontal overflow there); zoom to 400% on text blocks; "picks best of 3 when all fail".
Seen: PDF 1 is 22 pages (cover + 20 + About), text in a band under each picture, never over a face; About page carries the name and photo; "Try 1 of 3" and "Try 2 of 3" shown during generation; history reconciles (5.00 − 0.99 − 0.99 = 3.02); no keys in the tree or in git history.
Fix first:
1. A book takes 16–17 minutes, not 60 seconds (Engineering, -6). Pages cycle through "Checking page N" repeatedly before the PDF is written.
2. Progress overlay shows a counter and "Checking page N", not a named stage; thumbnails were seen only for the cover early on (UI, -4).
3. Average API cost per book vs $0.99 not visible on /wallet (Money, -3). to-do.md says it should be.
4. Refund stays "Waiting for the operator"; the card is not credited until someone processes it (Money, -1).
5. Check book 2's PDF and sign-out/sign-in persistence by hand: these are the unmeasured -17.

## Run 1 addendum — 2026-11-11 — correction to "Avg cost per book"
Run 1 listed the average cost as "not shown on /wallet". The api_calls ledger holds it, read from the live database:
Jack (ac4967a5): cover + 20 pages 21 calls $0.595; retries 31 calls $0.940; reviews and decisions 96 calls $0.067; **$1.60**.
Red Riding Hood (15657b73): cover + 20 pages 21 calls $0.601; retries 30 calls $0.898; reviews and decisions 85 calls $0.063; **$1.56**. (10 failed decision calls on this book, priced at 0.)
Average **$1.58 per book against the $0.99 price: a loss of about $0.59 a book.** Two causes: about 52 image calls a book (not 22) because pages averaged 1–2 redraws, and live pricing of about $0.029 per image against the README's $0.00588.
Fix first (Money): cut retries per page (stricter first draft, or a cheaper regenerate path), re-check the per-image price against the OpenAI bill, and reprice or cap before selling.

## Run 2 — 2026-11-13 — reviewer: self — browser: Playwright Chromium, local mock server (host: 127.0.0.1:8765) — evaluated by: Buffy (DeepSeek V4.1 Flash)
**No total.** This run is a local, offline self-check: `IMAGE_PROVIDER=mock`, `PAYMENTS=off`, SQLite, no Google sign-in, and no `.env` was read. The live site was not driven and the real image key was not used, per the hand-in instruction. Money lines, a real photo, likeness and the 60-second real-provider target are therefore **not measured**, and the skill's rule applies: no total without those.
Measured lines only: PDF 28/40 (the 8 likeness and 4 one-style points need real art); UI 5/20 local (first-time clarity 3, 400 px 2); Money 0/20 measured; Engineering 7/20 local (character bible 3, to-do 2, review log 2). The remaining lines are listed as not measured, not as passes.
Commit: `0ef033d` plus the uncommitted print round (this log is written before that commit).
Photo: a synthetic 512×512 PNG, not a real child — acceptable only because the artist is the mock and likeness cannot be judged either way.
Books: Jack starring Noa (24-page PDF, 1.4 MB) and Red starring Sarah (`docs/sample-book.pdf`, 24 pages, 1.5 MB).

Measured:
- Local upload → usable portrait 0.36 s; start → complete PDF 31.0 s for 21 pictures (mock, so this measures scheduling/assembly, not provider time).
- Shelf first load 4.4 s on a cold local server (seeding + first render); studio load and the name/gender step were immediate after that. At 400 px wide the studio has no horizontal overflow (scrollWidth − innerWidth = 0).
- Both PDFs: 24 pages — front cover, copyright, 20 story pages, The End, About Me. Story starts on page 3, the page after the copyright page. Page size 254×272 mm (4:3 art); real PDF text in Literata, in the band below the art.
- Cover text layer: “starring <child>” exactly once; the earlier doubled shadow copy is gone.
- Copyright and back cover name the child: “Made for Sarah” / “About Sarah”, not the tale's default “Red”.

Not measured: real-photo likeness and one-style consistency, text-over-face on real art, funding/refund/balance/history, sign-out and return, named progress stage and per-image thumbnails in a real render, the 3-attempt chip, and the live 60-second target. The print page itself needs a signed-in wallet, so its UI was not driven here; it is covered end to end by `tests/test_print_orders.py` against the real store and wallet.

Defects found and fixed during this run:
1. A blank About name fell back to the tale's default, so “Made for Red” and “About Red” appeared in a book starring Sarah. It now falls back to the typed child's name.
2. The cover credit used the About-page name, so a Jack book titled “Shai and the Beanstalk” said “starring Jack”. It now credits the story name, “starring Shai”.
3. The cover shadow drew the title twice, so copying the cover gave every word twice; it is now one fill-and-stroke text run.
4. Peecho was told a fixed A5 size; the print quote now measures the PDF's real page count and trim size.
5. `docs/sample-book.pdf` was 254×336 mm pages of flat 8×8 placeholder. It is rebuilt at 254×272 mm with a real cover illustration, by `scripts/make_sample_book.py`.

Fix first for the next (live) run:
1. Use a new real photo and a paid-provider run: measure likeness on pages 1/10/20, one-style consistency, and the real upload → PDF clock against 60 s.
2. Drive the wallet and the print page on the live site with Stripe test mode: fund, generate, refund, history reconciliation, and one Peecho **test** order.
3. Watch the first load on the deployed instance; 4.4 s locally is cold-start seeding, and the live free instance may be slower.
