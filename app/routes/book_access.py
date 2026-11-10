"""Shared rules for reaching a book's artefacts.

Every book URL — the API, the reader, the PDF, a single page — asks the same
two questions: who is this, and may they touch this book? The answers live
here once, so a change to ownership can never apply to only half the URLs.

`no_store` lives here too: everything these routes serve is derived from a
child's photograph, and none of it may be cached by a shared browser or proxy.
"""

from __future__ import annotations

import hashlib

from fastapi import Request, Response

from app.dependencies import ContainerDep
from app.errors import StoryNotReady, UnknownBook, UnknownStory
from app.models.story import StoryEdition
from app.routes.auth import CurrentSessionDep
from app.services.template_store import NoTemplateVersion, UnknownTemplate

__all__ = [
    "book_edition",
    "ensure_book",
    "new_book_edition",
    "no_store",
    "user_id",
    "versioned_picture",
]


def new_book_edition(container: ContainerDep, story_key: str) -> StoryEdition:
    """The edition a book of `story_key` starting now would be made from.

    Everything that spends money towards a new book — a portrait, the start
    itself — asks here first, so the start gate and an unknown story are
    refused before anything is billed.
    """
    try:
        return container.templates.edition_for_new_book(story_key)
    except NoTemplateVersion as exc:
        raise StoryNotReady(str(exc)) from exc
    except UnknownTemplate as exc:
        raise UnknownStory("There is no book by that name yet.") from exc


def book_edition(container: ContainerDep, book_id: str) -> StoryEdition:
    """The edition a saved book is pinned to."""
    try:
        return container.templates.book_edition(book_id)
    except UnknownTemplate as exc:
        raise UnknownBook(
            "No book with that id. It may have expired — start a new one."
        ) from exc


def user_id(session: CurrentSessionDep) -> str | None:
    """The person behind the request, or None (password/anon deployments)."""
    return session.user_id if session else None


def ensure_book(
    book_id: str,
    user_id: str | None,
    container: ContainerDep,
) -> dict[str, object]:
    """Authorise this request to touch a book's artefacts, and return its status.

    Books are keyed to the person who started them. A book with an owner is
    reachable only by that owner — anybody else, signed in or not, gets the
    same plain 404 they would get for a book that does not exist at all, so
    the id leaks no information. An unowned book is open to whoever has a
    session (that only happens where there is no identity to own things).

    Authorisation and payload come from one store call: this is the hot path
    of the progress UI, and a poll that spends three reads authorising itself
    is three round trips per browser tab per 1.5 seconds in database mode.
    """
    payload, owner = container.book_store.load_status_and_owner(book_id)
    if payload is None:
        raise UnknownBook("No book with that id. It may have expired — start a new one.")
    if owner and owner != (user_id or ""):
        raise UnknownBook("No book with that id. It may have expired — start a new one.")
    return payload


def private_cached(
    request: Request,
    data: bytes,
    *,
    media_type: str,
    etag: str | None = None,
) -> Response:
    """A derived picture, with an ETag so it is not downloaded twice (T13).

    The distinction this draws is the point of the whole module: the child's
    *photograph* is `no_store` — it never touches the device's cache — while the
    illustrations made from it are `private, must-revalidate`. `private` forbids
    any shared cache (a proxy, a CDN) from keeping them; `must-revalidate` means
    the browser must ask before reusing one. What the ETag buys is the answer:
    a 304, a few bytes, instead of a megabyte of page art the phone already has.

    The tag is the content hash, so it changes exactly when the picture does — a
    redrawn page cannot be served stale, because its bytes hash differently.
    """
    tag = etag or hashlib.sha256(data).hexdigest()
    headers = {
        "ETag": f'"{tag}"',
        # `private`: never a shared cache. `must-revalidate`: the browser asks
        # every time, so a redrawn page is never shown from an old copy.
        "Cache-Control": "private, max-age=0, must-revalidate",
        "X-Content-Type-Options": "nosniff",
    }
    if etag_matches(request, tag):
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type=media_type, headers=headers)


#: A year: the longest lifetime browsers honour, and longer than any book lives.
_IMMUTABLE = "private, max-age=31536000, immutable"
_REVALIDATE = "private, max-age=0, must-revalidate"


def versioned_picture(
    data: bytes | None,
    *,
    etag: str,
    immutable: bool,
    media_type: str = "image/webp",
) -> Response:
    """A screen-sized picture, or its 304 when `data` is None (P1).

    `immutable` is true only when the URL named the picture's current version:
    then the browser keeps it for a year and never asks again, because a
    redrawn picture is linked under a new URL. Still `private`: a shared cache
    must never hold a picture of somebody's child. An unversioned or stale URL
    is revalidated on every view, exactly as the full-size picture is.
    """
    headers = {
        "ETag": f'"{etag}"',
        "Cache-Control": _IMMUTABLE if immutable else _REVALIDATE,
        "X-Content-Type-Options": "nosniff",
    }
    if data is None:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type=media_type, headers=headers)


def not_modified(etag: str) -> Response:
    """A 304 for a picture the client already has: no bytes, same tag (T13)."""
    return Response(
        status_code=304,
        headers={
            "ETag": f'"{etag}"',
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )


def etag_matches(request: Request, tag: str) -> bool:
    """Whether the client already holds this exact picture.

    `If-None-Match` may carry a list and may carry the weak `W/` prefix; both are
    normal from browsers and neither means "a different picture".
    """
    header = request.headers.get("if-none-match", "")
    if not header:
        return False
    if header.strip() == "*":
        return True
    for candidate in header.split(","):
        if candidate.strip().removeprefix("W/").strip('"') == tag:
            return True
    return False


def no_store(
    data: bytes,
    *,
    media_type: str,
    filename: str | None = None,
    download: bool = False,
) -> Response:
    """A response carrying child-derived data: private, never cached."""
    headers: dict[str, str] = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if filename:
        disposition = "attachment" if download else "inline"
        headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    return Response(content=data, media_type=media_type, headers=headers)
