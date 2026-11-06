"""The story seeds: twenty pages, readable prose, a cast that resolves.

These tests are T2's acceptance criteria made executable. The validator under
test is the same one that runs at import, so a seed that passes here is a seed
the app can boot with.
"""

from __future__ import annotations

import copy
import re
import tomllib

import pytest

from app.books import (
    MAX_REFERENCE_CAST,
    PAGE_COUNT,
    SEED_DIR,
    SEEDS,
    SeedError,
    StorySeed,
    seed_from_mapping,
)
from app.models.book import BOOK_PAGES
from app.prompt_data import prompt_text

RED_PATH = SEED_DIR / "little_red_riding_hood.toml"
RED: StorySeed = next(seed for seed in SEEDS if seed.key == "little_red_riding_hood")
RED_DATA = tomllib.loads(RED_PATH.read_text(encoding="utf-8"))

JACK: StorySeed = next(seed for seed in SEEDS if seed.key == "jack_and_the_beanstalk")

PLACEHOLDER = re.compile(r"\{[a-zA-Z_]+\}")
WORD = re.compile(r"[a-z]+(?:'[a-z]+)?")


def words(text: str) -> list[str]:
    return WORD.findall(PLACEHOLDER.sub(" ", text).lower())


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|(?<=[.!?][\"'])\s+", text)
    return [part for part in parts if part.strip()]


def data() -> dict[str, object]:
    return copy.deepcopy(RED_DATA)


class TestTheSeedContract:
    def test_red_is_twenty_pages_plus_a_cover(self) -> None:
        assert RED.key == "little_red_riding_hood"
        assert len(RED.pages) == PAGE_COUNT == 20
        assert len(set(RED.page_keys)) == 20
        assert RED.cover.cast == ("red", "wolf")
        assert RED.title_band == "calm"

    def test_every_page_is_45_to_90_words_with_short_sentences(self) -> None:
        for page in RED.pages:
            count = len(words(page.story))
            assert 45 <= count <= 90, f"{page.key} has {count} words"
            for sentence in sentences(page.story):
                assert len(words(sentence)) <= 22, f"{page.key}: {sentence!r}"
            assert page.beat.strip() and page.emotion.strip()

    def test_every_page_names_its_cast_from_the_bible(self) -> None:
        for page in RED.pages:
            for name in page.scene.cast:
                assert name in RED.cast, f"{page.key} names {name!r}"
        assert RED.star in RED.cast

    def test_no_page_references_more_characters_than_the_provider_takes(self) -> None:
        photoable = {name for name, flag in RED.photoable.items() if flag}
        for page in RED.pages:
            referenced = ({RED.star, *page.scene.cast}) & photoable
            assert len(referenced) <= MAX_REFERENCE_CAST, page.key

    def test_the_wolf_can_never_be_given_a_photo(self) -> None:
        assert RED.photoable["wolf"] is False
        assert RED.photoable["red"] is True
        assert RED.photoable["woodcutter"] is True

    def test_the_prose_is_never_reused_as_a_prompt(self) -> None:
        for page in RED.pages:
            needle = PLACEHOLDER.sub("", page.story).strip()
            for field in (
                page.scene.caption,
                page.scene.subject_action,
                page.scene.environment,
                page.scene.camera,
            ):
                assert needle not in PLACEHOLDER.sub("", field), page.key

    def test_reds_tale_covers_the_whole_arc(self) -> None:
        prose = " ".join(page.story.lower() for page in RED.pages)
        for beat in (
            "basket",
            "wood",
            "wolf",
            "grandmother",
            "cupboard",
            "big ears",
            "big eyes",
            "big teeth",
            "woodcutter",
            "path",
        ):
            assert beat in prose, f"the story is missing {beat!r}"

    def test_nobody_is_hurt_and_no_page_needs_a_weapon(self) -> None:
        forbidden = ("blood", "wound", "dead", "corpse", "kill", "knife", "sword", "gun")
        for page in RED.pages:
            prose = page.story.lower()
            for word in forbidden:
                assert word not in prose, f"{page.key} contains {word!r}"
        # The axe exists only as an off-page sound, already set down.
        shout = next(page for page in RED.pages if page.key == "the-shout")
        assert "axe set down" in shout.story
        assert all("axe" not in page.scene.must_include for page in RED.pages)


class TestTheValidatorRefusesBadSeeds:
    def test_a_story_must_have_twenty_pages(self) -> None:
        broken = data()
        broken["pages"] = broken["pages"][:-1]  # type: ignore[index]
        with pytest.raises(SeedError, match="exactly 20 pages"):
            seed_from_mapping(broken)

    def test_a_page_must_be_the_right_length(self) -> None:
        short = data()
        short["pages"][0]["story"] = "Once upon a time."  # type: ignore[index]
        with pytest.raises(SeedError, match="must be 45-90"):
            seed_from_mapping(short)

        long = data()
        long["pages"][0]["story"] = (  # type: ignore[index]
            long["pages"][0]["story"] + " And then another little thing happened." * 8  # type: ignore[index]
        )
        with pytest.raises(SeedError, match="must be 45-90"):
            seed_from_mapping(long)

    def test_a_sentence_over_the_limit_is_refused(self) -> None:
        broken = data()
        broken["pages"][0]["story"] = (  # type: ignore[index]
            "The morning was bright. "
            * 5
            + "The child set out with a basket that held bread and honey and a "
            "little cake for Grandmother who lived on the other side of the wood."
        )
        with pytest.raises(SeedError, match="sentence"):
            seed_from_mapping(broken)

    def test_a_word_outside_the_simple_vocabulary_is_refused(self) -> None:
        broken = data()
        broken["pages"][0]["story"] = broken["pages"][0]["story"].replace(  # type: ignore[index]
            "The morning was bright", "The morning was luminous"
        )
        with pytest.raises(SeedError, match="simple vocabulary"):
            seed_from_mapping(broken)

    def test_an_unknown_cast_member_is_refused(self) -> None:
        broken = data()
        broken["pages"][0]["cast"] = ["dragon"]  # type: ignore[index]
        with pytest.raises(SeedError):
            seed_from_mapping(broken)

    def test_too_many_referenced_characters_is_refused(self) -> None:
        broken = data()
        broken["pages"][0]["cast"] = ["mother", "grandmother"]  # type: ignore[index]
        with pytest.raises(SeedError, match="image-referenced"):
            seed_from_mapping(broken)

    def test_prose_smuggled_into_a_prompt_is_refused(self) -> None:
        broken = data()
        page = broken["pages"][0]  # type: ignore[index]
        page["caption"] = page["story"]
        with pytest.raises(SeedError, match="story prose"):
            seed_from_mapping(broken)

    def test_a_missing_cover_is_refused(self) -> None:
        broken = data()
        del broken["cover"]
        with pytest.raises(SeedError, match=r"\[cover\]"):
            seed_from_mapping(broken)

    def test_a_missing_photographable_flag_is_refused(self) -> None:
        broken = data()
        del broken["cast_photoable"]["wolf"]  # type: ignore[index]
        with pytest.raises(SeedError, match=r"\[cast_photoable\] is missing"):
            seed_from_mapping(broken)


class TestJacksCatalogue:
    def test_jack_has_twenty_story_pages(self) -> None:
        assert len(BOOK_PAGES) == 20
        assert [page.number for page in BOOK_PAGES] == list(range(1, 21))

    def test_the_second_climb_sits_between_the_gold_and_the_hen(self) -> None:
        keys = [page.key for page in BOOK_PAGES]
        assert keys[9:11] == ["the-second-climb", "the-hen"]
        page = BOOK_PAGES[9]
        assert "mother" in page.cast
        assert 45 <= len(words(page.story)) <= 90

    def test_every_jack_page_keeps_the_reference_cap(self) -> None:
        photoable = set(prompt_text()["book"]["cast_photoable"])
        for page in BOOK_PAGES:
            referenced = {"child", *page.cast} & photoable
            assert len(referenced) <= MAX_REFERENCE_CAST, page.key

    def test_jack_prose_stays_between_45_and_90_words(self) -> None:
        """The sentence-length floor is Red's; Jack's prose predates it.

        Jack's nineteen pages were written before T2's readability rule, and
        rewriting them is part of the seed move (T7), not this change. Length
        and cast are asserted now so the twentieth page cannot drift.
        """
        for page in BOOK_PAGES:
            count = len(words(page.story))
            assert 45 <= count <= 90, f"{page.key} has {count} words"


class TestJacksSeed:
    def test_the_seed_holds_twenty_pages_and_a_cover(self) -> None:
        assert JACK.star == "jack"
        assert len(JACK.pages) == PAGE_COUNT
        assert JACK.cover.cast == ("jack",)
        assert JACK.title_band == "calm"

    def test_the_seed_prints_the_words_the_catalogue_prints(self) -> None:
        """Two copies exist only until T7's other half lands. They must not drift."""
        assert [page.key for page in JACK.pages] == [page.key for page in BOOK_PAGES]
        for page, catalogue in zip(JACK.pages, BOOK_PAGES, strict=True):
            assert page.beat == catalogue.beat, page.key
            assert page.story == catalogue.story, page.key

    def test_the_cow_is_never_a_cast_member(self) -> None:
        """No photograph can be given to a cow, so she is painted into the plate."""
        assert "milky_white" not in JACK.cast
        assert all("milky_white" not in page.scene.cast for page in JACK.pages)
        assert "Milky-White" in JACK.pages[0].scene.environment
        assert "Milky-White" in JACK.pages[1].scene.environment

    def test_every_jack_plate_prompt_names_nobody(self) -> None:
        """A plate is the empty stage: no placeholder survives into one."""
        for page in JACK.pages:
            for field in ("environment", "time_of_day", "camera"):
                assert "{" not in getattr(page.scene, field), (page.key, field)
