"""What the things that are sold cost.

A book's fee lives on the wallet (`DOCUMENT_PRICE_CENTS`), because the wallet is
the only thing that charges it. The picture price is read by two layers — the
settings, which let a deployment choose it, and the wallet, which takes it —
so it lives here, below both, with no imports of its own.
"""

from __future__ import annotations

__all__ = ["PICTURE_PRICE_CENTS"]

#: One picture drawn beyond a book's own fee — a portrait, an extra candidate,
#: a redrawn page or cover, a face swap's pages.
PICTURE_PRICE_CENTS = 15
