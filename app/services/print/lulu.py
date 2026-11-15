"""Lulu's Print API, in its sandbox only (Q1).

Lulu wants two files per book, an interior PDF (trim plus bleed, one page per
side) and a cover spread whose size Lulu itself computes from the product and
page count; `app.services.pdf.build_print_interior` and `build_print_cover`
make them. The adapter does four things:

* **cover size** — `POST /cover-dimensions/` for the real page count;
* **quote** — `POST /print-job-cost-calculations/` for one copy to the
  buyer's address. Everything beyond the line item's printing cost (shipping,
  fulfilment, fees, tax on them) is the shipping line the buyer pays at cost
  (A6);
* **submit** — `POST /print-jobs/` with our order id as `external_id`;
* **find** — `GET /print-jobs/?search=<order id>`, so a request whose answer
  was lost is looked up before it is ever sent again (A4).

Authentication is OAuth client credentials against Lulu's token endpoint; the
token is cached until shortly before it expires.

The host is a constant, not a setting. Lulu's live host appears nowhere in
this package: this app may place sandbox orders and nothing else, so even a
misconfigured deployment cannot print and ship a real book from here.
"""

from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.services.print.base import (
    PrintCost,
    PrintError,
    PrintJob,
    PrintUncertain,
    ShippingAddress,
)
from app.services.print.products import CoverSize, Product

__all__ = ["SANDBOX_BASE_URL", "SHIPPING_LEVELS", "LuluPrintProvider"]

logger = logging.getLogger(__name__)

#: The only host this provider talks to.
SANDBOX_BASE_URL = "https://api.sandbox.lulu.com"
_TOKEN_PATH = "/auth/realms/glasstree/protocol/openid-connect/token"
SHIPPING_LEVELS = ("MAIL", "PRIORITY_MAIL", "GROUND", "EXPEDITED", "EXPRESS")
#: Refresh the token this long before Lulu says it expires.
_TOKEN_MARGIN_SECONDS = 60.0


class LuluPrintProvider:
    """Price, size and place print jobs in Lulu's sandbox.

    Args:
        client_key: The sandbox account's client key (developers.sandbox.lulu.com).
        client_secret: Its client secret.
        contact_email: The operator's address for questions about a job.
        shipping_level: One of `SHIPPING_LEVELS`; quotes and jobs use it.
        timeout_seconds: One HTTP request's budget.
        transport: Tests pass an `httpx.MockTransport`.
    """

    name = "lulu"
    mode = "test"

    def __init__(
        self,
        *,
        client_key: str,
        client_secret: str,
        contact_email: str,
        shipping_level: str = "MAIL",
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not client_key.strip() or not client_secret.strip():
            raise PrintError(
                "LULU_CLIENT_KEY and LULU_CLIENT_SECRET must both be set: the print "
                "provider cannot start without its sandbox keys."
            )
        if shipping_level not in SHIPPING_LEVELS:
            raise PrintError(
                f"LULU_SHIPPING_LEVEL must be one of {', '.join(SHIPPING_LEVELS)}."
            )
        if not contact_email.strip():
            raise PrintError(
                "LULU_CONTACT_EMAIL is empty: Lulu needs a contact for every job."
            )
        self._key = client_key.strip()
        self._secret = client_secret.strip()
        self._contact_email = contact_email.strip()
        self._shipping_level = shipping_level
        self._client = httpx.Client(
            base_url=SANDBOX_BASE_URL, timeout=timeout_seconds, transport=transport
        )
        self._token = ""
        self._token_expires = 0.0
        self._token_lock = threading.Lock()

    # -- the provider contract -------------------------------------------------

    def cover_size(self, product: Product, pages: int) -> CoverSize:
        data = self._call(
            "POST",
            "/cover-dimensions/",
            json={
                "pod_package_id": product.pod_package_id,
                "interior_page_count": pages,
                "unit": "pt",
            },
        )
        try:
            width = float(data["width"])
            height = float(data["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PrintError("The print service did not say how big the cover is.") from exc
        return product.cover_from(width_pt=width, height_pt=height, pages=pages)

    def quote(self, product: Product, pages: int, address: ShippingAddress) -> PrintCost:
        data = self._call(
            "POST",
            "/print-job-cost-calculations/",
            json={
                "line_items": [
                    {
                        "page_count": pages,
                        "pod_package_id": product.pod_package_id,
                        "quantity": 1,
                    }
                ],
                "shipping_address": _address(address),
                "shipping_option": self._shipping_level,
            },
        )
        try:
            total = _cents(data["total_cost_incl_tax"])
            printing = sum(
                (_cents(item["total_cost_incl_tax"]) for item in data["line_item_costs"]),
                start=0,
            )
            currency = str(data.get("currency") or "USD")
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("print.lulu_quote_unreadable body=%s", str(data)[:500])
            raise PrintError("The print service could not price this order.") from exc
        if currency != "USD":
            raise PrintError("The print service quoted in a currency this shop does not take.")
        return PrintCost(
            printing_cents=printing,
            shipping_cents=max(0, total - printing),
            currency=currency,
        )

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
        payload = {
            "contact_email": self._contact_email,
            "external_id": order_id,
            "shipping_level": self._shipping_level,
            "shipping_address": _address(address),
            "line_items": [
                {
                    "external_id": order_id,
                    "title": title[:255] or "A picture book",
                    "quantity": 1,
                    "printable_normalization": {
                        "pod_package_id": product.pod_package_id,
                        "interior": {"source_url": interior_url},
                        "cover": {"source_url": cover_url},
                    },
                }
            ],
        }
        data = self._call("POST", "/print-jobs/", json=payload, sent_matters=True)
        job = _job(self.name, data)
        if job is None:
            # It answered success without an id: the job may well exist.
            logger.warning("print.lulu_no_job_id order=%s body=%s", order_id, str(data)[:500])
            raise PrintUncertain("The print service did not confirm the order number.")
        logger.info(
            "print.lulu_submitted order=%s pages=%d job=%s status=%s",
            order_id,
            pages,
            job.provider_order_id,
            job.state,
        )
        return job

    def find(self, order_id: str) -> PrintJob | None:
        data = self._call(
            "GET",
            "/print-jobs/",
            params={"search": order_id, "exclude_line_items": "true"},
        )
        results = data.get("results")
        if not isinstance(results, list):
            raise PrintError("The print service's job list could not be read.")
        for item in results:
            if isinstance(item, dict) and item.get("external_id") == order_id:
                return _job(self.name, item)
        return None

    # -- plumbing ---------------------------------------------------------------

    def _bearer(self) -> str:
        with self._token_lock:
            if self._token and time.monotonic() < self._token_expires:
                return self._token
            try:
                response = self._client.post(
                    _TOKEN_PATH,
                    data={"grant_type": "client_credentials"},
                    auth=(self._key, self._secret),
                )
            except httpx.HTTPError as exc:
                raise PrintError("The print service could not be reached.") from exc
            if response.status_code != 200:
                logger.warning("print.lulu_auth_refused status=%d", response.status_code)
                raise PrintError("The print service did not accept this shop's keys.")
            try:
                body = response.json()
                token = str(body["access_token"])
                lifetime = float(body.get("expires_in", 300))
            except (ValueError, KeyError, TypeError) as exc:
                raise PrintError(
                    "The print service's sign-in answer could not be read."
                ) from exc
            self._token = token
            self._token_expires = time.monotonic() + max(0.0, lifetime - _TOKEN_MARGIN_SECONDS)
            return token

    def _call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        sent_matters: bool = False,
    ) -> dict[str, Any]:
        """One authenticated call, its JSON object back, or a `PrintError`.

        With `sent_matters`, a failure after the request may have reached Lulu
        (a timeout, a dropped connection, a 5xx) raises `PrintUncertain`
        instead: the job might exist, so it must not be refunded or resent
        without looking it up first.
        """
        headers = {"Authorization": f"Bearer {self._bearer()}"}
        try:
            response = self._client.request(
                method, path, json=json, params=params, headers=headers
            )
        except httpx.ConnectError as exc:
            # Nothing reached Lulu.
            logger.warning("print.lulu_unreachable path=%s", path)
            raise PrintError(
                "The print service could not be reached. Nothing was sent."
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("print.lulu_transport path=%s error=%s", path, type(exc).__name__)
            if sent_matters:
                raise PrintUncertain("The print service did not answer in time.") from exc
            raise PrintError("The print service did not answer in time.") from exc

        if response.status_code >= 500 and sent_matters:
            logger.warning(
                "print.lulu_server_error path=%s status=%d", path, response.status_code
            )
            raise PrintUncertain("The print service failed while taking the order.")
        if response.status_code not in {200, 201}:
            # The body is for the operator's log, not a visitor-facing page.
            logger.warning(
                "print.lulu_refused path=%s status=%d body=%s",
                path,
                response.status_code,
                response.text[:500],
            )
            raise PrintError("The print service refused this request. Nothing was printed.")
        try:
            data = response.json()
        except ValueError as exc:
            if sent_matters:
                raise PrintUncertain("The print service's answer could not be read.") from exc
            raise PrintError("The print service's answer could not be read.") from exc
        if not isinstance(data, dict):
            if sent_matters:
                raise PrintUncertain("The print service's answer could not be read.")
            raise PrintError("The print service's answer could not be read.")
        return data


def _address(address: ShippingAddress) -> dict[str, str]:
    """Lulu's shipping-address object, without empty optional fields."""
    fields = {
        "name": address.name.strip(),
        "street1": address.street1.strip(),
        "street2": address.street2.strip(),
        "city": address.city.strip(),
        "state_code": address.state.strip().upper(),
        "postcode": address.postcode.strip(),
        "country_code": address.country_code.strip().upper(),
        "phone_number": address.phone.strip(),
        "email": address.email.strip(),
    }
    return {key: value for key, value in fields.items() if value}


def _cents(value: object) -> int:
    """A Lulu decimal-string amount ("12.34") as whole cents."""
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {value!r}") from exc
    return int((amount * 100).quantize(Decimal(1)))


def _job(provider: str, data: dict[str, Any]) -> PrintJob | None:
    job_id = data.get("id")
    if job_id in (None, ""):
        return None
    status = data.get("status")
    state = str(status.get("name", "")) if isinstance(status, dict) else str(status or "")
    return PrintJob(
        provider=provider,
        provider_order_id=str(job_id),
        state=state or "CREATED",
        detail="Sandbox print job created at Lulu; it is never printed or shipped.",
    )
