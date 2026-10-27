# The Book Feature — What's Done and What to Do Next

The picture-book iteration is built, tested end to end (real-server smoke:
photo → portrait → 4 pages → 5-page PDF) and checked — ruff, mypy and the full
pytest suite are green. This repo is the second commissioned iteration: the
picture book is now the whole app — one page, portrait first, then the book.
The first app (portrait+scene per picture) lives separately and shares no word
of UI with this one.

## The three deliverable links

1. **Render site** — <https://jack-and-the-beanstalk-book.onrender.com/>
   (create via **New → Blueprint** on the repo; `/healthz` should show healthy)
2. **GitHub repo (this one, public)** —
   <https://github.com/SYKhayyat/jack-and-the-beanstalk-book>
3. **This file** —
   <https://github.com/SYKhayyat/jack-and-the-beanstalk-book/blob/main/to-do.md>

## Agnes AI — the free provider (code in place, awaiting one real-API run)

- [ ] Add Agnes AI as a provider: `IMAGE_PROVIDER=agnes`, `AGNES_API_KEY=…`
      (key from the Agnes AI console), default model
      `agnes-image-2.5-flash`, default size `1K`. The provider is
      `app/services/image_providers/agnes_provider.py`. It is the only free
      tier found that accepts the uploaded photo as a reference image — every
      free tier checked so far refuses it: NVIDIA NIM answers `Expected:
      example_id, got: base64` and takes only its four canned examples.
- [ ] One real-API run to answer the question no test can: does likeness hold
      up? Upload a real photo, run the portrait step, then render a book, and
      compare the four pages against the portrait. Independent reviews put
      Agnes image quality mid-pack, so this is the one thing standing between
      "free" and "usable".
- [ ] Watch the free tier's ceiling while you test: 1K is about 10 requests a
      minute, and one finished book is five calls. The app already caps
      concurrent generations at 2 and per-client at 12 a minute
      (`MAX_GENERATIONS_PER_MINUTE`), so a slow queue means the limit is doing
      its job, not that anything is broken.

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
