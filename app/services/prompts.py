"""Prompt construction.

This module is the app's actual intellectual core. The API call is trivial;
deciding *what to say* is the work. The words themselves live in
``app/prompts.toml`` so they can be edited and reviewed without touching code;
what stays here is the logic that chooses which line applies and joins them.

The pipeline runs two passes:

1. `build_cartoon_prompt` — stylise the uploaded photo into a cartoon portrait,
   applying every user setting. The output is the identity anchor.
2. `build_scene_prompt` — place that cartoon person into the requested scene,
   re-stating the identity constraints because models drift when the scene
   action is interesting.

Passing the original photo alongside the cartoon portrait in pass 2 gives the
model two independent anchors on the same face, which is the single biggest
lever on "does this look like the person".
"""

from __future__ import annotations

from typing import cast

from app.models import CartoonSettings, CartoonStyle, Scene
from app.models.book import BookPage
from app.models.feedback import RejectionFeedback
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
    "build_book_page_prompt",
    "build_cartoon_prompt",
    "build_scene_prompt",
    "render_book_page",
    "render_scene",
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


def _substitutions(settings: CartoonSettings) -> dict[str, str]:
    """The name and pronouns the scene prose is written with, for this setting."""
    gender = settings.effective_gender
    return {"name": _NAMES[gender], **_PRONOUNS[gender]}


def render_scene(scene: Scene, settings: CartoonSettings) -> Scene:
    """Fill a scene's name/pronoun placeholders for the chosen gender."""
    return scene.formatted(_substitutions(settings))


def render_book_page(page: BookPage, settings: CartoonSettings) -> BookPage:
    """Fill a book page's name/pronoun placeholders for the chosen child."""
    return page.formatted(_substitutions(settings))


def build_book_page_prompt(
    page: BookPage, settings: CartoonSettings, *, page_number: int, total_pages: int
) -> str:
    """Prompt for one picture-book page.

    The book's identity contract is the same double anchor as the single-scene
    flow, plus two book-specific constraints: the style sentence is fixed book
    data (worded identically in every call — a book must not change style
    mid-story), and the story text is never drawn by the model because the PDF
    layer adds it.

    Args:
        page: The resolved page (callers pass the raw catalogue entry; the
            name/pronoun placeholders are filled here, like `compose` does).
        settings: The character settings.
        page_number: This page's one-based position in story order.
        total_pages: The book's page count, for the "you are page N of M" framing.
    """
    task_book = cast("dict[str, str]", _BOOK["assembly"])
    page = render_book_page(page, settings)
    sections = [
        task_book["task"].format(
            page_number=page_number,
            total_pages=total_pages,
            book_title=str(_BOOK["book_title"]),
            name=_substitutions(settings)["name"],
        ),
        task_book["page_identity"],
        settings.identity_directive(),
        _LIKENESS_EMPHASIS[settings.likeness_strength],
        str(_BOOK["same_clothes"]),
        task_book["quiet_zone"].format(area=page.settling),
        _ASSEMBLY["scene_to_draw"].format(caption=page.scene.caption),
        _ASSEMBLY["character_doing"].format(action=page.scene.subject_action),
        _ASSEMBLY["where"].format(environment=page.scene.environment),
        _ASSEMBLY["lighting"].format(time_of_day=page.scene.time_of_day),
        _ASSEMBLY["camera"].format(camera=page.scene.camera),
        page.scene.checklist,
        _common_character_block(settings),
        # The fixed book style, injected verbatim — the same wording on every
        # page of the book by construction.
        _ASSEMBLY["art_style"].format(style=str(_BOOK["style"])),
        _ASSEMBLY["composition"].format(aspect=_ASPECT_HINTS[settings.aspect_ratio]),
        _avoid_clause(page.scene),
        _ASSEMBLY["stylisation"].format(intensity=_intensity_text(settings.cartoon_intensity)),
        NEGATIVE_PROMPT_TAIL,
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
    settings: CartoonSettings,
    rejection: RejectionFeedback | None = None,
) -> str:
    """Prompt for pass 1: stylise the uploaded photo into a cartoon portrait.

    Args:
        settings: The user's settings.
        rejection: What was wrong with the previous attempt, if any. Rendered as
            corrective instructions near the top, where they outrank the general
            style guidance.

    Note:
        The previous attempt is deliberately *not* sent as a reference image.
        Showing a rejected portrait invites the model to converge on its faults
        rather than move away from them; only the corrections carry over.
    """
    style_text = _STYLE_DESCRIPTIONS[settings.cartoon_style]
    sections = [
        _ASSEMBLY["task_cartoon"],
        *([rejection.prompt_block()] if rejection and not rejection.is_empty else []),
        settings.identity_directive(),
        _LIKENESS_EMPHASIS[settings.likeness_strength],
        _common_character_block(settings),
        _ASSEMBLY["stylisation"].format(intensity=_intensity_text(settings.cartoon_intensity)),
        _photoreal_keywords(settings.cartoon_intensity),
        _ASSEMBLY["art_style"].format(style=style_text),
        _ASSEMBLY["framing"],
        _PORTRAIT_BACKGROUNDS[settings.portrait_background],
        _ASSEMBLY["composition"].format(aspect=_ASPECT_HINTS[settings.aspect_ratio]),
        NEGATIVE_PROMPT_TAIL,
    ]
    return _join_sections(sections)


def build_scene_prompt(scene: Scene, settings: CartoonSettings) -> str:
    """Prompt for pass 2: place the cartoon character into the scene.

    The first reference image is the cartoon portrait from pass 1; the second,
    when supplied, is the original photograph. They are the same person, and the
    model should treat them as a matched pair.

    The scene's name/pronoun placeholders are resolved here, so the prompt always
    matches the chosen character even if the caller passes the raw catalogue entry.
    """
    scene = render_scene(scene, settings)
    style_text = _STYLE_DESCRIPTIONS[settings.cartoon_style]
    sections = [
        _ASSEMBLY["task_scene"].format(**_substitutions(settings)),
        settings.identity_directive(),
        _LIKENESS_EMPHASIS[settings.likeness_strength],
        # Repeated rather than assumed: this is what models drop when the scene
        # action is exciting, and it is the first of the two graded requirements.
        _ASSEMBLY["scene_to_draw"].format(caption=scene.caption),
        _ASSEMBLY["character_doing"].format(action=scene.subject_action),
        _ASSEMBLY["where"].format(environment=scene.environment),
        _ASSEMBLY["lighting"].format(time_of_day=scene.time_of_day),
        _ASSEMBLY["camera"].format(camera=scene.camera),
        scene.checklist,
        _common_character_block(settings),
        _ASSEMBLY["stylisation"].format(intensity=_intensity_text(settings.cartoon_intensity)),
        _photoreal_keywords(settings.cartoon_intensity),
        _ASSEMBLY["art_style"].format(style=style_text),
        _ASSEMBLY["composition"].format(aspect=_ASPECT_HINTS[settings.aspect_ratio]),
        _avoid_clause(scene),
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
