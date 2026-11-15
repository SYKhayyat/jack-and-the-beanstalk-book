# The printed book: cost, price and margin

**Decision (AUDIT.md §10, Q2/A6):** the buyer picks an edition and pays its
price plus the printer's shipping quote for their address, at cost.

| Edition | Price |
| --- | --- |
| Hardcover casewrap, 8.5 × 8.5 in square or 8.5 × 11 in US Letter | **$29.99** |
| Paperback perfect bound, 8.5 × 8.5 in square or 8.5 × 11 in US Letter | **$17.99** |

The digital book keeps its own **$0.99** fee. Everything is paid from the
wallet.

The code works in US cents (`app/services/print/products.py`,
`app/services/print/pricing.py`). The test
`tests/test_print_orders.py::test_the_business_model_math_is_the_write_up_math`
checks the same arithmetic as this page, so the page and the code cannot
drift apart.

## What one book is

The printer gets two files (A1). Both are at full resolution, with a 0.125 in
bleed.

* **The interior:** the copyright page, the 20 story pages, "The End" and the
  About Me page. That is 23 pages, made up with blank pages to the binding's
  minimum and to an even count (A2). Hardcover needs at least 24 pages and
  paperback at least 20, so today's book prints as **24 interior pages** in
  either binding.
* **The cover:** one spread with the back cover, the spine and the front
  cover. Its size comes from Lulu's `/cover-dimensions/` for the real page
  count. Art whose shape differs from the trim is letterboxed on cream paper,
  never cropped (A11).

The buyer reviews the reading PDF. Approving it builds both print files from
the same pages and records their SHA-256 hashes. A link the printer fetches
names its hash, and serves nothing else (A3).

## Cost per printed book (24 interior pages)

Lulu's wholesale prices for these packages (premium colour, 80# white coated
paper, gloss cover) are a base fee per book plus 21.48¢ a page. The trim does
not change them.

| Line | Hardcover | Paperback | Source |
| --- | ---: | ---: | --- |
| Generation | 158 | 158 | Measured average model spend for one book (Run 1 ledger). |
| Printing | 1,614 | 732 | Lulu's base fee (1,098¢ hardcover / 216¢ paperback) + 24 × 21.48¢. |
| Payment processing | 117 | 82 | Stripe's 2.9% + 30¢ on the price. |
| **Total cost** | **1,889** | **972** | |
| Price | 2,999 | 1,799 | |
| **Margin** | **1,110 (37.0%)** | **827 (46.0%)** | Share of the price. |

The printing line is the planning figure. A real order is priced by Lulu's
quote for the buyer's address (`/print-job-cost-calculations/`), and the quote
is stored on the order.

## Shipping is passed on at cost

Lulu's quote for one copy to the buyer's address covers postage, fulfilment
and any tax on them. Everything in it beyond the printing line goes into
**shipping**, and the buyer is charged exactly that amount. So shipping adds
nothing to the margin and takes nothing from it, apart from Stripe's 2.9% on
the larger charge.

| Shipping quoted | Hardcover charged | Hardcover margin | Paperback charged | Paperback margin |
| ---: | ---: | ---: | ---: | ---: |
| $0.00 | $29.99 | $11.10 | $17.99 | $8.27 |
| $4.99 | $34.98 | $10.96 | $22.98 | $8.12 |
| $12.99 | $42.98 | $10.72 | $30.98 | $7.89 |

When the wallet is short, the top-up offer (A7) works from the quoted total,
shipping included.

## What is not in this model

- **Refunds and customer service.** A damaged book is a real cost. The margin
  leaves room for a small rate of replacements.
- **Tax.** Lulu's quote includes the tax it charges. The model is otherwise
  pre-tax.
- **Volume discounts.** The app orders one copy at a time.
- **A free image provider.** With the mock backend or Gemini-in-Chrome, the
  generation line is $0 and the margin is higher. $1.58 is the cost with a
  paid provider, and it is the honest number to plan with.

## Sandbox only

The app places **sandbox orders only**. The only print host anywhere in the
code is `SANDBOX_BASE_URL = "https://api.sandbox.lulu.com"`, and the default
provider is `mock`. A `lulu` order is a real API call to Lulu's sandbox, but
Lulu never prints, ships or charges for a book. The buyer's copy is still
charged through the app's own wallet ledger in either mode.
