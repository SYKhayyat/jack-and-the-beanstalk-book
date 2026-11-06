"""Per-story seed files: the words, the cast and the cover for each book.

A story is data. Before a story can be rendered it must satisfy the same
contract as the other: exactly twenty story pages, one cover, one About page,
a cast every page draws from, and prose simple enough to read aloud. The seed
file is the reviewed source; the database is what the running app reads (T4),
loaded from these seeds at every boot. The seeds are validated when this module
is imported, so a broken story fails at start-up rather than on a child's book.

The checks here are the acceptance constraints from
``docs/book-template-plan.md`` T2, in code:

* twenty story pages, one cover;
* 45-90 words of prose per page, no sentence over 22 words, no word outside the
  shared simple vocabulary plus the story's own allow-list;
* one or two syllables where the story can manage it, never more than three;
* every cast key a page names exists in that story's character bible, and
  every bible entry has the looks its photo rule needs;
* no page references more characters than the image provider can take;
* the prose is never reused as an image prompt;
* no scene restates a character's look — the bible is its only source (T4.3).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "COMPOSE_FIELDS",
    "MAX_REFERENCE_CAST",
    "PAGE_COUNT",
    "SEEDS",
    "SEED_DIR",
    "SeedCharacter",
    "SeedError",
    "SeedPage",
    "SeedScene",
    "StorySeed",
    "check_readability",
    "load_seed",
    "seed_from_mapping",
    "seed_paths",
]

SEED_DIR = Path(__file__).resolve().parent
VOCABULARY_PATH = SEED_DIR / "simple_words.txt"

#: The template holds twenty story pages for every story, plus the cover and
#: the About page the PDF composes locally.
PAGE_COUNT = 20

MIN_WORDS = 45
MAX_WORDS = 90
MAX_SENTENCE_WORDS = 22
MAX_SYLLABLES = 4
MAX_AVERAGE_WORD_LENGTH = 5.0

#: The scene lines a page may reword for the finished picture. The plate is an
#: empty stage; a finished page has the child in it, so its framing and light
#: can need saying differently. Everything else in a scene is shared.
COMPOSE_FIELDS = ("environment", "time_of_day", "camera")

#: The photo-gender answers a story names its default star for.
_NAME_KEYS = frozenset({"boy", "girl"})

#: How many image-referenced characters a page may cast, the star included.
#: The edit pass sends the plate plus one approved portrait per referenced
#: character; Gemini accepts three reference images per prompt, so two is the
#: ceiling that leaves room for the plate.
MAX_REFERENCE_CAST = 2


class SeedError(ValueError):
    """A story seed breaks the shared contract. Raised at boot, never later."""


@dataclass(frozen=True, slots=True)
class SeedScene:
    """The image specification for a page or the cover."""

    caption: str
    subject_action: str
    environment: str
    time_of_day: str
    camera: str
    must_include: tuple[str, ...]
    must_avoid: tuple[str, ...]
    cast: tuple[str, ...]
    #: The finished picture's own wording for any of `COMPOSE_FIELDS`.
    compose: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SeedCharacter:
    """One entry of the character bible: the only place a look is written.

    `look` is how a page draws the character with no photograph. A photoable
    supporting character also has `look_with_photo` (face and colouring defer
    to the uploaded portrait, the costume stays fixed) and `portrait`, the brief
    the portrait pass draws them from. Only the star may have a `costume`: a
    tale whose hero wears the story's clothes (the red hood) instead of the
    visitor's own outfit.
    """

    key: str
    label: str
    photoable: bool
    look: str
    look_with_photo: str = ""
    portrait: str = ""
    costume: str = ""


@dataclass(frozen=True, slots=True)
class SeedPage:
    """One story page: its scene plus the words the PDF prints beneath it."""

    number: int
    key: str
    title: str
    story: str
    beat: str
    emotion: str
    scene: SeedScene


@dataclass(frozen=True, slots=True)
class StorySeed:
    """One reviewed story: metadata, cast, cover and twenty pages."""

    key: str
    title: str
    blurb: str
    #: The cover's title, with `{name}` where the star's name goes.
    book_title: str
    star: str
    available: bool
    #: The name a blank name field becomes, keyed by photo-gender answer.
    names: Mapping[str, str]
    cover: SeedScene
    title_band: str
    #: The character bible, in the order the studio offers its slots.
    cast: Mapping[str, SeedCharacter]
    pages: tuple[SeedPage, ...]

    @property
    def page_keys(self) -> tuple[str, ...]:
        return tuple(page.key for page in self.pages)


# A sentence ends at . ! or ?, with or without a closing quote after it. The
# quote-aware branch matters: dialogue is ordinary prose here.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|(?<=[.!?][\"'])\s+")
_WORD = re.compile(r"[a-z]+(?:'[a-z]+)?")
_PLACEHOLDER = re.compile(r"\{[a-zA-Z_]+\}")
_VOWEL_GROUP = re.compile(r"[aeiouy]+")


def seed_paths() -> tuple[Path, ...]:
    """Every story seed file, in a stable order."""
    return tuple(sorted(SEED_DIR.glob("*.toml")))


def load_seed(path: Path) -> StorySeed:
    """Parse and validate one seed file.

    Raises:
        SeedError: if anything in the file breaks the shared contract.
    """
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SeedError(f"{path.name}: cannot read seed: {exc}") from exc
    return seed_from_mapping(data, path.name)


def seed_from_mapping(data: Mapping[str, Any], source: str = "<seed>") -> StorySeed:
    """Validate an already-parsed seed. The one validator, used by boot and tests."""
    story = _table(data, "story", source)
    key = _text(story, "key", source)
    star = _text(story, "star", source)
    cast = _cast(data, star, source)
    book_title = _text(story, "book_title", source)
    if "{name}" not in book_title:
        raise SeedError(f"{source}: book_title must say where the star's {{name}} goes")
    names = _names(story, source)

    cover_table = _table(data, "cover", source)
    cover = _scene(cover_table, source, "cover")
    _check_no_look_restated(cover, cast, source, "cover")

    raw_pages = data.get("pages")
    if not isinstance(raw_pages, list):
        raise SeedError(f"{source}: [pages] is missing")
    if len(raw_pages) != PAGE_COUNT:
        raise SeedError(
            f"{source}: a story has exactly {PAGE_COUNT} pages, this has {len(raw_pages)}"
        )

    photoable = {name for name, character in cast.items() if character.photoable}
    allowed = _vocabulary(data, source)
    pages: list[SeedPage] = []
    seen: set[str] = set()
    for number, raw in enumerate(raw_pages, start=1):
        if not isinstance(raw, Mapping):
            raise SeedError(f"{source}: page {number} is not a table")
        page_key = _text(raw, "key", f"{source} page {number}")
        if page_key in seen:
            raise SeedError(f"{source}: duplicate page key {page_key!r}")
        seen.add(page_key)
        scene = _scene(raw, source, page_key)
        _check_no_look_restated(scene, cast, source, page_key)
        for name in scene.cast:
            if name not in cast:
                raise SeedError(
                    f"{source} page {page_key!r}: cast member {name!r} is not in [cast]"
                )
        story_text = _text(raw, "story", f"{source} page {page_key}")
        _check_prose(story_text, source, page_key, allowed)
        _check_prose_is_not_a_prompt(story_text, raw, source, page_key)
        if len({star, *scene.cast} & photoable) > MAX_REFERENCE_CAST:
            raise SeedError(
                f"{source} page {page_key!r}: casts more than "
                f"{MAX_REFERENCE_CAST} image-referenced characters"
            )
        pages.append(
            SeedPage(
                number=number,
                key=page_key,
                title=_text(raw, "title", f"{source} page {page_key}"),
                story=story_text,
                beat=_text(raw, "beat", f"{source} page {page_key}"),
                emotion=_text(raw, "emotion", f"{source} page {page_key}"),
                scene=scene,
            )
        )

    return StorySeed(
        key=key,
        title=_text(story, "title", source),
        blurb=_text(story, "blurb", source),
        book_title=book_title,
        star=star,
        available=bool(story.get("available", True)),
        names=names,
        cover=cover,
        title_band=str(cover_table.get("title_band", "calm")),
        cast=cast,
        pages=tuple(pages),
    )


def _table(data: Mapping[str, Any], name: str, where: str) -> Mapping[str, Any]:
    value = data.get(name)
    if not isinstance(value, Mapping):
        raise SeedError(f"{where}: [{name}] is missing")
    return value


def _text(spec: Mapping[str, Any], field: str, where: str) -> str:
    value = spec.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SeedError(f"{where}: {field!r} is missing or blank")
    return value.strip()


_CAST_FIELDS = frozenset(
    {"label", "photoable", "look", "look_with_photo", "portrait", "costume"}
)


def _cast(data: Mapping[str, Any], star: str, where: str) -> dict[str, SeedCharacter]:
    """The character bible, validated entry by entry.

    The rules are what keep a look in one place and a face where it belongs:
    the star is photoable and drawn from the visitor's portrait, so it has no
    photo variant and no portrait brief; a photoable supporting character has
    both; a character who can never be photographed has neither; and only the
    star wears a story costume.
    """
    table = _table(data, "cast", where)
    if star not in table:
        raise SeedError(f"{where}: star {star!r} is not in [cast]")
    cast: dict[str, SeedCharacter] = {}
    for name, raw in table.items():
        at = f"{where} [cast.{name}]"
        if not isinstance(raw, Mapping):
            raise SeedError(f"{at}: must be a table with a label, photoable and look")
        unknown = set(raw) - _CAST_FIELDS
        if unknown:
            raise SeedError(f"{at}: unknown fields {sorted(unknown)}")
        photoable = raw.get("photoable")
        if not isinstance(photoable, bool):
            raise SeedError(f"{at}: photoable must be true or false")
        character = SeedCharacter(
            key=str(name),
            label=_text(raw, "label", at),
            photoable=photoable,
            look=_text(raw, "look", at),
            look_with_photo=_optional_text(raw, "look_with_photo", at),
            portrait=_optional_text(raw, "portrait", at),
            costume=_optional_text(raw, "costume", at),
        )
        if name == star:
            if not photoable:
                raise SeedError(f"{at}: the star is the visitor's child and is photoable")
            if character.look_with_photo or character.portrait:
                raise SeedError(
                    f"{at}: the star is drawn from the visitor's own portrait; "
                    "it has no look_with_photo or portrait brief"
                )
        else:
            if character.costume:
                raise SeedError(f"{at}: only the star wears a story costume")
            if photoable and not (character.look_with_photo and character.portrait):
                raise SeedError(
                    f"{at}: a photoable character needs look_with_photo and portrait"
                )
            if not photoable and (character.look_with_photo or character.portrait):
                raise SeedError(f"{at}: a character with no photograph has no photo look")
        cast[str(name)] = character
    return cast


def _optional_text(spec: Mapping[str, Any], field_name: str, where: str) -> str:
    if field_name not in spec:
        return ""
    return _text(spec, field_name, where)


def _names(story: Mapping[str, Any], where: str) -> dict[str, str]:
    table = story.get("names")
    if not isinstance(table, Mapping) or set(table) != _NAME_KEYS:
        raise SeedError(f"{where}: [story.names] must name the star for {sorted(_NAME_KEYS)}")
    return {str(key): _text(table, str(key), f"{where} [story.names]") for key in table}


def _scene(spec: Mapping[str, Any], where: str, name: str) -> SeedScene:
    cast_value = spec.get("cast", [])
    if not isinstance(cast_value, list) or not all(
        isinstance(item, str) for item in cast_value
    ):
        raise SeedError(f"{where} {name!r}: cast must be a list of names")
    return SeedScene(
        caption=_text(spec, "caption", f"{where} {name!r}"),
        subject_action=_text(spec, "subject_action", f"{where} {name!r}"),
        environment=_text(spec, "environment", f"{where} {name!r}"),
        time_of_day=_text(spec, "time_of_day", f"{where} {name!r}"),
        camera=_text(spec, "camera", f"{where} {name!r}"),
        must_include=_strings(spec, "must_include", where, name),
        must_avoid=_strings(spec, "must_avoid", where, name),
        cast=tuple(cast_value),
        compose=_compose(spec, where, name),
    )


def _compose(spec: Mapping[str, Any], where: str, name: str) -> dict[str, str]:
    table = spec.get("compose", {})
    if not isinstance(table, Mapping):
        raise SeedError(f"{where} {name!r}: compose must be a table")
    unknown = set(table) - set(COMPOSE_FIELDS)
    if unknown:
        raise SeedError(
            f"{where} {name!r}: compose may only reword {list(COMPOSE_FIELDS)}, "
            f"not {sorted(unknown)}"
        )
    return {
        key: _text(table, key, f"{where} {name!r} compose")
        for key in COMPOSE_FIELDS
        if key in table
    }


def _strings(spec: Mapping[str, Any], field: str, where: str, name: str) -> tuple[str, ...]:
    value = spec.get(field)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise SeedError(f"{where} {name!r}: {field} must be a list of non-empty strings")
    return tuple(str(item).strip() for item in value)


@lru_cache(maxsize=1)
def _shared_vocabulary() -> frozenset[str]:
    try:
        text = VOCABULARY_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise SeedError(f"cannot read {VOCABULARY_PATH.name}: {exc}") from exc
    return frozenset(word for word in _normalise(text.split()) if word)


def _vocabulary(data: Mapping[str, Any], where: str) -> frozenset[str]:
    table = data.get("vocabulary", {})
    if not isinstance(table, Mapping):
        raise SeedError(f"{where}: [vocabulary] must be a table")
    extra = table.get("allow", [])
    if not isinstance(extra, list) or not all(isinstance(word, str) for word in extra):
        raise SeedError(f"{where}: [vocabulary].allow must be a list of words")
    return _shared_vocabulary() | frozenset(_normalise([str(word) for word in extra]))


def _normalise(words: Iterable[str]) -> list[str]:
    """Lower-case words with possessives and punctuation removed."""
    cleaned: list[str] = []
    for raw in words:
        for match in _WORD.findall(raw.lower().replace("\u2019", "'")):
            if match.endswith("'s"):
                match = match[:-2]
            cleaned.append(match)
    return cleaned


def _words(text: str) -> list[str]:
    return _normalise([_PLACEHOLDER.sub(" ", text)])


def _syllables(word: str) -> int:
    groups = _VOWEL_GROUP.findall(word)
    return max(1, len(groups))


def check_readability(text: str, where: str, key: str) -> None:
    """The read-aloud rules, without the vocabulary gate.

    Length, sentence length, syllables and word length are what make a page
    readable to a five-year-old and still worth reading at twenty. They are
    checked on every page the app can print, seeds and the built-in catalogue
    alike; the word list is the seeds' own editing tool.
    """
    words = _words(text)
    if not MIN_WORDS <= len(words) <= MAX_WORDS:
        raise SeedError(
            f"{where} page {key!r}: {len(words)} words, must be {MIN_WORDS}-{MAX_WORDS}"
        )
    sentences = [part for part in _SENTENCE_SPLIT.split(text) if part.strip()]
    for sentence in sentences:
        count = len(_words(sentence))
        if count > MAX_SENTENCE_WORDS:
            raise SeedError(
                f"{where} page {key!r}: a {count}-word sentence, the limit is "
                f"{MAX_SENTENCE_WORDS}"
            )
    for word in words:
        if _syllables(word) > MAX_SYLLABLES:
            raise SeedError(f"{where} page {key!r}: {word!r} has too many syllables")
    average = sum(len(word) for word in words) / len(words)
    if average > MAX_AVERAGE_WORD_LENGTH:
        raise SeedError(
            f"{where} page {key!r}: average word length {average:.1f}, "
            f"the limit is {MAX_AVERAGE_WORD_LENGTH}"
        )


def _check_prose(text: str, where: str, key: str, allowed: frozenset[str]) -> None:
    check_readability(text, where, key)
    for word in _words(text):
        if word not in allowed:
            raise SeedError(f"{where} page {key!r}: {word!r} is not in the simple vocabulary")


def _check_prose_is_not_a_prompt(
    story_text: str, spec: Mapping[str, Any], where: str, key: str
) -> None:
    """The picture prompts must not be fed the paragraph the PDF prints.

    They travel in one block so they cannot contradict each other, but the prose
    is not a prompt: a page's scene fields must never contain it. This is a
    cheap structural check, not a wording check.
    """
    needle = _PLACEHOLDER.sub("", story_text).strip()
    for name in ("caption", "subject_action", "environment", "camera", "beat"):
        value = spec.get(name)
        if isinstance(value, str) and needle and needle in _PLACEHOLDER.sub("", value):
            raise SeedError(f"{where} page {key!r}: {name} contains the story prose")


#: A look sentence this long, copied into a scene, is a restated description.
_RESTATED_WORDS = 8


def _check_no_look_restated(
    scene: SeedScene, cast: Mapping[str, SeedCharacter], where: str, key: str
) -> None:
    """A page names its cast by key; only `[cast]` describes them.

    If a page copied a look into its scene, editing the bible would leave the
    page drawing the old look. Any sentence of eight or more words from a look,
    found verbatim in a scene field, is refused.
    """
    fields = [
        scene.caption,
        scene.subject_action,
        scene.environment,
        scene.time_of_day,
        scene.camera,
        *scene.must_include,
        *scene.must_avoid,
        *scene.compose.values(),
    ]
    haystack = " ".join(" ".join(_normalise(text.split())) for text in fields)
    for name, character in cast.items():
        looks = " ".join(
            (character.look, character.look_with_photo, character.portrait, character.costume)
        )
        for sentence in re.split(r"(?<=[.!?])\s+", looks):
            words = _normalise(sentence.split())
            if len(words) >= _RESTATED_WORDS and " ".join(words) in haystack:
                raise SeedError(
                    f"{where} {key!r}: restates {name!r}'s look; name the cast key and "
                    "let [cast] describe them"
                )


SEEDS: tuple[StorySeed, ...] = tuple(load_seed(path) for path in seed_paths())
