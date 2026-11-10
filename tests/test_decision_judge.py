"""The decision model's first look (JEV-style): accept when confident, escalate when unsure.

The probability vectors here are real answers from OpenAI's Decisions API on
real pages (2026-11-09): a clean page of an OpenAI book drawn on plates, an
unsure one, and an off-story contact sheet from an Agnes book.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import pytest

from app.services.decision_judge import (
    QUESTIONS,
    Decision,
    Outcome,
    _decision,
    classify,
)
from app.services.image_providers.base import ProviderError
from app.services.reviewer import ReviewerQuotaSpent
from tests.test_review_loop import ScriptedReviewer, artwork, grain, runner_with, said

#: Page 1 of the plated OpenAI book: the mother hands over the basket.
GOOD = {
    "shows_moment": 0.95,
    "extra_person": 0.0,
    "missing_character": 0.0,
    "text_in_image": 0.0,
    "deformed": 0.05,
    "collage": 0.0,
    "frightening": 0.0,
}
#: Page 7 of the same book: the wolf should be running ahead; unclear.
UNSURE = {**GOOD, "shows_moment": 0.43, "deformed": 0.24}
#: Page 13 of the Agnes book: a grid of tiles with numbers on them.
BAD = {
    "shows_moment": 0.0,
    "extra_person": 0.84,
    "missing_character": 1.0,
    "text_in_image": 1.0,
    "deformed": 0.04,
    "collage": 1.0,
    "frightening": 0.0,
}


@dataclass
class ScriptedJudge:
    name = "scripted-decisions"
    answers: list[Any] = field(default_factory=list)
    calls: int = 0
    portraits: list[bytes | None] = field(default_factory=list)

    def decide(
        self, image: bytes, *, brief: str, cast: str, portrait: bytes | None = None
    ) -> Decision:
        self.calls += 1
        self.portraits.append(portrait)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return Decision(probabilities=answer, input_tokens=2073, model="gpt-6-luna")


def judge(reviewer: Any, decisions: list[Any]) -> tuple[Any, ScriptedJudge]:
    runner = runner_with(reviewer)
    scripted = ScriptedJudge(answers=decisions)
    runner._decision_judge = scripted
    verdict = asyncio.run(
        runner._judge(
            book_id="book",
            page_number=7,
            image=grain(),
            plate=None,
            artwork=artwork(),
            page_title="t",
            story_line="the wolf runs ahead",
            cast_note="c",
            star_in_picture=True,
            attempt=1,
            user="-",
        )
    )
    return verdict, scripted


class TestTheThresholds:
    @pytest.mark.parametrize(
        ("probabilities", "outcome"),
        [(GOOD, Outcome.ACCEPT), (UNSURE, Outcome.UNSURE), (BAD, Outcome.REJECT)],
    )
    def test_real_answers_land_where_they_should(
        self, probabilities: dict[str, float], outcome: Outcome
    ) -> None:
        assert classify(probabilities) is outcome

    def test_one_near_certain_fault_rejects_a_page_that_shows_the_moment(self) -> None:
        assert classify({**GOOD, "text_in_image": 0.97}) is Outcome.REJECT

    def test_a_merely_likely_fault_is_unsure_not_accepted(self) -> None:
        assert classify({**GOOD, "extra_person": 0.4}) is Outcome.UNSURE

    def test_the_score_is_the_moment_docked_by_the_worst_fault(self) -> None:
        assert Decision(probabilities=GOOD).score == 90
        assert Decision(probabilities=BAD).score == 0
        assert Decision(probabilities=BAD).faults()[:1] == ("wrong_moment",)


class TestTheCascade:
    def test_a_confidently_good_picture_costs_no_review(self) -> None:
        reviewer = ScriptedReviewer(answers=[])
        verdict, scripted = judge(reviewer, [GOOD])
        assert verdict.passed is True
        assert scripted.calls == 1
        assert reviewer.calls == 0
        assert verdict.judged_by == "decision"
        assert verdict.as_dict()["decision"]["outcome"] == "accept"
        assert verdict.tiers_run == (1, 2)

    def test_a_confident_rejection_does_not_overrule_the_reviewer(self) -> None:
        """Real pages: the decision rejected two the reviewer rightly passed."""
        reviewer = ScriptedReviewer(answers=[said(True, 95)])
        verdict, _ = judge(reviewer, [BAD])
        assert reviewer.calls == 1
        assert verdict.passed is True and verdict.judged_by == "reviewer"
        assert verdict.as_dict()["decision"]["outcome"] == "reject"  # still recorded

    def test_a_confidently_bad_picture_gets_the_reviewer_s_words_for_the_redraw(
        self,
    ) -> None:
        note = "the picture is a grid of twelve small tiles"
        reviewer = ScriptedReviewer(answers=[said(False, 5, ("collage",), note)])
        verdict, _ = judge(reviewer, [BAD])
        assert verdict.passed is False and verdict.note == note

    def test_an_unsure_picture_is_the_reviewer_s_call(self) -> None:
        verdict, _ = judge(ScriptedReviewer(answers=[said(True, 92)]), [UNSURE])
        assert verdict.passed is True
        assert verdict.judged_by == "reviewer"
        failed, _ = judge(
            ScriptedReviewer(answers=[said(False, 40, ("wrong_moment",), "standing")]),
            [UNSURE],
        )
        assert failed.passed is False and failed.faults == ("wrong_moment",)

    def test_when_the_judge_is_down_the_reviewer_decides(self) -> None:
        verdict, _ = judge(
            ScriptedReviewer(answers=[said(True, 90)]), [ProviderError("beta is down")]
        )
        assert verdict.passed is True and verdict.judged_by == "reviewer"
        assert "decision" not in verdict.as_dict()

    def test_when_the_reviewer_is_down_the_judge_decides_alone(self) -> None:
        down = RuntimeError("503")
        unsure, _ = judge(ScriptedReviewer(answers=[down]), [UNSURE])
        assert unsure.passed is False  # the moment is under halfway
        assert unsure.judged_by == "decision" and not unsure.tier2_failed
        bad, _ = judge(ScriptedReviewer(answers=[down]), [BAD])
        assert bad.passed is False and "collage" in bad.faults

    def test_a_spent_free_quota_rests_the_reviewer_and_the_judge_decides(self) -> None:
        """The free tier used up: asked once, then left alone until the reset."""
        until = datetime.now(UTC) + timedelta(hours=3)
        reviewer = ScriptedReviewer(
            answers=[ReviewerQuotaSpent("spent", until=until)], default=said(True, 90)
        )
        runner = runner_with(reviewer)
        runner._decision_judge = ScriptedJudge(answers=[UNSURE, GOOD, BAD])

        def one_page() -> Any:
            return asyncio.run(
                runner._judge(
                    book_id="book",
                    page_number=7,
                    image=grain(),
                    plate=None,
                    artwork=artwork(),
                    page_title="t",
                    story_line="the wolf runs ahead",
                    cast_note="c",
                    star_in_picture=True,
                    attempt=1,
                    user="-",
                )
            )

        verdicts = [one_page(), one_page(), one_page()]
        assert reviewer.calls == 1
        assert runner.reviewer_resting_until == until
        assert [v.judged_by for v in verdicts] == ["decision"] * 3
        assert [v.passed for v in verdicts] == [False, True, False]
        assert not any(v.tier2_failed for v in verdicts)

        runner._reviewer_rests_until = datetime.now(UTC) - timedelta(seconds=1)
        runner._decision_judge = ScriptedJudge(answers=[UNSURE])
        assert one_page().judged_by == "reviewer"  # the quota reset: asked again
        assert runner.reviewer_resting_until is None

    def test_when_both_are_down_only_tier_one_ran_and_it_says_so(self) -> None:
        verdict, _ = judge(
            ScriptedReviewer(answers=[RuntimeError("503")]), [ProviderError("down")]
        )
        assert verdict.tier2_failed is True
        assert verdict.tiers_run == (1,)


class TestLikeness:
    """The approved portrait drew the child, so it is what the child must look like."""

    def _judged(self, *, star_in_picture: bool) -> tuple[ScriptedReviewer, ScriptedJudge]:
        reviewer = ScriptedReviewer(answers=[said(True, 90)])
        runner = runner_with(reviewer)
        scripted = ScriptedJudge(answers=[UNSURE])
        runner._decision_judge = scripted
        art = artwork()
        art.portrait = b"the-approved-portrait"
        asyncio.run(
            runner._judge(
                book_id="book",
                page_number=7,
                image=grain(),
                plate=None,
                artwork=art,
                page_title="t",
                story_line="s",
                cast_note="c",
                star_in_picture=star_in_picture,
                attempt=1,
                user="-",
            )
        )
        return reviewer, scripted

    def test_a_page_with_the_child_sends_the_portrait_to_both_checks(self) -> None:
        reviewer, scripted = self._judged(star_in_picture=True)
        assert scripted.portraits == [b"the-approved-portrait"]
        assert reviewer.portraits == [b"the-approved-portrait"]

    def test_a_page_without_the_child_sends_no_portrait(self) -> None:
        reviewer, scripted = self._judged(star_in_picture=False)
        assert scripted.portraits == [None]
        assert reviewer.portraits == [None]

    def test_the_likeness_question_is_asked_only_with_a_portrait(self) -> None:
        from app.services.decision_judge import OpenAIDecisionJudge

        sent: list[dict[str, Any]] = []

        class Decisions:
            def create(self, **kwargs: Any) -> Any:
                sent.append(kwargs)
                raise RuntimeError("stop here")

        judge_ = OpenAIDecisionJudge("test-key-not-real", model="m")
        judge_._client = Mock(decisions=Decisions())
        for portrait in (None, grain()):
            with pytest.raises(ProviderError):
                judge_.decide(grain(), brief="b", cast="c", portrait=portrait)
        without, with_ = ({q["name"] for q in call["questions"]} for call in sent)
        assert "likeness" not in without and "likeness" in with_
        assert len(sent[0]["input"][0]["content"]) == 2
        assert len(sent[1]["input"][0]["content"]) == 4


class TestReadingTheAnswer:
    def test_every_question_must_come_back_with_a_probability(self) -> None:
        answers = [
            {"type": "predicate", "name": name, "probability": 0.1} for name in QUESTIONS
        ]
        decision = _decision(
            {"answers": answers, "usage": {"input_tokens": 2073}}, model="gpt-6-luna"
        )
        assert decision.input_tokens == 2073
        assert set(decision.probabilities) == set(QUESTIONS)

        answers[2] = {"type": "refusal", "name": answers[2]["name"]}
        with pytest.raises(ProviderError, match="refusal"):
            _decision({"answers": answers}, model="gpt-6-luna")

    def test_the_metered_judge_bills_the_tokens_it_used(self) -> None:
        from app.services.costs import MeteredDecisionJudge

        recorded: list[dict[str, Any]] = []

        class Ledger:
            def record(self, **row: Any) -> None:
                recorded.append(row)

        metered = MeteredDecisionJudge(ScriptedJudge(answers=[GOOD]), Ledger(), 0.10)  # type: ignore[arg-type]
        metered.decide(b"", brief="b", cast="c")
        assert recorded[0]["cost_microusd"] == round(2073 * 0.10)
        assert recorded[0]["provider"] == "decisions" and recorded[0]["ok"] is True
