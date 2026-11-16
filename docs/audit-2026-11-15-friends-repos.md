# Audit 2026-11-15: what to add or remove, from friends' repos

Compared against three friends' apps (cloned at HEAD on 2026-11-15):

- [Ozer7/storybook-print](https://github.com/Ozer7/storybook-print) (Oriel's: the site the owner calls "beautiful and fast")
- [eliTouro/jack-and-the-beanstalk](https://github.com/eliTouro/jack-and-the-beanstalk)
- [yosefdwolf/jackAndTheGiantBeanstalk](https://github.com/yosefdwolf/jackAndTheGiantBeanstalk)

Ozer7 is Oriel. Oriel's speed comes partly from a paid host: the app runs on Render Starter
($7/mo) because the free plan was too slow for it (A7, Q1).

## Summary

All three friends make a 20-page book in 29–55 s for about $0.33–0.59, on the same model we use
(`gpt-image-2.5-flare`). They get there with four tricks we don't have: a book-wide deadline, a
backup call for slow pictures, hero-less pages copied with no model call, and small input images.
These four (A1–A4) are the main adds and line up with the memory `speed-under-a-minute` goal.

On the remove side, each friend runs **one** cheap judge call per picture. We run a decision
judge, then a reviewer, with three possible reviewer providers behind them. eliTouro's judge
passed all 92 real images on the first try, so before we keep both tiers we should measure what
each one catches.

We're ahead on storage (R2, where they keep images as Postgres rows), print quality (we upscale at
order time; they print at 136–171 dpi), CI (none of them have any), share links and page edits.

## The three repos at a glance

| | Ozer7 (Oriel) | eliTouro | yosefdwolf | **Ours** |
|---|---|---|---|---|
| Stack | Node 22, Express, pdf-lib | Node 22, Express 5, pdfkit | Python 3.12, FastAPI, reportlab | Python, FastAPI |
| Host | Render Starter ($7/mo) | Render free + keep-warm ping | Railway | Render free |
| Image storage | Postgres rows (~7 MB/book) | Postgres rows | Postgres rows (WebP) | Cloudflare R2 |
| Book time (measured) | 34–53 s | 29–55 s | 44–48 s | over 60 s (goal: under 60) |
| Cost per book | $0.587 | $0.33–0.59 | ~$0.59 | — |
| Quality setting | low | low | medium | low |
| Book deadline | 51 s; retry only if ≥13 s left | 40 s; no new draw after | 50 s; retry only if it fits | none (600 s per call) |
| Backup call | no | yes, at 21 s, first one back wins | no | no |
| Hero-less pages | copied from stored scene | copied from template | drawn once, shared by all books | pre-made only when an approved one exists |
| What the model sees | head crop (~3 heads wide) + portrait, then blended back | scene at 640 px + hero ref at 384 px; photo never sent to pages | sheets of only the characters on the page | plate + references up to 1024 px |
| Judge | gpt-5.4-mini, score ≥75, caps for stand-in hair / pasted look | gpt-4.1-mini JSON: hero count, face/prop boxes, ≥6/10 | gpt-4.1-mini: child count + face bands | decision judge → reviewer (openai / gemini / gemini-web) |
| Words placed by | solver around stored face boxes | font metrics vs face/prop boxes | face bands from the judge | — (check, Q5) |
| Print | Lulu sandbox, 6×9 saddle-stitch, bleed, no upscale (~170 dpi) | PDF only, no cover, no bleed (~171 dpi) | Lulu sandbox, 11×8.5 hardcover, bleed, no upscale (~136 dpi) | Lulu, upscale at order, background build |
| Page edits | none | free-text "Fix something" + Undo | none | yes |
| Sharing | none | none | none | share links |
| Tests / CI | 1 smoke test, no CI | 39 tests, no CI | 173 tests, no CI | 85 test files + `ci.yml` |

## Add

Ordered by what they'd do for the 60 s goal per unit of work.

### A1. A book-wide deadline — P1

**What they do.** All three set a budget for the whole book (40, 50 or 51 s). A retry starts only
when it can finish inside that budget, and otherwise the best try so far is kept
(Ozer7 `lib/pipeline.js:27-28`, yosefdwolf `jobs.py:48, 214-228`, eliTouro `src/jobs.js:139-140`).
Their per-call timeout is 120 s, where ours is 600 s (`generation_timeout_seconds`).

**For us.** Add a `book_deadline_seconds` setting (default 50) to `book_runner`. The page loop
checks the time left before each attempt after the first. One hung call currently costs us
minutes; with this it costs one page's second try. Cut the paid-provider call timeout to 120 s.
The GEMINI_WEB path keeps its long timeout, because it is the local, free path.

### A2. A backup call for slow pictures — P1

**What they do.** eliTouro fires a second, identical call when a page call is still running at
21 s. The first one back wins, and both are paid for and logged (`src/jobs.js:149-165`). Their slow
run (55.5 s) was one call that hung for 45 s, which is exactly the case this covers.

**For us.** Do this in `page_picture` around the provider call: race two tasks, cancel the loser,
and record both in `costs`. It must take a second slot on the concurrency gate, so it can't go
past 24 in flight. Cost: about one extra call per book, at low quality.

### A3. Copy hero-less pages straight from the plate — P1

**What they do.** All three skip the model for pages without the hero. Ozer7 and eliTouro copy the
stored scene. yosefdwolf draws it once and shares it across every book, keyed by a hash of prompt,
references and model.

**For us.** `template_store` only uses a pre-made page when an approved one exists
(`template_store.py:1490`). Make that the default for every starless page: no call, no review,
no attempt. The trade-off is that those pages are identical in every book, which is how all three
friends ship.

### A4. Smaller inputs to page calls — P1

**What they do.** eliTouro sends the scene at 640 px and the hero reference at 384 px
(`src/jobs.js:143-144`), which they say is about a third cheaper. Ozer7 shrinks the photo to 768 px.

**For us.** We send references up to 1024 px (`openai_provider.py:101`). Add separate caps for the
plate and the references, then run one Red and one Jack book with `scripts/time_book.py` and
compare likeness before deciding on the defaults.

### A5. Head-crop-only edits, blended back — P2

**What they do.** Ozer7 sends only a square crop about three heads wide around the stand-in's
head, upscaled to 1024. The output is blended back into the plate with a feathered superellipse
mask and a mean-colour shift (`lib/face.js:11-133`). The face boxes come from a magenta/cyan mask
painted once per scene (`lib/locate.js`). Other faces are greyed out before judging, so only the
hero is scored.

**For us.** This is the biggest change on the list: it replaces whole-page edits in
`plate_paste` and `page_picture`. It should make calls faster and leave the rest of the plate
pixel-identical. It needs per-plate head boxes, which our figure boxes from
memory `likeness-and-paste-pack` may already give us. Do it after A1–A4, measured against the same
books.

### A6. Wait out 429s without using a try — P2

**What they do.** Ozer7 waits out rate limits for up to 4 tries (`lib/images.js:27-37`).
yosefdwolf waits up to 90 s, and a failed try isn't charged (`jobs.py:43-44, 203-261`).
Content refusals (400/401/403/422) aren't retried at all (eliTouro `src/quality.js:386`).

**For us.** Check how `page_picture` counts a 429 today. If it uses up one of the three
`page_attempts`, separate rate limits from real failures.

### A7. Keep-warm ping — P2 (only if we stay on Render free)

**What they do.** eliTouro has a GitHub Action that pings the site every 10 minutes so the free
instance never sleeps (`.github/workflows/keep-warm.yml`). Ozer7 (Oriel) moved to Starter ($7/mo)
because free was too slow, so the site that feels fast to us isn't on the free plan.
eliTouro is on free and measured 29–55 s.

**For us.** A cold start lands on the first book of the session. Add a cron workflow next to
`ci.yml` that requests `/healthz`. This is a decision for the owner (Q1).

### A8. A text description of the photo in every prompt — P3

**What they do.** Ozer7 and yosefdwolf have a vision model write a short description of the child
(hair, skin, glasses), which goes into every page prompt (Ozer7 `lib/vision.js:124`, yosefdwolf
`jobs.py:451-460`).

**For us.** It's cheap: one mini-model call per book. It should help on the hair-sentence problem
in memory `retry-rework`.

### A9. Progress that shows each page landing — P3

**What they do.** All three show thumbnails as pages finish, with a live "try n/3" on each.
yosefdwolf's ETA is the median of the last 30 books (`jobs.py:536-548`), and its "clothesline"
has a page tear when a try fails.

**For us.** Compare with what our progress caption shows today
(memory `progress-caption-resting-bug`). A median-based ETA is the cheapest part to copy.

## Remove or simplify

None of these should be removed until it has been measured. Each one is a question for the
owner, not a decision.

### R1. Two judging tiers → one — measure first

Every friend uses one mini-model call per picture with strict JSON output (hero count, face
visible, likeness score, face boxes). We run `decision_judge`, then `reviewer`, and the reviewer
can be openai, gemini or gemini-web. Next step: log, over the next 10 live books, how often the
reviewer overturns the decision judge. If it's close to never, drop one tier. That removes a
model call from every unsure page, plus `reviewer_*` settings and a lot of code paths.

### R2. Paid-path generation timeout of 600 s

This goes away with A1. Keep the long timeout on the GEMINI_WEB path only.

### R3. Agnes provider in the live path

The live provider is OpenAI (memory `image-providers-in-use`), and yosefdwolf keeps Agnes as an
admin-only option. If Agnes is only used for free local runs, move it next to the gemini-web
local-book path, out of the production registry. Owner call (Q3).

### R4. Settings surface

`app/config.py` has 59 settings. Each friend has a handful of env vars. After A1 and R1, go
through the list and drop any setting that nobody has changed from its default on Render.

### R5. `book_runner.py` size

`book_runner.py` is 2,966 lines, which is about the size of eliTouro's whole server. This isn't a
reason to rewrite. But A1–A3 touch the page loop, so pull the attempt loop out into its own
module while doing them, rather than adding to it.

## Keep: where we're ahead

- **Object storage.** All three keep images in Postgres. Ozer7 estimates 60 books fills the Neon
  free tier. We're on R2 (memory `r2-blob-storage-plan`).
- **Print quality.** All three print at 136–171 dpi with no upscaling, and two of them list that
  as a known defect. We upscale at order time and build print files in the background.
- **CI and tests.** None of them run CI.
- **Page edits and sharing.** Only eliTouro has edits (free-text fix + Undo). Nobody has share
  links.
- **Fixed supporting-character portraits.** These match their "character bible", and ours are
  already approved (memory `fixed-portraits`).

## Questions for the owner

1. **Render plan.** Stay on free and add a keep-warm ping (A7), or move to Starter at $7/mo like
   Oriel (Ozer7)?
2. **Same hero-less pages in every book (A3).** OK? All three friends ship it this way.
3. **Agnes (R3).** Keep it in production, or local-only?
4. **Head-crop edits (A5).** Worth the rewrite once A1–A4 are measured, or only if we're still
   over 60 s?
5. **Word placement.** Do we place words around detected face boxes the way all three friends do?
   If not, add it as A10.

## Status (2026-11-16)

Owner answers: stay on Render free with no keep-warm (A7 no); copy hero-less pages (A3 yes);
Agnes local-only (R3); build head-crop edits now (A5); no word placement (Q5, the text band
under the art never covers faces).

| Item | State |
|---|---|
| A3 hero-less pages | Done. Red pages 7–10 have been approved pre-made pages on live since 2026-11-11. |
| A5 scene edits | Code 154bb20, migration 0025, deployed (154bb20 live). Scenes for all 16 remaining star pages (1–6, 11–20) were drawn free in Gemini-web, checked by hand, and approved on live with `figure` boxes. |
| R2 picture timeout | Done, 8cc7829: two minutes, not ten. |
| R3 Agnes | Done: out of the production registry, local only. |
| R4 config cleanup | Done, 921d35a. |
| A9 time-left estimate | Done, 5717d6c. |
| A7 keep-warm | Won't do (owner). Cold start measured at about 70 s on 2026-11-16. |

Lessons from making the scenes. Every book keeps the supporting cast exactly as drawn, so:
- check each scene against the cast portraits; Gemini added glasses to the woodcutter twice;
- tell Gemini "no lines, arrows, numbers or labels", because it once drew measurement marks;
- keep the figure box on the stand-in child alone, so the edit can't change anyone else.

Still open: one timed live Red book to measure A5 end to end (at most 2 live books).
