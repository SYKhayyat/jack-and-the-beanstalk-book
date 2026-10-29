"""Google sign-in: the authorization-code flow, written out by hand.

Why hand-rolled rather than through `authlib` or `google-auth`: the flow this
app needs is two requests — swap a code for a token, then ask who the token
belongs to. A library that implements the whole OIDC surface brings its own
session machinery, its own redirect handling and its own opinions about what
state is; what this app wants is the smallest, most testable seam, which is a
class with one method that turns a code into a person, and a fake transport in
the tests to prove it.

The pieces that are load-bearing:

* **`state` is checked against a signed cookie.** Google sends it back, and it
  must match the one this app handed out when it redirected away. Without that
  check anyone can trigger a sign-in on someone else's behalf.
* **The client secret is never in a URL.** It travels in the POST body to the
  token endpoint, over TLS, and it is redacted from every error string.
* **No retry loop.** Two calls at most; a failure fails loudly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.redaction import redact

__all__ = ["AUTH_URL", "SCOPES", "GoogleOAuthClient", "GoogleOAuthError", "GoogleProfile"]

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# Identity only: who you are, and the email you sign in with. Nothing that
# would let the app act as you anywhere else.
SCOPES = ("openid", "email", "profile")

_HTTP_TIMEOUT = 30.0


class GoogleOAuthError(RuntimeError):
    """Google refused the exchange, or answered with something unusable."""


@dataclass(frozen=True, slots=True)
class GoogleProfile:
    """Who Google says the signed-in person is.

    `sub` is the only stable field: it never changes for a person, which is why
    it is what books get filed under. `email_verified` is carried because an
    unverified email is a claim, not an identity.
    """

    sub: str
    email: str
    name: str
    picture: str
    email_verified: bool = False


class GoogleOAuthClient:
    """The two-request dance that turns a redirect back into a person."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = _HTTP_TIMEOUT,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        # httpx does not retry on its own: one call, one request.
        self._client = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=True,
        )

    def authorization_url(
        self, *, state: str, redirect_uri: str, prompt: str = "select_account"
    ) -> str:
        """The URL to send the visitor to, with everything already agreed.

        `access_type=online` (the default) is deliberate: this app does not
        need to act on the person's behalf when they are not there, and an
        offline refresh token is a long-lived credential nobody asked for.
        """
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "state": state,
                # `select_account` rather than `consent`: a returning visitor
                # who happens to be signed into several Google accounts should
                # not silently become the wrong one.
                "prompt": prompt,
            }
        )
        return f"{AUTH_URL}?{query}"

    def exchange_code(self, code: str, *, redirect_uri: str) -> GoogleProfile:
        """Trade the code for a token, then ask Google who it belongs to.

        Args:
            code: The one-time code Google redirects back with.
            redirect_uri: Exactly the one used in `authorization_url`. Google
                rejects an exchange when the two disagree, which is the
                documented defence against a code captured on a different path.

        Raises:
            GoogleOAuthError: if Google refuses, or answers with something this
                app cannot use.
        """
        token = self._token(code, redirect_uri=redirect_uri)
        access_token = token.get("access_token")
        if not access_token:
            raise GoogleOAuthError(
                "Google did not return an access token, so the sign-in cannot be completed."
            )
        return self._profile(str(access_token))

    # --- the two requests ------------------------------------------------------

    def _token(self, code: str, *, redirect_uri: str) -> dict[str, Any]:
        try:
            response = self._client.post(
                TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self._client_id,
                    # The secret goes in the body, over TLS, and never in a URL
                    # a proxy or a browser history might keep.
                    "client_secret": self._client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise GoogleOAuthError(_clean(exc, self._client_secret)) from None

        body = _json(response)
        # A refusal comes back as a 4xx, but a JSON body carrying an `error`
        # is a refusal whatever the status: the reason is what the operator
        # needs, not the code path it arrived by.
        if response.status_code >= 400 or body.get("error"):
            raise GoogleOAuthError(_clean(body, self._client_secret))
        return body

    def _profile(self, access_token: str) -> GoogleProfile:
        try:
            response = self._client.get(
                USERINFO_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise GoogleOAuthError(_clean(exc, self._client_secret)) from None

        body = _json(response)
        if response.status_code >= 400:
            raise GoogleOAuthError(_clean(body, self._client_secret))

        sub = body.get("sub")
        if not sub:
            raise GoogleOAuthError("Google did not say who this account belongs to.")
        return GoogleProfile(
            sub=str(sub),
            email=str(body.get("email", "")),
            name=str(body.get("name", "")),
            picture=str(body.get("picture", "")),
            email_verified=bool(body.get("email_verified", False)),
        )


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body: object = response.json()
    except ValueError as exc:
        raise GoogleOAuthError("Google returned a response that was not JSON.") from exc
    if not isinstance(body, dict):
        raise GoogleOAuthError("Google returned an unexpected response shape.")
    return body


def _clean(detail: object, secret: str) -> str:
    """One short line an operator can act on, with the secret nowhere in it."""
    if isinstance(detail, dict):
        detail = detail.get("error_description") or detail.get("error") or detail
    text = redact(str(detail), (secret,))
    if len(text) > 300:
        text = text[:300] + "..."
    return f"Google sign-in failed: {text}"
