"""The sign-in gate: the login page, and the guard in front of every route.

Two ways in, and exactly one of them is ever available at a time:

* **Google**, when a client is configured. This is the production way in: a real
  person, with an identity, so a book can be theirs and only theirs.
* **The shared password**, when it is not. Local development, offline tests, and
  a deployment that has not been wired up yet.

The presence of a Google client switches the password off, deliberately: two
simultaneous ways in would mean two kinds of session, and the kind that carries
no identity would always be the weaker link.

Two shapes of refusal, deliberately:

* **HTML** requests are redirected to the login page, so a human who opens the
  app in a browser sees a button instead of a JSON error.
* **API and media** requests get a 401 in the app's usual error shape, so a
  script gets something it can read.

The login page, the static assets, the health check and the two OAuth endpoints
stay open. Everything else needs a session.
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated, cast
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import RequestResponseEndpoint

from app.auth import AuthGate
from app.services.google_oauth import GoogleOAuthClient, GoogleOAuthError
from app.services.user_store import User, UserStore
from app.sessions import SESSION_COOKIE, Session, SessionCodec

__all__ = [
    "CurrentSessionDep",
    "GateDep",
    "get_gate",
    "get_oauth",
    "get_sessions",
    "get_user_store",
    "install_auth_gate",
    "router",
]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# Paths that stay open: the login page, the health check the platform polls, the
# favicon, and the two endpoints of the redirect-based Google flow — one of them
# is where the session is granted, so it cannot require one.
_OPEN_PATHS = frozenset(
    {"/login", "/healthz", "/favicon.ico", "/auth/google/login", "/auth/callback"}
)
# Prefixes that stay open: the static assets the login page needs.
_OPEN_PREFIXES = ("/static/",)

# The state cookie remembers where the visitor was going, plus the random state
# Google will echo back. Short-lived: it only has to survive one round trip.
_STATE_COOKIE = "jb_oauth_state"
_STATE_MAX_AGE_SECONDS = 15 * 60

# What the login page may say when a sign-in fails. A closed set, so a message
# arriving in a URL parameter can only ever select a sentence this app wrote —
# never put its own wording on the page.
_LOGIN_ERRORS = {
    "google_failed": "That sign-in did not work. Please try again.",
    "google_state": "That sign-in link had expired. Please start again.",
}


# --- dependency accessors -----------------------------------------------------


def get_gate(request: Request) -> AuthGate:
    """The password gate attached to this app at startup."""
    return cast(AuthGate, request.app.state.gate)


def get_sessions(request: Request) -> SessionCodec:
    """The cookie signer attached to this app at startup."""
    return cast(SessionCodec, request.app.state.sessions)


def get_oauth(request: Request) -> GoogleOAuthClient | None:
    """The Google client, or None when this deployment has no Google sign-in.

    Routes check `settings.google_enabled` first and redirect to `/login` when
    it is off, so a None here is a configuration mismatch rather than a
    request the visitor can reach.
    """
    return cast("GoogleOAuthClient | None", getattr(request.app.state, "oauth", None))


def get_user_store(request: Request) -> UserStore:
    """The store of everyone who has signed in."""
    return cast(UserStore, request.app.state.container.user_store)


def get_current_session(
    request: Request, sessions: Annotated[SessionCodec, Depends(get_sessions)]
) -> Session | None:
    """The session this request carries, or None when it has none.

    A route that needs a person reads the session and answers 401 when there is
    nobody behind the request; a route that only wants to know reads it here.
    """
    return sessions.read(request.cookies.get(SESSION_COOKIE))


GateDep = Annotated[AuthGate, Depends(get_gate)]
SessionsDep = Annotated[SessionCodec, Depends(get_sessions)]
UserStoreDep = Annotated[UserStore, Depends(get_user_store)]
CurrentSessionDep = Annotated[Session | None, Depends(get_current_session)]


# --- the login page -----------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, gate: GateDep) -> Response:
    """Show the way in: a Google button, a password form, or nothing at all.

    If nothing is configured the gate is wide open, so there is nothing to log
    into and the visitor is sent straight through.
    """
    settings = request.app.state.settings
    if not settings.auth_enabled:
        return RedirectResponse("/", status_code=303)

    templates: Jinja2Templates = request.app.state.templates
    next_path = _safe_next(request.query_params.get("next"))
    error = _LOGIN_ERRORS.get(request.query_params.get("error", ""))
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": error,
            "next_path": next_path,
            # Which way in is on offer. The password form is offered only when
            # Google is absent: with Google configured, the password is off.
            "google_enabled": settings.google_enabled,
            "password_enabled": gate.enabled and not settings.google_enabled,
        },
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    gate: GateDep,
    password: str = Form(default=""),
    next_path: str = Form(default=""),
) -> Response:
    """Check the password and, if it is right, hand out a session cookie."""
    settings = request.app.state.settings
    # With Google configured the password is not a way in any more, so there is
    # nothing for this form to do.
    if settings.google_enabled:
        return RedirectResponse("/login", status_code=303)

    client = _client_key(request)

    if gate.is_locked_out(client):
        logger.info("auth.locked_out client=%s", client)
        return _login_error(request, "Too many wrong passwords. Try again in a few minutes.")

    if gate.check_password(password):
        gate.clear_failures(client)
        logger.info("auth.login_ok client=%s (shared password)", client)
        response = RedirectResponse(_safe_next(next_path), status_code=303)
        _set_session(
            response,
            gate.issue_session(),
            max_age_seconds=gate.session_max_age_seconds,
            secure=settings.is_production,
        )
        return response

    gate.register_failure(client)
    logger.info("auth.login_failed client=%s", client)
    return _login_error(request, "That password is not right.", next_path)


# --- the Google flow ----------------------------------------------------------


@router.get("/auth/google/login")
def google_login(
    request: Request,
    sessions: SessionsDep,
    oauth: Annotated[GoogleOAuthClient | None, Depends(get_oauth)],
) -> Response:
    """Send the visitor to Google, remembering where to bring them back to."""
    settings = request.app.state.settings
    if not settings.google_enabled or oauth is None:
        return RedirectResponse("/login", status_code=303)

    state = secrets.token_urlsafe(32)
    next_path = _safe_next(request.query_params.get("next"))
    logger.info("auth.google_redirect next=%s", next_path)

    response = RedirectResponse(
        oauth.authorization_url(
            state=state,
            redirect_uri=_redirect_uri(request),
        ),
        status_code=303,
    )
    # The state and the intended destination are signed into a cookie: when
    # Google sends the visitor back, this app checks the echoed state against
    # it, which is what stops someone else from triggering the sign-in.
    response.set_cookie(
        _STATE_COOKIE,
        sessions.issue_state(state, next_path),
        max_age=_STATE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        path="/",
    )
    return response


@router.get("/auth/callback")
def google_callback(
    request: Request,
    sessions: SessionsDep,
    users: UserStoreDep,
    gate: GateDep,
    oauth: Annotated[GoogleOAuthClient | None, Depends(get_oauth)],
) -> Response:
    """Finish the sign-in: check the state, trade the code for a person."""
    settings = request.app.state.settings
    code = request.query_params.get("code", "")
    echoed_state = request.query_params.get("state", "")

    if request.query_params.get("error"):
        # Google's own refusal (the visitor pressed Cancel, say). Checked before
        # the state: a refusal carries no code, and it is a legitimate end to
        # the round trip rather than a forged one.
        logger.info("auth.google_cancelled detail=%s", request.query_params.get("error"))
        remembered = sessions.read_state(request.cookies.get(_STATE_COOKIE))
        return RedirectResponse(_safe_next(remembered[1] if remembered else "/"), 303)

    remembered = sessions.read_state(request.cookies.get(_STATE_COOKIE))
    if remembered is None or not code or not echoed_state or echoed_state != remembered[0]:
        logger.info("auth.google_state_mismatch")
        return RedirectResponse("/login?error=google_state", status_code=303)
    next_path = _safe_next(remembered[1])

    if oauth is None:
        return RedirectResponse("/login", status_code=303)

    try:
        profile = oauth.exchange_code(code, redirect_uri=_redirect_uri(request))
    except GoogleOAuthError as exc:
        # The reason is logged, never shown: it names Google's endpoints and
        # can carry detail about this deployment's client.
        logger.warning("auth.google_failed reason=%s", exc)
        return RedirectResponse("/login?error=google_failed", status_code=303)

    user = users.upsert(
        User(
            sub=profile.sub,
            email=profile.email,
            name=profile.name,
            picture=profile.picture,
        )
    )
    logger.info(
        "auth.google_login_ok sub=%s email=%s",
        user.sub,
        _mask_email(user.email),
    )

    response = RedirectResponse(next_path, status_code=303)
    _set_session(
        response,
        sessions.issue_google(
            sub=user.sub,
            name=user.name or user.email,
            ttl_seconds=gate.session_max_age_seconds,
        ),
        max_age_seconds=gate.session_max_age_seconds,
        secure=settings.is_production,
    )
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    """Forget the session and send the visitor back to the way in."""
    settings = request.app.state.settings
    target = "/login" if settings.auth_enabled else "/"
    response = RedirectResponse(target, status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    logger.info("auth.logout")
    return response


# --- the guard ----------------------------------------------------------------


def install_auth_gate(app: FastAPI, gate: AuthGate) -> None:
    """Put the guard in front of every route.

    A no-op when nothing is configured, so local development needs no ceremony
    at all. When Google *is* configured it is the only way through: a password
    session no longer opens anything.
    """

    @app.middleware("http")
    async def _guard(request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = app.state.settings
        if not settings.auth_enabled or _is_open_path(request.url.path):
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE)
        if settings.google_enabled:
            session = app.state.sessions.read(token)
            if session is not None and session.is_person:
                return await call_next(request)
        elif gate.session_from_cookie(token) is not None:
            return await call_next(request)

        return _refuse(request)


def _refuse(request: Request) -> Response:
    """Turn away a request that has no valid session."""
    path = request.url.path
    # Machine-facing surfaces get JSON 401, never an HTML redirect: a script
    # polling for bytes must see "log in first", not a login page. Keep this
    # list equal to every machine-facing prefix (media, /api, /book-media).
    if path.startswith("/api") or path.startswith("/media") or path.startswith("/book-media"):
        return JSONResponse(
            status_code=401,
            content={
                "error": "sign_in_required",
                "detail": "This app needs you to sign in first. Open /login in a browser.",
            },
        )
    target = quote(path, safe="/") if path.startswith("/") else "/"
    return RedirectResponse(f"/login?next={target}", status_code=303)


def _redirect_uri(request: Request) -> str:
    """Where Google sends the visitor back to.

    `GOOGLE_REDIRECT_URI` when it is set, so the operator controls the exact
    string registered in the console. Otherwise it is derived from the request,
    which is only right for local development — behind a proxy the derived host
    is the internal one, and Google refuses a redirect it was not given.
    """
    configured = request.app.state.settings.google_redirect_uri
    if configured:
        return str(configured)
    return f"{request.url.scheme}://{request.url.netloc}/auth/callback"


# --- helpers ------------------------------------------------------------------


def _set_session(
    response: Response, value: str, *, max_age_seconds: int, secure: bool
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def _mask_email(email: str) -> str:
    """A login trail that names the account without publishing the address."""
    local, _, domain = email.partition("@")
    if not local or not domain:
        return "-"
    shown = local[0] + "***"
    return f"{shown}@{domain}"


def _login_error(request: Request, message: str, next_path: str | None = None) -> Response:
    """Re-render the login page with an error, keeping the intended target."""
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": message,
            "next_path": _safe_next(next_path or request.query_params.get("next")),
            "google_enabled": request.app.state.settings.google_enabled,
            "password_enabled": not request.app.state.settings.google_enabled,
        },
        status_code=401,
    )


def _is_open_path(path: str) -> bool:
    return path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES)


def _safe_next(raw: str | None) -> str:
    """Only ever redirect to a path on this site.

    A `next` of `//evil.example` or `https://evil.example` is an open
    redirect: the visitor would be bounced off-site right after signing in.
    So is `/<backslash>evil.example`: browsers treat a backslash as a slash for
    http(s), so the emitted `Location: /\\evil.example` is read as the
    protocol-relative `//evil.example`. Parse rather than prefix-match.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    # A backslash or a control character can turn a same-site path into a
    # different host (or split the header) once a browser or proxy reads it.
    if "\\" in raw or any(char in raw for char in "\r\n\t\x00"):
        return "/"
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return "/"
    return raw


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"
