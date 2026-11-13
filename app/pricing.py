"""What the things that are sold cost.

A book's fee lives on the wallet (`DOCUMENT_PRICE_CENTS`), because the wallet is
the only thing that charges it. The picture price is read by two layers — the
settings, which let a deployment choose it, and the wallet, which takes it —
so it lives here, below both, with no imports of its own.

A printed copy adds four numbers of its own. They live here for the same
reason: the settings read them, the print quote uses them, and the business
write-up quotes them. The sources are documented in
`app/services/print/pricing.py`.
"""

from __future__ import annotations

__all__ = [
    "PICTURE_PRICE_CENTS",
    "PRINT_BASE_COST_CENTS",
    "PRINT_PER_PAGE_CENTS",
    "PRINT_RETAIL_CENTS",
    "PRINT_SHIPPING_CENTS",
]

#: One picture drawn beyond a book's own fee — a portrait, an extra candidate,
#: a redrawn page or cover, a face swap's pages.
PICTURE_PRICE_CENTS = 15

#: Peecho softcover wholesale: a base per book plus a per-page rate.
PRINT_BASE_COST_CENTS = 378
PRINT_PER_PAGE_CENTS = 17

#: Standard shipping, as Peecho publishes an example for a European delivery.
PRINT_SHIPPING_CENTS = 637

#: What one printed copy sells for. A business setting; the app reads it from
#: the environment, and this is the default the write-up prices.
PRINT_RETAIL_CENTS = 2499
