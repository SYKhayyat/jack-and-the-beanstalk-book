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
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy import false as sa_false
from sqlalchemy import true as sa_true
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

from app.blobs import BlobRef

__all__ = [
    "DEFAULT_STORY_KEY",
    "BookCharacterRow",
    "BookPageRow",
    "BookRow",
    "BookStoryCharacterRow",
    "BookStoryPageRow",
    "ChildProfileRow",
    "PrintOrderRow",
    "RuntimeSwitchRow",
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
    "configure_sqlite",
    "database_secrets",
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
    #: When this book's render first started: the window a restart may resume it
    #: inside. Never moved by a resume, so repeated crashes cannot extend it.
    job_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: How many times a restart has picked this book up again. Bounded, so a
    #: book that is what kills the server cannot keep killing it.
    resume_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: The worker rendering this book, and when its claim runs out (B9). A job
    #: lives in the process but its lease lives here, so a restart — or a
    #: second instance — can tell an abandoned book from one still being drawn.
    #: Renewed by a heartbeat while the job runs, and cleared when it ends.
    job_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    job_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    about: Mapped[dict[str, Any]] = mapped_column(JSON)
    #: The settings the portrait and pages were made with, so a single page can
    #: be regenerated after the original draft has expired.
    settings: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    prompts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    status_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    portrait: Mapped[bytes | None] = mapped_column(BlobRef, nullable=True)
    #: The original photograph: the second identity anchor for an edit.
    source_photo: Mapped[bytes | None] = mapped_column(BlobRef, nullable=True)
    cover: Mapped[bytes | None] = mapped_column(BlobRef, nullable=True)
    pdf: Mapped[bytes | None] = mapped_column(BlobRef, nullable=True)
    #: Hex SHA-256 of `cover` and of `portrait`, written with them. The version
    #: in a picture's URL, so the browser may keep it for good (P1), and the
    #: answer to "has this picture changed?" without reading its bytes.
    cover_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    portrait_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    #: Whether every picture must carry a verdict before the book can complete.
    #: True for every book made since every picture is reviewed; False only for
    #: books made before that, whose pictures nothing looked at and never will.
    review_required: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa_true()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class BookPageRow(Base):
    """One page's finished art. (book_id, page_number) is the key."""

    __tablename__ = "book_pages"

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), primary_key=True
    )
    page_number: Mapped[int] = mapped_column(primary_key=True)
    image: Mapped[bytes] = mapped_column(BlobRef)
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


class PictureVariantRow(Base):
    """A smaller copy of one of a book's pictures, made for the screen (P1, P2).

    The stored picture is the provider's full-size PNG, which the PDF wants;
    a phone turning pages does not. A variant is made the first time a width
    is asked for and kept here, keyed by the picture it was made from: when
    the picture is redrawn its hash changes, and the next request replaces the
    stale copy instead of serving it.
    """

    __tablename__ = "picture_variants"

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), primary_key=True
    )
    #: "cover", "portrait" or "page-<n>".
    slot: Mapped[str] = mapped_column(String(16), primary_key=True)
    width: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: The hash of the picture this copy was made from.
    source_sha256: Mapped[str] = mapped_column(String(64))
    image: Mapped[bytes] = mapped_column(BlobRef)
    media_type: Mapped[str] = mapped_column(String(50))


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
    portrait: Mapped[bytes] = mapped_column(BlobRef)
    portrait_media_type: Mapped[str] = mapped_column(String(50))
    photo: Mapped[bytes] = mapped_column(BlobRef)
    photo_media_type: Mapped[str] = mapped_column(String(50))
    #: The portrait draft this face came from (`portrait_attempts.id`), which
    #: leads to every attempt, chip and take behind it. Null for rows written
    #: before it was recorded.
    generation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The look settings the portrait was drawn with (`CartoonSettings` as
    #: JSON): this member's gender, style, age and the rest, not the star's.
    settings: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class RuntimeSwitchRow(Base):
    """An operator switch that changes what the app will do, right now (Idea 10).

    Two facts only: whether spending is stopped at the door (`new_books`) and
    whether the pages still being drawn should stop (`drawing`). They live in
    the database rather than the environment because the point of a kill
    switch is to work in the second it is needed, not at the next deploy — and
    because a running render has to see the same answer the page shows.

    A missing row means off, so a fresh database needs no seeding.
    """

    __tablename__ = "runtime_switches"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    on: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    #: Why, in the operator's own words; shown on the page.
    note: Mapped[str] = mapped_column(String(255), default="", server_default="")
    #: Who last changed it, so a switch nobody admits to pulling has an owner.
    changed_by: Mapped[str] = mapped_column(String(320), default="", server_default="")
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ChildProfileRow(Base):
    """A child the studio was asked to remember, so the next book skips the photo.

    One row per account: the name, the boy/girl answer, the approved portrait
    and the photograph it was drawn from. A saved portrait is handed back to
    the studio as a fresh draft, so a second book costs no upload and no image
    call. The story key is stored with it because a portrait is drawn in its
    tale's costume (Red's cloak is not Jack's tunic), so it can only be reused
    by the story that made it.

    Only the star is ever saved here. A cast member's face is part of a book,
    not of the person who made it.
    """

    __tablename__ = "child_profiles"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.sub", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(60), default="", server_default="")
    #: "boy" or "girl", the studio's one required answer; "" if never given.
    photo_gender: Mapped[str] = mapped_column(String(10), default="", server_default="")
    story_key: Mapped[str] = mapped_column(String(64), default=DEFAULT_STORY_KEY)
    #: The look settings the portrait was drawn with (`CartoonSettings` as JSON).
    settings: Mapped[dict[str, Any]] = mapped_column(JSON)
    portrait: Mapped[bytes] = mapped_column(BlobRef)
    portrait_media_type: Mapped[str] = mapped_column(String(50))
    #: Hex SHA-256 of `portrait`, so the studio's URL changes when it does.
    portrait_sha256: Mapped[str] = mapped_column(String(64))
    #: The photograph behind the portrait: the second identity anchor a page
    #: sends, and what a new tale's portrait would be drawn from.
    photo: Mapped[bytes] = mapped_column(BlobRef)
    photo_media_type: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class PortraitAttemptRow(Base):
    """One portrait the visitor was shown, or one take they picked.

    The studio's history, for the operator only: what was asked for (the look
    settings), what was wrong with the last one (the ticked chips), which
    backend drew it, and which take was kept. No image is stored here — the
    pictures live in the expiring draft — so nothing personal outlives the
    draft but a settings blob and a few labels.

    A `draw` row is one call to the image model. A `choose` row is the visitor
    picking take `chosen_index` of the draft `source_id`; its id is the new
    draft that holds that one take, and is what a book or a cast member points
    at.
    """

    __tablename__ = "portrait_attempts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    story_key: Mapped[str] = mapped_column(String(64))
    #: "" for the book's star, otherwise the photographed cast member.
    cast_key: Mapped[str] = mapped_column(String(64), default="")
    #: "draw" or "choose".
    kind: Mapped[str] = mapped_column(String(16))
    settings: Mapped[dict[str, Any]] = mapped_column(JSON)
    #: The rejection chips ticked on the portrait before this one (draw only).
    rejection: Mapped[list[str]] = mapped_column(JSON)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    #: How many takes the call returned (draw), or 1 (choose).
    takes: Mapped[int] = mapped_column(Integer)
    #: Which take was picked (choose only).
    chosen_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The draft the take was picked from (choose only).
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


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
    reference_image: Mapped[bytes | None] = mapped_column(BlobRef, nullable=True)
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
    image: Mapped[bytes] = mapped_column(BlobRef)
    media_type: Mapped[str] = mapped_column(String(50))
    prompt: Mapped[str] = mapped_column(Text)
    prompt_hash: Mapped[str] = mapped_column(String(64))
    slots: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class TemplatePortraitRow(Base):
    """A fixed portrait of one supporting character: the face a book draws them with.

    Used when the visitor uploaded no photo of that character. Status moves
    draft -> approved -> retired, as plates do; image and media type are never
    rewritten. A version pins the approved portrait it was made with.
    """

    __tablename__ = "template_portraits"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    story_key: Mapped[str] = mapped_column(
        ForeignKey("stories.key", ondelete="RESTRICT"), index=True
    )
    cast_key: Mapped[str] = mapped_column(String(64))
    image: Mapped[bytes] = mapped_column(BlobRef)
    media_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(16))
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class TemplatePremadeRow(Base):
    """A finished picture for a page the star is not in, made once from its plate.

    Used instead of an image call when the visitor gave no photo of anyone on
    the page, so the page is the same in every such book. It belongs to the
    plate it was painted onto. Status moves draft -> approved -> retired, as
    plates do; the image is never rewritten. A version pins the approved one.
    """

    __tablename__ = "template_premade_pages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    story_key: Mapped[str] = mapped_column(
        ForeignKey("stories.key", ondelete="RESTRICT"), index=True
    )
    page_number: Mapped[int] = mapped_column(Integer)
    plate_id: Mapped[str] = mapped_column(
        ForeignKey("template_plates.id", ondelete="RESTRICT"), index=True
    )
    image: Mapped[bytes] = mapped_column(BlobRef)
    media_type: Mapped[str] = mapped_column(String(50))
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
    #: The pre-made picture this version uses for the page, or NULL to draw it.
    premade_id: Mapped[str | None] = mapped_column(
        ForeignKey("template_premade_pages.id", ondelete="RESTRICT"), nullable=True, index=True
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
    #: The fixed portrait this version draws the character with, or NULL for none.
    portrait_id: Mapped[str | None] = mapped_column(
        ForeignKey("template_portraits.id", ondelete="RESTRICT"), nullable=True, index=True
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
    #: The token bill a token-billed backend reported for the call (OpenAI's
    #: images); null for backends priced per call, and for failed calls.
    text_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class PrintOrderRow(Base):
    """One printed copy of one book: its approval, address and provider job.

    A row starts life as an *approval*: the owner has looked at the exact PDF
    whose hash is stored here. Placing the order fills in the address and the
    provider's id and moves the state to `submitted`. A redraw produces a new
    PDF hash, so the old approval no longer matches and the page asks the owner
    to review the new book before anything can be sent.

    There is no card number or payment credential here: the print price is
    charged through the wallet ledger, and the provider in this build is a
    test environment by construction.
    """

    __tablename__ = "print_orders"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    #: approved | placing | submitted | failed | superseded
    state: Mapped[str] = mapped_column(String(16))
    #: The exact book the owner approved, so an edit cannot slip past them.
    pdf_sha256: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[int] = mapped_column(Integer)
    printing_cents: Mapped[int] = mapped_column(Integer)
    shipping_cents: Mapped[int] = mapped_column(Integer)
    retail_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    provider: Mapped[str] = mapped_column(String(32), default="")
    provider_order_id: Mapped[str] = mapped_column(String(128), default="")
    address: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


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
    connect_args: dict[str, Any] = {}
    if url.drivername.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    elif url.drivername.startswith("postgresql"):
        # Neon and Supabase hand out pooled (pgbouncer-style) connections in
        # transaction mode, where a server-side prepared statement lands on a
        # different backend than the one that prepared it. Never prepare.
        connect_args = {"prepare_threshold": None}
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=5,
        connect_args=connect_args,
    )
    return configure_sqlite(engine)


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


def configure_sqlite(engine: Engine) -> Engine:
    """Give a SQLite engine the three Postgres habits the app depends on.

    Applied by `build_engine`, so every engine the app builds has them. It stays
    public because a test (or a tool) that calls `create_engine` directly gets
    SQLite's defaults unless it asks for this. A no-op for any other dialect.

    * **Foreign keys**, which SQLite does not enforce unless asked.
    * **WAL rather than the rollback journal.** A book's twenty pages are a few
      hundred small commits, and a journaled commit is an fsync: measured at
      ~80 ms each on a development machine, which is most of the ten seconds a
      mock book used to take to render, and much of the test suite's wall time.
      WAL appends instead, so commits stop waiting for the disk. With
      `synchronous=NORMAL` the WAL is synced at a checkpoint rather than on
      every commit: an OS crash can lose the last few commits of a local
      database, never corrupt it. Production runs on Postgres, where this does
      not apply.
    * **A writer that waits** (`busy_timeout`) instead of failing with
      "database is locked". SQLite serialises writers; a render writing from
      twenty page threads at once is a queue, not an error — and with the
      default 5 s timeout it used to surface as one under load.
    """
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _sqlite_pragmas_on)
    return engine


def _sqlite_pragmas_on(dbapi_connection: Any, _record: Any) -> None:
    """Per-connection settings; see `configure_sqlite`."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=15000")
        # A no-op (not an error) for an in-memory database, which cannot use WAL.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def persistence_mode(engine: Engine) -> str:
    """The one word about the database allowed in a log line: the dialect.

    The URL itself is never logged — it embeds the password.
    """
    dialect = engine.dialect.name
    return {"postgresql": "postgres"}.get(dialect, dialect)
