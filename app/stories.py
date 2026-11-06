"""The shelf: which picture books the app offers, and which can be made today.

Only Jack has a page catalogue so far. *Little Red Riding Hood* sits on the
shelf as "Coming soon" until its catalogue exists (the story-catalogue phase of
``docs/ui-redesign-plan.md``); it has no studio route, so nothing can be started
for it by accident.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["JACK", "STORIES", "Story", "story_for"]


@dataclass(frozen=True, slots=True)
class Story:
    """One card on the shelf."""

    key: str
    title: str
    blurb: str
    cover_url: str
    #: False puts the card on the shelf dimmed, with no way in.
    available: bool


JACK = Story(
    key="jack_and_the_beanstalk",
    title="Jack and the Beanstalk",
    blurb="A cow, five magic beans, and a castle above the clouds.",
    cover_url="/static/covers/jack_and_the_beanstalk.svg",
    available=True,
)

RED = Story(
    key="little_red_riding_hood",
    title="Little Red Riding Hood",
    blurb="A basket for Grandmother, a path through the wood, and a wolf.",
    cover_url="/static/covers/little_red_riding_hood.svg",
    available=False,
)

STORIES: tuple[Story, ...] = (JACK, RED)


def story_for(key: str) -> Story | None:
    """The story a studio or About route names, if it can be made today."""
    for story in STORIES:
        if story.key == key and story.available:
            return story
    return None
