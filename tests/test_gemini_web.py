"""Gemini in a local Chrome as the free backend: no real browser is started here.

The browser's own behaviour was verified by hand against gemini.google.com;
these tests pin what the app does with it: the job waits (never fails) while a
person or the clock is needed, every backend sends what the paid ones send,
and a local book costs nothing.
"""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.config import ProviderName, Settings
from app.services.costs import MeteredProvider, image_call_price, review_call_price
from app.services.decision_judge import LIKENESS, QUESTIONS, GeminiWebDecisionJudge
from app.services.gemini_web.attachment import Attachment
from app.services.gemini_web.client import GeminiWeb, GeminiWebConfig, Paused, PauseReason
from app.services.gemini_web.factory import client_for
from app.services.image_providers.base import ImageRequest, ProviderError, ProviderUnavailable
from app.services.image_providers.gemini_web_provider import GeminiWebImageProvider
from app.services.image_providers.registry import build_provider
from app.services.reviewer import GeminiWebReviewer

ACCOUNT = "owner@example.com"


def _png(size: tuple[int, int] = (64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 120, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (20, 120, 40)).save(buffer, format="JPEG")
    return buffer.getvalue()


class FakeClient:
    """Records each job and answers from a script."""

    def __init__(self, answer: str | bytes = "") -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, tuple[Attachment, ...]]] = []

    def ask(self, prompt: str, images: Sequence[Attachment] = ()) -> str:
        self.calls.append(("ask", prompt, tuple(images)))
        assert isinstance(self.answer, str)
        return self.answer

    def draw(self, prompt: str, images: Sequence[Attachment] = ()) -> bytes:
        self.calls.append(("draw", prompt, tuple(images)))
        assert isinstance(self.answer, bytes)
        return self.answer


# --- pause and resume ----------------------------------------------------------


class ScriptedWeb(GeminiWeb):
    """The real job loop, with the browser steps replaced by a script."""

    def __init__(self, outcomes: list[BaseException | str], **config: Any) -> None:
        self.sleeps: list[float] = []
        self.notes: list[str] = []
        super().__init__(
            GeminiWebConfig(account=ACCOUNT, **config),
            sleep=self.sleeps.append,
            notify=self.notes.append,
        )
        self._outcomes = outcomes
        self.chats = 0

    def _ready_session(self) -> Any:
        return object()

    def _open_chat(self, session: Any) -> None:
        self.chats += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome

    def _ask(self, session: Any, prompt: str, images: Sequence[Attachment]) -> str:
        return f"answer to {prompt}"


class TestAJobWaitsForWhatAPersonOrTheClockFixes:
    def test_signed_out_waits_and_then_runs_the_same_job(self) -> None:
        web = ScriptedWeb(
            [Paused(PauseReason.SIGNED_OUT), Paused(PauseReason.SIGNED_OUT), "ok"],
            poll_seconds=7,
        )

        assert web.ask("hello") == "answer to hello"
        assert web.chats == 3
        assert web.sleeps == [7, 7]
        # One note per spell, not one per check.
        assert len(web.notes) == 1
        assert "signed out" in web.notes[0]

    def test_a_reached_limit_waits_the_longer_limit_wait(self) -> None:
        web = ScriptedWeb(
            [Paused(PauseReason.LIMIT), Paused(PauseReason.WRONG_ACCOUNT), "ok"],
            poll_seconds=5,
            limit_wait_seconds=900,
        )

        assert web.ask("x") == "answer to x"
        assert web.sleeps == [900, 5]
        assert len(web.notes) == 2

    def test_a_real_failure_is_not_waited_out(self) -> None:
        web = ScriptedWeb([ProviderError("the page changed")])

        with pytest.raises(ProviderError, match="the page changed"):
            web.ask("x")
        assert web.sleeps == []

    def test_no_account_configured_is_refused_up_front(self) -> None:
        with pytest.raises(ProviderError, match="GEMINI_WEB_ACCOUNT"):
            GeminiWeb(GeminiWebConfig(account=" "))


class FakeSession:
    """Answers the page checks `_open_chat` makes, from a dict of fragments."""

    def __init__(self, state: str, account_label: str) -> None:
        self.state = state
        self.account_label = account_label
        self.sent: list[str] = []

    def send(
        self, method: str, params: Any = None, *, timeout: float = 60.0
    ) -> dict[str, Any]:
        self.sent.append(method)
        return {}

    def evaluate(
        self, expression: str, *, timeout: float = 60.0, gesture: bool = False
    ) -> Any:
        if "Google Account" in expression:
            return self.account_label
        return None

    def wait_for(self, expression: str, *, timeout: float, interval: float = 0.5) -> Any:
        if "signed_out" in expression:
            return self.state
        return True


class TestTheAccountIsCheckedBeforeEveryJob:
    def _web(self) -> GeminiWeb:
        return GeminiWeb(GeminiWebConfig(account=ACCOUNT), sleep=lambda _: None)

    def test_the_configured_account_opens_a_temporary_chat(self) -> None:
        session = FakeSession("ready", f"Google Account: Owner ({ACCOUNT.upper()})")

        self._web()._open_chat(session)  # type: ignore[arg-type]

        assert session.sent == ["Page.navigate"]

    def test_another_account_pauses_and_shows_the_tab(self) -> None:
        session = FakeSession("ready", "Google Account: Student (student@school.example)")

        with pytest.raises(Paused) as paused:
            self._web()._open_chat(session)  # type: ignore[arg-type]

        assert paused.value.reason == PauseReason.WRONG_ACCOUNT
        assert ACCOUNT in str(paused.value)
        assert "Page.bringToFront" in session.sent

    def test_signed_out_pauses(self) -> None:
        session = FakeSession("signed_out", "")

        with pytest.raises(Paused) as paused:
            self._web()._open_chat(session)  # type: ignore[arg-type]

        assert paused.value.reason == PauseReason.SIGNED_OUT


class AnsweringWeb(GeminiWeb):
    """`_draw`/`_ask` for real, with the page's final answer scripted."""

    def __init__(self, answer: dict[str, Any]) -> None:
        super().__init__(GeminiWebConfig(account=ACCOUNT), sleep=lambda _: None)
        self.answer = answer

    def _send(self, session: Any, prompt: str, images: Sequence[Attachment]) -> int:
        return 0

    def _finished_answer(self, session: Any, before: int) -> dict[str, Any]:
        return self.answer


class TestWhatGeminiAnswersInsteadOfAPicture:
    def test_a_limit_message_is_a_pause(self) -> None:
        web = AnsweringWeb(
            {
                "text": "You've reached your limit for creating images. Come back tomorrow.",
                "picture": False,
            }
        )
        with pytest.raises(Paused) as paused:
            web._draw(None, "draw", ())  # type: ignore[arg-type]
        assert paused.value.reason == PauseReason.LIMIT

    def test_any_other_answer_without_a_picture_is_a_failed_attempt(self) -> None:
        web = AnsweringWeb({"text": "I can't help with that one.", "picture": False})
        with pytest.raises(ProviderError, match="without a picture"):
            web._draw(None, "draw", ())  # type: ignore[arg-type]

    def test_a_text_answer_that_carries_json_is_never_mistaken_for_a_limit(self) -> None:
        text = '{"passed": true, "score": 90, "note": "try again later is written nowhere"}'
        assert AnsweringWeb({"text": text})._ask(None, "q", ()) == text  # type: ignore[arg-type]


# --- the three backends ----------------------------------------------------------


class TestTheImageProvider:
    def test_sends_the_prompt_and_every_reference_with_its_real_type(self) -> None:
        picture = _jpeg()
        client = FakeClient(picture)
        provider = GeminiWebImageProvider(client)  # type: ignore[arg-type]
        png = _png()

        image = provider.generate(
            ImageRequest(prompt="Jack on the stalk", reference_images=(png, picture))
        )

        assert image.data == picture
        assert image.media_type == "image/jpeg"
        kind, prompt, images = client.calls[0]
        assert kind == "draw"
        assert prompt.endswith("Jack on the stalk")
        assert "4:3" in prompt
        assert [i.media_type for i in images] == ["image/png", "image/jpeg"]

    def test_the_registry_builds_it_from_the_settings(self, tmp_path: Path) -> None:
        settings = Settings(
            _env_file=None,
            image_provider="gemini_web",
            gemini_web_account=ACCOUNT,
            storage_dir=tmp_path,
        )
        assert isinstance(build_provider(settings), GeminiWebImageProvider)
        assert settings.active_model == "gemini-web"

    def test_no_account_is_an_unavailable_provider(self, tmp_path: Path) -> None:
        settings = Settings(_env_file=None, image_provider="gemini_web", storage_dir=tmp_path)
        with pytest.raises(ProviderUnavailable, match="GEMINI_WEB_ACCOUNT"):
            build_provider(settings)

    def test_every_backend_shares_one_browser(self, tmp_path: Path) -> None:
        settings = Settings(_env_file=None, gemini_web_account=ACCOUNT, storage_dir=tmp_path)
        assert client_for(settings) is client_for(settings)


class TestTheReviewer:
    def test_is_shown_the_picture_plate_and_portrait_in_a_stated_order(self) -> None:
        client = FakeClient('Sure!\n```json\n{"passed": true, "score": 88, "faults": []}\n```')
        reviewer = GeminiWebReviewer(client)  # type: ignore[arg-type]

        answer = reviewer.review(
            _png(),
            media_type="image/png",
            page_title="The beans",
            story_line="Jack plants the beans",
            cast="Jack only",
            plate=_png(),
            portrait=_png(),
        )

        assert answer.passed and answer.score == 88
        _, prompt, images = client.calls[0]
        assert len(images) == 3
        assert all(i.media_type == "image/jpeg" for i in images)
        assert (
            "first, the picture to judge; second, the empty background plate; last," in prompt
        )
        assert "Jack plants the beans" in prompt

    def test_without_a_plate_or_portrait_it_sees_one_picture(self) -> None:
        client = FakeClient('{"passed": false, "score": 30, "faults": ["extra_person"]}')
        answer = GeminiWebReviewer(client).review(  # type: ignore[arg-type]
            _png(), media_type="image/png", page_title="t", story_line="s", cast="c"
        )
        assert answer.faults == ("extra_person",)
        _, prompt, images = client.calls[0]
        assert len(images) == 1
        assert "background plate" not in prompt

    def test_describes_the_portraits_hair_in_one_clean_sentence(self) -> None:
        client = FakeClient('"Short, curly\n brown hair"')
        hair = GeminiWebReviewer(client).describe_hair(_png())  # type: ignore[arg-type]
        assert hair == "Short, curly brown hair."
        _, _, images = client.calls[0]
        assert len(images) == 1


class TestTheDecisionJudge:
    def _answer(self, names: Sequence[str], value: float = 0.1) -> str:
        return "JSON\n" + json.dumps(dict.fromkeys(names, value) | {"shows_moment": 0.9})

    def test_reads_a_probability_for_every_question_asked(self) -> None:
        asked = [name for name in QUESTIONS if name != LIKENESS]
        client = FakeClient(self._answer(asked))

        decision = GeminiWebDecisionJudge(client).decide(  # type: ignore[arg-type]
            _png(), brief="the child plants beans", cast="the child only"
        )

        assert set(decision.probabilities) == set(asked)
        assert decision.outcome == "accept"
        assert decision.input_tokens == 0
        _, prompt, images = client.calls[0]
        assert len(images) == 1
        assert f"{LIKENESS}:" not in prompt

    def test_the_portrait_brings_the_likeness_question(self) -> None:
        client = FakeClient(self._answer(list(QUESTIONS)))

        decision = GeminiWebDecisionJudge(client).decide(  # type: ignore[arg-type]
            _png(), brief="b", cast="c", portrait=_png()
        )

        assert LIKENESS in decision.probabilities
        _, prompt, images = client.calls[0]
        assert len(images) == 2
        assert f"{LIKENESS}:" in prompt

    @pytest.mark.parametrize(
        "answer",
        ["no json here", '{"shows_moment": 0.9}', '{"shows_moment": "high"}'],
    )
    def test_an_incomplete_answer_is_a_failed_judgement(self, answer: str) -> None:
        with pytest.raises(ProviderError):
            GeminiWebDecisionJudge(FakeClient(answer)).decide(  # type: ignore[arg-type]
                _png(), brief="b", cast="c"
            )


# --- settings and prices -----------------------------------------------------------


class TestALocalBookIsFree:
    def _settings(self, tmp_path: Path, **overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "image_provider": "gemini_web",
            "reviewer_provider": "gemini_web",
            "decision_judge_provider": "gemini_web",
            "gemini_web_account": ACCOUNT,
            "openai_api_key": "",
            "storage_dir": tmp_path,
        }
        return Settings(_env_file=None, **(values | overrides))

    def test_the_judge_needs_no_openai_key(self, tmp_path: Path) -> None:
        settings = self._settings(tmp_path)
        assert settings.decision_judge_enabled
        assert settings.uses_gemini_web
        assert settings.image_provider is ProviderName.GEMINI_WEB

    def test_the_openai_judge_still_needs_its_key(self, tmp_path: Path) -> None:
        assert not self._settings(
            tmp_path, decision_judge_provider="openai"
        ).decision_judge_enabled

    def test_pictures_and_reviews_cost_nothing(self, tmp_path: Path) -> None:
        settings = self._settings(tmp_path, review_call_cost_usd=0.01)
        assert image_call_price(settings).usd == 0.0
        assert review_call_price(settings).usd == 0.0

    def test_a_page_may_wait_a_day_for_the_browser(self, tmp_path: Path) -> None:
        settings = self._settings(tmp_path, generation_timeout_seconds=24 * 3600)
        assert settings.generation_timeout_seconds == 86400


def test_the_container_wires_every_backend_to_gemini_web(tmp_path: Path) -> None:
    from app.container import build_container

    settings = Settings(
        _env_file=None,
        image_provider="gemini_web",
        reviewer_provider="gemini_web",
        decision_judge_provider="gemini_web",
        gemini_web_account=ACCOUNT,
        openai_api_key="",
        database_url="",
        storage_dir=tmp_path,
    )
    container = build_container(settings)

    assert isinstance(container.provider, MeteredProvider)
    assert isinstance(container.provider.inner, GeminiWebImageProvider)
    assert container.book_runner.reviewer_name == "gemini_web"
    assert container.book_runner.decision_judge_name == "gemini-web-decisions"


def test_a_blank_profile_dir_means_the_default_profile(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, gemini_web_profile_dir="", storage_dir=tmp_path)
    assert settings.gemini_web_profile_dir.name == "gemini-chrome"
    assert settings.gemini_web_profile_dir.is_absolute()
