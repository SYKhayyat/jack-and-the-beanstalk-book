"""Pages the star is not in, and where each character rests on a plate.

Red's book drew the child on pages that happen before the child arrives (the
wolf at Grandmother's door, the wolf in her bed) and put Grandmother on her
feet where the words have her in bed. A page now says whether the star is in
its picture, and a slot says whether its figure is in bed, kneeling or seated.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from app.books import SEEDS, SeedError, load_seed, picture_cast
from app.models.settings import CartoonSettings
from app.services.book_runner import _Artwork, _cast_note, _plate_references, _references
from app.services.book_store import CharacterReference
from app.services.plates import page_cast, plate_plans, slots_for
from app.services.prompts import build_book_page_prompt, build_plate_edit_prompt
from app.services.template_store import RESTS, PlateArt, Slot, TemplateError

from .helpers import edition_for

RED = next(seed for seed in SEEDS if seed.key == "little_red_riding_hood")
RED_SEED = Path("app/books/little_red_riding_hood.toml")
STARLESS = ("the-wolf-runs", "the-cottage-door", "the-cupboard", "the-disguise")


def _red_page(key: str):
    pages = edition_for("little_red_riding_hood").pages
    return next(page for page in pages if page.key == key)


def _reference(tag: bytes) -> CharacterReference:
    return CharacterReference(
        portrait=b"portrait-" + tag,
        portrait_media_type="image/png",
        photo=b"photo-" + tag,
        photo_media_type="image/png",
    )


def _artwork(**characters: CharacterReference) -> _Artwork:
    return _Artwork(
        about=None,
        settings=CartoonSettings(),
        portrait=b"star-portrait",
        portrait_media_type="image/png",
        photo=b"star-photo",
        photo_media_type="image/png",
        characters=characters,
        plates={},
    )


# --- the seed ---------------------------------------------------------------


def test_picture_cast_puts_the_star_first_only_when_they_are_in_the_picture() -> None:
    assert picture_cast("red", ("wolf", "red")) == ("red", "wolf")
    assert picture_cast("red", ("grandmother", "wolf"), star_in_picture=False) == (
        "grandmother",
        "wolf",
    )


def test_reds_pages_before_the_child_arrives_leave_the_child_out() -> None:
    absent = {page.key for page in RED.pages if not page.scene.star_in_picture}
    assert absent == set(STARLESS)
    for page in RED.pages:
        if page.key in STARLESS:
            assert "red" not in page_cast(RED, page.scene)
            assert "{name}" not in page.scene.caption, page.key


def test_the_wolf_is_at_grandmothers_door() -> None:
    door = next(page for page in RED.pages if page.key == "the-cottage-door")
    assert page_cast(RED, door.scene) == ("grandmother", "wolf")


def _seed_with(tmp_path: Path, page_key: str, **fields: object) -> Path:
    text = RED_SEED.read_text()
    marker = f'key = "{page_key}"\n'
    extra = "".join(
        f"{name} = {str(value).lower() if isinstance(value, bool) else repr(value)}\n"
        for name, value in fields.items()
    )
    target = tmp_path / RED_SEED.name
    target.write_text(text.replace(marker, marker + extra, 1))
    return target


def test_the_star_flag_must_be_a_bool(tmp_path: Path) -> None:
    with pytest.raises(SeedError, match="star_in_picture"):
        load_seed(_seed_with(tmp_path, "the-path", star_in_picture="no"))


def test_a_page_without_the_star_must_cast_someone(tmp_path: Path) -> None:
    with pytest.raises(SeedError, match="must cast someone"):
        load_seed(_seed_with(tmp_path, "the-path", star_in_picture=False))


def test_the_cover_always_shows_the_star(tmp_path: Path) -> None:
    text = RED_SEED.read_text().replace(
        'title_band = "calm"\n', 'title_band = "calm"\nstar_in_picture = false\n', 1
    )
    target = tmp_path / RED_SEED.name
    target.write_text(text)
    with pytest.raises(SeedError, match="cover"):
        load_seed(target)


def test_reds_costume_is_trousers_never_a_dress() -> None:
    costume = tomllib.loads(RED_SEED.read_text())["cast"]["red"]["costume"]
    assert "trousers" in costume and "never a dress" in costume
    assert "tunic that reaches the knee" not in costume


# --- slots ------------------------------------------------------------------


def test_a_slot_rest_round_trips_and_is_checked() -> None:
    box = (0.1, 0.1, 0.3, 0.3)
    slot = Slot(cast_key="wolf", rect=box, scale=0.3, facing="left", rest="in_bed")
    assert Slot.from_json(slot.to_json()) == slot
    plain = Slot(cast_key="wolf", rect=(0.1, 0.1, 0.3, 0.3), scale=0.3, facing="left")
    assert "rest" not in plain.to_json()
    assert set(RESTS) == {"", "in_bed", "kneeling", "seated"}
    with pytest.raises(TemplateError, match="rest must be one of"):
        Slot(
            cast_key="wolf", rect=(0.1, 0.1, 0.3, 0.3), scale=0.3, facing="left", rest="flying"
        ).check()


def test_a_staging_carries_rest_and_facing() -> None:
    slots = slots_for(["red", "wolf"], staging="door_to_the_bed")
    assert [(s.cast_key, s.facing, s.rest) for s in slots] == [
        ("red", "right", ""),
        ("wolf", "left", "in_bed"),
    ]


def test_every_red_bed_page_puts_its_sleeper_in_the_bed() -> None:
    in_bed = {
        plan.key: next(slot.cast_key for slot in plan.slots if slot.rest == "in_bed")
        for plan in plate_plans(RED)
        if any(slot.rest == "in_bed" for slot in plan.slots)
    }
    assert in_bed == {
        "the-cottage-door": "grandmother",
        "the-cupboard": "grandmother",
        "the-disguise": "wolf",
        "red-arrives": "wolf",
        "big-ears": "wolf",
    }


def test_the_child_walks_towards_the_smoke() -> None:
    plan = next(plan for plan in plate_plans(RED) if plan.key == "the-path")
    assert plan.slots[0].facing == "away"


# --- the edit prompt --------------------------------------------------------


def _edit(page_key: str, slots: tuple[Slot, ...], **kwargs: object) -> str:
    return build_plate_edit_prompt(
        _red_page(page_key),
        slots,
        story=edition_for("little_red_riding_hood"),
        settings=CartoonSettings(photo_gender="boy"),
        prompt_name="Sam",
        **kwargs,
    )


def test_a_starless_edit_says_no_child_and_names_no_child_action() -> None:
    slots = slots_for(["grandmother", "wolf"], staging="door_and_bed")
    prompt = _edit("the-cottage-door", slots)
    assert "Sam IS NOT IN THIS PICTURE" in prompt
    assert "WHAT HAPPENS:" in prompt
    assert "WHAT Sam IS DOING" not in prompt
    assert "lying with the head on the pillow" in prompt  # Grandmother's in_bed slot


def test_an_edit_with_the_star_dresses_them_in_the_costume() -> None:
    slots = slots_for(["red", "wolf"], staging="door_to_the_bed")
    prompt = _edit("red-arrives", slots)
    assert "IS NOT IN THIS PICTURE" not in prompt
    assert "brown trousers" in prompt
    assert "lying with the head on the pillow" in prompt


def test_walking_away_is_said_as_seen_from_behind() -> None:
    slots = slots_for(["red"], staging="away_to_the_smoke")
    assert "Seen from behind" in _edit("the-path", slots)


def test_a_supplied_character_uses_the_look_with_a_photo() -> None:
    slots = slots_for(["red", "mother"], staging="hood_and_basket")
    without = _edit("the-basket", slots)
    supplied = _edit("the-basket", slots, supplied_characters=("mother",))
    assert "she has reference images" not in without
    assert "she has reference images" in supplied


# --- the runner -------------------------------------------------------------


def test_a_starless_plate_sends_no_image_of_the_child() -> None:
    page = _red_page("the-disguise")
    plate = PlateArt(
        plate_id="p",
        image=b"plate",
        media_type="image/png",
        slots=slots_for(["wolf"], staging="wolf_in_bed"),
    )
    references, _ = _plate_references(_artwork(), plate, page, star="red")
    assert references == [b"plate"]


def test_a_plate_with_the_star_sends_their_portrait_once() -> None:
    page = _red_page("big-ears")
    plate = PlateArt(
        plate_id="p",
        image=b"plate",
        media_type="image/png",
        slots=slots_for(["red", "wolf"], staging="beside_the_bed"),
    )
    references, _ = _plate_references(_artwork(), plate, page, star="red")
    assert references == [b"plate", b"star-portrait"]


def test_a_starless_page_from_scratch_sends_only_the_cast() -> None:
    page = _red_page("the-cottage-door")
    references, _ = _references(_artwork(grandmother=_reference(b"gran")), page)
    assert b"star-portrait" not in references and b"star-photo" not in references
    assert references == [b"portrait-gran", b"photo-gran"]


def test_the_reviewer_is_told_no_child_belongs_on_a_starless_page() -> None:
    edition = edition_for("little_red_riding_hood")
    note = _cast_note(_red_page("the-disguise"), edition)
    assert "NO CHILD" in note and "the child," not in note
    assert "the child" in _cast_note(_red_page("big-ears"), edition)


def test_a_starless_page_from_scratch_says_the_child_is_absent() -> None:
    prompt = build_book_page_prompt(
        _red_page("the-disguise"),
        CartoonSettings(photo_gender="boy"),
        story=edition_for("little_red_riding_hood"),
        prompt_name="Sam",
    )
    assert "Sam IS NOT IN THIS PICTURE" in prompt
