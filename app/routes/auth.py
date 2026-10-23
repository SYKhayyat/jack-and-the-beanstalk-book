"""The login gate: a password page, and the guard in front of every route.

The gate is one password for everyone. There is no account, no invite and no
per-user identity — just "are you one of us?", which is all this app needs to
keep strangers from spending the owner's image tokens.

Two shapes of refusal, deliberately:

* **HTML** requests are redirected to the login page, so a human who opens the
  app in a browser sees a form instead of a JSON error.
* **API and media** requests get a 401 in the app's usual error shape, so a
  script gets something it can read.

The login page, the static assets and the health check stay open. Everything
else needs the session cookie.
"""

from __future__ import annotations

import logging
from typing import Annotated, cast

from fastapi import APIRouter, Depends, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import RequestResponseEndpoint

from app.auth import SESSION_COOKIE, AuthGate

__all__ = ["get_gate", "install_auth_gate", "router"]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# Paths that stay open: the gate itself, the health check the platform polls,
# and the favicon.
_OPEN_PATHS = frozenset({"/login", "/healthz", "/favicon.ico"})
# Prefixes that stay open: the static assets the login page needs.
_OPEN_PREFIXES = ("/static/",)


def get_gate(request: Request) -> AuthGate:
    """The gate attached to this app at startup."""
    return cast(AuthGate, request.app.state.gate)


GateDep = Annotated[AuthGate, Depends(get_gate)]


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, gate: GateDep) -> Response:
    """Show the password form.

    If no password is configured the gate is open, so there is nothing to log
    into and the visitor is sent straight through.
    """
    if not gate.enabled:
        return RedirectResponse("/", status_code=303)

    templates: Jinja2Templates = request.app.state.templates
    next_path = _safe_next(request.query_params.get("next"))
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "next_path": next_path},
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    gate: GateDep,
    password: str = Form(default=""),
    next_path: str = Form(default=""),
) -> Response:
    """Check the password and, if it is right, hand out a session cookie."""
    client = _client_key(request)
    settings = request.app.state.settings

    if gate.is_locked_out(client):
        logger.info("auth.locked_out client=%s", client)
        return _login_error(request, "Too many wrong passwords. Try again in a few minutes.")

    if gate.check_password(password):
        gate.clear_failures(client)
        logger.info("auth.login_ok client=%s", client)
        response = RedirectResponse(_safe_next(next_path), status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            gate.issue_session(),
            max_age=gate.session_max_age_seconds,
            httponly=True,
            samesite="lax",
            secure=settings.is_production,
            path="/",
        )
        return response

    gate.register_failure(client)
    logger.info("auth.login_failed client=%s", client)
    return _login_error(request, "That password is not right.", next_path)


@router.post("/logout")
def logout(gate: GateDep) -> Response:
    """Forget the session and send the visitor back to the login page."""
    response = RedirectResponse("/login" if gate.enabled else "/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def install_auth_gate(app: FastAPI, gate: AuthGate) -> None:
    """Put the guard in front of every route.

    A no-op when no password is configured, so local development needs no
    ceremony at all.
    """

    @app.middleware("http")
    async def _guard(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not gate.enabled or _is_open_path(request.url.path):
            return await call_next(request)
        if gate.session_valid(request.cookies.get(SESSION_COOKIE)):
            return await call_next(request)
        return _refuse(request)


def _is_open_path(path: str) -> bool:
    return path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES)


def _refuse(request: Request) -> Response:
    """Turn away a request that has no valid session."""
    path = request.url.path
    if path.startswith("/api") or path.startswith("/media"):
        return JSONResponse(
            status_code=401,
            content={
                "error": "password_required",
                "detail": "This app is behind a password. Log in at /login first.",
            },
        )
    target = path if path.startswith("/") else "/"
    return RedirectResponse(f"/login?next={target}", status_code=303)


def _login_error(request: Request, message: str, next_path: str | None = None) -> Response:
    """Re-render the login page with an error, keeping the intended target."""
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": message,
            "next_path": _safe_next(next_path or request.query_params.get("next")),
        },
        status_code=401,
    )


def _safe_next(raw: str | None) -> str:
    """Only ever redirect to a path on this site.

    A `next` of `//evil.example` or `https://evil.example` is an open
    redirect: the visitor would be bounced off-site right after logging in.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"
