"""Keep API keys out of logs and error messages.

A provider exception can quote the request it was building, and the OpenAI SDK's
transport errors do exactly that: a value that is *unexpectedly* shaped (a
trailing newline pasted into an environment variable, say) turns up verbatim in
`Illegal header value b'Bearer sk-...'`, which then travels out through the
traceback and into the logs.

Two independent defences:

* Providers redact their own secret before wrapping an error, and drop the
  chained exception so the raw transport message is never formatted.
* `install_secret_redaction` scrubs every configured secret from any message
  that reaches a log handler, so a future code path cannot leak one by accident.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

__all__ = ["MASK", "SecretRedactingFilter", "install_secret_redaction", "redact"]

MASK = "***REDACTED***"


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every occurrence of any non-empty `secret` in `text` with `MASK`."""
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, MASK)
    return text


class SecretRedactingFilter(logging.Filter):
    """Scrub known secrets from a record's message before it is formatted.

    Secrets are accumulated rather than replaced, so a later call with a
    different key extends the set instead of silently being ignored. Only the
    message is rewritten; a formatted traceback is the provider's responsibility
    (see the module docstring).
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets: list[str] = []
        self.add_secrets(secrets)

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(self._secrets)

    def add_secrets(self, secrets: Iterable[str]) -> None:
        for secret in secrets:
            if secret and secret not in self._secrets:
                self._secrets.append(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        cleaned = redact(message, self._secrets)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


# One filter instance, shared by the root logger and every handler. Reusing a
# single object is what makes repeated startup calls accumulate secrets instead
# of stacking filters that each hold a different subset.
_installed: SecretRedactingFilter | None = None


def install_secret_redaction(secrets: Iterable[str]) -> None:
    """Attach the shared filter to the root logger and all of its handlers.

    Handlers matter because a record logged on a child logger propagates to the
    root *handlers* without passing through the root logger's own filters.
    """
    global _installed
    if _installed is None:
        _installed = SecretRedactingFilter(secrets)
    else:
        _installed.add_secrets(secrets)
    if not _installed.secrets:
        return
    root = logging.getLogger()
    if _installed not in root.filters:
        root.addFilter(_installed)
    for handler in root.handlers:
        if _installed not in handler.filters:
            handler.addFilter(_installed)
