"""Form fields for cartoon settings, as a reusable FastAPI dependency.

Both generation endpoints accept the same settings, so declaring them once here
beats repeating thirteen `Form(...)` parameters in each signature — and it means
adding a setting is a one-line change that both endpoints pick up.
"""

from __future__ import annotations

from fastapi import Form
from pydantic import ValidationError

from app.errors import InvalidSettings
from app.models import CartoonSettings
from app.models.settings import (
    ApparentAge,
    AspectRatio,
    CartoonStyle,
    CharacterGender,
    Expression,
    HairColour,
    HairStyle,
    LikenessStrength,
    Outfit,
    PhotoGender,
    PortraitBackground,
    SkinTone,
)

__all__ = ["cartoon_settings_from_form"]


def cartoon_settings_from_form(
    cartoon_intensity: int = Form(8, ge=1, le=10),
    cartoon_style: str = Form(CartoonStyle.PIXAR_LIKE),
    likeness_strength: str = Form(LikenessStrength.STRICT),
    aspect_ratio: str = Form(AspectRatio.LANDSCAPE),
    portrait_background: str = Form(PortraitBackground.PLAIN),
    candidates: int = Form(1, ge=1, le=3),
    apparent_age: str = Form(ApparentAge.AS_UPLOADED),
    character_gender: str = Form(CharacterGender.MATCH),
    photo_gender: str = Form(PhotoGender.BOY),
    skin_tone: str = Form(SkinTone.AS_UPLOADED),
    hair_style: str = Form(HairStyle.AS_UPLOADED),
    hair_color: str = Form(HairColour.AS_UPLOADED),
    outfit: str = Form(Outfit.STORYBOOK_TUNIC),
    expression: str = Form(Expression.FOCUSED_DETERMINED),
    keep_glasses: bool = Form(True),
    keep_facial_hair: bool = Form(True),
) -> CartoonSettings:
    """Validate the submitted settings.

    Form fields arrive as strings; pydantic turns them into enums and enforces
    the 1..10 range, so a bad value fails here with a 422 naming the field
    rather than deep inside the prompt builder.

    Raises:
        InvalidSettings: if any submitted value is out of range or unknown.
    """
    try:
        return CartoonSettings.model_validate(
            {
                "cartoon_intensity": cartoon_intensity,
                "cartoon_style": cartoon_style,
                "likeness_strength": likeness_strength,
                "aspect_ratio": aspect_ratio,
                "portrait_background": portrait_background,
                "candidates": candidates,
                "apparent_age": apparent_age,
                "character_gender": character_gender,
                "photo_gender": photo_gender,
                "skin_tone": skin_tone,
                "hair_style": hair_style,
                "hair_color": hair_color,
                "outfit": outfit,
                "expression": expression,
                "keep_glasses": keep_glasses,
                "keep_facial_hair": keep_facial_hair,
            }
        )
    except ValidationError as exc:
        # Report field and expectation, never the submitted value. pydantic's
        # errors() echoes the input back, which would reflect arbitrary visitor
        # text in the response body; harmless in JSON, but there is no reason to
        # do it.
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False)
        )
        raise InvalidSettings(f"Invalid cartoon settings — {details}") from exc
