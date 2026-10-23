# The Book Feature — What's Done and What to Do Next

The picture-book iteration is built, tested end to end (real-server smoke:
photo → portrait → 4 pages → 5-page PDF) and checked — ruff, mypy and the full
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
      photo → portrait → approve → 4 pages → PDF.
- [ ] Confirm the PDF downloads and Hebrew About Me text shapes correctly
      (uharfbuzz needs the system libstdc++ — already in the Dockerfile).

## Ideas worth trying (not required)

- Read-along voiceover per page (needs a speech provider key; none is wired yet).
- Page-1 "dedication" line built from the child's details.
