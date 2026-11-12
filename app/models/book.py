"""Picture-book domain models.

A book is one person, a sequence of pages, and an About page, exported as a
single PDF. The pages are data — each story's seed (``app/books/<key>.toml``),
loaded into the database and pinned per book (``app/models/story.py``); the
models here hold the typed shape and the values the uploader typed in.

Deliberate split of the two kinds of text on a page:

* the **image spec** (a `Scene`) — what the model is told to draw, and
* the **story line** — what the PDF layer draws on top of the finished art.

They travel together in the seed (one TOML block per page) so they cannot
drift apart, but only the story line is ever rendered on the finished page: the
model is told not to draw text at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.scenes import Scene
from app.prompt_data import prompt_text

__all__ = ["AboutMe", "AboutPhotoChoice", "BookPage"]


@dataclass(frozen=True, slots=True)
class BookPage:
    """One page of the book: a scene to draw plus the story text to overlay.

    Attributes:
        number: Position in the book: 1..20 for story pages, 0 for the cover.
        key: Stable identifier used in URLs and the API payload.
        scene: The image specification (the same `Scene` contract the
            single-scene pipeline uses, so every prompt helper applies).
        story: The narrative printed on the page, verbatim. This is the
            book's prose: several sentences that carry the tale forward, not
            a caption for the picture.
        beat: The one-line summary of the same moment, used only in the
            prompt's "whole story" context. The model needs the shape of the
            tale, not its prose — shipping the paragraphs to every call would
            make each prompt larger for no gain.
        emotion: The page's emotional beat, stated in the prompt so faces
            track the story instead of falling back on a stock smile.
        cast: The supporting cast this page draws, as keys of the story's
            character bible (the mother, the old man). The star is never
            listed. Looks come from the bible by key; a page never restates one.
        star_in_picture: False for a page the star is not drawn on (the wolf
            reaches the cottage first); the picture is then `cast` alone.
        paint_areas: What the story paints outside the characters' slots on a
            plated page (coins on the table, beans in the air), as (x, y, w, h)
            fractions of the picture. The plate is pasted back everywhere else
            (`app/services/plate_paste.py`).
    """

    number: int
    key: str
    scene: Scene
    story: str
    emotion: str = ""
    cast: tuple[str, ...] = ()
    beat: str = ""
    star_in_picture: bool = True
    paint_areas: tuple[tuple[float, float, float, float], ...] = ()

    def formatted(self, substitutions: Mapping[str, str]) -> BookPage:
        """A copy with name/pronoun placeholders filled in for this child.

        The returned page's `story`, `beat` and `emotion` are filled here; the page
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
            beat=fill(self.beat) if self.beat else "",
            emotion=fill(self.emotion),
        )


class AboutPhotoChoice(StrEnum):
    """What the About page frames: the cartoon, the real photo, or both."""

    CARTOON = "cartoon"
    PHOTO = "photo"
    BOTH = "both"


class AboutMe(BaseModel):
    """Who stars in the book and what the About page prints.

    Two names, deliberately: `prompt_name` is the name the story and the page
    prompts use for the character; `about_name` is what the About page shows,
    and may be a pseudonym. Both are resolved to a non-blank value by the
    caller: a blank name becomes the story's default star name for the
    photo-gender answer the settings carry (see `StoryEdition.default_name`).

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
    #: Defaulted, so a record written before the choice existed still
    #: validates and renders exactly as it did: the cartoon alone.
    about_photo: AboutPhotoChoice = AboutPhotoChoice.CARTOON
    #: An optional gift line, printed first on the About page ("For Grandma,
    #: who tells the best stories."). One line, as typed — a book given to a
    #: child usually is given by somebody. Defaulted, so every stored book
    #: written before this existed still validates and prints as it did.
    dedication: str = Field(default="", max_length=140)

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

    @field_validator("dedication")
    @classmethod
    def _clean_dedication(cls, value: str) -> str:
        """One line, blank meaning "no dedication"; it is drawn as typed."""
        return _sanitize(value)

    @field_validator("details")
    @classmethod
    def _clean_details(cls, value: str) -> str:
        # Newlines are meaningful inside the details block; everything else
        # control-wise is stripped.
        lines = (_sanitize(line) for line in value.splitlines())
        return "\n".join(line for line in lines if line).strip()

    def rendered_about_lines(self) -> list[str]:
        """The About page as PDF copy: a heading, then real sentences.

        Copy text comes from ``prompts.toml``; field values come from the
        uploader. The AI assessment read the old bare lines ("7 years old",
        "Author", a pet name) as authoring notes leaked into the book, so each
        field is now set in a sentence: "Noa is 7 years old." and "In real
        life, Noa is an astronaut." The free-text details stay exactly as typed.
        Supplied-but-empty fields add no sentence.
        """
        about = prompt_text()["book"]["about"]
        name = self.about_name
        heading = about["heading"].format(about_name=name)
        # The gift line comes first: it is what the person who gave the book
        # wants the child to read before the facts about them (Idea 5).
        lines: list[str] = [self.dedication] if self.dedication else []
        lines.append(about["star_line"].format(about_name=name))
        if self.about_age is not None:
            unit = "year" if self.about_age == 1 else "years"
            lines.append(
                about["age_line"].format(
                    about_name=name, age_phrase=f"{self.about_age} {unit} old"
                )
            )
        if self.title:
            lines.append(
                about["title_line"].format(
                    about_name=name, title_phrase=_title_phrase(self.title)
                )
            )
        if self.details:
            lines.append(self.details)
        return [heading, *lines]

    def cover_credit(self) -> str:
        """The cover's dedication line, under the title."""
        credit: str = prompt_text()["book"]["about"]["credit"]
        return credit.format(about_name=self.about_name)


_DETERMINERS = frozenset({"a", "an", "the", "my", "our", "his", "her", "their", "your"})


def _title_phrase(title: str) -> str:
    """Make a typed title read inside "In real life, X is ___."

    "Author" becomes "an author", "an astronaut" stays as typed and an acronym
    such as "NASA engineer" keeps its capitals ("a NASA engineer").
    """
    phrase = title.strip().rstrip(".!")
    first, *_ = phrase.split()
    if first.lower() in _DETERMINERS:
        return phrase[0].lower() + phrase[1:]
    if not (len(first) > 1 and first.isupper()):
        phrase = phrase[0].lower() + phrase[1:]
    article = "an" if phrase[0].lower() in "aeiou" else "a"
    return f"{article} {phrase}"


def _sanitize(text: str) -> str:
    """Strip control characters and collapse whitespace runs."""
    return "".join(char for char in " ".join(text.split()) if char.isprintable()).strip()
