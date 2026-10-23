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

Also worth eyeballing live: the PDF viewer box (`#book-pdf-frame`) showing the
real document in place of the old four-little-pages grid, and the portrait
review's chip flow ("Not right" → tick → "Try again with these fixes").

## Ideas worth trying (not required)

- Read-along voiceover per page (needs a speech provider key; none is wired yet).
- Page-1 "dedication" line built from the About details.
