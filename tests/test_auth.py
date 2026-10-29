"""The sign-in gate: the password fallback, the Google path, and the guard.

Both halves are tested from the outside — through the HTTP surface — because
that is what a visitor actually meets. The Google path runs against a fake
transport, so the suite stays offline: the request that goes out is pinned, and
every shape Google can answer with is exercised.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import AuthGate
from app.config import Settings
from app.main import create_app
from app.services.google_oauth import (
    GoogleOAuthClient,
    GoogleOAuthError,
    GoogleProfile,
)
from app.services.user_store import FileUserStore, User
from app.sessions import SESSION_COOKIE, SessionCodec
from tests.helpers import RecordingProvider

PASSWORD = "let-me-in"

GOOGLE_ID = "google-client-id"
GOOGLE_SECRET = "google-client-secret"
SUB = "109876543210987654321"


@pytest.fixture
def locked_settings(tmp_path: Path) -> Settings:
    """Settings with a password set: the app is no longer open."""
    return Settings(
        image_provider="mock",  # type: ignore[arg-type]
        gemini_api_key="",
        app_env="test",
        storage_dir=tmp_path / "storage",
        app_password=PASSWORD,
        session_secret="test-session-secret",
    )


@pytest.fixture
def google_settings(tmp_path: Path) -> Settings:
    """Settings with a Google client: the password is switched off."""
    return Settings(
        image_provider="mock",  # type: ignore[arg-type]
        gemini_api_key="",
        app_env="test",
        storage_dir=tmp_path / "storage",
        app_password="",
        google_client_id=GOOGLE_ID,
        google_client_secret=GOOGLE_SECRET,
        google_redirect_uri="http://testserver/auth/callback",
        session_secret="test-session-secret",
    )


@pytest.fixture
def locked_client(
    locked_settings: Settings, provider: RecordingProvider
) -> Iterator[TestClient]:
    """A client for an app that requires the shared password."""
    app = create_app(locked_settings, provider=provider)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def google_client(
    google_settings: Settings, provider: RecordingProvider
) -> Iterator[TestClient]:
    """A client for an app that signs people in with Google."""
    app = create_app(google_settings, provider=provider)
    app.state.oauth = _FakeGoogle()
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, password: str = PASSWORD, **extra: object) -> httpx.Response:
    return client.post(
        "/login",
        data={"password": password, "next_path": "/", **extra},
        follow_redirects=False,
    )


# --- the password fallback itself ---------------------------------------------


def test_no_password_leaves_the_app_open() -> None:
    gate = AuthGate(password="")
    assert gate.enabled is False
    assert gate.check_password("anything") is True
    # The gate itself never hands out a session when it is not enabled: the
    # guard does not ask for one, so there is nothing to mint.
    assert gate.session_from_cookie("nonsense") is None


def test_wrong_password_is_refused() -> None:
    gate = AuthGate(password=PASSWORD)
    assert gate.check_password("wrong") is False
    assert gate.check_password(PASSWORD) is True


def test_session_token_round_trips() -> None:
    codec = SessionCodec(b"key")
    gate = AuthGate(password=PASSWORD, codec=codec)
    token = gate.issue_session()
    assert gate.session_from_cookie(token) is not None
    assert gate.session_from_cookie("") is None
    assert gate.session_from_cookie("garbage") is None
    assert gate.session_from_cookie("1.deadbeef") is None


def test_tampered_cookie_is_refused() -> None:
    codec = SessionCodec(b"key")
    gate = AuthGate(password=PASSWORD, codec=codec)
    token = gate.issue_session()
    assert gate.session_from_cookie(token + "x") is None


def test_rotating_the_password_does_not_sign_anyone_out() -> None:
    """The session key is a setting of its own, decoupled from the password.

    They used to be the same value, which made the password the only way to
    kill every outstanding session — so rotating a password for any reason
    also logged everyone out. Now rotating the password changes nothing, and
    rotating `SESSION_SECRET` is the tool that does.
    """
    codec = SessionCodec(b"key")
    token = AuthGate(password=PASSWORD, codec=codec).issue_session()
    assert AuthGate(password="a-new-password", codec=codec).session_from_cookie(token)


def test_a_google_session_is_not_accepted_by_the_password_gate() -> None:
    """One way in at a time: the fallback gate must not open a person's session."""
    codec = SessionCodec(b"key")
    google_session = codec.issue_google(sub=SUB, name="Someone", ttl_seconds=3600)
    gate = AuthGate(password=PASSWORD, codec=codec)
    assert gate.session_from_cookie(google_session) is None


def test_lockout_after_too_many_wrong_passwords() -> None:
    gate = AuthGate(password=PASSWORD, max_attempts=3)
    for _ in range(3):
        gate.register_failure("1.2.3.4")
    assert gate.is_locked_out("1.2.3.4") is True
    assert gate.is_locked_out("5.6.7.8") is False
    gate.clear_failures("1.2.3.4")
    assert gate.is_locked_out("1.2.3.4") is False


# --- the guard, with the password ---------------------------------------------


def test_app_is_reachable_without_a_password(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200


def test_html_request_is_redirected_to_the_login_page(locked_client: TestClient) -> None:
    response = locked_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_api_request_is_refused_with_a_401(locked_client: TestClient) -> None:
    response = locked_client.get("/api/settings-options")
    assert response.status_code == 401
    assert response.json()["error"] == "sign_in_required"


def test_book_media_request_is_refused_with_a_401(locked_client: TestClient) -> None:
    response = locked_client.get("/book-media/nope/pdf")
    assert response.status_code == 401


def test_health_check_and_assets_stay_open(locked_client: TestClient) -> None:
    assert locked_client.get("/healthz").status_code == 200
    assert locked_client.get("/static/style.css").status_code == 200


def test_login_page_is_reachable(locked_client: TestClient) -> None:
    response = locked_client.get("/login")
    assert response.status_code == 200
    assert "password" in response.text.lower()


def test_wrong_password_re_renders_the_form(locked_client: TestClient) -> None:
    response = _login(locked_client, password="wrong")
    assert response.status_code == 401
    assert "not right" in response.text


def test_right_password_hands_out_a_session(locked_client: TestClient) -> None:
    response = _login(locked_client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    cookie = response.headers["set-cookie"]
    assert SESSION_COOKIE in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie

    assert locked_client.get("/").status_code == 200
    assert locked_client.get("/api/settings-options").status_code == 200


def test_logout_drops_the_session(locked_client: TestClient) -> None:
    _login(locked_client)
    assert locked_client.get("/").status_code == 200

    response = locked_client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert locked_client.get("/", follow_redirects=False).status_code == 303


def test_forged_cookie_does_not_get_in(locked_client: TestClient) -> None:
    locked_client.cookies.set(SESSION_COOKIE, "9999999999.forged")
    assert locked_client.get("/", follow_redirects=False).status_code == 303
    assert locked_client.get("/api/settings-options").status_code == 401


def test_expired_session_is_refused() -> None:
    codec = SessionCodec(b"key")
    token = codec.issue_password(ttl_seconds=1)
    assert codec.read(token, now=10_000_000_000) is None
    assert codec.read(token, now=0) is not None


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example/steal",
        "//evil.example/steal",
        # Browsers normalise a backslash to a slash for http(s), so this
        # `Location` header is read as the protocol-relative `//evil.example`.
        "/\\evil.example/steal",
        "\\\\evil.example/steal",
        "/\t/evil.example/steal",
    ],
)
def test_login_cannot_be_used_to_redirect_off_site(
    locked_client: TestClient, hostile: str
) -> None:
    response = _login(locked_client, next_path=hostile)
    assert response.headers["location"] == "/"


def test_login_keeps_the_intra_site_target(locked_client: TestClient) -> None:
    response = _login(locked_client, next_path="/api/settings-options")
    assert response.headers["location"] == "/api/settings-options"


def test_lockout_blocks_further_logins(locked_client: TestClient) -> None:
    for _ in range(8):
        assert _login(locked_client, password="wrong").status_code == 401
    locked = _login(locked_client, password="wrong")
    assert locked.status_code == 401
    assert "Too many" in locked.text


# --- the guard, with Google configured ----------------------------------------

# A Google client switches the password off: the password form disappears, the
# password itself stops working, and a password-shaped session opens nothing.


def test_google_login_page_offers_no_password_form(google_client: TestClient) -> None:
    response = google_client.get("/login")
    assert response.status_code == 200
    assert "auth/google/login" in response.text
    assert 'action="/login"' not in response.text
    assert "Continue with Google" in response.text


def test_the_password_does_not_open_a_google_configured_app(
    google_client: TestClient,
) -> None:
    response = google_client.post(
        "/login", data={"password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert google_client.get("/api/settings-options").status_code == 401


def test_an_unsigned_in_visitor_is_redirected_from_pages(google_client: TestClient) -> None:
    response = google_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


def test_the_authorization_url_carries_everything_google_needs() -> None:
    """The one URL the whole flow hangs off: no missing parameter, no extra."""
    client = GoogleOAuthClient(client_id=GOOGLE_ID, client_secret=GOOGLE_SECRET)
    url = client.authorization_url(
        state="the-state", redirect_uri="https://example.com/auth/callback"
    )

    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=google-client-id" in url
    assert "redirect_uri=https%3A%2F%2Fexample.com%2Fauth%2Fcallback" in url
    assert "response_type=code" in url
    assert "scope=openid+email+profile" in url
    assert "state=the-state" in url
    # No secret in the query string: everything sensitive stays off the URL.
    assert GOOGLE_SECRET not in url


def test_the_redirect_away_remembers_where_to_come_back(google_client: TestClient) -> None:
    response = google_client.get(
        "/auth/google/login?next=/api/settings-options", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(
        "https://accounts.google.com/o/oauth2/v2/auth?"
    )
    # The state cookie is what the callback checks the echoed state against.
    assert "jb_oauth_state=" in response.headers["set-cookie"]
    remembered = google_client.app.state.sessions.read_state(
        google_client.cookies.get("jb_oauth_state")
    )
    assert remembered is not None
    assert remembered[1] == "/api/settings-options"


def test_a_callback_without_the_state_cookie_is_refused(google_client: TestClient) -> None:
    response = google_client.get("/auth/callback?code=abc&state=abc", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=google_state"
    assert google_client.cookies.get(SESSION_COOKIE) is None


def test_a_callback_with_the_wrong_state_is_refused(google_client: TestClient) -> None:
    google_client.get("/auth/google/login?next=/", follow_redirects=False)
    login = google_client.get(
        "/auth/callback?code=abc&state=not-the-state", follow_redirects=False
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/login?error=google_state"


def test_the_full_google_round_trip_signs_the_visitor_in(google_client: TestClient) -> None:
    """Redirect away, come back with a code, and end up signed in."""
    start = google_client.get(
        "/auth/google/login?next=/api/book/pages", follow_redirects=False
    )
    assert start.status_code == 303

    # The state Google echoes back is the one this app handed out, recovered
    # from the signed cookie so the test does not have to parse a URL.
    state = _state_from_cookie(google_client)
    response = google_client.get(
        f"/auth/callback?code=one-time-code&state={state}", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/api/book/pages"
    # The session is a person's, and it is the person Google described.
    token = google_client.cookies.get(SESSION_COOKIE)
    assert token
    assert _session_for(google_client, token) is not None

    assert google_client.get("/api/settings-options").status_code == 200


def test_a_sign_in_records_the_person_once(tmp_path: Path) -> None:
    store = FileUserStore(tmp_path / "users")

    first = store.upsert(User(sub=SUB, email="someone@example.com", name="Someone"))
    second = store.upsert(User(sub=SUB, email="someone@example.com", name="A New Name"))

    assert first.sub == second.sub
    assert second.name == "A New Name"
    # The first sign-in's timestamp is not overwritten by the second.
    assert first.created_at == second.created_at
    assert store.get(SUB) is not None
    assert store.get("nobody") is None


def test_a_path_traversal_sub_is_refused(tmp_path: Path) -> None:
    store = FileUserStore(tmp_path / "users")
    with pytest.raises(ValueError):
        store.upsert(User(sub="../../etc/passwd"))


def test_google_can_refuse_the_sign_in(google_client: TestClient) -> None:
    """Google answers with an error parameter: back to the login page."""
    google_client.get("/auth/google/login?next=/", follow_redirects=False)
    state = _state_from_cookie(google_client)
    response = google_client.get(
        f"/auth/callback?error=access_denied&state={state}", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert google_client.cookies.get(SESSION_COOKIE) is None


def test_a_failed_exchange_never_echoes_googles_reason(google_client: TestClient) -> None:
    """The page says "try again"; the reason stays in the server's log."""
    app = google_client.app
    app.state.oauth = _FailingGoogle()
    google_client.get("/auth/google/login?next=/", follow_redirects=False)
    state = _state_from_cookie(google_client)
    response = google_client.get(
        f"/auth/callback?code=code&state={state}", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=google_failed"
    assert "token" not in response.text
    assert google_client.cookies.get(SESSION_COOKIE) is None


class _FakeGoogle:
    """A Google that always succeeds, wired into the app under test."""

    def authorization_url(self, *, state: str, redirect_uri: str, prompt: str = "") -> str:
        assert redirect_uri == "http://testserver/auth/callback"
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}&fake=1"

    def exchange_code(self, code: str, *, redirect_uri: str) -> Any:
        assert code in {"one-time-code", "code"}
        return GoogleProfile(
            sub=SUB,
            email="someone@example.com",
            name="Someone",
            picture="https://example.com/pic.png",
            email_verified=True,
        )


class _FailingGoogle:
    """A Google that refuses, so the failure path is exercised."""

    def authorization_url(self, *, state: str, redirect_uri: str, prompt: str = "") -> str:
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    def exchange_code(self, code: str, *, redirect_uri: str) -> Any:
        raise GoogleOAuthError("invalid_grant: token exchange failed for the client")


def _state_from_cookie(client: TestClient) -> str:
    """Recover the state the app handed out, from the signed state cookie."""
    remembered = client.app.state.sessions.read_state(client.cookies.get("jb_oauth_state"))
    assert remembered is not None
    return remembered[0]


def _session_for(client: TestClient, token: str) -> object:
    """Verify the session cookie with the codec the app is actually running."""
    session = client.app.state.sessions.read(token)
    assert session is not None
    assert session.sub == SUB
    assert session.is_person
    return session
