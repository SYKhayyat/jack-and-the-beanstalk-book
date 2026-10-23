"""Domain models: settings, scene data and the book.

Re-exported here so callers can `from app.models import CartoonSettings`
without knowing the internal module layout.
"""

from app.models.book import BOOK_PAGES, AboutMe, BookPage, TextZone, get_book_page
from app.models.feedback import (
    REJECTION_CLAUSES,
    REJECTION_LABELS,
    RejectionFeedback,
    RejectionReason,
)
from app.models.scenes import Scene
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
    "AboutMe",
    "ApparentAge",
    "AspectRatio",
    "BookPage",
    "CartoonSettings",
    "CartoonStyle",
    "CharacterGender",
    "Expression",
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
]
