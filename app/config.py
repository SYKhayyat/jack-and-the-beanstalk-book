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

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


def _random_signing_key() -> bytes:
    """A key that lives exactly as long as the process that made it."""
    return secrets.token_bytes(32)


class ProviderName(StrEnum):
    """Names of the image-generation backends the app knows how to build."""

    MOCK = "mock"
    GEMINI = "gemini"
    OPENAI = "openai"
    AGNES = "agnes"


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
    max_upload_mb: int = Field(default=10, ge=1, le=50)
    generation_timeout_seconds: int = Field(
        default=600,
        # No floor on purpose: this is the operator's control, and a test that
        # asserts the timeout fires should not have to wait minutes to do it.
        ge=1,
        le=3600,
        description=(
            "Wall-clock ceiling for one API request, covering upload decoding and "
            "the model call. Also applied to the provider's HTTP client so an "
            "in-flight request is actually aborted, not just abandoned."
        ),
    )
    max_concurrent_generations: int = Field(
        default=2,
        ge=1,
        le=64,
        description=(
            "How many generations may run at once per process. Image models are "
            "slow and rate-limited: without this, load testing or a burst of "
            "traffic turns straight into 429s and billable requests."
        ),
    )
    max_generations_per_minute: int = Field(
        default=30,
        ge=1,
        le=10_000,
        description="Per-client abuse brake for billable generation requests.",
    )

    storage_dir: Path = Field(
        default=BASE_DIR / "storage",
        description="Where uploads and generated images are written.",
    )

    database_url: str = Field(
        default="",
        # Pasted whole (e.g. a Neon pooled connection string); it embeds the
        # database password, so it must never be logged or committed. Empty
        # means the filesystem stores are used and the app runs offline.
        description=(
            "Postgres connection URL for durable persistence (books survive "
            "restarts, books are keyed to the person). Empty means filesystem "
            "mode."
        ),
    )

    @field_validator("storage_dir", mode="after")
    @classmethod
    def _make_storage_dir(cls, value: Path) -> Path:
        value.mkdir(parents=True, exist_ok=True)
        return value

    @property
    def database_mode(self) -> bool:
        """Whether books and people persist in Postgres rather than on disk."""
        return bool(self.database_url)

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
        }.get(self.image_provider, "-")

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
        2. A key derived from the shared password, which only exists in local
           development — the two are decoupled here rather than the password
           *being* the key, so rotating the password no longer has to.
        3. A fresh random key per process, which means every restart signs
           everyone out. That is the configuration mistake this property is
           designed to make loud in the log.
        """
        if self.session_secret:
            return hashlib.sha256(
                b"jack-and-the-beanstalk/session/" + self.session_secret.encode()
            ).digest()
        if self.app_password:
            return hashlib.sha256(
                b"jack-and-the-beanstalk/session/dev/" + self.app_password.encode()
            ).digest()
        return _random_signing_key()

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
