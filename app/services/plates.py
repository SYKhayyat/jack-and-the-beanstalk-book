"""The empty plates a page is painted onto, and where each character goes (T5).

A plate is one page's world, painted once and approved once: the place, the
props and the light, with nobody in it and no text of any kind. A book's pages
are then edit passes that paint the cast into the plate, so the plate is also
the layout contract — it declares a slot for every character on its page.

Two rules this module enforces by construction:

* **No user data, ever.** A plate is built from the story seed and the story's
  `[stories.<key>]` block in `prompts.toml`. The story's prose is not in it,
  the child's name is not in it, and a placeholder left in a scene field is an
  error rather than a prompt sent to a model — including in the camera line,
  which therefore describes the empty stage and never who stands on it.
* **One set, one book.** All 21 plates of a story carry the same style, palette
  and forbid-lists, and differ only in the place and the light the page names.

The slot layout is deterministic from the page's cast, so the same page always
gets the same stage, and an approval can check the plate against the page's
cast without a human measuring anything.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from app.books import PAGE_COUNT, SeedScene, StorySeed
from app.prompt_data import prompt_text
from app.services.template_store import COVER_PAGE, Slot

__all__ = [
    "COVER_KEY",
    "MAX_PLATE_CAST",
    "PlateError",
    "PlatePlan",
    "build_plate_prompt",
    "page_cast",
    "plate_plans",
    "slots_for",
]

#: How many characters a plate can hold. The seed validator already caps a page
#: at the provider's reference limit; this is the layout's own answer.
MAX_PLATE_CAST = 3

#: The cover's name in a story's `[plate_places]`; pages use their own key.
COVER_KEY = "cover"


class PlateError(ValueError):
    """A story cannot have plates made for it. Safe to print for an operator."""


@dataclass(frozen=True, slots=True)
class PlatePlan:
    """One plate to paint: which page, the exact prompt, and the stage it fixes."""

    page_number: int
    key: str
    prompt: str
    slots: tuple[Slot, ...]


#: (rect, facing) per position, by how many characters the page casts. The
#: star is always first. Rects are fractions of the art box, origin top left;
#: nothing starts above y=0.24, so the cover's top 22 % stays an empty title
#: band and no story page puts a face in its calm top strip.
_LAYOUTS: dict[int, tuple[tuple[tuple[float, float, float, float], str], ...]] = {
    1: (((0.33, 0.30, 0.34, 0.64), "toward"),),
    2: (
        ((0.06, 0.30, 0.32, 0.64), "right"),
        ((0.62, 0.30, 0.32, 0.64), "left"),
    ),
    3: (
        ((0.03, 0.32, 0.28, 0.60), "right"),
        ((0.37, 0.36, 0.26, 0.56), "toward"),
        ((0.70, 0.32, 0.27, 0.60), "left"),
    ),
}

#: Pages whose story the default stage would contradict, by the name a page's
#: `staging` field uses. In "chase_right" the star flees towards the right of
#: the frame with the pursuer behind on the left, so both face right; the star
#: is still first in cast order.
_STAGINGS: dict[str, tuple[tuple[tuple[float, float, float, float], str], ...]] = {
    "chase_right": (
        ((0.62, 0.30, 0.32, 0.64), "right"),
        ((0.06, 0.30, 0.32, 0.64), "right"),
    ),
}


def page_cast(story: StorySeed, cast_keys: Sequence[str]) -> tuple[str, ...]:
    """Everyone on a page, once each: the star, then the page's listed cast."""
    cast = tuple(dict.fromkeys([story.star, *cast_keys]))
    if len(cast) > MAX_PLATE_CAST:
        raise PlateError(
            f"a plate holds at most {MAX_PLATE_CAST} characters; this page casts {list(cast)}"
        )
    return cast


def slots_for(cast_keys: Sequence[str], *, staging: str = "") -> tuple[Slot, ...]:
    """One slot per character, laid out by how many there are.

    The blueprints are fixed: a lone character stands centre, a pair face each
    other, and three are staged left, centre and right. `scale` is the
    character's height as a share of the art box, so the edit pass is told how
    big they should be as well as where they go. A page may name a `staging`
    when its story needs a different stage.
    """
    if staging:
        layout = _STAGINGS.get(staging)
        if layout is None:
            raise PlateError(f"no staging named {staging!r}; there is {sorted(_STAGINGS)}")
        if len(layout) != len(cast_keys):
            raise PlateError(
                f"staging {staging!r} holds {len(layout)} characters, not {len(cast_keys)}"
            )
    else:
        layout = _LAYOUTS.get(len(cast_keys))
        if layout is None:
            raise PlateError(
                f"no slot layout for {len(cast_keys)} characters (1-{MAX_PLATE_CAST})"
            )
    return tuple(
        Slot(cast_key=key, rect=rect, scale=rect[3], facing=facing)
        for key, (rect, facing) in zip(cast_keys, layout, strict=True)
    )


#: The cover's own rule, so every story gets it without remembering to write
#: it into a seed. The layout keeps every slot below y=0.24 and the review
#: screen refuses a busy band, but the painter has to be told first.
_TITLE_BAND = (
    "THE TITLE BAND: the top fifth of the frame stays calm and open — bare sky "
    "or plain air with nothing in it, no tower, tree, cloud stack, bird or "
    "object — because the book's title is set there afterwards, outside this "
    "image."
)


def build_plate_prompt(
    story: StorySeed, scene: SeedScene, *, page_key: str, cover: bool = False
) -> str:
    """The exact prompt for one plate: place and light, and nothing alive.

    Assembled from the story's own section in `prompts.toml` — only the places
    this page is set in — and this page's place, time and framing from the seed.
    It contains no prose, no name and no placeholder — a `{name}` reaching a
    plate would be a bug, not a prompt. The cover is told to keep its title
    band empty; a story page is not.
    """
    section = _story_section(story.key)
    style = _style_sentence(section)
    parts = [
        section["intro"],
        _places_sentence(section, story.key, page_key),
        section["palette"],
        f"ART STYLE: {style}",
        f"THE PAGE: {_literal(scene.environment, story.key, 'environment')}",
        f"TIME OF DAY: {_literal(scene.time_of_day, story.key, 'time_of_day')}",
        f"CAMERA AND FRAMING: {_literal(scene.camera, story.key, 'camera')}",
    ]
    parts.append(_clear_space_sentence(len(page_cast(story, scene.cast))))
    if cover:
        parts.append(_TITLE_BAND)
    parts += [section["empty"], section["no_text"], section["negative"]]
    return "\n\n".join(parts)


def plate_plans(story: StorySeed) -> tuple[PlatePlan, ...]:
    """The cover and every story page, in order: 21 plans for a 20-page story."""
    plans = [
        PlatePlan(
            page_number=COVER_PAGE,
            key=COVER_KEY,
            prompt=build_plate_prompt(story, story.cover, page_key=COVER_KEY, cover=True),
            slots=slots_for(page_cast(story, story.cover.cast), staging=story.cover.staging),
        )
    ]
    for page in story.pages:
        plans.append(
            PlatePlan(
                page_number=page.number,
                key=page.key,
                prompt=build_plate_prompt(story, page.scene, page_key=page.key),
                slots=slots_for(page_cast(story, page.scene.cast), staging=page.scene.staging),
            )
        )
    if len(plans) != PAGE_COUNT + 1:
        raise PlateError(f"a story has {PAGE_COUNT + 1} plates; this has {len(plans)}")
    return tuple(plans)


def _story_section(story_key: str) -> Mapping[str, Any]:
    sections = prompt_text().get("stories")
    if not isinstance(sections, Mapping) or story_key not in sections:
        raise PlateError(
            f"prompts.toml has no [stories.{story_key}] block; the plate prompts "
            "are story data"
        )
    section = cast(Mapping[str, Any], sections[story_key])
    missing = [
        field
        for field in ("intro", "palette", "empty", "no_text", "negative")
        if not isinstance(section.get(field), str)
    ]
    if missing:
        raise PlateError(f"[stories.{story_key}] is missing {missing}")
    return section


def _common() -> Mapping[str, Any]:
    return cast(Mapping[str, Any], prompt_text()["plate_common"])


def _style_sentence(section: Mapping[str, Any]) -> str:
    key = section.get("style", "")
    styles = cast(Mapping[str, str], _common()["styles"])
    if key not in styles:
        raise PlateError(f"[stories] style {key!r} is not a key of [plate_common.styles]")
    return styles[key]


def _places_sentence(section: Mapping[str, Any], story_key: str, page_key: str) -> str:
    """The fixed designs of the places this page is set in, and no others."""
    places = cast(Mapping[str, str], section.get("places", {}))
    wanted = cast(Mapping[str, Sequence[str]], section.get("plate_places", {})).get(page_key)
    if not wanted:
        raise PlateError(f"[stories.{story_key}.plate_places] has no entry for {page_key!r}")
    unknown = [place for place in wanted if place not in places]
    if unknown:
        raise PlateError(f"{story_key}: {page_key!r} names unknown places {unknown}")
    designs = " ".join(places[place][0].upper() + places[place][1:] for place in wanted)
    return f"{_common()['places_intro']} {designs}"


def _clear_space_sentence(cast_size: int) -> str:
    return str(_common()[f"clear_space_{cast_size}"])


def _literal(text: str, story_key: str, field: str) -> str:
    """A scene field that must not name anybody — a place or a light.

    `{name}`, `{possessive}` and the rest are the visitor's child. None of them
    belongs in a plate, and neither does a literal brace, so either is refused
    rather than sent.
    """
    if "{" in text or "}" in text:
        raise PlateError(
            f"{story_key}: {field} contains a placeholder, and a plate names nobody"
        )
    return text
