"""The database: engine, tables and boot-time migration.

There is one storage path. `DATABASE_URL` names a Postgres database in
production; empty means a local SQLite file under `STORAGE_DIR`, so local
development and CI need no server and still exercise the same tables, the same
migrations and the same store code the deployment runs.

Two design points:

* **Sync engine, not async.** Every database access in this app already runs
  through `asyncio.to_thread` (the routes are sync functions and the book
  runner pushes blocking calls to worker threads), so an async driver would
  add a second execution model for nothing. Transactions are short; the pool
  is small.
* **`pool_pre_ping`.** Neon's pooled endpoint drops idle connections; the
  pre-ping turns that invisible instead of throwing `OperationalError` at
  the next request.

Connect arguments differ by dialect: SQLite (tests, local experiments) runs
multi-threaded through `to_thread`, so it needs `check_same_thread=False`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

__all__ = [
    "DEFAULT_STORY_KEY",
    "BookCharacterRow",
    "BookPageRow",
    "BookRow",
    "BookStoryCharacterRow",
    "BookStoryPageRow",
    "StoryCharacterRow",
    "StoryPageRow",
    "StoryRow",
    "TemplatePlateRow",
    "TemplateVersionCharacterRow",
    "TemplateVersionPlateRow",
    "TemplateVersionRow",
    "UserRow",
    "WalletRefundRow",
    "WalletTransactionRow",
    "build_engine",
    "database_secrets",
    "enforce_sqlite_foreign_keys",
    "local_database_url",
    "persistence_mode",
    "prepare_database",
]

logger = logging.getLogger(__name__)

#: Every book made before stories were data is a Jack book.
DEFAULT_STORY_KEY = "jack_and_the_beanstalk"


class Base(DeclarativeBase):
    """One metadata for every table."""


class UserRow(Base):
    """A person who signed in with Google."""

    __tablename__ = "users"

    sub: Mapped[str] = mapped_column(String(128), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    avatar: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class BookRow(Base):
    """One picture book: what was asked for, its state, and its own artefacts.

    The portrait and the finished PDF live here as blobs — hand-clothes
    bookkeeping across a second storage vendor was rejected on purpose ("DB
    blobs" was the ruling). `status_payload` is the exact document the
    browser polls, so a database-mode poll returns what a filesystem-mode
    poll returns, byte for byte.
    """

    __tablename__ = "books"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.sub"), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    generation_id: Mapped[str] = mapped_column(String(64))
    #: Hash of the request that started this book, so an identical retry finds
    #: it instead of billing a second one (`find_started`).
    request_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    about: Mapped[dict[str, Any]] = mapped_column(JSON)
    #: The settings the portrait and pages were made with, so a single page can
    #: be regenerated after the original draft has expired.
    settings: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    prompts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    status_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    portrait: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    #: The original photograph: the second identity anchor for an edit.
    source_photo: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    cover: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    pdf: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    #: Which story this is. Not a foreign key: books made before stories were
    #: data have no `stories` row to point at, and the pin below is the strong
    #: link for every book made on a template.
    story_key: Mapped[str] = mapped_column(String(64), default=DEFAULT_STORY_KEY)
    #: The resolved cover title ("Noa and the Wolf"): the story's title with the
    #: star's name in it, rewritten when the About edit changes the name.
    title: Mapped[str] = mapped_column(String(255), default="")
    #: The template version this book started on, never changed afterwards.
    #: Null for books made before templates existed. RESTRICT: a version a book
    #: uses cannot be deleted, only retired.
    template_version_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "template_versions.id",
            ondelete="RESTRICT",
            name="fk_books_template_version_id",
        ),
        nullable=True,
        index=True,
    )
    #: The cover's quality verdicts (T9). A pinned book cannot complete without it.
    cover_review: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    #: The cover's attempts, recorded exactly as a page's are, so a redrawn cover
    #: says how many billable calls it took and which one is on the book.
    cover_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    cover_chosen_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_best_effort: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class BookPageRow(Base):
    """One page's finished art. (book_id, page_number) is the key."""

    __tablename__ = "book_pages"

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), primary_key=True
    )
    page_number: Mapped[int] = mapped_column(primary_key=True)
    image: Mapped[bytes] = mapped_column(LargeBinary)
    media_type: Mapped[str] = mapped_column(String(50))
    #: Billable image calls spent on this page (T9 caps it at three).
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Every attempt's verdict, with its tier. Null until reviewed; re-saving the
    #: image clears it, so a redrawn page is never carried by an old verdict.
    review: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    chosen_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_effort: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Hex SHA-256 of `image`, written with it (T13's ETag).
    image_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)


class BookCharacterRow(Base):
    """A photographed cast member's portrait and source photo.

    Kept per book rather than per draft: a draft expires in a day, while a book
    lives for its retention window and must be editable throughout.
    """

    __tablename__ = "book_characters"

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), primary_key=True
    )
    cast_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    portrait: Mapped[bytes] = mapped_column(LargeBinary)
    portrait_media_type: Mapped[str] = mapped_column(String(50))
    photo: Mapped[bytes] = mapped_column(LargeBinary)
    photo_media_type: Mapped[str] = mapped_column(String(50))


class StoryRow(Base):
    """One story on the shelf. Mutable metadata; nothing a book pins lives here."""

    __tablename__ = "stories"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    blurb: Mapped[str] = mapped_column(Text)
    book_title: Mapped[str] = mapped_column(String(255))
    #: A small line under the cover title ("starring {name}"); "" for none.
    cover_subtitle: Mapped[str] = mapped_column(String(255), default="", server_default="")
    #: The cast key the uploaded child plays; on every page whether listed or not.
    star: Mapped[str] = mapped_column(String(64))
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    #: The star's default name per photo-gender answer, {"boy": .., "girl": ..}.
    names: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    #: The operator's start gate (T6.5): when true, a book of this story cannot
    #: start until the story has a complete template version. Never seeded.
    requires_template: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class StoryPageRow(Base):
    """One revision of one page's words and scene. Page 0 is the cover.

    Rows are append-only: a changed page is a new revision, so a template
    version that pinned the old one keeps reading the old words. There is no
    column for a character's look — that lives in the bible only.
    """

    __tablename__ = "story_pages"
    __table_args__ = (
        UniqueConstraint(
            "story_key", "page_number", "revision", name="uq_story_page_revision"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    story_key: Mapped[str] = mapped_column(ForeignKey("stories.key", ondelete="RESTRICT"))
    page_number: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    page_key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    prose: Mapped[str] = mapped_column(Text)
    beat: Mapped[str] = mapped_column(Text)
    emotion: Mapped[str] = mapped_column(Text)
    scene: Mapped[dict[str, Any]] = mapped_column(JSON)
    #: Cast keys as the seed lists them; the star is implied on every page.
    cast_keys: Mapped[list[str]] = mapped_column(JSON)
    text_band: Mapped[dict[str, Any]] = mapped_column(JSON)
    #: Hash of the seed content that wrote this revision (null if not seeded).
    seed_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class StoryCharacterRow(Base):
    """One revision of one cast member's entry in the character bible.

    The single source of a character's look. Append-only, like the pages: an
    operator's edit or a changed seed adds a revision and never rewrites one.
    """

    __tablename__ = "story_characters"
    __table_args__ = (
        UniqueConstraint(
            "story_key", "cast_key", "revision", name="uq_story_character_revision"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    story_key: Mapped[str] = mapped_column(ForeignKey("stories.key", ondelete="RESTRICT"))
    cast_key: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    display_name: Mapped[str] = mapped_column(String(255))
    #: How a page draws the character with no photograph.
    look: Mapped[str] = mapped_column(Text)
    #: How a page draws them when the uploader gave them a face (photoable only).
    look_with_photo: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The portrait pass's brief for a photographed supporting character.
    portrait: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The star's story costume, replacing the visitor's outfit (the red hood).
    costume: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The order the studio offers the cast in: the seed's order.
    position: Mapped[int] = mapped_column(Integer, default=0)
    photoable: Mapped[bool] = mapped_column(Boolean)
    reference_image: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    reference_media_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    #: "seed" or "operator".
    provenance: Mapped[str] = mapped_column(String(16))
    seed_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class TemplatePlateRow(Base):
    """One empty plate for one page: art with no people and no text.

    `slots` is a JSON list of {cast_key, rect: [x, y, w, h] in 0..1, scale,
    facing}. Status moves draft -> approved -> retired; image and prompt are
    never rewritten.
    """

    __tablename__ = "template_plates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    story_key: Mapped[str] = mapped_column(
        ForeignKey("stories.key", ondelete="RESTRICT"), index=True
    )
    page_number: Mapped[int] = mapped_column(Integer)
    image: Mapped[bytes] = mapped_column(LargeBinary)
    media_type: Mapped[str] = mapped_column(String(50))
    prompt: Mapped[str] = mapped_column(Text)
    prompt_hash: Mapped[str] = mapped_column(String(64))
    slots: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class TemplateVersionRow(Base):
    """The one thing a book pins: a complete, immutable set of plates, words and looks."""

    __tablename__ = "template_versions"
    __table_args__ = (
        UniqueConstraint("story_key", "number", name="uq_template_version_number"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    story_key: Mapped[str] = mapped_column(ForeignKey("stories.key", ondelete="RESTRICT"))
    #: 1, 2, 3 per story. The current version is the highest number.
    number: Mapped[int] = mapped_column(Integer)
    #: Hash of the words, looks and plate ids this version pins.
    content_hash: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class TemplateVersionPlateRow(Base):
    """Page N of a version: which plate and which revision of the words."""

    __tablename__ = "template_version_plates"

    template_version_id: Mapped[str] = mapped_column(
        ForeignKey("template_versions.id", ondelete="CASCADE"), primary_key=True
    )
    page_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    plate_id: Mapped[str] = mapped_column(
        ForeignKey("template_plates.id", ondelete="RESTRICT"), index=True
    )
    story_page_id: Mapped[int] = mapped_column(
        ForeignKey("story_pages.id", ondelete="RESTRICT")
    )


class TemplateVersionCharacterRow(Base):
    """A cast member of a version: which revision of the bible entry."""

    __tablename__ = "template_version_characters"

    template_version_id: Mapped[str] = mapped_column(
        ForeignKey("template_versions.id", ondelete="CASCADE"), primary_key=True
    )
    cast_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    character_id: Mapped[int] = mapped_column(
        ForeignKey("story_characters.id", ondelete="RESTRICT")
    )


class BookStoryPageRow(Base):
    """Page N of one book: the revision of the words and scene it is made from.

    Written once at start, never changed, so a re-seeded page or a new
    template version cannot change a book already made.
    """

    __tablename__ = "book_story_pages"

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), primary_key=True
    )
    page_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_page_id: Mapped[int] = mapped_column(
        ForeignKey("story_pages.id", ondelete="RESTRICT")
    )


class BookStoryCharacterRow(Base):
    """A cast member of one book: the bible revision its looks are read from."""

    __tablename__ = "book_story_characters"

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), primary_key=True
    )
    cast_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    character_id: Mapped[int] = mapped_column(
        ForeignKey("story_characters.id", ondelete="RESTRICT")
    )


class WalletTransactionRow(Base):
    """One signed movement in an account's ledger.

    The balance is the sum of these rows, never a column of its own: that is
    what keeps the balance and the transaction history from disagreeing. A
    `(user, kind, reference)` triple is unique, so a re-delivered Stripe
    session or a retried book charge cannot be applied twice.
    """

    __tablename__ = "wallet_transactions"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "kind", "reference", name="uq_wallet_transaction_reference"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(24))
    description: Mapped[str] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: The Stripe PaymentIntent behind a funding credit, so a refund knows
    #: which payment to send money back against.
    payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ApiCallRow(Base):
    """One billable call to a model: an image, a retry, or a review.

    The cost ledger behind "what a book costs to make". Written by the metered
    provider and reviewer (`app/services/costs.py`) on every call, success or
    failure, so retries and review calls are counted rather than estimated.

    No foreign key to `books` on purpose: deleting a book must not erase what it
    cost. `cost_microusd` is null when the call's price is not known for the
    configured backend, so an unpriced call is visible instead of counted as
    free.
    """

    __tablename__ = "api_calls"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: "image" or "review".
    kind: Mapped[str] = mapped_column(String(16))
    #: "portrait", "cover", "page", "retry", "review", "plate" or "other".
    category: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    book_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: The portrait draft the call belongs to; a book carries its draft's id, so
    #: the portrait it was made from is part of its cost.
    generation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    images: Mapped[int] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean)
    cost_microusd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class WalletRefundRow(Base):
    """A requested refund: how much, where it got to, and why it stopped."""

    __tablename__ = "wallet_refunds"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    provider_refund_ids: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


def normalise_database_url(database_url: str) -> str:
    """Rewrite the URL into a dialect the installed driver can serve.

    Two rewrites, both about real pasted URLs:

    * `postgres://` (the scheme people still paste from Heroku-era docs)
      means the same thing as `postgresql://`.
    * A bare `postgresql://` asks for psycopg2, which is not installed; this
      app ships psycopg3, so the driver is named explicitly.
    """
    url = make_url(database_url)
    if url.drivername in ("postgres", "postgresql"):
        url = url.set(drivername="postgresql+psycopg")
    return url.render_as_string(hide_password=False)


def database_secrets(database_url: str) -> tuple[str, ...]:
    """The credential fragments of a connection URL that must never be logged.

    The whole URL is one secret because it is pasted whole and already contains
    the password. The password itself is armed separately: a URL can reach a log
    line split into parts (a repr, a per-field debug line), and the fragment is
    what identifies the credential in those cases.

    A malformed URL still yields the raw string — refusing to parse is no reason
    to leave a pasted credential unprotected.
    """
    if not database_url:
        return ()
    try:
        url = make_url(database_url)
    except Exception:
        return (database_url,)
    secrets = [database_url]
    if url.password:
        secrets.append(url.password)
    return tuple(secrets)


def local_database_url(storage_dir: Path) -> str:
    """The SQLite URL an empty `DATABASE_URL` means.

    One file under the storage directory: it is the whole store (books, pages,
    PDFs, people, the story templates), so deleting it is how a developer
    starts over. Four slashes would be needed for an absolute Windows path;
    `Path` renders the platform's own separators and SQLite accepts them.
    """
    return f"sqlite:///{Path(storage_dir) / 'app.sqlite3'}"


def build_engine(database_url: str) -> Engine:
    """Build the one engine for the process.

    A SQLite engine gets foreign keys enforced from its first connection, so a
    local database refuses the same broken writes Postgres would.

    Raises:
        ValueError: when the URL is empty or not a URL the installed driver
            can handle (the caller turns that into a loud startup failure).
    """
    if not database_url:
        raise ValueError("there is no database URL to build an engine from")
    url = make_url(normalise_database_url(database_url))
    connect_args = {"check_same_thread": False} if url.drivername.startswith("sqlite") else {}
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=5,
        connect_args=connect_args,
    )
    return enforce_sqlite_foreign_keys(engine)


def prepare_database(engine: Engine) -> Engine:
    """Bring the database up to the schema the code expects, at boot.

    Runs the same Alembic chain as `make migrate`, against this process's own
    engine: a fresh SQLite file is created and built, an existing Postgres
    database gets whatever this deploy added, and a database already at the
    head is a cheap no-op. Nothing else in the app creates tables, so the
    deployed schema is always a migrated one.

    The engine is handed to Alembic through `config.attributes` rather than
    through `DATABASE_URL`, because this runs inside the app process: the URL
    is already parsed (and may embed a password), and other threads — and the
    test suite — must not see it change underneath them.

    Returns the engine, so a boot can be one expression.
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    config.attributes["engine"] = engine
    command.upgrade(config, "head")
    return engine


def enforce_sqlite_foreign_keys(engine: Engine) -> Engine:
    """Make a SQLite engine enforce foreign keys, as Postgres always does.

    Applied by `build_engine`, so every engine the app builds has it. It stays
    public because a test (or a tool) that calls `create_engine` directly gets
    SQLite's default — no enforcement — unless it asks for this. A no-op for
    any other dialect.
    """
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _sqlite_foreign_keys_on)
    return engine


def _sqlite_foreign_keys_on(dbapi_connection: Any, _record: Any) -> None:
    """SQLite ignores foreign keys unless asked, per connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def persistence_mode(engine: Engine) -> str:
    """The one word about the database allowed in a log line: the dialect.

    The URL itself is never logged — it embeds the password.
    """
    dialect = engine.dialect.name
    return {"postgresql": "postgres"}.get(dialect, dialect)
