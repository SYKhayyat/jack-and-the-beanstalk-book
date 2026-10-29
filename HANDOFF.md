# HANDOFF — from plan to code

Current HEAD when this file was written: `ca22a14` on `main`, working tree clean.
This file is the whole brief: §0 is the operating prompt (paste it to the
model), §1–§3 are context and judge-made decisions, §4 is the step plan with
acceptance criteria, §5–§6 are traps and procedure.

---

## 0. The prompt — copy-paste this to the model, pointing it at this repo

> You are the coding agent for the repository you were pointed at. Its
> `HANDOFF.md` is the authoritative brief; a human judge (the repo owner)
> reviews between steps.
>
> **Before any commit:** read `HANDOFF.md` end to end, then `README.md` and
> `to-do.md`. Then reply with a short confirmation: the nine steps of §4 in
> your own words (one line each), and any step you believe is blocked on a
> human.
>
> **Working rules — every step:**
>
> 1. Execute §4's steps strictly in order. One step = one commit (or a tight
>    commit series) straight to `main`. Commit style: `feat:` / `fix:` /
>    `test:` / `audit:` prefixes, as in `git log`.
> 2. Each step's **Acceptance criteria** is its definition of done. Verify
>    every criterion yourself before committing, and include the verification
>    results in your step report.
> 3. `make check` (ruff, mypy, pytest — see §1) must be green before every
>    commit. Never skip, weaken, or delete a test without stating the
>    constraint that changed and getting the judge's sign-off.
> 4. The judge reviews between steps: commit the step, update `to-do.md`,
>    summarize what changed and what to verify, and **stop**. Doing a second
>    step without a go-ahead is a violation.
> 5. Spend discipline: iterate only on the mock provider or the free Agnes
>    provider. Never spend OpenAI money without the judge's explicit go for
>    that specific run.
> 6. Secrets never enter the repo, the logs, or any committed file; `.env.example`
>    carries placeholders only. Existing leak/redaction tests are law.
> 7. §3 holds decisions the judge already made. Do not re-litigate, redesign,
>    or "improve" them. If you hit a genuine fork not covered there, present
>    at most 3 options with one recommendation and your reasons, then stop for
>    the judge — decide nothing unilaterally.
> 8. Test-pinned visitor-facing constraints (uphold everywhere): nothing a
>    visitor can fetch names the image provider or model beyond whether the
>    pictures are real; no retry loops around billable calls; visitor free
>    text never reaches a prompt; no secrets in any response or log line.
> 9. User-executed items — booking the crowd study, the Touro fee waiver
>    ticket, creating the Google Cloud OAuth client, any payment — are the
>    **judge's** to perform. Your part is drafts and step-by-step walkthroughs.
> 10. The AI-feedback (10 personas) method must **not** be published anywhere
>     before after Monday's class comparison; keep its design out of public
>     links until then.
> 11. The final deliverable is the full 20-page book, live on Render, with
>     Google login, per-user persistence that survives restarts, per-image
>     progress, structured logging, and the feedback package (10 real + 10 AI
>     responses + comparison). Stripe payments are explicitly out of scope.
>
> **Start now with the confirmation described above. Do not code until the
> judge gives the go for the step in question (Step 1 is explicitly gated:
> plan approved, code not yet authorized).**

---

## 1. The product

FastAPI app (MVC) that turns one uploaded photo into a picture book PDF:

```
photo -> cartoon portrait (1 API call, user approves) -> story pages -> PDF
```

Four page blocks exist today (empty cupboard, magic-beans trade, beans out
the window, beanstalk at dawn — the fourth is retired at HEAD) plus an About
page. Every word sent to any image model lives
in `app/prompts.toml` (never in Python). Upload → portrait uses
`POST /api/cartoonify`; approved portrait → book uses `POST /api/book/start`;
poll `GET /api/book/{id}`; pages land under `/book-media/{id}/pages/{n}`; PDF at
`/book-media/{id}/pdf`. Step 7 expands the book to the full 20 pages.

Read `README.md` before touching anything — it is accurate as of this HEAD and
contains the provider pricing table, the render.yaml/deploy story, and the
operational design decisions (no retry loops on billable calls, slot-transfer
on timeout, etc.).

### Key paths

| Path | What it is |
| --- | --- |
| `app/config.py` | All env-driven settings; `ProviderName` enum |
| `app/container.py` | Composition root (provider, stores, gate, rate limiter) |
| `app/services/image_providers/` | `base.py` (contract), `registry.py`, `openai_provider.py`, `gemini.py`, `mock.py` |
| `app/services/book_runner.py` | Background book job; renders pages **sequentially** under the concurrency gate (`MAX_CONCURRENT_GENERATIONS`, default 2) |
| `app/services/book_store.py` | Book artefact persistence (status, pages, PDF, prompts.json) |
| `app/services/storage.py` | Draft/portrait persistence (filesystem) |
| `app/routes/book.py` | Book API + media routes |
| `app/routes/api.py` | Cartoonify + settings catalogue |
| `app/auth.py` + `app/routes/auth.py` | Shared-password gate (HttpOnly signed cookie) — **to be replaced in production by Google OAuth, see step 3** |
| `app/static/book.js`, `book-view.js` | Poll loop + UI; today's progress is ONE text line ("Making page 2 of 4…") |
| `app/prompts.toml` | Every word sent to models; `[book.cast]` pins the mother/old man looks |
| `render.yaml` | Render blueprint; `STORAGE_DIR=/tmp/...` (ephemeral disk) |
| `to-do.md` | Submission tracker (links, UAT pass items) |

### Build / verify commands

```bash
make check      # ruff + mypy + pytest — must stay green after every step
make test / make lint / make typecheck
```

CI (`.github/workflows/ci.yml`) runs the same on every push. The test suite is
fully offline (mock provider, no keys). Python is pinned via `.python-version`
(3.12.8); dependencies are locked (`requirements.txt` → `requirements.lock`,
dev in `requirements-dev.lock`). Adding a dependency means updating both the
loose file and the lockfile, per the Makefile workflow.

---

## 2. The assignment (what this work is for)

Five workstreams, hand-in on Canvas + Slack, all links public and verified in a
private window:

1. **Real user feedback — 10 responses** via a crowd service
   (CloudResearch Connect recommended), survey built in Engage, ~3 min, whole
   PDF shown. ~$10. Touro email support ticket to request the first-study fee
   waiver. **User-executed**; our part is drafting materials.
2. **AI feedback — 10 genuinely different personas**, method unpublished until
   Monday's class comparison. Harness to be BUILT LATER (judge said:
   "you will later build it — for now, plan").
3. **Federated login** — Google (Gmail), the developer's OWN OAuth credentials.
4. **Database + logging** — per-user content persists between visits; live on
   Render; the grader signs in with their own Gmail and expects their book to
   still be there on a later visit.
5. **Progress per image + parallel generation** — a real per-image progress
   bar, pages generated concurrently, testable at low cost.

Explicitly NOT yet: Stripe payments. Do not build it.

**Non-negotiable repo constraints** (already enforced, keep them):

- Secrets only in `.env` / the Render dashboard; `.env.example` committed with
  placeholders only. Tests exist that fail if a key or backend detail leaks
  into any response or log.
- No retry loops around billable image calls, ever.
- Nothing a visitor can fetch names the image provider or model beyond "the
  pictures are real / placeholder" (pinned by tests).
- Visitor free text (About fields) never reaches a prompt — enforced by tests.

---

## 3. Judge's decisions (already made — do not re-litigate)

| Question | Decision |
| --- | --- |
| Step 1 (Agnes provider) | **Plan approved; do not code until told.** First coding assignment when it comes: cherry-pick `6501e85`, resolve the README conflict by hand |
| Postgres host | **Neon (free, AWS)** via a single `DATABASE_URL` |
| Login model | **Google replaces the shared password in production.** The password gate survives only as local-dev/offline fallback (delete later if judge says so) |
| OAuth credentials | Judge creates the Google Cloud client themselves; we provide a step-by-step console walkthrough |
| Artefact storage | **Postgres `bytea`** (books are a few MB); keep a repository interface so object storage stays possible later |
| Character bible | **Resolved into multi-character-by-photo (see §4 Step 6).** No cap on character count — cast is book-defined (`cast` lists in `prompts.toml`); user defines any character + photo + name and can add more, except Milky-White: cast, but never user-photoable. Page calls send only that page's own cast references. |
| Book length | **The hand-in book is the full 20 pages** (§4 Step 7). The current 3-story-page + About book must be completed — page 4 unretired, remaining pages authored in `prompts.toml` |
| AI-persona harness | Plan now, build later (after the Monday class comparison?) |
| Review workflow | **Direct commits to `main`.** Judge reviews after each step. No PR ceremony |

---

## 4. The step plan (in order; commit after each step, update to-do.md)

| Step | What | Depends on | Who |
| --- | --- | --- | --- |
| 1 | Re-land the Agnes AI provider (free, testing-only) | — | agent |
| 2 | Feedback instruments (survey + waiver ticket + comparison skeleton; AI-persona **plan** only) | — | agent (drafts) + judge (books/uploads) |
| 3 | Google OAuth login (judge creates the client; agent wires + walkthrough) | — | agent + judge |
| 4 | Database + persistence (Neon Postgres, per-user books) | 3 (users exist) | agent |
| 5 | Per-image progress bar + parallel page generation | 1 (free testing) preferred | agent |
| 6 | Character bible → multi-character by photo (no cap; per-page cast refs; Milky-White prose-pinned) | 4 | agent |
| 7 | The full 20-page book (unretire p4; author remaining pages; fix per-minute billing) | 5, 6 preferred | agent |
| 8 | Structured logging (per-page/per-book lines; new secrets redacted) | 4 | agent |
| 9 | Hand-in package (deploy, verify private-window, to-do.md links) | 1–8 | agent + judge |
| — | Feedback-driven iteration (apply both response sets, re-verify) | 2 + responses | agent |

Judge-executed, not step-gated: keep waiting on real responses while code
proceeds (§2) — build login/database/etc. while the crowd study runs.

### Step 1 — re-land the Agnes AI provider (free, testing-only)

The provider was added in `6501e85` and reverted in `ca22a14` (reason: free
tier that accepts uploaded photos, but likeness on real photos was judged
unusable — fine, we use it as the FREE testing backend the assignment's
"test with a free provider so you don't burn tokens" requires; production
stays OpenAI `gpt-image-2.5-flare` at `quality=low`).

Verified on 2026-10-28: `git cherry-pick --no-commit 6501e85` onto `ca22a14`
applies cleanly to **everything except README.md** (docs conflict only).
Procedure:

1. On `main`, run `git cherry-pick -x 6501e85`.
2. Resolve the README.md conflict by hand: reinsert the Agnes sections from
   the original commit's README hunks, adapted to today's README wording (the
   revert rewrote the "free options" prose; the Agnes parts from 6501e85's
   version are the reference).
3. `make check` must pass (the original commit carried ~50 offline Agnes
   tests in `tests/test_agnes_provider.py` plus additions to
   `tests/test_providers.py`).
4. Env additions: `IMAGE_PROVIDER=agnes`, `AGNES_API_KEY`, `AGNES_SIZE`
   (default `1K` — the only tier the free allowance sustains, ~10 req/min,
   which fits a 5-call book), model `agnes-image-2.5-flash`, base URL
   `https://apihub.agnes-ai.com/v1`. `.env.example`, `render.yaml` (sync:false
   keys) and README config table updated in the same commit.
5. Real-gotcha reminders baked into 6501e85 — keep them: references and
   `response_format` go inside `extra_body` (top-level `response_format` is
   the service's documented common mistake); `size` is a tier (`1K`) paired
   with `ratio`, not `WIDTHxHEIGHT`; key stripped of trailing newlines;
   redaction on every error string.

**Acceptance criteria** (from the approved plan):
- `IMAGE_PROVIDER=agnes` boots; `agnes ready` appears in the startup log; the
  leak-pinning tests still pass (`/healthz` and all payloads still never name
  the backend).
- All Agnes tests green; full `make check` green.
- One real end-to-end book at 1K on a free Agnes key — 5 calls, $0 spend.

### Step 2 — feedback instruments (drafts only; user executes)

Wall-clock-sensitive, so the user should launch ASAP. Our deliverables:

1. **Engage survey draft** — ~10-word description → Engage builds it;
   ~3 minutes; show the whole PDF; ask the four assignment questions (looks
   like me / story rings true / looks professional / what isn't good) plus
   one hard question that catches smallest-detail failures.
2. **CloudResearch Connect setup instructions + the support-ticket text**
   (Touro email, Robinson's class, ask for the first-study fee waiver).
3. **Comparison doc skeleton** (real 10 vs AI 10; what each caught that the
   other missed) to be filled in later.
4. The **AI-persona harness: PLAN ONLY in this step.** When the build is
   authorized, the harness runs locally against the developer's own API key
   and never publishes its method until after Monday's class comparison.

**Acceptance criteria:**
- Three committed draft artefacts exist (suggested home: `docs/feedback/`):
  the Engage survey text ready to paste, the CloudResearch Connect setup
  steps + the Touro fee-waiver support-ticket text, and the comparison-doc
  skeleton (real 10 vs AI 10, plus "what one caught that the other missed"
  sections both ways).
- The survey as drafted takes ~3 minutes, shows the whole PDF, and covers the
  four assignment questions (looks like me / story rings true / looks
  professional / what isn't good) plus one hard smallest-detail question.
- The AI-persona harness exists only as a design section — no harness code in
  this step.

### Step 3 — Google OAuth login (federated)

- Library choice: `authlib` (its Flask-style OIDC client works with Starlette)
  or hand-rolled authorization-code flow with `google-auth`. Prefer the
  smallest, most testable surface; the repo style is explicit code over magic.
- Registration walkthrough to hand the judge: create OAuth client (Web),
  authorized redirect URIs. URIs needed: the live Render URL
  (`https://jack-and-the-beanstalk-book.onrender.com/auth/callback`), any
  custom domain in use (a CNAME `book.siachshai.online` is referenced in a
  comment in `render.yaml` — confirm with judge whether it's actually live),
  and `http://localhost:8000/auth/callback` for local dev.
- **Critical gotcha:** the consent screen must be published out of "Testing"
  mode. In Testing mode Google hard-expires refresh tokens after ~7 days and
  restricts external test users — the grader's login must not break mid-week.
  Call this out in the walkthrough.
- Replace the gate in production: unauthenticated API/media → 401, pages →
  redirect to `/login` (Google button). The existing `AuthGate` password flow
  becomes a clearly-labelled local-dev fallback: active only when Google env
  vars are absent (e.g. offline tests, mock mode).
- Session: signed HttpOnly cookie as today, but keyed by a proper
  `SESSION_SECRET` env var, NOT the password (the current cookie signer
  derives its key from `APP_PASSWORD` — that coupling must go once accounts
  exist). Session max age configurable as today.
- Per-IP lockout logic for wrong passwords can stay (dev fallback); Google
  handles its own abuse control.

**Acceptance criteria:**
- Judge's Gmail signs in on the live site; session survives revisit + logout
  works; return-to-URL after redirect back still lands where they were going.
- 401 for unauthenticated API/media; redirects for pages; `/healthz` and
  `/static` stay open (existing behavior).
- Client id/secret only in `.env`/dashboard; `.env.example` gets
  placeholders; existing redaction/leak tests extended to the new secrets and
  to `SESSION_SECRET`.

### Step 4 — database + persistence (Neon Postgres)

- Stack: SQLAlchemy 2.x (sync Core/ORM) + Alembic migrations. Connection URL
  from `DATABASE_URL`; Neon **pooled** connection string (endpoint with the
  `-pooler` suffix) recommended for the always-on web process.
- Tables:
  - `users` — Google `sub` (PK), email, name, avatar url, created_at.
  - `books` — id, user_id FK, status, settings JSON, about JSON, cast-sheet
    JSON (placeholder for §5), timestamps.
  - `book_pages` — book_id FK, page number, image `bytea`, media type.
  - Portrait and PDF bytes live in `books` as `bytea` columns (or a dedicated
    `book_artifacts` table if size/migration ergonomics say so — judge's
    decision was DB blobs; don't reach for a second cloud vendor).
- Architecture rule prevailing in this repo: **the filesystem store stays** as
  a second implementation behind the same repository interface; tests and
  local dev keep working offline with zero cloud. `app/services/book_store.py`
  and `app/services/storage.py` are the doors to refactor behind interfaces.
- Config additions: `DATABASE_URL` (empty → filesystem mode), plus SQLAlchemy
  pool settings if needed.
- Log the mode the app booted with (filesystem vs database) without ever
  logging the URL's credentials.

**Acceptance criteria:**
- Judge signs in with their own Gmail on the live site, makes a book, waits
  across a Render restart/redeploy, signs back in — the same book (pages +
  PDF) is still there.
- Books/pages/artefacts keyed to the user; another signed-in account cannot
  fetch a user's book by id (404/401, not just obscurity).
- Local dev + CI fully offline and green with the filesystem implementation.
- `to-do.md` documents the first-deploy runbook (create Neon DB, set
  `DATABASE_URL`, run migrations once).

### Step 5 — per-image progress + parallel page generation

- Current state: pages render sequentially in
  `BookRunner._render` (one page, then the next, each holding a gate lease
  with the slot-transfer pattern in `_on_worker_thread_with_slot` /
  `_hand_slot_to_worker`). The UI's only progress is one text line.
- Changes:
  1. Status payload gains an explicit per-page `making` (in-flight) state
     between `pending` and `done`, so the frontend can show which page is
     currently being generated.
  2. Book pages render concurrently via `asyncio.gather`, each page still
     acquiring its own gate lease — parallelism is bounded by
     `MAX_CONCURRENT_GENERATIONS` (2 on Render) and per-client billing tokens
     by `MAX_GENERATIONS_PER_MINUTE` (12 on Render). The slot-transfer rule
     must survive: a page that outlives its deadline hands the slot to the
     worker and the worker releases it only when truly done.
  3. PDF assembly waits for all pages that finished; a late failure keeps the
     completed pages visible (existing behavior, do not regress).
  4. Frontend: replace the single text line with a per-page tile grid
     (pending/making/done) plus a genuine progress bar ("2 of 4 pages");
     pages land as thumbnails as each completes. Keep polling the existing
     `GET /api/book/{id}` shape (extend it, don't break it).
- Test on the **free** Agnes provider (that is exactly what step 1 re-lands it
  for) or mock; never burn OpenAI spend on iteration.

**Acceptance criteria:**
- Each of the 4 pages visibly transitions pending → making → done, with page
  thumbnails landing.
- Measured wall-clock for one full book drops vs. sequential on the same
  provider; gate/rate-limiter contracts never violated; timeout slot-transfer
  behaviour unchanged.
- Mid-render failure keeps finished pages visible; book status remains
  pollable.

### Step 6 — character bible, resolved: multi-character by photo

> The judge pivoted here from a settings-derived cast sheet to: **"Maybe we
> should actually send a picture of each person? This way we could let the
> user pick to define as many characters as he wants with images."** Both
> follow-up questions are now answered (2026-10-28):

**Decision 1 — no artificial cap on character count.** The judge: *"max
character — go through the book and it is however many characters there are.
In this book, I think we have Jack, the mother, the old man, the giant.
Anyone else? And the user starts by defining any character and can add any
other ones."*

- The cast is therefore **book-defined, not UI-capped**: whoever a page's
  `cast` (or story prose) names is a character a user may attach a photo to.
- Current cast of this book as declared in `app/prompts.toml`:
  - `mother` — pages 1 and 3 (`cast = ["mother"]`)
  - `old_man` — page 2 (`cast = ["old_man"]`)
  - `giant` — appears from page 4 "beanstalk-at-dawn" onward, **which is
    currently retired** (`[book.assembly]` comment: an earlier brief retired
    it; "the book is the About page + the three story beats below for now").
    That retirement is moot now — the judge has ruled the hand-in book is the
    full 20 pages (§4 Step 7), so unretiring page 4 and authoring the rest of
    the story is part of this work.
  - **Anyone else? Yes — Milky-White the cow.** She recurs on pages 1–2 in
    prose (`must_include`/`environment`/story) but is in no `cast` list yet,
    which is exactly why UAT 5 (coat match between pages 1–2) had to be
    fixed by hand in prose. **Judge's ruling: she is cast, but she can never
    be user-photographed** — cast entries need a `photoable` flag in
    `prompts.toml`; Milky-White's is `false` and she stays prose-pinned
    forever (a user cannot upload a photo for her under any circumstances).
- Cost/rate arithmetic the design must respect: N characters + P page calls
  = N + P billable calls (Render: 12 images/min brake; Agnes free tier ~10
  req/min at 1K). No cap needed --- the brake already bounds it.

**Decision 2 — per-page references only, of the page's own cast.** The
judge: *"of course only send few — there are only 20 pages — you can figure
out"*.

- A page call sends **only the characters named on that page's own `cast`
  list**, resolved from `prompts.toml` — never the whole book cast. So the
  per-call reference set stays small whatever the book has in total.
- **Decision 3 — the hand-in book is the full 20 pages.** The judge:
  *"this assignment finishes off the whole book, all 20 pages"* (matching
  the assignment sheet's "download the 20-page book"). See the new Step 7.
- Design details to settle at implementation time: whether the recurring
  child still rides every page as the original double anchor (approved
  portrait + original photo) alongside the page's other cast entries, and
  the per-call reference ceiling each provider can actually take (the README
  currently promises "at most 2 reference images", which multi-character
  pages like 1 = mother + Jack will break — verify provider limits when
  choosing the exact composition).

Remaining design checklist before coding this step (smaller now, not judge-
fenced):

- **UI flow:** character list starts empty (or pre-seeded with the book's
  named cast as slots); the user uploads a photo + name per character and
  defines any others themselves. Reuse `prompt_name`/`about_name` name
  resolution patterns; free text still never reaches a prompt.
- **Pipeline:** each character gets a cheap portrait pass (mirroring the
  existing cartoonify → approve flow) before page calls; pages then call
  with that page's cast portraits as references.
- **Persistence:** new `book_characters` table (photo, approved portrait,
  settings, name, role key); the deterministic cast prose from the existing
  `[book.cast]` patterns is frozen into the book record and injected
  verbatim into every page prompt — the text half of the character bible,
  so page-to-page identity doesn't drift (the generalization of UAT's
  cow/mother fixes).
- **Layout:** every layout at 1K must still obey the book's quiet zone and
  text-areas rules regardless of who is on the page.

**Acceptance criteria:**
- A book can be made with a photo attached to any user-photoable cast member
  (the mother, the old man, the giant…), and that character's likeness shows
  up on exactly the pages where their `cast` entry names them.
- **Milky-White has no photo upload path anywhere** — not in the UI, not in
  the API, not in the character-definition catalogue; a test pins that her
  cast entry is prose-pinned (`photoable = false`) and that user-supplied
  character photos are accepted only for photoable entries.
- Every page call sends only that page's own cast references; the exact
  per-page composition (e.g. whether the child double-anchors) is settled
  once and test-pinned.
- The cast-sheet text is byte-identical across all pages of the same book
  (prompt-equality test exists).
- The non-photo path is unchanged: a book made with no character photos
  defined renders exactly as before this step.

### Step 7 — the full 20-page book (new; authoring in prompts.toml)

The judge: *"this assignment finishes off the whole book, all 20 pages"* —
matching the assignment sheet's "buy a PDF for 99¢, download the 20-page
book". The current book is three story pages + About, with page 4 retired.

- **Unretire** page 4 (`beanstalk-at-dawn` has its artwork spec and prose
  already) and author the remaining story pages in `app/prompts.toml` so the
  assembled PDF is 20 pages (default: 19 story pages + the About page — the
  About is part of the book; if the judge's "20 pages" means 20 story pages,
  that is one extra block, and it is the implementation's call to state the
  arithmetic once, clearly, on the settings/status payload).
- **Story arc constraint:** the fixed cast is Jack, the mother, the old man,
  the giant, and Milky-White — no new human characters. The classic tale
  supplies the beats: sell the cow → the trade → beans out the window →
  beanstalk at dawn → climb → castle → money under the floor → escape →
  again: the hen that lays golden eggs → again: the singing harp →
  "Fee-fi-fo-fum" → the chase down the beanstalk → the axe → the giant's
  fall → riches and a proud mother. Author to the full tale.
- **Every new page obeys the existing page-block contract** (one editable
  block per page): `key`, `title`, `text_zone` (lower_third / sky / grass),
  `cast` list, `settling`, `story`, `caption`, `subject_action`,
  `environment`, `time_of_day`, `camera`, `must_include`, `must_avoid`.
  Preserve the hard rules the tests currently pin: no text in the image, the
  five-beans rule (page 2's five beans in five colours stay canon), the
  bobblehead tie-breaker, character-coherence guards, "each page draws only
  its own scene" negative tails. `tests/test_book_prompts.py` must pass
  unchanged in its assertions; new pages need their own such assertions.
  A page without a matching prompt block fails at startup by design
  (`KeyError`) — that stays.
- **Billable arithmetic changes and two real gotchas:**
  1. `POST /api/book/start` bills `len(BOOK_PAGES)` tokens at once
     (`routes/book.py _check_rate`). At Render's `MAX_GENERATIONS_PER_MINUTE=12`,
     **every single book-start would 429 forever** once the book is 20 pages.
     The per-minute accounting needs rework (bill per page as pages actually
     render, or raise the allowance, or bill only what will be rendered this
     minute) — decide and implement here, with a test pinning the new rule.
  2. One 20-page book is ~20 billable calls (~$0.12 on OpenAI low; ~2 minutes
     of Agnes free-tier allowance). README's "one book costs five API calls"
     and the "200 books = 1,000 calls" table rows must be rewritten to match.
- The book runner's sequential-then-parallel machinery (Step 5) already
  handles any page count; PDF assembly (`build_book_pdf`) iterates whatever
  pages exist.

**Acceptance criteria:**
- `/api/book/start` renders the full book end-to-end; the PDF assembles 19
  story pages + About and downloads correctly; page grid and progress UI
  reflect all 20.
- `make check` green; prompt-structure tests extended for the new pages.
- A user can request a book at the deployed rate limits without a
  guaranteed 429 (the billing rule change above is test-pinned).
- Prompt quality passes one real-API eyeball across several uploads
  (likeness + page faithfulness), then holds under the feedback round.



### Step 8 — logging

- Structured log lines exist ad hoc; add request-id middleware, per-page
  timing/outcome lines (book id, user id, page number, bytes, duration), login
  events (Google), book start/complete/fail events.
- Extend `app/redaction.install_secret_redaction` for the new secrets:
  Google client secret, `SESSION_SECRET`, and DATABASE_URL's password portion
  (URLs get pasted whole; a raw URL in a log is a leak).
- Render captures stdout — no extra infra; the grader must be able to find
  their own book's trail in the dashboard.

**Acceptance criteria:**
- One structured line per page render + one per book completion, containing
  user id, book id, page, bytes, duration, outcome.
- No secret, no full image bytes, no procurement detail in any log line;
  leak tests extended and green.

### Step 9 — hand-in package

- Update `to-do.md` directly with all links; hand-in is checked in a private
  window before posting to Canvas + Slack.
- Deploy on Render (blueprint already exists; add the new env keys).
- Deliver: live site, public GitHub repo, 10 real + 10 AI responses and the
  short comparison on a public link, updated to-do.md.
- Do not publish the AI-persona method before Monday's class comparison.

**Acceptance criteria:**
- All deliverable links resolve in a private window: the live Render site,
  the public GitHub repo, the public feedback/comparison link, and
  `to-do.md` linked directly.
- The grader flow verified end to end: sign in with a fresh Gmail → make a
  book → sign out → wait across a redeploy/restart → sign in → **the same
  book is still there** → the 20-page PDF downloads.
- No secret anywhere public; the repo passes the leak tests; `.env.example`
  has placeholders for every env var the app can read.
- `to-do.md` states, honestly, the status of every assignment item
  (feedback counts included) and all links.

---

## 5. Things that will bite you if ignored (hard-won context)

1. **`MODEL`/provider secrecy is tested.** `tests/` pins that `/healthz` and
   every payload name neither provider nor model. Any new endpoint or log line
   must uphold this.
2. **Slot-transfer on timeout** (`book_runner._hand_slot_to_worker`,
   `api._run_gated`): the worker keeps the concurrency slot until it truly
   finishes, otherwise two generations run against a limit of one and the
   book stalls the server. Preserve in any parallelization work.
3. **One budget, not two:** `GENERATION_TIMEOUT_SECONDS` covers queue wait +
   model call together. A test asserts the timeout fires; do not add a second
   allowance on top.
4. **No retry loops, period.** A failed billable call fails loudly; retrying
   is the classic way students burn money. Log and surface instead.
5. **Prompts.toml is load-bearing data.** A settings enum value without a
   matching prompt block raises `KeyError` at startup by design. Any new
   setting/character concept needs its block.
6. **Neon pooled endpoint** (`-pooler` suffix) for the web process; the
   unpooled direct endpoint for migrations can be fine, but pooled works for
   both — and never log the URL (it embeds the password).
7. **OAuth consent screen** must leave "Testing" mode before handing in, or
   the grader's fresh Gmail may not be accepted / sessions hard-expire in 7
   days.
8. **Render free instances sleep** after inactivity: first request after idle
   is slow; do not build anything that assumes a warm process (the
   background-task book runner is process-bound — a restart during a render
   loses it; DB persistence of per-page state is what makes recovery a
   matter of re-rendering, not losing the user's book).
9. **The lru_cache on `_default_rendered_pages`** in `routes/book.py` is
   request-invariant on purpose; don't "fix" it into per-request work.
10. **CI mirrors local quality gates:** ruff (line length 95, src app+tests),
    ruff-format, mypy (disallow_untyped_defs, excludes only tests), pytest
    with coverage floor 85% (`pyproject.toml`). `make check` before every
    commit.

## 6. Housekeeping on arrival

- Confirm `git status` clean on `main` and CI green on the latest commit.
- Read `to-do.md` and `app/prompts.toml` before designing anything.
- When told to start step 1, follow §4 Step 1 exactly; commit message should
  follow the repo's conventional style
  (`feat:`, `fix:`, `test:`, `audit:` prefixes seen in `git log`).
- After each committed step, update `to-do.md` accordingly and stop — the
  judge reviews between steps.
