"""Peecho's print API, in its test environment only.

Peecho accepts one PDF for a book: the first page is printed as the front
cover, the last as the back cover, and everything between as interior pages.
That is exactly the file this app already builds, so the print job is the
finished PDF and no second cover file is needed.

Why Peecho rather than Lulu: Lulu wants a separate interior PDF and a
wraparound cover spread, which would mean a second document layout and spine
calculation. Peecho's single-file contract keeps the book that the owner
reviewed byte-for-byte the book that is printed.

The host below is a constant, not a setting. There is deliberately no code
path to `www.peecho.com`: this app may place test orders and nothing else.
Even a misconfigured deployment cannot ship a real book from here.
"""

from __future__ import annotations

import logging

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

#: Peecho's book product is chosen in the test dashboard; the file's own
#: trim size is sent with the job, because the app's page follows the art's
#: shape rather than a fixed product size.
DEFAULT_WIDTH_MM = 210
DEFAULT_HEIGHT_MM = 148


class PeechoPrintProvider:
    """Create one order in Peecho's test environment.

    Args:
        merchant_api_key: The test account's key, from Settings > API.
        offering_id: The book product id from the test dashboard.
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
        offering_id: str,
        contact_email: str,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not merchant_api_key.strip():
            raise PrintError("PEEcho_API_KEY is empty: the print provider cannot start.")
        if not offering_id.strip():
            raise PrintError(
                "PEEcho_OFFERING_ID is empty: choose a book product in the test dashboard."
            )
        self._key = merchant_api_key.strip()
        self._offering_id = offering_id.strip()
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
        """POST one order to the test environment and return its id."""
        payload = {
            "merchant_api_key": self._key,
            "purchase_order": order_id,
            "currency": "USD",
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
        try:
            with httpx.Client(
                base_url=SANDBOX_BASE_URL, timeout=self._timeout, transport=self._transport
            ) as client:
                response = client.post("/rest/v3/order/", json=payload)
        except httpx.HTTPError as exc:
            logger.warning(
                "print.peecho_transport book=%s error=%s", book_id, type(exc).__name__
            )
            raise PrintError(
                "The print service could not be reached. Nothing was sent; please try again."
            ) from exc

        if response.status_code not in {200, 201}:
            # The provider's body is for the operator's log, not for a
            # visitor-facing page; it says nothing sensitive, but it is not a
            # sentence this app wrote either.
            logger.warning(
                "print.peecho_refused book=%s status=%d body=%s",
                book_id,
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

        provider_order_id = _order_id(data)
        if not provider_order_id:
            logger.warning(
                "print.peecho_no_order_id book=%s body=%s", book_id, response.text[:500]
            )
            raise PrintError(
                "The print service accepted the request but returned no order number. "
                "The operator has the details."
            )
        state = str(data.get("status") or data.get("state") or "submitted")
        logger.info(
            "print.peecho_submitted book=%s order=%s provider_order=%s",
            book_id,
            order_id,
            provider_order_id,
        )
        return PrintJob(
            provider=self.name,
            provider_order_id=provider_order_id,
            state=state,
            detail="Test order placed in Peecho's sandbox.",
        )


def _order_id(data: dict[str, object]) -> str:
    """Peecho's order id, under whichever of its documented keys came back."""
    for key in ("id", "orderId", "order_id"):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    order = data.get("order")
    if isinstance(order, dict):
        for key in ("id", "orderId", "order_id"):
            value = order.get(key)
            if value not in (None, ""):
                return str(value)
    return ""
