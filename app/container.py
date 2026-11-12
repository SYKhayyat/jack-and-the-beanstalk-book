"""Composition root.

The one module allowed to know about every concrete implementation. It reads
settings and wires the object graph; everything else receives what it needs
through FastAPI dependencies. Swapping the provider, the store location or the
pipeline is a change here and nowhere else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.engine import Engine

from app.blobs import install_blob_store
from app.books import SEEDS
from app.config import Settings
from app.db import build_engine, persistence_mode, prepare_database
from app.services.book_notice import BookReadyNotice
from app.services.book_runner import BookRunner
from app.services.book_store import BookStore
from app.services.child_profiles import ChildProfileStore, DatabaseChildProfileStore
from app.services.concurrency import ConcurrencyGate
from app.services.costs import (
    CostLedger,
    MeteredDecisionJudge,
    MeteredProvider,
    image_call_price,
    metered_reviewer,
    review_call_price,
)
from app.services.db_book_store import DatabaseBookStore
from app.services.db_wallet_store import DatabaseWalletStore
from app.services.decision_judge import (
    DecisionJudge,
    GeminiWebDecisionJudge,
    OpenAIDecisionJudge,
)
from app.services.gemini_web.factory import client_for
from app.services.image_providers.base import ImageProvider
from app.services.image_providers.registry import build_provider
from app.services.mailer import Mailer, ResendMailer
from app.services.payments import PaymentProvider, StripePaymentProvider
from app.services.pipeline import GenerationPipeline
from app.services.portrait_log import PortraitLog
from app.services.rate_limit import GenerationRateLimiter
from app.services.reviewer import GeminiWebReviewer, ImageReviewer, build_reviewer
from app.services.storage import GenerationStore
from app.services.template_store import TemplateStore
from app.services.user_store import DatabaseUserStore, UserStore
from app.services.wallet import Wallet, WalletStore

__all__ = ["Container", "build_container"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Container:
    """Everything the routes need, already constructed."""

    settings: Settings
    provider: ImageProvider
    store: GenerationStore
    pipeline: GenerationPipeline
    #: The one database: books, people, the wallet ledger and the story
    #: templates. Handed to the admin screens so they write through the same
    #: engine the routes read from.
    engine: Engine
    #: The stories: words, the character bible, plates, versions, the start
    #: gate, and the edition every book is pinned to.
    templates: TemplateStore
    # Everyone who has signed in, so a book can be filed under a person and
    # nobody else can read it.
    user_store: UserStore
    # The book pipeline: its own persistence root plus the runner that walks
    # page by page. The gate is shared with the interactive flow so books and
    # single-scene generations bound the same billable-call budget.
    book_store: BookStore
    book_runner: BookRunner
    #: The ledger every account's balance is summed from, and the payment
    #: provider money moves through. `payments` is None when no Stripe key is
    #: configured: the app then runs without top-ups, refunds or a book fee.
    wallet: Wallet
    payments: PaymentProvider | None
    #: Every billable model call, priced and tagged with its book (the cost
    #: panel's source). `provider` and the runner's reviewer write to it.
    costs: CostLedger
    #: Every portrait shown in the studio and every take picked (operator only).
    portraits: PortraitLog
    #: The one child each account asked the studio to remember, so the next
    #: book starts without a photo (Idea 2). Empty until somebody ticks it.
    child_profiles: ChildProfileStore
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


def build_container(
    settings: Settings,
    provider: ImageProvider | None = None,
    payments: PaymentProvider | None = None,
    reviewer: ImageReviewer | None = None,
    decision_judge: DecisionJudge | None = None,
) -> Container:
    """Construct the graph. Pass `provider`/`payments`/`reviewer`/`decision_judge`
    to override (tests do)."""
    unmetered = provider or build_provider(settings)
    resolved_payments: PaymentProvider | None = payments
    if resolved_payments is None and settings.payments_enabled:
        resolved_payments = StripePaymentProvider(settings.stripe_secret_key)
    wallet_store: WalletStore
    store = GenerationStore(settings.storage_dir)
    user_store: UserStore
    book_store: BookStore
    # One storage path, in every environment: a Postgres URL when the operator
    # set one, otherwise the SQLite file under STORAGE_DIR. Booting migrates
    # it, so a fresh checkout (or a deploy that added a migration) is ready
    # before the first request rather than failing on a missing table.
    # Pictures and PDFs go to R2 when it is configured; the database then
    # holds only pointers. Installed before the engine's first read or write.
    install_blob_store(settings)
    engine = build_engine(settings.effective_database_url)
    prepare_database(engine)
    # The one provider object every caller uses, so every image call — page,
    # retry, portrait, plate — lands in the cost ledger.
    costs = CostLedger(engine)
    resolved: ImageProvider = MeteredProvider(unmetered, costs, image_call_price(settings))
    # The stories are data the code reads at runtime, so they are loaded
    # before the first request: seeding is idempotent (a boot with unchanged
    # seeds writes nothing), and books saved before books pinned their story
    # are pinned to it once, here.
    templates = TemplateStore(engine)
    templates.seed(SEEDS)
    templates.pin_unpinned_books()
    user_store = DatabaseUserStore(engine)
    book_store = DatabaseBookStore(engine)
    wallet_store = DatabaseWalletStore(engine)
    # Logged as the dialect only — never the URL, which embeds the password.
    logger.info(
        "container.built provider=%s env=%s persistence=database (%s)",
        resolved.name,
        settings.app_env,
        persistence_mode(engine),
    )
    gate = ConcurrencyGate(settings.max_concurrent_generations)
    wallet = Wallet(wallet_store, picture_price_cents=settings.picture_price_cents)
    # The one mail this app sends (W2). Off — no key, or no From address — is a
    # complete answer: a book finishes and nothing is mailed, exactly as a
    # deployment without a Stripe key still makes free books.
    mailer: Mailer | None = (
        ResendMailer(settings.resend_api_key, settings.mail_from)
        if settings.resend_api_key.strip() and settings.mail_from.strip()
        else None
    )
    notice = (
        BookReadyNotice(
            mailer=mailer,
            users=user_store,
            site_url=settings.public_site_url,
            retention_days=settings.book_retention_days,
        )
        if mailer is not None
        else None
    )
    return Container(
        settings=settings,
        provider=resolved,
        store=store,
        pipeline=GenerationPipeline(resolved, store),
        engine=engine,
        templates=templates,
        user_store=user_store,
        book_store=book_store,
        book_runner=BookRunner(
            provider=resolved,
            store=store,
            book_store=book_store,
            templates=templates,
            gate=gate,
            page_timeout_seconds=settings.generation_timeout_seconds,
            wallet=wallet if resolved_payments is not None else None,
            # Built here, at start-up, so a production deployment with no
            # reviewer key fails before the first parent spends a credit rather
            # than after (T9.6).
            reviewer=metered_reviewer(
                reviewer
                or (
                    GeminiWebReviewer(client_for(settings))
                    if settings.reviewer_provider == "gemini_web"
                    else None
                )
                or build_reviewer(
                    api_key=(
                        settings.openai_api_key
                        if settings.reviewer_provider == "openai"
                        else settings.effective_reviewer_key
                    ),
                    provider=settings.reviewer_provider,
                    model=settings.reviewer_model,
                    required=settings.reviewer_required,
                    timeout_seconds=settings.reviewer_timeout_seconds,
                ),
                costs,
                review_call_price(settings),
            ),
            decision_judge=decision_judge
            or (
                MeteredDecisionJudge(
                    GeminiWebDecisionJudge(client_for(settings))
                    if settings.decision_judge_provider == "gemini_web"
                    else OpenAIDecisionJudge(
                        settings.openai_api_key,
                        model=settings.decision_judge_model,
                        timeout_seconds=settings.reviewer_timeout_seconds,
                    ),
                    costs,
                    # Free: the local judge reports no tokens, but say so.
                    0.0
                    if settings.decision_judge_provider == "gemini_web"
                    else settings.decision_judge_usd_per_million_tokens,
                )
                if settings.decision_judge_enabled
                else None
            ),
            page_attempts=settings.page_attempts,
            page_concurrency=settings.book_page_concurrency,
            image_threads=settings.image_work_threads,
            costs=costs,
            # What the review budget is a share of, and the spend the operator
            # is told about (C4).
            book_price_cents=wallet.price_cents,
            reviewer_budget_percent=settings.reviewer_budget_percent,
            retry_budget_percent=settings.book_retry_budget_percent,
            cost_alert_cents=settings.book_cost_alert_cents,
            notice=notice,
        ),
        wallet=wallet,
        payments=resolved_payments,
        costs=costs,
        portraits=PortraitLog(engine),
        child_profiles=DatabaseChildProfileStore(engine),
        gate=gate,
        rate_limiter=GenerationRateLimiter(settings.max_generations_per_minute),
    )
