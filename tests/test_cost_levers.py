"""The judging and prompt changes that bring a book's cost under its price.

Run 1 (2026-11-11) cost about $1.55 a book, nearly all of it redraws the
judges asked for over things a parent would never notice. Each test pins one
of the levers in to-do.md's "Cost per book under the $0.99 price".
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from app.models.settings import CartoonSettings
from app.services.book_runner import _confirmed_faults
from app.services.decision_judge import Decision
from app.services.prompts import _substitutions, findings_line
from app.services.reviewer import FAULTS, MINOR_FAULTS, verdict_passes

PROMPTS = tomllib.loads(Path("app/prompts.toml").read_text(encoding="utf-8"))


class TestLikenessNeedsBothJudges:
    """68 likeness faults in run 1 came from the reviewer alone, while the
    decision judge put them at 0.1-0.2. A likeness fail now needs both."""

    def test_the_reviewer_alone_cannot_fail_likeness(self) -> None:
        decision = Decision({"likeness": 0.15, "shows_moment": 0.6})
        assert _confirmed_faults(("likeness", "deformed"), decision) == ("deformed",)

    def test_both_judges_agreeing_keeps_the_fault(self) -> None:
        decision = Decision({"likeness": 0.7, "shows_moment": 0.6})
        assert _confirmed_faults(("likeness",), decision) == ("likeness",)

    def test_without_a_decision_the_reviewer_stands(self) -> None:
        assert _confirmed_faults(("likeness",), None) == ("likeness",)

    def test_a_page_with_only_a_disputed_likeness_passes(self) -> None:
        decision = Decision({"likeness": 0.1, "shows_moment": 0.6})
        assert verdict_passes(85, _confirmed_faults(("likeness",), decision))


def test_a_different_place_is_a_serious_fault() -> None:
    """Small plate differences are no longer faults; a different place is."""
    assert "wrong_place" in FAULTS
    assert "wrong_place" not in MINOR_FAULTS
    assert not verdict_passes(90, ("wrong_place",))
    assert "wrong_place" in PROMPTS["book"]["edit"]["fault_phrases"]


class TestFindingsAreCutCleanly:
    def test_a_long_note_ends_at_a_sentence(self) -> None:
        note = "The cow is missing. " * 30
        line = findings_line(note)
        assert line.endswith('missing."')
        assert "…" not in line

    def test_a_note_with_no_sentence_end_ends_at_a_word(self) -> None:
        line = findings_line("word " * 200)
        assert line.endswith('word…"')

    def test_a_short_note_is_kept_whole(self) -> None:
        assert findings_line('a second girl at the "far right"') == (
            "The reviewer saw: \"a second girl at the 'far right'\""
        )


class TestPicturePronouns:
    """Run 1 set "boy" on a girl's photo; "he/his" pulled the pages to a boy."""

    def test_under_match_the_picture_text_uses_the_name(self) -> None:
        subs = _substitutions(
            CartoonSettings(photo_gender="girl", character_gender="match"),
            "Maya",
            picture=True,
        )
        assert subs["subject"] == "Maya"
        assert subs["possessive"] == "Maya's"
        assert subs["Possessive"] == "Maya's"
        assert "she" not in subs.values() and "her" not in subs.values()

    def test_the_printed_page_keeps_real_pronouns(self) -> None:
        subs = _substitutions(
            CartoonSettings(photo_gender="girl", character_gender="match"), "Maya"
        )
        assert subs["subject"] == "she"
        assert subs["possessive"] == "her"

    def test_a_chosen_gender_still_reaches_the_picture(self) -> None:
        subs = _substitutions(
            CartoonSettings(photo_gender="girl", character_gender="boy"), "Sam", picture=True
        )
        assert subs["subject"] == "he"

    def test_the_unnamed_child_reads_naturally(self) -> None:
        subs = _substitutions(CartoonSettings(), "the child", picture=True)
        assert subs["possessive"] == "the child's"
        assert subs["Subject"] == "The child"


def test_both_judges_are_told_a_redraw_costs() -> None:
    """Told they may veto only 4 of 20, the judges save a fail for gross faults."""
    for text in (PROMPTS["review"]["instruction"], PROMPTS["decision"]["intro"]):
        assert "4 of this book's 20" in text
        assert "real cost" in text
