# Decisions — what was built and why

This is the plain-language record of the steps in `HANDOFF.md` §4. Every
step is committed separately, with the tests green before each commit. The
short version: the picture book is finished and the engineering is done; the
things that need a live account, a paid picture run, or other people are
listed at the end for you to do.

## 1. Free pictures while building, paid pictures for the hand-in

The app can draw pictures through several services. While building, it uses a
free one, so testing the whole flow costs nothing. The hand-in book itself uses
the paid service whose faces actually look like the uploaded child. This is a
setting, not a code change: the production deployment is already pointed at the
paid one, and nothing a visitor sees names either service.

**Why:** the free service was tried on real photos and the likeness was not
usable. It is a testing tool, not a product decision.

## 2. Feedback was prepared before the book was finished

The assignment asks for ten reactions from real people, ten from AI
personalities, and a short comparison. The survey, the crowd-service setup, and
the comparison skeleton are written and kept in [`feedback/`](feedback/README.md).
The AI side is a plan only, and the method stays unpublished until after
Monday's class comparison.

**Why:** the real-people study costs money and needs your account; the AI side
needs the same book to compare against, so neither could be run in code. Writing
the instruments now means the study can start the moment the book is live.

## 3. Google is the front door in production

Visitors sign in with Google. Their books are filed under their Google account,
and only that account can open them; anyone else gets the same "not found"
answer a stranger would get. In local development, where there is no Google
setup, the old shared password still works. There is never a choice between the
two on a live site.

**Why:** a book contains a child's face and a family's words. It should belong
to someone, survive a restart, and not be openable by guessing a link.

## 4. Books survive restarts

Everything is in the database: books, their pages, their PDFs, the people who
made them, and the story templates they are made from. A finished book survives
a restart or redeploy and is still there when its owner signs back in.

There is one storage path, not two. `DATABASE_URL` names a Postgres database in
production; leaving it empty means a SQLite file under `STORAGE_DIR`, created
and migrated on boot. Local development therefore still needs no database
server — it just gets a real database anyway, the same tables and the same
migrations the deployment runs. Books that only ever existed as files under
`<STORAGE_DIR>/books` are dropped rather than migrated; that affects local
development only, because production has used the database since books gained
accounts.

**Why:** the assignment's grader flow explicitly restarts the site between
making a book and downloading it. A book that disappears is a failed hand-in.
Keeping a filesystem mode as well meant two code paths, two test suites and a
class of bug where a feature worked in one and not the other — and the story
templates only exist in the database, so a filesystem book could not be rendered
from one.

## 5. Pages appear as they finish, several at a time

Pages are drawn in parallel, within a fixed limit so costs and rate limits stay
under control. The book page shows a tile per page with its own state: waiting,
being drawn, or done. A page only shows a picture once its image is actually
saved. If one page fails, the finished pages stay.

**Why:** a 20-page book takes minutes. One progress bar that sits still is
indistinguishable from a broken app; a page grid tells the truth about what is
happening.

## 6. More of the cast can be given a face — except the cow

The story's cast is fixed: Jack, the mother, the old man, the giant, and
Milky-White the cow. A visitor may optionally upload a photo for the mother, the
old man, or the giant. That person then gets the same quick cartoon portrait as
the starring child, and only the pages that actually feature them receive that
face.

Milky-White cannot be given a face anywhere: she is not in the character list,
has no upload field, and the server refuses any attempt. She is described in the
story text and drawn from that description.

Every page also carries one identical description of the whole cast, so a
character's look cannot drift from page to page. Each page receives only the
faces in its own scene, never the whole book's cast.

**Why:** the earlier version let the mother wear the child's face. Giving people
their own photos fixes that at the root, while keeping each page's image set
small and the cow safely a cow.

## 7. The book is now the whole tale, and a long book can't be turned away

The book is 20 story pages plus an About page — 21 sheets, or 22 with the cover.
It follows the classic tale from the empty cupboard, through the second climb
after the gold runs out, to the toppled beanstalk, including the hen that lays
golden eggs and the singing harp. Those two are props described in the story, not
characters, so they can never be given a face.

Starting a book costs one unit of the per-minute allowance; each page costs one
more as it is actually drawn. A full minute now makes a book take longer, never
refuses it.

**Why:** the old rule charged for all pages up front. Once the book grew past
the per-minute limit, every single book would have been refused forever. A book
is one product; its own length must not be able to block its start.

## 8. One log trail per request and per book

Every request gets a short id, shown back to the browser and attached to every
log line it causes. The book's background work inherits that id, so the whole
story is one searchable thread: started, one line per page with time and size
and outcome, then complete or failed.

Secrets are scrubbed before anything is written: service keys, the Google
client secret, the session key, and both the database address and its password.
Image contents are never logged — only how many bytes they were.

**Why:** when a grader says "my book didn't work", the operator needs its trail
without asking for account details or hunting through unrelated lines.

## 9. The hand-in package checks itself

The environment example now documents every setting the app reads, with every
secret left blank. Automated checks confirm that no real credential shape or
private key is committed, that no private environment file is in the repo, and
that every link in the documentation resolves. `to-do.md` carries the
private-window checklist and all deliverable links.

**Why:** these are exactly the failures that are invisible locally and fatal at
hand-in time. A test is cheaper than discovering them in front of the class.

## 10. Four screens instead of one long form

The app used to be one page with about forty controls, with the portrait review
and the book stacked underneath the form. It is now four screens, one job each:
a **shelf** to choose the book, a **studio** where each character gets a photo
and a comic beside it, an **About** screen for the last page, the picture choice
(cartoon, photo or both) and the page shape, and the **book** itself, drawn live
one page at a time and then downloadable as the PDF or as a zip of every photo
and comic. *Little Red Riding Hood* was on the shelf as "Coming soon" until its
story landed as data (§ "A book is made from its story's data" below). The full plan, including the review that settled the
details, is in [`ui-redesign-plan.md`](ui-redesign-plan.md).

**Why:** the old page read as a form, and the thing people came for appeared
underneath the thing they were filling in. Each screen now has one job, and the
book is the biggest thing on the screen that shows it.

## 11. One template, cream paper, words never on the picture

The page template is now explicit data rather than a value recomputed per page:
one art box, one text band beneath it, one font size for the whole book, on
cream paper. Every story page is the picture on top and its prose underneath; a
short page keeps the book's band, and a page too long for it grows the band
rather than losing the end of the story.

The cover is the only sheet whose title sits over art, so a plate's top band is
measured automatically. A busy band cannot be approved: the admin screen
disables Approve and shows the measured spread and the reason, while a flat sky
or a gentle gradient passes.

**Why:** "no text over a face" is a layout problem, not something to hope a model
gets right. Making the geometry data and refusing a busy cover band turns the
rule into something a test and an approval screen can enforce.

## 12. The stories are data, and both hold twenty pages

Jack grew its twentieth page — the last coin, the empty cupboard, and the
decision to climb again — so the arc between the gold and the hen is on the page
instead of implied. *Little Red Riding Hood* now exists as a full twenty-page
tale: the basket, the warning, the wood, the wolf's shortcut, Grandmother's
cupboard, the "what big ears" exchange, the Woodcutter's shout, and home. The
wolf is sly and never gory; nobody is bitten and no page needs a weapon.

Each story lives in a reviewed seed file (`app/books/<key>.toml`): its pages,
the scene each page draws, the cover, and the character bible. The seed is
validated at boot against the same contract for every story — twenty pages and a
cover, 45–90 words of prose per page, short sentences, a simple vocabulary, a
cast that resolves, and no page referencing more characters than the image
provider can take. A broken story now fails at start-up, not on a child's book.

**Why:** one template only holds quality steady if both stories are measured by
the same rules. Making the rules executable is what stops the second story from
drifting into a different product.

## 13. A book keeps the pictures, words and looks it started with

Every book made on a story template now records the exact template version it
started on: one approved empty picture (a "plate") per page, one revision of
each page's words, and one revision of each character's look. Approving a better
plate, editing a character's look or changing a story's seed file makes a new
version for books started afterwards. A book already started never changes, and
anything a book still uses can be retired but never deleted. Run
`make migrate && make seed` to load the stories (the app now also seeds at every
boot); seeding twice changes nothing.

A book made on a template can't be marked finished while any of its pictures,
the cover included, has no quality review on record, and one query
(`unreviewed_images`) proves no finished book slipped through.

**Why:** a parent who downloads their book next month must get the book they
paid for, not one silently redrawn on newer pictures. And a review that is
merely expected, rather than required, gets skipped the first time it's
inconvenient.

## 14. A book is made from its story's data, and you decide when a story opens

Both stories can now be made. Every word, scene and character look a book is
drawn and printed from comes from its story's seed file, loaded into the
database at every boot; `prompts.toml` keeps only the wording shared by every
story, plus each story's props-and-places sheet and its own extra rules. Jack's
twenty pages are no longer written twice. The seed carries two framings per
scene where it needs them: the empty stage a plate is painted from, and the
finished picture with the child in it.

When a book starts it records the exact revision of every page and every look
it uses (and the template version, when the story has one). A changed seed or a
revised look reaches the next book, never one already made. A portrait made for
one story is refused by the other, because the two stories share cast names
but not looks, and Red's star wears the story's red hood instead of the
visitor's chosen outfit.

Each story has a start gate on `/admin/plates`. Off (the default), books start
on the latest words and looks. On, a story without a complete set of approved
plates shows "Not ready yet" on the shelf, and its studio, portrait pass and
start all refuse before any picture is paid for. Books already started are not
affected.

**Why:** one source per page and per look is the only way the plates, the
prompts and the PDF can agree. The gate is a switch rather than a rule because
plates only change a finished book once the edit pass (T8) lands; until then,
holding a story back would stop books without making them better.

## What is left for you (not done in code)

These need your accounts, your money, or your judgement, so they are documented
and unchecked rather than claimed:

1. **The free end-to-end run** on the free service — it proves the whole flow
   works without spending anything.
2. **The Google sign-in console setup**, following
   [`google-oauth-setup.md`](google-oauth-setup.md).
3. **The database setup**, following the Neon runbook in `to-do.md`.
4. **One paid visual pass** across several photos, once the judge says go.
5. **Deploying** the Render blueprint and pasting the real key.
6. **The ten-person crowd study** and the AI comparison, then publishing the
   comparison link and pasting it into `to-do.md`.
7. **The private-window click-through** of every deliverable link, the grader
   flow across a restart, and only then the Canvas/Slack hand-in.