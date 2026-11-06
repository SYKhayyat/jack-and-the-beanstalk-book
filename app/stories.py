"""The shelf: which picture books the app offers, and which can be made today.

Stories are data (`app/books/<key>.toml`, loaded into the database at boot), so
the shelf is read from the template store rather than written here. A card has
three honest states before a visitor touches it:

* **available** — the studio is open;
* **waiting** — the operator's start gate is on and the story's pictures are
  not approved yet, so the card says so in one plain sentence and has no way in;
* **coming soon** — the seed marks the story unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.template_store import NOT_READY, StoryState, TemplateStore

__all__ = ["NOT_READY", "Story", "shelf", "shelf_story"]


@dataclass(frozen=True, slots=True)
class Story:
    """One card on the shelf."""

    key: str
    title: str
    blurb: str
    cover_url: str
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
        cover_url=f"/static/covers/{state.key}.svg",
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
