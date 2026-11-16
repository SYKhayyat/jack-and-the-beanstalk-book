"""One likeness everywhere: the star's description and dial, and where figures are."""

from __future__ import annotations

import pytest

from app.models.settings import LikenessStrength
from app.prompt_data import prompt_text
from app.services.decision_judge import LIKENESS, QUESTIONS, _likeness_intro, _questions
from app.services.image_providers.base import ProviderError
from app.services.reviewer import StarLook, parse_figures


class TestTheReviewersLikenessRule:
    def test_it_carries_the_books_description(self) -> None:
        rule = StarLook("A girl of about six with a round face.").rule()
        assert "A girl of about six with a round face." in rule

    @pytest.mark.parametrize("strength", list(LikenessStrength))
    def test_it_holds_the_child_to_the_books_dial(self, strength: LikenessStrength) -> None:
        rule = StarLook("", strength).rule()
        assert prompt_text()["review"]["likeness_standard"][strength.value] in rule

    def test_the_dials_differ(self) -> None:
        rules = {StarLook("", s).rule() for s in LikenessStrength}
        assert len(rules) == len(LikenessStrength)


class TestTheJudgesLikenessQuestion:
    def test_no_portrait_no_likeness_question(self) -> None:
        assert LIKENESS not in _questions(False, StarLook())

    def test_strict_asks_the_strict_question(self) -> None:
        asked = _questions(True, StarLook("", LikenessStrength.STRICT))
        assert asked[LIKENESS] == prompt_text()["decision"]["likeness_standard"]["strict"]

    def test_without_a_book_dial_the_plain_question_is_asked(self) -> None:
        assert _questions(True, None)[LIKENESS] == QUESTIONS[LIKENESS]

    def test_the_intro_names_the_look(self) -> None:
        assert "with glasses" in _likeness_intro(StarLook("A boy with glasses."))
        assert "{" not in _likeness_intro(None)


class TestWhereTheFiguresAre:
    def test_boxes_become_fractional_rects(self) -> None:
        figures = parse_figures(
            'Here: {"figures": [{"what": "girl in red", "box": [100, 200, 300, 900]}]}'
        )
        assert len(figures) == 1
        assert figures[0].what == "girl in red"
        assert figures[0].rect == pytest.approx((0.1, 0.2, 0.2, 0.7))

    def test_swapped_and_overflowing_corners_are_set_right(self) -> None:
        (figure,) = parse_figures(
            '{"figures": [{"what": "wolf", "box": [800, 1200, 600, 500]}]}'
        )
        assert figure.rect == pytest.approx((0.6, 0.5, 0.2, 0.5))

    def test_bad_and_empty_boxes_are_dropped(self) -> None:
        figures = parse_figures(
            '{"figures": [{"what": "a", "box": [1, 2]}, {"what": "b", "box": [5, 5, 5, 9]},'
            ' {"what": "c", "box": "x"}, "junk"]}'
        )
        assert figures == ()

    def test_no_figures_list_is_an_error(self) -> None:
        with pytest.raises(ProviderError):
            parse_figures("I see a girl and a wolf.")
