"""T5: the empty plates — one command, one prompt set, one slot per character."""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from app.books import PAGE_COUNT, SEEDS, StorySeed
from app.scripts import make_plates
from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
)
from app.services.plates import (
    COVER_PAGE,
    MAX_PLATE_CAST,
    PlateError,
    build_plate_prompt,
    page_cast,
    plate_plans,
    slots_for,
)
from app.services.template_store import TemplateStore
from tests.helpers import RecordingProvider, tiny_png_bytes

RED = next(seed for seed in SEEDS if seed.key == "little_red_riding_hood")


class PaidProvider(ImageProvider):
    """A provider whose pictures cost money — the only kind that needs a yes."""

    name = "paid"

    @property
    def draws_placeholders(self) -> bool:
        return False

    def __init__(self) -> None:
        self.requests: list[ImageRequest] = []

    def generate(self, request: ImageRequest) -> GeneratedImage:
        self.requests.append(request)
        return GeneratedImage(data=tiny_png_bytes(), media_type="image/png", model="paid-v1")


# --- the plan ------------------------------------------------------------------


def test_a_story_gets_a_cover_and_one_plate_per_page() -> None:
    plans = plate_plans(RED)
    assert len(plans) == PAGE_COUNT + 1
    assert plans[0].page_number == COVER_PAGE
    assert plans[0].key == "cover"
    assert [plan.page_number for plan in plans] == list(range(PAGE_COUNT + 1))
    assert [plan.key for plan in plans[1:]] == list(RED.page_keys)


@pytest.mark.parametrize("story", SEEDS, ids=lambda seed: seed.key)
def test_every_plate_prompt_forbids_text_and_people(story: StorySeed) -> None:
    """T5.2: the negative list is asserted on the assembled prompt."""
    for plan in plate_plans(story):
        prompt = plan.prompt.lower()
        for word in ("lettering", "signage", "caption", "logo", "no people", "no crowd"):
            assert word in prompt, (story.key, plan.key, word)
        assert "silhouette" in prompt


@pytest.mark.parametrize("story", SEEDS, ids=lambda seed: seed.key)
def test_a_plate_prompt_names_nobody_and_carries_no_prose(story: StorySeed) -> None:
    """A plate is generic by construction: no name, no prose, no cast look."""
    for plan in plate_plans(story):
        assert "{" not in plan.prompt and "}" not in plan.prompt, plan.key
        assert "the child in the red hood" not in plan.prompt.lower()
    for page in story.pages:
        prompt = build_plate_prompt(story, page.scene)
        assert page.story not in prompt
        assert story.cast[story.star].look[:60] not in prompt


def test_all_the_plates_share_one_palette_and_one_style() -> None:
    """T5.3: 21 plates read as one book; only the place and light vary."""
    plans = plate_plans(RED)
    palettes = {
        line for plan in plans for line in plan.prompt.splitlines() if "ONE PALETTE" in line
    }
    styles = {
        line
        for plan in plans
        for line in plan.prompt.splitlines()
        if line.startswith("ART STYLE")
    }
    assert len(palettes) == 1
    assert len(styles) == 1


@pytest.mark.parametrize("story", SEEDS, ids=lambda seed: seed.key)
def test_every_plate_has_one_slot_per_character_in_the_art_box(story: StorySeed) -> None:
    """T5.4's input: the plate's slots are exactly the page's cast."""
    pages = {page.number: page.scene.cast for page in story.pages}
    for plan in plate_plans(story):
        expected = (
            page_cast(story, story.cover.cast)
            if plan.page_number == COVER_PAGE
            else page_cast(story, pages[plan.page_number])
        )
        assert tuple(slot.cast_key for slot in plan.slots) == expected
        for slot in plan.slots:
            x, y, w, h = slot.rect
            assert min(x, y) >= 0, plan.key
            assert x + w <= 1 and y + h <= 1, plan.key
            assert 0 < slot.scale <= 1
            # The cover's top 22 % is the title band; a face never sits there.
            assert y >= 0.22, plan.key
            assert slot.facing in {"left", "right", "toward", "away"}


def test_only_the_cover_is_told_to_keep_its_title_band() -> None:
    """The title is set over the cover afterwards, so only the cover reserves it."""
    plans = plate_plans(RED)
    assert "THE TITLE BAND" in plans[0].prompt
    for plan in plans[1:]:
        assert "THE TITLE BAND" not in plan.prompt, plan.key


def test_a_placeholder_in_the_camera_never_reaches_a_plate() -> None:
    """A plate's framing is the empty stage's, so it names nobody either."""
    scene = replace(
        RED.pages[0].scene, camera="two-shot at {name}'s eye level, both faces clear"
    )
    with pytest.raises(PlateError, match="camera"):
        build_plate_prompt(RED, scene)


def test_a_page_crowded_past_the_layout_is_refused() -> None:
    with pytest.raises(PlateError, match="no slot layout"):
        slots_for([f"char-{index}" for index in range(MAX_PLATE_CAST + 1)])


def test_a_story_with_no_prompt_block_is_refused() -> None:
    unknown = replace(RED, key="no-such-story")
    with pytest.raises(PlateError, match=r"stories\.no-such-story"):
        plate_plans(unknown)


def test_a_placeholder_in_a_scene_never_reaches_a_plate() -> None:
    page = replace(
        RED.pages[0], scene=replace(RED.pages[0].scene, environment="{name}'s lane")
    )
    with pytest.raises(PlateError, match="placeholder"):
        build_plate_prompt(RED, page.scene)


# --- the command ---------------------------------------------------------------


@pytest.fixture
def store(engine) -> TemplateStore:
    template_store = TemplateStore(engine)
    template_store.seed([RED])
    return template_store


def test_the_command_reports_the_calls_before_making_them(
    store: TemplateStore, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = RecordingProvider()
    written = make_plates.run(story=RED, store=store, provider=provider)

    assert len(written) == PAGE_COUNT + 1
    assert len(provider.requests) == PAGE_COUNT + 1
    first = capsys.readouterr().out.splitlines()[0]
    assert f"{PAGE_COUNT + 1} image call(s)" in first
    assert f"{PAGE_COUNT + 1} plate(s) x 1 candidate(s)" in first


def test_the_command_writes_drafts_with_their_prompt_and_slots(
    store: TemplateStore,
) -> None:
    provider = RecordingProvider()
    written = make_plates.run(story=RED, store=store, provider=provider)

    for plate_id, plan in zip(written, plate_plans(RED), strict=True):
        assert store.plate_status(plate_id) == "draft"
        image, media_type = store.plate_image(plate_id)
        assert image == tiny_png_bytes()
        assert media_type == "image/png"
        # The stored prompt is the one that was sent, byte for byte.
        assert plan.prompt in {request.prompt for request in provider.requests}


def test_two_candidates_cost_two_calls_per_plate(store: TemplateStore) -> None:
    provider = RecordingProvider()
    make_plates.run(story=RED, store=store, provider=provider, candidates=2)
    assert len(provider.requests) == (PAGE_COUNT + 1) * 2


def test_the_painted_plates_can_all_be_approved(store: TemplateStore) -> None:
    """T5.4 end to end: exactly one slot per cast member, so approval passes."""
    make_plates.run(story=RED, store=store, provider=RecordingProvider())
    for plan in plate_plans(RED):
        store.approve_plate(_plate_for(store, plan.page_number), operator="ops")
    # The 21st approval is the one that completes a version (T4).
    version = store.current_version(RED.key)
    assert version is not None
    assert len(version.pages) == PAGE_COUNT + 1


def _plate_for(store: TemplateStore, page_number: int) -> str:
    """The id of the newest plate for a page, as the admin screen would pick it."""
    from sqlalchemy import select

    from app.db import TemplatePlateRow

    with store._engine.connect() as conn:  # type: ignore[attr-defined]
        return conn.execute(
            select(TemplatePlateRow.id)
            .where(
                TemplatePlateRow.story_key == RED.key,
                TemplatePlateRow.page_number == page_number,
            )
            .order_by(TemplatePlateRow.created_at.desc())
            .limit(1)
        ).scalar_one()


def test_the_contact_sheet_is_written_in_page_order(
    store: TemplateStore, tmp_path: Path
) -> None:
    path = tmp_path / "sheets" / "red.png"
    make_plates.run(story=RED, store=store, provider=RecordingProvider(), contact_sheet=path)
    assert path.is_file()
    with Image.open(io.BytesIO(path.read_bytes())) as sheet:
        assert sheet.width == make_plates._THUMB_WIDTH * make_plates._COLUMNS


# --- the paid-provider confirmation -------------------------------------------


def test_a_paid_provider_needs_a_typed_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = PaidProvider()
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert make_plates._confirm(provider, 21, assume_yes=False) is False

    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert make_plates._confirm(provider, 21, assume_yes=False) is True


def test_a_placeholder_provider_asks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_prompt: str) -> str:
        raise AssertionError("a free provider must not ask for confirmation")

    monkeypatch.setattr("builtins.input", refuse)
    assert make_plates._confirm(RecordingProvider(), 21, assume_yes=False) is True


def test_an_unknown_story_names_the_ones_that_exist() -> None:
    with pytest.raises(SystemExit, match="little_red_riding_hood"):
        make_plates.story_for("nope")
