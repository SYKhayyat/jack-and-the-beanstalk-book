"""Composition root.

The one module allowed to know about every concrete implementation. It reads
settings and wires the object graph; everything else receives what it needs
through FastAPI dependencies. Swapping the provider, the store location or the
pipeline is a change here and nowhere else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import Settings
from app.db import build_engine, persistence_mode
from app.services.book_runner import BookRunner
from app.services.book_store import BookStore, FileBookStore
from app.services.concurrency import ConcurrencyGate
from app.services.db_book_store import DatabaseBookStore
from app.services.image_providers.base import ImageProvider
from app.services.image_providers.registry import build_provider
from app.services.pipeline import GenerationPipeline
from app.services.rate_limit import GenerationRateLimiter
from app.services.storage import GenerationStore
from app.services.user_store import DatabaseUserStore, FileUserStore, UserStore

__all__ = ["Container", "build_container"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Container:
    """Everything the routes need, already constructed."""

    settings: Settings
    provider: ImageProvider
    store: GenerationStore
    pipeline: GenerationPipeline
    # Everyone who has signed in, so a book can be filed under a person and
    # nobody else can read it.
    user_store: UserStore
    # The book pipeline: its own persistence root plus the runner that walks
    # page by page. The gate is shared with the interactive flow so books and
    # single-scene generations bound the same billable-call budget.
    book_store: BookStore
    book_runner: BookRunner
    # Bounds concurrent generations per process. Image models are slow and
    # rate-limited, so without a ceiling a burst of traffic becomes a burst of
    # billable requests and 429s.
    gate: ConcurrencyGate = field(default_factory=lambda: ConcurrencyGate(2))
    rate_limiter: GenerationRateLimiter = field(
        default_factory=lambda: GenerationRateLimiter(30)
    )

    def provider_status(self) -> dict[str, object]:
        ok, reason = self.provider.health_check()
        return {
            "name": self.provider.name,
            "ready": ok,
            "reason": reason,
            # The provider says whether its pictures are real, rather than this
            # guessing from its name: a stub called "recording" is just as much
            # a placeholder as the mock.
            "using_real_api": not self.provider.draws_placeholders,
            "max_concurrent_generations": self.gate.limit,
        }


def build_container(settings: Settings, provider: ImageProvider | None = None) -> Container:
    """Construct the graph. Pass `provider` to override it (tests do)."""
    resolved = provider or build_provider(settings)
    store = GenerationStore(settings.storage_dir)
    user_store: UserStore
    book_store: BookStore
    if settings.database_mode:
        # The durable pair: people and books live in Postgres and survive
        # restarts. GC mode is logged as the dialect only — never the URL,
        # which embeds the password.
        engine = build_engine(settings.database_url)
        user_store = DatabaseUserStore(engine)
        book_store = DatabaseBookStore(engine)
        logger.info(
            "container.built provider=%s env=%s persistence=database (%s)",
            resolved.name,
            settings.app_env,
            persistence_mode(engine),
        )
    else:
        # The offline pair: everything on disk. Local development, tests and
        # a deployment that has not been given a database yet.
        book_store = FileBookStore(settings.storage_dir / "books")
        user_store = FileUserStore(settings.storage_dir / "users")
        logger.info(
            "container.built provider=%s env=%s persistence=filesystem",
            resolved.name,
            settings.app_env,
        )
    gate = ConcurrencyGate(settings.max_concurrent_generations)
    return Container(
        settings=settings,
        provider=resolved,
        store=store,
        pipeline=GenerationPipeline(resolved, store),
        user_store=user_store,
        book_store=book_store,
        book_runner=BookRunner(
            provider=resolved,
            store=store,
            book_store=book_store,
            gate=gate,
            page_timeout_seconds=settings.generation_timeout_seconds,
        ),
        gate=gate,
        rate_limiter=GenerationRateLimiter(settings.max_generations_per_minute),
    )
