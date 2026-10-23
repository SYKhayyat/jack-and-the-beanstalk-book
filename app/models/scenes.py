"""The scene data contract.

A `Scene` is pure data: the prose specification sent to the image model, plus
the fields the page prompts reuse. The prose lives in ``app/prompts.toml``;
adding a page to the book means adding one ``[[book_pages]]`` entry there — no
code changes anywhere else in the app.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from app.prompt_data import prompt_text

_CHECKLIST_TEMPLATE: str = prompt_text()["assembly"]["checklist"]


@dataclass(frozen=True, slots=True)
class Scene:
    """One story beat.

    Attributes:
        key: Stable identifier used in URLs and the API payload.
        title: Short human label for the UI.
        caption: The scene specification as sent to the model. This exact text
            is burned onto the generated image, which is what the assignment's
            "captioned with the scene specification you sent" requirement asks for.
        subject_action: What the character is physically doing. This is the part
            the model most often gets wrong, so it is kept separate and gets its
            own emphatic clause in the prompt.
        environment: Where the beat happens.
        time_of_day: Drives lighting and palette.
        camera: Framing and lens language.
        must_include: Concrete objects that must be visible. Also emitted as an
            explicit checklist so the model treats them as requirements.
        must_avoid: Things that would make the image unfaithful to the caption.
    """

    key: str
    title: str
    caption: str
    subject_action: str
    environment: str
    time_of_day: str
    camera: str
    must_include: tuple[str, ...] = field(default_factory=tuple)
    must_avoid: tuple[str, ...] = field(default_factory=tuple)

    @property
    def checklist(self) -> str:
        if not self.must_include:
            return ""
        items = ", ".join(f'"{item}"' for item in self.must_include)
        return _CHECKLIST_TEMPLATE.format(items=items)

    def formatted(self, substitutions: Mapping[str, str]) -> Scene:
        """A copy with ``{name}``/``{pronoun}`` placeholders filled in.

        The prose lives in TOML with placeholders because the character is Jack
        or Jane depending on the user's gender choice; this is where that choice
        is applied, so the prompt and the burned caption stay in step.
        """

        def fill(text: str) -> str:
            return text.format(**substitutions)

        return replace(
            self,
            title=fill(self.title),
            caption=fill(self.caption),
            subject_action=fill(self.subject_action),
            environment=fill(self.environment),
            time_of_day=fill(self.time_of_day),
            camera=fill(self.camera),
            must_include=tuple(fill(item) for item in self.must_include),
            must_avoid=tuple(fill(item) for item in self.must_avoid),
        )
