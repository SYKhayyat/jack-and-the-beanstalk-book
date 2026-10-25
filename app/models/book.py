"""Picture-book domain models.

A book is one person, a sequence of pages, and an About page, exported as a
single PDF. The page catalogue is data (``app/prompts.toml``, ``[[book_pages]]``)
because pages are prose; the models here hold the typed shape and the values the
uploader typed in.

Deliberate split of the two kinds of text on a page:

* the **image spec** (a `Scene`) — what the model is told to draw, and
* the **story line** — what the PDF layer draws on top of the finished art.

They travel together in the catalogue (one TOML block per page) so they cannot
drift apart, but only the story line is ever rendered on the finished page: the
model is told not to draw text at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.scenes import Scene
from app.prompt_data import prompt_text

__all__ = ["BOOK_PAGES", "AboutMe", "BookPage", "TextZone", "get_book_page"]


class TextZone(StrEnum):
    """Where on the page the PDF puts the story text."""

    LOWER_THIRD = "lower_third"
    SKY = "sky"
    GRASS = "grass"


@dataclass(frozen=True, slots=True)
class BookPage:
    """One page of the book: a scene to draw plus the story line to overlay.

    Attributes:
        number: One-based position in story order, assigned by catalogue order.
        key: Stable identifier used in URLs and the API payload.
        scene: The image specification (the same `Scene` contract the
            single-scene pipeline uses, so every prompt helper applies).
        story: The sentence rendered on the page, verbatim.
        text_zone: Where the PDF puts the story text.
        settling: Prompt clause telling the model to keep the text zone calm.
        cast: Names of the non-reference characters this page draws (keys of
            ``[book.cast]`` in prompts.toml — the mother, the old man). Whoever
            a scene names beyond the child has no reference image, so their
            pinned look is injected into the page prompt by name.
    """

    number: int
    key: str
    scene: Scene
    story: str
    text_zone: TextZone
    settling: str = ""
    cast: tuple[str, ...] = ()

    def formatted(self, substitutions: Mapping[str, str]) -> BookPage:
        """A copy with name/pronoun placeholders filled in for this child.

        The returned page's `story` and `settling` are filled here; the page
        title lives on `scene` and is filled by `Scene.formatted` — consumers
        must read the title from the *formatted* copy's scene, never from the
        raw catalogue entry.
        """

        def fill(text: str) -> str:
            return text.format(**substitutions)

        return replace(
            self,
            scene=self.scene.formatted(substitutions),
            story=fill(self.story),
            settling=fill(self.settling),
        )


def _assemble_page(spec: Mapping[str, object], number: int) -> BookPage:
    cast_names = tuple(str(item) for item in cast(tuple[object, ...], spec.get("cast", ())))
    for name in cast_names:
        if name not in prompt_text()["book"]["cast"]:
            raise ValueError(
                f"page {spec['key']!r} names a cast member with no pinned look: {name!r}"
            )
    return BookPage(
        number=number,
        key=str(spec["key"]),
        scene=Scene(
            key=str(spec["key"]),
            title=str(spec["title"]),
            caption=str(spec["caption"]),
            subject_action=str(spec["subject_action"]),
            environment=str(spec["environment"]),
            time_of_day=str(spec["time_of_day"]),
            camera=str(spec["camera"]),
            must_include=tuple(
                str(item) for item in cast(tuple[object, ...], spec.get("must_include", ()))
            ),
            must_avoid=tuple(
                str(item) for item in cast(tuple[object, ...], spec.get("must_avoid", ()))
            ),
        ),
        story=str(spec["story"]),
        text_zone=TextZone(str(spec["text_zone"])),
        settling=str(spec.get("settling", "")),
        cast=cast_names,
    )


# An unknown `text_zone` value in the TOML raises a ValueError the first time
# this module is imported — a data mistake must fail at boot, not on a request.
# Pages carrying `retired = true` are skipped entirely: no generation, no page
# number, no PDF. Their prose stays in the TOML, so retiring is reversible with
# a one-flag change — the catalogue is never written into a corner.
_BOOK_PAGE_SPECS: tuple[Mapping[str, object], ...] = tuple(
    spec for spec in prompt_text()["book_pages"] if not bool(spec.get("retired", False))
)
BOOK_PAGES: tuple[BookPage, ...] = tuple(
    _assemble_page(spec, number) for number, spec in enumerate(_BOOK_PAGE_SPECS, start=1)
)

_BOOK_PAGES_BY_KEY: dict[str, BookPage] = {page.key: page for page in BOOK_PAGES}


def get_book_page(key: str) -> BookPage | None:
    """Look up a book page by key, or None if unknown."""
    return _BOOK_PAGES_BY_KEY.get(key)


class AboutMe(BaseModel):
    """Who stars in the book and what the About page prints.

    Two names, deliberately: `prompt_name` is the name the story and the page
    prompts use for the character; `about_name` is what the About page shows,
    and may be a pseudonym. Both are resolved to a non-blank value by the
    caller: a blank name becomes Jack or Jane, following the photo-gender
    answer the settings carry (see `book_name_for`).

    All of this text goes **only** into the PDF — it never enters a prompt,
    except for `prompt_name` as a substitution — so length caps and
    control-character stripping still apply to every field.
    """

    model_config = ConfigDict(extra="forbid")

    prompt_name: str = Field(min_length=1, max_length=60)
    about_name: str = Field(min_length=1, max_length=60)
    about_age: int | None = Field(default=None, ge=0, le=99)
    title: str | None = Field(
        default=None,
        max_length=60,
        description="What the person calls themselves, e.g. 'an astronaut'.",
    )
    details: str = Field(
        default="",
        description="Free text the uploader wants on the About page.",
    )

    @field_validator("prompt_name")
    @classmethod
    def _clean_prompt_name(cls, value: str) -> str:
        cleaned = _sanitize(value)
        if not cleaned:
            raise ValueError("prompt_name must not be blank")
        return cleaned

    @field_validator("about_name")
    @classmethod
    def _clean_about_name(cls, value: str) -> str:
        cleaned = _sanitize(value)
        if not cleaned:
            raise ValueError("about_name must not be blank")
        return cleaned

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str | None) -> str | None:
        """An optional single line: blank means "not given"."""
        if value is None:
            return None
        return _sanitize(value) or None

    @field_validator("details")
    @classmethod
    def _clean_details(cls, value: str) -> str:
        # Newlines are meaningful inside the details block; everything else
        # control-wise is stripped.
        lines = (_sanitize(line) for line in value.splitlines())
        return "\n".join(line for line in lines if line).strip()

    def rendered_about_lines(self) -> list[str]:
        """The About page as PDF copy, built entirely from what was typed.

        Copy text (headings) comes from ``prompts.toml``; field values come
        from the uploader. Supplied-but-empty fields simply add no line. The
        display order is the About page's reading order: name, age, title,
        then the About lines.
        """
        about = prompt_text()["book"]["about"]
        heading = about["heading"].format(child_name=self.about_name)
        # The name itself is drawn as the page's title; it is not repeated here.
        lines: list[str] = []
        if self.about_age is not None:
            lines.append(about["age_line"].format(child_age=self.about_age))
        if self.title:
            lines.append(self.title)
        if self.details:
            lines.append(self.details)
        return [heading, *lines]


def _sanitize(text: str) -> str:
    """Strip control characters and collapse whitespace runs."""
    return "".join(char for char in " ".join(text.split()) if char.isprintable()).strip()
