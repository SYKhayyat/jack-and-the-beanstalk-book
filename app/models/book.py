"""Picture-book domain models.

A book is one child, a sequence of pages, and an About Me page, exported as a
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
    """

    number: int
    key: str
    scene: Scene
    story: str
    text_zone: TextZone
    settling: str = ""

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
    )


# An unknown `text_zone` value in the TOML raises a ValueError the first time
# this module is imported — a data mistake must fail at boot, not on a request.
BOOK_PAGES: tuple[BookPage, ...] = tuple(
    _assemble_page(spec, number)
    for number, spec in enumerate(prompt_text()["book_pages"], start=1)
)

_BOOK_PAGES_BY_KEY: dict[str, BookPage] = {page.key: page for page in BOOK_PAGES}


def get_book_page(key: str) -> BookPage | None:
    """Look up a book page by key, or None if unknown."""
    return _BOOK_PAGES_BY_KEY.get(key)


class AboutMe(BaseModel):
    """What the uploader typed about the child; rendered on the About Me page.

    This text goes **only** into the PDF. It never enters a prompt, so a closed
    vocabulary is not needed here the way it is for settings — but length caps
    and control-character stripping still keep one upload from turning a page
    into a poster or a mess.
    """

    model_config = ConfigDict(extra="forbid")

    child_name: str = Field(min_length=1, max_length=60)
    child_age: int | None = Field(default=None, ge=0, le=14)
    child_gender: str | None = Field(
        default=None,
        description='Optional, e.g. "boy" or "girl"; drawn as written.',
    )
    details: str = Field(
        default="",
        description="Free text the uploader wants on the About Me page.",
    )

    @field_validator("child_name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        cleaned = _sanitize(value)
        if not cleaned:
            raise ValueError("child_name must not be blank")
        return cleaned

    @field_validator("child_gender")
    @classmethod
    def _clean_gender(cls, value: str | None) -> str | None:
        """An optional single line: blank means \"not given\"."""
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
        """The About Me page as PDF copy, built entirely from what was typed.

        Copy text (headings) comes from ``prompts.toml``; field values come from
        the uploader. Supplied-but-empty fields simply add no line.
        """
        about = prompt_text()["book"]["about_me"]
        heading = about["heading"].format(child_name=self.child_name)
        # The name itself is drawn as the page's title; it is not repeated here.
        lines: list[str] = []
        if self.child_age is not None:
            lines.append(about["age_line"].format(child_age=self.child_age))
        if self.child_gender:
            lines.append(self.child_gender)
        if self.details:
            lines.append(about["details_heading"])
            lines.append(self.details)
        return [heading, *lines]


def _sanitize(text: str) -> str:
    """Strip control characters and collapse whitespace runs."""
    return "".join(char for char in " ".join(text.split()) if char.isprintable()).strip()
