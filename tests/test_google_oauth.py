"""The Google sign-in client, tested against a fake transport.

No network: a fake transport pins the JSON that goes out, and every shape the
answer can come back in is exercised. The client secret is the one thing that
must never appear anywhere it can escape to, so it is asserted against on every
path.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.services.google_oauth import (
    GoogleOAuthClient,
    GoogleOAuthError,
    GoogleProfile,
)

CLIENT_ID = "client-id"
CLIENT_SECRET = "client-secret"
REDIRECT = "https://example.com/auth/callback"
SUB = "109876543210987654321"


def client_with(handler: Any) -> GoogleOAuthClient:
    """A client whose traffic goes to `handler` instead of to Google."""
    return GoogleOAuthClient(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=httpx.MockTransport(handler),
    )


def token_then_profile(
    token_body: object, *, profile_body: object | None = None, status: int = 200
) -> Any:
    """A transport that answers the token call and then the userinfo call."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/userinfo"):
            return httpx.Response(
                status,
                json=profile_body
                if profile_body is not None
                else {
                    "sub": SUB,
                    "email": "someone@example.com",
                    "name": "Someone",
                    "picture": "https://example.com/pic.png",
                    "email_verified": True,
                },
            )
        return httpx.Response(status, json=token_body)

    return handler


class TestTheExchange:
    def test_the_happy_path_returns_who_google_says_it_is(self) -> None:
        client = client_with(token_then_profile({"access_token": "a-token"}))
        profile = client.exchange_code("the-code", redirect_uri=REDIRECT)

        assert profile == GoogleProfile(
            sub=SUB,
            email="someone@example.com",
            name="Someone",
            picture="https://example.com/pic.png",
            email_verified=True,
        )

    def test_the_request_carries_everything_the_grant_needs(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/userinfo"):
                seen["url"] = str(request.url)
                seen["body"] = request.read().decode()
            return token_then_profile({"access_token": "a-token"})(request)

        client = client_with(handler)
        client.exchange_code("the-code", redirect_uri=REDIRECT)

        assert seen["url"] == "https://oauth2.googleapis.com/token"
        assert "grant_type=authorization_code" in seen["body"]
        assert "code=the-code" in seen["body"]
        assert "redirect_uri=https%3A%2F%2Fexample.com%2Fauth%2Fcallback" in seen["body"]
        assert f"client_id={CLIENT_ID}" in seen["body"]
        # The secret is in the body, never in the URL.
        assert CLIENT_SECRET in seen["body"]
        assert CLIENT_SECRET not in seen["url"]

    def test_a_refused_code_raises_with_googles_reason(self) -> None:
        client = client_with(
            token_then_profile({"error": "invalid_grant", "error_description": "Code expired"})
        )
        with pytest.raises(GoogleOAuthError, match="Code expired"):
            client.exchange_code("stale-code", redirect_uri=REDIRECT)

    def test_a_token_response_without_a_token_is_refused(self) -> None:
        client = client_with(token_then_profile({"id_token": "no-access-token"}))
        with pytest.raises(GoogleOAuthError, match="access token"):
            client.exchange_code("the-code", redirect_uri=REDIRECT)

    def test_a_profile_without_a_sub_is_refused(self) -> None:
        client = client_with(token_then_profile({"access_token": "a-token"}, profile_body={}))
        with pytest.raises(GoogleOAuthError, match="who this account belongs to"):
            client.exchange_code("the-code", redirect_uri=REDIRECT)

    def test_a_non_json_answer_is_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/userinfo"):
                return httpx.Response(200, text="<html>not json</html>")
            return httpx.Response(200, json={"access_token": "a-token"})

        client = client_with(handler)
        with pytest.raises(GoogleOAuthError, match="not JSON"):
            client.exchange_code("the-code", redirect_uri=REDIRECT)

    def test_an_http_error_names_its_status_without_the_secret(self) -> None:
        client = client_with(token_then_profile({}, status=400))
        with pytest.raises(GoogleOAuthError) as raised:
            client.exchange_code("the-code", redirect_uri=REDIRECT)
        assert CLIENT_SECRET not in str(raised.value)

    def test_a_transport_failure_is_caught_and_cleaned(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = client_with(handler)
        with pytest.raises(GoogleOAuthError, match="Google sign-in failed"):
            client.exchange_code("the-code", redirect_uri=REDIRECT)

    def test_the_secret_is_redacted_from_every_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "error": "invalid_client",
                    "error_description": f"Bad secret: {CLIENT_SECRET}",
                },
            )

        client = client_with(handler)
        with pytest.raises(GoogleOAuthError) as raised:
            client.exchange_code("the-code", redirect_uri=REDIRECT)
        assert CLIENT_SECRET not in str(raised.value)
        assert "***REDACTED***" in str(raised.value)


class TestTheAuthorizationUrl:
    def test_it_carries_every_parameter_google_documents(self) -> None:
        client = client_with(lambda _request: httpx.Response(200, json={}))
        url = client.authorization_url(state="the-state", redirect_uri=REDIRECT)

        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        for fragment in (
            f"client_id={CLIENT_ID}",
            "redirect_uri=https%3A%2F%2Fexample.com%2Fauth%2Fcallback",
            "response_type=code",
            "scope=openid+email+profile",
            "state=the-state",
        ):
            assert fragment in url

    def test_the_secret_is_never_in_the_url(self) -> None:
        client = client_with(lambda _request: httpx.Response(200, json={}))
        assert CLIENT_SECRET not in client.authorization_url(state="s", redirect_uri=REDIRECT)
