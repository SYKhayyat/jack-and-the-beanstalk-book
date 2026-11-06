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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from app.books import SEEDS
from app.models import CartoonSettings, CartoonStyle, RejectionFeedback, Scene
from app.models.book import BookPage
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
from app.models.story import StoryEdition
from app.prompt_data import prompt_text

__all__ = [
    "NEGATIVE_PROMPT_TAIL",
    "STANDING_NEGATIVE",
    "book_cast_sheet",
    "book_name_for",
    "build_book_page_prompt",
    "build_cartoon_prompt",
    "build_cast_portrait_prompt",
    "build_cover_prompt",
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

# The standing negative block: the last paragraph of every image prompt.
STANDING_NEGATIVE: str = _DATA["negative"]["standing"]

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

# The same dial as book pages read it: how graphic the rendering is *inside*
# the book's one fixed style. The portrait bands describe a photograph being
# stylised ("keep the photograph almost intact" at 1 to 3), which contradicts the
# page's own "nothing photorealistic" rule, so book pages never use them.
_BOOK_INTENSITY_DESCRIPTIONS: dict[int, str] = {
    int(step): text for step, text in cast("dict[str, str]", _BOOK["intensity"]).items()
}

_PHOTOREAL: dict[str, str] = _DATA["photoreal"]


@dataclass(frozen=True, slots=True)
class _StoryWording:
    """A story's own prompt text: its style, its props and places, its rules."""

    style: str
    page_world: str
    page_rules: tuple[str, ...]


def _story_wording(key: str, section: Mapping[str, object]) -> _StoryWording:
    rules = section.get("page_rules", [])
    if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
        raise KeyError(f"[stories.{key}].page_rules must be a list of sentences")
    return _StoryWording(
        # The story's one fixed style sentence: `style` holds the *key* of a
        # shared [styles] entry, so portrait and pages read the identical
        # wording from one place. A book never changes style mid-story.
        style=_STYLE_DESCRIPTIONS[CartoonStyle(str(section["style"]))],
        page_world=str(section["page_world"]),
        page_rules=tuple(rules),
    )


# Every story the app can make has its wording, or the import fails: a story
# whose pages would be prompted with another tale's props must fail at boot.
_STORIES: dict[str, _StoryWording] = {
    seed.key: _story_wording(seed.key, _DATA["stories"][seed.key]) for seed in SEEDS
}

# The portrait pass's wording for a photographed cast member, keyed by cast
# key, comes from the story's bible (`CastMember.portrait`): a page with no
# reference photo must say so, while that pass has exactly one photograph and
# must never deny it.


def book_cast_sheet(story: StoryEdition, supplied: Iterable[str] = ()) -> str:
    """The character bible: identical text on every page of the same book.

    Heading, every supporting character's pinned look in bible order, then one
    book-level note naming who has a reference photo. `supplied` is the set of
    cast keys the uploader attached a photo to; the same book always builds
    the same string. The star is not listed: the identity block and the
    reference images are who the star is.
    """
    assembly = cast("dict[str, str]", _BOOK["assembly"])
    chosen = set(supplied)
    supplied_members = [member for member in story.photoable_cast if member.key in chosen]
    if supplied_members:
        photo_note = assembly["cast_photos"].format(
            names=", ".join(member.label for member in supplied_members)
        )
    else:
        photo_note = assembly["cast_photos_none"]
    looks = "\n".join(
        member.look_with_photo if member in supplied_members else member.look
        for member in story.supporting_cast
    )
    return assembly["cast_sheet"].format(cast_looks=looks, photo_note=photo_note)


def _reference_map(story: StoryEdition, character_photos: Iterable[str]) -> str:
    """The per-page reference order, matching the images sent with this call."""
    entries = [
        "1) the child's cartoon portrait",
        "2) the child's photograph",
    ]
    position = 3
    for name in character_photos:
        label = story.cast[name].label
        entries.append(f"{position}) {label}'s cartoon portrait")
        entries.append(f"{position + 1}) {label}'s photograph")
        position += 2
    return cast("dict[str, str]", _BOOK["assembly"])["reference_images"].format(
        entries="; ".join(entries)
    )


def _intensity_text(intensity: int) -> str:
    return _INTENSITY_DESCRIPTIONS[max(1, min(10, intensity))]


def _book_intensity_text(intensity: int) -> str:
    """The dial's wording on a book page: graphic-ness inside the fixed style.

    Deliberately not falling back to the portrait bands: a missing band is a
    data bug that would put "keep the photograph almost intact" on a page, and
    `tests/test_book_prompts.py` fails if any band is missing.
    """
    return _BOOK_INTENSITY_DESCRIPTIONS[max(1, min(10, intensity))]


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


def book_name_for(settings: CartoonSettings, story: StoryEdition) -> str:
    """The default star name for this story and the photo's gender answer.

    This is what a blank name resolves to, everywhere a name is shown: Jack or
    Jane in Jack's tale, Red in Red's.
    """
    return story.default_name(settings.effective_gender)


def _substitutions(settings: CartoonSettings, name: str) -> dict[str, str]:
    """The name and pronouns the prose is written with."""
    pronouns = _PRONOUNS[settings.effective_gender]
    capitalised = {key.capitalize(): value.capitalize() for key, value in pronouns.items()}
    return {"name": name, **pronouns, **capitalised}


def render_book_page(
    page: BookPage, settings: CartoonSettings, *, prompt_name: str
) -> BookPage:
    """Fill a book page's name/pronoun placeholders for the chosen child."""
    return page.formatted(_substitutions(settings, prompt_name))


def _star_outfit(story: StoryEdition, settings: CartoonSettings, name: str) -> str:
    """What the star wears: the story's costume when it has one, else the pick.

    Jack's tale dresses the child in the visitor's own outfit; Red's dresses
    them in the red hood, whatever the outfit picker said.
    """
    costume = story.star_member.costume
    if costume:
        return costume.format(**_substitutions(settings, name))
    return _OUTFIT_DESCRIPTIONS[settings.outfit]


def _story_lines(story: StoryEdition, settings: CartoonSettings, prompt_name: str) -> str:
    """The whole book's story, one labelled line per page.

    The lines come from the pages' own `beat` text — the one-line summary of
    each moment — so the story is stated once in the catalogue and never
    restated in a prompt template. Labelling them is what makes the block
    usable: the model can point at the line it is illustrating, and can see
    that the beans on one page are the "five magic beans" of another page's
    line.

    The pages' full prose is deliberately *not* shipped here. The PDF prints
    the narrative; the model needs the shape of the tale, and every call
    would otherwise carry nineteen paragraphs it does not need.
    """
    lines = []
    for page in story.pages:
        rendered = render_book_page(page, settings, prompt_name=prompt_name)
        summary = rendered.beat or rendered.story
        lines.append(f"Page {page.number}: {summary}")
    return "\n".join(lines)


def build_book_page_prompt(
    page: BookPage,
    settings: CartoonSettings,
    *,
    story: StoryEdition,
    prompt_name: str,
    supplied_characters: Iterable[str] = (),
    character_photos: Iterable[str] = (),
    correction: str = "",
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

    The prompt is two blocks: everything identical across the book's pages
    first, then this page's own material (task line, story, reference order,
    quiet zone, scene). A book is nineteen calls carrying the same book-level
    instructions, so the invariant block is exactly what a provider's prefix
    cache can serve — and it can only do that if nothing page-specific comes
    before it.

    Args:
        page: The resolved page (callers pass the raw catalogue entry; the
            name/pronoun placeholders are filled here).
        settings: The character settings.
        story: The edition the book is made from: its pages (for the page
            count and the whole-story context), its bible and its wording.
        prompt_name: The star's name.
        supplied_characters: Cast keys that have a reference photo somewhere in
            this book. Book-level, so the cast-sheet block stays byte-identical
            across the book's pages.
        character_photos: The page's own cast members that have a photo, in the
            order their references are sent. Page-level, because the reference
            images are.
    """
    task_book = cast("dict[str, str]", _BOOK["assembly"])
    wording = _STORIES[story.key]
    page_number = page.number
    total_pages = len(story.pages)
    page = render_book_page(page, settings, prompt_name=prompt_name)
    subs = _substitutions(settings, prompt_name)
    # Order is deliberate, and it is two blocks rather than one list.
    #
    # Everything that is byte-identical on every page of a book comes first:
    # about 7,500 characters (roughly 1,900 tokens) of this prompt is that
    # block, and a page-specific line in front of it means no provider can ever
    # serve it from a prefix cache. One book is nineteen calls of the same
    # book-level instructions, so the invariant block is the part worth
    # caching — and it clears a 1,024-token cache floor on its own.
    #
    # Everything that changes page to page — the task line and its page
    # number, the story context, this page's reference order, its quiet zone
    # and its scene — comes after it.
    shared = [
        task_book["page_identity"],
        settings.identity_directive(),
        _LIKENESS_EMPHASIS[settings.likeness_strength],
        # What the likeness dial means on book pages, and where it loses:
        # reading as one contract with the identity pressure above it and the
        # coherence guard below it — the pair no longer sit pages apart.
        str(_BOOK["likeness_not_size"]),
        str(_BOOK["body_coherence"]),
        str(_BOOK["same_age"]).format(**subs),
        str(_BOOK["same_clothes"]),
        str(_BOOK["signature_features"]),
        str(_BOOK["same_characters"]),
        str(_BOOK["one_art_style"]),
        # The story's fixed style, injected verbatim — the same wording on
        # every page of the book by construction, and the same sentence the
        # portrait resolves to (both read the shared [styles] entry).
        _ASSEMBLY["art_style"].format(style=wording.style),
        _ASSEMBLY["composition"].format(aspect=_ASPECT_HINTS[settings.aspect_ratio]),
        task_book["stylisation"].format(
            intensity=_book_intensity_text(settings.cartoon_intensity)
        ),
        # The expression picker is left out on purpose: a page's face follows
        # its EMOTIONAL BEAT, and "a warm, happy expression" on every page
        # fought the fear and wonder beats.
        _common_character_block(
            settings,
            subs,
            _star_outfit(story, settings, prompt_name),
            include_expression=False,
        ),
        # The character bible: the same block, byte for byte, on every page of
        # this book — so no character's look is restated (and allowed to drift)
        # per page. It names every cast member; the page-scope clause below and
        # the negative tail are what keep the page to its own cast.
        book_cast_sheet(story, supplied_characters),
        # The prop and set bible: every recurring object and place designed
        # once, so no page re-describes the cottage or the harp its own way.
        wording.page_world.format(**subs),
        str(_BOOK["physics"]),
        str(_BOOK["mood_and_light"]),
        # The tale's own rules (Milky-White's coat, the giant's face).
        *(rule.format(**subs) for rule in wording.page_rules),
        # The book's own negative tail: the single-scene tail's "only the one
        # person described above" contradicts the pages that require the
        # mother, which is how the child's face got onto her.
        str(_BOOK["negative"]),
        # The single no-text rule for the whole book. It used to appear here
        # *and* on the first line of the negative tail above.
        str(_BOOK["no_text"]),
    ]
    this_page = [
        task_book["task"].format(
            page_number=page_number,
            total_pages=total_pages,
            book_title=story.title_for(subs["name"]),
            name=subs["name"],
        ),
        # The whole story, labelled by page, so the model knows which line it
        # is illustrating and what the others are for. This is also how page 3
        # learns that the beans it throws are the five of page 2.
        task_book["story_context"].format(
            story_lines=_story_lines(story, settings, prompt_name),
            page_number=page_number,
            total_pages=total_pages,
        ),
        # The exact order of the reference images sent with this call, so the
        # model can tell the child's two anchors from a character's portrait
        # and photograph. Page-level because the references are.
        _reference_map(story, character_photos),
        task_book["full_frame"],
        *_scene_block(page.scene, page.emotion),
        correction,
        STANDING_NEGATIVE,
    ]
    return _join_sections([*shared, *this_page])


def _scene_block(scene: Scene, emotion: str) -> list[str]:
    """One scene, as the page and the cover both state it."""
    task_book = cast("dict[str, str]", _BOOK["assembly"])
    return [
        _ASSEMBLY["scene_to_draw"].format(caption=scene.caption),
        _ASSEMBLY["character_doing"].format(action=scene.subject_action),
        task_book["emotion"].format(emotion=emotion) if emotion else "",
        _ASSEMBLY["where"].format(environment=scene.environment),
        _ASSEMBLY["lighting"].format(time_of_day=scene.time_of_day),
        _ASSEMBLY["camera"].format(camera=scene.camera),
        scene.checklist,
        _avoid_clause(scene),
    ]


def build_cover_prompt(
    settings: CartoonSettings, *, story: StoryEdition, prompt_name: str
) -> str:
    """Prompt for the book's one cover call.

    The cover shares the book's fixed style, identity anchor and consistency
    rules so it belongs to the same book as the pages, and draws the story's
    own cover scene. Only the child has reference images on the cover; anyone
    else the scene names is drawn from the bible. The title is drawn by the
    PDF layer, never by the model, so the upper third is asked to stay calm.
    """
    task_book = cast("dict[str, str]", _BOOK["assembly"])
    wording = _STORIES[story.key]
    subs = _substitutions(settings, prompt_name)
    name = subs["name"]
    cover = story.cover.formatted(subs)
    sections = [
        task_book["cover_task"].format(book_title=story.title_for(name), name=name),
        settings.identity_directive(),
        _LIKENESS_EMPHASIS[settings.likeness_strength],
        str(_BOOK["likeness_not_size"]),
        str(_BOOK["body_coherence"]),
        str(_BOOK["same_age"]).format(**subs),
        str(_BOOK["same_clothes"]),
        str(_BOOK["signature_features"]),
        str(_BOOK["one_art_style"]),
        _ASSEMBLY["art_style"].format(style=wording.style),
        _ASSEMBLY["composition"].format(aspect=_ASPECT_HINTS[settings.aspect_ratio]),
        task_book["stylisation"].format(
            intensity=_book_intensity_text(settings.cartoon_intensity)
        ),
        _common_character_block(settings, subs, _star_outfit(story, settings, name)),
        *_cover_cast(story, cover.cast),
        # The places and props on the cover are the book's own.
        wording.page_world.format(**subs),
        str(_BOOK["physics"]),
        *(rule.format(**subs) for rule in wording.page_rules),
        str(_BOOK["negative"]),
        str(_BOOK["no_text"]),
        *_scene_block(cover.scene, cover.emotion),
        task_book["cover_band"],
        STANDING_NEGATIVE,
    ]
    return _join_sections(sections)


def _cover_cast(story: StoryEdition, cast_keys: Sequence[str]) -> list[str]:
    """The bible looks of anyone besides the star the cover scene names.

    The cover sends only the child's references, so everyone else on it is
    drawn from their plain look.
    """
    return [story.cast[key].look for key in cast_keys if key != story.star]


def _common_character_block(
    settings: CartoonSettings,
    substitutions: Mapping[str, str],
    outfit: str,
    *,
    include_expression: bool = True,
) -> str:
    """The lines describing *who the character is*, shared by both passes.

    `outfit` is the star's clothing line: the visitor's pick, or the story's
    costume. Book pages pass ``include_expression=False``: their faces follow
    each page's emotional beat, not the portrait's expression pick.
    """
    return " ".join(
        [
            _AGE_DESCRIPTIONS[settings.apparent_age],
            _GENDER_DESCRIPTIONS[settings.character_gender],
            _ASSEMBLY["character_name"].format(**substitutions),
            _SKIN_DESCRIPTIONS[settings.skin_tone],
            _HAIR_STYLE_DESCRIPTIONS[settings.hair_style],
            _HAIR_COLOUR_DESCRIPTIONS[settings.hair_color],
            outfit,
            *([_EXPRESSION_DESCRIPTIONS[settings.expression]] if include_expression else []),
            *_accessory_directives(settings),
        ]
    )


def build_cartoon_prompt(
    settings: CartoonSettings,
    *,
    story: StoryEdition,
    rejection: RejectionFeedback | None = None,
) -> str:
    """Prompt for pass 1: stylise the uploaded photo into a cartoon portrait.

    A retry sends the *tweaked settings*, never the previous image as a
    reference: showing a rejected portrait invites the model to converge on its
    faults rather than move away from them. `rejection` carries the user's
    ticked faults as closed-set corrections; omitting it (or passing an empty
    one) yields a first-attempt prompt, byte for byte.

    The portrait is the star of `story`: it is named with the story's default
    name, and dressed in the story's costume when the tale has one.
    """
    style_text = _STYLE_DESCRIPTIONS[settings.cartoon_style]
    name = book_name_for(settings, story)
    subs = _substitutions(settings, name)
    retry_clauses = rejection.prompt_block() if rejection else ""
    sections = [
        _ASSEMBLY["task_cartoon"],
        settings.identity_directive(),
        _LIKENESS_EMPHASIS[settings.likeness_strength],
        _common_character_block(settings, subs, _star_outfit(story, settings, name)),
        _ASSEMBLY["stylisation"].format(intensity=_intensity_text(settings.cartoon_intensity)),
        _photoreal_keywords(settings.cartoon_intensity),
        _ASSEMBLY["art_style"].format(style=style_text),
        _ASSEMBLY["framing"],
        _PORTRAIT_BACKGROUNDS[settings.portrait_background],
        _ASSEMBLY["composition"].format(aspect=_ASPECT_HINTS[settings.aspect_ratio]),
        retry_clauses,
        NEGATIVE_PROMPT_TAIL,
        STANDING_NEGATIVE,
    ]
    return _join_sections(sections)


def build_cast_portrait_prompt(
    story: StoryEdition,
    cast_key: str,
    settings: CartoonSettings,
    *,
    rejection: RejectionFeedback | None = None,
) -> str:
    """Prompt for a photographed cast member's portrait pass.

    Deliberately not `build_cartoon_prompt`: that prompt names the child,
    their age, pronouns, hair, outfit and expression, so the mother's photo
    came back prompted as a second Jack. This one carries only the cast
    member's own look, the book's fixed style and the user's portrait dials,
    so their portrait matches the pages they appear on while staying that
    person.

    How the slot's Advanced pickers apply (issue #34): `cartoon_style` is
    ignored on purpose — a cast member exists only inside the book, so they
    are always drawn in the book's one style; `cartoon_intensity` is read
    through the book's bands, which move graphic-ness inside that style
    rather than towards a photograph; `portrait_background`, the likeness
    strength and the glasses / facial-hair switches apply as chosen.

    Raises:
        KeyError: if `cast_key` is not a photographable supporting character
            of `story`. The route refuses those before this point.
    """
    if not story.can_photograph(cast_key):
        raise KeyError(f"{story.key} has no photographable character {cast_key!r}")
    wording = _STORIES[story.key]
    retry_clauses = rejection.prompt_block() if rejection else ""
    sections = [
        _ASSEMBLY["task_cast_portrait"],
        settings.identity_directive(),
        _LIKENESS_EMPHASIS[settings.likeness_strength],
        story.cast[cast_key].portrait,
        # The slot's own glasses / facial-hair pickers (issue #34: they were
        # sent by the browser and then ignored).
        *_accessory_directives(settings),
        _ASSEMBLY["stylisation"].format(
            intensity=_book_intensity_text(settings.cartoon_intensity)
        ),
        _photoreal_keywords(settings.cartoon_intensity),
        _ASSEMBLY["art_style"].format(style=wording.style),
        _ASSEMBLY["framing"],
        _PORTRAIT_BACKGROUNDS[settings.portrait_background],
        _ASSEMBLY["composition"].format(aspect=_ASPECT_HINTS[settings.aspect_ratio]),
        retry_clauses,
        NEGATIVE_PROMPT_TAIL,
        STANDING_NEGATIVE,
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
