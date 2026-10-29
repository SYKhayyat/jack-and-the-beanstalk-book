"""The shared-password gate: the local-development fallback sign-in.

This is what the app runs on when there is no Google client configured — local
development, offline tests, and a deployment the operator has not wired up yet.
When Google *is* configured, this gate is switched off entirely: one way in, and
it is a real account.

The gate is not trying to withstand a determined attacker. It exists to keep
strangers off an app where every image costs money, and to make sure the bill
only ever goes to someone who was invited. Three deliberate simplifications:

* **No server-side session.** The cookie carries an expiry and an HMAC of the
  payload that produced it. See `sessions.py`.
* **The cookie is signed with `SESSION_SECRET`, not with the password.** The two
  used to be the same value, which meant rotating the password was the only way
  to sign everyone out — the opposite of what an operator wants. Rotating the
  password now logs nobody out; rotating the session secret does, and that is
  the tool the operator reaches for when a session needs killing.
* **Constant-time comparisons.** The password and the signature are compared
  with `hmac.compare_digest`, so a wrong guess cannot be discovered by timing
  the response.
"""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass, field
from threading import Lock

from app.sessions import Session, SessionCodec

__all__ = ["AuthGate"]

# How long a wrong password is remembered when counting attempts.
_FAILURE_WINDOW_SECONDS = 15 * 60


@dataclass
class AuthGate:
    """Checks the shared password and mints the fallback session cookie."""

    password: str
    session_max_age_seconds: int = 30 * 24 * 60 * 60
    max_attempts: int = 8
    codec: SessionCodec = field(default_factory=lambda: SessionCodec(b""))
    _failures: dict[str, list[float]] = field(default_factory=dict, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    @property
    def enabled(self) -> bool:
        """Whether a password is required. False means the app is open."""
        return bool(self.password)

    def check_password(self, candidate: str) -> bool:
        """Whether `candidate` is the shared password."""
        if not self.enabled:
            return True
        return hmac.compare_digest(candidate.encode(), self.password.encode())

    def issue_session(self) -> str:
        """Mint the cookie for a visitor who typed the right password."""
        return self.codec.issue_password(ttl_seconds=self.session_max_age_seconds)

    def session_from_cookie(self, token: str | None) -> Session | None:
        """Verify a cookie and accept it only if it is this gate's kind.

        A cookie minted for a signed-in Google person is *not* accepted here:
        this gate is a fallback, and mixing the two would mean the password
        could open a session that has a person attached to it.
        """
        session = self.codec.read(token)
        if session is None or session.kind != "password":
            return None
        return session

    def is_locked_out(self, client: str) -> bool:
        """Whether this client has used up its wrong-password allowance."""
        with self._lock:
            self._prune()
            return len(self._failures.get(client, [])) >= self.max_attempts

    def register_failure(self, client: str) -> None:
        """Record one wrong password for `client`."""
        with self._lock:
            now = time.time()
            attempts = [
                stamp
                for stamp in self._failures.get(client, [])
                if now - stamp < _FAILURE_WINDOW_SECONDS
            ]
            attempts.append(now)
            self._failures[client] = attempts
            self._prune()

    def clear_failures(self, client: str) -> None:
        """Forget the wrong passwords for `client` after a success."""
        with self._lock:
            self._failures.pop(client, None)

    def _prune(self) -> None:
        """Drop failure records that have aged out, so the map stays small."""
        now = time.time()
        stale = [
            client
            for client, stamps in self._failures.items()
            if not [stamp for stamp in stamps if now - stamp < _FAILURE_WINDOW_SECONDS]
        ]
        for client in stale:
            self._failures.pop(client, None)
