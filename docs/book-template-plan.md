# The 20-page book template — plan

Status: **T1–T7, including T6.5, have landed on `main`.** T0 is still
blocked on a provider key; T8–T15 are gated behind T0 and Claude's T8/T9
contracts. Read [Where this actually stands](#where-this-actually-stands-2026-11-06)
first — it says what a visitor and an operator can each do today and the one
link still missing — then [Status of this pass](#status-of-this-pass-2026-11-06)
for the per-item record. Everything else here is the agreed plan, with
acceptance constraints that can be checked rather than argued about.

## Where this actually stands (2026-11-06)

**What a visitor can do today.** Make either book end to end — *Jack and the
Beanstalk* or *Little Red Riding Hood* (titled "{name} and the Wolf"): upload a
photo, stylise it, give the story's supporting cast their own portraits, start a
20-page book, watch the pages arrive, read it and download the PDF. Every word,
scene and look a book is drawn and printed from comes from its story's seed
(`app/books/<key>.toml`), loaded into the database at boot; the book pins the
exact page and bible revisions it started on, so a re-seeded page or a revised
look reaches the next book and never one already made. Pages are still
composed fresh by the image model (T8 is not built), so no approved plate is
painted into a book yet. Everything behind it — books, pages, PDFs, people,
wallets, retention, stories — runs on the one database from T3, and a fresh
checkout needs no database server: the app creates, migrates and seeds the
SQLite file on boot.

**What the operator can do today.** Prepare and review plates out of band, and
decide when each story opens. With Google sign-in configured and `OPERATOR_SUB` set to your Google
`sub`, `/admin/plates` walks story → contact sheet → one page. The story's
sheet has **Paint missing plates**, which paints every page that has no
candidate yet (one image call per press, repeated by the page itself until
there are none left); from a terminal the same job is
`python -m app.scripts.make_plates --story little_red_riding_hood` (Jack's
21 plates plan the same way, `--story jack_and_the_beanstalk`). On a page
you approve a candidate, re-roll it with an edited prompt, or retire a plate. Approving the last missing page writes
template version 1; replacing an approved plate writes version 2 and leaves
version 1 untouched. A cover whose top 22 % is too busy cannot be approved, and
the reason is shown in place of the button. Anybody who is not `OPERATOR_SUB`
gets a plain 404, and a password-mode deployment (no Google client) can never
reach the screen at all. Each story on `/admin/plates` has a **start gate**
(T6.5): ticked, a book of that story can start only on a complete template
version — the shelf says "Not ready yet" in one plain sentence, and the studio,
the portrait pass and `/api/book/start` refuse before any image is paid for;
unticked (the default), books start on the latest words and looks. A book that
does start on a version is pinned to it.

**What is not connected yet.** One link, and it is the payoff of the rest:

- **T8 — there is no edit pass.** `book_runner` composes each page from scratch
  with the image model; it does not take the book's pinned plate and paint the
  cast into its slots. Until it does, an approved plate changes what is stored
  and what a book is pinned to, not what a finished book looks like — which is
  why the start gate ships switched off.

**Waiting on someone else.** T0 needs a real provider key and three hand-made
plates before Claude can make the go/fallback call. T8 and T9 need Claude's
interfaces and `Verdict` schema before T10 can start. T2's Jack prose has not
had Claude's review pass (Red's 20 pages were reviewed when they landed), and
T7 added new prompt wording that T15's review must cover: Red's props-and-places
sheet, page rules, photo looks, portrait briefs and the red-hood costume.

## Decisions taken

| Question | Decision |
| --- | --- |
| Page count | **Cover + 20 story pages + About = 22 sheets.** Both stories. Supersedes the 19 pages in `ui-redesign-plan.md`. |
| Text placement | Story text **underneath the art**, on a **cream** page. Never over the picture. |
| Template method | **Empty plate generated once per scene**, reviewed by you, stored. Each book runs an **image-edit** pass that paints the characters into the plate. **Conditional on the T0 spike** (below). |
| Story words and cast | **Live in the database** (`story_pages`, `story_characters`), seeded from per-story TOML files kept in git. |
| Reviewer | **Two-tier:** free deterministic checks on every image, then a cheap vision model **on every image** too. |
| Template review | **In-app admin screen** (approve / re-roll / edit prompt per plate). |
| Cover title band | **Checked automatically**: a plate whose top 22 % is not empty can't be approved. |
| Storage | **Database required** for the new model; **SQLite by default** locally and in tests, Postgres in production. |
| Speed target | **Under 60 s for the normal one-pass book** (every image passes first time). A retry may exceed it; the progress bar still names the stage. |
| The Woodcutter | **Kept.** Red's cast is Red, Mother, Grandmother, the Woodcutter (all photoable) and the Wolf (not photoable). |
| Fee for a `best_effort` book | **Full fee.** The owner can redraw the weak page as today. |
| Not in this sprint | **Pricing** and **redraw spend limits** each get a later sprint. This plan changes neither the fee nor redraw rules. |

## Ownership — who builds what

Claude (Opus 5.5) access is limited, so it takes the few places where a
silent wrong answer is expensive and the design is hard to reverse. Everything
else is built by Buffy (MiMo 2.6). Ask Claude for **interfaces, migrations and tests**,
not whole screens: Opus decides and pins the contract, Buffy implements the
wide parts against it.

### Claude

| Item | Owner note |
| --- | --- |
| **T0 — read the spike results** | Buffy runs it; Claude makes the go / fallback call against the criteria in T0, because it decides the shape of T4–T9. |
| **T4 — schema, migration 0005, versioning/pinning** | Every later item depends on it. `template_versions`, "a book keeps the plates it started with" and the character-bible foreign keys are costly to undo once books exist. |
| **T8 — edit-pass contract** | Provider edit capability, reference limits, the edit prompt, and the background-preservation threshold. Whether the plate survives the character edit is the product's core quality question, not a typing task. |
| **T9 — quality-loop state machine and `Verdict` schema** | The invariants that spend money: never more than 3 billable image calls per page, every completed page has a verdict, deterministic best-of-three tie-break. The `Verdict` JSON schema is part of this contract; T10's adapter implements it. |
| **T3 — store contract tests only** | T3 is the long multi-file refactor the standing caveat warns about. Claude writes the tests that pin T3.1–T3.6 (SQLite boot, migration up/down on both engines, no filesystem writes, retention) before Buffy starts; Buffy makes them pass. |
| **T15 — prompt and guardrail review** | Required, not "if access allows": the plate and edit prompts shape every image of a child's face. One review pass over the assembled prompts and the T15 tests. |
| **T2 prose — review only** | Buffy drafts all 20 Red pages; Claude does one pass for voice, length and readability. Review is far cheaper than generating 20 pages of TOML. |

### Buffy

| Item | Owner note |
| --- | --- |
| **T0** — latency and background-preservation spike | Empirical; run it, record the numbers, hand them to Claude. |
| **T1** — template freeze + no-text-over-art tests | Mechanical; acceptance criteria already written. |
| **T2** — 20 Red pages, 20th Jack page and scene specs (draft) | Then Claude's prose pass above. |
| **T3** — database required, SQLite default, test migration | Wide but mechanical, against Claude's contract tests. |
| **T5** — plate-generation command | The prompt *language* may be refined by Claude or by the operator; the command is Buffy's. |
| **T6** — admin review screen | Largest single chunk, but CRUD + auth + templates with no ambiguity. |
| **T7** — character-bible wiring | Done: the page prompts read the pinned bible. Pinned looks approved by the operator. |
| **T10** — reviewer adapter, cost logging | Implements Claude's `Verdict` schema from T9. |
| **T11** — concurrency, measurement harness | Empirical; no benefit from Opus. |
| **T12** — stage names and thumbnails | |
| **T13** — page-load caching | |
| **T14** — persistence and second-login tests | |
| **T15** — guardrail tests and prompt audit | Then Claude's review pass above. |

### Two rules that matter more than the split

1. **T4 lands before T3 starts, and the two never run at the same time.**
   Claude's migration and store contract land first; then Buffy rewrites the
   store and tests against them. `db.py`, `db_book_store.py` and the store tests
   are the collision surface, so only one of them is open at any moment.
2. **The plate pipeline is built; its payoff is T8.** T5, T6, T6.5 and T7
   landed without needing Opus time. What remains on the critical path is the
   edit pass that *uses* a plate (T8, behind T0's numbers and Claude's
   contract). Until then, approving a plate changes what is stored and pinned,
   not what a book looks like.

**Standing caveat:** on long multi-file refactors Buffy can drift from the spec.
The acceptance constraints above are the contract — if T3 or T14 misses a
numbered criterion, send it back rather than patching around it.

## What the code already gives us

Worth stating so the plan does not re-solve solved problems:

- `app/services/pdf.py` already sets every story page on cream paper
  (`_PAPER = (250, 246, 236)`), art edge-to-edge on top, story in a margin
  beneath it, one font size per book. **No text can cover the art on a story
  page** — the fix is to freeze this and make it structural, not to design it.
- The cover is the one page where text sits over art (title lockup over a
  vignette). That is the only place the "no text over a face" rule needs a
  composition guarantee.
- Pages already render concurrently through `ConcurrencyGate`; status already
  moves `pending → making → done` per page.
- The cast sheet (pinned looks, photoable characters, the cow excluded) already
  exists in `app/prompts.toml` as `[book.cast]`.
- Book storage is behind one `BookStore` contract with filesystem and database
  implementations; the database is a switch.
- Image providers have **one call**, `generate(ImageRequest)`, which takes
  reference images. **None has an edit or inpainting call today.** Gemini takes
  at most 3 reference images per prompt (`gemini.py`).

## What already exists and must keep working

This plan changes how page art is made, so it touches features that are already
shipped. Each one is in scope only as far as stated here.

- **Book editing** (`POST /api/book/{id}/edit`: `page`, `character`, `cover`,
  `about`, `retry`, `delete`). A redrawn page or cover is a new edit pass on
  **the book's pinned template version**, through the same T9 loop and its
  3-call cap. Replacing a face reruns the edit pass on every page that casts
  that character. Redraw *spend limits* are a later sprint; this sprint only
  logs the number of redraws and their image calls per book (T10.3).
- **The wallet.** A book is charged its fee once, as today, including a book
  with `best_effort` pages. The fee and refund rules don't change here.
- **`ui-redesign-plan.md` §11.1 (story catalogue).** This plan implements it
  and replaces two of its details: story words and cast live in the
  **database**, with the per-story TOML files (`app/books/<key>.toml`) as the
  *seed* source rather than the runtime one; and each story has **20 pages**,
  not 19. The `books.story_key` column ships in this plan's migration `0005`;
  the UI plan adds no migration of its own for it. `catalogue.py` keeps the
  interface §11.1 describes (`stories()`, `story(key)`, `pages_for(key)`,
  `cast_for(key)`) and reads it from the database.

## Target shape

```
story catalogue (DB, seeded from TOML)  template plates (DB, versioned)
  Jack  · 20 pages + cover              empty art per scene, no people, no text
  Red   · 20 pages + cover              one slot per cast member on the page
  prose · scene spec · cast · text band            │
          │                                        │  (generated once, approved by you)
          └──────────────┬─────────────────────────┘
                         v
            template version (plates + bible hash), pinned per book
                         │
                         v
                 per-book edit pass
   plate + approved character portraits ──> final page art
                         │
                         v
       tier 1 (free) + tier 2 (vision), every image ──fail──> regenerate
                         │                        (max 3 total) ──> best of 3
                         v
              cover + 20 pages + About  →  PDF  →  DB  →  second login download
```

---

## T0 — Spike: can an edit keep the plate? How fast is it?

**Goal:** settle the two assumptions everything else rests on, before any
schema or screen is built: an image-edit call (1) keeps the plate's background
and (2) is fast enough for 60 s.

**Scope**

- Hand-make 3 plates for Jack (no admin screen, no database): one with one
  character slot, one with two, and the cover with its empty title band.
- On the production image provider, run 5 edits per plate with real
  portraits. Try the provider's true edit/mask endpoint if it has one, and
  `generate` with the plate as the first reference image.
- For each edit record: wall-clock latency, and background drift outside the
  union of the slots (perceptual hash per tile, compared with the plate).
- The operator eyeballs every result as "background kept" or "background
  redrawn", which calibrates the drift threshold T8 uses.

**Go / fallback criteria** (Claude reads the results)

1. **Go:** at least 12 of 15 edits are judged "background kept", and one drift
   threshold separates kept from redrawn. That threshold becomes T8.2's number.
2. **Fallback — plate as composition reference:** the background isn't kept
   reliably. T4–T7 still stand (plates are still generated, approved and pinned,
   and still fix the world, palette and framing). T8.2 and the tier-1 drift
   check in T9 are replaced by a tier-2 check that the scene matches the plate.
   The plan's first line changes from "paints the characters into the plate" to
   "redraws the scene from the plate".
3. **Latency:** at 8 s per call, 21 calls need concurrency of at least 3 to fit
   in 42 s; at 15 s per call they need at least 6. If the provider's rate limit
   can't supply that, the 60 s target becomes a provider decision, not a code
   decision.

**Acceptance**

1. The numbers, the 15 judgements and the go/fallback call are recorded in
   `docs/decisions.md`.
2. The spike script is committed, so the measurement can be repeated.

**Out of scope:** any production code. The spike script is throwaway.

---

## T1 — Freeze the page template and prove text can never cover art

**Goal:** one mechanical template, used byte-identically by both stories.

**Scope**

- Keep art on top, text band beneath, cream paper. Make the geometry explicit
  data on the page manifest rather than the current "whatever the longest page
  needs" decision.
- Both stories and the cover share the same page shape and margin rules.
- The cover plate reserves an empty top band for the title lockup; the
  characters are composed *below* it, so no face ever sits under the title.

**Acceptance**

1. For every one of the 22 sheets, a test asserts no drawn text cell has
   `y < art_height` (story pages), and the About page has no art under text.
2. The text band height is identical on all 20 story pages of a book, and the
   body font size is one value for the whole book.
3. A deliberately overlong page grows the margin and is never truncated;
   a deliberately short page still uses the same band.
4. The page's paper colour is cream (`250, 246, 236`) sampled from a rendered
   page, not just set in a constant.
5. The cover plate's top 22 % is checked automatically: if its pixel variance is
   over a fixed threshold (no flat sky or paper), the admin screen disables
   Approve and shows the reason. A test proves it with one flat plate and one
   busy one. The ruled overlay is still shown for the operator's eye.

**Out of scope:** typography redesign, new fonts.

---

## T2 — Story catalogue: 20 pages for Jack, 20 for Red, cover and About

**Goal:** the story text is written once, in the per-story seed files, lives in
the database at runtime, and both stories satisfy the same 20-page contract.

**Scope**

- Expand Jack from 19 to 20 story pages.
- Write **Little Red Riding Hood**: 20 pages of simple fairy-tale prose, plus
  its scene specs, beats, emotions, cast per page and must-include/must-avoid
  lists.
- Add a cover scene spec for Red (Jack already has one).
- Cast for Red: Red, Mother, Grandmother, the Woodcutter (photoable) and the
  Wolf (not photoable, pinned in prose).
- Stage crowded beats (the rescue at the cottage) so each page keeps to the
  reference cap in criterion 7.

**Acceptance**

1. Loader fails at boot unless every available story has exactly 20 story
   pages, one cover spec and one About page.
2. Every page's prose is 45–90 words and passes a readability floor: no
   sentence over 22 words, and no word outside the shared simple-vocabulary list
   **plus that story's own allow-list** (e.g. *beanstalk*, *giant*;
   *grandmother*, *woodcutter*).
3. Every page names only cast members defined in that story's character bible;
   an unknown name fails at import, not at request time.
4. Red's tale covers the whole arc across 20 pages: the basket, the warning,
   the wood, the wolf, the shortcut, grandmother's cottage, the disguise, the
   "what big eyes" exchange, the Woodcutter's rescue, and the moral. No page is
   filler.
5. The wolf is written as sly, never gory; no page requires a weapon, a corpse
   or blood. The Woodcutter's rescue is a chase-off or a shout, not a blow.
6. The prose reaches the PDF only — a test proves no page's story text appears
   in any image prompt.
7. **Reference cap:** no page casts more image-referenced characters (anyone
   with a portrait or an admin reference image) than the active provider accepts
   alongside the plate. For Gemini that's 2. The check runs at boot against the
   configured provider's `max_reference_images` (T8). Characters past the cap on
   a page appear only as prose-pinned background figures, or not at all.

---

## T3 — Database becomes required; SQLite is the default local database

**Goal:** one storage path, so templates and books cannot drift.

**Scope**

- Remove the filesystem book store from the runtime path.
- Empty `DATABASE_URL` → local SQLite file (e.g. `./storage/app.sqlite3`),
  created and migrated on boot. Production uses Neon/Postgres.
- Migration `0005` (written in T4) must run on SQLite: every `ALTER` uses
  Alembic batch mode (`op.batch_alter_table`).
- Books stored on the local filesystem are **dropped, not migrated**. This only
  affects local development; production already uses the database.
- Update `make install/dev`, README, `render.yaml` notes, the retention sweep,
  and `docs/decisions.md` §4 (which still says local development needs no
  database).

**Acceptance**

1. The app boots with no `DATABASE_URL` and makes a full book end to end; the
   database file is the only store.
2. `make migrate` is idempotent on both SQLite and Postgres; a new migration
   test runs the whole chain up and down on both.
3. No code path writes book artefacts to `<STORAGE_DIR>/books`.
4. Existing database-mode books still load and download unchanged after the
   migration.
5. Retention deletes expired books and their page rows, attempts and PDFs;
   a rendering book is never expired.
6. Tests never require a network or a Postgres server.

**Risk:** the current test suite leans on filesystem mode. Budget a real
migration of `tests/`, not a shim that keeps both alive in production.

---

## T4 — Schema: words, scenes, character bible, plates, template versions, per-book pins

**Goal:** store each story's words, plates and cast once; store each user's book
as a reference to one template version plus its own generated images.

**Tables**

- `stories` — key, title, blurb, available, current template version id.
- `story_pages` — story key, page number, page key, title, prose, beat, emotion,
  scene spec (JSON), cast keys, text-band rules, seed content hash.
- `story_characters` — the **character bible**: story key, cast key, display
  name, pinned visual description, photoable flag, reference image (optional,
  admin-supplied), provenance, seed content hash.
- `template_plates` — story key, page number (0 = cover), plate id, image,
  media type, prompt used, prompt hash, **slots** (JSON list: cast key,
  rectangle, scale hint, facing), status (`draft`/`approved`/`retired`),
  approved by, approved at.
- `template_versions` — id, story key, created at, **one approved plate id per
  page** (cover + 20, as a child table `template_version_plates`), and a hash of
  the story's words and character bible at that moment. This is the one thing a
  book pins.
- `books` gains `story_key` (default `'jack_and_beanstalk'`) and
  `template_version_id`.
- `book_pages` gains `attempt_count`, `review` (JSON: verdicts for every
  attempt, with tier), `chosen_attempt`, `best_effort` and `image_sha256`
  (used for ETags, T13). There is no per-page plate version: the plate comes
  from the book's template version.

**Acceptance**

1. A book records the template version it started with; approving a newer plate
   or editing a pinned look creates a new template version and never changes an
   existing book.
2. A plate or template version referenced by any book cannot be deleted; it can
   be retired.
3. `story_characters` is the single source of a character's look; page prompts
   read it and no page restates a description.
4. Seeding is idempotent and stamps a content hash, so re-seeding does not
   silently rewrite words or looks that a template version already pinned. A
   changed seed is a new template version, not an in-place edit.
5. Every stored page image has a matching review record; a query proves no
   book completes with an unreviewed image.
6. Migration `0005` runs up and down on SQLite (batch mode) and Postgres.

---

## T5 — Generate the template plates once per story (offline)

**Goal:** 21 plates per story (cover + 20), consistent world, no people, no text,
one slot reserved per cast member on the page.

**Scope**

- A command (`python -m app.scripts.make_plates --story red`) writes N candidates
  per page to the database as `draft`, with the exact prompt.
- Plate prompts in `prompts.toml`, under `[stories.<key>]`, derived from the
  same world/style blocks for all 21 plates.
- Each plate declares **a slot for every cast member on that page**: a
  rectangle, a scale hint and a facing direction, so the edit pass is told
  exactly where each character goes.
- Prompts must contain no named real person, no brand, no child-specific
  content — plates are generic by construction.

**Acceptance**

1. One command produces 21 plates for a story with no user data involved.
2. Every plate prompt forbids lettering, signage, captions, logos and people
   (including "no crowd", "no silhouettes"), and the negative list is asserted
   by a test on the assembled prompt.
3. The 21 plates for a story use one palette/lighting/time-of-day progression;
   an operator can see the sequence as a contact sheet.
4. A plate cannot become `approved` unless it has exactly one slot per cast
   member on its page.
5. Regeneration costs one call per plate per candidate; the command reports the
   number of calls before it makes them and requires confirmation when a paid
   provider is selected.

---

## T6 — Admin review screen for plates

**Goal:** you review and settle each plate in-app, without a deploy.

**Scope**

- Operator-only routes: list stories → pages → candidates; approve, re-roll one
  page, edit that page's prompt, retire a plate.
- A contact-sheet view of all 21 plates for a story, with the text-safe band and
  every character slot drawn as an overlay.
- Approving the last missing page, or replacing an approved plate, creates a new
  template version; nothing in the visitor UI changes for books already started.

**Acceptance**

1. A non-operator session gets 404 (not 403 with hints), matching how book
   ownership is handled today.
2. Approve is idempotent and records operator, timestamp and prompt hash.
3. Re-roll never overwrites or deletes an approved plate; it adds a `draft`.
4. The overlay toggle shows the reserved title band (cover) and every character
   slot, and each slot rectangle matches the stored slot exactly.
5. A book cannot start while its story has no complete template version; the
   shelf says so in one plain sentence.
6. Every action is logged with the request id and without image bytes.
7. The cover's automatic title-band check (T1.5) disables Approve with its
   reason shown.

---

## T7 — Character bible stored in the database

**Goal:** Jack, Red, the mother, the wolf and everyone else are data, with pinned
looks and optional user photos.

**Status (2026-11-06): done.** Both seeds hold everything a book is made from,
and `prompts.toml` holds no page and no look. See "T7 as built" under the
status of this pass for where it differs from the scope below.

**Scope**

- Seed `story_characters` for Jack from the existing `[book.cast]`, moved into
  `app/books/jack_and_the_beanstalk.toml`; write Red's in
  `app/books/little_red_riding_hood.toml`.
- Optional uploaded photo per photoable character, run through the existing
  portrait pass, stored per book (as today).
- Non-photoable characters (Milky-White, the wolf) are pinned in prose only and
  remain impossible to give a photo. Jack's seed goes one step further and
  leaves the cow out of `[cast]` entirely: she cannot be photographed, so the
  pages' places describe her and the plate paints her.

**Acceptance**

1. Every page's cast resolves to a bible row; a missing row fails at boot.
2. A non-photoable character has no API path, form field or UI control that can
   accept an image — asserted by tests at the route and form level, as today for
   the cow.
3. A photoable character's portrait is used only on pages that cast them.
4. The wolf's pinned look is identical on every page and includes the
   non-frightening constraint.
5. Updating a pinned look creates a new template version (T4.1), so old books
   keep their words, looks and plates.

---

## T8 — Per-book edit pass: characters into plate

**Goal:** each book is 21 edit calls against its pinned plates, not 21 fresh
compositions.

**Scope**

- Providers declare two capabilities: `supports_edit` and
  `max_reference_images`. The edit is implemented the way T0 chose: the
  provider's true edit/mask endpoint where one exists, otherwise `generate` with
  the plate as the first reference image and the cast portraits after it. Same
  `ImageRequest` shape where possible.
- A provider that cannot edit, or whose `max_reference_images` is below what any
  page needs (T2.7), is refused at start-up rather than degrading silently.
- The edit prompt says: keep the plate's background, framing, lighting and every
  object; place each listed character in its slot; nothing else; no text.
- The cover uses the cover plate + the same characters.
- Redraws through the edit endpoint use the same pass against the book's pinned
  template version.

**Acceptance**

1. Exactly one provider call per page (plus cover) when the first attempt passes.
2. Background preservation is measured: perceptual-hash drift between the final
   image and the plate, outside the union of the slots, stays under the
   threshold T0 calibrated. A test with a fake provider that redraws the
   background fails. (Replaced by a tier-2 scene-match check if T0 took the
   fallback.)
3. Each character appears in its slot, verified by the reviewer (T9) and by a
   human spot-check on the first real run.
4. No final image contains lettering (reviewer fault `text_in_image`, which
   tier 2 detects on every image).
5. A provider without edit support, or with too few reference slots, makes
   `/api/book/start` return a clear 503 at boot/health time, not a half-drawn
   book.
6. `prompts.json` records the template version, plate id, slots and exact edit
   prompt per page.

---

## T9 — Quality loop: analyse every image, 3 attempts max, best of three

**Goal:** no unreviewed image reaches a book; bounded spend.

**Design**

- `ImageReviewer` protocol: `review(image, page_spec, attempt) -> Verdict`
  (`passed`, `score 0–100`, `faults[]`, `tiers_run`).
- **Tier 1, every image, free, fast-fail:** decode, exact/duplicate of plate,
  blank or near-flat frame, wrong aspect or size, all-dark/all-light, a slot
  unchanged from the plate, background drift outside the slots. A tier-1
  failure skips tier 2 and goes straight to the next attempt.
- **Tier 2, every image that passes tier 1:** a vision model returns strict JSON
  against the page's checklist: does each cast character appear, is the scene the
  right moment, is any lettering visible, is a face obscured, are there extra
  people.
- **Attempts:** pass on attempt 1 → done. Otherwise up to **3 total**
  generations, then the highest score wins; tie → earliest attempt. The chosen
  page is marked `best_effort`. The book is charged its normal fee.
- Attempts and verdicts are stored; the chosen one is marked.

**Acceptance**

1. A page never makes more than 3 billable image calls, asserted with a counting
   fake provider. The same holds for each redraw.
2. Every completed page has a verdict; a book cannot complete with a missing one.
3. A forced double failure still produces a book: best-of-three is chosen, the
   page is marked `best_effort`, and the book still downloads.
4. Scores are deterministic given the same verdicts; the tie-break is tested.
5. The reviewer never receives the About free text, the child's name or the
   original photo — asserted by inspecting the request it is handed.
6. **A verdict counts as a review only if both tiers ran.** In production a
   missing reviewer key fails at boot. In local and mock mode the book completes
   on tier 1 alone, every verdict is stamped `tiers_run: [1]`, and the log says
   tier 2 was off. No silent pretend-review.

---

## T10 — Reviewer provider and cost

**Choice:** a cheap vision model run on **every image** (T9, tier 2). Candidate
options; **every price below is an estimate from memory and must be checked
against the vendor's price page before T10 starts**:

| Option | Rough per-book cost (21 images, unverified) | Catch |
| --- | --- | --- |
| **Gemini Flash-Lite (current generation)** | ~1–3 ¢ | Already a vendor here; strong instruction-following |
| Qwen2-VL-7B / Llama 3.2 11B Vision / Gemma 3 12B | ~0.5–1 ¢ | Another vendor and key; weaker JSON/checklist discipline |
| OpenAI small vision model | ~5–6 ¢ | 2–5× the cost, no quality need |
| DeepSeek (text models) | n/a | **Cannot see images** — only usable to judge captions, not pictures |

Recommendation: **Gemini Flash-Lite**, batched up to 4 images per request
(about 6 calls per one-pass book). Even at the high estimate, reviewing every
image costs a few cents against a 99 ¢ fee, which is why tier 2 isn't gated on
tier-1 suspicion.

**Acceptance**

1. Reviewer provider is chosen by environment variable, like `IMAGE_PROVIDER`;
   no visitor-visible surface names it.
2. A reviewer failure on a single call never fails the book: that image's verdict
   records tier 2 as failed, the page is treated as unreviewed-by-tier-2 for
   T9.6 purposes, and it is logged.
3. A per-book `review_cost_estimate`, image-call count and redraw count are
   logged; a 20-book run reports the totals. (Input for the later pricing and
   redraw sprint.)
4. The verified prices and the date they were checked are recorded in
   `docs/decisions.md`.

---

## T11 — Under 60 seconds for a one-pass book

**Goal:** cover + 20 pages + PDF in under 60 s when nothing needs a retry.

**Scope**

- Raise the book's page concurrency to what T0 measured as necessary, well
  above today's default 2 (2 slots means ~11 rounds of calls and cannot meet
  60 s).
- Start the cover and all 20 pages at once inside the gate; review each image
  as it lands; assemble the PDF as soon as the last page is chosen.
- Pre-warm plate reads and provider connections; never re-encode a page twice.
- Keep `MAX_CONCURRENT_GENERATIONS` honest per provider tier. The gate is per
  process and shared by every book being drawn, so two books at once each get
  about half the slots.

**Acceptance**

1. A measured run on the real provider, real plates, one test photo:
   **p50 < 60 s**, p95 reported, with the number in `docs/decisions.md`.
2. The same measurement with **two books started together** is reported
   alongside it, so the 60 s claim says how it behaves under load.
3. Mock-mode test asserts the schedule: all 21 calls are in flight at once when
   the gate allows, none is started twice on a first-pass book.
4. A per-page deadline aborts a slow call and counts it as a failed attempt.
5. PDF assembly adds < 3 s for 22 sheets at 1600 px long edge.
6. The measurement command is committed, so the claim is reproducible.

---

## T12 — Progress bar that names the stage, thumbnails as images land

**Acceptance**

1. Poll document carries an explicit `stage`: `checking_template`, `painting`
   (`7 of 21`), `reviewing`, `retrying` (`attempt 2 of 3`), `assembling`,
   `complete`; the UI prints the stage, not a bare percentage.
2. Each page's thumbnail appears within one poll of its image being stored;
   images are fetched lazily.
3. A retry changes the stage text; the bar never advances on a page that is
   being re-painted.
4. Polling is no more frequent than 1 s and stops on `complete`/`failed`.

---

## T13 — Near-instant page load

**Acceptance**

1. Server time for `/` (shelf) and `/books/{id}` is **p95 < 300 ms** locally with
   20 books in the library; no image generation or plate read blocks the render.
2. The book page draws the full 22-tile grid before any image is requested.
3. Images are served with `Cache-Control` and an `ETag` taken from the stored
   `book_pages.image_sha256`; a revisit with a matching `If-None-Match` gets 304
   without reading the image blob.
4. Opening a book started earlier is a single database read plus the status
   payload; it does not re-derive prompts or re-read plates.

---

## T14 — Persistence and the second login

**Acceptance**

1. Make a book, restart the app process, sign in again with the same account:
   the library shows it and both downloads work.
2. A second account gets 404 for that book id, as today.
3. The PDF is stored as a blob and is not rebuilt on download.
4. A book whose render was interrupted is marked failed, never left polling.
5. SQLite survives a restart for local dev; Postgres survives a redeploy.

---

## T15 — Safety guardrails and prompt hygiene

**Acceptance**

1. Every plate prompt is child-free, text-free, brand-free and violence-free;
   asserted on the assembled prompt, not the template.
2. Red's wolf is described as a talking animal in clothes, never as a threat
   to a child; no page shows a weapon or blood, including the Woodcutter's
   rescue.
3. The uploaded photo is the only source of a real person's likeness; no plate
   or character reference depicts an identifiable real person.
4. Provider refusals are surfaced as a page failure with a plain message, never
   retried blindly into the same refusal.
5. Logs continue to contain no image bytes, no About text, no names.

---

## Sequencing

Done, in the order it landed: T1 and T2 (template and words), T4 (schema and
versions, by Claude), T3 (one storage path), T5 (plate generation), T6 (the
review screen), and T7 with T6.5 (books made from their story's data, and the
start gate). What remains, in dependency order:

1. **T2 prose pass on Jack** — Claude reviews the 20 Jack pages now that they
   are seed data; best before any plates are approved, since a changed beat can
   change a plate. The same pass should read T7's new Red wording (see T15).
2. **T0 spike** — edit latency and background preservation on a real provider.
   Needs an operator with a key and three hand-made plates; Claude makes the
   go/fallback call on the numbers.
3. **T8** — per-book edit pass on the pinned plates. **Claude's contract first**
   (provider `supports_edit`, reference limits, background-preservation
   threshold); Buffy then wires it into `book_runner`.
4. **T9** — quality loop and `Verdict` schema (**Claude**), then **T10** — the
   reviewer adapter and cost logging (Buffy).
5. **T11, T12, T13, T14** — speed, stage names, page-load caching, persistence
   and second-login tests. Buffy.
6. **T15** — guardrail pass and the recorded timing/cost numbers. Buffy, then
   Claude's required review of the prompts and guardrail tests.

The operator's plate review (T5 → T6) is no longer the long pole: the screen
exists, and the gate that turns plates into a requirement is one tick away.
What it is waiting for is T8, so that approving a plate changes what a book
looks like.

## Status of this pass (2026-11-06)

| Item | State |
| --- | --- |
| **T1** — template freeze, cream page, cover title band | **Done.** `TemplateGeometry` is the one geometry every page uses; `title_band_report()` measures the cover's top 22 % and returns the reason an admin screen will show. `tests/test_page_template.py` covers 20 pages, overlong prose, cream fill, and flat/busy/gradient bands. |
| **T2** — twenty pages for both stories | **Done for Red and content-complete for Jack.** `app/books/little_red_riding_hood.toml` holds 20 pages, cover, cast and the wolf's non-photoable flag; `app/books/__init__.py` is the boot-time validator. Jack gained `the-second-climb` as page 10, so all counts moved to 21 drawn pictures / 22 sheets. |
| **T0** — edit latency and plate preservation | **Blocked.** The environment has `IMAGE_PROVIDER=mock` and no OpenAI, Gemini or Agnes key, so there are no real numbers to hand Claude. Needs an operator run with a key and three hand-made plates. |
| **T4** — schema, migration 0005, versioning and pinning | **Done.** Built ahead of T0 because T0's fallback keeps T4–T7 unchanged. Contract in `app/services/template_store.py` (`TemplateStore`), review guard in `app/services/db_book_store.py`, seed command `make seed`. `tests/test_template_store.py` holds each acceptance criterion and `tests/test_migrations.py::TestMigration0005` runs up/down/up; both run on Postgres with `TEST_POSTGRES_URL` set and passed on Postgres 17 locally. See "T4 as built" below. |
| **T3** — database required, SQLite default | **Done.** `build_engine` turns SQLite foreign keys on, the filesystem book store is gone from the runtime path, `book_store.py` is now the contract alone, and `tests/test_migrations.py` runs the whole chain up and down on SQLite (Postgres with `TEST_POSTGRES_URL`). `README`, `Makefile`, `render.yaml`, `.env.example` and `docs/decisions.md` §4 say one storage path. |
| **T5** — empty plates per story | **Done.** `app/services/plates.py` makes the 21 plans (deterministic slot layout by cast size, the exact prompt, no prose, no name, no placeholder) and `python -m app.scripts.make_plates --story <key>` writes them as `draft` plates with that prompt and those slots, one call per plate per candidate; `--contact-sheet` writes the set as one PNG. The prompt text is the story's own `[stories.<key>]` block in `prompts.toml`; `tests/test_plates.py` holds T5.1–T5.5. |
| **T6** — admin review screen | **Done.** `/admin/plates` (stories and progress), `/admin/plates/{story}` (the contact sheet, with a no-JS overlay of every stored slot and the cover's title band) and `/admin/plates/{story}/{page}` (candidates: approve, re-roll with an edited prompt, retire; plate bytes at `/admin/plates/{story}/{plate}.png`). Gated on `OPERATOR_SUB`; every route answers a plain 404 to anybody else, and a password session can never be the operator. Approve is idempotent and records operator and time; only approval creates a version. Delete is not wired on purpose — retire is the scope, and `delete_plate` refuses a plate any version names. Reads come from `TemplateStore.pages()` and `.plates()`. `tests/test_admin_plates.py` covers T6.1–T6.4 and T6.6–T6.7. |
| **T6.5** — the start gate | **Done, as an operator switch.** `stories.requires_template` (migration 0006), set from `/admin/plates` (`POST /admin/plates/{story}/gate`, operator only, 404 to anybody else) and never touched by seeding. On, with no complete version, `TemplateStore.edition_for_new_book` raises `NoTemplateVersion`; the shelf card is "Not ready yet" with the sentence, the studio and About screens answer 409, and `/api/cartoonify` and `/api/book/start` answer `409 story_not_ready` before anything is billed. Off (the default), books start on the latest revisions. `tests/test_story_editions.py::TestTheStartGate`. |
| **T7** — the bible is the prompts' source | **Done.** See "T7 as built" below. `tests/test_story_editions.py` makes a Red book end to end, proves a re-seed and a revised look never reach a book already made, refuses the wolf and cross-story portraits, and pins a legacy book at boot; `tests/test_migrations.py::TestMigration0006` runs up/down/up on SQLite and Postgres 17. |
| **T8–T15** | **Blocked on contracts.** T8 (edit pass) and T9 (quality loop) are Claude's; T10–T15 build on them. |

One transitional note for whoever picks this up:

1. **The reference cap is a constant for now.** `MAX_REFERENCE_CAST = 2`
   (star included) stands in for T8's per-provider `max_reference_images`;
   when T8 lands, the seed check should read the configured provider's limit.

### T7 as built — where it differs from the scope above

1. **A book pins its edition, not only a version.** `book_story_pages` and
   `book_story_characters` (migration 0006) record the exact page and bible
   revisions every book is made from, written in the same transaction as the
   book row by `save_record(edition=…)`. That holds with the gate off too, where
   there is no version to pin; a book on a version pins that version's
   revisions and its id. `TemplateStore.pin_book` is gone. Books saved before
   0006 are pinned once at boot (`pin_unpinned_books`) to the latest revisions.
2. **The edition is the one runtime view of a story.** `StoryEdition`
   (`app/models/story.py`) carries a story's title template, default names,
   cover, twenty pages and bible. The prompt builder, the runner, the PDF, the
   start and edit routes, the media archive and every screen take an edition;
   `BOOK_PAGES` and every `[book.cast*]` table are gone.
3. **Two framings per scene.** A seed scene describes the empty plate; where the
   finished picture needs different framing (the child's size in the frame,
   light on a face), the page carries `[pages.compose]` with `environment`,
   `time_of_day` or `camera`. Jack's twenty pages carry the catalogue's tested
   framing there; Red needs none.
4. **The bible carries every look.** `[cast.<key>]` holds `label`, `photoable`,
   `look`, and for a photoable supporting character `look_with_photo` and
   `portrait` (the portrait-pass brief); only the star may have `costume`, which
   replaces the visitor's outfit (Red's hood; the studio hides the outfit
   picker for such a story). The validator enforces each rule at boot.
5. **Story wording that is not a look stays in `prompts.toml`.**
   `[stories.<key>]` gains `page_world` (the props-and-places sheet the finished
   page reads, beside the plate's `world`) and `page_rules` (the tale's own
   rules: Milky-White's coat, the giant's face, Jack's physics examples; Red's
   cloak and the wolf's disguise). Jack-only clauses were moved out of the
   shared `[book]` lines into those rules.
6. **Names and titles are per story.** `[story.names]` gives the default star
   name per photo-gender answer, and `book_title` has a `{name}` slot; a book
   stores its resolved title (`books.title`, backfilled for old books). This
   also fixed a bug where every page prompt said "The character's name is
   Jack." whatever name the visitor chose.
7. **A portrait belongs to one story.** Drafts record `story_key`; a start or an
   edit refuses a portrait made for another story, because cast keys repeat
   across tales (both have a mother) but their looks do not.
8. **The app seeds at boot.** `build_container` runs `seed` (a no-op when the
   seeds are unchanged) and `pin_unpinned_books`; `make seed` now uses the same
   database the app would, SQLite included.
9. **The cover is drawn from the story's cover scene**, with the same scene
   block as a page and a shared title-band line, instead of one hard-coded
   beanstalk sentence.

### T4 as built — where it differs from the table list above

Each difference keeps T4's acceptance criteria true where the list as written
could not:

1. **Words and looks are revisions, not rows edited in place.** `story_pages`
   and `story_characters` are append-only (`revision` per page / cast key), and
   a version pins row ids through `template_version_plates.story_page_id` and a
   second child table, `template_version_characters`. A content hash alone
   (as listed) can detect a change but cannot give an old book back its old
   words (T4.1, T7.5).
2. **No `stories.current_template_version_id`.** The current version is the
   highest `template_versions.number` for the story. A stored pointer would be
   a circular foreign key for no gain, and T6 turned out not to need one: the
   review screen reads the highest number, and nothing rolls back.
3. **`books.story_key` defaults to `jack_and_the_beanstalk`** (the key the code
   uses, not `jack_and_beanstalk`) and is not a foreign key: books made before
   stories were data have no `stories` row. `template_version_id` is the
   enforced link.
4. **`books.cover_review`** is added beside the page `review`, because the
   cover is an image too and T4.5 says no book completes with an unreviewed one.
5. **The cover is page 0** in `story_pages`, `template_plates` and versions.
6. **The star is on every page.** A plate needs exactly one slot per cast
   member *plus the star*; seeds list only the supporting cast per page.
   Approval enforces it; a seed change that alters a page's cast stops new
   versions until that plate is re-approved.
7. **Approving a plate retires the page's previous approved plate.** Versions
   that already use the old one keep it.
8. **T4.3 is enforced structurally and at boot.** `story_pages` has no look
   column, and the seed validator refuses a scene that copies an 8-word-plus
   sentence of a cast look. Making the page *prompts* read the bible is T7.
9. **`image_sha256` is not backfilled** for old pages; it is written with every
   new image.

**Handoffs:**

- **T8:** a book's pinned plates are `templates.book_edition(book_id)
  .template_version_id` → `load_version(...)`; the edition already carries the
  words and looks the edit prompt needs. T9 writes verdicts with
  `record_page_review` / `record_cover_review`.

### T6 as built — where it differs from the scope above

1. **The gate is an identity, not a role.** `OPERATOR_SUB` names one Google
   account; every route (`GET` pages, image bytes and all three `POST`s) answers
   a plain 404 to anybody else, before it reads anything. A password-mode
   session has an empty `sub`, so that deployment cannot reach `/admin` at all —
   deliberate, because a shared password has no identity to compare.
2. **"Change that plate" means say what, then paint.** A plate's prompt is
   immutable once the image exists (the hash beside the image is the record of
   what was asked). The common case is one correction — "warmer light", "no
   well" — so both the contact sheet and the page view take a short change note
   and append it to the page's planned prompt; the whole prompt is still
   editable one fold down, and a blank form repaints the planned prompt. The new
   plate keeps the page's planned slots, not the previous plate's, so the layout
   cannot drift between candidates.
3. **The overlay is HTML and CSS, not a composited image.** Each slot is a
   `<span>` positioned by the stored rectangle as percentages, with the numbers
   also printed in `data-rect`; the title band is the same shape at 22 %. The
   toggle is a checkbox and a sibling selector, so the screen needs no
   JavaScript and a re-render of the page is not required to inspect a plate.
4. **Re-roll is synchronous inside the request.** One image call, one new draft,
   then a redirect. There is no job, because the operator is watching and the
   server's wall-clock budget already covers one generation.
5. **T6.5 landed with T7** as a per-story switch on `/admin/plates`; see its row
   in the status table.
6. **Delete is not wired.** The scope says retire, and retire is what the store
   needs: `delete_plate` exists and is tested, but it refuses any plate a
   version names, so it belongs beside version cleanup, not on this screen.
7. **"Make plates" is one image call per request, not one long request.** A
   whole story is 21 calls (minutes), which no HTTP request should carry, so
   `POST /admin/plates/{story}/paint` paints exactly the first page with no
   candidate and returns; the sheet's script repeats it through
   `paint.json` until nothing is missing, showing each page as it lands. With
   JavaScript off the form still works one press at a time, and because the
   next missing page is computed from the database, a reload never loses the
   operator's place. Approved plates are skipped, retired pages are painted
   again, and the same code path as T5 supplies the prompt and slots.

## Open questions (small, can be answered during the work)

1. **Plate aspect ratio** — settled as one ratio for every plate and every
   story: 4:3, used by `make_plates` and by an admin re-roll, so a re-roll is
   framed like the plate it competes with and the PDF geometry stays identical.
2. **Admin identity** — settled as recommended: one Google `sub` in
   `OPERATOR_SUB`. Empty disables `/admin`; every other identity gets a plain
   404.
3. **Best-of-three visibility** — should the reader see a "best effort" badge, or
   should that stay operator-only? Recommend operator-only. Still open: T9.

Settled: the Woodcutter stays; a `best_effort` book is charged the full fee;
the cover title band is checked automatically. Pricing and redraw spend limits
are a later sprint.
