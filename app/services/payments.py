"""Stripe payments, test mode only: fund a wallet and send money back.

The app never touches card numbers. Funding is a Stripe **Checkout Session**:
the visitor is sent to Stripe's own hosted page, pays there, and comes back;
this app then asks Stripe whether that session was really paid before it
credits anything. A refund is a call to Stripe's Refund API against the
payment that funded the wallet, and only the amount Stripe confirms is
recorded.

Three rules this module keeps, because they are the ones that matter:

* **The secret key never leaves the process.** It travels as HTTP Basic auth
  over TLS, and every error string is redacted before it is raised. The key is
  read from the environment by `Settings`, never from a template or a URL.
* **Test mode is the only mode this app ships with.** The key is a sandbox
  key; the account it belongs to moves no real money. A live key would work
  the same way, which is exactly why the operator has to be deliberate about
  which one is in `.env`.
* **No retries.** One request, one answer. A payment call that fails fails
  loudly; retrying a refund by itself is how money moves twice.

Nothing here decides what a wallet is; `wallet.py` owns the ledger, and the
routes own the flow. This is the seam between them and Stripe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.redaction import redact

__all__ = [
    "CheckoutSession",
    "PaidCheckout",
    "PaymentError",
    "PaymentProvider",
    "Refund",
    "StripePaymentProvider",
]

logger = logging.getLogger(__name__)

STRIPE_API = "https://api.stripe.com"
_HTTP_TIMEOUT = 30.0


class PaymentError(RuntimeError):
    """Stripe refused the call, or answered with something unusable.

    The message is safe to show to an operator — the secret is never in it —
    but it is not necessarily safe to show a visitor. Routes decide that.
    """


@dataclass(frozen=True, slots=True)
class CheckoutSession:
    """A hosted payment page the visitor is about to be sent to."""

    id: str
    url: str
    amount_cents: int
    currency: str


@dataclass(frozen=True, slots=True)
class PaidCheckout:
    """What Stripe says about a session the visitor has come back from."""

    id: str
    amount_cents: int
    currency: str
    payment_intent: str
    paid: bool
    #: Our own account id, echoed back from `client_reference_id`. It is how a
    #: returned session is proved to belong to the visitor claiming it.
    reference: str = ""


@dataclass(frozen=True, slots=True)
class Refund:
    """One refund Stripe accepted."""

    id: str
    amount_cents: int
    status: str


class PaymentProvider:
    """The contract the routes and the wallet use."""

    name = "none"

    def create_checkout(
        self,
        *,
        amount_cents: int,
        currency: str,
        success_url: str,
        cancel_url: str,
        reference: str,
    ) -> CheckoutSession:
        """Make a hosted payment page for `amount_cents`."""
        raise NotImplementedError

    def read_checkout(self, session_id: str) -> PaidCheckout:
        """Ask Stripe whether `session_id` was paid, and for how much."""
        raise NotImplementedError

    def refund(self, *, payment_intent: str, amount_cents: int) -> Refund:
        """Send `amount_cents` of `payment_intent` back to the card."""
        raise NotImplementedError


class StripePaymentProvider(PaymentProvider):
    """The Stripe implementation, over its documented form-encoded API."""

    name = "stripe"

    def __init__(
        self,
        secret_key: str,
        *,
        timeout_seconds: float = _HTTP_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        base_url: str = STRIPE_API,
    ) -> None:
        # A key pasted into a dashboard often carries a trailing newline, and
        # httpx rejects a header value containing one. Strip it here, once, so
        # every use of the key — auth, redaction — sees the same string.
        secret_key = secret_key.strip()
        if not secret_key:
            raise ValueError("Stripe needs a secret key; it is read from the environment.")
        if secret_key.startswith("sk_live_"):
            # This app ships with test mode only, by design. A live key in the
            # environment would move real money, so the boot fails loudly
            # rather than quietly turning a demo into a shop.
            raise ValueError(
                "This app runs Stripe in test mode only; put a sandbox key "
                "(sk_test_...) in STRIPE_SECRET_KEY, not a live one."
            )
        self._secret = secret_key
        # httpx does not retry, and this client is deliberately not reused
        # across a retry of its own: one call is one request.
        self._client = httpx.Client(
            base_url=base_url,
            auth=(secret_key, ""),
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    # --- funding ---------------------------------------------------------------

    def create_checkout(
        self,
        *,
        amount_cents: int,
        currency: str,
        success_url: str,
        cancel_url: str,
        reference: str,
    ) -> CheckoutSession:
        """Create a Checkout Session for one top-up.

        `client_reference_id` carries our own id, so a session found in the
        Stripe dashboard can be traced back to an account here. The charge is
        one line item with an inline price: there is no product catalogue to
        keep in sync for a single "picture-book credit" SKU.
        """
        body = self._post(
            "/v1/checkout/sessions",
            {
                "mode": "payment",
                "success_url": success_url,
                "cancel_url": cancel_url,
                "client_reference_id": reference,
                "line_items[0][quantity]": "1",
                "line_items[0][price_data][currency]": currency,
                "line_items[0][price_data][unit_amount]": str(amount_cents),
                "line_items[0][price_data][product_data][name]": "Picture-book credit",
            },
        )
        return CheckoutSession(
            id=str(body["id"]),
            url=str(body["url"]),
            amount_cents=int(body.get("amount_total") or amount_cents),
            currency=str(body.get("currency") or currency),
        )

    def read_checkout(self, session_id: str) -> PaidCheckout:
        """The truth about a returned session, straight from Stripe.

        A visitor controls the `session_id` query parameter, so this read is
        what makes the credit real: the id is fetched with our secret, and
        only a paid session whose amount Stripe reports is credited. The
        caller cross-checks the amount against what it asked for.
        """
        body = self._get(f"/v1/checkout/sessions/{session_id}")
        payment_intent = body.get("payment_intent")
        return PaidCheckout(
            id=str(body.get("id") or session_id),
            amount_cents=int(body.get("amount_total") or 0),
            currency=str(body.get("currency") or "usd"),
            payment_intent=str(payment_intent) if isinstance(payment_intent, str) else "",
            paid=body.get("payment_status") == "paid",
            reference=str(body.get("client_reference_id") or ""),
        )

    # --- refunds ---------------------------------------------------------------

    def refund(self, *, payment_intent: str, amount_cents: int) -> Refund:
        """Refund part or all of one payment.

        Stripe answers `pending`, `succeeded` or `failed`. A pending refund is
        accepted money-back, not a failure; the caller records it and does not
        call again.
        """
        body = self._post(
            "/v1/refunds",
            {"payment_intent": payment_intent, "amount": str(amount_cents)},
        )
        status = str(body.get("status") or "pending")
        if status == "failed":
            # Stripe can accept the request (HTTP 200) and still report that
            # the refund failed. Recording that as money sent would be a lie.
            raise PaymentError("Stripe payment failed: the refund was refused.")
        return Refund(
            id=str(body["id"]),
            amount_cents=int(body.get("amount") or amount_cents),
            status=status,
        )

    # --- transport -------------------------------------------------------------

    def _post(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._client.post(path, data=data)
        except httpx.HTTPError as exc:
            raise PaymentError(_clean(exc, self._secret)) from None
        return _body(response, self._secret)

    def _get(self, path: str) -> dict[str, Any]:
        try:
            response = self._client.get(path)
        except httpx.HTTPError as exc:
            raise PaymentError(_clean(exc, self._secret)) from None
        return _body(response, self._secret)


def _body(response: httpx.Response, secret: str) -> dict[str, Any]:
    """A JSON object, or a `PaymentError` that names the reason and no secret."""
    try:
        body: object = response.json()
    except ValueError as exc:
        raise PaymentError("Stripe returned a response that was not JSON.") from exc
    if not isinstance(body, dict):
        raise PaymentError("Stripe returned an unexpected response shape.")
    if response.status_code >= 400 or body.get("error"):
        raise PaymentError(_clean(body, secret))
    return body


def _clean(detail: object, secret: str) -> str:
    """One short line an operator can act on, with the key nowhere in it."""
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict):
            detail = error.get("message") or error.get("type") or error
        else:
            detail = error or detail
    text = redact(str(detail), (secret,))
    if len(text) > 300:
        text = text[:300] + "..."
    return f"Stripe payment failed: {text}"
