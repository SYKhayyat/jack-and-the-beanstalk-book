---
name: storybook-review
description: Review or grade a student storybook app — the live UI and the PDFs it produces — against the MCON 368 rubric, driving the site in a connected browser as a real user, and log the score to review-log.md. Use when grading a submission or self-checking one before submitting.
---

# Storybook review

Score a personalized-storybook app out of 100 by **using it**, then append the result to `review-log.md`. The same skill serves the instructor grading a submission and a student self-checking before they submit.

## Step 0 — connect a browser

This skill drives the live site as a real user. Before anything else, the agent needs a browser: the **Claude in Chrome extension** (it runs in Edge too) or Claude's built-in browser. Open a fresh private window so no earlier session or saved login is in play.

Have ready, and give the agent before starting:

- The live site URL
- **A photo the builder did not develop with.** The instructor uses their own. A student self-checking uses a photo other than the one they have been testing on all week.
- Stripe's published test card: `4242 4242 4242 4242`, any future expiry, any CVC
- The GitHub repo URL

Every check below is tagged **[browser]** — the agent performs it live in the connected browser — or **[desk]** — the agent judges it from the PDFs, screenshots and repo. **If no browser is connected**, score only [desk] lines, list every [browser] line as *not measured*, and do not produce a total. Never invent a timing, a balance or a page count.

### Browser fallback for this repo

When no Chrome-extension or built-in browser is available, drive Chromium through Playwright instead — it is still a real browser with a fresh, empty profile, so no saved login can leak into the run:

```sh
NODE_PATH=/home/shaul/Scrivio/node_modules node /tmp/opencode/storybook-browser.js
```

Chromium binaries are already in `~/.cache/ms-playwright`. The harness script must launch a **new browser context** (no persisted storage), navigate to the live URL, and expose the page for driving, clicking, typing, uploading files and taking screenshots. Take every screenshot and every downloaded PDF into a run directory (e.g. `/tmp/opencode/storybook-run-<n>/`) and cite those paths as evidence.

## How to run it

1. **[browser] Open the site** in the private window. Note whether anything is slow to appear.
2. **[browser] Fund, then generate.** Record the balance. Fund with the test card. Record the balance again. Pick the story the builder did *not* demo. Upload the unfamiliar photo and **note the clock** at the moment you submit. Watch the progress bar and the thumbnails. Note the clock again when the PDF download link appears. Record the balance a third time.
3. **[browser] Download the PDF.** Then generate the other story too, so there are two PDFs.
4. **[browser] Refund.** Request a refund, process it, record the balance. Open the transaction history and screenshot it.
5. **[browser] Come back later.** Sign out. Sign back in. Download one of the earlier PDFs.
6. **[browser] Phone width.** Resize the window to about 400px wide and repeat the upload flow far enough to see whether it still works.
7. **[desk] Open both PDFs.** Page through all of each. Zoom to 400% on at least three text blocks.
8. **[desk] Read the repo last** — for keys, `to-do.md`, and `review-log.md`.

Every finding cites evidence: a page number, a clock time, a balance before and after, a screenshot, a file and line. "Looks good" is not a finding and neither is "needs polish."

## Rubric

### The PDF — 40

| Pts | | Check |
|---|---|---|
| 4 | desk | 20 story pages plus an About Me back page, in **each** of the two PDFs. Count them. |
| 4 | desk | One 20-page template shared by both stories. Lay the two PDFs side by side: same layouts, same text zones. Two unrelated-looking books fail this line. |
| 8 | desk | The child is recognizably the same person on every page. Compare pages 1, 10 and 20 side by side; drift shows by the middle. |
| 4 | desk | One visual style throughout. No page looks like it came from a different book. |
| **10** | desk | **Text never covers a face or anything the page needs you to see.** Check every page of both PDFs, not a sample. |
| 4 | desk | Text is rendered type, not lettering drawn by the image model. At 400%, warped or nonsense letterforms mean the model drew it. |
| 2 | desk | Text is legible against what sits behind it at 100%. |
| 2 | desk | The story runs in order and is complete. |
| 2 | desk | About Me carries the child's name and the details that were entered. |

**Cap:** if any page in either PDF has text over a face, the PDF section scores at most **20**, whatever the other lines add up to.

### The UI — 20

| Pts | | Check |
|---|---|---|
| 3 | browser | Opens with no perceptible delay. |
| 3 | browser | A first-time user can tell what to do without being told. You are that user: did you have to guess? |
| 3 | browser | A progress bar that names the current stage, not a spinner. |
| 3 | browser | Thumbnails appear as each image finishes, not all at the end. |
| 2 | browser | Balance visible without hunting for it. |
| 2 | browser | Transaction history reachable and readable. |
| 2 | browser | Usable at about 400px wide. |
| 2 | browser | Failures say what went wrong and what to do; nothing fails silently. Try generating with an empty balance to find out. |

### Money — 20

| Pts | | Check |
|---|---|---|
| 4 | browser | Funding with the test card works and the balance rises by the right amount. |
| 4 | browser | Generating a document deducts exactly $0.99. |
| 3 | browser | Generation is refused when the balance is short; the balance never goes negative. |
| 4 | browser | A refund can be requested and processed, and the balance follows. |
| 2 | browser | Every credit and debit appears in history with its cause, amount and timestamp. |
| 1 | desk | The history reconciles with the balance. Add it up from the screenshot. |
| 3 | desk | The average API cost to generate one book is tracked and shown — image calls, retries and analysis calls included — and compared with the $0.99 price. |

### Engineering — 20

| Pts | | Check |
|---|---|---|
| 6 | browser | Upload to downloadable PDF in **under 60 seconds**. Report both clock readings. |
| 4 | browser | Sign out and back in — earlier PDFs still download. |
| 3 | desk | The character bible is stored and can be retrieved from the database. |
| 3 | browser | The regeneration loop is visible: the UI shows the attempt count per image, caps at 3, and picks the best when all 3 have problems. If the count isn't shown, this line scores 0 — it cannot be verified. |
| 2 | desk | `to-do.md` exists and is specific about real defects. |
| 2 | desk | `review-log.md` exists and holds every run, oldest first, none overwritten. |

## Automatic deductions

- **An API key in the repo, in git history, or in client-side source: Engineering scores 0**, and the key must be rotated — deleting the file does not help, the history is what people clone. This includes the shared class Stripe key.
- **A PDF won't download: the PDF section scores 0.** There is no product without it.
- **Site unreachable in the private window: not submitted.** A site that only works while logged into the owner's browser is not deployed.

## What to report, and where

Append one entry to `review-log.md`. **Never edit or delete an earlier entry** — the log is the record of progress, and the trajectory is graded, not just the last number.

```
## Run 3 — 2026-11-09 — reviewer: self — browser: Claude in Chrome — evaluated by: Claude Sonnet 4.5
Total 71 (was 58)
PDF 30/40 · UI 14/20 · Money 15/20 · Engineering 12/20
Upload 18:42:07 → PDF 18:42:51 (44 s)
Balance: 0.00 → fund 5.00 → generate 4.01 → refund 5.00
Avg cost per book: $0.31 over 12 books (price $0.99)
Not measured: none
Changed since run 2: text zones moved to a per-page safe area; faces no longer covered on pp. 4, 11, 17 (Jack) or p. 6 (Red).
Fix first:
1. Refund doesn't update the balance until reload (money, -4)
2. Red Riding Hood PDF is 19 pages — the wolf-at-the-door page is missing (PDF, -4)
3. ...
```

The entry has: which browser was used; **the name of the AI/model that evaluated the run** (the model powering the review, e.g. `evaluated by: Claude Sonnet 4.5`); total and previous total; the four section scores; the clock readings, the balance trail and the average cost per book; any lines not measured; what changed since the last run; and **Fix first** — at most five items, ordered by how much they cost the product, each naming the defect, where it is, and the points it is costing.

Be direct about what is broken and concrete about what would fix it. A review that reads as encouraging but names nothing actionable has failed the builder.

## A note on the score

The number this produces is not the verdict. It is a checklist run against a product, and a product can pass every line here and still be bad. Whoever built it owns how good it actually is.
