# Jack and the Beanstalk

Upload one photo. The app cartoonifies it with settings you control, then
renders a **picture book**: an AI cover, the whole tale in 20 story pages —
from the empty cupboard and the magic-beans trade to the giant's castle, the hen
that lays golden eggs and the toppling beanstalk — plus an About page, all
delivered as one downloadable PDF. Signed-in visitors get a library sidebar of
their books and can redraw a page, replace a face, rewrite the About text or
delete a book. See [The picture book](#the-picture-book).

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

**Read this bit first: one finished book costs twenty-two API calls.** The
pipeline cartoonifies the photo (call 1), draws the cover (call 2), then renders
the twenty story pages (calls 3–22). So 200 finished books is **4,400 billed
image generations**. Every figure below is per *call*.

| Provider | Per call (1K image) | 200 books (4,200 calls) | Free tier? |
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

One finished book is twenty-two calls, so at the free tier's 1K ceiling of ten
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

At roughly $0.034 per image and twenty-two calls per finished book, one book costs
about **74 cents**. Twenty test books across several photos is roughly $14.80.
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
  page, the JSON the browser receives and the open `/healthz` endpoint say
  nothing about the backend beyond whether the pictures are real and how many
  generations may run at once. The provider and model are in the startup log
  and in the deployment's environment, which is where the operator reads them.
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
├── scripts/make_plates.py  # paint a story's plates through the provider
├── scripts/plate_prompts.py # print/export the exact plate prompts (no calls)
├── scripts/import_plates.py # bring in plates painted elsewhere, and approve them
├── services/             # domain logic, no HTTP
│   ├── prompts.py        #     prompt assembly: which line applies, joined how
│   ├── image_review.py   #     tier-1 review: free, deterministic, every image
│   ├── concurrency.py    #     admission control for generation requests
│   ├── pipeline.py       #     the cartoonify pass: build prompt, call provider
│   ├── images.py         #     upload validation, normalisation, EXIF stripping
│   ├── storage.py        #     artefact persistence (drafts)
│   ├── book_runner.py    #     the picture-book background job
│   ├── book_store.py     #     the book-store contract
│   ├── db_book_store.py  #     the one implementation, over the database
│   ├── user_store.py     #     the people who signed in
│   ├── pdf.py            #     the PDF assembler: full-bleed art + real text
│   ├── rate_limit.py     #     per-minute billable-image brake
│   └── image_providers/  #     the swappable backend boundary
│       ├── base.py       #         the ImageProvider contract
│       ├── mock.py       #         offline placeholder renderer
│       ├── openai_provider.py #   the default real backend
│       ├── agnes_provider.py #     the free testing backend (reference images)
│       ├── gemini.py     #         the fallback backend
│       └── registry.py   #         name → class
├── db.py                 # the database engine, tables and boot-time migration
├── stories.py            # the shelf: which books exist and which can be made today
├── routes/               # CONTROLLER: web (the four screens), book, api, media, auth
├── templates/            # VIEW: shelf, studio, about_page, book, login (signed-out shelf), wallet
├── models/book.py        #     book pages, emotional beats, About
├── models/labels.py      #     UI labels for the closed enums
└── static/               # VIEW: style.css, one script per screen (shelf.js, studio.js,
                          #       about.js, book-live.js), drawer.js, api.js (the only fetch),
                          #       photo.js, downscale.js, studio-draft.js, covers/
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
cast is story data: each story's seed (`app/books/<story>.toml`) holds its
character bible under `[cast.<key>]` — the look a page draws with no photo, the
look it draws when the visitor supplied one, and the brief the portrait pass
draws them from. Jack's cast is the mother, the old man and the giant; Red's is
the mother, Grandmother, the Woodcutter and the wolf. Every page prompt carries
the same cast sheet — one block of pinned looks, byte-identical across the
book — so no character's description is restated (and allowed to drift) page
by page. A visitor may optionally attach a photo to any supporting character
marked `photoable`; that character then gets the same cheap portrait pass the
star does, and only the pages that cast them send those references. The wolf
has `photoable = false`, and Milky-White is not cast at all (she is a prop of
the places, painted into the plate), so there is no UI field, catalogue entry
or API path that can give either of them a face. A page always sends only its
own cast's references — never the whole book's — so the per-call reference set
stays small.

## The picture book

`/` turns one approved portrait into a **22-page picture book PDF**: a cover,
twenty story pages and an About page. You star
on every page of the whole tale — the empty cupboard, the magic-beans trade, the
beans flying out the window, the beanstalk at dawn, the climb into the clouds,
the giant's kitchen and the bag of gold, the hen that lays golden eggs and the
singing harp, "Fee-fi-fo-fum!", the chase and the toppling beanstalk — twenty
story pages followed by an **About** page. The cover is one extra AI image of
the star beside the beanstalk; the title is drawn by the PDF as real text, not
by the model. The story
text is never drawn by the model: every story page is one template — the art
edge to edge on top, the fairy-tale prose set beneath it in a parchment margin
of the same height on every page, at one font size for the whole book, as
real, selectable text (fpdf2, Literata + Noto Sans Hebrew bundled, HarfBuzz
shaping when available). Text never covers the art. The last page frames the
child's portrait above a few sentences built from the About fields. The About free text you type is PDF-only and never reaches a
prompt — that is enforced by a test.

- The UI is four screens, one job each (see
  [`docs/ui-redesign-plan.md`](docs/ui-redesign-plan.md)):
  `GET /` the shelf (choose a book) → `GET /stories/{story}/studio` (one
  section per character: photo, comic beside it, alternative takes, "Not quite
  right?" chips, "Adjust the look") → `GET /stories/{story}/about` (the About
  page's words, cartoon / photo / both, the page shape, Make the book) →
  `GET /books/{id}` (the book drawn live, one page at a time, then the two
  downloads and the edit strip). Signed out, `/login` is the same shelf with a
  sign-in banner.
- `POST /api/cartoonify/{generation_id}/choose` — `index`: keep one of a
  draft's alternative takes. Saves it as a new draft and returns it in the
  `cartoonify` shape; draws nothing and spends no rate token.
- `GET /api/stories/{story}/characters` — the cast slots a photo may be
  attached to in that story (`mother`, `old_man`, `giant`; `mother`,
  `grandmother`, `woodcutter`). The wolf and the cow have no upload path
  anywhere.
- `POST /api/book/start` — multipart: `story`, `generation_id` (+ optional `child_name`,
  `about_name`, `about_age`, `about_title`, `details`, `about_photo` =
  `cartoon | photo | both`, `aspect_ratio` for the whole book, and repeated
  `character=cast_key:generation_id`) → renders in the background. A blank
  name resolves to the story's default star name (Jack or Jane; Red), following
  the photo's gender answer; the two names exist so the About page can show a
  pseudonym. Each `character` pair names a photoable cast member and the
  portrait made for them with `/api/cartoonify` (for the same story); pages
  that cast that character send their images as references, and only those
  pages do. The book is pinned to the story as it is when it starts — its
  template version when there is one, otherwise the latest words and looks —
  so a later seed change or look revision never reaches it.
- `GET /api/book/{id}` — poll: `rendering | complete | failed`, plus one entry
  per page (`pending | making | done`) and a url once a page is done
- `GET /book-media/{id}/portrait`, `GET /book-media/{id}/photo`,
  `GET /book-media/{id}/pages/{n}`, `GET /book-media/{id}/pdf`,
  `GET /book-media/{id}/prompts.json` — the portrait and the original photo can
  also be handed over as files with `?download=1`
- `GET /book-media/{id}/sources.zip` — every photo and comic behind the book
  (star and each photographed character), the cover, the 20 page pictures,
  the About text and `prompts.json`, in one archive to keep

Pages render concurrently on the same concurrency gate as everything else, one
slot each — so the gate, not the book, bounds how many provider calls are in
flight at once. Starting a book charges one admission, and each page is charged
as it renders rather than the whole page count up front, so a book larger than a
minute's allowance is delayed, never refused. A story's words, scenes and
cast live in its seed file (`app/books/<story>.toml`), which the app validates
and loads into the database at every boot; every other word sent to a model
lives in `app/prompts.toml`.

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | The book UI |
| `GET` | `/healthz` | Liveness, whether the pictures are real, and the per-process generation ceiling (`max_concurrent_generations`). Names neither the provider nor the model, and never includes secrets. |
| `GET` | `/api/settings-options` | Every enum, the defaults, and the slider limits |
| `GET` | `/api/stories/{story}/pages` | A new book's page identities (titles, story text) |
| `GET` | `/api/stories/{story}/characters` | The story's photoable cast slots; a character who can't be photographed is never listed |
| `POST` | `/api/cartoonify` | multipart: `story`, `photo` + settings (+ optional `character`) → the portrait (1 call) |
| `POST` | `/api/book/start` | multipart: `story`, `generation_id` (+ optional star/About fields and repeated `character=key:generation_id`) → background book (1 call/page); `409 story_not_ready` while the start gate holds the story |
| `GET` | `/api/book/{id}` | Book status: rendering / complete / failed + page grid |
| `GET` | `/api/books` | The signed-in person's library (empty in password mode) |
| `POST` | `/api/book/{id}/edit` | multipart `action=page|character|cover|about|retry|delete` — edit an existing book |
| `DELETE` | `/api/book/{id}` | Delete a book and all of its artefacts (the sidebar has a × on every card) |
| `GET` | `/media/{id}/cartoon` | The cartoon portrait |
| `GET` | `/book-media/{id}/portrait` | The book's approved portrait (`?download=1` saves it as a file) |
| `GET` | `/book-media/{id}/photo` | The original photograph the book was drawn from (`?download=1` saves it as a file) |
| `GET` | `/book-media/{id}/cover` | The AI cover illustration |
| `GET` | `/book-media/{id}/pages/{n}` | One story page's art, once it exists |
| `GET` | `/book-media/{id}/pdf` | The finished 22-page PDF (409 until complete) |
| `GET` | `/book-media/{id}/prompts.json` | The exact prompt sent for every page |

`POST /api/cartoonify` returns image *URLs* rather than inline bytes, so the
browser can fetch them lazily and the response stays small. Interactive docs are
at `/docs`.

### Your books, and editing one

In Google mode every screen's header carries **My books**: a drop-down sheet of
every book the signed-in person owns, newest first, with its cover thumbnail
and status. Each row carries a small **×** so a book can be put away without
opening it first. Clicking one opens `/books/{id}` — the book itself, one spread
at a time (click, swipe or use the arrow keys; `#page-7` in the URL keeps your
place), a map of all twenty pictures, the two downloads and the edit strip. Password/open mode deliberately has no library: there is no person to
file books under, and one shared pile of children's photographs would be the
wrong answer.

The finished book can be read without the PDF at all: `/books/{id}` shows the
pictures and their story lines in the page, and the PDF (cover + 20 story pages
+ About = 22 pages) is one click away. The PDF embeds its pictures as
print-sized JPEGs, so a finished book is a few megabytes rather than the ~42 MB
it used to be — small enough to assemble, download and view on a 512 MB
instance.

The wallet fee is taken once per book, so edits add no further fee. In image
calls the edit surface is priced honestly: redrawing one page is one image call;
replacing the star redraws the whole book and cover; replacing the mother, old
man or giant redraws only the pages that cast them; rewriting the About text or
changing the title rebuilds the PDF with no model call. The original photo,
portrait, settings and every supplied character face are stored with the book,
so an edit still works after the upload draft has expired. One edit per book may
run at a time (`409 book_busy`), and a session that expires mid-render sends the
visitor through sign-in back to the same book rather than losing it.

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
  -F story=jack_and_the_beanstalk \
  -F photo=@me.jpg -F cartoon_intensity=9 -F cartoon_style=manga \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["generation_id"])')

# Calls 3–22 — the twenty story pages, rendered in the background. The
# About page costs no call: the PDF draws it locally.
BOOK=$(curl -s -X POST http://localhost:8000/api/book/start \
  -F story=jack_and_the_beanstalk -F generation_id="$ID" | python3 -c 'import json,sys; print(json.load(sys.stdin)["book_id"])')

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

Every word the app sends to an image model lives in data, not in Python: a
story's own words, scenes and cast in its seed (`app/books/<story>.toml`), and
everything else — the shared rules, each story's props-and-places sheet and its
own extra rules under `[stories.<story>]` — in `app/prompts.toml`. Change the
prose there and restart; no code change and no rebuild. A changed seed is
loaded at boot as new revisions, so books already made keep their words. The
values are keyed by the enum values in `app/models` (for example
`cartoon_style = "western_cartoon"` maps to `[styles].western_cartoon`), so a
setting added without its wording raises `KeyError` at startup rather than
sending a prompt with a hole in it. `app/services/prompts.py` only decides which
lines apply and how they are joined.

## Reviewing plates (operator only)

Every story page is painted once as an empty **plate** — the place, the props
and the light, with nobody in it and no lettering — and approved by the
operator before any book is made. A book's pages are then edit passes that
paint the cast into those plates, so the plate is also the layout: it declares
one slot per character on its page.

The app loads the story seeds into the database at every boot (unchanged seeds
write nothing); `make seed` does the same for a database you are preparing
ahead of a deploy.

Signed in as the operator — set `OPERATOR_SUB` to your Google `sub` —
open `/admin/plates` and press **Paint missing plates** on a story. It paints
21 draft plates (cover plus twenty pages), one image call each, and the button
shows which page it just finished; the same command from a terminal is

```bash
.venv/bin/python -m app.scripts.make_plates --story little_red_riding_hood
```

The screen and the command paint the same plates from the same prompts. Either
way it is one call per plate, the count is shown before you start, and an
already approved plate is never repainted.

- `/admin/plates` — every story, how many of its 21 plates are painted and
  approved, whether it has a complete template version, and its **start
  gate**: tick "Only start books on approved plates" and that story's books
  cannot start until it has a complete version — the shelf card says "Not
  ready yet" with one plain sentence, and the studio, the portrait pass and
  `/api/book/start` refuse it before any image is paid for. Untick it and books
  start on the latest words and looks. Books already started are untouched
  either way.
- `/admin/plates/{story}` — the contact sheet: all 21 plates in page order,
  with a toggle showing each character's slot and the cover's title band. This
  is also where **Paint missing plates** lives: one press, one image call per
  plate, and it stops when every page has a candidate. Each painted tile has
  its own **What should change?** box: type a correction, press Paint again,
  and only that plate is painted again.
- `/admin/plates/{story}/{page}` — that page's candidates: approve, paint one
  page again with a change note, edit the whole prompt, or retire a plate. The
  cover's Approve is disabled while its top band is too busy to carry real
  text, and the reason is printed instead.

Approving is idempotent and records the operator and the timestamp. Approving
the last missing page — or replacing an approved plate — writes a new template
version; books already started keep the version they pinned. Plate images and
prompts live in the database, never in the repo, and every action is logged
with the request id and without image bytes. Anybody who is not `OPERATOR_SUB`
gets the same 404 an unknown URL gives.

### Painting the plates somewhere else

A plate is painted once per story and is nobody's personal data, so it does not
have to be painted by this app at all: any image tool works, including a free
web UI with no API key, which costs nothing and needs nobody's credit card.
Two commands exist for that loop. Neither one makes an image call.

```bash
# 1. Read the exact prompts out — the same words make_plates would have sent.
make plates-prompts              # writes plates/jack_and_the_beanstalk/0.txt … 20.txt
make plates-prompts STORY=little_red_riding_hood

# 2. Save each result as its page number (0 is the cover) in that directory,
#    then bring the set back in and approve it in one go.
make plates-import

# The same two steps, spelled out, if you prefer:
python -m app.scripts.plate_prompts --story jack_and_the_beanstalk \
    --out ~/plates/jack                     # or --page 4 for one page
python -m app.scripts.import_plates --story jack_and_the_beanstalk \
    --dir ~/plates/jack --approve
```

Plain `python` is enough: if it cannot import the project's dependencies, every
operator script hands the command to `.venv/bin/python` and says so. There is no
way to run one of these against the wrong interpreter and only find out from a
`ModuleNotFoundError: No module named 'PIL'`.

`STORY` and `DIR` are overridable on every one of those targets.

The import stores each file with **the page's planned prompt and slots**, never
anything measured at import time, so a plate painted in a web UI and one painted
by `make_plates` are the same plate to the app. `.png`, `.jpg`, `.jpeg` and
`.webp` are all accepted and stored as one normalised PNG. Four refusals, each
before anything is written:

- a file whose name is not a page number of this story, or two files claiming
  the same page;
- a file that is not a readable image (every file is decoded before the first
  write, so a broken download cannot leave half a story imported);
- a page that **already has an approved plate** — importing would retire it, so
  the run stops and says so. `--replace --approve` is the way to say you mean it,
  and `--replace` without `--approve` is refused because replacing is exactly
  what writes a new template version;
- an empty directory.

### What a book is drawn onto

A book started on a template version renders **one edit call per page**: the
approved plate is the first reference image and the prompt tells the model to
keep it — the place, the props, the palette, the framing, the light — and paint
the named characters into the slots the plate reserved for them. That prompt is
in `[book.edit]` in `app/prompts.toml`, and what it leaves out matters as much
as what it says: no scene block, no props sheet, no whole-story context, because
on an edit an instruction to draw is an instruction to change the plate.

A book with **no** pinned version — made while the start gate is off, or saved
before template versions existed — composes each page from scratch exactly as
the app did before plates, so nothing regresses.

### What the edit pass costs

Worth being precise, because the obvious guess is wrong. **An edit call and a
from-scratch call cost the same per call** at the same output size and quality —
vendors bill the returned picture, not the instructions. The plate is not a
discount; it is a way to get a consistent book. What it does change is everything
around the call:

| Lever | What changes | Measurable? |
| --- | --- | --- |
| Prompt size | ~20,500 chars from scratch → **~4,200 chars** per page (a fifth). One book: ~410k → ~79k characters of input. | Yes — the `book.page` line logs `prompt_chars` for every page. |
| Reference images | The star's photograph is dropped where the plate already anchors the frame, so fewer images are uploaded per call. | Yes — the same line logs `refs`. |
| Retries | Plates pin the place, the palette, the props and their counts. Whole classes of failure that today force a **full-price re-roll** (wrong room, six beans, mixed media, the cow's coat) stop being possible. | By counting `outcome=failed` re-renders per book. |
| Quality tier | Composition is the hard part of a from-scratch page and the plate removes it, so the cheap tier should hold up more often — the bound is what T0 measures, not a guess. | One real run at `OPENAI_QUALITY=low` against the gate clinical trial in `docs/book-template-plan.md`. |
| Painting the plates | 21 calls per story, **once**, shared by every book of that story forever — and free if painted in a web UI. | Not a per-book cost at all. |

So the honest claim is not "editing is cheaper per call". It is: *the same call,
with a fifth of the prompt, fewer uploads, and far less to get wrong.* The retry
rate and the quality tier are where the real money is, and both are measured by
running a book and reading its log rather than argued here.

## Checking every picture, and trying again

A picture that reaches a child's book has been looked at twice. This is the
part of the pipeline that decides whether the page is *the page*, and it runs on
every one of the twenty-two images.

**Tier 1 is free.** `app/services/image_review.py` decodes the bytes and answers
mechanically: is it a picture at all, is it big enough, is it the right shape, is
it blank or a black square, and — for a plate edit — did the model hand the
plate back with nobody painted into it, or replace the background instead of
painting into it? It costs nothing, needs no key, and catches the failures that
actually happen.

**Tier 2 is a vision model.** `app/services/reviewer.py` sends the finished
picture and one page's checklist to a cheap model and requires one strict JSON
object back: a score, a sentence, and any faults from a closed set —
`wrong_moment`, `missing_character`, `extra_person`, `text_in_image`,
`face_obscured`, `frightening`, `wrong_counts`, `deformed`. A page passes on a
score of 70 or more with no faults. Anything else — an unknown fault name, a
missing key, prose instead of JSON — is a failed review, never a default pass.

The reviewer is never shown the child's photograph, name or About text. It is
told "the child, and the mother", because a reviewer shown "this is Emma's book"
starts being polite about Emma instead of checking the picture.

**Then the loop tries again.** A page that fails is drawn again, up to
`PAGE_ATTEMPTS` (3), with the previous attempt's faults named in the retry prompt
in plain words — aimed at what was wrong rather than re-rolled. Two rules keep it
from becoming a money pit:

* it stops the moment a review passes;
* the best attempt is the one kept (a tie goes to the **earliest**, since an
  equally good later attempt cost money and bought nothing).

A page that never passes is kept anyway and marked `best_effort`, with every
attempt and verdict stored. A book with one imperfect picture is worth more than
no book, and the flag means nobody can mistake it for a reviewed page.

`prompts.json` keeps **one entry per attempt**, holding the text that attempt
actually sent, so a redrawn page shows two different prompts side by side. It is
the record of what happened, not a summary of what won.

A reviewer failure is recorded, not raised: one unparseable answer or a reviewer
outage stamps `tier2_failed` on that image and the page continues. In production
a *missing key* is a start-up error instead, so a deployment cannot quietly ship
pictures nobody looked at. In development, test and mock mode the reviewer is
off, every verdict says `tiers_run: [1]`, and the log says so.

```
REVIEWER_API_KEY=      # empty: falls back to GEMINI_API_KEY
REVIEWER_MODEL=gemini-3.1-flash-lite
PAGE_ATTEMPTS=3        # 1-3; every attempt is a billable call
```

### Measuring it, rather than guessing

`make time-book` drives a running server through one real book and reports what
a parent actually waits for: the spread of per-page time, the time spent putting
the PDF together, how many pages needed a second try, and the prompt characters
sent. `make time-books` makes two books at once to see whether one waits for the
other.

```
IMAGE_PROVIDER=mock uvicorn app.main:create_app --factory --port 8000
make time-book URL=http://localhost:8000
```

Mock mode is free and its pictures are placeholders, so timings there are a
floor, not a forecast. Against a real key the same command measures the real
thing and prints an estimated spend; it refuses to guess a cost unless you pass
`--yes-i-am-using-a-paid-key`.

## Two steps, so you only pay for what you keep

The book is twenty-two calls, and the interface keeps the portrait apart on purpose:

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
                                             | 20 calls
                                             v
                               +-------------------------------+
                               | /api/book/start               |
                               | portrait -> cover -> 20 pages |
                               +-------------------------------+
```

Three things fall out of that split, and all three are cost or quality wins:

- **A likeness you dislike costs one call, not twenty-two.** Change the intensity,
  the style, the hair, the likeness strength, and retry — the settings panel
  stays live while you look at the portrait. Only an approved portrait is worth
  the twenty story pages.
- **Every page is built from the portrait you approved.** Not one you hoped
  would look better by page twenty.
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

One API call can return several alternatives ("Takes" under Adjust the look),
and you pick rather than re-roll — the studio shows them as a strip under the
comic, and the one you click is the one the book uses.
To be clear about the arithmetic: **this is not cheaper per call** — three
options is three times the cost of one, because you are billed per returned
image. It is cheaper per *kept* picture only if you would otherwise re-roll
repeatedly. At $0.00588 a call, three options is $0.018, so it is off by default
and worth turning on while settling a likeness.

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
make test-fast   # the inner loop: parallel, no coverage
make test        # the full run: serial, with coverage
make lint
make typecheck
# or all three with:
make check
```

`make test-fast` runs the same tests across every CPU with pytest-xdist and
skips coverage, which is the difference between roughly two minutes and eight
on a full run. `make check` (and CI) keeps the serial, covered run.

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
   `must_include` and `must_avoid` in its story's seed (`app/books/<story>.toml`).
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

### Reading the logs

Render captures stdout, so the dashboard has every line. Each request gets a
short id that is returned in the `X-Request-ID` header and appended to every
line it causes (`request=...`) — including the lines written later by the book's
background task, which inherits the id. One book's trail looks like this:

```
book.started   user=… book=… draft=… provider=… characters=- pages=20
book.page      user=… book=… page=7/20 key=the-giant's-kitchen mode=edit prompt_chars=4180 refs=3 bytes=… ms=… outcome=done
book.page      user=… book=… page=12/20 key=fee-fi-fo-fum mode=edit prompt_chars=4195 refs=3 bytes=… ms=… outcome=failed error=ProviderError
book.review    user=… book=… page=12 attempt=1 tier=1 faults=plate_unchanged score=10 outcome=failed
book.page      user=… book=… page=12/20 key=fee-fi-fo-fum mode=edit prompt_chars=4210 refs=3 bytes=… ms=… outcome=done
book.review    user=… book=… page=12 attempt=2 tier=2 faults=text_in_image score=62 outcome=failed note='a sign on the door'
book.page      user=… book=… page=12 attempts=3 outcome=best_effort faults=deformed
book.complete  user=… book=… pages=20 pdf_bytes=… ms=… outcome=complete
book.failure   user=… book=… pages_done=11 ms=… outcome=failed message=…
```

`mode` is `edit` when the page was painted into an approved plate and
`compose` when it was drawn from scratch, and `prompt_chars`/`refs` are this
page's cost record — which is what makes the question "is editing cheaper"
answerable from a real run instead of an estimate. `book.review` lines are the
two tiers: `tier=1` is the free mechanical check, `tier=2` the vision model, and
`outcome=unavailable` means the reviewer could not be reached (the page was kept
and stamped `tier2_failed`). `attempt=` on each line is which try it was, and
`outcome=best_effort` marks the page that used all three and was kept anyway.

Sign-in has its own lines (`auth.google_login_ok sub=… email=s***@…`). A
logging filter scrubs secrets before any handler formats a record: provider
keys, the Google client secret, `SESSION_SECRET`, the Stripe secret key, the
whole `DATABASE_URL` and its password fragment. Image bytes are never written — only their length — and
the visitor's About free text never reaches a logger.

**There is one store, and it is a database.** Set `DATABASE_URL` (a Neon
free-tier Postgres works well) and books, their pages, their PDFs, the people
who made them and the story templates live in Postgres: a completed book
survives a restart or redeploy and stays answerable to the account that made
it. Leave it empty and the same schema runs in one SQLite file under
`STORAGE_DIR` (`storage/app.sqlite3`), created and migrated when the app boots
— local development and CI need no server, and no test and no request ever
takes a second storage path.

Nothing needs `make migrate` to get started: the app migrates its own database
at boot. The command still exists for applying a deploy's migrations ahead of
time (see `to-do.md` for the first-deploy runbook), and with no `DATABASE_URL`
it targets the local SQLite file.

## Paying for a book (optional)

Set `STRIPE_SECRET_KEY` to a **sandbox** key (`sk_test_...`) and every
signed-in account gets a wallet at `/wallet`. Leave it empty and books are free
and the wallet page does not exist — the rest of the app is unchanged.

- **Top up** in whole dollars ($5, $10 or $20 — a closed set; no free-text
  amount ever reaches Stripe). The visitor pays on Stripe's hosted Checkout
  page, so card details never touch this app. On return, the session id is read
  back from Stripe with the secret key; only a **paid** session in that closed
  set is credited, once, keyed by session id.
- **One picture book costs 99¢.** The start endpoint refuses a book the wallet
  cannot cover (HTTP 402 with the price and the balance); the fee is actually
  taken when the finished PDF is written, so a book that never finishes is
  never charged. The charge is idempotent per book id, so edits that rebuild
  the PDF do not charge twice.
- **Refunds go back to the card.** Everything refundable is requested at once:
  the amount leaves the wallet immediately (so it cannot be requested twice),
  and is then sent through Stripe against the funding payments oldest first. A
  failed Stripe refund puts the money back in the wallet.
- A ledger, not a stored balance: the balance is the sum of signed
  transactions, so the history and the balance cannot disagree. In database
  mode the ledger lives in `wallet_transactions` / `wallet_refunds`
  (migration `0004_wallet`); otherwise it is one JSON file per account under
  `<STORAGE_DIR>/wallets`.

Stripe's own error text goes to the operator's log; the page only ever shows a
sentence this app wrote. The secret key is armed with the same log redaction as
the provider and OAuth keys.

### What a book costs to make

Every billable model call — the portrait, the cover, each page, every redraw
after a failed check, every quality check, and template plates — is written to
`api_calls` (migration `0008_api_calls`) with its provider, model, book, page,
attempt and price. The provider and reviewer are wrapped once, in
`build_container`, so no call site can skip the ledger
(`app/services/costs.py`). When a book starts it adopts the portrait drafts it
was made from, so their cost is the book's.

`/wallet` shows the **average API spend per finished book next to the 99¢
price**, a per-category breakdown (how many redraws an average book needs, and
what they cost), and on each book charge what that book cost to make. Spend no
finished book carries — rejected portraits, books that stopped early, plates —
is shown separately rather than hidden.

Prices follow the configured provider: **Agnes is free** (and so is the mock),
**OpenAI** is priced from its published per-image table for the model and
quality. `IMAGE_CALL_COST_USD` overrides the image price, and
`REVIEW_CALL_COST_USD` prices review calls. A call with no known price is
counted as *unpriced* and the page says how many, rather than calling it free.
A failed call is logged at no cost.

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
| `REVIEWER_API_KEY` | *(empty)* | The vision reviewer. Empty falls back to `GEMINI_API_KEY`; in production with neither set the app refuses to start. |
| `REVIEWER_MODEL` | `gemini-3.1-flash-lite` | The vision model every finished picture is checked with. Text and image in, strict JSON out. |
| `REVIEWER_TIMEOUT_SECONDS` | `90` | One review's budget. A timed-out review fails that image, not the book. |
| `PAGE_ATTEMPTS` | `3` | Billable tries per page before the best is kept (1–3). A page that never passes is kept and marked best-effort. |
| `APP_ENV` | `development` | `production` raises the log level |
| `GOOGLE_CLIENT_ID` | *(empty)* | With `GOOGLE_CLIENT_SECRET`, makes Google sign-in the only way in. See `docs/google-oauth-setup.md`. |
| `GOOGLE_CLIENT_SECRET` | *(empty)* | The OAuth client secret; lives only in the environment. |
| `GOOGLE_REDIRECT_URI` | *(empty)* | The exact redirect URI registered with Google; empty means the app derives it from the request (local development only). |
| `SESSION_SECRET` | *(empty)* | Signs the session cookie. Unset, every restart signs everyone out. Not the password. |
| `APP_PASSWORD` | *(empty)* | The local-dev fallback: shared password required when no Google client is configured. Both unset means the app is open. |
| `APP_SESSION_MAX_AGE_DAYS` | `7` | How long one successful sign-in stays valid. Rotating `SESSION_SECRET` invalidates every session. |
| `APP_LOGIN_MAX_ATTEMPTS` | `8` | Wrong passwords per client before a 15-minute lockout. |
| `OPERATOR_SUB` | *(empty)* | The Google `sub` of the one person who may review plates at `/admin`. Empty disables the admin screens. |
| `MAX_UPLOAD_MB` | `10` | Enforced before decoding |
| `GENERATION_TIMEOUT_SECONDS` | `600` | One budget per request: queue + model call. Also applied to the provider's HTTP client. |
| `MAX_CONCURRENT_GENERATIONS` | `2` | Admission control per process. Beyond this, requests queue and then get 429. |
| `MAX_GENERATIONS_PER_MINUTE` | `30` | Per-client abuse brake for billable requests; Render's blueprint uses `12`. |
| `STORAGE_DIR` | `./storage` | Created on startup |
| `DRAFT_RETENTION_HOURS` | `24` | How long an uploaded photo and the portraits made from it are kept. Swept on boot and hourly. |
| `BOOK_RETENTION_DAYS` | `7` | How long a finished (or failed) book is kept after it started; then its portrait, pages and PDF are deleted. |
| `DATABASE_URL` | *(empty)* | Postgres URL (Neon pooled endpoint). Set = the database is Postgres and survives redeploys; empty = a local SQLite file under `STORAGE_DIR`, created and migrated on boot. Both run the same schema and the same code. |
| `STRIPE_SECRET_KEY` | *(empty)* | Stripe **sandbox** secret key (`sk_test_...`). Set = wallets, top-ups, a 99¢ per-book fee and card refunds; empty = free books, no wallet page. |

`MAX_UPLOAD_MB` and `STORAGE_DIR` map to `MAX_UPLOAD_MB` and `STORAGE_DIR`.

**Retention.** Nothing is kept forever. An uploaded photograph and the cartoon
portraits made from it are deleted after `DRAFT_RETENTION_HOURS` (a day by
default — the review flow only needs minutes); a finished book, with its
portrait, twenty pages and PDF, is deleted `BOOK_RETENTION_DAYS` after it
started (a week by default). A book still pending or rendering is never expired.
The sweep runs once at boot and then hourly, and works the same in both
storage modes, so a deployment's disk and a Neon database both stay bounded.
Retention is per-deployment, not per-visitor: there is no "delete my book"
control in the UI, and lowering the setting is how an operator shortens it.
