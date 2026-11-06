# The new face of the book app — project plan

**Status:** plan agreed, no code written. This document is the deliverable of the
design session; building starts at Phase 0.

**Scope:** the visitor-facing UI — the shelf, the character studio, the About
page, the book as it is drawn, and the chrome around them — plus the two pieces
of plumbing the new shape needs: a second story (*Little Red Riding Hood*) and an
About page that can print a photograph. The PDF design, the prompt content of
Jack, the wallet mechanics and the auth model stay as they are.

---

## 1. Why it feels like a form

Everything the app can do is on one page, all the time. `book.html` carries about
forty controls: a photo picker and a camera, a name, a gender select, three
character slots each with its own file input, camera button and cartoon button,
four About fields, then a `<details>` block with thirteen more settings, and a
submit button that says "Make the cartoon portrait". The review stage and the
book stage are two hidden `<section>`s stacked *below* that form, so the thing you
came for appears underneath the thing you were filling in. The library is a fixed
280px sidebar bolted to the left of everything, and the finished book is a PDF in
an iframe at the bottom.

The result is honest and complete, and it reads as a form. Nothing on the page
looks like a book, nothing invites a photo, and the one delightful artefact —
the cartoon of a child's face — is a preview thumbnail in a file-upload widget.

The redesign keeps every capability and changes the shape of the journey:
**choose a book → give it faces → say who it's for → watch it being drawn → keep
it.** Each step is a screen with one job.

## 2. The shape of the new experience

```
  ┌────────────┐    ┌──────────────────┐    ┌────────────┐    ┌──────────────┐
  │   SHELF    │───▶│      STUDIO      │───▶│  ABOUT ME  │───▶│   THE BOOK   │
  │ two covers │───▶│ one section per  │───▶│ name, age, │───▶│ drawn live,  │
  │  faceless  │───▶│ character, comic │───▶│ title, the │───▶│  flippable,  │
  │            │───▶│ beside each face │───▶│ photo pick │───▶│ downloadable │
  └────────────┘    └──────────────────┘    └────────────┘    └──────────────┘
        ▲                                                                    │
        └────────────────  MY BOOKS drawer (top right)  ◀────────────────────┘
```

Four routes, one flow, and one drawer that can reopen any earlier book from
anywhere:

| Route | Screen | Auth |
| --- | --- | --- |
| `GET /` | The shelf | open — shows the sign-in banner when signed out |
| `GET /stories/{story}/studio` | The studio | session required |
| `GET /stories/{story}/about` | The About page | session required |
| `GET /books/{book_id}` | The book (drawing or finished) | owner required |
| `GET /wallet` | Wallet | unchanged |

Decisions taken in this session, recorded so they are not re-litigated:

1. The second book is **Little Red Riding Hood**.
2. A book needs **the star's face and only the star's**: the mother, grandmother
   and woodcutter are optional and drawn from the story when left empty.
3. Under each comic: the **alternative takes, the "not quite right" chips and
   Try again** — every setting stays behind the character's **Advanced** button.
4. The **About me page** is its own screen, after the studio, with name, age,
   title, description, a choice of **real photo / cartoon / both**, and the
   **page shape** — the shape sits next to the Generate button.
5. Character sections in the studio are **collapsible**.
6. The book is not crammed onto one screen: it gets its own stage, a progress map
   at the bottom, and the finished state replaces the progress line with the two
   downloads.

---

## 3. Screen 1 — The shelf

```
┌───────────────────────────────────────────────────────────────────────────┐
│  ✦ Storybook Studio                              [ My books ▾ ]  (o 12.00)│
├───────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│      Make a picture book starring someone you love.                       │
│      One photo. Twenty pages. A story they keep.                          │
│                                                                           │
│   ┌──────────────────────┐      ┌──────────────────────┐                  │
│   │                      │      │                      │                  │
│   │      [cover art]     │      │      [cover art]     │                  │
│   │         ╭───╮        │      │         ╭───╮        │                  │
│   │         │ ◯ │  ← the │      │         │ ◯ │        │                  │
│   │         ╰───╯    face│      │         ╰───╯        │                  │
│   │                      │      │                      │                  │
│   │  Jack and the        │      │  Little Red          │                  │
│   │  Beanstalk           │      │  Riding Hood         │                  │
│   │  20 pages · ~5 min   │      │  20 pages · ~5 min   │                  │
│   │  ┌────────────────┐  │      │  ┌────────────────┐  │                  │
│   │  │  Start this ▸  │  │      │  │  Start this ▸  │  │                  │
│   │  └────────────────┘  │      │  └────────────────┘  │                  │
│   └──────────────────────┘      └──────────────────────┘                  │
│                                                                           │
└───────────────────────────────────────────────────────────────────────────┘
```

- **Two cards, each a single big button.** The whole card is the target: no
  separate "learn more" competing with the title.
- **The cover has a face-shaped hole.** Every cover art is drawn (or, until real
  art exists, hand-written as SVG in `app/assets/covers/`) with the story's hero
  and a plain white circle where the face is, ringed with a dashed marigold
  line. It is the app's core promise in one image: *your face goes here*. The
  shelf adds a one-time hint under the first card: "We put your face where the
  circle is."
- **State on the card**, never a separate page: `Start this book` when fresh,
  `Keep going` when a studio draft exists in this browser, `Being drawn…` when a
  book of that story is already rendering, and `Coming soon` (card dimmed, not
  clickable) when a story has no catalogue yet.
- **A live cover, not a stock one**: once a person has a finished book for a
  story, their card shows *their* cover with *their* cartoon on it. Until then it
  shows the placeholder. One card, two states, no extra screen.
- **Mobile:** cards stack full-width; the cover is the first thing above the
  fold; the tagline shrinks to one line.

### Signed out

The shelf is visible immediately, with one friendly banner instead of a login
page — the site explains itself before it asks for anything:

```
┌───────────────────────────────────────────────────────────────────────┐
│  ✦ Storybook Studio                                                   │
├───────────────────────────────────────────────────────────────────────┤
│  You bring a photo of someone you love. We draw them into a           │
│  20-page picture book — the cover, the whole story, an About page     │
│  about them, all in one PDF you keep.                                 │
│                                                                       │
│  Sign in with Google so your books are yours and survive a restart.   │
│  ┌───────────────────────┐                                            │
│  │  Continue with Google │                                            │
│  └───────────────────────┘                                            │
└───────────────────────────────────────────────────────────────────────┘
```

In password mode the same banner carries a single password field and "Enter",
and the separate `login.html` card disappears. The `/login` **route** stays —
OAuth failures and the lockout message redirect there — but it renders this same
shelf in signed-out mode with the message in the banner, so there is one
signed-out look and the redirects keep working. The covers stay visible in both
cases: they contain no visitor data.

---

## 4. Screen 2 — The studio

One vertical section per character, in story order, the star first. Every
section is independently collapsible, independently filled, and independently
"comic'd" — nothing in one section blocks another.

```
┌────────────────────────────────────────────────────────────────────────┐
│  ✦ Jack and the Beanstalk        [ My books ▾ ]      (o 12.00)         │
├────────────────────────────────────────────────────────────────────────┤
│  ← Choose another book                                                 │
│                                                                        │
│  Who's in the story?                                                   │
│  Give a face to whoever you like. The star goes on every page;         │
│  the rest appear only on the pages they belong to.                     │
│                                                                        │
  ┌─ ★ THE STAR ────────────────────────────────────── ready ────── ▾ ─┐ │
  │  This is the face on every page.                                   │ │
  │                                                                    │ │
  │  [  Take a photo  ]   [  Upload a photo  ]   [  Advanced  ]        │ │
  │                                                                    │ │
  │  ┌────────────┐   ┌────────────┐    [  Make the comic  ]           │ │
  │  │   photo    │   │  cartoon   │    Not quite right?               │ │
  │  └────────────┘   └────────────┘    ☐ hair   ☐ age                 │ │
  │                                     ☐ likeness …                   │ │
  └────────────────────────────────────────────────────────────────────┘ │
│                                                                        │
  ┌─ The mother ─────────────────────────────────── optional ────── ▸ ─┐ │
  │  No photo — drawn from the story                                   │ │
  └────────────────────────────────────────────────────────────────────┘ │
│                                                                        │
  ┌─ The old man ────────────────────────────────── optional ────── ▸ ─┐ │
  │  No photo — drawn from the story                                   │ │
  └────────────────────────────────────────────────────────────────────┘ │
│                                                                        │
  ┌─ The giant ──────────────────────────────────── optional ────── ▸ ─┐ │
  │  No photo — drawn from the story                                   │ │
  └────────────────────────────────────────────────────────────────────┘ │
│                                                                        │
│  1 portrait ready · 3 optional characters skipped                      │
│                                                ┌──────────────────────┐│
│                                                │  Next: about me  →   ││
│                                                └──────────────────────┘│
└────────────────────────────────────────────────────────────────────────┘
```

### A section, step by step

1. **Two big friendly buttons** — `Take a photo` (opens the existing in-page
   camera pane, `getUserMedia`, mirrored preview) and `Upload a photo`. Two
   targets, the same weight, because on a laptop you upload and on a phone you
   shoot. Once a photo is chosen they become `Change photo`.
2. **Your photo appears at once**, in a rounded frame, with the client-side
   downscale the app already does (`downscale.js`). The server is not called
   until you ask for a comic.
3. **`Make the comic`** — one call to the existing `POST /api/cartoonify` with
   this section's `character=`. The comic appears *beside* the photo, in the same
   row, framed like a printed plate. Alternatives come back as a strip of takes
   under it; clicking one makes it the chosen comic.
4. **`Not quite right?`** — the existing closed-set chips (hair, age, likeness,
   …) plus `Try again with these fixes`. Exactly the chips the backend already
   validates as `RejectionReason`; no free text, still.
5. **`⚙ Advanced`** — opens a modal over a dimmed page: every setting we have
   today **except** "make it in one go" (that flag dies with this redesign — the
   flow is staged now, so `one_step` disappears from the model, the form and the
   tests). Contents: art style, cartoon intensity, age, skin tone, hair style,
   hair colour, outfit, expression, likeness to the real you, portrait
   background, keep glasses, keep facial hair, number of alternatives. Saving
   returns to the section with a quiet "style: soft storybook · 3 alternatives"
   summary line so nobody has to open it twice.
6. **Collapse** — the section folds to one row: name, a 44px thumbnail of the
   photo or comic, and a state word (`ready`, `no photo`, `expired`). The star's
   section cannot be collapsed to *nothing*; it can be collapsed, but the state
   word is always there.

### The rules the studio keeps

- **One photo per section, never two.** Replacing a photo replaces the comic;
  the old draft stays server-side until it expires.
- **The star is required, the rest are not.** `Next: about me` is enabled the
  moment the star has a chosen comic. The other three sections, left empty, say
  "No photo — drawn from the story" and the book uses the prose description.
- **Nothing is lost on a refresh.** Per-story draft state is kept in
  `localStorage` (`studio:jack_and_the_beanstalk`) — chosen draft ids, per-section
  settings, the collapsed/expanded state. Drafts live 24 hours server-side; if a
  draft has expired, that section says "That photo expired — add it again" and
  nothing else resets.
- **Cost is stated once, plainly.** Above the buttons: "Each comic is one
  picture. You'll usually need one." No dollar amounts per image; the book fee is
  the wallet's business.

### Advanced modal

```
        ┌─────────────────────────────────────────────────┐
        │  The star's look                        ✕       │
        │                                                 │
        │  Style          [ Soft storybook        ▾ ]     │
        │  Intensity      [ ▁▂▃▄▅▆▇█ ]  8                 │
        │  Age            [ Same as the photo     ▾ ]     │
        │  Skin tone      [ As in the photo       ▾ ]     │
        │  Hair           [ As in the photo       ▾ ]     │
        │  Outfit         [ Storybook tunic       ▾ ]     │
        │  Likeness       [ Strict — most like you ▾ ]    │
        │  Background     [ Plain                 ▾ ]     │
        │  ☑ Keep glasses      ☑ Keep facial hair         │
        │  Alternatives   [ 1 ▾ ]   one picture each      │
        │                                                 │
        │           [ Cancel ]      [ Save and close ]    │
        └─────────────────────────────────────────────────┘
           (the studio behind it is dimmed and inert)
```

Behaviour: focus trapped, `Esc` closes, the page behind does not scroll, and the
modal is a real `<dialog>`-style element with `role="dialog"` and
`aria-modal="true"`. Cancel discards; Save writes the settings into the section
and into the localStorage draft.

---

## 5. Screen 3 — About me

The last page of the book is about the person, so it gets its own screen — and
the one book-wide setting lives here, next to the button it affects.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  ← Back to the faces                                                     │
│                                                                          │
│  The last page is about you                                              │
│  This is the page the book closes on. Say anything you like.             │
│                                                                          │
│   Name          [ Mia                        ]                           │
│   Age           [ 7        ]    What do they want to be?                 │
│                                 [ an astronaut            ]              │
│   Anything else?                                                         │
│   [ Mia loves rockets, her dog Bo, and anything with                     ]
│   [ sprinkles on it.                                                     ]
│                                                                          │
│   On that page, show:                                                    │
│   ┌──────────────┐ ┌──────────────┐ ┌──────────────┐                     │
│   │  ● cartoon   │ │  ○ the photo │ │  ○ both      │                     │
│   └──────────────┘ └──────────────┘ └──────────────┘                     │
│                                                                          │
│   Page shape:  ( Landscape )  ( Square )  ( Portrait )                   │
│                                                                          │
│   Your book is one fee of 99¢, taken when the PDF is written.            │
│                                        ┌───────────────────────────┐     │
│                                        │       Make the book       │     │
│                                        └───────────────────────────┘     │
└──────────────────────────────────────────────────────────────────────────┘
```

- **Every field is optional.** A blank name becomes Jack/Jane (or Red) by the
  same rule the backend already applies; blank age, title and description simply
  add nothing. The screen never refuses to advance because of a field.
- **The image choice is new.** `cartoon` (default, today's behaviour), `photo`,
  or `both` — drawn on the About page of the PDF, framed. See §11.3.
- **Page shape** is one choice for all twenty pages, and it is here because this
  is the screen immediately before Generate. It is sent with the start request
  and overrides the star portrait's own aspect setting.
- The Generate button carries the fee sentence beside it, never a hidden charge.
- A quiet summary of what was locked in: "Star: 1 · Mother: 1 · Old man: 0 ·
  Giant: 0".

---

## 6. Screen 4 — The book

```
┌───────────────────────────────────────────────────────────────────────┐
│  ✦ Mia and the Beanstalk          [ My books ▾ ]        ⟨◯ 11.01⟩     │
├───────────────────────────────────────────────────────────────────────┤
│                                                                       │
│                       ┌───────────────────────┐                       │
│                       │                       │                       │
│  ┌────────┐           │                       │           ┌────────┐  │
│  │   ←    │           │      page 7 of 20     │           │  →     │  │
│  │ back   │           │      (the art, the    │           │ next   │  │
│  └────────┘           │       story line)     │           └────────┘  │
│                       │                       │                       │
│                       └───────────────────────┘                       │
│                           click the page to turn it                   │
│                                                                       │
│  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░  12 of 20 pages drawn                         │
│  ┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐┌─┐         │
│  │▓││▓││▓││▓││▓││▓││▓││▓││▓││▓││▓││▓││░││░││░││░││░││░││░││░│         │
│  └─┘└─┘└─┘└─┘└─┘└─┘└─┘└─┘└─┘└─┘└─┘└─┘└─┘└─┘└─┘└─┘└─┘└─┘└─┘└─┘         │
│  1  2  3  4  5  6  7  8  9  10 11 12 13 14 15 16 17 18 19 20          │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
```

- **The book slot at the top is the artefact itself**, not a viewport for a PDF.
  One page at a time: cover, then pages 1–19, then the About page. Clicking the
  page turns forward; the `←` button on the left turns back. Arrow keys work
  too. The turn is a 240ms paper flip, and with `prefers-reduced-motion` it is a
  cut.
- **While it draws**, pages appear in the slot the moment they land (out of
  order, because they are drawn in parallel — the slot always shows the page you
  are on, and a page that is not ready says "being drawn…" with the pencil
  animation). The map at the bottom shows all twenty positions: waiting (dashed),
  drawing (marigold, pulsing), done (green, thumbnail), failed (berry, retry).
- **The progress bar is the map's caption**: "12 of 20 pages drawn". When the
  PDF is assembled the whole progress block is **replaced** — not overlaid — by:

```
  ✨ Your book is ready.
  ┌──────────────────────────────┐  ┌───────────────────────────────────┐
  │  Download the book (PDF)     │  │  Keep the photos and comics (zip) │
  └──────────────────────────────┘  └───────────────────────────────────┘
```

- **It never resets.** The server's status document is the only source of truth:
  a reload re-polls, restores the page you were on (URL `#page-7`), and the map
  is rebuilt from the stored page list. Reopening from the drawer lands on the
  last page of the book — the last finished page while it is drawing, the About
  page when it is done.
- **A page that failed** does not stop the book: its tile shows `retry`, the book
  says "19 of 20 pages — one didn't make it", and `Finish this book` picks up only
  what is missing (the existing `action=retry`, which is already
  `only_missing`).
- **After it is finished** the same stage stays usable: the map becomes an index
  (click a tile to jump), and an "Edit this book" strip offers the existing
  powers — redraw one page (chips), replace a face (reopens the studio for that
  book, one section), edit the About page, delete the book — each with its cost
  in one plain sentence.

---

## 7. Global chrome

**My books (top right, always).** A pill button that expands *down* into a sheet:
the person's books, newest first — cover thumbnail, title, "finished 2 Nov · 20
pages" or "being drawn · 12 of 20", and a "still drawing" dot. Clicking one opens
that book at its last page. It is a `<details>`-style panel, not a permanent
sidebar: the old always-on library column is gone. On phones it is a full-width
sheet.

**The wallet** stays a small pill in the header (`◯ 12.00`), and only when money
is on. It is a link, not a panel, and it never competes with the book.

**Header behaviour per screen:** shelf shows the wordmark and the drawer; studio
shows the story title and a back link to the shelf; the book screen shows the
book's own title. The sign-in banner appears on the shelf only.

**One footer line**, everywhere: "Books are kept for 7 days." Gentle, honest, and
it stops "where did my book go?" from being a mystery.

---

## 8. Look and feel

**Direction: "paper and crayon".** Warm paper, ink-navy type, one green and one
marigold doing most of the colour, and two small accent hues used only for
state. Colourful because the *art* is colourful — the chrome stays quiet so the
child's face is the loudest thing on the screen.

| Token | Value | Used for |
| --- | --- | --- |
| `--paper` | `#FFF9F0` | page background, reader surround |
| `--card` | `#FFFFFF` | cards, sections, modal |
| `--ink` | `#23212C` | text |
| `--ink-soft` | `#6B6577` | help text, meta |
| `--line` | `#EADFCC` | 1–2px borders, dashed slots |
| `--bean` | `#3C7A4E` | primary buttons, done state |
| `--bean-dark` | `#2A5B39` | hover, headings |
| `--marigold` | `#F2A63B` | the face ring, drawing state, highlights |
| `--berry` | `#D3566B` | failure, delete, "not quite right" |
| `--sky` | `#4F8FC0` | links, secondary accents |

- **Type:** Literata (already vendored in `app/assets/fonts/`, OFL) for headings
  and body — a book face in a book app, no new dependency, no CDN. Sizes:
  32/24/19/16/14, headings at 700, body 400, UI labels 600. Line height 1.5.
- **Shape:** cards 18px radius with a 2px `--line` border and a sticker shadow
  (`0 2px 0 var(--line), 0 10px 24px rgba(35,33,44,.06)`); buttons are pills;
  inputs 12px. Nothing is a hard rectangle, nothing floats without a border.
- **The face circle** is the one repeated graphic idea: dashed marigold ring on
  a white disc, on covers, on empty character sections, on the "no photo" rows.
  It teaches the mechanic without a word of instruction.
- **Motion:** 180–240ms, `cubic-bezier(.2,.7,.3,1)`, and only for meaning — a page
  turns, a comic lands, a tile fills, the modal fades. Everything collapses to a
  cut under `prefers-reduced-motion: reduce`.
- **Accessibility:** 4.5:1 contrast for text, visible 2px focus ring in `--sky`,
  real `<button>`s, states never carried by colour alone (icon or word too),
  modal focus trap, `aria-live` on progress, keyboard-flippable book.
- **Mobile first for the studio**, because the camera is the natural entry there:
  one section per screen-width, buttons 48px tall, the Next bar sticky at the
  bottom, the drawer a sheet.

---

## 9. Copy deck

| Where | Words |
| --- | --- |
| Shelf headline | Make a picture book starring someone you love. |
| Shelf sub | One photo. Twenty pages. A story they keep. |
| Card action | Start this book / Keep going / Being drawn… / Coming soon |
| Face hint | We put your face where the circle is. |
| Signed-out banner | You bring a photo of someone you love. We draw them into a 20-page picture book… Sign in with Google so your books are yours. |
| Studio headline | Who's in the story? |
| Studio sub | Give a face to whoever you like. The star goes on every page; the rest appear only on the pages they belong to. |
| Star label | ★ The star — this is the face on every page. |
| Buttons | Take a photo · Upload a photo · Change photo |
| Comic action | Make the comic · Drawing… |
| Review | Happy with it? · Not quite right? · Try again with these fixes |
| Empty optional section | No photo — drawn from the story |
| Studio footer | 1 portrait ready · 3 optional characters skipped |
| Studio next | Next: about me → |
| About headline | The last page is about you |
| About sub | This is the page the book closes on. Say anything you like. |
| About labels | Name · Age · What do they want to be? · Anything else? · On that page, show: cartoon / the photo / both |
| Page shape | Page shape: Landscape · Square · Portrait |
| Generate | Make the book ✨ |
| Fee line | Your book is one fee of 99¢, taken when the PDF is written. |
| Drawing | Your book is being drawn. · 12 of 20 pages drawn |
| Queue pause | The pencil is resting for a moment — the pictures come one a minute. |
| Finished | ✨ Your book is ready. |
| Downloads | Download the book (PDF) · Keep the photos and comics (zip) |
| Drawer | My books · finished 2 Nov · 20 pages · being drawn · 12 of 20 |
| Footer | Books are kept for 7 days. |
| Errors | That photo is too big (10MB). · That portrait expired — add the photo again. · That page didn't make it. Try it once more. |

---

## 10. Rules the UI must keep

1. **Progress is never invented.** Every number on screen comes from
   `GET /api/book/{id}`; nothing is counted in the browser. Reload, second tab
   and a restored drawer entry all show the same truth.
2. **Nothing billable is one click away by accident.** The comic button is
   explicit; the book fee is stated before Generate; Delete is behind a
   confirmation with the book's title in it.
3. **The queue is explained, not hidden.** Book pages *wait* for the per-minute
   allowance (`_await_page_charge` in `book_runner.py`) instead of failing, so
   the UI must say "resting for a moment" rather than look frozen. Comic calls
   can genuinely 429; that message offers "try again in a minute".
4. **Faces are private.** Every image route stays `no-store`; the shelf never
   renders another person's cover; the drawer is empty when signed out.
5. **Expiry is visible.** The 7-day window appears in the footer and next to a
   book older than a couple of days, not just in the docs.

---

## 11. Technical plan

### 11.1 A catalogue of stories (two books)

Today the story is hard-wired: `app/prompts.toml` holds `[book]`, `[book.*]` and
nineteen `[[book_pages]]`, and `app/models/book.py` builds `BOOK_PAGES` from it at
import. The second book needs one dimension, not a fork.

- **New module** `app/services/catalogue.py`: `stories()`, `story(key)`,
  `pages_for(key)`, `cast_for(key)`. One cached load per key, same
  fail-at-boot discipline as today (a page naming an unknown cast member raises
  on import).
- **New data files** `app/books/jack_and_the_beanstalk.toml` and
  `app/books/little_red_riding_hood.toml`, each holding exactly what is
  story-specific: title and title template (`"{name} and the Beanstalk"`,
  `"{name} and the Big Bad Wolf"` — to confirm), world, About copy, cast, cast
  labels, photoable flags, and the page catalogue. `app/prompts.toml` keeps only
  the shared vocabulary: styles, ages, skin tones, outfits, hair, expressions,
  likeness, backgrounds, intensity, rejections, assembly.
- **`story_key` everywhere a book exists**: `BookRecord`, `BookSummary`, the
  start request, the status document, the PDF builder (`build_book_pdf` takes the
  story's About copy), and the runner (which page list to draw). The default
  star-name pair and the title template move into the story file with the rest
  (Jack/Jane, `{name} and the Beanstalk`; Red, to confirm).
- **Database:** one nullable-then-defaulted column, `books.story_key`, default
  `'jack_and_beanstalk'`, with an Alembic migration; existing rows keep working.
- **Backwards compatibility during the move:** `BOOK_PAGES` stays as a thin alias
  for `pages_for("jack_and_beanstalk")` until every caller is converted, then is
  deleted. Every prompt builder takes `story_key` with a default so the diff
  stays reviewable.

**The second story's content** (proposed; to confirm before Phase 1 ships):

| Cast | Photoable |
| --- | --- |
| Red (the star) | yes — the required one |
| Mother | yes |
| Grandmother | yes |
| The woodcutter | yes |
| The wolf | **no** — pinned in prose, exactly like Milky-White |

Nineteen story pages plus the About page, same page shape and cost as Jack:
the errand and the basket → the wood → the wolf in the path → the cottage →
"what big ears you have" → the woodcutter → the escape → home. Beats to be
written in `little_red_riding_hood.toml` with the same `Scene` contract, so every
existing prompt helper applies unchanged.

### 11.2 Settings, split by what they belong to

The current form mixes two scopes, and the new screens separate them — which is
also a small backend change, because a book's page shape is currently inherited
from the star portrait's draft.

| Setting | Scope | Lives in |
| --- | --- | --- |
| cartoon style, intensity, apparent age, skin tone, hair style/colour, outfit, expression, likeness, background, keep glasses, keep facial hair, alternatives | **per character** | that character's Advanced modal, sent on its `cartoonify` call |
| page shape (aspect ratio) | **per book** | the About me screen, sent on `book/start` |
| About page image (cartoon / photo / both) | **per book** | the About me screen, stored on the book |
| name, age, title, description | **per book** | the About me screen |
| "make it in one go" | **removed** | — |

`POST /api/book/start` gains `aspect_ratio` and `about_photo` form fields; both
validate through the existing `CartoonSettings`/new enum, and the book's stored
settings are the star's with the chosen shape substituted.

### 11.3 The About page can print a photograph

`draw_about_page(pdf, about, portrait)` already frames the cartoon. Add
`about_photo: AboutPhotoChoice = cartoon | photo | both` to `AboutMe`, pass the
stored source photo into the builder, and lay out:

- one image: today's centred frame;
- both: two frames side by side, the photo in the cream mat and the cartoon in
  the gold frame, each at half the current max side, with the sentence block
  re-fitting below them (`_fit_text` already measures the available height).

Default `cartoon` means every existing book renders exactly as it does now; the
field is defaulted, so records written before this change still validate.

### 11.4 API delta

| Endpoint | Change |
| --- | --- |
| `GET /api/stories` | **new** — the shelf: key, title, cover url, cast, page count, blurb |
| `GET /api/stories/{key}` | **new** — characters, page titles, defaults (replaces `/api/book/pages` and `/api/book/characters`) |
| `POST /api/cartoonify` | unchanged — one call per character section already |
| `POST /api/book/start` | **+** `story_key`, `aspect_ratio`, `about_photo`; reuse-by-fingerprint gains the story |
| `GET /api/book/{id}` | **+** `story_key`, and `phase`/`page_count` (already in `extra`) for the map |
| `POST /api/book/{id}/edit` | unchanged — `page`, `character`, `cover`, `about`, `retry`, `delete` all stay |
| `GET /api/books` | **+** `story_key`, `last_page` (highest done), so the drawer can deep-link |
| `GET /book-media/{id}/sources.zip` | **new** — §11.5 |
| `GET /book-media/{id}/…` | unchanged |
| old `/api/book/pages`, `/api/book/characters` | removed with the old UI |

### 11.5 "Keep the photos and comics"

`GET /book-media/{id}/sources.zip`, built with stdlib `zipfile` in a worker
thread, `no-store`, owner-checked through the same `_ensure_book` path:

```
README.txt                 what this is, in one paragraph
star/photo.jpg             the original you uploaded
star/comic.png             the cartoon
mother/photo.jpg           only the characters that had a photo
mother/comic.png
pages/page-01.png … page-19.png
cover.png
about.txt                  the About page as plain text
prompts.json               the existing record of what was sent
```

The files already exist per book (`read_portrait`, `read_source_photo`,
`read_page`, `read_cover`, `load_characters`, `read_prompts_json`), so this is
assembly, not new storage. Character photos are read from the stored
`CharacterReference`, not from the 24-hour drafts.

### 11.6 Templates, styles, scripts

| File | Action |
| --- | --- |
| `app/templates/shelf.html` | **new** — wordmark, tagline, story cards, banner, footer |
| `app/templates/studio.html` | **new** — character sections (a Jinja loop over the story's cast), advanced modal, Next bar |
| `app/templates/about_page.html` | **new** — name/age/title/description, photo choice, page shape, Generate |
| `app/templates/book.html` | **rewritten** — the stage, the map, the finished block, the edit strip |
| `app/templates/_story_card.html` | **new** |
| `app/templates/_character_section.html` | **new** |
| `app/templates/_advanced_modal.html` | **new** |
| `app/templates/_books_drawer.html` | **new** (replaces `_library.html`) |
| `app/templates/_banner.html` | **new** — signed-out explainer + Google/password |
| `app/templates/login.html` | **folded into `shelf.html`** — the `/login` route keeps serving it (with `?error=` and the lockout copy), so OAuth redirects and their tests are unchanged |
| `app/templates/book_detail.html` | **removed** — folded into `book.html` |
| `app/templates/wallet.html` | restyled, structure unchanged |
| `app/static/style.css` | **rewritten** around tokens and components (one file, sectioned, no build step) |
| `app/static/shelf.js` | **new** — story cards, resume state |
| `app/static/studio.js` | **new** — sections, camera pane, comic calls, takes, chips, modal, localStorage |
| `app/static/book-live.js` | **new** — poll, page flip, progress map, finished state, deep link |
| `app/static/drawer.js` | **new** — My books panel |
| `app/static/book.js`, `book-view.js`, `book-detail.js` | removed; `api.js`, `photo.js`, `downscale.js`, `book-state.js` reused |
| `app/assets/covers/*.svg` | **new** — two faceless placeholder covers |

No framework, no bundler, no CDN: server-rendered Jinja plus ES modules, exactly
the stack the app already uses.

### 11.7 Where state lives

| State | Home | Survives |
| --- | --- | --- |
| Photo, comic, candidates | server draft (`generation_id`), 24h | a reload, another tab |
| Which draft each section chose, its settings, collapsed state | `localStorage` per story | a reload, a browser restart |
| The book itself: pages, cover, PDF, About | server (disk or Postgres) | a restart, another device |
| Which page you were on | URL `#page-7` | a reload, a shared tab |

An unstarted studio has no book row: it is a draft plus a browser's memory. That
is deliberate — nothing of a child's face enters the durable store until the
person asks for a book, and the 24-hour draft window already makes that promise.

### 11.8 Auth and the banner

`GET /` becomes readable without a session, because the banner needs somewhere to
live: `app/auth.py`'s page guard gains exactly one allowance (`/` and the static
files it needs), and **nothing else moves** — `/books/{id}`, every `/api` route
and every `/book-media` route keep the session requirement and the owner check.
`/login` keeps its route and its `?error=` handling, and renders the shelf in
signed-out mode instead of a card on a page of its own. Tests in `test_auth.py`
pin both halves: the shelf is public, the book is not.

---

## 12. Phases

Effort is rough focused-days for one person; the order is the point.

| Phase | What lands | Acceptance | Rough |
| --- | --- | --- | --- |
| **P0 Design lock** | `design/` static HTML prototypes of the four screens + the modal, no backend, rendered at 3 widths; reviewed and adjusted | every screen here is approved before code | 0.5d |
| **P1 Foundations** | story catalogue, `story_key` in models/store/migration, about_photo + PDF frames, `sources.zip`, `/api/stories` | Jack's prompts byte-identical to today (golden fixtures); both stories load; old book rows still open; zip contents tested | 1.5d |
| **P2 Shelf + banner + drawer** | shelf, signed-out banner, password fold-in, My books drawer, restyled wallet | signed-out shelf shows banner and no books; signed-in shows drawer; reopening deep-links to the last page | 1d |
| **P3 Studio** | sections, camera, comic beside photo, takes, chips, advanced modal, localStorage, Next | star-only and four-face runs both complete; refresh mid-studio loses nothing; modal is keyboard-only usable | 2d |
| **P4 Book stage** | flip stage, progress map, queue-pause copy, finished block, sources.zip button, edit strip, failed-page retry | a real mock-mode book draws end to end; reload mid-draw resumes at the same page; finished state replaces the progress line | 1.5d |
| **P5 Polish** | mobile pass, motion + reduced-motion, focus/contrast pass, empty/expired/error states, cover SVGs | keyboard-only walkthrough of the whole flow; axe-clean pages; 360px-wide screens usable | 1d |
| **P6 QA + docs** | full click-through on mock mode, tests green, README/HANDOFF updated, decisions.md entry | `make check` green; the click-through matrix in §13 all passes | 0.5d |

Total: about eight focused days, with P1 and P3 as the two risky ones.

---

## 13. Test plan

**Automated (pytest, the existing suite):**

- *Golden prompts*: for each story, the cover prompt, page 1 and page 19 rendered
  with fixed settings compared to checked-in fixtures. Jack's fixture is captured
  **before** the catalogue move, so it proves the refactor changed nothing.
- *Catalogue*: page numbers contiguous; every page's `cast` names exist in that
  story's cast; the wolf and the cow are not photoable; every story has exactly
  one star; unknown `story_key` is a 422, not a traceback.
- *Store/migration*: a book written before `story_key` loads, opens and
  downloads; a new book records its story; `list_for_owner` returns the story.
- *About page*: all three photo choices build a PDF; `both` puts two images on
  the last page; `cartoon` is identical to today's output.
- *Sources zip*: contains the expected members for a book with 2 photographed
  characters; 404 for another owner; `no-store` header present.
- *Routes*: `GET /` signed out is 200 and leaks no book; `/books/{id}` still 404s
  for a stranger; `/stories/{key}/studio` requires a session.
- *Assets*: every `id`/`data-` hook the JS looks for exists in its template, and
  every `src` in the templates resolves to a served path — the cheap guard
  against a rename breaking a hand-written script.
- Existing suites (wallet, payments, providers, runner, auth, PDF) stay green
  throughout; `one_step` removals are mirrored in their tests.

**Manual click-through matrix (P6):**

mock provider · empty shelf → finished book · star only · star + three faces ·
three alternatives on the giant · refresh at 12 of 20 · failed page then finish ·
reopen from the drawer · delete then shelf · signed out · wrong password ·
360px / 768px / 1280px · keyboard only · reduced motion · camera on a phone.

---

## 14. Risks

| Risk | Why it bites | Mitigation |
| --- | --- | --- |
| The catalogue refactor touches prompts, PDF and runner | Jack's book could quietly change | golden prompt fixtures captured before the move; the old `BOOK_PAGES` alias keeps call sites compiling |
| Two images on the About page re-flow the text | a long description could overrun "The End" | `_fit_text` already measures the available height; test with a maximal name, title and 1000-character details |
| The per-minute brake vs the studio | 4 comics × 3 takes + 20 pages is one very busy minute | the book runner already *waits* rather than failing; the studio shows the wait in words, and the default `max_generations_per_minute` is raised from 30 to 60 in the same change |
| "Never resets" regresses | a client-side counter would drift from the server | §10.1: status document is the only truth; a reload mid-draw is in the test matrix |
| Relaxing `/` unauthenticated | could expose a stranger's books | only static shelf content is public; drawer and every API/media route keep the guard; pinned in `test_auth.py` |
| A hand-written JS rewrite drifts from the templates | silent dead buttons | the asset-hook test in §13 |
| The fun gets distracting | colour and motion on a page someone uses with a child | palette spends its colour on one accent per state; motion only where it means something; reduced-motion honoured |
| Old books after the migration | the grader opens a book made before the change | defaulted column, defaulted `about_photo`, plus an explicit migration test |

---

## 15. Open questions

1. **Little Red Riding Hood's cover title.** `AboutMe.book_title` is a template
   today ("{name} and the Beanstalk"). Proposed: shelf title "Little Red Riding
   Hood", cover "Mia and the Big Bad Wolf". Confirm the wording.
2. **The wolf's slot.** Proposed: no photo slot, pinned in prose like Milky-White
   — one prose character per story is a rule the prompts already enforce.
3. **Nineteen story pages for Red**, matching Jack's length and cost. Confirm, or
   say a shorter book for the second one.
4. **Password-mode banner wording** — the site is now described before the
   password is asked for. Proposed copy is in §3; the risk is saying too much to
   a stranger.
5. **Does the studio need a "start over" affordance** (clear this story's drafts)?
   Proposed: a small "Start over" in the section's overflow, behind a confirm —
   the one thing that *should* reset.

---

## 16. Not doing

- No SPA, no component framework, no bundler, no CDN fonts.
- No change to the PDF's story-page design, the prompt content of Jack, the
  wallet's mechanics, or the auth model (beyond the public shelf).
- No sharing links, no public galleries, no story editor.
- No new image provider, model or pricing surface in the UI.

---

## Appendix — the working order when code starts

```
P1  catalogue.py + app/books/*.toml + story_key (models, stores, migration)
    → golden prompt test green, Jack unchanged
P1  AboutMe.about_photo + draw_about_page(photo, cartoon, both)
P1  /api/stories, start-field changes, sources.zip
P2  shelf.html + banner + drawer + wallet restyle
P3  studio.html + studio.js + advanced modal + localStorage
P4  book.html + book-live.js + progress map + finished block + edit strip
P5  motion, mobile, a11y, covers
P6  click-through, docs, decisions entry
```
