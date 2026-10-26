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

## NVIDIA NIM — the free provider (done in code, awaiting one real-API pass)

- [ ] Add NVIDIA's hosted NIM as a provider: `IMAGE_PROVIDER=nim`,
      `NIM_API_KEY=…`, `NIM_MODEL=flux.2-klein-4b` (free tier, ~40 requests a
      minute, no card). The provider is
      `app/services/image_providers/nim_provider.py`; the choice of provider
      and model is environment-only, and nothing a visitor can fetch names
      either — the page, the browser's JSON and `/healthz` all stay silent
      about the backend.
- [ ] One real-API run to settle two things the tests cannot: that NIM's
      likeness holds up (same question the OpenAI pass has to answer), and
      that the prompt condensation to NIM's 800-character cap (head and tail
      kept, middle dropped) still leaves a usable instruction. If the pictures
      suffer, `NIM_MAX_PROMPT_CHARS` and `NIM_PROMPT_TAIL_CHARS` are the
      dials, and a different `NIM_MODEL` is the other lever.

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
