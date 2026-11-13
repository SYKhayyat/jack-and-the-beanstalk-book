"""The shape every print provider and route shares.

A print order has three moments, and the data says which one it is in:

* **approved** — the owner has looked at the finished PDF. Nothing has left
  the building; the row records the exact PDF's hash, so a redraw invalidates
  the approval instead of silently printing a different book.
* **submitted** — the provider accepted the job and gave us its own id.
* **failed** — the provider refused it; the order page says why, and a retry
  is a new attempt.

The provider contract is deliberately small: take the PDF's address and a
shipping address, return the provider's order. `mock` does it locally, and
`peecho` does it against Peecho's *test* environment. There is no live mode in
this module and no setting that can point at a production host.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

__all__ = [
    "COUNTRIES",
    "PrintError",
    "PrintJob",
    "PrintOrder",
    "PrintProvider",
    "PrintQuote",
    "ShippingAddress",
]

#: The countries the delivery form offers, as Peecho's ISO-3166 **alpha-2**
#: codes. A closed list on purpose: the form is not a free-text field that a
#: provider has to interpret, and every code here is one Peecho documents.
#: Peecho's order and quote endpoints take two letters ("US", "GB"), not the
#: three-letter codes the API's older examples sometimes show.
COUNTRIES: tuple[tuple[str, str], ...] = (
    ("US", "United States"),
    ("CA", "Canada"),
    ("GB", "United Kingdom"),
    ("IE", "Ireland"),
    ("AU", "Australia"),
    ("NZ", "New Zealand"),
    ("DE", "Germany"),
    ("FR", "France"),
    ("NL", "Netherlands"),
    ("BE", "Belgium"),
    ("ES", "Spain"),
    ("IT", "Italy"),
    ("PT", "Portugal"),
    ("CH", "Switzerland"),
    ("AT", "Austria"),
    ("SE", "Sweden"),
    ("NO", "Norway"),
    ("DK", "Denmark"),
    ("FI", "Finland"),
    ("IL", "Israel"),
    ("JP", "Japan"),
    ("SG", "Singapore"),
)

_COUNTRY_CODES = frozenset(code for code, _name in COUNTRIES)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class PrintError(RuntimeError):
    """A print job could not be approved, quoted or submitted."""


@dataclass(frozen=True, slots=True)
class ShippingAddress:
    """Where the printed book goes, exactly as the delivery form collected it."""

    name: str
    street1: str
    city: str
    postcode: str
    country_code: str
    email: str
    street2: str = ""
    state: str = ""

    def issues(self) -> list[str]:
        """Everything a provider would refuse the job for, in plain words."""
        problems: list[str] = []
        if len(self.name.strip()) < 2:
            problems.append("Please give the name of the person receiving the book.")
        if not self.street1.strip():
            problems.append("Please give the street address.")
        if not self.city.strip():
            problems.append("Please give the town or city.")
        if not self.postcode.strip():
            problems.append("Please give the postal code.")
        code = self.country_code.strip().upper()
        if code not in _COUNTRY_CODES:
            problems.append("Please choose a country from the list.")
        if not _EMAIL.match(self.email.strip()):
            problems.append("Please give an email address for the order confirmation.")
        return problems

    @property
    def first_name(self) -> str:
        parts = self.name.strip().split()
        return parts[0] if parts else ""

    @property
    def last_name(self) -> str:
        parts = self.name.strip().split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""

    def as_dict(self) -> dict[str, str]:
        """A JSON-safe copy for the order row and the provider payload."""
        return {
            "name": self.name.strip(),
            "street1": self.street1.strip(),
            "street2": self.street2.strip(),
            "city": self.city.strip(),
            "state": self.state.strip(),
            "postcode": self.postcode.strip(),
            "country_code": self.country_code.strip().upper(),
            "email": self.email.strip(),
        }


@dataclass(frozen=True, slots=True)
class PrintQuote:
    """What one printed copy costs the customer, and what it costs us."""

    page_count: int
    printing_cents: int
    shipping_cents: int
    retail_cents: int
    #: The finished PDF's own trim size, in millimetres. The provider is told
    #: the size of the file it is about to receive; it is measured from the
    #: PDF rather than guessed from a product name.
    page_width_mm: float = 210.0
    page_height_mm: float = 148.0
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class PrintJob:
    """What the provider answered when it accepted a job."""

    provider: str
    provider_order_id: str
    state: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PrintOrder:
    """One row of `print_orders`, as the routes and the store pass it."""

    id: str
    book_id: str
    user_id: str
    state: str
    pdf_sha256: str
    page_count: int
    printing_cents: int
    shipping_cents: int
    retail_cents: int
    currency: str = "USD"
    provider: str = ""
    provider_order_id: str = ""
    address: dict[str, str] = field(default_factory=dict)
    error: str = ""
    approved_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PrintProvider(Protocol):
    """The small surface a print API has to implement."""

    name: str
    mode: str

    def submit(
        self,
        *,
        order_id: str,
        book_id: str,
        pdf_url: str,
        address: ShippingAddress,
        quote: PrintQuote,
    ) -> PrintJob:
        """Place the job and return the provider's own id."""
        ...
