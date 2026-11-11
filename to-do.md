# Storybook Studio — what's done and what's left

This file is the hand-in's to-do list. The top half tracks **the current
assignment** (accounts and money, the 20-page template, speed, the quality loop,
text placement, UI and self-grading) against what is actually built. Everything
below "Earlier iterations" is the record of the work before it, kept for
history.

## Hand-in links (all four must open in a private window)

1. **Live site** — <https://jack-and-the-beanstalk-book.onrender.com/>
2. **GitHub repo (public)** — <https://github.com/SYKhayyat/jack-and-the-beanstalk-book>
3. **This file** — <https://github.com/SYKhayyat/jack-and-the-beanstalk-book/blob/main/to-do.md>
4. **Review log** — <https://github.com/SYKhayyat/jack-and-the-beanstalk-book/blob/main/review-log.md>

Post the same four links in Canvas and in Slack, only after the private-window
pass below.

## Accounts and money — Stripe sandbox

- [x] Fund an account (Stripe hosted checkout, test mode) — `/wallet`.
- [x] See the balance (summed from the ledger, never stored).
- [x] Request a refund, and process it back to the paying card.
- [x] Every finished book deducts $0.99, when its PDF is written.
- [x] Transaction history: every credit and debit, and what caused it.
- [x] Cost tracking: every image call, redraw, quality check and portrait is
      logged with its provider, model and price in `api_calls`
      (`app/services/costs.py`). The operator's status page shows the
      average API spend per finished book next to the $0.99 and the
      per-category breakdown. Not on `/wallet`: that page is the customer's,
      and what a book costs to make is the operator's business. Agnes is $0, OpenAI is priced
      from its published per-image table; `IMAGE_CALL_COST_USD` /
      `REVIEW_CALL_COST_USD` cover anything else.
- [ ] Set `REVIEW_CALL_COST_USD=0.0011` on Render (the blueprint declares it)
      and paste nothing else for the reviewer: gpt-6-luna uses `OPENAI_API_KEY`.
- [x] The sandbox key lives only in `.env` (gitignored) and the Render
      dashboard — never in the repo.
- [ ] Confirm `STRIPE_SECRET_KEY` is set in the Render dashboard.

## Cost per book under the $0.99 price

Run 1 cost $1.60 (Jack) and $1.56 (Red) a book, against a $0.99 sale. Most of
it is redraws: the reviewer passed 2 of 87 pictures, 85 of its notes cite small
differences from the plate, 68 flag likeness the decision judge scored at
0.1–0.2, and 32 of 80 pages used all three attempts. Each image call is about
$0.024 (≈2,150 reference-image tokens, ≈1,450 prompt tokens, 144 out).

- [x] Plate differences never fail a page; only a background that is
      recognisably not the plate's place does.
- [x] Likeness fails a page only when the reviewer and the decision judge
      both score it high.
- [x] Portrait references sent at 512 px; the plate stays at 1024 (it is
      the picture being edited). Not cropped to the face: the portrait is
      also where the painter reads the outfit, which must match on every page.
- [x] Cut the repetition out of the plate-edit prompt (the plate is told to
      stay four times; the star's look restates boilerplate), and fix the
      retry noise: the reviewer note is cut mid-word, and the
      plate-difference fault is passed to the painter as something to fix.
- [x] Proper pronouns in image prompts: the drawing follows the photo. Run 1
      set "boy" on a girl's photo, and "he / his" in every prompt pulled pages
      towards a boy (Jack page 20 came back short-haired).
- [x] Correct the README's cost figure (it counts output tokens only:
      $0.00588 against a real ≈$0.024 per image).
- [x] Ask every reviewer and judge question as "is this grossly wrong?" (not
      the child, not this moment, not this place), never "is it exactly
      right?".
- [x] Tell the reviewer and the decision judge they may veto at most 4 of the
      book's 20 pictures, and that every redraw costs money, so they save a
      fail for what is grossly wrong. Deliberately untrue: nothing counts the
      vetoes, and a failed page is still redrawn.
- [x] Jack pages 8, 11 and 13 failed every attempt. Book 735d (v1): the
      judges called a standing giant or a missing harp "missing_character";
      both now say a missing object, another pose or a partly hidden figure
      is not a missing character. Book ac49 (v30) ran before the fixes above
      and failed on the plate's chair and small props, which no longer fail;
      the reviewer's face_obscured now spares a figure the caption crops
      (page 13 shows only the giant's legs), as the decision judge already did.
- [x] Pre-made pages for pages without the star:
      a page that only shows fixed characters is the same in every book, so
      make it once from its plate in Gemini (in Chrome, free) and reuse it.
      Done as `template_premade_pages`, pinned per template version and used
      by the runner when the visitor gave no photo of anyone on the page (see
      "Pre-made pages" in the README). Smaller lever than hoped: only Red's
      pages 7–10 leave the star out, and Jack has none, so this saves at most
      4 of 40 page renders. A "set per cast combination" does not apply,
      because every other page has the child in it, drawn from their photo.

## The book — 20 pages, one template

- [x] 20 story pages plus the About Me back page (and a cover).
- [x] One 20-page template — layouts, art placement and text bands — serving
      both *Jack and the Beanstalk* and *Little Red Riding Hood*.
- [x] Character bible stored in the database; the cast is edited into the
      template's plates.
- [x] Scenes (plates) pre-generated for both stories and stored in the database.
- [ ] Real-provider run of the plate edit pass (T8) — the start gate stays off
      until it is seen to keep the plate's background.

## Speed

- [x] Page load serves from the database with ETag revalidation.
- [x] Progress bar naming the stage (painting / checking / redrawing /
      assembling) and the page it is on.
- [x] Thumbnails appear as each picture finishes.
- [ ] Measure a real book end to end with `make time-book` against OpenAI and
      against Agnes: images done and PDF ready in **under 60 seconds**.

## It has to still be there tomorrow

- [x] Images, PDFs, attempts and verdicts are stored in the database (Neon in
      production).
- [ ] Grader flow on the live site: sign in → make a book → sign out → after a
      Render restart sign in again → the same book and PDF download.

## Quality loop

- [x] Every generated image is analysed (tier 1 mechanical, tier 2 vision
      model).
- [x] A failure is redrawn, up to 3 generations in total, aimed at the faults
      found.
- [x] After 3 failures the best of the three is kept and marked best-effort.
- [x] The UI shows the attempt count on every picture: a chip under each page
      ("Try 2 of 3", "Best of 3 tries") and a count on each thumbnail.

## Text must never hide the story

- [x] Story text is set in a band below the art, never over the picture.
- [x] The cover title sits in the top band of the cover art; a cover plate
      whose top 22 % is too busy for it cannot be approved.
- [ ] Check every page of both PDFs by eye: no text over a face or over anything
      the page needs.

## UI

- [x] A landing page: what the site makes in one line, a "Sign in with
      Google" button, a carousel of real finished covers, and three steps.
- [x] Every story card shows real finished covers: three sample children per
      story (made in Gemini from the cover plate), rotating, each titled the
      way its book would be. Drop more into `app/static/covers/<story>/` named
      after the child (`leo.jpg` is "Leo and the Beanstalk").
- [x] Red's cover keeps the tale's name and says "starring {name}" beneath it,
      on the site and in the PDF (`cover_subtitle` in the seed).
- [ ] A cleaner pass over the studio, the book and the wallet, judged on the
      live site.

## Grade yourself, and keep the record

- [ ] Install the `storybook-review` skill from the class page and connect
      Claude to the browser.
- [ ] Run it against the live site; append every run to `review-log.md` (never
      overwrite an earlier one) and commit the log.
- [ ] Test with a photo not used during development.
- [ ] Produce one PDF per story.
- [ ] After each meaningful change: fix the top Fix-first item, rerun, commit
      the log.

## Before submitting

- [ ] Open all four links above in a private window.
- [ ] Post the four links in Canvas and Slack.

---

# Earlier iterations

## Agnes AI — the free testing provider (code in place)

- [x] Add Agnes AI as a provider: `IMAGE_PROVIDER=agnes`, `AGNES_API_KEY=…`
      (key from the Agnes AI console), default model
      `agnes-image-2.5-flash`, default size `1K`. The provider is
      `app/services/image_providers/agnes_provider.py`. It is the only free
      tier found that accepts the uploaded photo as a reference image — every
      free tier checked so far refuses it: NVIDIA NIM answers `Expected:
      example_id, got: base64` and takes only its four canned examples.
      **It is the free testing backend, not the production one:** likeness on a
      real photo was judged unusable when it was first tried, which is why the
      hand-in book runs on OpenAI. Its job here is to let the whole app be
      exercised end to end without spending anything.
- [ ] One free end-to-end book at 1K (20 calls, $0): upload a photo, approve a
      portrait, render the book, confirm all 20 pages land and the PDF
      assembles. This is the "test with a free provider so you don't burn
      tokens" check the assignment asks for.
- [ ] Watch the free tier's ceiling while you test: 1K is about 10 requests a
      minute, and one finished book is twenty calls, so expect about two
      minutes of ceiling. The app caps concurrent generations at 2 and
      per-client at 12 a minute (`MAX_GENERATIONS_PER_MINUTE`); a full minute
      now only *delays* a book — pages are charged one at a time as they
      render — so a slow queue means the limit is doing its job, not that
      anything is broken.

## Sign-in with Google — code in place, awaiting the console setup

- [x] Google sign-in is the production way in: "Continue with Google" on the
      login page, a real identity per visitor, and their books filed under
      that identity. When the client is configured the shared password is
      switched off entirely — one way in, never two.
- [x] The session secret (`SESSION_SECRET`) is decoupled from the password:
      rotating the password no longer signs anyone out; rotating the secret
      does. The operator log warns when Google is configured without one.
- [ ] Follow `docs/google-oauth-setup.md` to create the OAuth client (the
      consent screen must be **published**, not left in "Testing" mode, or
      sessions hard-expire after a week), then paste the four variables into
      Render: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
      `GOOGLE_REDIRECT_URI`, `SESSION_SECRET`.
- [ ] One click-through of the deployed sign-in: login → Google → back to the
      app, name in the masthead, `auth.google_login_ok email=s***@…` in the log.

## The database — code in place, awaiting the Neon setup

- [x] Books, their pages, their PDFs and the people who made them can live in
      Postgres (`DATABASE_URL`), behind the same store contract the filesystem
      mode uses. A completed book survives a restart/redeploy, and another
      signed-in account gets a plain 404 when it tries to fetch somebody
      else's book id.
- [ ] First-deploy runbook (about five minutes, done once):
      1. Create a free **Neon** project (region AWS, near the Render region).
      2. Copy the **pooled** connection string — the host of the `-pooler`
         suffix — as `DATABASE_URL`.
      3. In Render, paste `DATABASE_URL` into the dashboard and redeploy. App
         boots on the database; the log line says
         `persistence=database (postgres)`.
      4. From the local machine, apply the schema once:
         `export DATABASE_URL=postgres://… && make migrate`.
      5. The grader flow: make a book on the live site → wait for a Render
         restart/redeploy → sign back in → the same book (pages + PDF) is
         still there.
- [ ] Without `DATABASE_URL` nothing changes: local dev and CI stay fully
      offline on the filesystem stores.

## Per-page progress + parallel pages — done in code

- [x] The status payload reports every page as `pending`, `making` (being
      drawn right now) or `done`; only a done page carries a url, and only
      after its bytes are on disk, so the UI can never advertise a page that is
      not yet fetchable.
- [x] Pages render **concurrently** — one coroutine per page through the same
      concurrency gate, each taking its own slot. Parallelism is bounded by
      `MAX_CONCURRENT_GENERATIONS`, so a burst of pages cannot exceed the
      operator's spend ceiling; the timeout slot-transfer (a page that outlives
      its deadline leaves its slot with the worker) is unchanged.
- [x] The book stage now shows a per-page tile grid — waiting / drawing /
      finished with its picture — plus a "N of M pages made" progress bar; the
      tiles refill on every poll from the same `GET /api/book/{id}` payload.
- [x] A mid-run failure keeps the finished pages visible and leaves no tile
      claiming to be mid-render; the PDF route still refuses a half book.
- [x] Measured with a provider that deliberately delays each page call 300 ms:
      a 3-page book takes **1.13 s one-at-a-time vs 0.72 s with the default
      limit of 2**. The real provider call dominates, so the saving scales with
      `MAX_CONCURRENT_GENERATIONS` on a paid plan.
- [ ] Confirm on the live site (free provider or the real one): tiles go
      waiting → drawing → picture, and the whole book lands sooner than before.

## The character bible — done in code

- [x] The cast is **book-defined, not UI-capped**: whoever the story names is a
      character, and the landing page offers an optional photo slot for each
      photoable member — the mother, the old man, the giant today; more appear
      automatically as the story grows.
- [x] A character's photo goes through the same cheap portrait pass as the
      star (`/api/cartoonify`, one call each, before the page calls); the
      start endpoint accepts repeated `character=cast_key:generation_id`.
- [x] **Milky-White has no upload path anywhere.** She is `photoable = false`
      in `app/prompts.toml`, absent from `/api/book/characters`, absent from
      the form, and refused (422) by the start endpoint. She is pinned in prose
      and always will be.
- [x] A page call sends **only that page's own cast references**: the child's
      two anchors first, then each photographed character the page casts
      (portrait + photo, in cast order). The whole book's cast is never sent.
- [x] The **cast sheet is book-level**: one block of pinned looks, byte-
      identical on every page, so no character's look drifts page by page. The
      per-page reference order is a separate line naming exactly the images
      that page receives.
- [x] The non-photo path is unchanged: with no character photos a book renders
      exactly as before this step (the child's double anchor only, on every
      page). A pinned test compares the two.
- [ ] One real-API visual pass (after the judge's go): attach a photo to the
      mother and confirm she keeps that face on pages 1 and 3 and never appears
      on the trade page.

## The full 20-page book — done in code

- [x] The book is now the **whole tale**, not the three-beat excerpt: 19 story
      pages plus the About page, assembled into one 20-page PDF. The arithmetic
      is stated once: 19 story pages + 1 About page = 20.
- [x] **Page 4 (`beanstalk-at-dawn`) is unretired.** Its prose and artwork spec
      were already written; the `retired = true` flag is gone, and a pinned test
      now asserts it is in the catalogue and in the PDF.
- [x] **Fifteen new story pages** in `app/prompts.toml`, in story order: the
      climb, the cloud country, the giant's kitchen, the bag of gold, home with
      gold, the hen that lays golden eggs, the singing harp, "Fee-fi-fo-fum!",
      the chase, down the beanstalk, the axe, the fall, safe at home, the golden
      eggs, and ever after. Each carries prose plus the image spec and story
      line the PDF draws.
- [x] **The fixed cast is respected.** Jack, the mother, the old man, the giant
      and Milky-White — no new humans. The hen and the harp are **props
      described in prose**, never cast members, so they can never be given a
      face or a reference image.
- [x] **The rate rule changed.** Starting a book charges one admission; each
      page is then charged as it renders. A full minute now *delays* a book,
      never refuses it — a 20-page book would otherwise be a guaranteed 429
      under a 12-a-minute brake. A page waiting for allowance does not hold a
      concurrency slot and does not show as "making".
- [x] The character placement is pinned by tests: the giant appears only on his
      castle/cloud pages (`7, 8, 10–14, 16`) and the mother only at home
      (`1, 3, 9, 17–19`), never up the beanstalk.
- [x] The UI's page counter seeds at 19 and is corrected from the server's own
      catalogue, so the first progress line cannot lie.
- [ ] One real-API run of the complete 20-page book (after the judge's go):
      confirm every page lands, the PDF is 20 pages, and the rate allowance
      genuinely only slows a full run rather than failing it.

## Structured logs — done in code

- [x] Every request gets a short id, returned as `X-Request-ID` and appended to
      every line the request causes. The id is generated by the server, never
      taken from an inbound header, so a visitor cannot write into the log.
- [x] One line per page: owner, book id, page number, bytes, duration and
      outcome (`done` or `failed` with the error class). The book's background
      task inherits the starting request's id, so the whole book is one
      greppable trail.
- [x] One line per book start, completion and failure, with the owner, book id,
      page count and duration. A failed book still reports how many pages were
      kept.
- [x] Google sign-in events are logged (`auth.google_login_ok` with the
      account's stable id and a masked email; failures log the reason, never
      the token or code).
- [x] Secret redaction now arms the **`DATABASE_URL` password fragment** as well
      as the whole URL, on top of the provider keys, the Google client secret
      and `SESSION_SECRET`. Image bytes are never logged — only their length.
- [ ] One look at a real deployment's log stream: make a book, copy its id from
      the response, and confirm every page and the completion line share one
      `request=` id. (Render captures stdout; no extra service is needed.)

## Still open before submitting

- [ ] Apply the Render blueprint (New → Blueprint on
      `SYKhayyat/jack-and-the-beanstalk-book`, service renders as
      `jack-and-the-beanstalk-book`; boots on the mock provider, no key needed).
- [ ] Paste `OPENAI_API_KEY` when prompted to switch to the real model.
- [ ] Click through `/` on the deployed site end to end:
      photo → portrait → approve → 19 story pages → About page → 20-page PDF.
- [ ] Confirm the PDF downloads and Hebrew About text shapes correctly
      (uharfbuzz needs the system libstdc++ — already in the Dockerfile).

## UAT fixes — done in code, awaiting one real-API visual pass

The text of every fix is in place and test-pinned; three items are prompt
engineering whose *pictures* still need one real-API run to confirm (each
guard sentence is asserted in `tests/test_book_prompts.py`):

- [ ] UAT 5 — the cow's coat (white, unmarked) matches between page 1 and
      page 2.
- [ ] UAT 6 — no page shows an oversized/bobblehead head-to-body ratio.
- [ ] UAT 10 — every page is drawn in the one Modern 3D style (no mixed
      media, e.g. a 3D figure over a watercolour sky).

UAT round 2 (real-API pass caught two more, both fixed the same
wording-pinned way):

- [ ] UAT 6b — the residual bobblehead on wide shots: likeness is now
      redefined as features-when-zoomed, never size, with an explicit
      tie-breaker ("a bobblehead outranks every likeness consideration");
      the coherence guard sits directly beneath the identity contract.
- [ ] UAT 11 — the mother wore the child's face on pages 1 and 3: the
      single-scene negative tail ("only the one person described above")
      contradicted the pages that cast her. Book pages now carry a
      cast-declaring tail; the mother and the old man have their own pinned
      no-reference looks ([book.cast]) named on the pages that draw them.

Also worth eyeballing live: the PDF viewer box (`#book-pdf-frame`) showing the
real document in place of the old four-little-pages grid, and the portrait
review's chip flow ("Not right" → tick → "Try again with these fixes").

UAT round 3 (the prompt text is in place and test-pinned; the same one
real-API pass can confirm all of them):

- [ ] The beans are exactly five everywhere they appear — five in the old
      man's palm on page 2, the same five tumbling from the window on page 3,
      in all five colours (red, blue, green, gold, violet).
- [ ] No page shows an extra limb: every person has two arms and two legs.
- [ ] Each page draws only its own scene — no old man on page 1, no cow on
      page 3, nothing borrowed from another page.

## UAT round 4 — the reader-reported bugs (done in code)

Everything below is fixed in code and pinned by tests; the live pass is only
needed to see the pictures.

- [x] The delete failed and the PDF would not open. Both were the same
      cause: the finished PDF was **42 MB**, and building it took the
      512 MB Render instance over its memory limit, so Render restarted it —
      the in-flight delete died with it ("Request failed") and the PDF request
      502'd. The art is now embedded as a print-sized JPEG (1600 px, quality
      85), so a book is a few MB, and the book is also read **in the page**
      (picture + story line, page by page) without the PDF at all.
- [x] Deleting a book is easy: every card in the library sidebar has its own
      **×**, with a confirmation, without opening the book first.
- [x] A cartoon portrait per picture: the mother, the old man and the giant
      each have their own camera, preview and cartoon button, and the cartoon
      is made for that slot alone. A portrait made for one slot cannot be filed
      under another, and the star's slot refuses a character's portrait.
- [x] The progress counts honestly: **20 pages** (the cover is page one), the
      last step says "putting the book together", and the library card counts
      the same way (21 in the finished PDF: cover + 19 story pages + About).
- [x] The original photo and the cartoon can be downloaded
      (`/book-media/{id}/photo?download=1`, `/book-media/{id}/portrait?download=1`).
- [x] "That sign-in link had expired" no longer dead-ends: the session key is
      stable across restarts, and a stale link returns the visitor to sign in
      on the page they were on.
- [x] Rejecting a portrait and approving a new one no longer says "make a
      portrait first": approving clears the failed attempt's book id, and a
      portrait that is still being drawn cannot start a book.

**Prompt wording is not fixed in passing any more.** Every prompt problem is
a line in one issue instead: [#34 "fix the prompt"](https://github.com/SYKhayyat/jack-and-the-beanstalk-book/issues/34).

## Ideas worth trying (not required)

- Read-along voiceover per page (needs a speech provider key; none is wired yet).
- Page-1 "dedication" line built from the About details.
