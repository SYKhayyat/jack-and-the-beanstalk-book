# Print it and ship it: what I built

The app can turn a finished picture book into a **printed, bound book posted
to the buyer's address**, using Lulu's Print API in its **sandbox**. Lulu
prints nothing, ships nothing and charges nothing. A sandbox order is a real
API call to `api.sandbox.lulu.com`. With the default `mock` provider, the
order is recorded locally instead.

## The flow

1. A book finishes, and its page offers **Order a printed copy**.
2. **Read, then choose an edition.** The print page shows the reading PDF and
   two choices: the shape (8.5 × 8.5 in square or 8.5 × 11 in US Letter) and
   the binding (hardcover $29.99 or paperback $17.99). The owner presses
   **"I've reviewed this book — continue."**
3. **Approval builds the print files** (A1). These are an interior at the
   edition's trim plus a 0.125 in bleed, and a separate cover spread sized by
   Lulu for the real page count. Approval records the reading PDF's hash and
   the hash of each print file. An edit afterwards clears the approval.
4. **Address, then quote** (A6). The owner enters the address, including the
   phone number the carrier needs. Lulu prices one copy to that address, and
   the page shows the edition, the shipping (Lulu's price, passed on at cost)
   and the total. If the wallet is short, it offers to add exactly the
   difference (A7).
5. **Place.** The app takes the total from the wallet, sends Lulu the job
   with our order id as `external_id`, and records Lulu's job number.

**Nothing is sent before approval.** The API enforces this, not just the
template. Every step refuses without a matching approval, and the service
checks the PDF hash again when the order is placed.

**One approval places one copy.** Before the wallet is charged, the approval
is claimed with a single conditional update (`approved` → `placing`). The
charge is keyed by order id. A double click, a retried POST or a second tab
cannot buy two copies. If a claim is left behind by a crashed request, it can
be taken over after 15 minutes. The order is then looked up at Lulu before
anything is sent again.

## When Lulu's answer is lost (A4)

* **Lulu refuses**, or cannot be reached at all: nothing was printed, so the
  charge is refunded at once and the order is marked failed.
* **The answer is lost** (a timeout, a dropped connection, a 5xx, or an
  unexpected bug): the job may exist. The order becomes **unknown**. The
  money stays charged, and there is no automatic refund. The page offers two
  buttons:
  * **Try again** first looks the order up at Lulu by `external_id`. If Lulu
    has it, the order is marked placed. If not, it is sent once more.
  * **Cancel and refund** also looks first. If Lulu has the job, the order
    stays placed. If not, the money goes back to the wallet, once.

## The files the printer gets

| File | What it holds |
| --- | --- |
| Interior | copyright page, 20 story pages, "The End", About Me, then blank pages up to the binding's minimum (24 hardcover, 20 paperback) and an even count (A2) |
| Cover | one spread: the About Me picture on the back, a plain spine, and the cover art with the title and **"starring &lt;child&gt;"** on the front |

Pictures go in at full resolution. Art whose shape differs from the trim is
letterboxed on the book's cream paper, never cropped (A11).

Lulu fetches the files by URL. Each link is signed in its own scope
(`print-file`) and names one order, one file and its SHA-256. It serves only
those exact bytes (A3). A print link is not a share link, and a share link is
not a print link.

## What the child types is checked

`app/moderation.py` checks the child's name and every printed About Me field
**before a book starts**, and **again before a print order is placed**:

- A name must look like a name, and it cannot be an insult or an epithet.
- The About Me text cannot contain profanity, slurs, sexual words, threats,
  email addresses, links or phone numbers.

## The price

See [`print-business-model.md`](print-business-model.md). For a 24-page book
before shipping:

* hardcover $29.99, costing $18.89, for a **$11.10 (37.0%)** margin;
* paperback $17.99, costing $9.72, for an **$8.27 (46.0%)** margin.

The buyer pays shipping at Lulu's price.

## Why it is safe

The only print host in the code is `SANDBOX_BASE_URL =
"https://api.sandbox.lulu.com"`, and it is a constant, not a setting. The
default provider is `mock`, and `PRINT_PROVIDER=off` hides the feature. The
Lulu adapter is tested with `httpx.MockTransport`, so the suite proves the
payloads without a network call or a credential.

## How to demo it

- **With no setup:** `PRINT_PROVIDER=mock` (the default). The whole flow
  works. The page says **Test mode**, shipping is a fixed table ($4.99 US,
  $8.99 Canada and Mexico, $12.99 elsewhere), and the order number starts
  `mock-`.
- **With Lulu's sandbox:**
  1. Create an account at <https://developers.sandbox.lulu.com>.
  2. Copy the client key and secret from **API Keys**.
  3. Set `PRINT_PROVIDER=lulu`, `LULU_CLIENT_KEY` and `LULU_CLIENT_SECRET`.
     `LULU_CONTACT_EMAIL` is optional and falls back to `MAIL_FROM`.

  Jobs then appear in the sandbox dashboard.
- The app will not start with `PRINT_PROVIDER=lulu` unless both keys are set.

## Verification

- `ruff check app tests` and `mypy app` are clean.
- `tests/test_print_orders.py` covers:
  - every edition's file sizes;
  - the quote and the single charge;
  - an edit clearing the approval;
  - the moderation refusal;
  - the refund on a refusal;
  - unknown orders after a lost answer or a bug;
  - retry and cancel, with Lulu holding the job and without it;
  - the double-submit claim and the short-wallet hand-back;
  - the print-file links;
  - the business-model math;
  - the Lulu adapter: token caching, cover size, the quote split, the job
    payload, timeouts and 5xx answers as uncertain, refusals and refused
    connections as errors.
- `tests/test_print_topup.py` covers topping up to the quoted total.
- No paid API call was made while building this.
