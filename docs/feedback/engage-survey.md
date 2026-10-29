# The Engage survey — paste-ready

## The short description (one line, for Engage to build from)

> Upload one photo of a person, get a cartoon version of that person, then read
> a 20-page picture book that stars them and tell us what you think.

## How to build it in Engage

1. Create a new study. Title: **"A picture book that stars you"**.
2. Paste the question block below, in order, as a new section.
3. Set the section to **full width** with the PDF shown in an embedded viewer —
   see *Showing the book* below.
4. Time-box the section to **3 minutes**. The instrument is built to fit: eight
   questions, six of which are one tap and two of which are one line.
5. Turn **off** any requirement that would force a participant to scroll past
   the PDF before answering: the book is the whole study.

## Who is being asked

Ten adults recruited through CloudResearch Connect, at least 18, who can read
English comfortably. They do not need to know anything about AI, picture books
or this project. They do need **a Google account** — the app asks them to sign
in with Google so their book waits for them if they come back.

## Showing the book

The book is one PDF. The study shows the whole document, not a page at a time,
so the participant can flip through it and judge the whole arc:

> Here is your book. Take a look through all twenty pages — click through them
> or scroll, whichever you prefer. There is an About page at the end that has
> your name on it.

Two things that matter for honesty:

- The participant's own photo was uploaded, so the pictures are of a real
  person they chose. Say so once, at the top: the book was made from the photo
  they uploaded.
- Nothing about the pictures being made by a machine, or by which service, is
  stated by the app. Keep it that way in the survey text: the questions ask
  about the *book*, never about the tool that made it.

## The questions, in order

**Intro screen (no question, just the framing)**

> Thank you for taking part. This takes about three minutes.
>
> You will look at a short picture book. It was made from a photo of a person —
> you can see that person on every page. Read through the whole book, then answer
> a few questions about it. There are no right answers; we want your honest
> reaction, including the parts you did not like.
>
> You can take as long as you like on the book itself. The questions after it
> take about two minutes.

**Q1 — Does it look like them?** *(single choice, required)*

> Looking at the person in the book — does the picture of them look like the
> same person who was photographed?

- Yes, clearly the same person
- Roughly — I can see it is meant to be them, but something is off
- No — a different person
- I cannot tell

*Why this one is here:* it is the assignment's first graded question, asked in
the participant's own words rather than the developer's.

**Q2 — Does the story ring true?** *(single choice, required)*

> Read the story. Does it feel like a real story that holds together from the
> first page to the last?

- Yes — the story makes sense and carries me through it
- Mostly — the story is fine but some pages do not fit
- No — it does not hold together as a story
- I did not read it

*Why:* the second graded question. The scale separates "weak arc" from "one odd
page", which is the difference between two different fixes.

**Q3 — Does it look professional?** *(single choice, required)*

> Ignore the story for a moment and just look at the pictures. Do they look like
> they were made by a professional — the sort of book you would pay for?

- Yes, it looks like a published book
- Almost — good, with one or two rough pages
- No — it looks homemade or unfinished
- I am not sure

*Why:* the third graded question. "Almost" is the answer worth hunting: it is
the one that names the pages to redo.

**Q4 — What is not good?** *(free text, optional, one box)*

> If you could change one thing about this book, what would it be?

*Why:* the fourth graded question, deliberately open. A single change rather
than a list, because "change one thing" surfaces the thing that actually
bothered them instead of a pile of small talk.

**Q5 — The hard one: the smallest detail.** *(single choice, required)*

> Now look very closely at the pages. Pick any page and look at the small
> things: hands, feet, the number of objects, anything written anywhere in the
> picture, the same thing drawn on two pages.
>
> Did you find anything that is wrong, even slightly?

- Yes — something is wrong on at least one page
- No — everything looks right to me

**Q6 — Where?** *(only shown when Q5 = "Yes"; free text, required)*

> Which page, and what is wrong about it? If it happens on more than one page,
> say so.

*Why:* the assignment's "one hard question that catches smallest-detail
failures". Asked in two parts on purpose: Q5 is a commitment, Q6 is where the
detail gets named. Free text is fine here — this is feedback to the developer,
not text sent anywhere near a picture generator.

**Q7 — Would you show it to someone?** *(single choice, required)*

> Imagine this book was made for someone you know — a child in your family, a
> friend, yourself. Would you happily show it to them?

- Yes, I would be pleased to show it
- I would show it but apologise for something
- No, I would not show it

*Why:* the question that no other question answers. Q3 asks whether it looks
professional; this one asks whether the whole thing is good enough to hand over.
It is the number a publisher would care about.

**Q8 — Anything else?** *(free text, optional)*

> Anything else you want to tell us about the book?

*Why:* the catch-all. Small, optional, and it is where the surprise findings
land.

## The prompt book each participant saw

For the record, so the responses can be read against what was actually shown:

- One photo of a person, uploaded by the participant.
- One cartoon portrait of that person, approved by the participant.
- Nineteen story pages plus an About page with the name the participant typed.
- Delivered as one PDF, opened in the survey.

## What the survey deliberately does not ask

- Nothing about how the pictures were made, or by what. The app says only
  whether the pictures are real; the survey does not ask anyone to guess.
- Nothing about price or willingness to pay. The assignment's price question is
  a business question, not a quality question, and mixing the two muddies both.
- Nothing about the photo upload step. That step exists so the book can be made;
  the study is about the book.

## Timing check

| Part | Expected time |
| --- | --- |
| Intro + sign-in | 30s |
| Flipping through the book | 60–90s |
| Eight questions | 45–60s |
| **Total** | **~3 minutes** |
