"""The shape every print provider and route shares.

A print order moves through these states, and the row says which one it is in:

* **approved** — the owner has looked at the finished book and chosen an
  edition. The print interior and cover were built then and their hashes are
  on the row, so the files a printer fetches are exactly the approved ones
  (A3) and a redraw invalidates the approval instead of printing a different
  book. An address and its shipping quote are added to the approval before
  the order is placed.
* **placing** — claimed by one request, charged, and being sent.
* **submitted** — the provider accepted the job and gave us its own id.
* **failed** — the provider refused it; the money went back to the wallet.
* **unknown** — the request may or may not have reached the provider (A4).
  Nothing is refunded automatically, because a book may be printing; the
  next attempt looks the job up by our order id first, and the buyer can ask
  for a refund.
* **superseded** — a newer approval replaced it before it was placed.

The provider contract: price an edition for an address, size its cover, place
the job, and find a job by our order id. `mock` does it locally; `lulu` does
it against Lulu's sandbox. There is no live host anywhere in this package.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.services.print.products import CoverSize, Product

__all__ = [
    "COUNTRIES",
    "PrintCost",
    "PrintError",
    "PrintJob",
    "PrintOrder",
    "PrintProvider",
    "PrintQuote",
    "PrintUncertain",
    "ShippingAddress",
]

#: The countries the delivery form offers, as ISO-3166 **alpha-2** codes, the
#: form Lulu's address takes. A closed list on purpose: the form is not a
#: free-text field that a provider has to interpret.
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
#: Lulu needs a state or province code for these (its address schema says so).
_STATE_REQUIRED = frozenset({"US", "CA", "AU", "MX"})
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^\+?[0-9 ().\/-]{8,20}$")


class PrintError(RuntimeError):
    """A print job could not be approved, quoted or submitted."""


class PrintUncertain(PrintError):
    """The provider may have received the job, and its answer was lost (A4).

    Raised for a timeout or a dropped connection after the request was sent.
    The order is not refunded or retried blindly: the next attempt asks the
    provider whether a job with our order id exists.
    """


@dataclass(frozen=True, slots=True)
class ShippingAddress:
    """Where the printed book goes, exactly as the delivery form collected it."""

    name: str
    street1: str
    city: str
    postcode: str
    country_code: str
    email: str
    phone: str = ""
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
        elif code in _STATE_REQUIRED and not self.state.strip():
            problems.append("Please give the state or province.")
        if not _EMAIL.match(self.email.strip()):
            problems.append("Please give an email address for the order confirmation.")
        if not _PHONE.match(self.phone.strip()):
            problems.append("Please give a phone number the courier can call.")
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
            "phone": self.phone.strip(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> ShippingAddress:
        """The address an order row stored."""
        return cls(
            name=data.get("name", ""),
            street1=data.get("street1", ""),
            street2=data.get("street2", ""),
            city=data.get("city", ""),
            state=data.get("state", ""),
            postcode=data.get("postcode", ""),
            country_code=data.get("country_code", ""),
            email=data.get("email", ""),
            phone=data.get("phone", ""),
        )


@dataclass(frozen=True, slots=True)
class PrintCost:
    """What the provider charges us for one copy sent to one address."""

    printing_cents: int
    shipping_cents: int
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class PrintQuote:
    """What one printed copy costs the buyer, and what it costs us.

    The buyer pays the edition's price plus shipping at cost (A6): the
    shipping line is everything the provider charges beyond printing, for
    this address.
    """

    product_key: str
    page_count: int
    printing_cents: int
    shipping_cents: int
    retail_cents: int
    currency: str = "USD"

    @property
    def total_cents(self) -> int:
        return self.retail_cents + self.shipping_cents


@dataclass(frozen=True, slots=True)
class PrintJob:
    """What the provider answered when it accepted (or still holds) a job."""

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
    #: The web PDF the owner reviewed: an edit after approval changes it.
    pdf_sha256: str
    product_key: str
    #: Interior pages as printed, blanks included.
    page_count: int
    printing_cents: int
    shipping_cents: int
    retail_cents: int
    currency: str = "USD"
    interior_sha256: str = ""
    cover_sha256: str = ""
    provider: str = ""
    provider_order_id: str = ""
    address: dict[str, str] = field(default_factory=dict)
    error: str = ""
    quoted_at: datetime | None = None
    approved_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def total_cents(self) -> int:
        return self.retail_cents + self.shipping_cents


class PrintProvider(Protocol):
    """The small surface a print API has to implement."""

    name: str
    mode: str

    def cover_size(self, product: Product, pages: int) -> CoverSize:
        """The cover spread the printer wants for this edition and page count."""
        ...

    def quote(self, product: Product, pages: int, address: ShippingAddress) -> PrintCost:
        """Printing and shipping for one copy to this address."""
        ...

    def submit(
        self,
        *,
        order_id: str,
        product: Product,
        pages: int,
        interior_url: str,
        cover_url: str,
        address: ShippingAddress,
        title: str,
    ) -> PrintJob:
        """Place the job under our order id and return the provider's own id.

        Raises:
            PrintError: the provider refused the job; nothing will print.
            PrintUncertain: the answer was lost; the job may exist.
        """
        ...

    def find(self, order_id: str) -> PrintJob | None:
        """The provider's job placed under our order id, if there is one."""
        ...
