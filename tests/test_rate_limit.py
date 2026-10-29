"""The per-client billable-image brake, tested directly."""

from __future__ import annotations

import time

from app.services.rate_limit import GenerationRateLimiter


def test_a_client_is_forgotten_once_its_window_has_expired() -> None:
    """The map must track clients seen this window, not every client ever."""
    limiter = GenerationRateLimiter(5, window_seconds=0.01)
    for index in range(200):
        assert limiter.allow(f"10.0.0.{index}")
    assert limiter.tracked_clients > 1

    time.sleep(0.02)
    assert limiter.allow("new-client")

    assert limiter.tracked_clients == 1, "aged-out clients were not swept"
    # The surviving client still has its full allowance (one token spent).
    assert limiter.allow("new-client")


def test_a_client_charged_after_its_window_is_still_tracked() -> None:
    """The sweep must not drop the client it is about to charge.

    Dropping the key after trimming its deque would append the new charge to a
    detached deque, silently resetting that client's allowance.
    """
    limiter = GenerationRateLimiter(1, window_seconds=0.01)
    assert limiter.allow("same-client")
    time.sleep(0.02)

    # This call sweeps (the window elapsed) and charges the same client again.
    assert limiter.allow("same-client")
    assert limiter.tracked_clients == 1
    assert not limiter.allow("same-client"), "the second charge must be real"


def test_other_clients_do_not_refill_a_full_client() -> None:
    limiter = GenerationRateLimiter(2, window_seconds=60.0)
    assert limiter.allow("busy")
    assert limiter.allow("busy")
    assert not limiter.allow("busy")

    for index in range(20):
        limiter.allow(f"other-{index}")

    assert not limiter.allow("busy")
