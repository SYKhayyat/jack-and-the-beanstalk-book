"""One story, exactly as a book is made from it.

A `StoryEdition` is the whole of a story's content at one moment: its title,
its cover and twenty pages (words and scene), and its character bible. It is
built from the database's revisions — the latest ones for a book about to
start, the pinned ones for a book that exists — and nothing downstream reads a
story from anywhere else. The prompt builder, the renderer, the PDF and the
screens all take an edition, so a story is never half one revision and half
another, and a book never changes when its seed does.

Pure data: this module knows no database and no HTTP. `edition_from_content`
is the one constructor, and it takes the same column values the
`story_pages` and `story_characters` rows hold, so a seed (in tests and tools)
and the database (at runtime) build an edition the same way.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.models.book import BookPage
from app.models.scenes import Scene
from app.models.settings import PhotoGender

__all__ = [
    "COVER_NUMBER",
    "CastMember",
    "StoryEdition",
    "edition_from_content",
    "page_from_content",
]

#: The cover is page zero of every story and every book.
COVER_NUMBER = 0


@dataclass(frozen=True, slots=True)
class CastMember:
    """One bible entry as an edition carries it."""

    key: str
    label: str
    photoable: bool
    look: str
    look_with_photo: str = ""
    portrait: str = ""
    costume: str = ""


@dataclass(frozen=True, slots=True)
class StoryEdition:
    """A story's words, scenes and looks, fixed for the life of one book."""

    key: str
    title: str
    #: The cover title with `{name}` where the star's name goes.
    book_title: str
    star: str
    names: Mapping[str, str]
    cover: BookPage
    pages: tuple[BookPage, ...]
    #: The bible, star included, in the order the studio offers it.
    cast: Mapping[str, CastMember]
    #: The template version the edition came from, when it came from one.
    template_version_id: str | None = None
    #: The row ids behind every page (cover included) and every cast member:
    #: what a book pins. Empty for an edition built straight from a seed.
    page_ids: Mapping[int, int] = field(default_factory=dict)
    character_ids: Mapping[str, int] = field(default_factory=dict)

    def title_for(self, name: str) -> str:
        """The cover title for a star called `name`."""
        return self.book_title.format(name=name)

    def default_name(self, gender: PhotoGender) -> str:
        """What a blank name field becomes for this photo-gender answer."""
        return self.names[gender.value]

    @property
    def star_member(self) -> CastMember:
        return self.cast[self.star]

    @property
    def supporting_cast(self) -> tuple[CastMember, ...]:
        """Everyone but the star, in bible order."""
        return tuple(member for key, member in self.cast.items() if key != self.star)

    @property
    def photoable_cast(self) -> tuple[CastMember, ...]:
        """The supporting cast a visitor may give a face to, in bible order."""
        return tuple(member for member in self.supporting_cast if member.photoable)

    def can_photograph(self, cast_key: str) -> bool:
        """Whether a photo may be attached to `cast_key`. The star is not a slot."""
        member = self.cast.get(cast_key)
        return member is not None and member.photoable and cast_key != self.star

    def page(self, number: int) -> BookPage | None:
        """Story page `number` (1..20), or None."""
        for page in self.pages:
            if page.number == number:
                return page
        return None

    def pages_casting(self, cast_key: str) -> set[int]:
        """The story pages a supporting cast member appears on."""
        return {page.number for page in self.pages if cast_key in page.cast}


def page_from_content(number: int, content: Mapping[str, Any]) -> BookPage:
    """A finished-page view of one `story_pages` revision.

    The scene's `compose` lines, where a page has them, replace the plate's
    framing: the plate is an empty stage, the page has the child in it.
    """
    scene: Mapping[str, Any] = content["scene"]
    compose: Mapping[str, str] = scene.get("compose") or {}
    key = str(content["page_key"])
    return BookPage(
        number=number,
        key=key,
        scene=Scene(
            key=key,
            title=str(content["title"]),
            caption=str(scene["caption"]),
            subject_action=str(scene["subject_action"]),
            environment=str(compose.get("environment", scene["environment"])),
            time_of_day=str(compose.get("time_of_day", scene["time_of_day"])),
            camera=str(compose.get("camera", scene["camera"])),
            must_include=tuple(str(item) for item in scene.get("must_include", ())),
            must_avoid=tuple(str(item) for item in scene.get("must_avoid", ())),
        ),
        story=str(content.get("prose", "")),
        emotion=str(content.get("emotion", "")),
        cast=tuple(str(key) for key in content.get("cast_keys", ())),
        beat=str(content.get("beat", "")),
    )


def edition_from_content(
    *,
    story: Mapping[str, Any],
    pages: Iterable[tuple[int, Mapping[str, Any]]],
    characters: Iterable[tuple[str, Mapping[str, Any]]],
    template_version_id: str | None = None,
    page_ids: Mapping[int, int] | None = None,
    character_ids: Mapping[str, int] | None = None,
) -> StoryEdition:
    """Assemble an edition from story, page and character column values.

    `story` carries key, title, book_title, star and names; `pages` is
    (page_number, content) with the cover as page 0; `characters` is
    (cast_key, content) in bible order.

    Raises:
        ValueError: if the content is not one whole story — no cover, a page
            missing, or a page casting someone the bible does not have.
    """
    star = str(story["star"])
    by_number = dict(pages)
    if COVER_NUMBER not in by_number:
        raise ValueError(f"{story['key']}: the edition has no cover")
    cover_content = by_number.pop(COVER_NUMBER)
    numbers = sorted(by_number)
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError(f"{story['key']}: story pages are not 1..N: {numbers}")
    cast = {
        key: CastMember(
            key=key,
            label=str(content["display_name"]),
            photoable=bool(content["photoable"]),
            look=str(content["look"]),
            look_with_photo=str(content.get("look_with_photo") or ""),
            portrait=str(content.get("portrait") or ""),
            costume=str(content.get("costume") or ""),
        )
        for key, content in characters
    }
    if star not in cast:
        raise ValueError(f"{story['key']}: the star {star!r} has no bible entry")
    story_pages = tuple(page_from_content(number, by_number[number]) for number in numbers)
    for page in story_pages:
        unknown = [key for key in page.cast if key not in cast]
        if unknown:
            raise ValueError(
                f"{story['key']} page {page.number}: no bible entry for {unknown}"
            )
    cover = page_from_content(COVER_NUMBER, cover_content)
    return StoryEdition(
        key=str(story["key"]),
        title=str(story["title"]),
        book_title=str(story["book_title"]),
        star=star,
        names={str(k): str(v) for k, v in dict(story["names"]).items()},
        cover=cover,
        pages=story_pages,
        cast=cast,
        template_version_id=template_version_id,
        page_ids=dict(page_ids or {}),
        character_ids=dict(character_ids or {}),
    )
