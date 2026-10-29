# The Book Feature — What's Done and What to Do Next

The picture-book iteration is built, tested end to end (real-server smoke:
photo → portrait → 19 story pages → 20-page PDF) and checked — ruff, mypy and
the full pytest suite are green. This repo is the second commissioned
iteration: the picture book is now the whole app — one page, portrait first,
then the whole book.
The first app (portrait+scene per picture) lives separately and shares no word
of UI with this one.

## The three deliverable links

1. **Render site** — <https://jack-and-the-beanstalk-book.onrender.com/>
   (create via **New → Blueprint** on the repo; `/healthz` should show healthy)
2. **GitHub repo (this one, public)** —
   <https://github.com/SYKhayyat/jack-and-the-beanstalk-book>
3. **This file** —
   <https://github.com/SYKhayyat/jack-and-the-beanstalk-book/blob/main/to-do.md>

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

## Still open before submitting

- [ ] Apply the Render blueprint (New → Blueprint on
      `SYKhayyat/jack-and-the-beanstalk-book`, service renders as
      `jack-and-the-beanstalk-book`; boots on the mock provider, no key needed).
- [ ] Paste `OPENAI_API_KEY` when prompted to switch to the real model.
- [ ] Click through `/` on the deployed site end to end:
      photo → portrait → approve → 3 story pages + About page → PDF.
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

## Ideas worth trying (not required)

- Read-along voiceover per page (needs a speech provider key; none is wired yet).
- Page-1 "dedication" line built from the About details.
