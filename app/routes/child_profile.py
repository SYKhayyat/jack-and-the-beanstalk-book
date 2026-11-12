"""The remembered child: save the star once, and start the next book with them.

The studio's photo step is the friction before the product: an upload, a
portrait call, a pick. These four routes let a person keep the child they
already made — the name, the boy/girl answer and the approved portrait — and
hand that portrait back as a fresh draft, so a second book needs neither the
upload nor the image call (AUDIT.md, Idea 2).

Nothing here draws a picture, so nothing here is billed and no rate token is
spent. What is stored is exactly what a book already keeps for its retention
window: the star's portrait and the photograph behind it — never a cast
member's face, which belongs to one book, not to the person who made it.

The picture is served owner-only and `private, no-store`, like every other
picture derived from a child's photograph.
"""

from __future__ import annotations

import hashlib
import logging
from uuid import uuid4

from fastapi import APIRouter, Form, Response, status

from app.dependencies import ContainerDep
from app.errors import InvalidSettings, NoSavedChild
from app.models import CartoonSettings, PhotoGender
from app.routes.auth import CurrentSessionDep
from app.routes.book_access import new_book_edition, no_store
from app.routes.book_access import user_id as _user_id
from app.services.child_profiles import ChildProfile
from app.services.pipeline import UnknownGeneration
from app.services.storage import Draft, candidate_url, draft_urls

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/child-profile", tags=["studio"])


@router.post("", status_code=status.HTTP_201_CREATED)
def remember_child(
    container: ContainerDep,
    session: CurrentSessionDep,
    generation_id: str = Form(
        ..., description="The star's approved portrait draft, from /api/cartoonify."
    ),
    name: str = Form("", max_length=60),
    photo_gender: str = Form("", description="`boy` or `girl`; blank keeps what was drawn."),
) -> dict[str, object]:
    """Remember this account's star, so the next book skips the photo step.

    The draft is the source of every picture: its chosen portrait, the
    photograph it was drawn from and the look settings it was drawn with. A
    draft made for a cast member is refused — remembering the mother would put
    her face on every page of the next book.
    """
    owner = _user_id(session)
    if not owner:
        raise NoSavedChild(
            "There is no account here to remember a child for, so the next book "
            "will ask for a photo again."
        )
    draft = container.store.load_draft(generation_id)
    if draft is None:
        raise UnknownGeneration(generation_id)
    if draft.cast_key:
        raise InvalidSettings(
            "Only the star can be remembered — that portrait was made for "
            f"{draft.cast_key!r}, and a cast member's face belongs to one book."
        )
    gender = photo_gender.strip()
    if gender and gender not in tuple(value.value for value in PhotoGender):
        raise InvalidSettings(f"{gender!r} is not a boy or a girl answer.")
    portrait = draft.cartoon_image
    profile = container.child_profiles.save(
        ChildProfile(
            user_id=owner,
            name=name.strip(),
            photo_gender=gender or draft.settings.photo_gender.value,
            story_key=draft.story_key,
            settings=draft.settings.model_dump(mode="json"),
            portrait=portrait,
            portrait_media_type=draft.cartoon_media_type,
            portrait_sha256=hashlib.sha256(portrait).hexdigest(),
            photo=draft.photo_data,
            photo_media_type=draft.photo_media_type,
        )
    )
    return _card(profile)


@router.post("/use", status_code=status.HTTP_201_CREATED)
def use_saved_child(
    container: ContainerDep,
    session: CurrentSessionDep,
    story: str = Form(..., description="The story this book is for."),
) -> dict[str, object]:
    """Hand the remembered portrait back as a fresh draft, drawing nothing.

    A portrait is drawn in its story's costume, so only the story that made it
    may use it. The new draft has a new id, which is what the studio's chosen
    portrait and `/api/book/start` already understand; no upload is repeated and
    no image is bought.
    """
    owner = _user_id(session)
    profile = container.child_profiles.get(owner) if owner else None
    if profile is None:
        raise NoSavedChild(
            "There is no saved child to use — upload a photo instead, or add one "
            "again and tick to remember it."
        )
    edition = new_book_edition(container, story)
    if profile.story_key != edition.key:
        raise InvalidSettings(
            "That portrait was drawn for a different book, in that story's costume. "
            "Start this book with a photo of the star."
        )
    draft = Draft(
        generation_id=uuid4().hex,
        settings=CartoonSettings.model_validate(profile.settings or {}),
        cartoon_prompt="",
        candidates=(profile.portrait,),
        candidate_media_types=(profile.portrait_media_type,),
        photo_data=profile.photo,
        photo_media_type=profile.photo_media_type,
        provider="saved",
        model="saved",
        cast_key="",
        story_key=profile.story_key,
    )
    container.store.save_draft(draft)
    logger.info("child_profile.used user=%s story=%s", owner, profile.story_key)
    return _payload(draft)


@router.delete("")
def forget_child(container: ContainerDep, session: CurrentSessionDep) -> dict[str, object]:
    """Forget the remembered child, and with it both stored pictures."""
    owner = _user_id(session)
    forgot = container.child_profiles.forget(owner) if owner else False
    return {"forgotten": forgot}


@router.get("/portrait")
def saved_portrait(container: ContainerDep, session: CurrentSessionDep) -> Response:
    """The remembered portrait, for the studio card. Owner-only, never cached."""
    owner = _user_id(session)
    profile = container.child_profiles.get(owner) if owner else None
    if profile is None:
        raise NoSavedChild("There is no saved child here.")
    return no_store(profile.portrait, media_type=profile.portrait_media_type)


def _card(profile: ChildProfile) -> dict[str, object]:
    """What the studio needs to show the child it just remembered."""
    return {
        "name": profile.name,
        "photo_gender": profile.photo_gender,
        "story_key": profile.story_key,
        "portrait_url": portrait_url(profile),
        "saved": True,
    }


def portrait_url(profile: ChildProfile) -> str:
    """The card's picture URL, versioned so a redrawn portrait is a new URL."""
    return f"/api/child-profile/portrait?v={profile.portrait_sha256}"


def _payload(draft: Draft) -> dict[str, object]:
    """The cartoonify-shaped answer the studio's section renderer already reads."""
    urls = draft_urls(draft.generation_id)
    return {
        "generation_id": draft.generation_id,
        "stage": "chosen",
        "settings": draft.settings.model_dump(mode="json"),
        "images": {
            "cartoon_url": urls["cartoon"],
            "candidate_urls": [candidate_url(draft.generation_id, 0)],
        },
        "candidate_count": 1,
        "cast_key": "",
        "provider_call_count": 0,
        "billed_image_count": 0,
    }
