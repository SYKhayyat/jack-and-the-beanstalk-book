"""The session cookie: one signed value, two ways it can be earned.

A session is a tiny JSON payload — who it belongs to and when it ends — signed
with HMAC-SHA256 and put in an `HttpOnly` cookie. There is no server-side
session store, for the same reason the password gate has none: the state that
matters fits in the cookie, and a store would only add a table to keep and a
query on every request.

Two kinds of session exist, and the kind is what decides which routes accept it:

* **google** — a signed-in person with a real identity. Issued after the OAuth
  callback has exchanged a code with Google and looked up who they are. Carries
  their `sub` (Google's permanent id for them), so books can be filed under that
  person and nobody else can read them.
* **password** — the local-dev/offline fallback. There is no person behind it,
  so it carries no identity and there is nothing to file a book under.

Keeping both under one cookie and one signer is deliberate: the routes ask "is
there a session of the kind this app accepts?" rather than knowing which
mechanism issued it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

__all__ = ["SESSION_COOKIE", "Session", "SessionCodec"]

SESSION_COOKIE = "jb_session"

# Fifteen minutes, for state that only has to survive a redirect: the OAuth
# round trip, the click back to the book, nothing longer. Deliberately not a
# day — the long-lived cookie is the signed-in session, not this one.
_SHORT_LIVED_SECONDS = 15 * 60


class SessionKind:
    """The two ways a session can be earned."""

    GOOGLE = "google"
    PASSWORD = "password"


@dataclass(frozen=True, slots=True)
class Session:
    """A verified session: who it is, and for how much longer it is valid."""

    kind: str
    expires_at: int
    # Google's permanent id for the person; empty for a password session.
    sub: str = ""
    # The person's display name, carried so a page can greet them without a
    # lookup on every render.
    name: str = ""

    @property
    def user_id(self) -> str | None:
        """The id books are filed under, or None when there is no person."""
        return self.sub or None

    @property
    def is_person(self) -> bool:
        """Whether a real, identified person holds this session."""
        return self.kind == SessionKind.GOOGLE


class SessionCodec:
    """Issues and verifies the signed session cookie."""

    def __init__(self, signing_key: bytes) -> None:
        self._key = signing_key

    # --- issuing ---------------------------------------------------------------

    def issue(
        self,
        kind: str,
        *,
        ttl_seconds: int,
        sub: str = "",
        name: str = "",
    ) -> str:
        """Mint a cookie value: an expiry, the payload that justifies it, a signature."""
        expiry = int(time.time()) + ttl_seconds
        payload = _encode({"kind": kind, "sub": sub, "name": name, "exp": expiry})
        return f"{payload}.{self._sign(payload)}"

    def issue_google(self, *, sub: str, name: str, ttl_seconds: int) -> str:
        return self.issue(SessionKind.GOOGLE, ttl_seconds=ttl_seconds, sub=sub, name=name)

    def issue_password(self, *, ttl_seconds: int) -> str:
        return self.issue(SessionKind.PASSWORD, ttl_seconds=ttl_seconds)

    # --- verifying -------------------------------------------------------------

    def read(self, token: str | None, *, now: int | None = None) -> Session | None:
        """Verify a cookie value and return the session, or None if it is not valid.

        Every failure mode — no cookie, malformed, wrong signature, expired —
        answers with None, so no caller can distinguish them.
        """
        if not token:
            return None
        payload_text, _, signature = token.partition(".")
        if not payload_text or not signature:
            return None
        if not hmac.compare_digest(signature, self._sign(payload_text)):
            return None
        try:
            payload = json.loads(_decode(payload_text))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        expiry = payload.get("exp")
        if not isinstance(expiry, int):
            return None
        moment = int(time.time()) if now is None else now
        if expiry <= moment:
            return None
        kind = payload.get("kind")
        if kind not in (SessionKind.GOOGLE, SessionKind.PASSWORD):
            return None
        return Session(
            kind=str(kind),
            expires_at=expiry,
            sub=str(payload.get("sub", "")),
            name=str(payload.get("name", "")),
        )

    # --- the short-lived state the OAuth round trip needs ----------------------

    def issue_state(self, state: str, next_path: str) -> str:
        """Mint the cookie that remembers the OAuth state and where to go back to.

        The state is checked against the cookie when Google redirects back, so a
        forged callback cannot make this app sign anyone in. The cookie is
        short-lived because it only has to survive one round trip.
        """
        expiry = int(time.time()) + _SHORT_LIVED_SECONDS
        payload = _encode({"state": state, "next": next_path, "exp": expiry})
        return f"{payload}.{self._sign(payload)}"

    def read_state(self, token: str | None) -> tuple[str, str] | None:
        """Return the remembered `(state, next_path)`, or None if it is not valid."""
        if not token:
            return None
        payload_text, _, signature = token.partition(".")
        if not payload_text or not signature:
            return None
        if not hmac.compare_digest(signature, self._sign(payload_text)):
            return None
        try:
            payload = json.loads(_decode(payload_text))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        expiry = payload.get("exp")
        state = payload.get("state")
        if not isinstance(expiry, int) or not isinstance(state, str):
            return None
        if expiry <= int(time.time()):
            return None
        next_path = payload.get("next", "/")
        return state, str(next_path) if next_path else "/"

    # --- signing ---------------------------------------------------------------

    def _sign(self, payload_text: str) -> str:
        return hmac.new(self._key, payload_text.encode(), hashlib.sha256).hexdigest()


def _encode(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(payload_text: str) -> bytes:
    padding = "=" * (-len(payload_text) % 4)
    return base64.urlsafe_b64decode(payload_text + padding)
