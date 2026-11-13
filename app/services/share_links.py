"""Read-only links to a finished book, signed and expiring (W3).

A grandparent has no account here. A share link is the smallest thing that lets
them read the book: the book's id, an expiry, and an HMAC over both, keyed with
the deployment's own signing key. Nothing is stored, so there is nothing to
clean up, and nothing can be guessed — without the key, forging one is a
256-bit problem.

Two rules the code keeps:

* **One book, and only reading.** The token names a single book, and the share
  routes serve the finished book's own pages, cover and PDF. They never serve
  the uploaded photograph, and they never accept an edit, a delete or a
  payment. The reader's owner-only surfaces are untouched by design.
* **It cannot outlive the book.** A link's expiry is capped at the book's own
  retention, so a link can never point at a book that has already been swept.

The payload is the same shape the session cookie uses (base64url JSON, then
`.signature`), so a share link has one obvious way in and out and no new
format to review.

A second kind of token lives here too: the one-hour file link a print provider
fetches a PDF from (`scope="print-file"`). Its signature mixes in a different
domain string, so it can never be used as a share link, and a share link can
never be used to fetch a print file. They share a format and a key, not a
permission.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

__all__ = ["ShareLink", "issue", "read"]

#: What a token is for. Mixed into the signature so a share token can never be
#: accepted anywhere that expects a session or a print file, and vice versa.
_DOMAINS: dict[str, bytes] = {
    "share": b"share-link/v1",
    "print-file": b"print-file/v1",
}
_DEFAULT_SCOPE = "share"


def issue(book_id: str, *, key: bytes, expires_at: int, scope: str = _DEFAULT_SCOPE) -> str:
    """A token for one book, good until `expires_at` (a unix timestamp)."""
    payload = _encode({"b": book_id, "e": int(expires_at)})
    return f"{payload}.{_sign(payload, key, _domain(scope))}"


def read(
    token: str,
    *,
    key: bytes,
    now: int | None = None,
    scope: str = _DEFAULT_SCOPE,
) -> ShareLink | None:
    """The book a token names, or None for anything that is not one.

    A tampered signature, a malformed payload, an exhausted expiry and a token
    from another deployment all answer the same way, so a caller cannot be
    tricked into telling them apart.
    """
    payload_text, _, signature = token.partition(".")
    if not payload_text or not signature:
        return None
    if not hmac.compare_digest(signature, _sign(payload_text, key, _domain(scope))):
        return None
    try:
        payload = json.loads(_decode(payload_text))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    book_id = payload.get("b")
    expiry = payload.get("e")
    if not isinstance(book_id, str) or not book_id or not isinstance(expiry, int):
        return None
    moment = int(time.time()) if now is None else now
    if expiry <= moment:
        return None
    return ShareLink(book_id=book_id, expires_at=expiry)


class ShareLink:
    """What a valid token says: which book, and until when."""

    __slots__ = ("book_id", "expires_at")

    def __init__(self, *, book_id: str, expires_at: int) -> None:
        self.book_id = book_id
        self.expires_at = expires_at

    def __repr__(self) -> str:  # a token holder's data, never in a log line
        return f"ShareLink(book_id={self.book_id!r}, expires_at={self.expires_at})"


def _domain(scope: str) -> bytes:
    try:
        return _DOMAINS[scope]
    except KeyError as exc:  # a typo in a caller, not a bad token
        raise ValueError(f"unknown token scope {scope!r}") from exc


def _sign(payload_text: str, key: bytes, domain: bytes) -> str:
    return hmac.new(key, domain + b"." + payload_text.encode(), hashlib.sha256).hexdigest()


def _encode(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(text: str) -> str:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode()
