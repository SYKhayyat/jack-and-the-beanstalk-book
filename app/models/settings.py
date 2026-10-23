"""The knobs the user can turn, as validated domain objects.

Every field here maps to something the image model can act on. Fields whose
names begin with `keep_` control whether a real-world trait of the upload is
preserved or deliberately re-stylised.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.prompt_data import prompt_text


class CartoonStyle(StrEnum):
    """Art direction for the stylisation pass."""

    MANGA = "manga"
    ANIME = "anime"
    WESTERN_CARTOON = "western_cartoon"
    PIXAR_LIKE = "pixar_like"
    DISNEY_LIKE = "disney_like"
    COMIC_BOOK = "comic_book"
    PENCIL_SKETCH = "pencil_sketch"
    WATERCOLOUR = "watercolour"
    VECTOR_FLAT = "vector_flat"


class ApparentAge(StrEnum):
    """Apparent age of the cartoon character."""

    AS_UPLOADED = "as_uploaded"
    CHILD = "child"
    TEEN = "teen"
    ADULT = "adult"


class CharacterGender(StrEnum):
    """Which gender the AI should draw the character as.

    ``MATCH`` means "the same as the person in the photo"; because the app cannot
    infer a child's gender from pixels, that choice requires ``photo_gender`` to
    say which it is. Either way the answer resolves to a boy or a girl, which is
    what names the character (Jack or Jane) and picks the pronouns.
    """

    MATCH = "match"
    BOY = "boy"
    GIRL = "girl"


class PhotoGender(StrEnum):
    """What the uploaded photo shows, asked only when ``CharacterGender.MATCH``."""

    BOY = "boy"
    GIRL = "girl"


class SkinTone(StrEnum):
    """Skin tone of the cartoon character."""

    AS_UPLOADED = "as_uploaded"
    VERY_LIGHT = "very_light"
    LIGHT = "light"
    MEDIUM = "medium"
    TAN = "tan"
    OLIVE = "olive"
    BROWN = "brown"
    DEEP_BROWN = "deep_brown"
    DEEPEST_BROWN = "deepest_brown"


class PortraitBackground(StrEnum):
    """Background of the cartoon portrait.

    Applies to pass 1 only. It is deliberately *not* carried into the scene
    prompt: pass 2 draws the scene from its own specification, so a background
    requested here would not survive into the final image — and a busy one in
    the reference actively fights the scene's caption. See
    `prompts._portrait_background_clause`.
    """

    AS_UPLOADED = "as_uploaded"
    PLAIN = "plain"
    SOFT_COLOUR = "soft_colour"
    STORYBOOK = "storybook"


class Outfit(StrEnum):
    """What the cartoon character wears.

    A closed set, not free text. Every other user-facing field is too: visitor
    text reaching a system prompt cannot be validated, cannot be tested for,
    grows without bound, and is an injection surface.
    """

    AS_UPLOADED = "as_uploaded"
    STORYBOOK_TUNIC = "storybook_tunic"
    PEASANT = "peasant"
    ADVENTURER = "adventurer"
    SIMPLE_SHIRT = "simple_shirt"
    HOODED_CLOAK = "hooded_cloak"
    NONE_SPECIFIED = "none_specified"


class HairStyle(StrEnum):
    """Hair shape and length."""

    AS_UPLOADED = "as_uploaded"
    SHORT = "short"
    BOB = "bob"
    LONG_STRAIGHT = "long_straight"
    LONG_WAVY = "long_wavy"
    CURLY = "curly"
    COILY = "coily"
    PONYTAIL = "ponytail"
    BUNS = "buns"
    BALD = "bald"


class HairColour(StrEnum):
    """Hair colour. `None` for hair colour choices that read as no colour."""

    AS_UPLOADED = "as_uploaded"
    BLACK = "black"
    DARK_BROWN = "dark_brown"
    BROWN = "brown"
    AUBURN = "auburn"
    RED = "red"
    BLONDE = "blonde"
    PLATINUM = "platinum"
    GREY = "grey"
    WHITE = "white"


class Expression(StrEnum):
    """Facial expression. Read literally by the prompt."""

    AS_UPLOADED = "as_uploaded"
    FOCUSED_DETERMINED = "focused_determined"
    HAPPY = "happy"
    SERIOUS = "serious"
    SURPRISED = "surprised"
    CURIOUS = "curious"
    BRAVE = "brave"
    WORRIED = "worried"
    GRINNING = "grinning"


class LikenessStrength(StrEnum):
    """How hard to hold on to the uploaded face.

    This exists because "recognisably the same person" is a graded requirement.
    Dropping to ``loose`` is occasionally worth it when style and fidelity fight,
    but ``strict`` is the default.
    """

    STRICT = "strict"
    BALANCED = "balanced"
    LOOSE = "loose"


class AspectRatio(StrEnum):
    PORTRAIT = "1:1"
    LANDSCAPE = "4:3"
    TALL = "3:4"
    CINEMATIC = "16:9"


class CartoonSettings(BaseModel):
    """Validated user settings for one generation request."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    # --- how cartoon vs. real ---
    cartoon_intensity: int = Field(
        default=8,
        ge=1,
        le=10,
        description=(
            "1 = nearly photorealistic with a hint of line art; 10 = flat graphic cartoon."
        ),
    )
    likeness_strength: LikenessStrength = LikenessStrength.STRICT

    # --- art direction ---
    cartoon_style: CartoonStyle = CartoonStyle.WESTERN_CARTOON
    aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE
    portrait_background: PortraitBackground = PortraitBackground.PLAIN
    candidates: int = Field(
        default=1,
        ge=1,
        le=3,
        description=(
            "How many alternatives to generate for the portrait. One API call "
            "covers all of them, so three candidates is three times the cost of "
            "one, but it lets you pick rather than re-roll blindly."
        ),
    )

    # --- who the character is ---
    apparent_age: ApparentAge = ApparentAge.AS_UPLOADED
    character_gender: CharacterGender = CharacterGender.MATCH
    photo_gender: PhotoGender = PhotoGender.BOY
    skin_tone: SkinTone = SkinTone.AS_UPLOADED

    # --- details ---
    # Closed sets rather than text boxes: see the note on Outfit.
    hair_style: HairStyle = HairStyle.AS_UPLOADED
    hair_color: HairColour = HairColour.AS_UPLOADED
    outfit: Outfit = Outfit.STORYBOOK_TUNIC
    expression: Expression = Expression.FOCUSED_DETERMINED

    # --- what to keep from the photo ---
    keep_glasses: bool = True
    keep_facial_hair: bool = True

    def identity_directive(self) -> str:
        """The sentence that tells the model to preserve the uploaded face."""
        directive: str = prompt_text()["identity"]["directive"]
        return directive

    @property
    def effective_gender(self) -> PhotoGender:
        """The resolved gender: which name and pronouns the prompts must use."""
        if self.character_gender is CharacterGender.MATCH:
            return self.photo_gender
        return PhotoGender(self.character_gender.value)


__all__ = [
    "ApparentAge",
    "AspectRatio",
    "CartoonSettings",
    "CartoonStyle",
    "CharacterGender",
    "LikenessStrength",
    "PhotoGender",
    "SkinTone",
]
