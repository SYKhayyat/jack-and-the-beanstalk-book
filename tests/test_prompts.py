"""Prompt construction is the product. These tests pin the language that makes
the two graded requirements hold: identity and scene fidelity."""

from __future__ import annotations

import pytest

from app.models import CartoonSettings, CartoonStyle, Scene, get_scene
from app.models.settings import (
    ApparentAge,
    CharacterGender,
    LikenessStrength,
    PhotoGender,
    SkinTone,
)
from app.services.prompts import (
    build_cartoon_prompt,
    build_scene_prompt,
    render_scene,
)

PLANTING = get_scene("planting-seeds")
CLIMBING = get_scene("climbing-beanstalk")


class TestSceneCatalogue:
    def test_both_required_scenes_exist(self) -> None:
        assert PLANTING is not None
        assert CLIMBING is not None

    def test_scene_keys_are_unique(self) -> None:
        from app.models import SCENES

        keys = [scene.key for scene in SCENES]
        assert len(keys) == len(set(keys))

    def test_unknown_scene_returns_none(self) -> None:
        assert get_scene("giant-chases-jack") is None

    @pytest.mark.parametrize("scene", [PLANTING, CLIMBING], ids=lambda s: s.key)
    def test_each_scene_caption_names_its_action(self, scene: Scene) -> None:
        assert scene.caption
        assert scene.caption.endswith(".")
        assert scene.subject_action

    def test_planting_excludes_the_climbing_beat(self) -> None:
        # The two scenes must not contaminate each other: planting seeds is a
        # ground-level scene, so anything beanstalk-shaped is explicitly banned.
        assert any("beanstalk" in item for item in PLANTING.must_avoid)

    def test_climbing_excludes_the_planting_beat(self) -> None:
        assert any("planting" in item for item in CLIMBING.must_avoid)


class TestCartoonPrompt:
    def test_defaults_carry_identity_and_style(self) -> None:
        prompt = build_cartoon_prompt(CartoonSettings())
        assert "Preserve the identity" in prompt
        assert "western cartoon" in prompt.lower()
        assert "no writing in it" in prompt

    def test_no_text_artifacts_requested(self) -> None:
        prompt = build_cartoon_prompt(CartoonSettings())
        assert "Do not include any text" in prompt
        assert "speech bubble" in prompt

    def test_only_one_person_requested(self) -> None:
        prompt = build_cartoon_prompt(CartoonSettings())
        assert "only the one" in prompt

    @pytest.mark.parametrize("style", list(CartoonStyle), ids=lambda s: s.value)
    def test_every_style_has_distinct_language(self, style: CartoonStyle) -> None:
        prompt = build_cartoon_prompt(CartoonSettings(cartoon_style=style))
        expected_terms = {
            CartoonStyle.MANGA: "screentone",
            CartoonStyle.ANIME: "cel shading",
            CartoonStyle.WESTERN_CARTOON: "western cartoon",
            CartoonStyle.PIXAR_LIKE: "3d animated feature",
            CartoonStyle.DISNEY_LIKE: "hand-painted 2d",
            CartoonStyle.COMIC_BOOK: "comic book",
            CartoonStyle.PENCIL_SKETCH: "graphite pencil",
            CartoonStyle.WATERCOLOUR: "watercolour",
            CartoonStyle.VECTOR_FLAT: "flat vector",
        }
        assert expected_terms[style] in prompt.lower()

    def test_styles_are_not_all_identical(self) -> None:
        prompts = {
            build_cartoon_prompt(CartoonSettings(cartoon_style=style))
            for style in CartoonStyle
        }
        assert len(prompts) == len(CartoonStyle)

    @pytest.mark.parametrize("intensity", list(range(1, 11)))
    def test_every_intensity_has_guidance(self, intensity: int) -> None:
        prompt = build_cartoon_prompt(CartoonSettings(cartoon_intensity=intensity))
        assert "STYLISATION LEVEL" in prompt

    def test_low_and_high_intensity_differ_in_meaning(self) -> None:
        low = build_cartoon_prompt(CartoonSettings(cartoon_intensity=1))
        high = build_cartoon_prompt(CartoonSettings(cartoon_intensity=10))
        assert "almost intact" in low
        assert "maximally simplified" in high

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("apparent_age", ApparentAge.CHILD, "10 years old"),
            ("character_gender", CharacterGender.GIRL, "girl"),
            ("skin_tone", SkinTone.DEEP_BROWN, "deep brown"),
        ],
    )
    def test_identity_overrides_reach_the_prompt(
        self, field: str, value: object, expected: str
    ) -> None:
        prompt = build_cartoon_prompt(CartoonSettings(**{field: value}))
        assert expected in prompt

    def test_glasses_and_facial_hair_toggles(self) -> None:
        kept = build_cartoon_prompt(CartoonSettings(keep_glasses=True, keep_facial_hair=True))
        dropped = build_cartoon_prompt(
            CartoonSettings(keep_glasses=False, keep_facial_hair=False)
        )
        assert "keep those glasses" in kept
        assert "Remove any glasses" in dropped
        assert "Remove any facial hair" in dropped

    @pytest.mark.parametrize(
        ("strength", "expected"),
        [
            (LikenessStrength.STRICT, "Match the uploaded face"),
            (LikenessStrength.LOOSE, "letting the art style shape it"),
        ],
    )
    def test_likeness_strength_is_reflected(
        self, strength: LikenessStrength, expected: str
    ) -> None:
        prompt = build_cartoon_prompt(CartoonSettings(likeness_strength=strength))
        assert expected in prompt


class TestScenePrompt:
    def test_scene_caption_is_embedded_verbatim(self) -> None:
        # This is what makes the burned-in caption honest: the spec we send and
        # the caption we show are the same string. The catalogue is rendered for
        # the default character (a boy, so Jack) before it is embedded.
        settings = CartoonSettings()
        for scene in (PLANTING, CLIMBING):
            prompt = build_scene_prompt(scene, settings)
            assert render_scene(scene, settings).caption in prompt

    def test_subject_action_is_emphasised(self) -> None:
        prompt = build_scene_prompt(PLANTING, CartoonSettings())
        action = render_scene(PLANTING, CartoonSettings()).subject_action
        assert "WHAT THE CHARACTER IS DOING" in prompt
        assert action.split(",")[0] in prompt

    def test_checklist_is_included(self) -> None:
        prompt = build_scene_prompt(CLIMBING, CartoonSettings())
        assert "must be clearly visible" in prompt
        for item in render_scene(CLIMBING, CartoonSettings()).must_include:
            assert item in prompt

    def test_avoidance_is_included(self) -> None:
        prompt = build_scene_prompt(PLANTING, CartoonSettings())
        assert "Do not show any of the following" in prompt

    def test_identity_is_restated_not_assumed(self) -> None:
        prompt = build_scene_prompt(CLIMBING, CartoonSettings())
        assert prompt.count("Preserve the identity") == 1
        assert "bone structure" in prompt

    def test_character_block_applies(self) -> None:
        settings = CartoonSettings(hair_color="auburn", outfit="adventurer")
        prompt = build_scene_prompt(PLANTING, settings)
        assert "auburn" in prompt.lower()
        assert "adventurer's tunic" in prompt

    def test_style_carries_into_the_scene(self) -> None:
        prompt = build_scene_prompt(
            PLANTING, CartoonSettings(cartoon_style=CartoonStyle.MANGA)
        )
        assert "manga" in prompt.lower()
        assert "screentone" in prompt.lower()

    def test_aspect_ratio_is_reflected(self) -> None:
        prompt = build_scene_prompt(CLIMBING, CartoonSettings(aspect_ratio="16:9"))
        assert "16:9" in prompt


class TestCharacterGender:
    def test_boy_settings_name_the_character_jack(self) -> None:
        prompt = build_scene_prompt(
            PLANTING, CartoonSettings(character_gender=CharacterGender.BOY)
        )
        assert "Jack" in prompt
        assert "Jane" not in prompt

    def test_girl_settings_name_the_character_jane(self) -> None:
        prompt = build_scene_prompt(
            PLANTING, CartoonSettings(character_gender=CharacterGender.GIRL)
        )
        assert "Jane" in prompt
        assert "Jack" not in prompt

    def test_match_defers_to_the_stated_photo_gender(self) -> None:
        settings = CartoonSettings(
            character_gender=CharacterGender.MATCH, photo_gender=PhotoGender.GIRL
        )
        prompt = build_scene_prompt(CLIMBING, settings)
        assert "Jane" in prompt
        assert "Jack" not in prompt

    def test_pronouns_follow_the_name(self) -> None:
        girl = build_scene_prompt(
            PLANTING, CartoonSettings(character_gender=CharacterGender.GIRL)
        )
        boy = build_scene_prompt(
            PLANTING, CartoonSettings(character_gender=CharacterGender.BOY)
        )
        assert " her " in girl
        assert " her " not in boy

    def test_caption_and_prompt_agree_on_the_name(self) -> None:
        settings = CartoonSettings(character_gender=CharacterGender.GIRL)
        scene = render_scene(PLANTING, settings)
        prompt = build_scene_prompt(PLANTING, settings)
        assert scene.caption in prompt
        assert scene.title.startswith("Jane")


class TestPromptHygiene:
    @pytest.mark.parametrize(
        ("scene", "settings"),
        [
            (PLANTING, CartoonSettings()),
            (CLIMBING, CartoonSettings()),
            (
                PLANTING,
                CartoonSettings(cartoon_intensity=10, cartoon_style=CartoonStyle.ANIME),
            ),
            (
                CLIMBING,
                CartoonSettings(cartoon_intensity=1, likeness_strength=LikenessStrength.LOOSE),
            ),
        ],
        ids=["planting-default", "climbing-default", "max-style", "min-style"],
    )
    def test_prompts_have_no_broken_whitespace(
        self, scene: Scene, settings: CartoonSettings
    ) -> None:
        for prompt in (
            build_cartoon_prompt(settings),
            build_scene_prompt(scene, settings),
        ):
            assert "\n\n\n" not in prompt
            assert not any(line != line.strip() for line in prompt.splitlines())
            assert "None" not in prompt
