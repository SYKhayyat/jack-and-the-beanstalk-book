"""Prompt construction is the product. These tests pin the language that keeps
identity and art-direction faithful."""

from __future__ import annotations

import pytest

from app.models import CartoonSettings, CartoonStyle
from app.models.settings import (
    ApparentAge,
    CharacterGender,
    LikenessStrength,
    PhotoGender,
    SkinTone,
)
from app.services.prompts import build_cartoon_prompt


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


class TestCharacterGender:
    def test_boy_settings_name_the_character_jack(self) -> None:
        prompt = build_cartoon_prompt(CartoonSettings(character_gender=CharacterGender.BOY))
        assert "Jack" in prompt
        assert "Jane" not in prompt

    def test_girl_settings_name_the_character_jane(self) -> None:
        prompt = build_cartoon_prompt(CartoonSettings(character_gender=CharacterGender.GIRL))
        assert "Jane" in prompt
        assert "Jack" not in prompt

    def test_match_defers_to_the_stated_photo_gender(self) -> None:
        settings = CartoonSettings(
            character_gender=CharacterGender.MATCH, photo_gender=PhotoGender.GIRL
        )
        prompt = build_cartoon_prompt(settings)
        assert "Jane" in prompt
        assert "Jack" not in prompt

    def test_pronouns_follow_the_name(self) -> None:
        girl = build_cartoon_prompt(CartoonSettings(character_gender=CharacterGender.GIRL))
        boy = build_cartoon_prompt(CartoonSettings(character_gender=CharacterGender.BOY))
        assert " her " in girl
        assert " her " not in boy


class TestPromptHygiene:
    @pytest.mark.parametrize(
        "settings",
        [
            CartoonSettings(),
            CartoonSettings(cartoon_intensity=10, cartoon_style=CartoonStyle.ANIME),
            CartoonSettings(cartoon_intensity=1, likeness_strength=LikenessStrength.LOOSE),
        ],
        ids=["default", "max-style", "min-style"],
    )
    def test_prompts_have_no_broken_whitespace(self, settings: CartoonSettings) -> None:
        prompt = build_cartoon_prompt(settings)
        assert "\n\n\n" not in prompt
        assert not any(line != line.strip() for line in prompt.splitlines())
        assert "None" not in prompt
