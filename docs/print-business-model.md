# The printed book: cost, price and margin

**Decision:** one printed, shipped copy sells for **$24.99**. The finished
digital book keeps its existing **$0.99** fee.

All amounts are US cents in the code (`app/pricing.py`,
`app/services/print/pricing.py`) and dollars below. The test
`tests/test_print_orders.py::test_the_business_model_math_is_the_write_up_math`
asserts the same arithmetic, so this document cannot drift from what the app
charges.

## What one book is

The PDF is 24 printed pages:

| Pages | What they are |
| --- | --- |
| 1 | front cover: the title and **“starring &lt;child&gt;”** |
| 2 | copyright page: © year, publisher, “Made for &lt;child&gt;” |
| 3–22 | the 20 story pages; the story starts on page 3, the first page after the copyright page |
| 23 | “The End” |
| 24 | back cover: **About Me**, the portrait and the child’s details |

The even page count is deliberate: Peecho’s softcover product expects an even
number of pages and prints page 1 as the front cover and the last page as the
back cover. The file the owner reviews is the file sent to the printer,
byte-for-byte.

## Cost per printed book

| Line | Cents | Source / assumption |
| --- | ---: | --- |
| Generation | 158 | Measured average model spend per book in Run 1’s `api_calls` ledger ($1.60 Jack, $1.56 Red). Retry caps have lowered the ceiling since, so this is conservative. |
| Printing | 786 | Peecho softcover: base 378¢ + 24 pages × 17¢. Converted from Peecho’s published €3.50 base + €0.15/page at 1.08. |
| Shipping | 637 | Peecho’s published €5.90 shipping example, converted at 1.08. Destination-dependent; see the sensitivity table. |
| Payment processing | 102 | Stripe 2.9% + 30¢ on $24.99: 72¢ + 30¢. |
| **Total cost** | **1,683** | **$16.83** |

## Price and margin

```
Retail price                 2,499 ¢   $24.99
Total cost                   1,683 ¢   $16.83
----------------------------------------------
Margin                         816 ¢    $8.16
Margin as a share of price             32.7%
```

The $0.99 digital fee is charged earlier, when the PDF is written. If the
printed copy and the digital book are counted as one sale, revenue is $25.98
and the margin is **$9.15 (35.2%)**. They are shown separately here because a
customer can buy the digital book without printing it.

**Why $24.99:** the book is a personalised, hard-to-replicate gift that arrives
at the door; $24.99 is in the range of a mass-market hardcover and still leaves
a third of the price as margin after every cost, including card processing.
$19.99 would leave only $3.30 (16.5%) and would be wiped out by one expensive
destination; $29.99 leaves more but is above the impulse price for a picture
book.

## The digital-only problem this fixes

At $0.99, a digital book costs about $1.58 to generate: **a loss of $0.59 a
book**. The printed copy is the product that makes the unit economics
positive:

| Sold | Revenue | Cost | Margin |
| --- | ---: | ---: | ---: |
| Digital only | $0.99 | $1.58 | **−$0.59** |
| Printed + digital | $25.98 | $16.83 | **+$9.15** |

That is the business reason to offer printing, beyond the feature itself.

## Sensitivity

Shipping is the assumption most likely to be wrong. Holding everything else
fixed:

| Shipping | Total cost | Margin at $24.99 | Margin % |
| ---: | ---: | ---: | ---: |
| $5.00 | $15.46 | $9.53 | 38.1% |
| $6.37 (model) | $16.83 | $8.16 | 32.7% |
| $8.00 | $18.46 | $6.53 | 26.1% |

At the other price points:

| Retail | Stripe fee | Total cost | Margin | Margin % |
| ---: | ---: | ---: | ---: | ---: |
| $19.99 | $0.88 | $16.69 | $3.30 | 16.5% |
| $24.99 | $1.02 | $16.83 | $8.16 | 32.7% |
| $29.99 | $1.17 | $16.98 | $13.01 | 43.4% |

The retail price is a setting (`PRINT_RETAIL_CENTS`), as are the wholesale
assumptions, so an operator can reprice without a code change. The price the
customer sees is the price the wallet is charged; the quote is stored on the
order.

## What is not in this model

- **Refunds and customer service.** A damaged book is a real cost; the model
  gives enough headroom for a small rate of replacements.
- **Tax.** Peecho and Stripe handle their own side; the model is pre-tax.
- **Volume discounts.** Peecho discounts at 5+ copies; this app orders one at a
  time.
- **A free image provider.** With the mock or Gemini-in-Chrome backend the
  generation line is $0, so the margin is higher; $1.58 is the paid-provider
  case and is the honest planning number.

## Sandbox only

The app places **test orders only**. In this build there is no production
Peecho host anywhere in the code: `SANDBOX_BASE_URL` is
`https://test.www.peecho.com`, and the `mock` provider is the default. A
`peecho` order is a real API call to that test environment, but no book is
printed, shipped or charged by the provider. The customer’s copy is charged
through the app’s own wallet ledger in either mode.
