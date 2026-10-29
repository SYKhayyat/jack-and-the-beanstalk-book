# Jack and the Beanstalk

Upload one photo. The app cartoonifies it with settings you control, then
renders a **picture book**: the whole tale in 19 story pages, from the empty
cupboard and the magic-beans trade to the giant's castle, the hen that lays
golden eggs and the toppling beanstalk, plus an About page — 20 pages delivered
as one downloadable PDF. See [The picture book](#the-picture-book).

---

## Quick start

```bash
git clone <your-fork> && cd jack-and-the-beanstalk
make install

cp .env.example .env          # optional; mock mode needs no key
make dev
```

`make install` uses the committed lockfile and `.python-version` pins the local
runtime to Python 3.12.8. If you prefer not to install Python dependencies on the
host, build and run the same app in Docker:

```bash
docker build -t jack-and-the-beanstalk .
docker run --rm -p 8000:8000 jack-and-the-beanstalk
```

Open <http://localhost:8000>. With no `.env` at all the app runs in **mock
mode**: the full UI works, no network calls are made, and images come back as
clearly-labelled placeholders. That is the right way to build, test and load
test the app without spending anything — see
[Stress testing](#stress-testing-for-free).

To use the real model, set in `.env`:

```bash
IMAGE_PROVIDER=openai
OPENAI_API_KEY=your-key-here
```

Whichever provider you choose, the provider and the model are picked in the
environment; the visitor-facing UI has no control for either.

## Signing in

The app costs money per image, so it should not be open to everyone who finds
the URL. Two ways in, and exactly one is ever available at a time:

**Google sign-in (production).** With `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET` set, "Continue with Google" is the way in and the
password form is switched off entirely. Each visitor is a real person with a
Google identity, their books are theirs alone, and signing out clears only
their own session. The click-by-click setup — including the consent-screen
mode that must not be left on "Testing", or sessions hard-expire after a
week — is [docs/google-oauth-setup.md](docs/google-oauth-setup.md).

**The shared password (local development).** Without a Google client, one
password guards the whole app:

```bash
# .env
APP_PASSWORD=a-good-long-password
```

- The password is one value for everyone. There are no accounts and no
  per-person identity — just "are you one of us?". It exists to keep strangers
  off an app that bills per image; it is gone the moment Google is configured.
- A successful login sets an `HttpOnly` cookie valid for
  `APP_SESSION_MAX_AGE_DAYS` (7 by default). `Log out` in the header clears it.
- The cookie is signed with `SESSION_SECRET`. Changing `APP_PASSWORD` no
  longer signs anybody out; changing `SESSION_SECRET` does, which is the
  knob to reach for when a session needs killing.
- API and media requests without a session get a `401`; pages get redirected
  to `/login`. `/healthz` and `/static` stay open, because the platform polls
  the health check and the login page needs its CSS.
- Eight wrong passwords in fifteen minutes locks that client out of the login
  form (`APP_LOGIN_MAX_ATTEMPTS`).
- Leave `APP_PASSWORD` empty and the gate is open. That is fine for local
  development and reckless for anything else.

On Render the blueprint prompts for both the Google variables and
`APP_PASSWORD` at apply time, so the values live in the dashboard and never
land in the repo.

## Choosing an API key

Everything below was checked against vendor pricing pages on **2026-10-16**.
Prices move; re-check before you rely on them.

### What actually costs money

**Read this bit first: one finished book costs twenty API calls.** The pipeline
cartoonifies the photo (call 1), then renders the nineteen story pages (calls
2–20). So 200 finished books is **4,000 billed image generations**. Every figure
below is per *call*.

| Provider | Per call (1K image) | 200 books (4,000 calls) | Free tier? |
| --- | --- | --- | --- |
| **Agnes `agnes-image-2.5-flash`** | **$0** | **$0** | **Yes — free, ~10 requests/minute at 1K** |
| **OpenAI `gpt-image-2.5-flare`, `quality=low`** (default) | **$0.00588** | **~$23.52** | No |
| OpenAI `gpt-image-2.5-*`, `quality=medium` | not published | — | No |
| Cloudflare `flux-2-klein-9b` | $0.015 / MP | ~$60.00 | **Yes — ~6 images/day free** |
| Gemini `gemini-3.1-flash-lite-image` | $0.0336 | ~$134.40 | No |
| Gemini `gemini-3.1-flash-image` | $0.067 | ~$268.00 | No |

**OpenAI is the default here**, because at `quality=low` it is roughly 5.7×
cheaper than the cheapest Gemini image model for the same job, and the
portrait-then-approve flow below makes the quality question cheap to settle
empirically rather than by guesswork. If likeness turns out to need more than `low`, raise
`OPENAI_QUALITY` — an environment change, not a code change. Gemini stays
wired up as the fallback; `IMAGE_PROVIDER=gemini` switches to it.

Note that OpenAI only renders its per-image token counts in an interactive
calculator, so the `medium` and `high` tiers cannot be quoted from the static
pricing page. The `low` figure above is the one OpenAI states outright.

Input tokens add a few cents across a whole run — negligible. OpenAI also warns
that complex image prompts can take up to two minutes, which is why the default
`GENERATION_TIMEOUT_SECONDS` is generous.

**Gemini has no free trial credit any more.** Google's billing
docs state that the $300 Google Cloud welcome credit *cannot* be used for
Gemini API or AI Studio usage, and that Gemini API was excluded from the Cloud
Free Trial from March 2026. Since March 2026, paid Gemini API access is on a
prepay plan with a **$5 minimum** and credits that are non-refundable and expire
after a year. For a 200-book run, prepay about **$40**.

### The free testing option: Agnes AI

Agnes AI is the free tier that does the thing this app is built on: an
image-editing endpoint that takes **an array of reference images**, Data URI
Base64 or public URLs, alongside the prompt. Every resolution tier and every
input reference image is currently priced at zero.

```bash
# .env
IMAGE_PROVIDER=agnes
AGNES_API_KEY=your-agnes-key
```

The key comes from the Agnes AI console; the base URL is
`https://apihub.agnes-ai.com/v1`. The model is
`agnes-image-2.5-flash` and it does text-to-image, image-to-image and
multi-image composition, so the cartoonify pass (the photo) and the book pass
(the portrait plus the photo) both fit the same call shape.

What the free tier costs you is **speed, not money**:

| Free tier (`default`) | Requests a minute |
| --- | --- |
| 1K output | 10 |
| 2K output | 5 |
| 3K / 4K output | 1 |

One finished book is twenty calls, so at the free tier's 1K ceiling of ten
requests a minute a book takes about two minutes — fine for a classroom or a
family, not for a queue of strangers. `1K`
is therefore the default and `AGNES_SIZE` is the dial if you upgrade. The 1K
sizes per ratio are `1024x1024` (1:1), `1152x864` (4:3), `864x1152` (3:4) and
`1312x736` (16:9) — the same four ratios the app offers.

Two cautions worth stating plainly, because neither shows up in the docs:

- **Likeness on a real photo was judged unusable here.** Agnes accepts and
  returns the photo fine, which is why it is the free testing backend — but the
  face it drew was not the person who was uploaded. The assignment's "test with
  a free provider so you don't burn tokens" is exactly what this is for; the
  picture-book hand-in runs on OpenAI.
- **Free is a pricing page, not a promise.** The rate limits were cut by half
  for free users in September 2026, and NVIDIA's own free tier turned out to
  refuse the photo this app needs, so test on the day rather than trusting a
  number written into a README.

Two free tiers were tested against this app's one hard requirement — a model
that will edit *the uploaded photo* and then hold that face in the story pages.
One works well enough to test with, one does not work at all:

- **NVIDIA NIM** is free and its image models are quick, but the hosted preview
  API accepts only four canned example images and rejects an uploaded photo
  outright (`Expected: example_id, got: base64`). Text-to-image only, so no use
  here.
- **Agnes AI** does take an array of reference images and its free tier is
  genuinely free at 1K. It was implemented once, tested on the day, and removed
  when the likeness was judged unusable; it is re-landed now as the **free
  testing provider**, because iterating on a real app costs money and this one
  does not. `IMAGE_PROVIDER=agnes` is a config change, and it stays out of
  production.

Cloudflare Workers AI is the remaining candidate. Its free allocation is about
six edited images a day — enough to try a prompt, not enough to run a class —
and it needs a provider written to its own request shape rather than the
OpenAI one.

Two corrections worth stating plainly, because the obvious assumptions are wrong:


Two corrections worth stating plainly, because the obvious assumptions are wrong:


- **Google's free tier does not cover image generation.** The pricing page's
  free-tier column reads *Not available* for every image model, while text
  models read *Free of charge*. You need billing enabled on the API key. The
  *web UI* at <https://aistudio.google.com> is still free — see below.
- **The provider is a config change, not a rewrite.** `IMAGE_PROVIDER` picks
  between `mock`, `openai`, `agnes` and `gemini`. All three real providers send
  reference images with the text in one call, which is the capability this app
  is built on.
- **Do not use `gemini-2.5-flash-image`.** Google deprecated it and gave a
  shutdown date of **2026-10-02**, naming `gemini-3.1-flash-image` and
  `gemini-3.1-flash-lite-image` as the migration targets. This repo defaults to
  the Lite model, and a test fails if the default ever reverts to the retired
  one.

At roughly $0.034 per image and twenty calls per finished book, one book costs
about **68 cents**. Twenty test books across several photos is roughly $13.60.
(That is the star's portrait plus one call per story page; each optional
character photo adds one more portrait call before the pages start.)

### Testing your prompts for free, before you pay

You can iterate on this project's prompt strategy without an API key:

1. **Google AI Studio web UI — free, no key, no billing.** Upload a photo,
   paste the prompt, look at the result. Copy a prompt straight out of this
   app via the *Show the prompts that were sent* panel in the UI, or from
   `prompts.json`. This is the fastest loop and it costs nothing.
   Sign in at <https://aistudio.google.com>.
2. **Cloudflare Workers AI — a free tier that actually covers image editing.**
   Their docs: *"Our free allocation allows anyone to use a total of 10,000
   Neurons per day at no charge."* `@cf/black-forest-labs/flux-2-klein-9b`
   *"supports multi-reference editing capabilities"*, and their pricing table
   charges per input-image megapixel, which only makes sense if images are
   accepted. One 1K output plus one 1K reference costs about 1,545 neurons —
   roughly **6 edited images per day, every day, free**. The catch: Cloudflare
   publishes no worked example of an image-input request, so expect to spend an
   afternoon reverse-engineering the request body.
   <https://developers.cloudflare.com/workers-ai/platform/pricing/>
3. **Pollinations.ai — cheapest image-editing API.** A keyed, OpenAI-shaped
   `POST /v1/images/edits`; `black-forest-labs/flux.2-klein-4b` is around
   0.005 pollen (~$0.005) per image and is *not* paid-only. You register
   without a card and claim a "Quest Pollen" grant; the amount is variable by
   policy, so treat it as small but effectively free rather than unlimited.
   <https://gen.pollinations.ai/image/models>

For the graded checks — several different uploads, every book page — option 1
is enough, and option 2 covers you if you want to test *through this app* without
spending. Adding either as a third provider is a config change; see
*Adding a provider*.

### Limits this app is designed around

- **A small number of reference images per prompt.** A book page sends the
  child's two anchors (approved portrait + original photo) plus, for each
  photographed character the page actually casts, that character's portrait and
  photo — never the whole book's cast. The per-page set stays inside every
  model's limit whatever the book's total cast is.
- **Around 7MB per inline image.** Uploads are normalised to JPEG at up to
  1536px in `images.py`, well under that.
- **Aspect ratios** `1:1`, `3:2`, `2:3`, `3:4`, `4:3`, `4:5`, `5:4`, `9:16`,
  `16:9`, `21:9`. The `AspectRatio` enum offers only values from that list.

## Your API key

- It lives in the `OPENAI_API_KEY` environment variable when using the default
  real provider. Never in code, never in the repo, never in Slack.
- Gemini is also supported: set `IMAGE_PROVIDER=gemini` and use
  `GEMINI_API_KEY` instead. Agnes takes the same route:
  `IMAGE_PROVIDER=agnes` with `AGNES_API_KEY`. Only the selected provider's key
  is read.
- **Which provider and which model are used is the developer's choice and the
  visitor's non-business.** Both are environment variables
  (`IMAGE_PROVIDER`, `OPENAI_MODEL` / `GEMINI_MODEL`), and no part of the UI
  lets a visitor change either. Nor does anything a visitor can fetch: the
  page, the JSON the browser receives and the open `/healthz` endpoint all say
  nothing about the backend beyond whether the pictures are real. The provider
  and model are in the startup log and in the deployment's environment, which
  is where the operator reads them.
- `.env` is gitignored. `.env.example` is committed and contains only blank
  placeholders.
- The app never logs it, and the open `/healthz` endpoint never names the
  provider or the model. There are tests
  (`test_the_open_health_endpoint_names_neither_provider_nor_model`,
  `test_a_failing_request_never_echoes_the_key`) that fail if a key or a
  backend detail ever appears in a response.
- There is **no retry loop** around the API call, by design. A retry loop on a
  paid endpoint is the classic way to burn a student's balance. A failed call
  fails loudly and you see the reason in the logs.

## Settings the user controls

| Control | Type | What it does |
| --- | --- | --- |
| Cartoon vs. real | slider 1–10 | 1 is nearly photographic with a hint of line art; 10 is fully graphic. Each step has its own hand-written prompt language rather than a numeric formula. |
| Cartoon style | 9 options | Manga, anime, western cartoon, soft 3D animated feature, hand-painted 2D animation, comic book, pencil sketch, watercolour, flat vector. |
| Portrait background | 4 options | Plain, soft colour, keep the photo's, storybook. Portrait only — see above. |
| Options to choose from | 1–3 | Several alternatives from one call; 3 costs 3×. |
| Likeness to the real you | strict / balanced / loose | How hard to hold on to the uploaded face. Strict is the default because "recognisably the same person" is a graded requirement. |
| Picture shape | 4 options | 1:1, 4:3, 3:4, 16:9. Passed through to the model natively. |
| Age | as uploaded / child / teen / adult | Apparent age of the cartoon character. |
| Gender presentation | as uploaded / feminine / masculine / androgynous | Re-designed from the same facial bone structure, not swapped in from elsewhere. |
| Skin tone | as uploaded + 8 tones | Independent of the photo, or faithful to it. |
| Hair style, hair colour | closed choices | `as_uploaded` keeps the original; the other reviewed choices are safe, predictable prompt terms. |
| Outfit | closed choices | Defaults to storybook costume. |
| Expression | closed choices | Defaults to *focused and determined*. |
| Keep my glasses / Keep my facial hair | checkboxes | Whether real-world accessories carry into the cartoon. |

## Architecture

MVC, with the controller layer split into web pages and a JSON API.

```
app/
├── main.py               # application factory; the only entry point
├── config.py             # settings from the environment; the only place secrets enter
├── container.py          # composition root — the one module that wires everything
├── errors.py             # domain exceptions → HTTP status codes
├── dependencies.py       # FastAPI Depends accessors
├── auth.py               # the fallback sign-in gate: shared password + issued session
├── sessions.py           # the signed session cookie shared by both ways in
├── prompts.toml          # THE PROMPTS — every word sent to the model, as data
├── prompt_data.py        # the one loader for prompts.toml
├── models/               # MODEL: book pages, cartoon settings, results
│   ├── scenes.py         #     the image spec shared by every page
│   └── settings.py       #     the user-facing knobs, validated by pydantic
├── scripts/stress_test.py  # free load testing
├── services/             # domain logic, no HTTP
│   ├── prompts.py        #     prompt assembly: which line applies, joined how
│   ├── concurrency.py    #     admission control for generation requests
│   ├── pipeline.py       #     the cartoonify pass: build prompt, call provider
│   ├── images.py         #     upload validation, normalisation, EXIF stripping
│   ├── storage.py        #     artefact persistence (drafts)
│   ├── book_runner.py    #     the picture-book background job
│   ├── book_store.py     #     the book-store contract + filesystem implementation
│   ├── db_book_store.py  #     the same contract against DATABASE_URL
│   ├── user_store.py     #     the people who signed in (file or database)
│   ├── pdf.py            #     the PDF assembler: full-bleed art + real text
│   ├── rate_limit.py     #     per-minute billable-image brake
│   └── image_providers/  #     the swappable backend boundary
│       ├── base.py       #         the ImageProvider contract
│       ├── mock.py       #         offline placeholder renderer
│       ├── openai_provider.py #   the default real backend
│       ├── agnes_provider.py #     the free testing backend (reference images)
│       ├── gemini.py     #         the fallback backend
│       └── registry.py   #         name → class
├── db.py                 # the database engine and tables (only used with DATABASE_URL)
├── routes/               # CONTROLLER: book (the UI), api, media, auth, settings_form
├── templates/            # VIEW: book.html, login.html
├── models/book.py        #     book pages, text zones, About
├── models/labels.py      #     UI labels for the closed enums
└── static/               # VIEW: style.css + book.js, book-view.js
```

### The pipeline: portrait first, book second

One API call per phase, no loops:

1. **cartoonify** — the photo goes in, a cartoon portrait comes out. Every user
   setting is applied here.
2. **book render** — call `POST /api/book/start` only on an approved portrait;
   the story pages then render **concurrently** on the same concurrency gate,
   one slot per page, so a book finishes in roughly one page-time per
   `MAX_CONCURRENT_GENERATIONS` instead of one page-time per page.

The pages take the portrait deliberately. Models drift towards a generic
face when the story action is interesting — a giant beanstalk at dawn is a much
stronger instruction than "draw this person" — so every page prompt carries the
portrait as a reference image and repeats the identity directive.

### Where identity and story fidelity come from

These are the two graded requirements, so they are worth naming explicitly:

**It looks like the person.** Every call carries an identity directive that names
the specific features to preserve (bone structure, eye shape and colour, nose,
mouth, eyebrows, hairline, skin tone, moles, freckles, glasses, facial hair) and
forbids inventing a new face or averaging towards a generic one. The story-page
prompts repeat it rather than assuming the portrait's result survived.
`likeness_strength` lets the user trade fidelity against style when they conflict.

**It is faithful to the story.** Each page carries its own caption, a subject
action, an environment, a lighting note, a camera note, a checklist of objects
that must be visible (`must_include`) and a list of things that would make the
image unfaithful (`must_avoid`). The negative instructions also forbid text
anywhere in the image — the story words are painted by the PDF layer, not by the
model, which is what keeps the picture honest against its page.

**The other characters stay themselves too.** Beyond the starring child, the
cast is book data (`[book.cast]` in `app/prompts.toml`): Jack, the mother, the
old man, the giant, and Milky-White. Every page prompt carries the same cast
sheet — one block of pinned looks, byte-identical across the book — so no
character's description is restated (and allowed to drift) page by page. A
visitor may optionally attach a photo to any cast member marked photoable
(`mother`, `old_man`, `giant`); that character then gets the same cheap portrait
pass the star does, and only the pages that cast them send those references.
Milky-White is the cow: she is pinned in prose and has `photoable = false`, so
there is no UI field, catalogue entry or API path that can give her a face. A
page always sends only its own cast's references — never the whole book's — so
the per-call reference set stays small.

## The picture book

`/` turns one approved portrait into a **20-page picture book PDF**. You star
on every page of the whole tale — the empty cupboard, the magic-beans trade, the
beans flying out the window, the beanstalk at dawn, the climb into the clouds,
the giant's kitchen and the bag of gold, the hen that lays golden eggs and the
singing harp, "Fee-fi-fo-fum!", the chase and the toppling beanstalk — nineteen
story pages followed by an **About** page. The story
text is never drawn by the model: the art carries a deliberately empty quiet
zone, and the PDF paints the polished storybook prose as real, selectable text
(fpdf2, Comic Neue + Noto Sans Hebrew bundled, HarfBuzz shaping when
available). The About free text you type is PDF-only and never reaches a
prompt — that is enforced by a test.

- `GET /` — the book UI (photo → who is the star → About → portrait →
  approve → the book grows page by page → download the PDF)
- `GET /api/book/characters` — the cast slots a photo may be attached to
  (`mother`, `old_man`, `giant`). Milky-White is cast in prose and is absent:
  she has no upload path anywhere.
- `POST /api/book/start` — multipart: `generation_id` (+ optional `child_name`,
  `about_name`, `about_age`, `about_title`, `details`, and repeated
  `character=cast_key:generation_id`) → renders in the background. A blank
  name resolves to Jack or Jane, following the photo's gender answer; the two
  names exist so the About page can show a pseudonym. Each `character` pair
  names a photoable cast member and the portrait made for them with
  `/api/cartoonify`; pages that cast that character send their images as
  references, and only those pages do.
- `GET /api/book/{id}` — poll: `rendering | complete | failed`, plus one entry
  per page (`pending | making | done`) and a url once a page is done
- `GET /book-media/{id}/portrait`, `GET /book-media/{id}/pages/{n}`,
  `GET /book-media/{id}/pdf`, `GET /book-media/{id}/prompts.json`

Pages render concurrently on the same concurrency gate as everything else, one
slot each — so the gate, not the book, bounds how many provider calls are in
flight at once. Starting a book charges one admission, and each page is charged
as it renders rather than the whole page count up front, so a book larger than a
minute's allowance is delayed, never refused. Adding a page is one new
`[[book_pages]]` block in
`app/prompts.toml`. Every story wording lives in that single prompts file — as
required.

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | The book UI |
| `GET` | `/healthz` | Liveness and whether the pictures are real. Names neither the provider nor the model, and never includes secrets. |
| `GET` | `/api/settings-options` | Every enum, the defaults, and the slider limits |
| `GET` | `/api/book/pages` | The book's page identities (titles, text zones) |
| `GET` | `/api/book/characters` | The photoable cast slots (`mother`, `old_man`, `giant`); the cow is never listed |
| `POST` | `/api/cartoonify` | multipart: `photo` + settings → the portrait (1 call) |
| `POST` | `/api/book/start` | multipart: `generation_id` (+ optional star/About fields and repeated `character=key:generation_id`) → background book (1 call/page) |
| `GET` | `/api/book/{id}` | Book status: rendering / complete / failed + page grid |
| `GET` | `/media/{id}/cartoon` | The cartoon portrait |
| `GET` | `/book-media/{id}/portrait` | The book's approved portrait |
| `GET` | `/book-media/{id}/pages/{n}` | One story page's art, once it exists |
| `GET` | `/book-media/{id}/pdf` | The finished 20-page PDF (409 until complete) |
| `GET` | `/book-media/{id}/prompts.json` | The exact prompt sent for every page |

`POST /api/cartoonify` returns image *URLs* rather than inline bytes, so the
browser can fetch them lazily and the response stays small. Interactive docs are
at `/docs`.

Errors are mapped in one place (`app/errors.py`), so a failure never leaks a
stack trace or a key:

| Status | `error` | Meaning |
| --- | --- | --- |
| `404` | `unknown_draft` | That `generation_id` expired or never existed |
| `400` | `invalid_image` | Not a readable still image, or too large |
| `422` | `invalid_settings` | A setting failed validation |
| `429` | `server_busy` | Every generation slot was busy; nothing was sent to the model |
| `429` | `generation_rate_limited` | This client burned its per-minute allowance of billable images |
| `404` | `unknown_book` | That book id expired or never existed |
| `409` | `book_not_ready` | The book is still rendering — poll `/api/book/{id}` |
| `502` | `provider_error` | The model was reached and failed |
| `503` | `provider_unavailable` | No usable provider — usually a missing API key |
| `504` | `generation_timeout` | Ran past the budget; safe to retry |

Example:

```bash
# Call 1 — one portrait, all settings applied here.
ID=$(curl -s -X POST http://localhost:8000/api/cartoonify \
  -F photo=@me.jpg -F cartoon_intensity=9 -F cartoon_style=manga \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["generation_id"])')

# Calls 2–5 — the four story pages, rendered in the background.
BOOK=$(curl -s -X POST http://localhost:8000/api/book/start \
  -F generation_id="$ID" | python3 -c 'import json,sys; print(json.load(sys.stdin)["book_id"])')

# Poll while rendering, then download:
curl -s http://localhost:8000/api/book/"$BOOK"
curl -o book.pdf http://localhost:8000/book-media/"$BOOK"/pdf
```

## Adding a provider

1. Subclass `ImageProvider` in `app/services/image_providers/` and implement
   `generate(ImageRequest) -> GeneratedImage`.
2. Register it in `registry.py`.
3. Add the name to `ProviderName` in `config.py`.

Nothing else changes. The pipeline, prompts, routes and UI are unaware of which
backend is running.

## Editing the prompts

Every word the app sends to an image model lives in `app/prompts.toml`, not in
Python. Change the prose there and restart; no code change and no rebuild. The
values are keyed by the enum values in `app/models` (for example
`cartoon_style = "western_cartoon"` maps to `[styles].western_cartoon`), so a
setting added without its wording raises `KeyError` at startup rather than
sending a prompt with a hole in it. `app/services/prompts.py` only decides which
lines apply and how they are joined.

## Two steps, so you only pay for what you keep

The book is twenty calls, and the interface keeps the portrait apart on purpose:

```
  upload + settings
        |
        v
  +-------------+   1 call   +------------------------------+
  | /api/cartoonify |------->| cartoon portrait             |
  +-------------+            | "does this look like me?"    |
        ^                    +---------------+--------------+
        |  tweak settings, retry             | approve
        +-------------------------------------+
                                             | 19 calls
                                             v
                               +-------------------------------+
                               | /api/book/start               |
                               | portrait -> the 19 story pages|
                               +-------------------------------+
```

Three things fall out of that split, and all three are cost or quality wins:

- **A likeness you dislike costs one call, not twenty.** Change the intensity,
  the style, the hair, the likeness strength, and retry — the settings panel
  stays live while you look at the portrait. Only an approved portrait is worth
  the nineteen story pages.
- **Every page is built from the portrait you approved.** Not one you hoped
  would look better by page nineteen.
- **The judgement happens where it is cheap.** Deciding whether the face
  survived is a question about the portrait, and the portrait is the cheap
  phase. Settling it after paying for the book is backwards.

The draft — the portrait — is persisted, which is what lets the book pass send
the approved likeness without the browser re-uploading anything.

### Portrait background

Applies to the cartoon portrait only, and every variant still demands an
unobstructed face. The story pages draw their environment from their own page
specification, so a background asked for here would not survive into the story
art anyway.

### Options to choose from

One API call can return several alternatives, and you pick rather than re-roll.
To be clear about the arithmetic: **this is not cheaper per call** — three
options is three times the cost of one, because you are billed per returned
image. It is cheaper per *kept* picture only if you would otherwise re-roll
repeatedly. At $0.00588 a call, three options is $0.018, so it is off by default
and worth turning on while settling a likeness.

### One-step mode

A checkbox that skips the preview gate and goes straight from upload to the
rendered book, for when you already know your settings or just want the result.
The UI calls `/api/cartoonify` and immediately starts the book on it.

## Stress testing, for free

`scripts/stress_test.py` drives `POST /api/cartoonify` concurrently and reports
throughput, latency percentiles, the status-code breakdown and the estimated
spend. Point it at a **mock-mode** instance for unlimited, zero-cost load:

```bash
# terminal 1 — no API key, no network, no cost
IMAGE_PROVIDER=mock STORAGE_DIR=/tmp/stress uvicorn app.main:create_app --factory --port 8000

# terminal 2
python scripts/stress_test.py --url http://localhost:8000 --requests 120 --concurrency 16
```

```
  requests       120 in 64.9s  (1.9/s)
  status codes   {201: 120}
  latency        p50 6.515s  p95 16.529s  max 21.200s  mean 7.939s
  missing cartoon urls 0

  cost: $0.00 — mock mode makes no API calls.
PASS: every request succeeded.
```

Mock mode still exercises everything that can break in production: upload
validation, Pillow decoding, the concurrency gate, the cartoonify pass,
file writes, and the media routes. Each request uploads a distinct synthetic
image, so nothing is served from a cache.

The p50 above is the gate working, not a bottleneck: 16 requests against
`MAX_CONCURRENT_GENERATIONS=2` means most of them wait their turn.

Against a real provider it costs roughly `successful × $0.006` (one flare-low
call per portrait), so the script requires an explicit acknowledgement first:

```bash
python scripts/stress_test.py --url https://your-app.onrender.com \
  --requests 12 --concurrency 3 --yes-i-am-using-a-paid-key
```

Beyond the script, the free AI Studio tier is rate-limited rather than
unlimited — fine for the several-upload testing described below, not for load
testing. Use mock mode for load and the free tier for picture quality.

### What the timeout and the gate actually do

`GENERATION_TIMEOUT_SECONDS` (default 600) is **one budget for the whole
request**, covering the wait for a free slot *and* the model call. The book
applies the same budget per story page. It is not a queue allowance plus a
generation allowance — otherwise a request could sit in the queue for the full
timeout and then get another full timeout on top.

| Situation | Result |
| --- | --- |
| Generation finishes inside the budget | `201`, with the images it asked for |
| Queues longer than its remaining budget | `429 server_busy`, "retry in a moment", **nothing sent to the model** |
| Runs past the budget | `504 generation_timeout` |

Two details that are easy to get wrong and expensive to get wrong:

- **The abort is real.** The budget is applied to the provider's HTTP client,
  so a slow generation aborts its socket. It is not a Python thread left
  running and burning tokens after the client has gone.
- **A timed-out request keeps its slot.** The worker outlives the `504`, so it
  holds the concurrency slot until it genuinely finishes. Releasing at timeout
  would tell the next request the server was idle while it was still working,
  and two generations would run against a limit of one.

`MAX_CONCURRENT_GENERATIONS` is per process. Render runs one worker, so it is a
global limit there; if you ever scale to several workers, the effective limit
is the setting times the worker count.

## Tests

```bash
make test
make lint
make typecheck
# or all three with:
make check
```

The suite is fully offline and deterministic: it never touches the network and
never needs an API key. Provider behaviour is verified against a recording
double, and the Gemini provider is tested with the real SDK and a fake transport
— request shaping and response parsing are covered without spending a cent.

Coverage is concentrated where the risk is: prompt construction (the product),
the provider boundary (the swap point), upload validation (untrusted input), and
path-traversal refusal.

### Checking the two graded requirements

Automated tests can prove the *prompts* demand identity and story fidelity. They
cannot prove the *pixels* do. That check is yours to run by eye:

1. Run the app for real (`IMAGE_PROVIDER=gemini`).
2. Upload several photos that are not the developer: different faces, skin
   tones, glasses, facial hair, ages. Two or three at minimum.
3. Render whole books for each person.
4. For each page ask two questions, and be honest about the answers:
   - *Is this recognisably the person who was uploaded?* Show the photo and the
     output side by side to someone who does not know either of you.
   - *Does the picture show the page's moment?* Read the page's `must_include`
     and `must_avoid`, then describe the picture. No invented props, no second
     person, no text.
5. If identity slips, tighten `identity_directive()` and switch
   `likeness_strength` to `strict`. If a page drifts, adjust that page's
   `must_include` and `must_avoid` in `app/prompts.toml`.
6. Re-run. `prompts.json` for each book shows the exact prompt behind every
   page, so you can check attempts side by side without confusion.

Use a few different uploads and iterate the prompt. One photo that happens to
work is not evidence.

## Deploying to Render

The repo ships a blueprint, so this is short:

1. Push the repo to GitHub.
2. In Render: **New → Blueprint**, pick the repo, apply. `render.yaml` declares
   the build and start commands and the health check.
3. Render will prompt for `OPENAI_API_KEY` — paste it there. That is the only
   place it belongs. It is never in the repo. It also prompts for the Google
   sign-in variables (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
   `GOOGLE_REDIRECT_URI`, `SESSION_SECRET`) and `APP_PASSWORD`; the
   walk-through for the Google ones is
   [docs/google-oauth-setup.md](docs/google-oauth-setup.md). With Google set,
   the password is unused — the deployment signs people in with Google.
4. The app boots on the mock provider: with no `IMAGE_PROVIDER` set it runs
   free, so the first deploy is always green. Once your key is in, set
   `IMAGE_PROVIDER=openai` in the dashboard and save; Render redeploys. The
   blueprint deliberately does not pin this variable, so the dashboard is the
   single source of truth and the next sync keeps your choice. The blueprint
   uses the cheap `gpt-image-2.5-flare` model at low quality.
5. Check `https://<your-app>.onrender.com/healthz` — it should report
   `"using_real_api": true`.
6. Put the live URL in the hand-in and in Slack.

Render's free plan sleeps after inactivity, so the first request to the deployed
URL takes a while.

**Persistence is a switch, not a rewrite.** Without `DATABASE_URL` the app
stores everything on the instance disk, which Render wipes on every restart —
books disappear, and a restart between the portrait and the book asks the
uploader to start over. Set `DATABASE_URL` (a Neon free-tier Postgres works
well) and books, their pages, their PDFs and the people who made them live in
Postgres instead: a completed book survives a restart or redeploy, and it
stays answerable to the account that made it. The schema is applied once with `make migrate` (see `to-do.md` for the
first-deploy runbook). Local development needs none of this — with the
variable empty, nothing ever touches a database.

## Configuration reference

| Variable | Default | Notes |
| --- | --- | --- |
| `IMAGE_PROVIDER` | `mock` | `mock`, `openai`, `gemini` or `agnes` |
| `OPENAI_API_KEY` | *(empty)* | Required for `openai` |
| `OPENAI_MODEL` | `gpt-image-2.5-flare` | Cheapest published per-call price |
| `OPENAI_QUALITY` | `low` | `medium`/`high`/`xhigh`/`max` cost more; raise only if likeness needs it |
| `AGNES_API_KEY` | *(empty)* | Required for `agnes`. Free tier: ~10 requests/minute at 1K. |
| `AGNES_MODEL` | `agnes-image-2.5-flash` | The image model that takes an array of reference images. |
| `AGNES_SIZE` | `1K` | Output tier: `1K`, `2K`, `3K`, `4K`. Free tier allows ~10/5/1 requests a minute. |
| `AGNES_BASE_URL` | `https://apihub.agnes-ai.com/v1` | Change only if the service moves. |
| `GEMINI_API_KEY` | *(empty)* | Required for `gemini` |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite-image` | Keep it cheap. `gemini-3.1-flash-image` is the higher-quality option. |
| `APP_ENV` | `development` | `production` raises the log level |
| `GOOGLE_CLIENT_ID` | *(empty)* | With `GOOGLE_CLIENT_SECRET`, makes Google sign-in the only way in. See `docs/google-oauth-setup.md`. |
| `GOOGLE_CLIENT_SECRET` | *(empty)* | The OAuth client secret; lives only in the environment. |
| `GOOGLE_REDIRECT_URI` | *(empty)* | The exact redirect URI registered with Google; empty means the app derives it from the request (local development only). |
| `SESSION_SECRET` | *(empty)* | Signs the session cookie. Unset, every restart signs everyone out. Not the password. |
| `APP_PASSWORD` | *(empty)* | The local-dev fallback: shared password required when no Google client is configured. Both unset means the app is open. |
| `APP_SESSION_MAX_AGE_DAYS` | `7` | How long one successful sign-in stays valid. Rotating `SESSION_SECRET` invalidates every session. |
| `APP_LOGIN_MAX_ATTEMPTS` | `8` | Wrong passwords per client before a 15-minute lockout. |
| `MAX_UPLOAD_MB` | `10` | Enforced before decoding |
| `GENERATION_TIMEOUT_SECONDS` | `600` | One budget per request: queue + model call. Also applied to the provider's HTTP client. |
| `MAX_CONCURRENT_GENERATIONS` | `2` | Admission control per process. Beyond this, requests queue and then get 429. |
| `MAX_GENERATIONS_PER_MINUTE` | `30` | Per-client abuse brake for billable requests; Render's blueprint uses `12`. |
| `STORAGE_DIR` | `./storage` | Created on startup |
| `DATABASE_URL` | *(empty)* | Postgres URL (Neon pooled endpoint). Set = books/people persist in the database and survive restarts; empty = everything on disk. Apply the schema once with `make migrate`. |

`MAX_UPLOAD_MB` and `STORAGE_DIR` map to `MAX_UPLOAD_MB` and `STORAGE_DIR`.
