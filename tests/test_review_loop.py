"""The three-try quality loop and the two tiers that drive it (T9, T10).

The loop's contract, tested here: a page is drawn at most three times; a retry
only happens when a review actually failed; a retry names the faults but never
touches the plate; the best attempt is the one kept, with the earliest winning a
tie; a page that never passes is kept anyway and marked best-effort; and a
reviewer outage records itself without killing the book.
"""

from __future__ import annotations

import asyncio
import io
import json
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from PIL import Image, ImageDraw

from app.models.settings import CartoonSettings
from app.prompt_data import prompt_text
from app.services.book_runner import MAX_PAGE_ATTEMPTS, BookRunner
from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
    ProviderError,
)
from app.services.prompts import build_plate_edit_prompt
from app.services.reviewer import (
    GeminiReviewer,
    ReviewAnswer,
    ReviewerQuotaSpent,
    ReviewerUnavailable,
    UnavailableReviewer,
    Verdict,
    build_reviewer,
    next_quota_reset,
    parse_answer,
    verdict_passes,
)
from app.services.template_store import PlateArt, Slot

from .helpers import edition_for

#: How many plates a story has: the cover plus its story pages.
PLAN_COUNT = 21

JACK = edition_for("jack_and_the_beanstalk")

#: One slot as a plate would declare it: a cast key, a rect as fractions of the
#: art box, a height and a facing. Copied here so the loop tests own their data.
A_SLOT = Slot(cast_key="mother", rect=(0.1, 0.4, 0.3, 0.5), scale=0.5, facing="right")


class ABigPainter(ImageProvider):
    """Paints what a real edit looks like: the plate, with someone in it.

    Distinct from `RecordingProvider` in two ways that matter to the loop: its
    pictures are big enough for tier 1 (`too_small` otherwise), and they *keep
    the plate's background* (`background_redrawn` otherwise) while differing
    from it by enough that they are not the untouched plate (`plate_unchanged`).
    Without both, no page could ever reach tier 2 and the loop's retry path
    would be untestable.
    """

    name = "big"
    draws_placeholders = False

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[ImageRequest] = []
        # Painted once: each attempt differs, and every one is a small change to
        # the same background, which is what the plate edit pass produces.
        self._pictures = [a_plate_with_someone_in_it(shift) for shift in range(0, 7)]

    def health_check(self) -> tuple[bool, str]:
        return True, "big painter"

    def generate(self, request: ImageRequest) -> GeneratedImage:
        self.calls += 1
        self.requests.append(request)
        return GeneratedImage(
            data=self._pictures[self.calls % len(self._pictures)],
            media_type="image/png",
            model="big",
        )


# --- pictures the two tiers will actually accept --------------------------------


def grain(colour: tuple[int, int, int] = (47, 125, 79), seed: int = 0) -> bytes:
    """A 4:3 PNG of coloured noise: big enough, varied enough, deterministic.

    Tier 1 rejects a flat frame as blank and an 8x8 stub as too_small, so the
    loop's tests need something that looks like a picture. Noise does, and the
    seed makes each attempt's picture distinguishable.
    """
    rng = random.Random(seed)
    image = Image.new("RGB", (512, 384), colour)
    pixels = image.load()
    for y in range(384):
        for x in range(512):
            pixels[x, y] = tuple(
                max(0, min(255, channel + rng.randint(-45, 45))) for channel in colour
            )
    return _png(image)


#: The colour the test plates are painted: `_write_plates` fills every one with
#: this, so a painted page that keeps the background has to be close to it.
PLATE_COLOUR = (120, 150, 190)


def a_plate_with_someone_in_it(shift: int = 0) -> bytes:
    """A frame that keeps the plate's background and puts a figure in a corner.

    This is the shape of a good edit: most of the frame untouched, one patch
    changed. `shift` moves the figure so two attempts are different pictures.
    """
    image = Image.new("RGB", (1152, 864), PLATE_COLOUR)
    draw = ImageDraw.Draw(image)
    x = 40 + shift * 12
    draw.ellipse((x, 380, x + 260, 780), fill=(60, 45, 35))
    return _png(image)


def plate(blob: bool = False) -> bytes:
    """A plate. With `blob`, something stands in it, so an edit can be close to it."""
    image = Image.open(io.BytesIO(grain((110, 90, 60), seed=9))).convert("RGB")
    if blob:
        ImageDraw.Draw(image).ellipse((160, 120, 340, 280), fill=(30, 30, 30))
    return _png(image)


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --- the stand-ins ---------------------------------------------------------------


@dataclass
class ScriptedReviewer:
    """A reviewer whose first answers are queued, then a default if one is given.

    Without a default, asking for more reviews than were scripted raises: a test
    that wanted to prove a retry must fail loudly rather than pass quietly with
    the retry swallowed. With a default, a whole book can be reviewed and only
    the scripted pages behave differently.
    """

    name = "gemini"
    answers: list[Any] = field(default_factory=list)
    default: Any = None
    calls: int = 0

    def review(self, image: bytes, **_: Any) -> Any:
        self.calls += 1
        if not self.answers:
            if self.default is None:
                raise AssertionError("the loop asked for more reviews than were scripted")
            return self.default
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def said(
    passed: bool, score: int, faults: tuple[str, ...] = (), note: str = ""
) -> ReviewAnswer:
    return ReviewAnswer(passed=passed, score=score, faults=faults, note=note)


#: Everything a page's placeholders need: the name, and the pronouns the story's
#: prose is written with. Read from the same table the prompts use, so a test
#: page is filled exactly as a real one is.
_SUBS = {"name": "Jack", **dict(prompt_text()["pronouns"]["boy"])}


def a_page() -> Any:
    """One real page of the Jack edition, with the child's name filled in."""
    return JACK.pages[5].formatted(_SUBS)


def artwork(plated: bool = False) -> Any:
    """The runner's view of a book: with a plate for page 5, or without one."""
    plates = {}
    if plated:
        plates[5] = PlateArt(
            plate_id="p5",
            image=plate(),
            media_type="image/png",
            slots=(A_SLOT,),
        )
    return Mock(
        plates=plates,
        characters={},
        settings=CartoonSettings(aspect_ratio="4:3"),
    )


def runner_with(reviewer: Any, attempts: int = MAX_PAGE_ATTEMPTS) -> BookRunner:
    """A runner carrying only what the loop touches, built without its stores."""
    runner = BookRunner.__new__(BookRunner)
    runner._reviewer = reviewer
    runner._page_attempts = attempts
    runner._provider = Mock(name="provider")
    runner._provider.name = "big-painter"
    runner._decision_judge = None
    runner._reviewer_rests_until = None
    return runner


def attempt_loop(runner: BookRunner, draws: list[bytes] | None = None, **overrides: Any):
    """Run one page through the loop with scripted pictures, and return it."""
    images = draws or [a_plate_with_someone_in_it(shift) for shift in range(3)]

    class Script:
        """Hands out the next picture per call, so attempts are distinguishable."""

        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, _prompt: Any) -> GeneratedImage:
            image = images[self.calls]
            self.calls += 1
            return GeneratedImage(data=image, media_type="image/png", model="painter-v2")

    draw = Script()
    arguments = {
        "book_id": "book",
        "page_number": 5,
        "user": "-",
        "page_title": "the kitchen",
        "story_line": "the mother sends Jack to market",
        "cast_note": "Only these people may be in the picture: the child, and the mother.",
        "plate": None,
        "artwork": artwork(),
        "draw": draw,
        "prompt_for": lambda _faults, _findings: "the prompt",
    }
    arguments.update(overrides)
    result = asyncio.run(runner._draw_until_pass(**arguments))
    return result, draw, images


# --- the loop --------------------------------------------------------------------


class TestTheAttemptLoop:
    def test_a_passing_first_try_is_not_redrawn(self) -> None:
        result, draw, images = attempt_loop(
            runner_with(ScriptedReviewer(answers=[said(True, 95, note="good")])),
            artwork=artwork(plated=True),
        )
        assert draw.calls == 1
        assert result.image == images[0]
        assert result.prompt == "the prompt"
        assert result.chosen == 1 and result.best_effort is False
        assert result.verdicts[0]["passed"] is True
        assert result.verdicts[0]["tiers_run"] == [1, 2]

    def test_a_failing_try_is_redrawn_until_it_passes(self) -> None:
        reviewer = ScriptedReviewer(
            answers=[
                said(False, 40, ("text_in_image",), "there was lettering"),
                said(True, 88),
            ]
        )
        result, draw, images = attempt_loop(
            runner_with(reviewer),
            artwork=artwork(plated=True),
            # A real builder puts the faults into the retry's text.
            prompt_for=lambda faults, _findings: f"prompt {faults}",
        )
        assert draw.calls == 2
        assert reviewer.calls == 2
        assert [verdict["attempt"] for verdict in result.verdicts] == [1, 2]
        assert result.chosen == 2 and result.image == images[1]
        assert result.best_effort is False
        # One prompt per attempt, and the retry's text is not the first's: the
        # record is what was actually sent, not one text repeated.
        assert result.prompts == ("prompt ()", "prompt ('text_in_image',)")
        assert result.prompt == result.prompts[1]

    def test_the_retry_is_aimed_at_the_faults(self) -> None:
        """A retry that re-sends the same prompt is a dice roll, not a fix."""
        runner = runner_with(ScriptedReviewer(answers=[said(False, 30, ("deformed",))]))
        asked: list[tuple[str, ...]] = []
        attempt_loop(
            runner,
            artwork=artwork(plated=True),
            prompt_for=lambda faults, _findings: asked.append(tuple(faults)) or "p",
        )
        assert asked[0] == ()  # nothing was wrong yet
        assert asked[1] == ("deformed",)

    def test_a_page_that_never_passes_keeps_its_best_and_says_best_effort(self) -> None:
        reviewer = ScriptedReviewer(
            answers=[
                said(False, 25, ("wrong_moment",)),
                said(False, 60, ("text_in_image",)),
                said(False, 30, ("deformed",)),
            ]
        )
        result, draw, images = attempt_loop(
            runner_with(reviewer), artwork=artwork(plated=True)
        )
        assert draw.calls == MAX_PAGE_ATTEMPTS == 3
        assert len(result.verdicts) == 3
        assert result.best_effort is True
        assert result.chosen == 2 and result.image == images[1]  # the 60, not the last

    def test_the_earliest_attempt_wins_a_tie(self) -> None:
        """An equally good later attempt cost money and buys nothing."""
        reviewer = ScriptedReviewer(
            answers=[
                said(False, 50, ("deformed",)),
                said(False, 50, ("wrong_moment",)),
                said(False, 50, ("text_in_image",)),
            ]
        )
        result, _, images = attempt_loop(runner_with(reviewer), artwork=artwork(plated=True))
        assert result.chosen == 1 and result.image == images[0]

    def test_a_reviewer_outage_is_recorded_and_the_page_is_kept(self) -> None:
        reviewer = ScriptedReviewer(answers=[RuntimeError("the reviewer is down")])
        result, draw, images = attempt_loop(
            runner_with(reviewer), artwork=artwork(plated=True)
        )
        # A reviewer outage must not cost a parent a finished book (T10.2).
        assert draw.calls == 1
        assert result.verdicts[0]["tier2_failed"] is True
        assert result.verdicts[0]["tiers_run"] == [1]
        assert result.verdicts[0]["passed"] is True
        assert result.chosen == 1 and result.best_effort is False
        assert result.image == images[0]

    def test_a_tier_one_failure_keeps_a_verdict_but_is_redrawn(self) -> None:
        """A blank frame is caught for free, and re-drawn without paying a reviewer."""
        reviewer = ScriptedReviewer(answers=[said(True, 90)])
        blank = _png(Image.new("RGB", (512, 384), (255, 255, 255)))
        result, draw, _ = attempt_loop(
            runner_with(reviewer),
            draws=[blank, grain(seed=3)],
            plate=None,
            artwork=artwork(plated=True),
        )
        assert draw.calls == 2
        assert reviewer.calls == 1  # the blank frame never reached tier 2
        assert "blank" in result.verdicts[0]["faults"]
        assert result.chosen == 2

    def test_a_picture_drawn_without_a_plate_is_reviewed_by_both_tiers(self) -> None:
        """A composed page is where a weak model goes most wrong: never waved through."""
        reviewer = ScriptedReviewer(answers=[said(False, 20, ("wrong_moment",), "a park")])
        verdict = asyncio.run(
            runner_with(reviewer)._judge(
                book_id="book",
                page_number=5,
                image=grain(),
                plate=None,
                artwork=artwork(plated=False),
                page_title="t",
                story_line="s",
                cast_note="c",
                attempt=1,
                user="-",
            )
        )
        assert reviewer.calls == 1
        assert verdict.tiers_run == (1, 2)
        assert verdict.passed is False
        assert verdict.faults == ("wrong_moment",)

    def test_a_named_fault_fails_the_picture_whatever_the_model_says(self) -> None:
        """ "Passed, score 90, but there is lettering in the sky" is not a pass."""
        reviewer = ScriptedReviewer(answers=[said(True, 90, ("text_in_image",))])
        verdict = asyncio.run(
            runner_with(reviewer)._judge(
                book_id="book",
                page_number=5,
                image=grain(),
                plate=None,
                artwork=artwork(),
                page_title="t",
                story_line="s",
                cast_note="c",
                attempt=1,
                user="-",
            )
        )
        assert verdict.passed is False

    def test_a_small_detail_alone_does_not_fail_a_good_picture(self) -> None:
        """ "Three cups, not two" failed a good book (real run): recorded, not failed."""
        reviewer = ScriptedReviewer(answers=[said(False, 90, ("wrong_counts",), "three cups")])
        verdict = asyncio.run(
            runner_with(reviewer)._judge(
                book_id="book",
                page_number=5,
                image=grain(),
                plate=None,
                artwork=artwork(),
                page_title="t",
                story_line="s",
                cast_note="c",
                attempt=1,
                user="-",
            )
        )
        assert verdict.passed is True
        assert verdict.faults == ("wrong_counts",)  # still on the record

    @pytest.mark.parametrize(
        ("score", "faults", "passes"),
        [
            (90, (), True),
            (69, (), False),
            (90, ("other",), True),
            (60, ("other",), False),
            (95, ("extra_person",), False),
            (95, ("wrong_counts", "deformed"), False),
        ],
    )
    def test_the_pass_rule(self, score: int, faults: tuple[str, ...], passes: bool) -> None:
        assert verdict_passes(score, faults) is passes

    def test_someone_the_description_names_is_not_an_extra(self) -> None:
        """The wolf slipping away in the background belongs, though he has no slot."""
        from app.services.book_runner import _cast_note

        red = edition_for("little_red_riding_hood")
        flowers = next(page for page in red.pages if page.number == 6)
        note = _cast_note(flowers, red)
        assert "anyone the description above names" in note
        assert note.startswith("Who may appear in this picture: the child")
        assert "may be absent" in note

    def test_a_contact_sheet_fails_tier_one_for_free(self) -> None:
        """A grid of tiles is caught before a reviewer is paid to look at it."""
        reviewer = ScriptedReviewer(answers=[said(True, 95)])
        sheet = Image.new("RGB", (1152, 864), (240, 240, 240))
        draw = ImageDraw.Draw(sheet)
        rng = random.Random(4)
        for row in range(3):
            for column in range(4):
                left, top = 8 + column * 286, 8 + row * 286
                draw.rectangle(
                    (left, top, left + 276, top + 276),
                    fill=tuple(rng.randrange(30, 200) for _ in range(3)),
                )
        result, _, _ = attempt_loop(
            runner_with(reviewer),
            draws=[_png(sheet), grain(seed=5)],
            artwork=artwork(),
        )
        assert "collage" in result.verdicts[0]["faults"]
        assert reviewer.calls == 1  # only the second picture reached tier 2
        assert result.chosen == 2

    def test_the_reviewer_s_own_words_reach_the_retry(self) -> None:
        """The fault kind says what is wrong; the note says which thing, and where."""
        note = "a second girl in a red cloak stands at the far right"
        reviewer = ScriptedReviewer(
            answers=[said(False, 30, ("extra_person",), note), said(True, 90)]
        )
        seen: list[tuple[tuple[str, ...], str]] = []
        attempt_loop(
            runner_with(reviewer),
            prompt_for=lambda faults, findings: seen.append((tuple(faults), findings)) or "p",
        )
        assert seen == [((), ""), (("extra_person",), note)]

    def test_a_tier_one_failure_sends_no_findings(self) -> None:
        """Tier 1 has no words of its own: the fault name is all there is."""
        blank = _png(Image.new("RGB", (512, 384), (255, 255, 255)))
        seen: list[str] = []
        attempt_loop(
            runner_with(ScriptedReviewer(answers=[said(True, 90)])),
            draws=[blank, grain(seed=3)],
            prompt_for=lambda _faults, findings: seen.append(findings) or "p",
        )
        assert seen == ["", ""]

    def test_what_the_owner_asked_for_steers_every_attempt_and_is_kept(self) -> None:
        reviewer = ScriptedReviewer(
            answers=[
                said(False, 40, ("deformed",), "the left hand has six fingers"),
                said(True, 90),
            ]
        )
        asked: list[tuple[str, ...]] = []
        result, _, _ = attempt_loop(
            runner_with(reviewer),
            prompt_for=lambda faults, _findings: asked.append(tuple(faults)) or "p",
            requested=("likeness",),
        )
        assert asked == [("likeness",), ("deformed", "likeness")]
        assert all(verdict["requested"] == ["likeness"] for verdict in result.verdicts)

    def test_every_attempt_records_what_drew_it(self) -> None:
        result, _, _ = attempt_loop(
            runner_with(
                ScriptedReviewer(answers=[said(False, 30, ("deformed",)), said(True, 90)])
            )
        )
        assert [(v["provider"], v["model"]) for v in result.verdicts] == [
            ("big-painter", "painter-v2"),
            ("big-painter", "painter-v2"),
        ]

    def test_a_placeholder_provider_is_never_retried(self) -> None:
        """Three identical placeholders would be three claimed attempts."""
        runner = BookRunner(
            provider=Mock(draws_placeholders=True),
            store=Mock(),
            book_store=Mock(),
            templates=Mock(),
            gate=Mock(),
            page_timeout_seconds=60,
            page_attempts=3,
        )
        assert runner._page_attempts == 1

    def test_the_retry_prompt_names_the_faults_but_protects_the_plate(self) -> None:
        retry = build_plate_edit_prompt(
            a_page(),
            (A_SLOT,),
            story=JACK,
            settings=CartoonSettings(),
            prompt_name="Jack",
            retry_faults=("text_in_image",),
            retry_findings='the word "MARKET" is\n painted on the cart',
        )
        assert "there was lettering or writing in the picture" in retry
        # The load-bearing sentence: a fault list reads as permission to redraw
        # unless it says in the same breath that the plate is not one of them.
        assert "must stay as they are in the plate" in retry
        # The reviewer's words, flattened to one line and quoted as data.
        assert "in its own words: \"the word 'MARKET' is painted on the cart\"" in retry
        first_try = build_plate_edit_prompt(
            a_page(), (A_SLOT,), story=JACK, settings=CartoonSettings(), prompt_name="Jack"
        )
        assert "another try" not in first_try
        assert "in its own words" not in first_try

    def test_the_edit_carries_the_moment_but_keeps_the_plate(self) -> None:
        """Without the moment every figure stood posed (real run, 2026-11-09)."""
        red = edition_for("little_red_riding_hood")
        page = next(page for page in red.pages if page.key == "red-arrives")
        prompt = build_plate_edit_prompt(
            page,
            (A_SLOT,),
            story=red,
            settings=CartoonSettings(photo_gender="girl"),
            prompt_name="Maya",
        )
        assert "THE MOMENT THIS PAGE SHOWS" in prompt
        assert "stay exactly as they are" in prompt  # said before the caption
        assert "Maya in the red hood steps through the half-open blue door" in prompt
        assert "WHAT Maya IS DOING: stepping in through the door" in prompt
        assert "the wolf clearly visible or showing teeth" in prompt  # must_avoid
        assert "{name}" not in prompt and "{subject}" not in prompt
        # Still none of the composition's instructions to draw the place.
        assert "THE WHOLE STORY" not in prompt
        assert prompt.index("KEEP THE PLATE") < prompt.index("THE MOMENT")

    def test_the_reviewer_is_told_the_picture_not_the_narration(self) -> None:
        """The story line names people off-frame; the caption is what to judge."""
        from app.services.book_runner import _picture_brief

        red = edition_for("little_red_riding_hood")
        page = next(page for page in red.pages if page.key == "red-arrives")
        brief = _picture_brief(page, CartoonSettings(photo_gender="girl"))
        assert brief.startswith("the child in the red hood steps through")
        assert "Maya" not in brief and "{name}" not in brief


# --- the reviewer ---------------------------------------------------------------


class TestTheReviewer:
    def test_a_busy_reviewer_is_asked_again_and_a_refusal_is_not(self) -> None:
        """429/503 clear in seconds and are not billed; a bad answer is never re-asked."""
        reviewer = GeminiReviewer("k", model="gemini-x")
        reviewer._sleep = lambda _seconds: None
        answers: list[Any] = [
            RuntimeError("503 UNAVAILABLE. high demand"),
            Mock(text='{"passed": true, "score": 90, "faults": [], "note": "ok"}'),
        ]
        calls: list[int] = []

        def generate_content(**_: Any) -> Any:
            calls.append(1)
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        reviewer._client = Mock()
        reviewer._client.models.generate_content = generate_content
        image = _png(Image.new("RGB", (8, 8), "white"))
        answer = reviewer.review(
            image, media_type="image/png", page_title="t", story_line="s", cast="c"
        )
        assert answer.passed and len(calls) == 2

        calls.clear()
        answers[:] = [RuntimeError("400 INVALID_ARGUMENT")]
        with pytest.raises(ProviderError):
            reviewer.review(
                image, media_type="image/png", page_title="t", story_line="s", cast="c"
            )
        assert len(calls) == 1

    def test_a_used_up_daily_quota_is_not_retried(self) -> None:
        """Retrying a per-day free-tier refusal cannot succeed; it is reported at once."""
        reviewer = GeminiReviewer("k", model="gemini-x")
        pauses: list[Any] = []
        reviewer._sleep = pauses.append
        calls: list[int] = []

        def generate_content(**_: Any) -> Any:
            calls.append(1)
            raise RuntimeError(
                "429 RESOURCE_EXHAUSTED. Quota exceeded, quotaId: "
                "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
            )

        reviewer._client = Mock()
        reviewer._client.models.generate_content = generate_content
        with pytest.raises(ReviewerQuotaSpent) as spent:
            reviewer.review(
                _png(Image.new("RGB", (8, 8), "white")),
                media_type="image/png",
                page_title="t",
                story_line="s",
                cast="c",
            )
        assert len(calls) == 1 and pauses == []
        assert spent.value.until > datetime.now(UTC)

    def test_the_quota_rests_until_the_next_midnight_pacific(self) -> None:
        evening = datetime(2026, 11, 10, 20, 30, tzinfo=UTC)
        assert next_quota_reset(evening) == datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
        early = datetime(2026, 11, 10, 7, 59, tzinfo=UTC)
        assert next_quota_reset(early) == datetime(2026, 11, 10, 8, 0, tzinfo=UTC)

    def test_a_reviewer_that_stays_busy_gives_up(self) -> None:
        reviewer = GeminiReviewer("k", model="gemini-x")
        reviewer._sleep = lambda _seconds: None
        calls: list[int] = []

        def generate_content(**_: Any) -> Any:
            calls.append(1)
            raise RuntimeError("503 UNAVAILABLE")

        reviewer._client = Mock()
        reviewer._client.models.generate_content = generate_content
        with pytest.raises(ProviderError):
            reviewer.review(
                _png(Image.new("RGB", (8, 8), "white")),
                media_type="image/png",
                page_title="t",
                story_line="s",
                cast="c",
            )
        assert len(calls) == 2  # the first try plus one bounded retry

    def test_a_strict_json_answer_is_read(self) -> None:
        answer = parse_answer('{"passed": true, "score": 80, "faults": [], "note": "fine"}')
        assert answer.passed is True
        assert answer.score == 80
        assert answer.faults == ()

    def test_an_answer_wrapped_in_prose_is_still_read(self) -> None:
        """A chatty model is not wrong, so its JSON is found inside the prose."""
        answer = parse_answer(
            'Here is my verdict.\n{"passed": false, "score": 12, '
            '"faults": ["text_in_image"], "note": "x"}\nThat is all.'
        )
        assert answer.passed is False
        assert answer.faults == ("text_in_image",)

    def test_a_fault_name_nobody_knows_is_kept_as_other(self) -> None:
        """An objection the app has no name for still fails the picture.

        Dropping it let a picture the model objected to pass on its score
        alone; it is stored as "other", with the model's word kept in the note.
        """
        answer = parse_answer(
            '{"passed": true, "score": 90, "faults": ["looks_silly", "deformed"], '
            '"note": "the cow has two heads"}'
        )
        assert answer.faults == ("other", "deformed")
        assert answer.note == "[looks_silly] the cow has two heads"

    def test_a_collage_is_a_fault_the_reviewer_can_name(self) -> None:
        answer = parse_answer(
            '{"passed": false, "score": 10, "faults": ["collage"], "note": "a grid"}'
        )
        assert answer.faults == ("collage",)

    def test_an_impossible_score_is_clamped(self) -> None:
        assert parse_answer('{"passed": true, "score": -10, "faults": []}').score == 0
        assert parse_answer('{"passed": true, "score": 400, "faults": []}').score == 100

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "no JSON here at all",
            "[]",
            '{"passed": "yes", "score": 90}',
            '{"score": 90}',
            '{"passed": true, "faults": []}',
        ],
    )
    def test_an_answer_without_the_shape_fails_loudly(self, text: str) -> None:
        """A default-pass here would ship unreviewed pictures and look fine."""
        with pytest.raises(ProviderError):
            parse_answer(text)

    def test_a_verdict_reports_the_highest_tier_that_ran(self) -> None:
        both = Verdict(
            attempt=1, passed=False, score=55, faults=("deformed",), tiers_run=(1, 2)
        )
        assert both.as_dict()["tier"] == 2
        only_first = Verdict(attempt=1, passed=True, score=100, tiers_run=(1,))
        assert only_first.as_dict()["tier"] == 1

    def test_a_missing_key_is_a_start_up_error_when_the_reviewer_is_required(self) -> None:
        with pytest.raises(ReviewerUnavailable):
            build_reviewer(api_key="", model="gemini-x", required=True)

    def test_without_a_key_the_reviewer_is_honestly_off(self) -> None:
        assert build_reviewer(api_key="", model="x", required=False).name == "off"
        assert UnavailableReviewer().name == "off"

    def test_a_key_builds_a_real_reviewer(self) -> None:
        assert isinstance(
            build_reviewer(api_key="k", model="gemini-x", required=True), GeminiReviewer
        )

    def test_the_real_prompt_reaches_the_model_and_its_answer_comes_back(self) -> None:
        """The shipped instruction, filled in, sent through the Gemini reviewer.

        The instruction carries a JSON example, and it is filled with
        `str.format`; an unescaped brace there raised before any call was made,
        and the runner recorded every review as "tier 2 did not run".
        """
        reviewer = GeminiReviewer("k", model="gemini-x")
        sent: dict[str, Any] = {}

        def generate_content(*, model: str, contents: list[Any]) -> Any:
            sent["model"] = model
            sent["text"] = contents[1].text
            return Mock(text='{"passed": true, "score": 90, "faults": [], "note": "ok"}')

        reviewer._client = Mock()
        reviewer._client.models.generate_content = generate_content

        answer = reviewer.review(
            _png(Image.new("RGB", (8, 8), "white")),
            media_type="image/png",
            page_title="The Beanstalk",
            story_line="Jack climbs into the clouds.",
            cast="Jack is in this picture.",
        )

        assert answer.passed and answer.score == 90
        assert sent["model"] == "gemini-x"
        assert "Jack climbs into the clouds." in sent["text"]
        assert '{"passed": true, "score": 0' in sent["text"]
        assert "The picture it must be: Jack climbs into the clouds." in sent["text"]

    def test_a_production_deployment_needs_a_reviewer_but_mock_mode_does_not(self) -> None:
        from app.config import Settings

        chosen: dict[str, Any] = {"reviewer_provider": "gemini", "_env_file": None}
        assert Settings(
            app_env="production", image_provider="gemini", **chosen
        ).reviewer_required
        assert not Settings(
            app_env="development", image_provider="gemini", **chosen
        ).reviewer_required
        assert not Settings(app_env="production", **chosen).reviewer_required

    def test_a_reviewer_turned_off_is_never_required_and_never_built(self) -> None:
        from app.config import Settings

        chosen: dict[str, Any] = {
            "app_env": "production",
            "image_provider": "openai",
            "reviewer_provider": "off",
            "_env_file": None,
        }
        settings = Settings(**chosen)
        assert not settings.reviewer_required
        built = build_reviewer(api_key="k", model="m", required=True, provider="off")
        assert isinstance(built, UnavailableReviewer)


# --- the loop, through the real HTTP flow --------------------------------------


class TestTheLoopEndToEnd:
    """The whole path, through `/api/book/start`, with a reviewer that can be
    made to fail.

    These approve the real plates first, so the book is drawn by the edit
    pass. The claims are the book-level ones: a page that fails is redrawn, and
    a reviewer that is down never costs a parent the finished book.
    """

    def _prompts(self, tmp_path: Path) -> list[dict[str, str]]:
        """Read the prompt record from the database the book was written to."""
        from sqlalchemy import create_engine, text

        # The conftest puts the database under `storage/`, not in tmp_path itself.
        engine = create_engine(f"sqlite:///{tmp_path / 'storage' / 'app.sqlite3'}")
        with engine.connect() as conn:
            row = conn.execute(text("select prompts from books")).first()
        engine.dispose()
        assert row is not None and row[0]
        # SQLite hands JSON back as text; Postgres would hand back a list.
        stored = row[0] if isinstance(row[0], list) else json.loads(row[0])
        return list(stored)

    def _a_book(
        self, settings: Any, tmp_path: Path, provider: Any, reviewer: Any
    ) -> dict[str, Any]:
        from fastapi.testclient import TestClient

        from app.main import create_app
        from tests.test_plate_import_and_edit import _approved_plates, _start_a_book

        app = create_app(settings, provider=provider, reviewer=reviewer)
        with TestClient(app) as client:
            store = client.app.state.container.templates  # type: ignore[attr-defined]
            _approved_plates(store, tmp_path)
            book_id = _start_a_book(client)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                payload = client.get(f"/api/book/{book_id}").json()
                if payload["status"] in {"complete", "failed"}:
                    return dict(payload)
                time.sleep(0.05)
            raise AssertionError("the book never settled")

    def test_a_reviewer_outage_never_costs_a_parent_the_book(
        self, settings: Any, tmp_path: Path
    ) -> None:
        class AlwaysDown:
            name = "down"

            def review(self, *_: Any, **__: Any) -> Any:
                raise RuntimeError("the reviewer is down")

        payload = self._a_book(settings, tmp_path, ABigPainter(), AlwaysDown())
        assert payload["status"] == "complete", payload.get("error")
        assert payload["pages"][0]["status"] == "done"

    def test_a_failing_picture_is_redrawn_and_the_book_still_completes(
        self, settings: Any, tmp_path: Path
    ) -> None:
        provider = ABigPainter()
        # The first page reviewed fails its check; every page after it passes.
        reviewer = ScriptedReviewer(
            answers=[said(False, 20, ("text_in_image",))], default=said(True, 95)
        )
        payload = self._a_book(settings, tmp_path, provider, reviewer)
        assert payload["status"] == "complete", payload.get("error")

        # One extra picture was paid for, and the provider saw it: a sheet of
        # 21 plates is 22 calls, and a retry makes 23.
        assert len(provider.requests) == PLAN_COUNT + 2, len(provider.requests)

        # The prompt record holds one entry per attempt, so the retry is visible
        # as a page with two entries — and the second one names the fault,
        # because a retry that re-sends the same text is a dice roll.
        prompts = self._prompts(tmp_path)
        per_page: dict[str, list[str]] = {}
        for entry in prompts:
            per_page.setdefault(str(entry["page"]), []).append(entry["prompt"])
        retried = {page: texts for page, texts in per_page.items() if len(texts) > 1}
        assert len(retried) == 1, retried.keys()
        first, second = next(iter(retried.values()))
        assert first != second
        assert "there was lettering or writing in the picture" in second
