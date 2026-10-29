"""Prompt construction.

This module is the app's actual intellectual core. The API call is trivial;
deciding *what to say* is the work. The words themselves live in
``app/prompts.toml`` so they can be edited and reviewed without touching code;
what stays here is the logic that chooses which line applies and joins them.

The pipeline runs two passes:

1. `build_cartoon_prompt` — stylise the uploaded photo into a cartoon portrait,
   applying every user setting. The output is the identity anchor.

Passing the original photo alongside the cartoon portrait in pass 2 gives the
model two independent anchors on the same face, which is the single biggest
lever on "does this look like the person".
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from app.models import CartoonSettings, CartoonStyle, RejectionFeedback, Scene
from app.models.book import BOOK_PAGES, BookPage
from app.models.settings import (
    ApparentAge,
    AspectRatio,
    CharacterGender,
    Expression,
    HairColour,
    HairStyle,
    LikenessStrength,
    Outfit,
    PhotoGender,
    PortraitBackground,
    SkinTone,
)
from app.prompt_data import prompt_text

__all__ = [
    "NEGATIVE_PROMPT_TAIL",
    "book_cast_sheet",
    "book_name_for",
    "build_book_page_prompt",
    "build_cartoon_prompt",
    "cast_photoable",
    "photoable_cast",
    "render_book_page",
]

_DATA = prompt_text()

_BOOK: dict[str, object] = _DATA["book"]

# Fixed scaffolding around the variable parts; ``{braces}`` are filled at build
# time. Kept as templates so the labels live in the data file too.
_ASSEMBLY: dict[str, str] = _DATA["assembly"]

# Kept in one place so every pass rejects the same failure modes: text artefacts
# (which would fight the caption) and extra people (which would break identity).
NEGATIVE_PROMPT_TAIL: str = _DATA["negative"]["tail"]

# Every map below is keyed by the enum's value. Iterating the enum (rather than
# reading the TOML keys directly) is what makes a missing entry fail at import:
# the comprehension raises a KeyError the moment a setting has no wording.
_STYLE_DESCRIPTIONS: dict[CartoonStyle, str] = {
    style: _DATA["styles"][style.value] for style in CartoonStyle
}

_ASPECT_HINTS: dict[AspectRatio, str] = {
    ratio: _DATA["aspect_hints"][ratio.value] for ratio in AspectRatio
}

_AGE_DESCRIPTIONS: dict[ApparentAge, str] = {
    age: _DATA["ages"][age.value] for age in ApparentAge
}

_GENDER_DESCRIPTIONS: dict[CharacterGender, str] = {
    gender: _DATA["genders"][gender.value] for gender in CharacterGender
}

_NAMES: dict[PhotoGender, str] = {
    gender: _DATA["names"][gender.value] for gender in PhotoGender
}

_PRONOUNS: dict[PhotoGender, dict[str, str]] = {
    gender: _DATA["pronouns"][gender.value] for gender in PhotoGender
}

_SKIN_DESCRIPTIONS: dict[SkinTone, str] = {
    tone: _DATA["skins"][tone.value] for tone in SkinTone
}

# Background descriptions for the portrait pass only. See PortraitBackground
# for why these never reach the scene prompt. Without these the enum value
# itself would be interpolated, and the model would be asked to wear
# "storybook_tunic".
_OUTFIT_DESCRIPTIONS: dict[Outfit, str] = {
    outfit: _DATA["outfits"][outfit.value] for outfit in Outfit
}

_HAIR_STYLE_DESCRIPTIONS: dict[HairStyle, str] = {
    style: _DATA["hair_styles"][style.value] for style in HairStyle
}

_HAIR_COLOUR_DESCRIPTIONS: dict[HairColour, str] = {
    colour: _DATA["hair_colours"][colour.value] for colour in HairColour
}

_EXPRESSION_DESCRIPTIONS: dict[Expression, str] = {
    expression: _DATA["expressions"][expression.value] for expression in Expression
}

_PORTRAIT_BACKGROUNDS: dict[PortraitBackground, str] = {
    background: _DATA["portrait_backgrounds"][background.value]
    for background in PortraitBackground
}

_LIKENESS_EMPHASIS: dict[LikenessStrength, str] = {
    strength: _DATA["likeness"][strength.value] for strength in LikenessStrength
}

# The cartoon-vs-real dial, sampled at each intensity from 1 to 10. Written out
# rather than interpolated so each step has human-vetted language behind it —
# the alternative, a numeric formula, produced mush at the middle values.
_INTENSITY_DESCRIPTIONS: dict[int, str] = {
    int(step): text for step, text in _DATA["intensity"].items()
}

_PHOTOREAL: dict[str, str] = _DATA["photoreal"]

# The book's one fixed style sentence. `[book].style` holds the *key* of the
# shared [styles] entry (the app default), so portrait and book pages read the
# identical wording from one place — the sentence exists exactly once in the
# data file. A book must never change style mid-story.
_BOOK_STYLE_KEY = str(cast("dict[str, object]", _BOOK)["style"])
_BOOK_STYLE: str = _STYLE_DESCRIPTIONS[CartoonStyle(_BOOK_STYLE_KEY)]

# The minor characters' pinned looks, keyed by the `cast` name a page lists.
_BOOK_CAST: dict[str, str] = {
    name: str(text) for name, text in cast("dict[str, object]", _BOOK["cast"]).items()
}

# Which cast members may ever be given a face by the uploader. Milky-White is
# false: the cow is prose-pinned, permanently (see prompts.toml).
_CAST_PHOTOABLE: dict[str, bool] = {
    name: bool(flag)
    for name, flag in cast("dict[str, object]", _BOOK.get("cast_photoable", {})).items()
}
_CAST_LABELS: dict[str, str] = {
    name: str(label)
    for name, label in cast("dict[str, object]", _BOOK.get("cast_labels", {})).items()
}


def photoable_cast() -> tuple[tuple[str, str], ...]:
    """The cast members a photo may be attached to, in catalogue order.

    Returns (key, display label) pairs. Anyone not in here is prose-pinned:
    the cow can never be given a face by the uploader, by construction.
    """
    return tuple(
        (name, _CAST_LABELS.get(name, name.replace("_", " ").capitalize()))
        for name in _BOOK_CAST
        if _CAST_PHOTOABLE.get(name, False)
    )


def cast_photoable(name: str) -> bool:
    """Whether `name` may be given a photo. Unknown names are never photoable."""
    return name in _BOOK_CAST and _CAST_PHOTOABLE.get(name, False)


def book_cast_sheet(supplied: Iterable[str] = ()) -> str:
    """The character bible: identical text on every page of the same book.

    Heading, every pinned look in catalogue order, then one book-level note
    naming who has a reference photo. `supplied` is the set of cast keys the
    uploader attached a photo to; the same book always builds the same string.
    """
    assembly = cast("dict[str, str]", _BOOK["assembly"])
    supplied_keys = [name for name in _BOOK_CAST if name in set(supplied)]
    if supplied_keys:
        photo_note = assembly["cast_photos"].format(
            names=", ".join(_CAST_LABELS.get(name, name) for name in supplied_keys)
        )
    else:
        photo_note = assembly["cast_photos_none"]
    looks = "\n".join(_BOOK_CAST[name] for name in _BOOK_CAST)
    return assembly["cast_sheet"].format(cast_looks=looks, photo_note=photo_note)


def _reference_map(character_photos: Iterable[str]) -> str:
    """The per-page reference order, matching the images sent with this call."""
    entries = [
        "1) the child's cartoon portrait",
        "2) the child's photograph",
    ]
    position = 3
    for name in character_photos:
        label = _CAST_LABELS.get(name, name)
        entries.append(f"{position}) {label}'s cartoon portrait")
        entries.append(f"{position + 1}) {label}'s photograph")
        position += 2
    return cast("dict[str, str]", _BOOK["assembly"])["reference_images"].format(
        entries="; ".join(entries)
    )


def _intensity_text(intensity: int) -> str:
    return _INTENSITY_DESCRIPTIONS[max(1, min(10, intensity))]


def _photoreal_keywords(intensity: int) -> str:
    """Terms to suppress as the dial turns away from photorealism."""
    if intensity <= 3:
        return ""
    if intensity <= 5:
        return _PHOTOREAL["soft"]
    return _PHOTOREAL["strong"]


def _accessory_directives(settings: CartoonSettings) -> list[str]:
    accessories: dict[str, str] = _DATA["accessories"]
    if settings.keep_glasses:
        glasses = accessories["keep_glasses"]
    else:
        glasses = accessories["remove_glasses"]
    if settings.keep_facial_hair:
        facial_hair = accessories["keep_facial_hair"]
    else:
        facial_hair = accessories["remove_facial_hair"]
    return [glasses, facial_hair]


def book_name_for(settings: CartoonSettings) -> str:
    """The default character name: Jack or Jane, from the photo's gender answer.

    This is what a blank name resolves to, everywhere a name is shown.
    """
    return _NAMES[settings.effective_gender]


def _substitutions(
    settings: CartoonSettings, prompt_name: str | None = None
) -> dict[str, str]:
    """The name and pronouns the prose is written with.

    `prompt_name`, when given (star name), overrides the gender default.
    """
    gender = settings.effective_gender
    return {"name": prompt_name or _NAMES[gender], **_PRONOUNS[gender]}


def render_book_page(
    page: BookPage, settings: CartoonSettings, *, prompt_name: str | None = None
) -> BookPage:
    """Fill a book page's name/pronoun placeholders for the chosen child."""
    return page.formatted(_substitutions(settings, prompt_name))


def _story_lines(settings: CartoonSettings, prompt_name: str | None) -> str:
    """The whole book's story, one labelled line per page.

    The lines come from the pages' own `story` text, so the story is stated
    once in the catalogue and never restated in a prompt template. Labelling
    them is what makes the block usable: the model can point at the line it is
    illustrating, and can see that the beans on one page are the "five magic
    beans" of another page's line.
    """
    lines = []
    for page in BOOK_PAGES:
        story = render_book_page(page, settings, prompt_name=prompt_name).story
        lines.append(f"Page {page.number}: {story}")
    return "\n".join(lines)


def build_book_page_prompt(
    page: BookPage,
    settings: CartoonSettings,
    *,
    page_number: int,
    total_pages: int,
    prompt_name: str | None = None,
    supplied_characters: Iterable[str] = (),
    character_photos: Iterable[str] = (),
) -> str:
    """Prompt for one picture-book page.

    The book's identity contract is the same double anchor as the single-scene
    flow, plus book-specific constraints: the style sentence is fixed book
    data (worded identically in every call — a book must not change style
    mid-story), the story text is never drawn by the model because the PDF
    layer adds it, the negative tail is the book's cast-declaring variant
    rather than the single-scene one, and the character bible is carried by
    `book_cast_sheet` — the same block on every page of a book.

    The whole story goes into every page prompt, labelled by page, with the
    model told that its own page's line is the one to illustrate and that the
    other lines are context only. Showing the story is what lets a page know
    what the earlier pages established — page 3's beans are the five beans of
    page 2 — and the page-scope clause is what stops that context from being
    drawn onto the wrong page.

    Args:
        page: The resolved page (callers pass the raw catalogue entry; the
            name/pronoun placeholders are filled here).
        settings: The character settings.
        page_number: This page's one-based position in story order.
        total_pages: The book's page count, for the "you are page N of M" framing.
        prompt_name: The star name; None falls back to the gender default
            (Jack/Jane).
        supplied_characters: Cast keys that have a reference photo somewhere in
            this book. Book-level, so the cast-sheet block stays byte-identical
            across the book's pages.
        character_photos: The page's own cast members that have a photo, in the
            order their references are sent. Page-level, because the reference
            images are.
    """
    task_book = cast("dict[str, str]", _BOOK["assembly"])
    page = render_book_page(page, settings, prompt_name=prompt_name)
    sections = [
        task_book["task"].format(
            page_number=page_number,
            total_pages=total_pages,
            book_title=str(_BOOK["book_title"]),
            name=_substitutions(settings, prompt_name)["name"],
        ),
        # The whole story, labelled by page, so the model knows which line it
        # is illustrating and what the others are for. This is also how page 3
        # learns that the beans it throws are the five of page 2.
        task_book["story_context"].format(
            story_lines=_story_lines(settings, prompt_name),
            page_number=page_number,
            total_pages=total_pages,
        ),
        task_book["page_identity"],
        settings.identity_directive(),
        _LIKENESS_EMPHASIS[settings.likeness_strength],
        # What the likeness dial means on book pages, and where it loses:
        # reading as one contract with the identity pressure above it and the
        # coherence guard below it — the pair no longer sit pages apart.
        str(_BOOK["likeness_not_size"]),
        str(_BOOK["body_coherence"]),
        str(_BOOK["same_clothes"]),
        str(_BOOK["same_characters"]),
        str(_BOOK["same_cow"]),
        str(_BOOK["one_art_style"]),
        task_book["quiet_zone"].format(area=page.settling),
        _ASSEMBLY["scene_to_draw"].format(caption=page.scene.caption),
        _ASSEMBLY["character_doing"].format(action=page.scene.subject_action),
        _ASSEMBLY["where"].format(environment=page.scene.environment),
        _ASSEMBLY["lighting"].format(time_of_day=page.scene.time_of_day),
        _ASSEMBLY["camera"].format(camera=page.scene.camera),
        page.scene.checklist,
        _common_character_block(settings),
        # The character bible: the same block, byte for byte, on every page of
        # this book — so no character's look is restated (and allowed to drift)
        # per page. It names every cast member; the page-scope clause above and
        # the negative tail are what keep the page to its own cast.
        book_cast_sheet(supplied_characters),
        # The exact order of the reference images sent with this call, so the
        # model can tell the child's two anchors from a character's portrait
        # and photograph.
        _reference_map(character_photos),
        # The fixed book style, injected verbatim — the same wording on every
        # page of the book by construction, and the same sentence the portrait
        # default resolves to (both read the shared [styles] entry).
        _ASSEMBLY["art_style"].format(style=_BOOK_STYLE),
        _ASSEMBLY["composition"].format(aspect=_ASPECT_HINTS[settings.aspect_ratio]),
        _avoid_clause(page.scene),
        _ASSEMBLY["stylisation"].format(intensity=_intensity_text(settings.cartoon_intensity)),
        # The book's own negative tail: the single-scene tail's "only the one
        # person described above" contradicts the pages that require the
        # mother, which is how the child's face got onto her.
        str(_BOOK["negative"]),
        str(_BOOK["no_text"]),
    ]
    return _join_sections(sections)


def _common_character_block(settings: CartoonSettings) -> str:
    """The lines describing *who the character is*, shared by both passes."""
    return " ".join(
        [
            _AGE_DESCRIPTIONS[settings.apparent_age],
            _GENDER_DESCRIPTIONS[settings.character_gender],
            _ASSEMBLY["character_name"].format(**_substitutions(settings)),
            _SKIN_DESCRIPTIONS[settings.skin_tone],
            _HAIR_STYLE_DESCRIPTIONS[settings.hair_style],
            _HAIR_COLOUR_DESCRIPTIONS[settings.hair_color],
            _OUTFIT_DESCRIPTIONS[settings.outfit],
            _EXPRESSION_DESCRIPTIONS[settings.expression],
            *_accessory_directives(settings),
        ]
    )


def build_cartoon_prompt(
    settings: CartoonSettings, *, rejection: RejectionFeedback | None = None
) -> str:
    """Prompt for pass 1: stylise the uploaded photo into a cartoon portrait.

    A retry sends the *tweaked settings*, never the previous image as a
    reference: showing a rejected portrait invites the model to converge on its
    faults rather than move away from them. `rejection` carries the user's
    ticked faults as closed-set corrections; omitting it (or passing an empty
    one) yields a first-attempt prompt, byte for byte.
    """
    style_text = _STYLE_DESCRIPTIONS[settings.cartoon_style]
    retry_clauses = rejection.prompt_block() if rejection else ""
    sections = [
        _ASSEMBLY["task_cartoon"],
        settings.identity_directive(),
        _LIKENESS_EMPHASIS[settings.likeness_strength],
        _common_character_block(settings),
        _ASSEMBLY["stylisation"].format(intensity=_intensity_text(settings.cartoon_intensity)),
        _photoreal_keywords(settings.cartoon_intensity),
        _ASSEMBLY["art_style"].format(style=style_text),
        _ASSEMBLY["framing"],
        _PORTRAIT_BACKGROUNDS[settings.portrait_background],
        _ASSEMBLY["composition"].format(aspect=_ASPECT_HINTS[settings.aspect_ratio]),
        retry_clauses,
        NEGATIVE_PROMPT_TAIL,
    ]
    return _join_sections(sections)


def _avoid_clause(scene: Scene) -> str:
    if not scene.must_avoid:
        return ""
    items = ", ".join(scene.must_avoid)
    return _ASSEMBLY["avoid"].format(items=items)


def _join_sections(sections: list[str]) -> str:
    """Join non-empty sections into a prompt, one labelled paragraph each."""
    return "\n\n".join(section.strip() for section in sections if section.strip())
