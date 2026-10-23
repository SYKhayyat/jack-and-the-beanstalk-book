"""The shared-password gate.

One password, set with `APP_PASSWORD`, sits in front of the whole app. The
goal is not to withstand a determined attacker — it is to keep strangers off an
app where every image costs money, and to make sure the bill only ever goes to
someone who was invited.

Three deliberate simplifications:

* **No session store.** The cookie carries an expiry and an HMAC of that
  expiry, keyed by the password itself. Rotating the password invalidates
  every outstanding cookie, which is exactly what you want when you rotate it
  because it leaked.
* **One password, not accounts.** Nobody gets their own login, so nobody
  needs an invite, a reset flow or a database table.
* **Constant-time comparisons.** The password and the signature are compared
  with `hmac.compare_digest`, so a wrong guess cannot be discovered by timing
  the response.

When no password is configured the gate is open: the app is then a local
development toy, and says so in the log.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from threading import Lock

__all__ = ["SESSION_COOKIE", "AuthGate"]

SESSION_COOKIE = "jb_session"

# How long a wrong password is remembered when counting attempts.
_FAILURE_WINDOW_SECONDS = 15 * 60


@dataclass
class AuthGate:
    """Checks the shared password and signs the session cookie."""

    password: str
    session_max_age_seconds: int = 30 * 24 * 60 * 60
    max_attempts: int = 8
    _key: bytes = field(init=False, repr=False)
    _failures: dict[str, list[float]] = field(default_factory=dict, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        # The key is derived from the password, so changing the password
        # silently invalidates every cookie that was ever issued.
        self._key = hashlib.sha256(
            b"jack-and-the-beanstalk/session/" + self.password.encode()
        ).digest()

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
        """Mint a session token for the cookie."""
        expiry = int(time.time()) + self.session_max_age_seconds
        return f"{expiry}.{self._sign(expiry)}"

    def session_valid(self, token: str | None) -> bool:
        """Whether the cookie value is a live, correctly signed session."""
        if not self.enabled:
            return True
        if not token:
            return False
        expiry_text, _, signature = token.partition(".")
        try:
            expiry = int(expiry_text)
        except ValueError:
            return False
        if expiry <= int(time.time()):
            return False
        return hmac.compare_digest(signature, self._sign(expiry))

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

    def _sign(self, expiry: int) -> str:
        return hmac.new(self._key, str(expiry).encode(), hashlib.sha256).hexdigest()

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
