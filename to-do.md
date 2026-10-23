# The Book Feature — What's Done and What to Do Next

The picture-book iteration is built, tested end to end (real-server smoke:
photo → portrait → 3 pages → 4-page PDF) and checked — ruff, mypy and the full
pytest suite are green. This repo is the second commissioned iteration:
a copy of the first app with the picture book added as its own story.

## The three deliverable links

1. **Render site** — <https://jack-and-the-beanstalk-book.onrender.com/book>
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
- [ ] Click through `/book` on the deployed site end to end:
      photo → star details → About Me → portrait → approve → 3 pages → PDF.
- [ ] Confirm the PDF downloads and Hebrew About Me text shapes correctly
      (uharfbuzz needs the system libstdc++ — already in the Dockerfile).

## Ideas worth trying (not required)

- A fourth page (the beanstalk at dawn). Adding a page is one new
  `[[book_pages]]` entry in `app/prompts.toml` plus tests.
- Read-along voiceover per page (the server has Azure Speech wiring already).
- Page-1 "dedication" line built from the child's details.
