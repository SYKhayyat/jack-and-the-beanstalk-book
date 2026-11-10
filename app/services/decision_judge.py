"""The confident first look: a decision model judges every picture (JEV-style).

A decision model answers typed questions about an image with calibrated
probabilities instead of prose — the design of TypeSafe's JEV, and of OpenAI's
Decisions API (`POST /v1/decisions`), which this uses. It is cheap (about
2,000 input tokens per picture at $0.10 per million, nothing for output) and
quick (well under a second), so it looks first, and the generative reviewer
is only asked when it has something to add:

* **Confidently good** — the picture shows the moment and every fault is
  improbable. It passes; no reviewer call is spent on it.
* **Anything else** goes to the reviewer, whose verdict decides and whose
  words steer the redraw ("a second girl in a red cloak at the far right").
  A confident *rejection* does not overrule it: on real pages the decision
  model rejected two pictures the reviewer rightly called perfect.
* **The reviewer is down, or resting** (its free daily quota used up, until
  the quota resets) — the decision decides alone: a confident rejection
  fails, an unsure picture is judged at the halfway line.

"Accept when confident, escalate when unsure" is the policy the JEV-as-a-Judge
paper measures (arXiv 2609.26550). The thresholds below were set on real
pages: 21 pictures of an OpenAI book drawn on plates, and the contact sheets
and off-story scenes an Agnes book shipped with (UI.org Q1). Every bad Agnes
page scored "shows the moment" 0.00-0.15 with two or more faults near 1.0; the
good book's clean pages scored 0.75-0.97 with every fault under 0.25.

Like every backend, the model and key are the operator's alone: environment
only, never named on a visitor-facing surface.
"""

from __future__ import annotations

import base64
import io
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from PIL import Image

from app.services.image_providers.base import ProviderError

__all__ = [
    "QUESTIONS",
    "Decision",
    "DecisionJudge",
    "OpenAIDecisionJudge",
    "Outcome",
    "classify",
]

logger = logging.getLogger(__name__)

#: The question whose "yes" is good: does the picture show the page's moment.
SHOWS_MOMENT = "shows_moment"

#: One yes/no question per thing that can be wrong, keyed by the reviewer's own
#: fault names, so a decision and a review speak the same vocabulary. The
#: wording mirrors the review prompt's definitions, including what is NOT a
#: fault, because a decision model takes the question literally.
QUESTIONS: Mapping[str, str] = {
    SHOWS_MOMENT: (
        "Does the picture show the described moment: the right action and place, "
        "with the characters doing what the description says?"
    ),
    "extra_person": (
        "Is there a person or character in the picture whom neither the description "
        "nor the list of who may appear names?"
    ),
    "missing_character": (
        "Is someone the description says is in the picture missing from it?"
    ),
    "text_in_image": (
        "Is there any lettering, writing, sign, label, number or speech bubble "
        "anywhere in the picture?"
    ),
    "deformed": (
        "Does any figure have extra or missing limbs, extra fingers, fused hands, "
        "doubled feet, a body that reads as broken, a body or legs passing "
        "through a chair, table or other solid object, a figure squeezed under a "
        "table instead of beside it, or a figure floating with nothing under or "
        "holding it?"
    ),
    "collage": (
        "Is the frame more than one picture: a grid of tiles, panels, a character "
        "sheet, or the same person shown twice?"
    ),
    "frightening": (
        "Does the picture show blood, a wound, a weapon being used on or pointed at "
        "someone, an attack in progress, or a child in real terror? A sly or "
        "toothy cartoon villain does not count, and neither does a tool being "
        "used for the job the description says (an axe chopping a tree)."
    ),
}

#: A picture passes without a reviewer only when it clearly shows the moment...
ACCEPT_MOMENT = 0.75
#: ...and no fault is more than this likely.
ACCEPT_FAULT_MAX = 0.25
#: A picture fails without a reviewer's verdict when the moment is this unlikely...
REJECT_MOMENT = 0.20
#: ...or any one fault is at least this likely.
REJECT_FAULT = 0.80
#: Where a call has to be made without a reviewer (it is down or not set), the
#: plain halfway line decides.
COIN = 0.5

#: Pictures are sent at most this many pixels on their long side, as JPEG. The
#: judge reads composition, people and lettering, none of which needs more, and
#: the bill is per input token.
_MAX_SIDE = 1024


class Outcome(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    UNSURE = "unsure"


@dataclass(frozen=True, slots=True)
class Decision:
    """One picture's answers: a probability per question."""

    probabilities: Mapping[str, float]
    input_tokens: int = 0
    model: str = ""
    outcome: Outcome = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome", classify(self.probabilities))

    @property
    def moment(self) -> float:
        return float(self.probabilities.get(SHOWS_MOMENT, 0.0))

    def faults(self, threshold: float = COIN) -> tuple[str, ...]:
        """The faults at least `threshold` likely, the missed moment first."""
        found = ["wrong_moment"] if self.moment < threshold else []
        found += [
            name
            for name, probability in self.probabilities.items()
            if name != SHOWS_MOMENT and probability >= threshold
        ]
        return tuple(found)

    @property
    def score(self) -> int:
        """0-100, the same scale the reviewer scores on: the moment, docked by faults."""
        worst = max(
            (p for name, p in self.probabilities.items() if name != SHOWS_MOMENT),
            default=0.0,
        )
        return max(0, min(100, round(100 * self.moment * (1.0 - worst))))

    def as_record(self) -> dict[str, Any]:
        """What the stored verdict keeps of it: every probability, rounded."""
        return {
            "outcome": str(self.outcome),
            "model": self.model,
            "probabilities": {k: round(v, 3) for k, v in self.probabilities.items()},
        }


def classify(probabilities: Mapping[str, float]) -> Outcome:
    """Accept when confident, reject when confident, otherwise unsure."""
    moment = float(probabilities.get(SHOWS_MOMENT, 0.0))
    worst = max(
        (float(p) for name, p in probabilities.items() if name != SHOWS_MOMENT),
        default=0.0,
    )
    if moment <= REJECT_MOMENT or worst >= REJECT_FAULT:
        return Outcome.REJECT
    if moment >= ACCEPT_MOMENT and worst <= ACCEPT_FAULT_MAX:
        return Outcome.ACCEPT
    return Outcome.UNSURE


class DecisionJudge(Protocol):
    """One picture and its brief in, one probability per question out."""

    name: str

    def decide(self, image: bytes, *, brief: str, cast: str) -> Decision:
        """Raises ProviderError when the judge could not answer."""
        ...


class OpenAIDecisionJudge:
    """The judge on OpenAI's Decisions API (public beta as of October 2026)."""

    name = "openai-decisions"

    def __init__(self, api_key: str, *, model: str, timeout_seconds: int = 60) -> None:
        if not api_key.strip():
            raise ProviderError("OPENAI_API_KEY is not set, so the decision judge is off.")
        import openai

        self._model = model
        self._client = openai.OpenAI(api_key=api_key.strip(), timeout=timeout_seconds)

    def decide(self, image: bytes, *, brief: str, cast: str) -> Decision:
        text = f"A page of a children's picture book. The picture it must be: {brief}\n{cast}"
        try:
            response = self._client.decisions.create(
                model=self._model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": text},
                            {"type": "input_image", "image_url": _data_url(image)},
                        ],
                    }
                ],
                questions=[
                    {"type": "predicate", "name": name, "instructions": question}
                    for name, question in QUESTIONS.items()
                ],
            )
        except Exception as exc:  # the SDK raises a wide range of types
            raise ProviderError(f"the decision judge could not be reached: {exc}") from None
        return _decision(response.model_dump(), model=self._model)


def _decision(payload: Mapping[str, Any], *, model: str) -> Decision:
    """Read the answers; every question must come back with a probability."""
    answers = {
        str(answer.get("name")): answer
        for answer in payload.get("answers", [])
        if isinstance(answer, Mapping)
    }
    probabilities: dict[str, float] = {}
    for name in QUESTIONS:
        answer = answers.get(name)
        probability = answer.get("probability") if answer else None
        if not isinstance(probability, (int, float)):
            kind = answer.get("type") if answer else "missing"
            raise ProviderError(f"the decision judge gave no probability for {name} ({kind})")
        probabilities[name] = max(0.0, min(1.0, float(probability)))
    usage = payload.get("usage") or {}
    return Decision(
        probabilities=probabilities,
        input_tokens=int(usage.get("input_tokens") or 0),
        model=model,
    )


def _data_url(image: bytes) -> str:
    """The picture as an inline JPEG, at most `_MAX_SIDE` on its long side."""
    picture = Image.open(io.BytesIO(image)).convert("RGB")
    picture.thumbnail((_MAX_SIDE, _MAX_SIDE))
    buffer = io.BytesIO()
    picture.save(buffer, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
