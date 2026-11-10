"""What a story decides about its star's portrait settings, and what is left to the visitor.

The studio used to offer every setting for every story, and a few of them
were never really the visitor's to choose:

* **The art style.** The pages are always drawn in the story's one style
  (``[stories.<key>].style`` in ``prompts.toml``). A star portrait in any
  other style handed the page pass a reference that fought it.
* **The outfit, in a tale with a costume.** Red wears the red hood on every
  page whatever the picker says, so a stored ``storybook_tunic`` was untrue.
* **The page shape and the portrait background, once plates exist.** A 4:3
  plate cannot become a 3:4 page without redrawing the background, and a busy
  portrait background is one more thing for the edit pass to keep out of the
  plate.

`StoryRules.fit` forces those values onto any settings that come in, so what
is stored with a portrait or a book always says what was really drawn. The
studio renders `StoryRules.defaults` and leaves the locked settings out of its
dialogs.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.settings import (
    AspectRatio,
    CartoonSettings,
    CartoonStyle,
    Expression,
    Outfit,
    PortraitBackground,
)
from app.models.story import StoryEdition
from app.prompt_data import prompt_text
from app.services.plates import PLATE_SHAPE

__all__ = ["StoryRules", "story_rules"]


@dataclass(frozen=True, slots=True)
class StoryRules:
    """The settings one edition of a story fixes, and the defaults it suggests."""

    style: CartoonStyle
    expression: Expression
    #: True when the story dresses its star in a costume (Red's hood).
    costumed: bool
    #: True when the edition is a template version, i.e. its pages are drawn onto plates.
    plated: bool

    @property
    def locked(self) -> dict[str, object]:
        """The settings this story decides, by field name."""
        fixed: dict[str, object] = {"cartoon_style": self.style}
        if self.costumed:
            fixed["outfit"] = Outfit.NONE_SPECIFIED
        if self.plated:
            fixed["aspect_ratio"] = PLATE_SHAPE
            fixed["portrait_background"] = PortraitBackground.PLAIN
        return fixed

    def is_locked(self, field: str) -> bool:
        return field in self.locked

    @property
    def page_shape(self) -> AspectRatio | None:
        """The one page shape this story allows, or None when the visitor chooses."""
        return PLATE_SHAPE if self.plated else None

    def fit(self, settings: CartoonSettings) -> CartoonSettings:
        """`settings` with every value this story decides put back."""
        return settings.model_copy(update=self.locked)

    def defaults(self) -> CartoonSettings:
        """What the studio starts from for this story."""
        return self.fit(CartoonSettings(expression=self.expression))


def story_rules(edition: StoryEdition) -> StoryRules:
    """The rules for `edition`, read from its story's section of ``prompts.toml``."""
    section = prompt_text()["stories"][edition.key]
    return StoryRules(
        style=CartoonStyle(str(section["style"])),
        expression=Expression(str(section.get("star_expression", Expression.AS_UPLOADED))),
        costumed=bool(edition.star_member.costume),
        plated=edition.template_version_id is not None,
    )
