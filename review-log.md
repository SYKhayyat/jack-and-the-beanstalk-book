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
