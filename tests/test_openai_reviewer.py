"""The OpenAI picture reviewer: request shaping and wiring, no network.

Real SDK, fake transport, in the style of test_openai_provider.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from app.config import Settings
from app.services.costs import review_call_price
from app.services.image_providers.base import ProviderError
from app.services.reviewer import OpenAIReviewer, build_reviewer
from tests.helpers import tiny_png_bytes

PNG = tiny_png_bytes()
GOOD = json.dumps({"passed": True, "score": 9, "faults": [], "note": "fine"})


@dataclass
class FakeMessage:
    content: str


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeCompletion:
    choices: list[FakeChoice]


class FakeCompletions:
    def __init__(self, answer: str) -> None:
        self._answer = answer
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeCompletion:
        self.calls.append(kwargs)
        return FakeCompletion([FakeChoice(FakeMessage(self._answer))])


@dataclass
class FakeChat:
    completions: FakeCompletions


@dataclass
class FakeClient:
    chat: FakeChat


def reviewer(
    answer: str = GOOD, model: str = "gpt-6-luna"
) -> tuple[OpenAIReviewer, FakeCompletions]:
    completions = FakeCompletions(answer)
    built = OpenAIReviewer(api_key="test-key-not-real", model=model)
    built._client = FakeClient(FakeChat(completions))  # type: ignore[assignment]
    return built, completions


def review(built: OpenAIReviewer, plate: bytes | None = None) -> Any:
    return built.review(
        PNG, media_type="image/png", page_title="p", story_line="s", cast="c", plate=plate
    )


class TestWiring:
    def test_the_shipped_default_is_openai_luna(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.reviewer_provider == "openai"
        assert settings.reviewer_model == "gpt-6-luna"

    def test_the_builder_returns_the_openai_reviewer(self) -> None:
        built = build_reviewer(
            api_key="k", model="gpt-6-luna", required=True, provider="openai"
        )
        assert isinstance(built, OpenAIReviewer)

    def test_the_default_model_is_luna(self) -> None:
        assert OpenAIReviewer(api_key="k")._model == "gpt-6-luna"


class TestReasoning:
    def test_the_gpt_6_family_reviews_with_no_reasoning(self) -> None:
        assert OpenAIReviewer("k", model="gpt-6-luna")._reasoning_kwargs() == {
            "reasoning_effort": "none"
        }

    def test_older_models_are_left_alone(self) -> None:
        assert OpenAIReviewer("k", model="gpt-5-nano")._reasoning_kwargs() == {}

    def test_the_request_carries_the_setting(self) -> None:
        built, completions = reviewer()
        review(built)
        assert completions.calls[0]["reasoning_effort"] == "none"
        assert completions.calls[0]["model"] == "gpt-6-luna"


class TestTheRequest:
    def images_sent(self, completions: FakeCompletions) -> int:
        content = completions.calls[0]["messages"][0]["content"]
        return sum(1 for part in content if part["type"] == "image_url")

    def test_one_image_without_a_plate(self) -> None:
        built, completions = reviewer()
        review(built)
        assert self.images_sent(completions) == 1

    def test_the_plate_goes_as_a_second_image(self) -> None:
        built, completions = reviewer()
        review(built, plate=PNG)
        assert self.images_sent(completions) == 2

    def test_a_good_answer_is_read(self) -> None:
        built, _ = reviewer()
        answer = review(built)
        assert answer.passed and answer.score == 9

    def test_a_malformed_answer_is_an_error_and_is_not_asked_again(self) -> None:
        built, completions = reviewer(answer="no json here")
        with pytest.raises(ProviderError):
            review(built)
        assert len(completions.calls) == 1


class TestCost:
    def test_the_review_price_follows_the_setting(self) -> None:
        settings = Settings(_env_file=None, review_call_cost_usd=0.0011)
        assert review_call_price(settings).usd == 0.0011

    def test_no_setting_means_unpriced(self) -> None:
        assert review_call_price(Settings(_env_file=None)).usd is None
