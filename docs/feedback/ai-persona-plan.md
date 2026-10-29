# The AI-persona harness — plan only, no code yet

**Status: design.** This file is the plan the handoff asked for. When the build
is authorised, the work is described in *The build* below and nothing in it is
written into this repo before then.

**Embargo.** The method is not published, not posted, and not linked anywhere
before Monday's class comparison. That covers this file, the persona set, the
harness and any results. The class sees the comparison; the internet sees it
after.

---

## What it is for

The assignment asks for ten responses from real people and ten from AI
personas, plus a short comparison. The real set is the evidence. The AI set is a
**method test**: can ten genuinely different readers, run locally against the
developer's own key, find the same things in a picture book that ten paid
strangers found — and can they find anything the strangers did not?

That question is only interesting if the personas are genuinely different from
each other. Ten copies of the same instruction with the name changed are not ten
personas, and a comparison run against them measures nothing.

## The thing being tested

One fixed book. The same book the real participants saw:

- one photo of a person, uploaded;
- one cartoon portrait, approved;
- nineteen story pages plus an About page, assembled into one PDF;
- that PDF is the input every persona reviews.

The book is **frozen before the run starts**: the PDF and its prompt record
(`prompts.json`) are copied into the run folder with the run's id, so a result
can always be traced back to the exact artefact it was about.

## What a persona is

Three parts, written out in full for each of the ten, and frozen for the whole
run:

1. **Who they are** — age, what they do, their relationship to picture books,
   how much they read, who they read with. Concrete and specific enough that
   two personas never collapse into the same reader.
2. **How they read a book** — whether they read every word first, flip through
   the pictures first, look for craft, or read it aloud to a child. This is what
   makes the answers differ in *kind*, not just in wording.
3. **Their standard** — what would make them say the book is good, and what
   would make them say it is not. A persona with no opinion is useless.

The ten are chosen to cover the space a real study would cover: a parent who
reads aloud nightly, a librarian, an illustrator, a childless adult who has not
held a picture book in twenty years, a primary-school teacher, a teenager, a
grandparent, a book reviewer, an ESL reader, and someone who is simply easily
bored. Ten genuinely different readers, not ten levels of the same slider.

Every persona answers the same eight questions the real participants answered
([engage-survey.md](engage-survey.md)), so the two sets are read against the
same ruler in [comparison-template.md](comparison-template.md).

## The shape of the run

```
one frozen book (PDF + prompts.json)
   |
   +-- persona 1 ----+
   +-- persona 2 ----+   one call each: the persona profile plus the eight
   +-- ...           +   questions plus the book, asked once, answered in full
   +-- persona 10 ---+
   |
   v
run-<id>/persona-1.json ... persona-10.json
```

- **One call per persona**, carrying the profile, the book and all eight
  questions. Ten calls for ten personas — not ten calls per persona per
  question, which would multiply the spend by eight for the same reading.
- **Output is one JSON per persona**, written as it completes, so a partial run
  still leaves evidence of the personas that finished.
- **No retry loop.** A failed call is logged and the persona is marked
  "unanswered". Retrying around a billable call is the habit this app forbids
  everywhere else, and the harness is not an exception to it.
- **Spend is recorded per call** — model, prompt tokens, completion tokens,
  cost estimate — in the run's header, so the class can see what the method
  cost rather than being asked to trust it.

## Guardrails (the reason this can be run safely at all)

These are the constraints the harness inherits from the app, restated because a
harness that runs on the developer's own key is exactly the thing that can go
wrong quietly:

1. **One-way pipe.** Persona answers are never fed back into the app's prompts,
   into `prompts.toml`, or into any picture call. The book's words stay the only
   words the app sends to a model. A persona's opinion is an input to the
   *comparison*, never to the *generator*.
2. **The harness is not served by the web app.** It runs locally, from a
   command, against the developer's key. It has no route, no endpoint, and no
   place in the visitor-facing surface.
3. **The book is frozen before the run.** No re-rendering inside a run: the
   picture and the prompt record served to persona 1 are the ones served to
   persona 10, or the set is not comparable.
4. **No key in any output.** Responses, headers and logs carry no key, no base
   URL and no model that a published artefact would leak. The app's existing
   redaction is applied to anything the harness logs.
5. **Nothing is published before the class comparison.** The run's outputs are
   private until the comparison has been shown.
6. **The persona set is written down and frozen.** Ten profiles in one file,
   versioned. A method whose personas change between runs cannot be repeated,
   and a method that cannot be repeated cannot be argued about.

## What gets built (for whoever gets the go-ahead)

Not now. When authorised, the work is:

1. A `personas.toml`-style data file holding the ten profiles (data, not code —
   the same rule the app follows for its prompts).
2. One local runner that reads a run folder, calls the model once per persona,
   and writes one JSON per persona plus a run header with the spend.
3. A reader that lays the ten answers next to the real ten in the comparison
   template.
4. The persona set is *chosen by the developer*, in conversation with the class
   requirement, and written out in full — the choosing is the work, not the
   calling.

Estimated spend for the whole method: ten short text calls against a private
key, single-digit cents. Verify on the day, and record it in the run header
rather than promising it in advance.

## What would make the method fail

Written down now, so the build is judged against them rather than around them:

- The ten personas sound alike. If two profiles produce answers that could be
  swapped without anyone noticing, the set is nine personas and one echo.
- Personas answer the *questions a developer asked* instead of the questions a
  reader was asked. Mitigation: the eight questions are copied verbatim from the
  real instrument, not reworded for a model.
- The comparison claims agreement it does not have. Mitigation: the counts in
  the comparison template name their denominators, and a persona that was never
  shown the detail (Q5) is reported as unanswered, not as "no".
- The method gets published before the class comparison. Mitigation: the embargo
  at the top of this file, and no link shared until it lifts.
