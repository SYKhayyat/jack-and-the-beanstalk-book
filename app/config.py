"""Application configuration, loaded from environment variables.

All secrets enter the process through here and nowhere else. Nothing in this
module ever reads a value out of source code, and nothing here is ever logged.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class ProviderName(StrEnum):
    """Names of the image-generation backends the app knows how to build."""

    MOCK = "mock"
    GEMINI = "gemini"
    OPENAI = "openai"
    NIM = "nim"


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
    nim_api_key: str = Field(
        default="",
        description="NVIDIA NIM API key. Only needed for the nim provider.",
    )
    nim_model: str = Field(
        # flux.2-klein-4b edits with up to 8 reference images, which covers the
        # pipeline's maximum of 2. Other NIM image models are named in the
        # README; swapping one is an environment change.
        default="flux.2-klein-4b",
        description="NVIDIA NIM image model id.",
    )
    nim_base_url: str = Field(
        default="https://integrate.api.nvidia.com/v1",
        description="Base URL of the NVIDIA NIM OpenAI-compatible endpoint.",
    )
    nim_size: str = Field(
        default="",
        description=(
            "Force one output size, as WIDTHxHEIGHT, instead of deriving it "
            "from the requested aspect ratio. Empty means the ratio decides."
        ),
    )
    nim_max_prompt_chars: int = Field(
        default=800,
        ge=1,
        le=20_000,
        description=(
            "How many characters of prompt NIM may be sent. The OpenAI-compatible "
            "schema caps the prompt at 800, so a longer prompt is condensed "
            "before it goes out."
        ),
    )
    nim_prompt_tail_chars: int = Field(
        default=200,
        ge=0,
        le=4_000,
        description=(
            "How many characters at the end of a condensed prompt are kept. The "
            "tail carries the negative constraints, so it survives the cut."
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

    @field_validator("storage_dir", mode="after")
    @classmethod
    def _make_storage_dir(cls, value: Path) -> Path:
        value.mkdir(parents=True, exist_ok=True)
        return value

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
            ProviderName.NIM: self.nim_model,
        }.get(self.image_provider, "-")

    @property
    def auth_enabled(self) -> bool:
        """Whether a password is required to reach the app."""
        return bool(self.app_password)

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
