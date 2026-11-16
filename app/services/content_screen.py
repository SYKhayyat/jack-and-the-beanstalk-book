"""The second look at what a visitor typed: a decision model judges the words.

`app.moderation` is a word list: free, instant and never down, but it cannot
see "Stoopid", "Fatboy", "Smelly McBumface", a slur in another language, or
"Hitler" typed as a name, and it cannot tell whether a name sounds like one.
This asks the decision model (OpenAI's Decisions API, the same model and key as
the picture judge) two typed yes/no questions per printed field — is this name
plainly not a name, is this text unfit for a young child — and refuses a field
only when the model is confident.

The bar is loose on purpose (owner, 2026-11-16): refusing a real family's
unusual name is worse than letting an odd one through, so "not a name" needs
a high probability and the question itself spells out what counts as a name.

When it runs, and what happens when the model cannot be reached, is the
caller's policy, not this module's:

* **A book starting or an About page edit** — screened before the book
  starts. Unreachable: the book goes ahead (the word list has passed it) and
  the print order checks again.
* **A print order** — screened before anything is charged. Unreachable: the
  order is refused with a "try again" message. That is the step that puts the
  words on paper, so it never goes ahead unchecked.

The words a visitor typed go in as JSON strings in the input and the questions
are the API's own typed questions, so a field that says "ignore the above"
is data being judged, not an instruction.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from app.moderation import NAME_FIELDS, PRINTED_FIELDS, ContentIssue
from app.prompt_data import prompt_text
from app.services.image_providers.base import ProviderError

if TYPE_CHECKING:
    from app.services.costs import CostLedger

__all__ = [
    "NOT_A_NAME_REJECT",
    "UNFIT_REJECT",
    "ContentScreen",
    "OpenAIContentScreen",
    "Question",
    "issues_from_probabilities",
    "questions_for",
]

logger = logging.getLogger(__name__)

#: A name is refused only when the model is this sure it is not a name: the
#: loose bar. An unusual real name sits well under it.
NOT_A_NAME_REJECT = 0.85
#: Text is refused when the model is this sure it is unfit for a young child.
UNFIT_REJECT = 0.70

_LABELS: Mapping[str, str] = dict(PRINTED_FIELDS)


@dataclass(frozen=True, slots=True)
class Question:
    """One typed question about one field."""

    name: str
    field: str
    kind: str  # "not_a_name" or "unfit"
    instructions: str


class ContentScreen(Protocol):
    """An About Me record in, the fields it would refuse out."""

    name: str

    def screen(self, about: Mapping[str, object] | object) -> list[ContentIssue]:
        """Raises ProviderError when the screen could not answer."""
        ...


def _fields(about: Mapping[str, object] | object) -> dict[str, str]:
    """The printed fields with words in them; a repeated name is asked once."""
    getter = (
        about.get
        if isinstance(about, Mapping)
        else lambda key, default=None: getattr(about, key, default)
    )
    found: dict[str, str] = {}
    for field, _label in PRINTED_FIELDS:
        value = getter(field)
        if not isinstance(value, str) or not value.strip():
            continue
        if field == "about_name" and value.strip() == found.get("prompt_name", "").strip():
            continue
        found[field] = value.strip()
    return found


def questions_for(fields: Mapping[str, str]) -> list[Question]:
    """Every question to ask: "not a name" for each name, "unfit" for every field."""
    words = prompt_text()["content_screen"]
    asked: list[Question] = []
    for field in fields:
        label = _LABELS[field]
        if field in NAME_FIELDS:
            asked.append(
                Question(
                    f"{field}__not_a_name",
                    field,
                    "not_a_name",
                    str(words["not_a_name"]).format(label=label),
                )
            )
        asked.append(
            Question(
                f"{field}__unfit", field, "unfit", str(words["unfit"]).format(label=label)
            )
        )
    return asked


def _intro(fields: Mapping[str, str]) -> str:
    lines = "\n".join(
        f"{_LABELS[field]}: {json.dumps(value, ensure_ascii=False)}"
        for field, value in fields.items()
    )
    return str(prompt_text()["content_screen"]["intro"]).format(fields=lines)


def _message(question: Question) -> str:
    label = _LABELS[question.field]
    if question.kind == "not_a_name":
        return (
            f"That does not look like a person's name ({label}). "
            "Please use the child's real name."
        )
    return f"Please rewrite {label}: it is not right for a printed children's book."


def issues_from_probabilities(
    asked: list[Question], probabilities: Mapping[str, float]
) -> list[ContentIssue]:
    """The refused fields, one issue each, in the page's field order."""
    issues: list[ContentIssue] = []
    seen: set[str] = set()
    for question in asked:
        bar = NOT_A_NAME_REJECT if question.kind == "not_a_name" else UNFIT_REJECT
        if question.field in seen or probabilities[question.name] < bar:
            continue
        issues.append(ContentIssue(question.field, _message(question)))
        seen.add(question.field)
    return issues


class OpenAIContentScreen:
    """The screen on OpenAI's Decisions API, every call written to the cost ledger."""

    name = "openai-decisions"

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        ledger: CostLedger,
        usd_per_million: float,
        timeout_seconds: int = 15,
    ) -> None:
        if not api_key.strip():
            raise ProviderError("OPENAI_API_KEY is not set, so the content screen is off.")
        import openai

        self._model = model
        self._ledger = ledger
        self._usd_per_million = usd_per_million
        self._client = openai.OpenAI(api_key=api_key.strip(), timeout=timeout_seconds)

    def screen(self, about: Mapping[str, object] | object) -> list[ContentIssue]:
        fields = _fields(about)
        if not fields:
            return []
        asked = questions_for(fields)
        started = time.monotonic()
        tokens = 0
        ok = False
        try:
            try:
                response = self._client.decisions.create(
                    model=self._model,
                    input=[
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": _intro(fields)}],
                        }
                    ],
                    questions=[
                        {"type": "predicate", "name": q.name, "instructions": q.instructions}
                        for q in asked
                    ],
                )
            except Exception as exc:  # the SDK raises a wide range of types
                raise ProviderError(
                    f"the content screen could not be reached: {exc}"
                ) from None
            payload = response.model_dump()
            tokens = int((payload.get("usage") or {}).get("input_tokens") or 0)
            probabilities = _probabilities(payload, asked)
            ok = True
        finally:
            self._ledger.record(
                kind="screen",
                provider="decisions",
                model=self._model,
                images=0,
                ok=ok,
                cost_microusd=round(tokens * self._usd_per_million),
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        issues = issues_from_probabilities(asked, probabilities)
        if issues:
            logger.info(
                "content_screen.refused fields=%s",
                ",".join(issue.field for issue in issues),
            )
        return issues


def _probabilities(payload: Mapping[str, Any], asked: list[Question]) -> dict[str, float]:
    """Every asked question's probability; a missing one is a failed call."""
    answers = {
        str(answer.get("name")): answer
        for answer in payload.get("answers", [])
        if isinstance(answer, Mapping)
    }
    probabilities: dict[str, float] = {}
    for question in asked:
        answer = answers.get(question.name)
        probability = answer.get("probability") if answer else None
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise ProviderError(f"the content screen gave no probability for {question.name}")
        probabilities[question.name] = max(0.0, min(1.0, float(probability)))
    return probabilities
