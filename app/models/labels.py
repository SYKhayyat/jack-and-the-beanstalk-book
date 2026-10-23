"""Display labels for the closed sets the UI offers.

Enums keep short, stable values — `storybook_tunic` is fine in a URL or a
config file, but "Storybook Tunic" in a dropdown tells the user nothing about
what they are picking. These labels do that.

Kept beside the vocabulary rather than inside the templates, so a new option
cannot be added without someone writing the sentence for it.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, TypeVar

from app.models import (
    ApparentAge,
    AspectRatio,
    CartoonStyle,
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

__all__ = [
    "CHOICE_LABELS",
    "choice_label",
    "choices_for",
]


_E = TypeVar("_E", bound=StrEnum)


def _labels(enum_cls: type[_E], mapping: Mapping[Any, str]) -> dict[_E, str]:
    """Check the label set matches the enum exactly, at import time.

    A new option with no sentence for it, or a label for an option that no
    longer exists, fails here rather than showing up as "Storybook Tunic" in a
    dropdown.
    """
    unknown = set(mapping) - set(enum_cls)
    if unknown:
        raise ValueError(f"Labels for {enum_cls.__name__} name unknown values: {unknown}")
    missing = set(enum_cls) - set(mapping)
    if missing:
        raise ValueError(f"Labels for {enum_cls.__name__} are missing: {missing}")
    return {member: mapping[member] for member in enum_cls}


_STYLE = {
    CartoonStyle.MANGA: "Manga (black & white ink and screentone)",
    CartoonStyle.ANIME: "Anime (cel-shaded colour)",
    CartoonStyle.WESTERN_CARTOON: "Western cartoon (bold outlines, flat colour)",
    CartoonStyle.PIXAR_LIKE: "Modern 3D animated feature",
    CartoonStyle.DISNEY_LIKE: "Hand-painted 2D animation",
    CartoonStyle.COMIC_BOOK: "Comic book (heavy ink, halftone)",
    CartoonStyle.PENCIL_SKETCH: "Pencil sketch (graphite, cross-hatched)",
    CartoonStyle.WATERCOLOUR: "Watercolour (washes on paper)",
    CartoonStyle.VECTOR_FLAT: "Flat vector (smooth shapes, no gradients)",
}

_BACKGROUND = {
    PortraitBackground.PLAIN: "Plain — nothing behind the face",
    PortraitBackground.AS_UPLOADED: "Keep the background from my photo",
    PortraitBackground.SOFT_COLOUR: "Soft blurred colour",
    PortraitBackground.STORYBOOK: "Storybook hills and sky",
}

_OUTFIT = {
    Outfit.AS_UPLOADED: "Keep my outfit",
    Outfit.STORYBOOK_TUNIC: "Storybook tunic and breeches",
    Outfit.PEASANT: "Rough peasant clothes",
    Outfit.ADVENTURER: "Adventurer's tunic and belt",
    Outfit.SIMPLE_SHIRT: "Plain shirt and trousers",
    Outfit.HOODED_CLOAK: "Hooded travelling cloak",
    Outfit.NONE_SPECIFIED: "No particular outfit",
}

_HAIR_STYLE = {
    HairStyle.AS_UPLOADED: "Keep my hair",
    HairStyle.SHORT: "Short",
    HairStyle.BOB: "Bob",
    HairStyle.LONG_STRAIGHT: "Long, straight",
    HairStyle.LONG_WAVY: "Long, wavy",
    HairStyle.CURLY: "Curly",
    HairStyle.COILY: "Coily",
    HairStyle.PONYTAIL: "Ponytail",
    HairStyle.BUNS: "Buns",
    HairStyle.BALD: "Bald",
}

_HAIR_COLOUR = {
    HairColour.AS_UPLOADED: "Keep my hair colour",
    HairColour.BLACK: "Black",
    HairColour.DARK_BROWN: "Dark brown",
    HairColour.BROWN: "Brown",
    HairColour.AUBURN: "Auburn",
    HairColour.RED: "Red",
    HairColour.BLONDE: "Blonde",
    HairColour.PLATINUM: "Platinum",
    HairColour.GREY: "Grey",
    HairColour.WHITE: "White",
}

_EXPRESSION = {
    Expression.AS_UPLOADED: "Keep my expression",
    Expression.FOCUSED_DETERMINED: "Focused and determined",
    Expression.HAPPY: "Happy",
    Expression.SERIOUS: "Serious",
    Expression.SURPRISED: "Surprised",
    Expression.CURIOUS: "Curious",
    Expression.BRAVE: "Brave",
    Expression.WORRIED: "Worried",
    Expression.GRINNING: "Grinning",
}

_AGE = {
    ApparentAge.AS_UPLOADED: "Keep my age",
    ApparentAge.CHILD: "A child, about 10",
    ApparentAge.TEEN: "A teenager, about 16",
    ApparentAge.ADULT: "An adult, about 30",
}

_GENDER = {
    CharacterGender.MATCH: "Match the photo",
    CharacterGender.BOY: "Boy",
    CharacterGender.GIRL: "Girl",
}

_PHOTO_GENDER = {
    PhotoGender.BOY: "Boy",
    PhotoGender.GIRL: "Girl",
}

_SKIN = {
    SkinTone.AS_UPLOADED: "Keep my skin tone",
    SkinTone.VERY_LIGHT: "Very light",
    SkinTone.LIGHT: "Light",
    SkinTone.MEDIUM: "Medium",
    SkinTone.TAN: "Tan",
    SkinTone.OLIVE: "Olive",
    SkinTone.BROWN: "Brown",
    SkinTone.DEEP_BROWN: "Deep brown",
    SkinTone.DEEPEST_BROWN: "Deepest brown",
}

_LIKENESS = {
    LikenessStrength.STRICT: "Strict — as close to my face as possible",
    LikenessStrength.BALANCED: "Balanced — close, with some stylisation",
    LikenessStrength.LOOSE: "Loose — let the style come through",
}

_ASPECT = {
    AspectRatio.PORTRAIT: "Square 1:1",
    AspectRatio.LANDSCAPE: "Landscape 4:3",
    AspectRatio.TALL: "Tall portrait 3:4",
    AspectRatio.CINEMATIC: "Wide cinematic 16:9",
}


def _build() -> dict[type[StrEnum], dict[StrEnum, str]]:
    table: dict[type[StrEnum], dict[StrEnum, str]] = {
        CartoonStyle: _labels(CartoonStyle, _STYLE),
        PortraitBackground: _labels(PortraitBackground, _BACKGROUND),
        Outfit: _labels(Outfit, _OUTFIT),
        HairStyle: _labels(HairStyle, _HAIR_STYLE),
        HairColour: _labels(HairColour, _HAIR_COLOUR),
        Expression: _labels(Expression, _EXPRESSION),
        ApparentAge: _labels(ApparentAge, _AGE),
        CharacterGender: _labels(CharacterGender, _GENDER),
        PhotoGender: _labels(PhotoGender, _PHOTO_GENDER),
        SkinTone: _labels(SkinTone, _SKIN),
        LikenessStrength: _labels(LikenessStrength, _LIKENESS),
        AspectRatio: _labels(AspectRatio, _ASPECT),
    }
    return table


CHOICE_LABELS: dict[type[StrEnum], dict[StrEnum, str]] = _build()


def choices_for(enum_cls: type[_E]) -> list[tuple[str, str]]:
    """`(value, label)` pairs for a template, using the labels above.

    Falls back to a tidied-up version of the value if a member has no
    hand-written label, so a new option still renders rather than disappearing.
    """
    labels = CHOICE_LABELS.get(enum_cls, {})
    return [
        (member.value, labels.get(member) or member.value.replace("_", " ").title())
        for member in enum_cls
    ]


def choice_label(enum_cls: type[_E], value: str) -> str:
    """The label for one value, with the same fallback as `choices_for`."""
    try:
        member = enum_cls(value)
    except ValueError:
        return value.replace("_", " ").title()
    labels = CHOICE_LABELS.get(enum_cls, {})
    return labels.get(member) or value.replace("_", " ").title()
