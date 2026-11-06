"""Shared rules for reaching a book's artefacts.

Every book URL — the API, the reader, the PDF, a single page — asks the same
two questions: who is this, and may they touch this book? The answers live
here once, so a change to ownership can never apply to only half the URLs.

`no_store` lives here too: everything these routes serve is derived from a
child's photograph, and none of it may be cached by a shared browser or proxy.
"""

from __future__ import annotations

from fastapi import Response

from app.dependencies import ContainerDep
from app.errors import UnknownBook
from app.routes.auth import CurrentSessionDep

__all__ = ["ensure_book", "no_store", "user_id"]


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
