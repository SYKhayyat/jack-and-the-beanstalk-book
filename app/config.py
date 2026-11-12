"""Application configuration, loaded from environment variables.

All secrets enter the process through here and nowhere else. Nothing in this
module ever reads a value out of source code, and nothing here is ever logged.
"""

from __future__ import annotations

import hashlib
import secrets
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.pricing import PICTURE_PRICE_CENTS

BASE_DIR = Path(__file__).resolve().parent.parent


def _random_signing_key() -> bytes:
    """A key that lives exactly as long as the process that made it.

    Cached on purpose: everything this key signs is verified by a later call —
    a session cookie, the OAuth state cookie, a share link — so "a key per
    process" only works if every call in the process sees the same bytes. A
    fresh key per call would make every cookie and every link unreadable the
    moment it was issued.
    """
    return _ephemeral_signing_key()


@lru_cache(maxsize=1)
def _ephemeral_signing_key() -> bytes:
    return secrets.token_bytes(32)


class ProviderName(StrEnum):
    """Names of the image-generation backends the app knows how to build."""

    MOCK = "mock"
    GEMINI = "gemini"
    OPENAI = "openai"
    AGNES = "agnes"
    #: Gemini at gemini.google.com in a local Chrome: free, slow, one machine.
    GEMINI_WEB = "gemini_web"


#: The dedicated Chrome profile Gemini's tab lives in, apart from any of yours.
_GEMINI_WEB_PROFILE = Path.home() / ".local/share/jack-and-the-beanstalk/gemini-chrome"


class Settings(BaseSettings):
    """Process-wide settings.

    Reads from the real environment and from a local `.env` file (dev only).
    Field names map to upper-case environment variables, e.g. `max_upload_mb`
    is populated from `MAX_UPLOAD_MB`.
    """

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- provider ---
    image_provider: ProviderName = Field(
        default=ProviderName.MOCK,
        description="Which image backend to use. 'mock' needs no API key.",
    )
    gemini_api_key: str = Field(
        default="",
        description="Google AI Studio API key. Only needed for the gemini provider.",
    )
    openai_api_key: str = Field(
        default="",
        description="OpenAI API key. Only needed for the openai provider.",
    )
    openai_model: str = Field(
        # Cheapest per-image figure OpenAI publishes: $0.00588 at quality=low.
        # gpt-image-2 is priced at half the per-token rate ($15 vs $30 per 1M
        # output tokens) but OpenAI renders its per-image token count only in
        # an interactive calculator, so it cannot be quoted with confidence.
        default="gpt-image-2.5-flare",
        description="OpenAI image model id.",
    )
    openai_quality: str = Field(
        default="low",
        description="OpenAI image quality tier: low, medium, high, xhigh or max.",
    )
    openai_images_per_minute: int = Field(
        # OpenAI's image cap is 20 per minute on this account; stay just under.
        default=18,
        ge=0,
        le=10_000,
        description=(
            "Images per minute the OpenAI provider may start. Calls wait for room "
            "instead of being rejected with 429. Set just under your OpenAI "
            "images-per-minute limit; 0 turns pacing off."
        ),
    )
    gemini_model: str = Field(
        # Not gemini-2.5-flash-image: Google deprecated it and scheduled
        # shutdown for 2026-10-02, naming these two as the migration targets.
        # Lite is the cheaper of the pair; see the README for the trade-off.
        default="gemini-3.1-flash-lite-image",
        description="Gemini image-capable model id.",
    )
    agnes_api_key: str = Field(
        default="",
        description="Agnes AI API key. Only needed for the agnes provider.",
    )
    agnes_model: str = Field(
        # The image model that takes multiple reference images: the cartoonify
        # pass sends the photo, the book pass sends the portrait plus the photo.
        default="agnes-image-2.5-flash",
        description="Agnes AI image model id.",
    )
    agnes_base_url: str = Field(
        default="https://apihub.agnes-ai.com/v1",
        description="Base URL of the Agnes AI OpenAI-compatible endpoint.",
    )
    agnes_size: str = Field(
        # The free tier is rate limited by resolution — about 10 requests a
        # minute at 1K, 5 at 2K, one above that — so 1K is the only tier that
        # keeps a whole book inside one sitting.
        default="1K",
        description="Agnes output resolution tier: 1K, 2K, 3K or 4K.",
    )

    # --- the reviewer (T9 tier 2) ---
    # Every finished picture is checked by a vision model before a book can
    # complete. The key is the operator's, like every other backend: environment
    # only, never named on a visitor-facing surface.
    reviewer_api_key: str = Field(
        default="",
        description=(
            "Key for the picture reviewer. Empty falls back to GEMINI_API_KEY, "
            "and with neither set the reviewer is off in development and a "
            "start-up error in production."
        ),
    )
    reviewer_provider: str = Field(
        default="openai",
        pattern="^(off|gemini|openai|gemini_web)$",
        description=(
            "Which vision model checks each picture after the decision judge: "
            "openai (the default: gpt-6-luna; uses OPENAI_API_KEY and is shown "
            "the plate as well), gemini, gemini_web (Gemini in a local Chrome, "
            "free; see GEMINI_WEB_ACCOUNT), or off (the decision judge and "
            "tier 1 decide alone)."
        ),
    )
    allow_unreviewed: bool = Field(
        default=False,
        description=(
            "Run a production deployment that pays for pictures with the reviewer "
            "off. Off by default: paid images with no review loop is either a "
            "deliberate saving or a mistake, and only the operator can say which, "
            "so the app refuses to start unless this is set (W9, C7)."
        ),
    )
    reviewer_model: str = Field(
        default="gpt-6-luna",
        description=(
            "Vision model used to check each finished picture. gpt-6-luna with "
            "REVIEWER_PROVIDER=openai; a Gemini model such as "
            "gemini-3.1-flash-lite with REVIEWER_PROVIDER=gemini."
        ),
    )
    page_attempts: int = Field(
        default=3,
        ge=1,
        le=3,
        description=(
            "Billable attempts per page before the best one is kept. Three is "
            "the cap by design: each attempt costs money, and a page that never "
            "passes is kept and marked best-effort rather than retried forever."
        ),
    )
    decision_judge: bool = Field(
        default=True,
        description=(
            "Let a decision model (OpenAI's Decisions API, JEV-style) look at every "
            "picture first: confidently good pictures pass without a reviewer call, "
            "confidently bad ones fail, and only unsure ones go to the reviewer. Needs "
            "OPENAI_API_KEY; off in mock mode."
        ),
    )
    decision_judge_provider: str = Field(
        default="openai",
        pattern="^(openai|gemini_web)$",
        description=(
            "Who answers the decision questions: openai (the Decisions API) or "
            "gemini_web (Gemini in a local Chrome, asked for the probabilities "
            "as JSON; free, for local books)."
        ),
    )
    decision_judge_model: str = Field(
        default="gpt-6-luna",
        description="The decision model. gpt-6-luna is the one the Decisions API serves.",
    )
    decision_judge_usd_per_million_tokens: float = Field(
        default=0.10,
        ge=0,
        description="The decision model's price per million input tokens (output is free).",
    )
    gemini_web_account: str = Field(
        default="",
        description=(
            "The Google account Gemini in Chrome must be signed in as; checked "
            "before every job, and a job waits while it is not. Needed when any "
            "of IMAGE_PROVIDER, REVIEWER_PROVIDER or DECISION_JUDGE_PROVIDER is "
            "gemini_web."
        ),
    )
    gemini_web_port: int = Field(
        default=9333, ge=1024, le=65535, description="Chrome's debugging port."
    )
    gemini_web_profile_dir: Path = Field(
        default=_GEMINI_WEB_PROFILE,
        description="The Chrome profile kept signed in for Gemini (never your own).",
    )
    gemini_web_chrome: str = Field(
        default="google-chrome", description="The Chrome binary started when none is running."
    )
    gemini_web_limit_wait_seconds: int = Field(
        default=1800,
        ge=1,
        description="How long a job waits after Gemini says a limit is reached.",
    )
    reviewer_timeout_seconds: int = Field(
        default=90,
        ge=5,
        description="One review's budget. A timed-out review fails that image, not the book.",
    )

    # --- what a call costs (the cost ledger) ---
    image_call_cost_usd: float | None = Field(
        default=None,
        ge=0,
        description=(
            "What one generated image costs, in dollars. Empty means: priced "
            "from the configured provider — Agnes and mock are free, OpenAI is "
            "priced from its published per-image table for the model and "
            "quality. Set it for a backend that table does not know."
        ),
    )
    review_call_cost_usd: float | None = Field(
        default=None,
        ge=0,
        description=(
            "What one review call costs, in dollars. Empty leaves review calls "
            "counted but unpriced, and the cost page says how many."
        ),
    )

    # --- app ---
    app_env: str = Field(default="development")
    app_password: str = Field(
        default="",
        description=(
            "Shared password required to use the app. Empty means the app is "
            "open to anyone — fine locally, reckless in production, because "
            "every generation costs money."
        ),
    )
    app_session_max_age_days: int = Field(
        default=7,
        ge=1,
        le=365,
        description="How long a successful login stays valid.",
    )
    app_login_max_attempts: int = Field(
        default=8,
        ge=1,
        le=100,
        description=(
            "Wrong passwords allowed per client in a quarter of an hour before "
            "that client is locked out of the login form."
        ),
    )

    # --- sign-in (Google OAuth) ---
    google_client_id: str = Field(
        default="",
        description=(
            "Google OAuth client id for federated sign-in. Empty means the app "
            "has no Google login and falls back to the shared password."
        ),
    )
    google_client_secret: str = Field(
        default="",
        description="Google OAuth client secret. Only ever set in the environment.",
    )
    google_redirect_uri: str = Field(
        default="",
        description=(
            "The exact redirect URI registered with Google, e.g. "
            "https://your-app.onrender.com/auth/callback. Empty means the app "
            "derives it from the request's own host, which is right for local "
            "development only."
        ),
    )
    session_secret: str = Field(
        default="",
        description=(
            "Key that signs the session cookie. If it is not set the app "
            "derives one, which signs everyone out on every restart — set it in "
            "production. It is never the password."
        ),
    )
    operator_sub: str = Field(
        default="",
        description=(
            "Who may use /admin: one or more Google `sub` values or account "
            "emails, comma separated. Empty disables the admin screens entirely: "
            "the operator is a named account, never 'anybody signed in'."
        ),
    )
    max_upload_mb: int = Field(default=10, ge=1, le=50)
    generation_timeout_seconds: int = Field(
        default=600,
        # No floor on purpose: this is the operator's control, and a test that
        # asserts the timeout fires should not have to wait minutes to do it.
        ge=1,
        # A week: with GEMINI_WEB every page queues behind one browser and a
        # job waits out Gemini's daily limit, so a page can take a day.
        le=7 * 24 * 3600,
        description=(
            "Wall-clock ceiling for one API request, covering upload decoding and "
            "the model call. Also applied to the provider's HTTP client so an "
            "in-flight request is actually aborted, not just abandoned."
        ),
    )
    max_concurrent_generations: int = Field(
        default=12,
        ge=1,
        le=64,
        description=(
            "How many generations may run at once per process. Image models are "
            "slow and rate-limited: without this, load testing or a burst of "
            "traffic turns straight into 429s and billable requests."
        ),
    )
    book_page_concurrency: int = Field(
        default=6,
        ge=1,
        le=64,
        description=(
            "How many of one book's pages are worked on at once: drawn, pasted "
            "onto their plate and reviewed. Separate from the gate above, which "
            "bounds model calls across the server. Every page in flight holds "
            "its pictures in memory and queues Pillow work, so 21 at once is "
            "what starved the free plan's event loop (Run 2); the model calls "
            "are I/O, so a lower number mostly costs latency, not CPU."
        ),
    )
    image_work_threads: int = Field(
        default=2,
        ge=1,
        le=16,
        description=(
            "Worker threads for a render's own image work (tier-1 review, "
            "pasting the plate back). Their own pool, so that work never runs "
            "on the event loop nor queues the database calls and routes that "
            "share the default one."
        ),
    )
    book_max_resumes: int = Field(
        default=2,
        ge=0,
        le=10,
        description=(
            "How many times a book a restart interrupted is picked up again by "
            "itself. Bounded, so a book that is what kills the server cannot "
            "keep killing it: past this it fails, and its owner can finish it."
        ),
    )
    book_resume_window_minutes: int = Field(
        default=60,
        ge=1,
        # A week, as for the generation timeout: a GEMINI_WEB book can take a day.
        le=7 * 24 * 60,
        description=(
            "A book whose job started longer ago than this is not resumed after "
            "a restart: it fails, and its owner can finish it by hand."
        ),
    )
    max_generations_per_minute: int = Field(
        default=120,
        ge=1,
        le=10_000,
        description="Per-client abuse brake for billable generation requests.",
    )

    storage_dir: Path = Field(
        default=BASE_DIR / "storage",
        description="Where uploads and generated images are written.",
    )
    draft_retention_hours: int = Field(
        default=24,
        ge=1,
        le=24 * 30,
        description=(
            "How long an uploaded photo and the portrait made from it are kept. "
            "The review flow needs minutes; the rest of the window is slack for "
            "a slow visit, not an archive. Expired on boot and hourly after that."
        ),
    )
    book_retention_days: int = Field(
        default=7,
        ge=1,
        le=365,
        description=(
            "How long a finished (or failed) book is kept before its pages, "
            "portrait and PDF are deleted. Books of a child's photographs should "
            "not be retained forever, and a database has a size limit."
        ),
    )

    picture_price_cents: int = Field(
        default=PICTURE_PRICE_CENTS,
        ge=1,
        le=10_000,
        description=(
            "What one picture drawn beyond a book's own fee costs: a portrait, "
            "a redrawn page or cover, a face swap's pictures. The book's own "
            "cover and story pages stay inside its document price."
        ),
    )

    reviewer_budget_percent: int = Field(
        default=0,
        ge=0,
        le=1000,
        description=(
            "Stop paying the reviewer for a book once its measured spend has "
            "reached this percentage of the book's own price (a setting of 100 "
            "rests it at the price). Tier 1 and the decision judge still look, "
            "so a picture is never passed unchecked and every verdict says the "
            "reviewer did not run. 0, the default, never rests it for money."
        ),
    )
    book_cost_alert_cents: int = Field(
        default=100,
        ge=0,
        le=100_000,
        description=(
            "A book whose measured model spend passes this many cents is named "
            "on /admin/status and in one log line, so a book that costs more "
            "than it sells for is noticed without reading the ledger. 0 turns "
            "the alert off."
        ),
    )

    stripe_secret_key: str = Field(
        default="",
        # A sandbox key (sk_test_...), which moves no real money. It is a
        # credential exactly like the others: pasted into `.env` or the Render
        # dashboard, never committed, never logged. Empty means money is off:
        # no top-ups, no refunds and no per-book fee, and the app behaves
        # exactly as it did before accounts existed.
        description=(
            "Stripe sandbox secret key for top-ups and refunds. Empty disables "
            "payments and the document fee."
        ),
    )

    database_url: str = Field(
        default="",
        # Pasted whole (e.g. a Neon pooled connection string); it embeds the
        # database password, so it must never be logged or committed. Empty
        # means SQLite: one file under STORAGE_DIR, created and migrated on
        # boot. There is no other store to fall back to.
        description=(
            "Postgres connection URL for durable persistence (books survive "
            "restarts, books are keyed to the person). Empty means a local "
            "SQLite database under STORAGE_DIR."
        ),
    )

    r2_account_id: str = Field(
        default="",
        description="Cloudflare account id (the R2 endpoint's subdomain).",
    )
    r2_bucket: str = Field(
        default="", description="The private R2 bucket pictures and PDFs live in."
    )
    r2_access_key_id: str = Field(default="", description="R2 API token's access key id.")
    # A credential like the others: environment only, never logged.
    r2_secret_access_key: str = Field(
        default="", description="R2 API token's secret access key."
    )

    @property
    def r2_enabled(self) -> bool:
        """Whether blobs go to R2. All four settings, or none (inline blobs)."""
        return all(
            (
                self.r2_account_id,
                self.r2_bucket,
                self.r2_access_key_id,
                self.r2_secret_access_key,
            )
        )

    @field_validator("image_call_cost_usd", "review_call_cost_usd", mode="before")
    @classmethod
    def _blank_price_is_unset(cls, value: object) -> object:
        """`IMAGE_CALL_COST_USD=` (as `.env.example` ships it) means "not set"."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("gemini_web_profile_dir", mode="before")
    @classmethod
    def _blank_profile_is_default(cls, value: object) -> object:
        """`GEMINI_WEB_PROFILE_DIR=` means the default profile, not the working dir."""
        return _GEMINI_WEB_PROFILE if isinstance(value, str) and not value.strip() else value

    @field_validator("storage_dir", mode="after")
    @classmethod
    def _make_storage_dir(cls, value: Path) -> Path:
        value.mkdir(parents=True, exist_ok=True)
        return value

    @property
    def local_database_path(self) -> Path:
        """The SQLite file an empty `DATABASE_URL` means: the whole store."""
        return self.storage_dir / "app.sqlite3"

    @property
    def effective_reviewer_key(self) -> str:
        """The key the reviewer will use, from either variable that can carry it.

        One fallback, in one place: `REVIEWER_API_KEY` wins, and a deployment
        that already has `GEMINI_API_KEY` for images needs no second key.
        """
        return self.reviewer_api_key.strip() or self.gemini_api_key.strip()

    @property
    def decision_judge_enabled(self) -> bool:
        """Whether the decision model looks first: asked for, keyed, and not mock."""
        keyed = self.decision_judge_provider == "gemini_web" or bool(
            self.openai_api_key.strip()
        )
        return self.decision_judge and keyed and self.image_provider is not ProviderName.MOCK

    @property
    def uses_gemini_web(self) -> bool:
        """Whether any backend is Gemini in a local Chrome."""
        return (
            self.image_provider is ProviderName.GEMINI_WEB
            or self.reviewer_provider == "gemini_web"
            or (self.decision_judge_enabled and self.decision_judge_provider == "gemini_web")
        )

    @property
    def reviewer_required(self) -> bool:
        """Whether a missing reviewer key is a start-up error (T9.6).

        Only once a reviewer is chosen (`REVIEWER_PROVIDER` not off), and only
        in production: a deployment that asked for a reviewer must not quietly
        run without one. Off in development, test and mock mode, where every
        verdict is stamped `tiers_run: [1]` and said so in the log rather than
        dressed up as a review.
        """
        return (
            self.reviewer_provider != "off"
            and self.is_production
            and self.image_provider is not ProviderName.MOCK
        )

    @property
    def effective_database_url(self) -> str:
        """The URL the app actually connects to; never empty.

        `DATABASE_URL` when the operator set one, otherwise this machine's
        SQLite file. Everything downstream — the container, the stores, the
        migration command — goes through this, so there is one storage path
        and local development cannot drift onto a second one.
        """
        if self.database_url:
            return self.database_url
        from app.db import local_database_url

        return local_database_url(self.storage_dir)

    @property
    def payments_enabled(self) -> bool:
        """Whether money is on: a Stripe key is configured."""
        return bool(self.stripe_secret_key)

    @model_validator(mode="after")
    def _a_paid_book_is_checked(self) -> Settings:
        """Refuse to start buying pictures nobody will look at (W9, C7).

        The review loop is both a product promise and the app's cost control; a
        deployment that turns it off while paying for images is either making a
        deliberate saving or has lost a setting. The free backends (mock, Agnes,
        Gemini in Chrome) are exempt: there is no money there to waste.
        Development may run unreviewed, because there is nothing to protect.
        """
        paid = self.image_provider in {ProviderName.OPENAI, ProviderName.GEMINI}
        if (
            self.is_production
            and paid
            and self.reviewer_provider == "off"
            and not self.allow_unreviewed
        ):
            raise ValueError(
                "REVIEWER_PROVIDER=off with a paid image provider in production: "
                "every picture would be bought and never checked. Set "
                "ALLOW_UNREVIEWED=1 if that is what you want."
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @property
    def active_model(self) -> str:
        """The model the configured provider will actually call.

        The provider and its model are chosen by environment variables alone —
        there is no user-facing control for either — so this is the operator's
        answer to "what is this deployment running?".
        """
        return {
            ProviderName.OPENAI: self.openai_model,
            ProviderName.GEMINI: self.gemini_model,
            ProviderName.AGNES: self.agnes_model,
            ProviderName.GEMINI_WEB: "gemini-web",
        }.get(self.image_provider, "-")

    @property
    def operator_ids(self) -> tuple[str, ...]:
        """The people allowed into /admin: Google subs and/or emails.

        `OPERATOR_SUB` keeps its historical name and takes a comma-separated
        list. A value containing `@` is matched against the signed-in Google
        account's email; anything else is matched against its `sub`. Listing an
        email is the friendlier form — an operator can copy it from the Google
        account page without decoding an OAuth subject id.
        """
        return tuple(part.strip() for part in self.operator_sub.split(",") if part.strip())

    def is_operator_identity(self, *, sub: str = "", email: str = "") -> bool:
        """Whether this signed-in Google identity is the operator.

        Email comparison ignores case; a Google `sub` is compared exactly. An
        empty configuration makes nobody the operator — the admin screens then
        answer their usual 404, so a deployment cannot accidentally open them.
        """
        email_folded = email.strip().casefold()
        sub = sub.strip()
        for value in self.operator_ids:
            if "@" in value:
                if email_folded and value.casefold() == email_folded:
                    return True
            elif sub and value == sub:
                return True
        return False

    @property
    def auth_enabled(self) -> bool:
        """Whether anything at all is required to reach the app."""
        return self.google_enabled or bool(self.app_password)

    @property
    def google_enabled(self) -> bool:
        """Whether federated Google sign-in is configured.

        When it is, it is the *only* way in: the shared password is the
        local-dev/offline fallback and is switched off by the presence of a
        Google client, exactly as the deployment story requires.
        """
        return bool(self.google_client_id and self.google_client_secret)

    def session_signing_key(self) -> bytes:
        """The key the session cookie is signed with.

        Order of preference, deliberately:

        1. `SESSION_SECRET`, when it is set: one stable key, so a restart does
           not sign anyone out.
        2. A key derived from the Google client secret, which is always set
           where Google sign-in is. This is the fix for the deployment that
           forgot `SESSION_SECRET`: without it the key was random per process,
           so every restart — and Render's free plan restarts the container
           whenever it wakes from sleep — invalidated the signed OAuth state
           cookie mid-round-trip. The visitor saw "that sign-in link had
           expired" and had to sign in a second time. The Google client secret
           is stable for the life of the deployment, so the key is too.
        3. A key derived from the shared password, which only exists in local
           development — the two are decoupled here rather than the password
           *being* the key, so rotating the password no longer has to.
        4. A fresh random key per process, which means every restart signs
           everyone out. That is the configuration mistake this property is
           designed to make loud in the log.
        """
        if self.session_secret:
            return hashlib.sha256(
                b"jack-and-the-beanstalk/session/" + self.session_secret.encode()
            ).digest()
        if self.google_client_secret:
            return hashlib.sha256(
                b"jack-and-the-beanstalk/session/google/" + self.google_client_secret.encode()
            ).digest()
        if self.app_password:
            return hashlib.sha256(
                b"jack-and-the-beanstalk/session/dev/" + self.app_password.encode()
            ).digest()
        return _random_signing_key()

    @property
    def session_key_is_ephemeral(self) -> bool:
        """Whether the signing key dies with this process.

        True means every restart signs everyone out and breaks any sign-in in
        flight. The operator needs to hear about it in the log, not discover it
        as "the sign-in link expired" from a visitor.
        """
        return not (self.session_secret or self.google_client_secret or self.app_password)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that every layer reads the same object. Tests can call
    `get_settings.cache_clear()` to force a re-read after patching the env.
    """
    return Settings()
