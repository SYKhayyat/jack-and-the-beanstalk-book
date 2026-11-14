"""What is kept about a person, for how long, and the way to delete it all (A8).

    GET  /privacy           what is stored and when it goes; open to anyone
    POST /account/delete    delete the signed-in account's books and pictures

The privacy page is public because the question "what do you keep?" comes
before signing in. The deletion is behind the session, and asks for the word
DELETE typed out: it cannot be undone, and a stray click must not do it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.dependencies import ContainerDep
from app.routes.auth import CurrentSessionDep
from app.routes.web import page_chrome
from app.services.accounts import AccountBusy, delete_account
from app.sessions import SESSION_COOKIE

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["account"])

#: The word typed to confirm, compared without case or surrounding spaces.
CONFIRM_WORD = "DELETE"


@router.get("/privacy", response_class=HTMLResponse)
def privacy_page(
    request: Request, container: ContainerDep, session: CurrentSessionDep
) -> HTMLResponse:
    """What the studio keeps, how long, and (signed in) the delete button."""
    return _page(request, container, session)


@router.post("/account/delete", response_model=None)
def delete_my_account(
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    confirm: str = Form(""),
) -> Response:
    """Delete everything this account made, then sign it out.

    A wrong confirmation word or a book still being drawn re-renders the page
    with the reason and changes nothing. Success answers the goodbye page
    directly, with the session cookie cleared, so there is no signed-in page
    left to land on.
    """
    owner = session.user_id if session is not None else None
    if not owner:
        return RedirectResponse("/privacy", status_code=status.HTTP_303_SEE_OTHER)
    if confirm.strip().upper() != CONFIRM_WORD:
        return _page(
            request,
            container,
            session,
            error=f"Type {CONFIRM_WORD} in the box to delete the account.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    try:
        delete_account(container.engine, owner)
    except AccountBusy as busy:
        return _page(
            request, container, session, error=str(busy), status_code=status.HTTP_409_CONFLICT
        )
    response = _page(request, container, None, deleted=True)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def _page(
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    *,
    error: str = "",
    deleted: bool = False,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    chrome = page_chrome(container, session, request)
    settings = container.settings
    owner = session.user_id if session is not None else None
    return templates.TemplateResponse(
        request=request,
        name="privacy.html",
        context={
            **chrome,
            "can_delete": bool(owner) and not deleted,
            "deleted": deleted,
            "error": error,
            "confirm_word": CONFIRM_WORD,
            "draft_retention_hours": settings.draft_retention_hours,
            "child_profile_retention_days": settings.child_profile_retention_days,
            "book_count": len(chrome["books"]),
        },
        status_code=status_code,
    )
