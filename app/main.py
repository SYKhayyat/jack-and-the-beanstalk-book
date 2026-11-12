"""Application factory.

`create_app` is the only entry point — no module-level side effects — so tests
can build an app against a temporary storage dir and a fake provider without
touching global state.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import AuthGate
from app.books import SEEDS
from app.config import Settings, get_settings
from app.container import Container, build_container
from app.db import database_secrets
from app.errors import register_exception_handlers
from app.logging_context import (
    install_request_id_logging,
    new_request_id,
    reset_request_id,
    set_request_id,
)
from app.redaction import install_secret_redaction
from app.routes import (
    admin,
    admin_status,
    api,
    auth,
    book,
    book_media,
    media,
    share,
    wallet,
    web,
)
from app.routes.auth import install_auth_gate
from app.services.alerts import log_alerts, operator_alerts
from app.services.google_oauth import GoogleOAuthClient
from app.services.image_providers.base import ImageProvider
from app.services.payments import PaymentProvider
from app.services.pictures import picture_set
from app.services.reviewer import ImageReviewer
from app.services.vitals import watch_loop
from app.sessions import SessionCodec
from app.stories import NOT_READY, shelf, showcase

__all__ = ["create_app"]

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


def _short_date(stamp: object) -> str:
    """An ISO timestamp as "2 Nov" for the drawer; anything else as given."""
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return str(stamp)
    return f"{moment.day} {moment:%b}"


def static_build(directory: Path = STATIC_DIR) -> str:
    """A fingerprint of every static file: it changes whenever any file does.

    Pages link the stylesheet and scripts under `/static/<build>/`, so each
    deploy that changes them hands out URLs no cache has ever seen. That is
    the only fix a cache cannot override: Cloudflare in front of the custom
    domain applies its own four-hour browser TTL and kept serving the old
    stylesheet to the new pages, whatever Cache-Control the app sent. A
    module's relative imports resolve under the same prefix, so a page never
    mixes one deploy's script with another's.
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


class ImmutableStaticFiles(StaticFiles):
    """Fingerprinted static files: the URL changes with the content, so keep them."""

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


class RevalidatedStaticFiles(StaticFiles):
    """Static files every cache must check with us before reusing.

    Without a Cache-Control header, Cloudflare in front of the custom domain
    kept `book-detail.js` for four hours, so a deploy shipped new HTML that
    called into an old script. `no-cache` still lets a browser keep the file,
    but only after an ETag check — a 304 when nothing changed, the new file
    the moment a deploy changes it.
    """

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers["Cache-Control"] = "no-cache"
        return response


# No request body may exceed this, whatever the endpoint. Uploads are already
# capped lower by their own reader; this ceiling exists for the routes that
# have no billable gate — /login is pre-session by definition — so an
# anonymous body cannot push a worker into memory death.
MAX_BODY_BYTES = 20 * 1024 * 1024

# Google's token and userinfo endpoints are fast; a sign-in that takes longer
# than this has failed, and the visitor is waiting on a browser.
_OAUTH_TIMEOUT = 30.0


#: How often a running server sweeps expired drafts and books. Hourly is
#: free and keeps a long-lived instance from holding a week of photographs
#: that only a restart would otherwise clear.
_EXPIRY_INTERVAL_SECONDS = 3600.0


def expire_once(container: Container) -> tuple[int, int]:
    """Delete drafts and books past their retention windows.

    One policy for both stores, expressed once: the windows are settings, and
    every store implements the deletion behind `expire`. Returns (drafts,
    books) removed, so the caller can log one line instead of two.
    """
    drafts = container.store.expire(
        older_than_seconds=container.settings.draft_retention_hours * 3600
    )
    books = container.book_store.expire(
        older_than_seconds=container.settings.book_retention_days * 24 * 3600
    )
    # The studio's history is kept as long as the books it led to, no longer.
    container.portraits.expire(
        older_than=timedelta(days=container.settings.book_retention_days)
    )
    if drafts or books:
        logger.info("storage.expired drafts=%d books=%d", drafts, books)
    return drafts, books


def check_alerts(container: Container) -> int:
    """Run the operator's thresholds and log what they say; returns the count.

    One place for both callers — the boot and the hourly sweep — so the log and
    `/admin/status` cannot disagree about what is wrong (W7).
    """
    alerts = operator_alerts(
        container.engine, price_cents=container.wallet.price_cents, wallet=container.wallet
    )
    log_alerts(alerts)
    return len(alerts)


async def _sweep_expired(container: Container) -> None:
    """Run `expire_once` for as long as the server lives.

    A failure is logged and the sweep continues: a transient error deleting
    one directory must not stop retention for the rest of the process, and
    the next hour retries.
    """
    while True:
        await asyncio.sleep(_EXPIRY_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(expire_once, container)
        except Exception:
            logger.warning("storage.expire_failed", exc_info=True)
        # The thresholds worth a line in the log: the same ones the operator's
        # page shows, so a deployment can be trusted without being watched (W7).
        try:
            await asyncio.to_thread(check_alerts, container)
        except Exception:
            logger.warning("operator.alert_check_failed", exc_info=True)


@asynccontextmanager
async def _app_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Resume interrupted books, sweep expired data, and keep sweeping."""
    container = app.state.container
    # A restart mid-render (a deploy, or the platform's health check giving
    # up) picks the book up again rather than losing it; one that has been
    # resumed too often, or started too long ago, fails with its pages kept.
    resumed = await container.book_runner.recover(
        max_resumes=container.settings.book_max_resumes,
        window_minutes=container.settings.book_resume_window_minutes,
    )
    if resumed:
        logger.info("book.resumed_start resumed=%d", len(resumed))
    # At boot, directly and before serving: a restart is the one moment the
    # process is guaranteed idle, so the sweep cannot race a live render.
    expire_once(container)
    # A boot is a good moment to say what is wrong, loudly and once (W7).
    try:
        await asyncio.to_thread(check_alerts, container)
    except Exception:
        logger.warning("operator.alert_check_failed", exc_info=True)
    sweeper = asyncio.create_task(_sweep_expired(container))
    # Logs `loop.lag` only when the event loop falls behind; silence is health.
    vitals = asyncio.create_task(watch_loop())
    try:
        yield
    finally:
        for task in (sweeper, vitals):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        # Pauses the books still rendering, for the next boot to resume.
        await container.book_runner.close()


def create_app(
    settings: Settings | None = None,
    *,
    provider: ImageProvider | None = None,
    payments: PaymentProvider | None = None,
    reviewer: ImageReviewer | None = None,
) -> FastAPI:
    """Build a configured application.

    Args:
        settings: Overrides the environment-derived defaults. Tests pass one.
        provider: Overrides provider construction. Tests pass a fake.
        payments: Overrides Stripe construction. Tests pass a fake.
        reviewer: Overrides reviewer construction. Tests pass a fake, so the
            retry path can be exercised without a vision model or a key.
    """
    if not SEEDS:
        # ``app.books`` validates every seed it finds at import, so reaching
        # here with none means the story data is missing, not merely unread.
        raise RuntimeError("No story seeds found in app/books")
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
    app.state.container = build_container(
        resolved, provider=provider, payments=payments, reviewer=reviewer
    )
    app.state.templates = Jinja2Templates(directory=TEMPLATES_DIR)
    # Every page can show the shelf's stories (the signed-out login page too)
    # and the footer's retention line, so both are template globals. The shelf
    # is read per render: the operator's start gate can change it at any time.
    container = app.state.container
    app.state.templates.env.globals["shelf_stories"] = lambda: shelf(container.templates)
    app.state.templates.env.globals["showcase_covers"] = lambda: showcase(container.templates)
    app.state.templates.env.globals["not_ready"] = NOT_READY
    app.state.templates.env.globals["static_url"] = f"/static/{static_build()}"
    app.state.templates.env.globals["book_retention_days"] = resolved.book_retention_days
    app.state.templates.env.filters["short_date"] = _short_date
    app.state.templates.env.globals["picture_set"] = picture_set
    app.state.gate = AuthGate(
        password=resolved.app_password,
        session_max_age_seconds=resolved.app_session_max_age_days * 24 * 60 * 60,
        max_attempts=resolved.app_login_max_attempts,
    )
    # One signer for the session cookie, shared by the gate and the Google
    # flow. Its key is a setting, never the password: see
    # `Settings.session_signing_key`.
    sessions = SessionCodec(resolved.session_signing_key())
    app.state.sessions = sessions
    app.state.gate.codec = sessions
    # The signing key's lifetime decides whether a container restart is
    # invisible to the visitor. A key that dies with the process signs every
    # session out and breaks any Google sign-in in flight ("that sign-in link
    # had expired"), so the operator hears about it here rather than from a
    # visitor. Render's free plan restarts the container whenever it wakes
    # from sleep, which is exactly when this bites.
    if resolved.session_key_is_ephemeral:
        logger.warning(
            "app.session_key source=ephemeral detail=no_SESSION_SECRET_no_google_client_secret"
            " impact=every_restart_signs_everyone_out"
        )
    elif not resolved.session_secret:
        logger.info(
            "app.session_key source=derived detail=stable_across_restarts "
            "action=set_SESSION_SECRET_to_choose_the_key_explicitly"
        )
    # The Google sign-in client, only when it is configured. Without a client
    # there is nothing to build and nothing to call.
    app.state.oauth = (
        GoogleOAuthClient(
            client_id=resolved.google_client_id,
            client_secret=resolved.google_client_secret,
            timeout_seconds=_OAUTH_TIMEOUT,
        )
        if resolved.google_enabled
        else None
    )

    register_exception_handlers(app)
    app.include_router(web.router)
    app.include_router(api.router)
    app.include_router(book.router)
    app.include_router(book.books_router)
    app.include_router(book.stories_router)
    app.include_router(book_media.router)
    app.include_router(media.router)
    app.include_router(wallet.router)
    app.include_router(share.router)
    app.include_router(auth.router)
    # Operator-only, and hidden from the schema: the review screen is not part
    # of the visitor-facing app, and every route in it 404s for anybody who is
    # not the configured operator.
    app.include_router(admin.router)
    app.include_router(admin_status.router)
    # The web faces are WOFF2 copies in app/static/webfonts, served by the
    # static mounts below under the fingerprinted prefix (S6). The TTFs in
    # app/assets/fonts are the PDF's own and are not served to a browser.
    # The fingerprinted path pages link (see `static_build`) comes before the
    # plain one, which stays for the favicon, the covers and anything typed.
    app.mount(
        app.state.templates.env.globals["static_url"],
        ImmutableStaticFiles(directory=STATIC_DIR),
        name="static_build",
    )
    app.mount("/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static")

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

    # Outermost middleware: every request — including one the guard turns away
    # and one the body limit refuses — gets an id and exactly one line saying
    # how it ended. The id also travels into the book's background task, so a
    # book's whole trail shares one request id. Path only, never the query
    # string: the Google callback carries a code there.
    @app.middleware("http")
    async def _request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = new_request_id()
        request.state.request_id = request_id
        token = set_request_id(request_id)
        started = monotonic()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request.failed method=%s path=%s", request.method, request.url.path
            )
            reset_request_id(token)
            raise
        duration_ms = (monotonic() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request.done method=%s path=%s status=%d duration_ms=%.1f request=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request_id,
        )
        reset_request_id(token)
        return response

    # The provider's own readiness sentence, in the operator's log and nowhere
    # a visitor can reach: "agnes ready (model=..., size=1K)", "openai ready
    # (model=..., quality=low)". This is the line that says the deployment can
    # actually draw pictures, rather than merely having booted.
    ready, reason = app.state.container.provider.health_check()
    logger.info("app.provider_health ready=%s %s", ready, reason)

    logger.info(
        "app.ready provider=%s model=%s env=%s auth=%s payments=%s",
        app.state.container.provider.name,
        resolved.active_model,
        resolved.app_env,
        _auth_mode(resolved),
        "stripe" if app.state.container.payments else "off",
    )
    return app


def _auth_mode(settings: Settings) -> str:
    """Which way in this deployment has, in one word for the log."""
    if settings.google_enabled:
        return "google"
    if settings.app_password:
        return "password"
    return "open"


def _configure_logging(settings: Settings) -> None:
    """Set the log level, and arm the redaction filter with every secret."""
    level = logging.INFO if settings.is_production else logging.DEBUG
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        )
    root.setLevel(level)
    # Correlate every line with the request that produced it. Installed before
    # redaction so redaction still gets the last word on the finished message.
    install_request_id_logging()
    # Last line of defence: even if a secret reaches a logger some other way, it
    # is scrubbed before the handler formats the record.
    install_secret_redaction(
        (
            settings.openai_api_key,
            settings.gemini_api_key,
            settings.agnes_api_key,
            settings.app_password,
            # The OAuth client secret and the session key: the one gets pasted
            # whole into a dashboard, the other is the signing key of every
            # session in the deployment. Neither may appear in a log line.
            settings.google_client_secret,
            settings.session_secret,
            # A Stripe key is a bearer credential: whoever holds it can move
            # money on the account. It is armed like every other secret.
            settings.stripe_secret_key,
            # R2 token secret: bearer credential for the whole picture bucket.
            settings.r2_secret_access_key,
            # A database URL is pasted whole and embeds its password; a raw
            # URL in a log line is a leak of the whole credential. The password
            # fragment is armed separately because a URL can be logged in
            # pieces.
            *database_secrets(settings.database_url),
        )
    )
    if settings.google_enabled and not settings.session_secret:
        logger.warning(
            "app.session_key_randomized SESSION_SECRET is not set, so every "
            "restart signs everyone out. Set it in the environment."
        )
