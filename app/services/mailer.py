"""The one mail this app sends: "your book is ready" (W2).

A book takes minutes, not seconds, and a parent who locks the phone or closes
the tab has no way to hear that the pictures finished. The channel is
deliberately small: one provider, one POST, plain text. A notification platform
(queues, templates, retries, preferences) is a second product to operate, and
this app needs one message that says a book is done and where to read it.

**Empty configuration means no mail**, the same rule the Stripe key follows: a
deployment with no `RESEND_API_KEY` or no `MAIL_FROM` renders books and sends
nothing, and the boot log says so rather than failing.

Nothing here ever raises and nothing here logs an address: a mail that cannot be
sent is one warning line naming the book, never a reason a finished book fails.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

__all__ = ["Mailer", "ResendMailer"]

logger = logging.getLogger(__name__)

#: Resend's API root; the send itself is one POST to `/emails`.
RESEND_API = "https://api.resend.com"

#: Long enough for a slow TLS handshake, short enough not to hold a book's job.
_HTTP_TIMEOUT = 10.0


class Mailer:
    """The contract: hand one plain-text message to a provider."""

    name = "none"

    def send(self, *, to: str, subject: str, text: str) -> bool:
        """Send one message. False means it went nowhere, not that it is lost.

        Callers do not treat a False as an error: the book is finished either
        way, and the parent can still open it.
        """
        raise NotImplementedError


class ResendMailer(Mailer):
    """Resend's HTTP API: one bearer key, one JSON body, one status code."""

    name = "resend"

    def __init__(
        self,
        api_key: str,
        sender: str,
        *,
        timeout_seconds: float = _HTTP_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        base_url: str = RESEND_API,
    ) -> None:
        # A key pasted into a dashboard often carries a trailing newline, and
        # httpx refuses a header value containing one.
        api_key = api_key.strip()
        sender = sender.strip()
        if not api_key or not sender:
            raise ValueError("Resend needs an API key and a From address.")
        self._sender = sender
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    def send(self, *, to: str, subject: str, text: str) -> bool:
        """One message. A rejected send is a log line, never an exception.

        The body is Resend's documented shape: `from`, one `to`, `subject` and
        `text`. HTML is deliberately not offered — the mail is a sentence and a
        link, and a plain-text body cannot be a phishing page wearing this
        app's design.
        """
        try:
            response = self._client.post(
                "/emails",
                json={"from": self._sender, "to": [to], "subject": subject, "text": text},
            )
        except httpx.HTTPError:
            logger.warning("mail.transport_failed", exc_info=True)
            return False
        if 200 <= response.status_code < 300:
            return True
        # The provider's own error body says why (an unverified sender, a bad
        # address). It never contains the key; it may contain the address, so
        # only the status and the provider's error code are kept.
        logger.warning(
            "mail.rejected status=%d error=%s",
            response.status_code,
            _error_code(response),
        )
        return False


def _error_code(response: httpx.Response) -> str:
    """The provider's short error name, when the body carries one."""
    try:
        body: Any = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    for key in ("name", "message", "error"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value[:80]
        if isinstance(value, dict):
            nested = value.get("message") or value.get("name")
            if isinstance(nested, str) and nested:
                return nested[:80]
    return ""
