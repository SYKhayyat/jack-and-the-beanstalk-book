# Print it and ship it — what I built

The app can now turn a finished picture book into a **printed, bound book
posted to the customer’s address**, in a print service’s **test environment**.
Nothing is printed, shipped or charged by the provider: the order is a real
API call to Peecho’s sandbox (or a recorded local test order with the default
`mock` provider).

## The flow

1. A book finishes. Its page gains **Order a printed copy — $24.99**.
2. The print page shows the exact PDF and nothing else: no address form yet.
   The owner presses **“I’ve reviewed this PDF — continue.”**
3. Only then does the delivery form appear. The approval stores the PDF’s
   SHA-256. If the book is edited after review, the hash no longer matches, the
   form disappears, and the owner must read and approve the new PDF.
4. The owner enters the delivery address. The app shows the price and places
   the order.
5. The app charges the copy through the wallet ledger, calls the provider, and
   records the provider’s test order number. If the provider refuses, the
   charge is reversed in the same ledger.

**Nothing is sent before approval.** This is enforced in the API, not just the
template: the order endpoint refuses without a matching approval, and the
service re-checks the PDF hash at the moment of ordering. Pinned by
`test_nothing_is_sent_before_the_pdf_is_approved` and
`test_an_edit_after_approval_clears_it`.

## It looks like a real book

The PDF assembler (`app/services/pdf.py`) now produces a 24-page book in the
order a printed picture book has:

| Page | Content |
| --- | --- |
| 1 | Front cover: the title and **“starring &lt;child&gt;”** |
| 2 | Copyright page: © year, **Storybook Studio**, “Made for &lt;child&gt;” |
| 3–22 | 20 story pages. The story starts on page 3 — the first page after the copyright page. |
| 23 | “The End” |
| 24 | Back cover: **About Me** — the portrait, age, what they want to be, the dedication and the free-text details. |

The last page is the back cover, and the first page is the front cover, so the
single PDF can go to Peecho unchanged: Peecho prints page 1 as the front cover
and the last page as the back. The even page count is deliberate.

A real 4:3 book measures **254 × 272 mm**; the provider payload carries that
size and the page count, measured from the PDF itself with `pypdf` at approval
and again at order time, rather than a hard-coded product size. The Peecho
test account's `PEECHO_OFFERING_ID` must therefore be a softcover product whose
allowed content box covers that trim (a dynamic-size product), or the provider
will refuse the job — which is the safe failure: nothing prints and the wallet
charge is returned.

## What the child types is checked

`app/moderation.py` checks the child’s name and every printed About Me field
**before a book starts** and **again before a print order is placed** (so a
book made before the rule existed cannot slip through):

- Names must look like names (letters, spaces, hyphens, apostrophes, periods;
  at least two letters; at most five words) and cannot be an insult or an
  epithet (“Stupid”, “Idiot”, “Fat Pig” are refused).
- The About Me text cannot carry clear profanity, slurs, sexual words,
  threats, email addresses, web links or phone numbers.
- False positives are tested too: “Jean-Luc”, “O’Brien”, lower-case typing,
  Hebrew names and “The Ugly Duckling” all pass.

Limitation, stated plainly: this is a blocklist plus a name-shape rule, not a
national name register. It catches the obvious abuse; it cannot prove
“Blorp” is not a name. The blocklist is one set in `app/moderation.py` that an
operator can extend.

## The price

**$24.99 per printed, shipped copy**, with the existing **$0.99** digital book
fee. For the 24-page book:

```
Generation        $1.58
Printing          $7.86   (Peecho base + 24 pages)
Shipping          $6.37   (Peecho standard example)
Card processing   $1.02   (Stripe 2.9% + 30¢)
------------------------------------------
Cost             $16.83
Price            $24.99
Margin            $8.16   32.7%
```

If the $0.99 digital sale is counted with it: revenue $25.98, margin **$9.15
(35.2%)**. The full model, sources and sensitivity table are in
[`print-business-model.md`](print-business-model.md).

## Why Peecho, and why it is safe

Peecho accepts **one PDF** with the front cover, interior and back cover; Lulu
would have needed a separate interior file and a wraparound cover spread. The
contract matches the file the owner already reviewed.

There is **no production host in the code**: `SANDBOX_BASE_URL` is
`https://test.www.peecho.com`, and the default provider is `mock`. The `peecho`
adapter is tested with `httpx.MockTransport`, so the test suite proves the
payload without a network call or a credential. `PRINT_PROVIDER=off` hides the
feature entirely.

## How to demo it

- With no setup: `PRINT_PROVIDER=mock` (the default). The whole flow works; the
  order page says **Test mode** and gives a `mock-…` order number.
- With Peecho: create a free **test** account at
  <https://test.www.peecho.com>, copy the test API key and the book product id,
  and set `PRINT_PROVIDER=peecho`, `PEECHO_API_KEY`,
  `PEECHO_OFFERING_ID`. Orders then appear in Peecho’s test dashboard.
- Either way, no real book is printed and no provider charges money.

## Verification

- `ruff check .` — clean.
- `mypy app` — clean.
- New tests: `tests/test_print_orders.py`, `tests/test_print_moderation.py`,
  the updated `tests/test_book_pdf.py`, and the migration test.
- A local self-check was appended to `review-log.md` (Run 2, 13 Nov 2026).
  It drove the local site with Playwright Chromium against the mock provider
  and measured both PDFs; money and live-site lines are marked not measured,
  because the real key and Stripe were deliberately not used.
- `docs/sample-book.pdf` is rebuilt by `python -m scripts.make_sample_book`:
  it forces the mock provider, never reads `.env` and makes no API call.
- The print flow is exercised end to end through the HTTP routes, with the real
  SQLite store, the real wallet ledger and a recording print provider: approval
  gate, stale-approval refusal, moderation refusal, charge, provider payload,
  failure refund, and the 24-page PDF structure.
- No paid API calls were made while building this; every book used the mock
  image provider.

## What I could not do from here

- Deploy and click through the live site (the build and test suite are local).
- Post to Canvas and Slack — those need your accounts.
- Place a real Peecho test order — that needs the test credentials. The
  adapter is ready for them, and the mock path is the one the live site runs
  until they are set.
