"""Peecho's print API, in its test environment only.

Peecho accepts one PDF for a book: the first page is printed as the front
cover, the last as the back cover, and everything between as interior pages.
That is exactly the file this app already builds, so the print job is the
finished PDF and no second cover file is needed.

Why Peecho rather than Lulu: Lulu wants a separate interior PDF and a
wraparound cover spread, which would mean a second document layout and spine
calculation. Peecho's single-file contract keeps the book that the owner
reviewed byte-for-byte the book that is printed.

Two API details the adapter has to get right, in the order Peecho's own guide
gives them:

1. **Creating an order is not ordering.** `POST /rest/v3/order/` returns a
   draft order id; nothing reaches a print facility until
   `POST /rest/v3/order/payment` confirms it. The second call needs a secret
   derived as `SHA-256(secret_key + order_id)`.
2. **Country codes are ISO alpha-2** ("US", "GB"), the same codes the quote
   endpoint documents, not the three-letter codes of some older examples.

The host below is a constant, not a setting. There is deliberately no code
path to `www.peecho.com`: this app may place test orders and nothing else.
Even a misconfigured deployment cannot ship a real book from here.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

import httpx

from app.services.print.base import (
    PrintError,
    PrintJob,
    PrintQuote,
    ShippingAddress,
)

__all__ = ["SANDBOX_BASE_URL", "PeechoPrintProvider"]

logger = logging.getLogger(__name__)

#: The only host this provider will talk to. Peecho's live host is
#: `https://www.peecho.com`; it appears nowhere in this package on purpose.
SANDBOX_BASE_URL = "https://test.www.peecho.com"


class PeechoPrintProvider:
    """Create and pay for one order in Peecho's test environment.

    Args:
        merchant_api_key: The test account's key, from Settings > API.
        secret_key: The test account's secret key, from the same screen. Used
            only to sign the payment call; never sent on its own.
        offering_id: The numeric book product id from the test dashboard.
        contact_email: The operator's address for questions about the job.
        timeout_seconds: One HTTP request's budget.
        transport: Tests pass an `httpx.MockTransport`; production leaves it
            as None so the real test-environment host is used.
    """

    name = "peecho"
    mode = "test"

    def __init__(
        self,
        *,
        merchant_api_key: str,
        secret_key: str,
        offering_id: str,
        contact_email: str,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not merchant_api_key.strip():
            raise PrintError("PEECHO_API_KEY is empty: the print provider cannot start.")
        if not secret_key.strip():
            raise PrintError(
                "PEECHO_SECRET_KEY is empty: a Peecho order cannot be paid for, so "
                "nothing would ever be printed."
            )
        try:
            self._offering_id = int(offering_id.strip())
        except ValueError as exc:
            raise PrintError(
                f"PEECHO_OFFERING_ID must be the numeric product id shown in the "
                f"test dashboard, not {offering_id!r}."
            ) from exc
        self._key = merchant_api_key.strip()
        self._secret = secret_key.strip()
        self._contact_email = contact_email.strip()
        self._timeout = timeout_seconds
        self._transport = transport

    def submit(
        self,
        *,
        order_id: str,
        book_id: str,
        pdf_url: str,
        address: ShippingAddress,
        quote: PrintQuote,
    ) -> PrintJob:
        """Create the draft order, pay for it, and return the provider's id."""
        payload = {
            "merchant_api_key": self._key,
            "purchase_order": order_id,
            "currency": quote.currency,
            "item_details": [
                {
                    "item_reference": order_id,
                    "offering_id": self._offering_id,
                    "quantity": 1,
                    "file_details": {
                        "content_url": pdf_url,
                        "content_width": round(quote.page_width_mm, 1),
                        "content_height": round(quote.page_height_mm, 1),
                        "number_of_pages": quote.page_count,
                    },
                }
            ],
            "address_details": {
                "email_address": self._contact_email or address.email.strip(),
                "shipping_address": {
                    "first_name": address.first_name,
                    "last_name": address.last_name,
                    "address_line_1": address.street1.strip(),
                    "address_line_2": address.street2.strip() or None,
                    "zip_code": address.postcode.strip(),
                    "city": address.city.strip(),
                    "state": address.state.strip() or None,
                    "country_code": address.country_code.strip().upper(),
                },
            },
        }
        with httpx.Client(
            base_url=SANDBOX_BASE_URL, timeout=self._timeout, transport=self._transport
        ) as client:
            created = self._post(client, "/rest/v3/order/", payload, book_id=book_id)
            provider_order_id = _order_id(created)
            if not provider_order_id:
                logger.warning(
                    "print.peecho_no_order_id book=%s body=%s",
                    book_id,
                    _brief(created),
                )
                raise PrintError(
                    "The print service accepted the request but returned no order "
                    "number. The operator has the details."
                )

            payment = self._post(
                client,
                "/rest/v3/order/payment",
                {
                    "order_id": _number_or_text(provider_order_id),
                    "merchant_api_key": self._key,
                    "secret": self._payment_secret(provider_order_id),
                },
                book_id=book_id,
                provider_order_id=provider_order_id,
            )
            state = str(payment.get("order_state") or "PAID")

        logger.info(
            "print.peecho_submitted book=%s order=%s provider_order=%s state=%s",
            book_id,
            order_id,
            provider_order_id,
            state,
        )
        return PrintJob(
            provider=self.name,
            provider_order_id=provider_order_id,
            state=state,
            detail="Test order created and paid in Peecho's sandbox.",
        )

    def _post(
        self,
        client: httpx.Client,
        path: str,
        payload: Mapping[str, object],
        *,
        book_id: str,
        provider_order_id: str = "",
    ) -> dict[str, Any]:
        """POST one call and return its JSON object, or raise a `PrintError`."""
        try:
            response = client.post(path, json=dict(payload))
        except httpx.HTTPError as exc:
            logger.warning(
                "print.peecho_transport path=%s book=%s error=%s",
                path,
                book_id,
                type(exc).__name__,
            )
            raise PrintError(
                "The print service could not be reached. Nothing was sent; please try again."
            ) from exc

        if response.status_code not in {200, 201}:
            # The provider's body is for the operator's log, not for a
            # visitor-facing page; it says nothing sensitive, but it is not a
            # sentence this app wrote either.
            logger.warning(
                "print.peecho_refused path=%s book=%s provider_order=%s status=%d body=%s",
                path,
                book_id,
                provider_order_id or "-",
                response.status_code,
                response.text[:500],
            )
            raise PrintError(
                "The print service refused this order. Nothing was printed; "
                "the operator has the details."
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise PrintError(
                "The print service answered something this app could not read. "
                "Nothing is known to have been printed."
            ) from exc
        if not isinstance(data, dict):
            raise PrintError(
                "The print service answered something this app could not read. "
                "Nothing is known to have been printed."
            )
        return data

    def _payment_secret(self, provider_order_id: str) -> str:
        """Peecho's payment signature: SHA-256 of the secret key and order id."""
        order_number = _numeric_or_text(provider_order_id)
        value = f"{self._secret}{order_number}".encode()
        return hashlib.sha256(value).hexdigest()


def _order_id(data: dict[str, Any]) -> str:
    """Peecho's order id, under whichever of its documented keys came back."""
    for key in ("order_id", "id", "orderId"):
        value = data.get(key)
        if value not in (None, ""):
            return _numeric_or_text(value)
    order = data.get("order")
    if isinstance(order, dict):
        for key in ("order_id", "id", "orderId"):
            value = order.get(key)
            if value not in (None, ""):
                return _numeric_or_text(value)
    return ""


def _numeric_or_text(value: object) -> str:
    """An order id as a string without a pointless `.0` for whole numbers."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    return str(value)


def _number_or_text(text: str) -> int | str:
    """The payment call wants `order_id` as a number; keep odd ids as text."""
    return int(text) if text.isdigit() else text


def _brief(data: object) -> str:
    """A log-sized view of a provider response."""
    return str(data)[:500]
