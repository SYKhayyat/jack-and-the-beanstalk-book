"""The shelf: which picture books the app offers, and which can be made today.

Stories are data (`app/books/<key>.toml`, loaded into the database at boot), so
the shelf is read from the template store rather than written here. A card has
three honest states before a visitor touches it:

* **available** — the studio is open;
* **waiting** — the operator's start gate is on and the story's pictures are
  not approved yet, so the card says so in one plain sentence and has no way in;
* **coming soon** — the seed marks the story unavailable.

Every card shows real finished covers: sample books made once with invented
children, kept as `app/static/covers/<story key>/<name>.jpg`. The file's stem
is the sample child's name, so the title and credit set over the art are the
ones a book for that child would carry. The shelf rotates through them.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

from app.prompt_data import prompt_text
from app.services.template_store import NOT_READY, StoryState, TemplateStore

__all__ = [
    "COVERS_DIR",
    "NOT_READY",
    "SampleCover",
    "Story",
    "shelf",
    "shelf_story",
    "showcase",
]

COVERS_DIR = Path(__file__).resolve().parent / "static" / "covers"
_COVER_SUFFIXES = frozenset({".jpg", ".jpeg", ".webp", ".png"})


@dataclass(frozen=True, slots=True)
class SampleCover:
    """One finished sample cover: the art, and the words the book sets over it."""

    url: str
    name: str
    title: str
    #: The line under the title: the story's subtitle ("starring Ava") when it
    #: has one, otherwise the credit ("A story for Ava") — as the PDF sets it.
    byline: str


@dataclass(frozen=True, slots=True)
class Story:
    """One card on the shelf."""

    key: str
    title: str
    blurb: str
    #: Finished sample covers, in name order; never empty for a seeded story.
    samples: tuple[SampleCover, ...]
    #: False puts the card on the shelf dimmed as "Coming soon", with no way in.
    available: bool
    #: False while the start gate holds the story back; the card says why.
    ready: bool

    @property
    def can_start(self) -> bool:
        return self.available and self.ready


def _story(state: StoryState) -> Story:
    return Story(
        key=state.key,
        title=state.title,
        blurb=state.blurb,
        samples=_samples(state.key, state.book_title, state.cover_subtitle),
        available=state.available,
        ready=state.can_start or not state.available,
    )


def shelf(templates: TemplateStore) -> tuple[Story, ...]:
    """Every story, in seed order."""
    return tuple(_story(state) for state in templates.stories())


def shelf_story(templates: TemplateStore, key: str) -> Story | None:
    """The story a studio or About route names, if it is on the shelf at all."""
    for story in shelf(templates):
        if story.key == key and story.available:
            return story
    return None


def showcase(templates: TemplateStore) -> list[tuple[Story, SampleCover]]:
    """Every sample cover of every story on offer, taking turns between stories.

    The landing page's carousel shows these, so a visitor sees a beanstalk,
    then a wolf, then a beanstalk again rather than one tale's covers in a row.
    """
    stories = [story for story in shelf(templates) if story.available]
    rounds = max((len(story.samples) for story in stories), default=0)
    return [
        (story, story.samples[index])
        for index in range(rounds)
        for story in stories
        if index < len(story.samples)
    ]


@cache
def _sample_files(story_key: str) -> tuple[Path, ...]:
    """The sample cover files on disk; they ship with the app, so read once."""
    folder = COVERS_DIR / story_key
    if not folder.is_dir():
        return ()
    return tuple(
        sorted(path for path in folder.iterdir() if path.suffix.lower() in _COVER_SUFFIXES)
    )


def _samples(story_key: str, book_title: str, subtitle: str) -> tuple[SampleCover, ...]:
    credit: str = prompt_text()["book"]["about"]["credit"]
    covers = []
    for path in _sample_files(story_key):
        name = path.stem.replace("_", " ").title()
        covers.append(
            SampleCover(
                url=f"/static/covers/{story_key}/{path.name}",
                name=name,
                title=book_title.format(name=name),
                byline=subtitle.format(name=name) or credit.format(about_name=name),
            )
        )
    return tuple(covers)
