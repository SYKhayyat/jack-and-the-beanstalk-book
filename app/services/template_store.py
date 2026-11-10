"""Story templates: the words, the character bible, the plates, and the versions books pin.

This is the T4 contract (`docs/book-template-plan.md`). Four rules hold it up:

* **Revisions, not edits.** A page's words and a character's look are
  append-only rows. Changing either adds a revision; nothing a version pinned
  is ever rewritten.
* **A version is a complete snapshot.** It names one approved plate and one
  page revision for the cover and every story page, and one bible revision for
  every cast member those pages use (the star included). It is never changed
  after it is written; the current version is the highest number.
* **A book pins once.** A book is saved with the edition it starts from
  (`edition_for_new_book`): the exact page and bible revisions, and the
  template version when there is one. Nothing moves a book afterwards;
  `book_edition` reads it back.
* **In use means undeletable.** A version a book pins, and a plate any version
  names, can be retired but not deleted. Both the store and the foreign keys
  say so; the store says it first, with a clear error.

Every method is one transaction on its own connection, like `DatabaseBookStore`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, delete, func, insert, select, update
from sqlalchemy.engine import Engine

from app.books import PAGE_COUNT, SeedScene, StorySeed
from app.db import (
    BookRow,
    BookStoryCharacterRow,
    BookStoryPageRow,
    StoryCharacterRow,
    StoryPageRow,
    StoryRow,
    TemplatePlateRow,
    TemplateVersionCharacterRow,
    TemplateVersionPlateRow,
    TemplateVersionRow,
)
from app.models.story import StoryEdition, edition_from_content

__all__ = [
    "COVER_PAGE",
    "FACINGS",
    "NOT_READY",
    "PAGE_NUMBERS",
    "NoTemplateVersion",
    "PageSummary",
    "PlateArt",
    "PlateNotApprovable",
    "PlateStatus",
    "PlateSummary",
    "SeedReport",
    "Slot",
    "StoryState",
    "TemplateError",
    "TemplateInUse",
    "TemplateStore",
    "TemplateVersion",
    "UnknownTemplate",
    "VersionCharacter",
    "VersionPage",
    "seed_edition",
    "write_pins",
]

logger = logging.getLogger(__name__)

#: Page 0 is the cover; story pages are 1..PAGE_COUNT.
COVER_PAGE = 0
PAGE_NUMBERS = tuple(range(COVER_PAGE, PAGE_COUNT + 1))

FACINGS = frozenset({"left", "right", "toward", "away"})


class PlateStatus:
    DRAFT = "draft"
    APPROVED = "approved"
    RETIRED = "retired"


class TemplateError(ValueError):
    """A template operation was refused. The message is safe to show an operator."""


class UnknownTemplate(TemplateError):
    """No such story, plate, version, character or book."""


class TemplateInUse(TemplateError):
    """A plate or version is referenced and can only be retired."""


class PlateNotApprovable(TemplateError):
    """The plate cannot become approved (retired, or its slots don't fit the page)."""


class NoTemplateVersion(TemplateError):
    """The story requires a template and has no complete version, so no book can start.

    The message is the one plain sentence the shelf and the start endpoint show.
    """


#: What a visitor is told when the start gate is on and the plates aren't ready.
NOT_READY = "This story's pictures are still being prepared, so a book can't be started yet."


@dataclass(frozen=True, slots=True)
class PlateArt:
    """One page's approved plate, ready to be edited: the image and its slots.

    The image is the bytes the painter produced (the imported file, or one a
    provider painted) — never re-cropped here, because the slots were measured
    against it and a crop would move them.
    """

    plate_id: str
    image: bytes
    media_type: str
    slots: tuple[Slot, ...]


@dataclass(frozen=True, slots=True)
class Slot:
    """Where one cast member goes on a plate.

    `rect` is (x, y, width, height) as fractions of the art box, origin top
    left. `scale` is the character's height as a fraction of the art height.
    """

    cast_key: str
    rect: tuple[float, float, float, float]
    scale: float
    facing: str

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Slot:
        try:
            cast_key = str(raw["cast_key"])
            x, y, w, h = (float(value) for value in raw["rect"])
            scale = float(raw["scale"])
            facing = str(raw["facing"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TemplateError(f"a slot is malformed: {exc}") from exc
        slot = cls(cast_key=cast_key, rect=(x, y, w, h), scale=scale, facing=facing)
        slot.check()
        return slot

    def check(self) -> None:
        x, y, w, h = self.rect
        if not self.cast_key:
            raise TemplateError("a slot has no cast key")
        if min(x, y) < 0 or w <= 0 or h <= 0 or x + w > 1 or y + h > 1:
            raise TemplateError(f"slot {self.cast_key!r} lies outside the art box")
        if not 0 < self.scale <= 1:
            raise TemplateError(f"slot {self.cast_key!r} scale must be in (0, 1]")
        if self.facing not in FACINGS:
            raise TemplateError(
                f"slot {self.cast_key!r} facing must be one of {sorted(FACINGS)}"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "cast_key": self.cast_key,
            "rect": list(self.rect),
            "scale": self.scale,
            "facing": self.facing,
        }


@dataclass(frozen=True, slots=True)
class VersionPage:
    """One page as a version pinned it. Carries cast keys, never a look."""

    page_number: int
    page_key: str
    title: str
    prose: str
    beat: str
    emotion: str
    scene: Mapping[str, Any]
    cast_keys: tuple[str, ...]
    text_band: Mapping[str, Any]
    plate_id: str
    slots: tuple[Slot, ...]


@dataclass(frozen=True, slots=True)
class VersionCharacter:
    """One bible entry as a version pinned it: the only place a look lives."""

    cast_key: str
    display_name: str
    look: str
    photoable: bool
    has_reference_image: bool
    revision: int
    look_with_photo: str = ""
    portrait: str = ""
    costume: str = ""


@dataclass(frozen=True, slots=True)
class TemplateVersion:
    id: str
    story_key: str
    number: int
    content_hash: str
    reason: str
    pages: tuple[VersionPage, ...]
    characters: Mapping[str, VersionCharacter]

    def page(self, number: int) -> VersionPage:
        for page in self.pages:
            if page.page_number == number:
                return page
        raise UnknownTemplate(f"version {self.number} has no page {number}")


@dataclass(frozen=True, slots=True)
class PageSummary:
    """The latest revision of one page: what the admin screen reviews against.

    There is no look here, because a page never had one — the cast is keys, and
    the words are for reading on the review screen rather than for the prompt.
    """

    page_number: int
    page_key: str
    title: str
    prose: str
    cast_keys: tuple[str, ...]
    text_band: Mapping[str, Any]
    revision: int


@dataclass(frozen=True, slots=True)
class PlateSummary:
    """A plate's metadata, never its bytes: a contact sheet must stay cheap."""

    id: str
    story_key: str
    page_number: int
    status: str
    prompt: str
    prompt_hash: str
    slots: tuple[Slot, ...]
    media_type: str
    created_at: datetime
    approved_by: str | None
    approved_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoryState:
    """One story as the shelf and the admin screen see it."""

    key: str
    title: str
    blurb: str
    #: The finished book's title, with ``{name}`` where the star's name goes.
    book_title: str
    #: The line under the cover title ("starring {name}"); "" for none.
    cover_subtitle: str
    available: bool
    #: The operator's start gate (T6.5).
    requires_template: bool
    #: The current template version's number, or None when there is none.
    version_number: int | None

    @property
    def can_start(self) -> bool:
        """Whether a book of this story may start right now."""
        return self.available and (
            not self.requires_template or self.version_number is not None
        )


@dataclass(slots=True)
class SeedReport:
    """What one seeding run changed. All zeros means the run was a no-op."""

    stories: int = 0
    page_revisions: int = 0
    character_revisions: int = 0
    versions: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.page_revisions or self.character_revisions or self.versions)


class TemplateStore:
    """The T4 contract over the database."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        # A book's edition never changes once it is saved, so it is read from
        # the database once per process and kept (bounded) after that.
        self._book_editions: OrderedDict[str, StoryEdition] = OrderedDict()
        self._book_editions_lock = threading.Lock()

    # --- seeding ---------------------------------------------------------------

    def seed(self, seeds: Iterable[StorySeed]) -> SeedReport:
        """Load reviewed seed files into the database. Idempotent.

        A page or character whose seed content is unchanged since it was last
        seeded is left alone — including a character an operator has since
        revised, so a re-seed never undoes an operator's edit. A changed seed
        adds a revision, which (if the story has plates approved for every
        page) makes a new template version. Existing versions are untouched.
        """
        report = SeedReport()
        with self._engine.begin() as conn:
            for seed in seeds:
                self._seed_story(conn, seed, report)
                before = _current_version_id(conn, seed.key)
                version = self._refresh_version(conn, seed.key, "seed changed")
                if version is not None and version != before:
                    report.versions.append(version)
        logger.info(
            "templates.seeded stories=%d pages=%d characters=%d versions=%d",
            report.stories,
            report.page_revisions,
            report.character_revisions,
            len(report.versions),
        )
        return report

    def _seed_story(self, conn: Connection, seed: StorySeed, report: SeedReport) -> None:
        now = _now()
        values = {
            "title": seed.title,
            "blurb": seed.blurb,
            "book_title": seed.book_title,
            "cover_subtitle": seed.cover_subtitle,
            "star": seed.star,
            "available": seed.available,
            "names": dict(seed.names),
        }
        existing = conn.execute(select(StoryRow).where(StoryRow.key == seed.key)).one_or_none()
        if existing is None:
            conn.execute(
                insert(StoryRow).values(key=seed.key, created_at=now, updated_at=now, **values)
            )
            report.stories += 1
        elif any(getattr(existing, name) != value for name, value in values.items()):
            conn.execute(
                update(StoryRow)
                .where(StoryRow.key == seed.key)
                .values(updated_at=now, **values)
            )

        for number, content in _seed_pages(seed):
            seed_hash = _hash(content)
            latest = conn.execute(
                select(StoryPageRow.revision, StoryPageRow.seed_hash)
                .where(
                    StoryPageRow.story_key == seed.key,
                    StoryPageRow.page_number == number,
                )
                .order_by(StoryPageRow.revision.desc())
                .limit(1)
            ).one_or_none()
            if latest is not None and latest.seed_hash == seed_hash:
                continue
            conn.execute(
                insert(StoryPageRow).values(
                    story_key=seed.key,
                    page_number=number,
                    revision=(latest.revision + 1) if latest is not None else 1,
                    seed_hash=seed_hash,
                    created_at=now,
                    **content,
                )
            )
            report.page_revisions += 1

        for cast_key, content in _seed_characters(seed):
            seed_hash = _hash(content)
            last_seeded = conn.execute(
                select(StoryCharacterRow.seed_hash)
                .where(
                    StoryCharacterRow.story_key == seed.key,
                    StoryCharacterRow.cast_key == cast_key,
                    StoryCharacterRow.seed_hash.is_not(None),
                )
                .order_by(StoryCharacterRow.revision.desc())
                .limit(1)
            ).scalar_one_or_none()
            if last_seeded == seed_hash:
                continue
            conn.execute(
                insert(StoryCharacterRow).values(
                    story_key=seed.key,
                    cast_key=cast_key,
                    revision=_next_character_revision(conn, seed.key, cast_key),
                    provenance="seed",
                    seed_hash=seed_hash,
                    created_at=now,
                    **content,
                )
            )
            report.character_revisions += 1

    # --- the character bible ---------------------------------------------------

    def revise_character(
        self,
        story_key: str,
        cast_key: str,
        *,
        look: str | None = None,
        look_with_photo: str | None = None,
        portrait: str | None = None,
        costume: str | None = None,
        display_name: str | None = None,
        reference_image: bytes | None = None,
        reference_media_type: str | None = None,
    ) -> str | None:
        """An operator's change to a pinned look. Returns the new version id, if any.

        Adds a revision rather than editing one, so every book already pinned
        keeps the look it was drawn with (T4.1, T7.5). A non-photoable
        character can never be given a reference image.
        """
        with self._engine.begin() as conn:
            latest = conn.execute(
                select(StoryCharacterRow)
                .where(
                    StoryCharacterRow.story_key == story_key,
                    StoryCharacterRow.cast_key == cast_key,
                )
                .order_by(StoryCharacterRow.revision.desc())
                .limit(1)
            ).one_or_none()
            if latest is None:
                raise UnknownTemplate(f"{story_key!r} has no cast member {cast_key!r}")
            if reference_image is not None and not latest.photoable:
                raise TemplateError(f"{cast_key!r} cannot have a reference image")
            if look is not None and not look.strip():
                raise TemplateError("a look cannot be blank")
            if (look_with_photo or portrait) and not latest.photoable:
                raise TemplateError(
                    f"{cast_key!r} cannot be photographed, so has no photo look"
                )
            conn.execute(
                insert(StoryCharacterRow).values(
                    story_key=story_key,
                    cast_key=cast_key,
                    revision=latest.revision + 1,
                    display_name=display_name or latest.display_name,
                    look=look.strip() if look is not None else latest.look,
                    look_with_photo=_revised(look_with_photo, latest.look_with_photo),
                    portrait=_revised(portrait, latest.portrait),
                    costume=_revised(costume, latest.costume),
                    position=latest.position,
                    photoable=latest.photoable,
                    reference_image=(
                        reference_image
                        if reference_image is not None
                        else latest.reference_image
                    ),
                    reference_media_type=(
                        reference_media_type
                        if reference_image is not None
                        else latest.reference_media_type
                    ),
                    provenance="operator",
                    seed_hash=None,
                    created_at=_now(),
                )
            )
            return self._refresh_version(conn, story_key, f"look updated: {cast_key}")

    # --- reading the review surfaces -------------------------------------------

    def pages(self, story_key: str) -> dict[int, PageSummary]:
        """Every page's latest revision, keyed by page number.

        Reads only the words: the admin screen shows what a plate will carry.
        A revision is append-only, so "latest" is the highest `revision` per
        page number.
        """
        with self._engine.connect() as conn:
            _require_story(conn, story_key)
            rows = conn.execute(
                select(
                    StoryPageRow.page_number,
                    StoryPageRow.page_key,
                    StoryPageRow.title,
                    StoryPageRow.prose,
                    StoryPageRow.cast_keys,
                    StoryPageRow.text_band,
                    StoryPageRow.revision,
                )
                .where(StoryPageRow.story_key == story_key)
                .order_by(StoryPageRow.page_number, StoryPageRow.revision)
            ).all()
        latest: dict[int, PageSummary] = {}
        for row in rows:
            latest[int(row.page_number)] = PageSummary(
                page_number=int(row.page_number),
                page_key=row.page_key,
                title=row.title,
                prose=row.prose,
                cast_keys=tuple(row.cast_keys),
                text_band=row.text_band,
                revision=int(row.revision),
            )
        return latest

    def plates(self, story_key: str, page_number: int | None = None) -> list[PlateSummary]:
        """A story's plates (or one page's), in page order and newest first.

        The image column is deliberately not selected: a contact sheet of 21
        pages must not pull 21 blobs to render a list.
        """
        with self._engine.connect() as conn:
            _require_story(conn, story_key)
            query = select(
                TemplatePlateRow.id,
                TemplatePlateRow.story_key,
                TemplatePlateRow.page_number,
                TemplatePlateRow.status,
                TemplatePlateRow.prompt,
                TemplatePlateRow.prompt_hash,
                TemplatePlateRow.slots,
                TemplatePlateRow.media_type,
                TemplatePlateRow.created_at,
                TemplatePlateRow.approved_by,
                TemplatePlateRow.approved_at,
            ).where(TemplatePlateRow.story_key == story_key)
            if page_number is not None:
                query = query.where(TemplatePlateRow.page_number == page_number)
            rows = conn.execute(
                query.order_by(
                    TemplatePlateRow.page_number, TemplatePlateRow.created_at.desc()
                )
            ).all()
        return [
            PlateSummary(
                id=row.id,
                story_key=row.story_key,
                page_number=int(row.page_number),
                status=row.status,
                prompt=row.prompt,
                prompt_hash=row.prompt_hash,
                slots=tuple(Slot.from_json(slot) for slot in row.slots),
                media_type=row.media_type,
                created_at=row.created_at,
                approved_by=row.approved_by,
                approved_at=row.approved_at,
            )
            for row in rows
        ]

    # --- plates ----------------------------------------------------------------

    def add_plate(
        self,
        story_key: str,
        page_number: int,
        *,
        image: bytes,
        media_type: str,
        prompt: str,
        slots: Sequence[Slot],
    ) -> str:
        """Store a candidate plate as `draft`. Never touches an approved plate."""
        if page_number not in PAGE_NUMBERS:
            raise TemplateError(f"page {page_number} is not 0..{PAGE_COUNT}")
        if not image:
            raise TemplateError("a plate needs an image")
        for slot in slots:
            slot.check()
        plate_id = uuid.uuid4().hex
        with self._engine.begin() as conn:
            _require_story(conn, story_key)
            conn.execute(
                insert(TemplatePlateRow).values(
                    id=plate_id,
                    story_key=story_key,
                    page_number=page_number,
                    image=image,
                    media_type=media_type,
                    prompt=prompt,
                    prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    slots=[slot.to_json() for slot in slots],
                    status=PlateStatus.DRAFT,
                    created_at=_now(),
                )
            )
        return plate_id

    def approve_plate(self, plate_id: str, *, operator: str) -> str | None:
        """Approve a plate; return the story's current version id afterwards.

        Idempotent: approving an approved plate changes nothing. The plate must
        carry exactly one slot per character on its page, the star included.
        It replaces (retires) the page's previously approved plate. When every
        page then has an approved plate, a new version is written.
        """
        if not operator:
            raise TemplateError("approval needs an operator")
        with self._engine.begin() as conn:
            plate = _plate(conn, plate_id)
            if plate.status == PlateStatus.APPROVED:
                return _current_version_id(conn, plate.story_key)
            if plate.status == PlateStatus.RETIRED:
                raise PlateNotApprovable("a retired plate cannot be approved")
            page = _latest_page(conn, plate.story_key, plate.page_number)
            if page is None:
                raise PlateNotApprovable(
                    f"page {plate.page_number} has no words yet; seed the story first"
                )
            star = _story_star(conn, plate.story_key)
            problem = _slot_problem(plate.slots, _page_cast(star, page.cast_keys))
            if problem:
                raise PlateNotApprovable(problem)
            conn.execute(
                update(TemplatePlateRow)
                .where(
                    TemplatePlateRow.story_key == plate.story_key,
                    TemplatePlateRow.page_number == plate.page_number,
                    TemplatePlateRow.status == PlateStatus.APPROVED,
                )
                .values(status=PlateStatus.RETIRED)
            )
            conn.execute(
                update(TemplatePlateRow)
                .where(TemplatePlateRow.id == plate_id)
                .values(status=PlateStatus.APPROVED, approved_by=operator, approved_at=_now())
            )
            logger.info(
                "templates.plate_approved story=%s page=%d plate=%s",
                plate.story_key,
                plate.page_number,
                plate_id,
            )
            return self._refresh_version(
                conn, plate.story_key, f"plate approved for page {plate.page_number}"
            )

    def retire_plate(self, plate_id: str) -> None:
        """Take a plate out of future versions. Versions already written keep it."""
        with self._engine.begin() as conn:
            _plate(conn, plate_id)
            conn.execute(
                update(TemplatePlateRow)
                .where(TemplatePlateRow.id == plate_id)
                .values(status=PlateStatus.RETIRED)
            )

    def delete_plate(self, plate_id: str) -> None:
        """Delete a plate no version names. A plate in any version can only be retired."""
        with self._engine.begin() as conn:
            _plate(conn, plate_id)
            used = conn.execute(
                select(func.count())
                .select_from(TemplateVersionPlateRow)
                .where(TemplateVersionPlateRow.plate_id == plate_id)
            ).scalar_one()
            if used:
                raise TemplateInUse("this plate is part of a template version; retire it")
            conn.execute(delete(TemplatePlateRow).where(TemplatePlateRow.id == plate_id))

    def plate_image(self, plate_id: str) -> tuple[bytes, str]:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(TemplatePlateRow.image, TemplatePlateRow.media_type).where(
                    TemplatePlateRow.id == plate_id
                )
            ).one_or_none()
        if row is None:
            raise UnknownTemplate("no such plate")
        return row.image, row.media_type

    def plate_status(self, plate_id: str) -> str:
        with self._engine.connect() as conn:
            return str(_plate(conn, plate_id).status)

    def book_plates(self, book_id: str) -> dict[int, PlateArt]:
        """The plates a book is drawn onto, by page number (0 is the cover).

        Read through the book's *pinned* version, so a book made on version 1
        keeps version 1's plates when version 2 is approved later. A book with
        no pinned version — made with the start gate off — has no plates, and
        the caller falls back to composing each page from scratch exactly as
        it did before plates existed.
        """
        with self._engine.connect() as conn:
            version_id = conn.execute(
                select(BookRow.template_version_id).where(BookRow.id == book_id)
            ).scalar_one_or_none()
            if not version_id:
                return {}
            rows = conn.execute(
                select(
                    TemplateVersionPlateRow.page_number,
                    TemplatePlateRow.id,
                    TemplatePlateRow.image,
                    TemplatePlateRow.media_type,
                    TemplatePlateRow.slots,
                )
                .select_from(TemplateVersionPlateRow)
                .join(
                    TemplatePlateRow,
                    TemplatePlateRow.id == TemplateVersionPlateRow.plate_id,
                )
                .where(TemplateVersionPlateRow.template_version_id == version_id)
            ).all()
        return {
            int(row.page_number): PlateArt(
                plate_id=row.id,
                image=row.image,
                media_type=row.media_type,
                slots=tuple(Slot.from_json(slot) for slot in row.slots),
            )
            for row in rows
        }

    # --- versions --------------------------------------------------------------

    def current_version(self, story_key: str) -> TemplateVersion | None:
        with self._engine.connect() as conn:
            version_id = _current_version_id(conn, story_key)
            return _load_version(conn, version_id) if version_id else None

    def load_version(self, version_id: str) -> TemplateVersion:
        with self._engine.connect() as conn:
            return _load_version(conn, version_id)

    def delete_version(self, version_id: str) -> None:
        """Delete a version no book pins. A pinned version can never be deleted."""
        with self._engine.begin() as conn:
            exists = conn.execute(
                select(TemplateVersionRow.id).where(TemplateVersionRow.id == version_id)
            ).scalar_one_or_none()
            if exists is None:
                raise UnknownTemplate("no such template version")
            pinned = conn.execute(
                select(func.count())
                .select_from(BookRow)
                .where(BookRow.template_version_id == version_id)
            ).scalar_one()
            if pinned:
                raise TemplateInUse(f"{pinned} book(s) were made on this template version")
            conn.execute(
                delete(TemplateVersionPlateRow).where(
                    TemplateVersionPlateRow.template_version_id == version_id
                )
            )
            conn.execute(
                delete(TemplateVersionCharacterRow).where(
                    TemplateVersionCharacterRow.template_version_id == version_id
                )
            )
            conn.execute(delete(TemplateVersionRow).where(TemplateVersionRow.id == version_id))

    # --- books -----------------------------------------------------------------

    def edition_for_new_book(self, story_key: str) -> StoryEdition:
        """The edition a book starting now is made from.

        The story's current template version when it has one — the words and
        looks its plates were approved against — and otherwise the latest
        revisions. With the operator's start gate on, a story without a
        version refuses instead.

        Raises:
            UnknownTemplate: no such story, or it is not on the shelf.
            NoTemplateVersion: the gate is on and there is no version yet.
        """
        with self._engine.connect() as conn:
            story = conn.execute(
                select(StoryRow).where(StoryRow.key == story_key)
            ).one_or_none()
            if story is None or not story.available:
                raise UnknownTemplate(f"no story {story_key!r}")
            version_id = _current_version_id(conn, story_key)
            if version_id is None and story.requires_template:
                raise NoTemplateVersion(NOT_READY)
            if version_id is not None:
                page_ids = dict(
                    conn.execute(
                        select(
                            TemplateVersionPlateRow.page_number,
                            TemplateVersionPlateRow.story_page_id,
                        ).where(TemplateVersionPlateRow.template_version_id == version_id)
                    ).all()
                )
                character_ids = dict(
                    conn.execute(
                        select(
                            TemplateVersionCharacterRow.cast_key,
                            TemplateVersionCharacterRow.character_id,
                        ).where(TemplateVersionCharacterRow.template_version_id == version_id)
                    ).all()
                )
            else:
                page_ids, character_ids = _latest_ids(conn, story_key)
            return _edition(conn, story, page_ids, character_ids, version_id)

    def latest_edition(self, story_key: str) -> StoryEdition:
        """The story as it is written now, whatever the gate or the plates say.

        What the studio and About screens describe before a book exists.
        """
        with self._engine.connect() as conn:
            story = conn.execute(
                select(StoryRow).where(StoryRow.key == story_key)
            ).one_or_none()
            if story is None:
                raise UnknownTemplate(f"no story {story_key!r}")
            page_ids, character_ids = _latest_ids(conn, story_key)
            return _edition(conn, story, page_ids, character_ids, None)

    def book_edition(self, book_id: str) -> StoryEdition:
        """The edition a saved book was made from, exactly as it was pinned.

        Raises:
            UnknownTemplate: the book is gone, or has no pins.
        """
        with self._book_editions_lock:
            cached = self._book_editions.get(book_id)
            if cached is not None:
                self._book_editions.move_to_end(book_id)
                return cached
        with self._engine.connect() as conn:
            book = conn.execute(
                select(BookRow.story_key, BookRow.template_version_id).where(
                    BookRow.id == book_id
                )
            ).one_or_none()
            if book is None:
                raise UnknownTemplate("no such book")
            page_ids = dict(
                conn.execute(
                    select(BookStoryPageRow.page_number, BookStoryPageRow.story_page_id).where(
                        BookStoryPageRow.book_id == book_id
                    )
                ).all()
            )
            character_ids = dict(
                conn.execute(
                    select(
                        BookStoryCharacterRow.cast_key, BookStoryCharacterRow.character_id
                    ).where(BookStoryCharacterRow.book_id == book_id)
                ).all()
            )
            if not page_ids:
                raise UnknownTemplate(f"book {book_id} has no story pinned")
            story = conn.execute(select(StoryRow).where(StoryRow.key == book.story_key)).one()
            edition = _edition(conn, story, page_ids, character_ids, book.template_version_id)
        with self._book_editions_lock:
            self._book_editions[book_id] = edition
            while len(self._book_editions) > _BOOK_EDITION_CACHE:
                self._book_editions.popitem(last=False)
        return edition

    def pin_unpinned_books(self) -> int:
        """Pin every book saved before books pinned their story. Returns how many.

        Those books were all drawn from the story as it was then; the closest
        honest pin is the latest revision of their story at the first boot
        that runs this, after which they never move. Idempotent and cheap: a
        boot with nothing to do runs one query.
        """
        with self._engine.begin() as conn:
            unpinned = conn.execute(
                select(BookRow.id, BookRow.story_key).where(
                    ~select(BookStoryPageRow.book_id)
                    .where(BookStoryPageRow.book_id == BookRow.id)
                    .exists()
                )
            ).all()
            latest: dict[str, tuple[dict[int, int], dict[str, int]]] = {}
            pinned = 0
            for book_id, story_key in unpinned:
                if story_key not in latest:
                    if _story_exists(conn, story_key):
                        latest[story_key] = _latest_ids(conn, story_key)
                    else:
                        latest[story_key] = ({}, {})
                page_ids, character_ids = latest[story_key]
                if not page_ids:
                    logger.warning("templates.unpinnable book=%s story=%s", book_id, story_key)
                    continue
                write_pins(conn, book_id, page_ids, character_ids)
                pinned += 1
        if pinned:
            logger.info("templates.pinned_legacy_books count=%d", pinned)
        return pinned

    # --- the shelf and the start gate -----------------------------------------

    def stories(self) -> list[StoryState]:
        """Every seeded story, in seed order, with its gate and version state."""
        with self._engine.connect() as conn:
            rows = conn.execute(select(StoryRow).order_by(StoryRow.created_at, StoryRow.key))
            states: list[StoryState] = []
            for row in rows.all():
                number = conn.execute(
                    select(func.max(TemplateVersionRow.number)).where(
                        TemplateVersionRow.story_key == row.key
                    )
                ).scalar_one()
                states.append(
                    StoryState(
                        key=row.key,
                        title=row.title,
                        blurb=row.blurb,
                        book_title=row.book_title,
                        cover_subtitle=row.cover_subtitle or "",
                        available=row.available,
                        requires_template=row.requires_template,
                        version_number=int(number) if number is not None else None,
                    )
                )
        return states

    def story_state(self, story_key: str) -> StoryState:
        for state in self.stories():
            if state.key == story_key:
                return state
        raise UnknownTemplate(f"no story {story_key!r}")

    def set_requires_template(self, story_key: str, required: bool) -> None:
        """The operator's start gate. Seeding never touches it."""
        with self._engine.begin() as conn:
            _require_story(conn, story_key)
            conn.execute(
                update(StoryRow)
                .where(StoryRow.key == story_key)
                .values(requires_template=required, updated_at=_now())
            )
        logger.info("templates.start_gate story=%s required=%s", story_key, required)

    # --- internals -------------------------------------------------------------

    def _refresh_version(self, conn: Connection, story_key: str, reason: str) -> str | None:
        """Write a new version if the story's complete current state differs.

        Returns the current version id afterwards (new or unchanged), or None
        when the story is not complete: a page without words, without an
        approved plate, or whose plate's slots no longer fit its cast.
        """
        star = _story_star(conn, story_key)
        pages: list[tuple[int, int, str]] = []
        cast_keys: set[str] = set()
        hashed: list[Any] = []
        for number in PAGE_NUMBERS:
            page = _latest_page(conn, story_key, number)
            if page is None:
                return _current_version_id(conn, story_key)
            plate = conn.execute(
                select(TemplatePlateRow.id, TemplatePlateRow.slots).where(
                    TemplatePlateRow.story_key == story_key,
                    TemplatePlateRow.page_number == number,
                    TemplatePlateRow.status == PlateStatus.APPROVED,
                )
            ).one_or_none()
            if plate is None:
                return _current_version_id(conn, story_key)
            cast = _page_cast(star, page.cast_keys)
            if _slot_problem(plate.slots, cast):
                logger.warning(
                    "templates.stale_plate story=%s page=%d plate=%s",
                    story_key,
                    number,
                    plate.id,
                )
                return _current_version_id(conn, story_key)
            cast_keys.update(cast)
            pages.append((number, page.id, plate.id))
            hashed.append([number, page.seed_hash or _hash(_page_content(page)), plate.id])

        characters: list[tuple[str, int]] = []
        for cast_key in sorted(cast_keys):
            character = conn.execute(
                select(StoryCharacterRow)
                .where(
                    StoryCharacterRow.story_key == story_key,
                    StoryCharacterRow.cast_key == cast_key,
                )
                .order_by(StoryCharacterRow.revision.desc())
                .limit(1)
            ).one_or_none()
            if character is None:
                return _current_version_id(conn, story_key)
            characters.append((cast_key, character.id))
            hashed.append([cast_key, _character_hash(character)])

        current_id = _current_version_id(conn, story_key)
        if current_id is not None and _composition(conn, current_id) == (
            sorted(pages),
            sorted(characters),
        ):
            return current_id

        number = (
            conn.execute(
                select(func.max(TemplateVersionRow.number)).where(
                    TemplateVersionRow.story_key == story_key
                )
            ).scalar_one()
            or 0
        ) + 1
        version_id = uuid.uuid4().hex
        conn.execute(
            insert(TemplateVersionRow).values(
                id=version_id,
                story_key=story_key,
                number=number,
                content_hash=_hash(hashed),
                reason=reason,
                created_at=_now(),
            )
        )
        conn.execute(
            insert(TemplateVersionPlateRow),
            [
                {
                    "template_version_id": version_id,
                    "page_number": page_number,
                    "story_page_id": page_id,
                    "plate_id": plate_id,
                }
                for page_number, page_id, plate_id in pages
            ],
        )
        conn.execute(
            insert(TemplateVersionCharacterRow),
            [
                {"template_version_id": version_id, "cast_key": key, "character_id": row_id}
                for key, row_id in characters
            ],
        )
        logger.info(
            "templates.version_created story=%s number=%d reason=%s", story_key, number, reason
        )
        return version_id


# --- module helpers ------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _hash(content: Any) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: How many saved books' editions one process keeps in memory.
_BOOK_EDITION_CACHE = 256


def _revised(new: str | None, old: str | None) -> str | None:
    """An operator's optional field edit: None keeps it, blank clears it."""
    if new is None:
        return old
    return new.strip() or None


def _scene_json(scene: SeedScene) -> dict[str, Any]:
    content: dict[str, Any] = {
        "caption": scene.caption,
        "subject_action": scene.subject_action,
        "environment": scene.environment,
        "time_of_day": scene.time_of_day,
        "camera": scene.camera,
        "must_include": list(scene.must_include),
        "must_avoid": list(scene.must_avoid),
    }
    if scene.compose:
        content["compose"] = dict(scene.compose)
    return content


def _seed_pages(seed: StorySeed) -> list[tuple[int, dict[str, Any]]]:
    """The cover (page 0) and every story page, as `story_pages` column values."""
    pages = [
        (
            COVER_PAGE,
            {
                "page_key": "cover",
                "title": seed.book_title,
                "prose": "",
                "beat": "",
                "emotion": "",
                "scene": _scene_json(seed.cover),
                "cast_keys": list(seed.cover.cast),
                "text_band": {"placement": "title_band", "band": seed.title_band},
            },
        )
    ]
    for page in seed.pages:
        pages.append(
            (
                page.number,
                {
                    "page_key": page.key,
                    "title": page.title,
                    "prose": page.story,
                    "beat": page.beat,
                    "emotion": page.emotion,
                    "scene": _scene_json(page.scene),
                    "cast_keys": list(page.scene.cast),
                    "text_band": {"placement": "below_art"},
                },
            )
        )
    return pages


def _seed_characters(seed: StorySeed) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            cast_key,
            {
                "display_name": character.label,
                "look": character.look,
                "look_with_photo": character.look_with_photo or None,
                "portrait": character.portrait or None,
                "costume": character.costume or None,
                "photoable": character.photoable,
                "position": position,
            },
        )
        for position, (cast_key, character) in enumerate(seed.cast.items())
    ]


def seed_edition(seed: StorySeed) -> StoryEdition:
    """The edition a seed describes, built without a database.

    The same constructor and the same column values the database path uses,
    so a test or a tool reading a seed sees exactly what a book would.
    """
    return edition_from_content(
        story={
            "key": seed.key,
            "title": seed.title,
            "book_title": seed.book_title,
            "cover_subtitle": seed.cover_subtitle,
            "star": seed.star,
            "names": seed.names,
        },
        pages=_seed_pages(seed),
        characters=_seed_characters(seed),
    )


def write_pins(
    conn: Connection,
    book_id: str,
    page_ids: Mapping[int, int],
    character_ids: Mapping[str, int],
) -> None:
    """Record the revisions one book is made from. Once per book, never rewritten."""
    conn.execute(
        insert(BookStoryPageRow),
        [
            {"book_id": book_id, "page_number": number, "story_page_id": row_id}
            for number, row_id in sorted(page_ids.items())
        ],
    )
    if character_ids:
        conn.execute(
            insert(BookStoryCharacterRow),
            [
                {"book_id": book_id, "cast_key": key, "character_id": row_id}
                for key, row_id in sorted(character_ids.items())
            ],
        )


def _story_exists(conn: Connection, story_key: str) -> bool:
    return (
        conn.execute(
            select(StoryRow.key).where(StoryRow.key == story_key)
        ).scalar_one_or_none()
        is not None
    )


def _latest_ids(conn: Connection, story_key: str) -> tuple[dict[int, int], dict[str, int]]:
    """The newest revision id of every page (cover included) and every cast member."""
    page_ids: dict[int, int] = {}
    for number, row_id in conn.execute(
        select(StoryPageRow.page_number, StoryPageRow.id)
        .where(StoryPageRow.story_key == story_key)
        .order_by(StoryPageRow.page_number, StoryPageRow.revision)
    ).all():
        page_ids[int(number)] = int(row_id)
    character_ids: dict[str, int] = {}
    for cast_key, row_id in conn.execute(
        select(StoryCharacterRow.cast_key, StoryCharacterRow.id)
        .where(StoryCharacterRow.story_key == story_key)
        .order_by(StoryCharacterRow.cast_key, StoryCharacterRow.revision)
    ).all():
        character_ids[str(cast_key)] = int(row_id)
    return page_ids, character_ids


def _edition(
    conn: Connection,
    story: Any,
    page_ids: Mapping[int, int],
    character_ids: Mapping[str, int],
    version_id: str | None,
) -> StoryEdition:
    """Read the named revisions and assemble them into one edition."""
    page_rows = conn.execute(
        select(StoryPageRow).where(StoryPageRow.id.in_(list(page_ids.values())))
    ).all()
    character_rows = conn.execute(
        select(StoryCharacterRow)
        .where(StoryCharacterRow.id.in_(list(character_ids.values())))
        .order_by(StoryCharacterRow.position, StoryCharacterRow.cast_key)
    ).all()
    return edition_from_content(
        story={
            "key": story.key,
            "title": story.title,
            "book_title": story.book_title,
            "cover_subtitle": story.cover_subtitle or "",
            "star": story.star,
            "names": story.names or {},
        },
        pages=[(int(row.page_number), _page_content(row)) for row in page_rows],
        characters=[(row.cast_key, _character_content(row)) for row in character_rows],
        template_version_id=version_id,
        page_ids=page_ids,
        character_ids=character_ids,
    )


def _character_content(row: Any) -> dict[str, Any]:
    return {
        name: getattr(row, name)
        for name in (
            "display_name",
            "look",
            "look_with_photo",
            "portrait",
            "costume",
            "photoable",
        )
    }


def _page_content(page: Any) -> dict[str, Any]:
    return {
        name: getattr(page, name)
        for name in (
            "page_key",
            "title",
            "prose",
            "beat",
            "emotion",
            "scene",
            "cast_keys",
            "text_band",
        )
    }


def _character_hash(character: Any) -> str:
    image = character.reference_image
    return _hash(
        {
            "display_name": character.display_name,
            "look": character.look,
            "look_with_photo": character.look_with_photo,
            "portrait": character.portrait,
            "costume": character.costume,
            "photoable": character.photoable,
            "reference_image": hashlib.sha256(image).hexdigest() if image else None,
        }
    )


def _page_cast(star: str, cast_keys: Iterable[str]) -> tuple[str, ...]:
    """Everyone on a page: the star (always) and the page's listed cast, once each."""
    return tuple(dict.fromkeys([star, *cast_keys]))


def _slot_problem(slots: Sequence[Mapping[str, Any]], cast: Sequence[str]) -> str | None:
    keys = [str(slot.get("cast_key", "")) for slot in slots]
    if sorted(keys) != sorted(cast):
        return (
            f"a plate needs exactly one slot for each of {sorted(cast)}; "
            f"this one has {sorted(keys)}"
        )
    try:
        for slot in slots:
            Slot.from_json(slot)
    except TemplateError as exc:
        return str(exc)
    return None


def _require_story(conn: Connection, story_key: str) -> None:
    if (
        conn.execute(
            select(StoryRow.key).where(StoryRow.key == story_key)
        ).scalar_one_or_none()
        is None
    ):
        raise UnknownTemplate(f"no story {story_key!r}; seed it first")


def _story_star(conn: Connection, story_key: str) -> str:
    star = conn.execute(
        select(StoryRow.star).where(StoryRow.key == story_key)
    ).scalar_one_or_none()
    if star is None:
        raise UnknownTemplate(f"no story {story_key!r}; seed it first")
    return str(star)


def _plate(conn: Connection, plate_id: str) -> Any:
    row = conn.execute(
        select(
            TemplatePlateRow.id,
            TemplatePlateRow.story_key,
            TemplatePlateRow.page_number,
            TemplatePlateRow.status,
            TemplatePlateRow.slots,
        ).where(TemplatePlateRow.id == plate_id)
    ).one_or_none()
    if row is None:
        raise UnknownTemplate("no such plate")
    return row


def _latest_page(conn: Connection, story_key: str, number: int) -> Any:
    return conn.execute(
        select(StoryPageRow)
        .where(StoryPageRow.story_key == story_key, StoryPageRow.page_number == number)
        .order_by(StoryPageRow.revision.desc())
        .limit(1)
    ).one_or_none()


def _next_character_revision(conn: Connection, story_key: str, cast_key: str) -> int:
    latest = conn.execute(
        select(func.max(StoryCharacterRow.revision)).where(
            StoryCharacterRow.story_key == story_key,
            StoryCharacterRow.cast_key == cast_key,
        )
    ).scalar_one()
    return int(latest or 0) + 1


def _current_version_id(conn: Connection, story_key: str) -> str | None:
    value = conn.execute(
        select(TemplateVersionRow.id)
        .where(TemplateVersionRow.story_key == story_key)
        .order_by(TemplateVersionRow.number.desc())
        .limit(1)
    ).scalar_one_or_none()
    return str(value) if value is not None else None


def _composition(
    conn: Connection, version_id: str
) -> tuple[list[tuple[int, int, str]], list[tuple[str, int]]]:
    pages = conn.execute(
        select(
            TemplateVersionPlateRow.page_number,
            TemplateVersionPlateRow.story_page_id,
            TemplateVersionPlateRow.plate_id,
        ).where(TemplateVersionPlateRow.template_version_id == version_id)
    ).all()
    characters = conn.execute(
        select(
            TemplateVersionCharacterRow.cast_key, TemplateVersionCharacterRow.character_id
        ).where(TemplateVersionCharacterRow.template_version_id == version_id)
    ).all()
    return (
        sorted((int(n), int(p), str(i)) for n, p, i in pages),
        sorted((str(k), int(c)) for k, c in characters),
    )


def _load_version(conn: Connection, version_id: str) -> TemplateVersion:
    version = conn.execute(
        select(TemplateVersionRow).where(TemplateVersionRow.id == version_id)
    ).one_or_none()
    if version is None:
        raise UnknownTemplate("no such template version")
    page_rows = conn.execute(
        select(
            StoryPageRow.page_number,
            StoryPageRow.page_key,
            StoryPageRow.title,
            StoryPageRow.prose,
            StoryPageRow.beat,
            StoryPageRow.emotion,
            StoryPageRow.scene,
            StoryPageRow.cast_keys,
            StoryPageRow.text_band,
            TemplatePlateRow.id.label("plate_id"),
            TemplatePlateRow.slots,
        )
        .join(
            TemplateVersionPlateRow,
            TemplateVersionPlateRow.story_page_id == StoryPageRow.id,
        )
        .join(TemplatePlateRow, TemplatePlateRow.id == TemplateVersionPlateRow.plate_id)
        .where(TemplateVersionPlateRow.template_version_id == version_id)
        .order_by(TemplateVersionPlateRow.page_number)
    ).all()
    pages = tuple(
        VersionPage(
            page_number=row.page_number,
            page_key=row.page_key,
            title=row.title,
            prose=row.prose,
            beat=row.beat,
            emotion=row.emotion,
            scene=row.scene,
            cast_keys=tuple(row.cast_keys),
            text_band=row.text_band,
            plate_id=row.plate_id,
            slots=tuple(Slot.from_json(slot) for slot in row.slots),
        )
        for row in page_rows
    )
    character_rows = conn.execute(
        select(StoryCharacterRow)
        .join(
            TemplateVersionCharacterRow,
            TemplateVersionCharacterRow.character_id == StoryCharacterRow.id,
        )
        .where(TemplateVersionCharacterRow.template_version_id == version_id)
    ).all()
    characters = {
        row.cast_key: VersionCharacter(
            cast_key=row.cast_key,
            display_name=row.display_name,
            look=row.look,
            photoable=row.photoable,
            has_reference_image=row.reference_image is not None,
            revision=row.revision,
            look_with_photo=row.look_with_photo or "",
            portrait=row.portrait or "",
            costume=row.costume or "",
        )
        for row in character_rows
    }
    return TemplateVersion(
        id=version.id,
        story_key=version.story_key,
        number=version.number,
        content_hash=version.content_hash,
        reason=version.reason,
        pages=pages,
        characters=characters,
    )
