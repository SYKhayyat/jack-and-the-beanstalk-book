"""The password gate.

Two halves: the gate itself (password check, signed cookie, lockout) and the
guard in front of the routes. Both are tested from the outside — through the
HTTP surface — because that is what a visitor actually meets.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import SESSION_COOKIE, AuthGate
from app.config import Settings
from app.main import create_app
from tests.helpers import RecordingProvider

PASSWORD = "let-me-in"


@pytest.fixture
def locked_settings(tmp_path: Path) -> Settings:
    """Settings with a password set: the app is no longer open."""
    return Settings(
        image_provider="mock",  # type: ignore[arg-type]
        gemini_api_key="",
        app_env="test",
        storage_dir=tmp_path / "storage",
        app_password=PASSWORD,
    )


@pytest.fixture
def locked_client(
    locked_settings: Settings, provider: RecordingProvider
) -> Iterator[TestClient]:
    """A client for an app that requires a password."""
    app = create_app(locked_settings, provider=provider)
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, password: str = PASSWORD, **extra: object) -> object:
    return client.post(
        "/login",
        data={"password": password, "next_path": "/", **extra},
        follow_redirects=False,
    )


# --- the gate ----------------------------------------------------------------


def test_no_password_leaves_the_app_open() -> None:
    gate = AuthGate(password="")
    assert gate.enabled is False
    assert gate.check_password("anything") is True
    assert gate.session_valid("nonsense") is True


def test_wrong_password_is_refused() -> None:
    gate = AuthGate(password=PASSWORD)
    assert gate.check_password("wrong") is False
    assert gate.check_password(PASSWORD) is True


def test_session_token_round_trips() -> None:
    gate = AuthGate(password=PASSWORD)
    token = gate.issue_session()
    assert gate.session_valid(token) is True
    assert gate.session_valid("") is False
    assert gate.session_valid("garbage") is False
    assert gate.session_valid("1.deadbeef") is False


def test_changing_the_password_invalidates_outstanding_sessions() -> None:
    token = AuthGate(password=PASSWORD).issue_session()
    assert AuthGate(password="a-new-password").session_valid(token) is False


def test_tampered_cookie_is_refused() -> None:
    gate = AuthGate(password=PASSWORD)
    token = gate.issue_session()
    assert gate.session_valid(token + "x") is False


def test_lockout_after_too_many_wrong_passwords() -> None:
    gate = AuthGate(password=PASSWORD, max_attempts=3)
    for _ in range(3):
        gate.register_failure("1.2.3.4")
    assert gate.is_locked_out("1.2.3.4") is True
    assert gate.is_locked_out("5.6.7.8") is False
    gate.clear_failures("1.2.3.4")
    assert gate.is_locked_out("1.2.3.4") is False


# --- the guard ---------------------------------------------------------------


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
    assert response.json()["error"] == "password_required"


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


def test_login_cannot_be_used_to_redirect_off_site(locked_client: TestClient) -> None:
    response = _login(locked_client, next_path="https://evil.example/steal")
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
