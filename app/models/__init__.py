"""Domain models: what the app knows about scenes, settings and results.

Re-exported here so callers can `from app.models import CartoonSettings, get_scene`
without knowing the internal module layout.
"""

from app.models.book import BOOK_PAGES, AboutMe, BookPage, TextZone, get_book_page
from app.models.feedback import (
    REJECTION_CLAUSES,
    REJECTION_LABELS,
    RejectionFeedback,
    RejectionReason,
)
from app.models.generation import GenerationRequest, GenerationResult
from app.models.scenes import SCENES, Scene, get_scene
from app.models.settings import (
    ApparentAge,
    AspectRatio,
    CartoonSettings,
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

__all__ = [
    "BOOK_PAGES",
    "REJECTION_CLAUSES",
    "REJECTION_LABELS",
    "SCENES",
    "AboutMe",
    "ApparentAge",
    "AspectRatio",
    "BookPage",
    "CartoonSettings",
    "CartoonStyle",
    "CharacterGender",
    "Expression",
    "GenerationRequest",
    "GenerationResult",
    "HairColour",
    "HairStyle",
    "LikenessStrength",
    "Outfit",
    "PhotoGender",
    "PortraitBackground",
    "RejectionFeedback",
    "RejectionReason",
    "Scene",
    "SkinTone",
    "TextZone",
    "get_book_page",
    "get_scene",
]
