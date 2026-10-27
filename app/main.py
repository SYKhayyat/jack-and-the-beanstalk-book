"""Application factory.

`create_app` is the only entry point — no module-level side effects — so tests
can build an app against a temporary storage dir and a fake provider without
touching global state.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import AuthGate
from app.config import Settings, get_settings
from app.container import build_container
from app.errors import register_exception_handlers
from app.redaction import install_secret_redaction
from app.routes import api, auth, book, media, web
from app.routes.auth import install_auth_gate
from app.services.image_providers.base import ImageProvider

__all__ = ["create_app"]

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# No request body may exceed this, whatever the endpoint. Uploads are already
# capped lower by their own reader; this ceiling exists for the routes that
# have no billable gate — /login is pre-session by definition — so an
# anonymous body cannot push a worker into memory death.
MAX_BODY_BYTES = 20 * 1024 * 1024


@asynccontextmanager
async def _app_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot/reap book jobs around the server's life."""
    reaped = app.state.container.book_store.reap_incomplete()
    if reaped:
        logger.info("book.reaped_start reaped=%d", reaped)
    yield


def create_app(
    settings: Settings | None = None,
    *,
    provider: ImageProvider | None = None,
) -> FastAPI:
    """Build a configured application.

    Args:
        settings: Overrides the environment-derived defaults. Tests pass one.
        provider: Overrides provider construction. Tests pass a fake.
    """
    resolved = settings or get_settings()
    _configure_logging(resolved)

    app = FastAPI(
        title="Jack and the Beanstalk",
        description=(
            "Upload a photo, cartoonify it with your own settings, and place the "
            "cartoon you into a scene from Jack and the Beanstalk."
        ),
        version="1.0.0",
        docs_url="/docs",
        lifespan=_app_lifespan,
    )

    app.state.settings = resolved
    app.state.container = build_container(resolved, provider=provider)
    app.state.templates = Jinja2Templates(directory=TEMPLATES_DIR)
    app.state.gate = AuthGate(
        password=resolved.app_password,
        session_max_age_seconds=resolved.app_session_max_age_days * 24 * 60 * 60,
        max_attempts=resolved.app_login_max_attempts,
    )

    register_exception_handlers(app)
    app.include_router(web.router)
    app.include_router(api.router)
    app.include_router(book.router)
    app.include_router(book.media_router)
    app.include_router(media.router)
    app.include_router(auth.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def _limit_body(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # First and cheapest defence: refuse oversized bodies before reading.
        header = request.headers.get("content-length")
        if header and header.isdigit() and int(header) > MAX_BODY_BYTES:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={
                    "error": "body_too_large",
                    "detail": "The request body is too large for this app.",
                },
            )
        return await call_next(request)

    install_auth_gate(app, app.state.gate)

    logger.info(
        "app.ready provider=%s model=%s env=%s auth=%s",
        app.state.container.provider.name,
        resolved.active_model,
        resolved.app_env,
        "password" if resolved.auth_enabled else "open",
    )
    return app


def _configure_logging(settings: Settings) -> None:
    level = logging.INFO if settings.is_production else logging.DEBUG
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        )
    root.setLevel(level)
    # Last line of defence: even if a secret reaches a logger some other way, it
    # is scrubbed before the handler formats the record.
    install_secret_redaction(
        (
            settings.openai_api_key,
            settings.gemini_api_key,
            settings.app_password,
        )
    )
