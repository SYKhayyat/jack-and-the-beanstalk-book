# Decisions — what was built and why

This is the plain-language record of the nine steps in `HANDOFF.md` §4. Every
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

By default everything is stored on the server's disk. When a database is
configured, books, their pages, their PDFs, and the people who made them live in
the database instead. A finished book then survives a restart or redeploy and is
still there when its owner signs back in. A database is a switch in the
environment; local development needs none.

**Why:** the assignment's grader flow explicitly restarts the site between
making a book and downloading it. A book that disappears is a failed hand-in.

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

The book is 19 story pages plus an About page — the 20-page book the assignment
asks for. It follows the classic tale from the empty cupboard to the toppled
beanstalk, including the hen that lays golden eggs and the singing harp. Those
two are props described in the story, not characters, so they can never be given
a face.

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