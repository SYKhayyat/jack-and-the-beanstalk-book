"""Read-only links to a finished book, signed and expiring (W3).

A grandparent has no account here. A share link is the smallest thing that lets
them read the book: the book's id, an expiry, and an HMAC over both, keyed with
the deployment's own signing key. Only a per-book counter is stored, so there is
nothing to clean up, and nothing can be guessed — without the key, forging one is a
256-bit problem.

Two rules the code keeps:

* **One book, and only reading.** The token names a single book, and the share
  routes serve the finished book's own pages, cover and PDF. They never serve
  the uploaded photograph, and they never accept an edit, a delete or a
  payment. The reader's owner-only surfaces are untouched by design.
* **Its owner can stop it.** A token carries the book's share generation, and
  works only while the book still has that number. "Stop sharing" adds one,
  retiring every link already sent without a list of them to keep (A10).
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
from datetime import UTC, datetime, timedelta

__all__ = ["SHARE_LIFETIME_DAYS", "ShareLink", "book_link", "issue", "read"]

#: What a token is for. Mixed into the signature so a share token can never be
#: accepted anywhere that expects a session or a print file, and vice versa.
_DOMAINS: dict[str, bytes] = {
    "share": b"share-link/v1",
    "print-file": b"print-file/v1",
}
_DEFAULT_SCOPE = "share"


def issue(
    book_id: str,
    *,
    key: bytes,
    expires_at: int,
    scope: str = _DEFAULT_SCOPE,
    generation: int = 0,
) -> str:
    """A token for one book, good until `expires_at` (a unix timestamp).

    `generation` is the book's share generation when it was minted (A10): the
    share routes honour a token only while the book still has that number, so
    "Stop sharing" retires every link at once. 0 is left out of the payload,
    which keeps a token minted before generations existed reading the same.
    """
    body: dict[str, object] = {"b": book_id, "e": int(expires_at)}
    if generation:
        body["g"] = int(generation)
    payload = _encode(body)
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
    generation = payload.get("g", 0)
    if (
        not isinstance(book_id, str)
        or not book_id
        or not isinstance(expiry, int)
        or not isinstance(generation, int)
    ):
        return None
    moment = int(time.time()) if now is None else now
    if expiry <= moment:
        return None
    return ShareLink(book_id=book_id, expires_at=expiry, generation=generation)


class ShareLink:
    """What a valid token says: which book, until when, and under which generation."""

    __slots__ = ("book_id", "expires_at", "generation")

    def __init__(self, *, book_id: str, expires_at: int, generation: int = 0) -> None:
        self.book_id = book_id
        self.expires_at = expires_at
        self.generation = generation

    def __repr__(self) -> str:  # a token holder's data, never in a log line
        return (
            f"ShareLink(book_id={self.book_id!r}, expires_at={self.expires_at}, "
            f"generation={self.generation})"
        )


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


#: How long a link lives, at most. The book's own retention is the other cap:
#: a link that outlived its book would only ever answer 404.
SHARE_LIFETIME_DAYS = 30


def book_link(
    book_id: str, created_at: str, *, key: bytes, retention_days: int, generation: int
) -> tuple[str, int]:
    """The share token a book has right now, and when it stops working.

    Everything in it comes from the book (when it started, its generation),
    so the same book always has the same token until sharing is stopped: the
    owner's page can show the live link on every load without storing it (R3).
    It lasts until the book is swept, and never more than 30 days.
    """
    try:
        started = datetime.fromisoformat(created_at)
    except ValueError:
        started = datetime.now(UTC)
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    days = min(retention_days, SHARE_LIFETIME_DAYS)
    expires_at = int((started + timedelta(days=days)).timestamp())
    token = issue(book_id, key=key, expires_at=expires_at, generation=generation)
    return token, expires_at
