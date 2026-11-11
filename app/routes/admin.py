"""The plate review screen: where an operator settles a story's plates (T6).

Three screens, one gate:

    /admin/plates                               every story and its progress
    /admin/plates/{story}                       the contact sheet — all 21 plates
    /admin/plates/{story}/{page}                one page's candidates, to approve
    /admin/plates/{story}/cast/{cast_key}       a supporting character's fixed portrait

The portrait screen (P2) takes one draft per upload and approves one of them; the
approved portrait is what a book draws a character with when its visitor gave no
photograph. The star has no fixed portrait, so the screen answers 404 for the star.

and one switch per story, the start gate (T6.5): `POST /admin/plates/{story}/gate`
decides whether a book of that story may start before its plates are all
approved. Off, books are drawn from the words and looks alone; on, a story with
no complete template version is "not ready yet" on the shelf and refuses a start.

The gate is an identity, not a role: `OPERATOR_SUB` names the Google account(s)
who may come in — one or more OAuth subject ids or account emails, comma
separated — and every route answers 404 to anybody else. That 404 is the same
answer an unknown URL gives, so `/admin` neither confirms that it exists nor
hints at what it holds.

Every write goes through `TemplateStore`; this module owns no SQL. The one rule
it adds in front of the store is the cover's title-band check (T1.5): a plate
whose top band is too busy for real text cannot be approved, and the reason is
shown on the button that would have done it — checked again on POST, because a
hidden button is not a rule.

Approving idempotently records the operator and timestamp (the store does that),
and replacing an approved plate writes a new template version; nothing a
visitor already started changes.
"""

from __future__ import annotations

import io
import logging
import threading
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from PIL import Image

from app.books import SEEDS, StorySeed, picture_cast
from app.container import Container
from app.dependencies import ContainerDep
from app.routes.auth import CurrentSessionDep
from app.routes.book_access import no_store
from app.routes.web import page_chrome
from app.services.costs import cost_scope
from app.services.image_providers.base import ImageRequest
from app.services.pdf import COVER_TITLE_BAND_FRACTION, title_band_report
from app.services.plates import PLATE_SHAPE, PlatePlan, plate_plans
from app.services.template_store import (
    COVER_PAGE,
    PAGE_NUMBERS,
    PageSummary,
    PlateNotApprovable,
    PlateStatus,
    PlateSummary,
    PortraitSummary,
    Slot,
    TemplateError,
    TemplateStore,
    UnknownTemplate,
)

__all__ = ["operator_or_404", "render_admin", "router"]


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)

#: One lock per story around "pick the next missing page, paint it, store it".
#: Two tabs (or a reload mid-call) otherwise both read the same missing page and
#: pay for it twice. Serialising instead makes the second request paint the page
#: after it and wait its turn; nothing is invented and nothing is paid for twice.
_PAINT_LOCKS: dict[str, threading.Lock] = {}
_PAINT_LOCKS_GUARD = threading.Lock()


def _paint_lock(story_key: str) -> threading.Lock:
    with _PAINT_LOCKS_GUARD:
        return _PAINT_LOCKS.setdefault(story_key, threading.Lock())


def get_templates(container: ContainerDep) -> TemplateStore:
    """The store the admin screens read and write: the app's own."""
    return container.templates


TemplateDep = Annotated[TemplateStore, Depends(get_templates)]


def operator_or_404(container: Container, session: CurrentSessionDep) -> str:
    """The operator's name, or a 404 for everyone else.

    `OPERATOR_SUB` may list Google subject ids and/or account emails; either
    matches. The returned value is what a plate records as its approver: the
    sub when the sub was named (the long-standing behaviour, and what the
    tests pin), otherwise the email that was named.

    Deliberately indistinguishable from a missing page: a 403 would tell a
    signed-in visitor that a review screen exists and who may use it.
    """
    settings = container.settings
    if session is None:
        raise HTTPException(status_code=404, detail="Not Found")
    if session.sub and session.sub in settings.operator_ids:
        return session.sub
    if session.email and settings.is_operator_identity(email=session.email):
        return session.email
    raise HTTPException(status_code=404, detail="Not Found")


#: The operator's own navigation. One list, so every admin screen agrees.
ADMIN_TABS: tuple[tuple[str, str], ...] = (
    ("/admin/status", "Overview"),
    ("/admin/plates", "Stories & plates"),
    ("/", "Back to the shelf"),
)


def admin_chrome(
    container: Container, session: CurrentSessionDep, request: Request, current: str
) -> dict[str, Any]:
    """Shared page chrome plus the operator navigation and who they are."""
    return {
        **page_chrome(container, session, request),
        "admin_tabs": ADMIN_TABS,
        "admin_current": current,
        "admin_operator": (session.email or session.name or session.sub) if session else "",
    }


def _seed_or_404(story_key: str) -> StorySeed:
    for seed in SEEDS:
        if seed.key == story_key:
            return seed
    raise HTTPException(status_code=404, detail="Not Found")


def _page_or_404(page_number: int) -> int:
    if page_number not in PAGE_NUMBERS:
        raise HTTPException(status_code=404, detail="Not Found")
    return page_number


def render_admin(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(request=request, name=name, context=context)


def _page_url(story_key: str, page_number: int, *, error: str = "") -> str:
    path = f"/admin/plates/{story_key}/{page_number}"
    return f"{path}?{urlencode({'error': error})}" if error else path


def _page_label(page_number: int, title: str) -> str:
    return "Cover" if page_number == COVER_PAGE else f"Page {page_number} — {title}"


def _cast_label(star: str, page: PageSummary | None) -> str:
    """Who is drawn on the page, in the order the slots are laid out.

    The star is first unless the page leaves them out, so the screen and the
    stored slots name people in the same order.
    """
    if page is None:
        return star or "the star"
    names = picture_cast(star, page.cast_keys, star_in_picture=page.star_in_picture)
    return ", ".join(names) if names else "the star"


def _slot_rows(slots: tuple[Slot, ...]) -> list[dict[str, Any]]:
    """Each slot as the page draws it: a CSS rectangle plus its stored numbers."""
    return [
        {
            "cast_key": slot.cast_key,
            "rect": ",".join(f"{value:.4f}" for value in slot.rect),
            "style": (
                f"left:{slot.rect[0] * 100:.2f}%;top:{slot.rect[1] * 100:.2f}%;"
                f"width:{slot.rect[2] * 100:.2f}%;height:{slot.rect[3] * 100:.2f}%"
            ),
        }
        for slot in slots
    ]


def _band(image: bytes) -> tuple[bool, str]:
    """Whether a plate's title band is calm, and why not when it is not."""
    try:
        report = title_band_report(image)
    except ValueError as exc:
        return False, str(exc)
    return report.ok, report.reason


def _plate_view(plate: PlateSummary) -> dict[str, Any]:
    return {
        "id": plate.id,
        "status": plate.status,
        "url": f"/admin/plates/{plate.story_key}/{plate.id}.png",
        "prompt": plate.prompt,
        "prompt_hash": plate.prompt_hash,
        "approved_by": plate.approved_by,
        "created": plate.created_at.strftime("%Y-%m-%d %H:%M"),
        "slots": _slot_rows(plate.slots),
    }


# --- the screens --------------------------------------------------------------


@router.get("/plates", response_class=HTMLResponse)
def plate_index(
    request: Request, container: ContainerDep, session: CurrentSessionDep, store: TemplateDep
) -> HTMLResponse:
    """Every story, how much of it is approved, and whether it can make books."""
    operator_or_404(container, session)
    stories = [_story_progress(seed, store) for seed in SEEDS]
    return render_admin(
        request,
        "admin_plates.html",
        {
            **admin_chrome(container, session, request, "/admin/plates"),
            "stories": stories,
            "page_total": len(PAGE_NUMBERS),
        },
    )


@router.get("/plates/{story_key}", response_class=HTMLResponse)
def contact_sheet(
    story_key: str,
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    store: TemplateDep,
    painted_page: int | None = None,
    finished: bool = False,
) -> HTMLResponse:
    """All 21 plates of a story in page order: the set, not one picture."""
    operator_or_404(container, session)
    seed = _seed_or_404(story_key)
    try:
        pages = store.pages(story_key)
        # One query for all 21 pages, not one per page: on Postgres each
        # round trip is tens of milliseconds, and 21 of them is a ten-second
        # screen for the operator.
        all_plates = store.plates(story_key)
    except UnknownTemplate:
        raise HTTPException(status_code=404, detail="Not Found") from None
    by_page: dict[int, list[PlateSummary]] = {number: [] for number in PAGE_NUMBERS}
    for plate in all_plates:
        by_page.setdefault(plate.page_number, []).append(plate)
    painted = {
        plate.page_number for plate in all_plates if plate.status != PlateStatus.RETIRED
    }
    tiles = []
    for number in PAGE_NUMBERS:
        page = pages.get(number)
        plates = by_page[number]
        approved = next((p for p in plates if p.status == PlateStatus.APPROVED), None)
        drafts = [p for p in plates if p.status == PlateStatus.DRAFT]
        shown = approved or (drafts[0] if drafts else None)
        title = page.title if page else ""
        tiles.append(
            {
                "number": number,
                "label": _page_label(number, title),
                "approved": approved is not None,
                "drafts": len(drafts),
                "plate": _plate_view(shown) if shown else None,
                "band_percent": f"{COVER_TITLE_BAND_FRACTION * 100:.2f}",
            }
        )
    version = store.current_version(story_key)
    return render_admin(
        request,
        "admin_plate_sheet.html",
        {
            **admin_chrome(container, session, request, "/admin/plates"),
            "story": seed,
            "tiles": tiles,
            "version": version.number if version else None,
            "approved_count": sum(1 for tile in tiles if tile["approved"]),
            "painted_count": len(painted),
            "painted_page": painted_page,
            "finished": finished,
            "page_total": len(PAGE_NUMBERS),
        },
    )


# Declared before the page view on purpose: a plate URL ends in `.png`, and
# the page route would otherwise claim it and answer 422 for a bad page number.
@router.get("/plates/{story_key}/{plate_id}.png")
def plate_image(
    story_key: str,
    plate_id: str,
    container: ContainerDep,
    session: CurrentSessionDep,
    store: TemplateDep,
) -> Response:
    """A plate's bytes, for the operator's own screen only."""
    operator_or_404(container, session)
    _seed_or_404(story_key)
    try:
        image, media_type = store.plate_image(plate_id)
    except UnknownTemplate:
        raise HTTPException(status_code=404, detail="Not Found") from None
    # A plate carries no child's face, but it is the operator's working set:
    # keep it out of any shared cache, like the rest of the book artefacts.
    return no_store(image, media_type=media_type)


@router.get("/plates/{story_key}/{page_number}", response_class=HTMLResponse)
def page_review(
    story_key: str,
    page_number: int,
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    store: TemplateDep,
    error: str = "",
) -> HTMLResponse:
    """One page's candidates, each with its slot overlay and Approve state."""
    operator_or_404(container, session)
    seed = _seed_or_404(story_key)
    _page_or_404(page_number)
    try:
        pages = store.pages(story_key)
        plates = store.plates(story_key, page_number)
    except UnknownTemplate:
        raise HTTPException(status_code=404, detail="Not Found") from None
    page = pages.get(page_number)
    candidates = [_candidate(plate, store, page_number) for plate in plates]
    plan = _plan(seed, page_number)
    return render_admin(
        request,
        "admin_plate_review.html",
        {
            **admin_chrome(container, session, request, "/admin/plates"),
            "story": seed,
            "page": {
                "number": page_number,
                "label": _page_label(page_number, page.title if page else ""),
                "title": page.title if page else "",
                "prose": page.prose if page else "",
                "cast_label": _cast_label(seed.star, page),
            },
            "candidates": candidates,
            "plan": {"prompt": plan.prompt, "slots": _slot_rows(plan.slots)},
            "band_percent": f"{COVER_TITLE_BAND_FRACTION * 100:.2f}",
            "prev_page": page_number - 1 if page_number > min(PAGE_NUMBERS) else None,
            "next_page": page_number + 1 if page_number < max(PAGE_NUMBERS) else None,
            "error": error,
        },
    )


# --- the actions --------------------------------------------------------------


@router.post("/plates/{story_key}/gate")
def set_start_gate(
    story_key: str,
    container: ContainerDep,
    session: CurrentSessionDep,
    store: TemplateDep,
    required: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Turn the story's start gate on or off.

    `required=on` (the checkbox ticked) means a book of this story can start
    only on a complete template version; anything else turns the gate off.
    Books already started are untouched either way: the gate only decides
    whether the next one may begin.
    """
    operator = operator_or_404(container, session)
    _seed_or_404(story_key)
    on = required == "on"
    store.set_requires_template(story_key, on)
    logger.info("admin.start_gate story=%s required=%s operator=%s", story_key, on, operator)
    return RedirectResponse("/admin/plates", status_code=303)


@router.post("/plates/{story_key}/{page_number}/{plate_id}/approve")
def approve(
    story_key: str,
    page_number: int,
    plate_id: str,
    container: ContainerDep,
    session: CurrentSessionDep,
    store: TemplateDep,
) -> RedirectResponse:
    """Approve one plate, refusing a cover whose title band is too busy."""
    operator = operator_or_404(container, session)
    _seed_or_404(story_key)
    _page_or_404(page_number)
    plates = _plates_on(store, story_key, page_number, plate_id)
    if page_number == COVER_PAGE:
        busy = next((p for p in plates if p.id == plate_id), None)
        if busy is not None and busy.status != PlateStatus.APPROVED:
            ok, reason = _band(store.plate_image(plate_id)[0])
            if not ok:
                logger.info("admin.plate_refused cover=%s reason=title_band", plate_id)
                return RedirectResponse(
                    _page_url(story_key, page_number, error=reason), status_code=303
                )
    try:
        version = store.approve_plate(plate_id, operator=operator)
    except PlateNotApprovable as exc:
        logger.info("admin.plate_refused plate=%s reason=%s", plate_id, exc)
        return RedirectResponse(
            _page_url(story_key, page_number, error=str(exc)), status_code=303
        )
    logger.info(
        "admin.plate_approved story=%s page=%d plate=%s version=%s operator=%s",
        story_key,
        page_number,
        plate_id,
        version or "",
        operator,
    )
    return RedirectResponse(_page_url(story_key, page_number), status_code=303)


@router.post("/plates/{story_key}/{page_number}/reroll")
def reroll(
    story_key: str,
    page_number: int,
    container: ContainerDep,
    session: CurrentSessionDep,
    store: TemplateDep,
    prompt: Annotated[str, Form()] = "",
    change: Annotated[str, Form()] = "",
    back: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Paint one more candidate for a page. Never touches an approved plate.

    Two ways in. A `change` note — "make the light warmer" — is added to the
    page's planned prompt, so an operator corrects the one thing they did not
    like instead of rewriting twenty paragraphs of spec. A `prompt` is the
    whole text, for when the plate needs more than a correction. With neither,
    the planned prompt is painted again.
    """
    operator_or_404(container, session)
    seed = _seed_or_404(story_key)
    _page_or_404(page_number)
    plan = _plan(seed, page_number)
    note = " ".join(change.split())
    text = prompt.strip() or plan.prompt
    if note:
        text = f"{plan.prompt}\n\nCHANGE REQUESTED: {note}"
    try:
        with cost_scope(purpose="plate"):
            image = container.provider.generate(
                ImageRequest(prompt=text, aspect_ratio=str(PLATE_SHAPE), label="plate")
            )
        plate_id = store.add_plate(
            story_key,
            page_number,
            image=image.data,
            media_type=image.media_type,
            prompt=text,
            slots=plan.slots,
        )
    except TemplateError as exc:
        logger.info(
            "admin.plate_reroll_failed story=%s page=%d reason=%s",
            story_key,
            page_number,
            exc,
        )
        return RedirectResponse(
            _page_url(story_key, page_number, error=str(exc)), status_code=303
        )
    logger.info(
        "admin.plate_rerolled story=%s page=%d plate=%s provider=%s changed=%s",
        story_key,
        page_number,
        plate_id,
        container.provider.name,
        bool(note),
    )
    if back == "sheet":
        return RedirectResponse(
            f"/admin/plates/{story_key}?{urlencode({'painted': page_number})}",
            status_code=303,
        )
    return RedirectResponse(_page_url(story_key, page_number), status_code=303)


@router.post("/plates/{story_key}/paint")
def paint_next(
    story_key: str,
    container: ContainerDep,
    session: CurrentSessionDep,
    store: TemplateDep,
) -> RedirectResponse:
    """Paint one missing plate, then come back (the no-JavaScript path).

    One image call per press: a whole story is 21 calls, which is minutes, so
    it is one plate per request and the browser repeats it. The sheet says how
    many are painted, so a reload never loses the operator's place.
    """
    operator_or_404(container, session)
    seed = _seed_or_404(story_key)
    page, _plate_id, _remaining = _paint_next(container, store, seed)
    query = {"painted": page} if page is not None else {"finished": "1"}
    return RedirectResponse(f"/admin/plates/{story_key}?{urlencode(query)}", status_code=303)


@router.post("/plates/{story_key}/paint.json")
def paint_next_json(
    story_key: str,
    container: ContainerDep,
    session: CurrentSessionDep,
    store: TemplateDep,
) -> JSONResponse:
    """The same one plate, counted: what the browser's repeat loop reads."""
    operator_or_404(container, session)
    seed = _seed_or_404(story_key)
    page, plate_id, remaining = _paint_next(container, store, seed)
    return JSONResponse(
        {
            "page": page,
            "plate_id": plate_id,
            "remaining": remaining,
            "total": len(PAGE_NUMBERS),
        }
    )


@router.post("/plates/{story_key}/{page_number}/{plate_id}/retire")
def retire(
    story_key: str,
    page_number: int,
    plate_id: str,
    container: ContainerDep,
    session: CurrentSessionDep,
    store: TemplateDep,
) -> RedirectResponse:
    """Take a plate out of future versions. Old versions keep it."""
    operator_or_404(container, session)
    _seed_or_404(story_key)
    _page_or_404(page_number)
    _plates_on(store, story_key, page_number, plate_id)
    try:
        store.retire_plate(plate_id)
    except UnknownTemplate:
        raise HTTPException(status_code=404, detail="Not Found") from None
    logger.info(
        "admin.plate_retired story=%s page=%d plate=%s", story_key, page_number, plate_id
    )
    return RedirectResponse(_page_url(story_key, page_number), status_code=303)


# --- shared shaping -----------------------------------------------------------


def _story_progress(seed: StorySeed, store: TemplateStore) -> dict[str, Any]:
    try:
        plates = store.plates(seed.key)
        pages = store.pages(seed.key)
    except UnknownTemplate:
        return {
            "key": seed.key,
            "title": seed.title,
            "seeded": False,
            "approved": 0,
            "drafts": 0,
            "version": None,
            "requires_template": False,
            "can_start": False,
        }
    state = store.story_state(seed.key)
    return {
        "key": seed.key,
        "title": seed.title,
        "seeded": bool(pages),
        "approved": sum(1 for p in plates if p.status == PlateStatus.APPROVED),
        "drafts": sum(1 for p in plates if p.status == PlateStatus.DRAFT),
        "version": state.version_number,
        "requires_template": state.requires_template,
        "can_start": state.can_start,
    }


def _next_page(store: TemplateStore, story_key: str) -> tuple[int | None, int]:
    """The first page with no candidate, and how many pages still need one.

    A retired plate does not count: retiring is how a plate leaves, and the
    page then needs a new candidate. An approved plate does count — it is the
    page's current answer, and re-rolling it is a deliberate act for one page,
    not something a bulk paint should ever do behind the operator's back.
    """
    painted = {
        plate.page_number
        for plate in store.plates(story_key)
        if plate.status != PlateStatus.RETIRED
    }
    missing = [number for number in PAGE_NUMBERS if number not in painted]
    return (missing[0] if missing else None), len(missing)


def _paint_next(
    container: Container, store: TemplateStore, seed: StorySeed
) -> tuple[int | None, str, int]:
    """Paint the planned plate for the next unpainted page. One image call."""
    with _paint_lock(seed.key):
        number, remaining = _next_page(store, seed.key)
        if number is None:
            return None, "", 0
        plan = _plan(seed, number)
        with cost_scope(purpose="plate"):
            image = container.provider.generate(
                ImageRequest(prompt=plan.prompt, aspect_ratio=str(PLATE_SHAPE), label="plate")
            )
        try:
            plate_id = store.add_plate(
                seed.key,
                number,
                image=image.data,
                media_type=image.media_type,
                prompt=plan.prompt,
                slots=plan.slots,
            )
        except UnknownTemplate:
            raise HTTPException(status_code=404, detail="Not Found") from None
    logger.info(
        "admin.plate_painted story=%s page=%d plate=%s provider=%s",
        seed.key,
        number,
        plate_id,
        container.provider.name,
    )
    return number, plate_id, remaining - 1


def _plates_on(
    store: TemplateStore, story_key: str, page_number: int, plate_id: str
) -> list[PlateSummary]:
    """The page's plates, refusing a plate that belongs to some other page."""
    try:
        plates = store.plates(story_key, page_number)
    except UnknownTemplate:
        raise HTTPException(status_code=404, detail="Not Found") from None
    if not any(plate.id == plate_id for plate in plates):
        raise HTTPException(status_code=404, detail="Not Found")
    return plates


def _plan(seed: StorySeed, page_number: int) -> PlatePlan:
    return next(plan for plan in plate_plans(seed) if plan.page_number == page_number)


def _candidate(plate: PlateSummary, store: TemplateStore, page_number: int) -> dict[str, Any]:
    """One candidate, with the cover's band verdict the button obeys."""
    view = _plate_view(plate)
    view["can_approve"] = True
    view["reason"] = ""
    if page_number == COVER_PAGE and plate.status == PlateStatus.DRAFT:
        ok, reason = _band(store.plate_image(plate.id)[0])
        view["can_approve"] = ok
        view["reason"] = reason
    return view


# --- the fixed portraits of supporting characters ---------------------------
#
# A book whose visitor gave no photo of a supporting character is drawn with
# that character's approved portrait, pinned by its template version. These
# screens add candidates and settle which one is approved, one character at a
# time. The star has no fixed portrait: their face is the book's own.

PORTRAIT_UPLOAD_LIMIT = 8 * 1024 * 1024
_PORTRAIT_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})


def _cast_or_404(story_key: str, cast_key: str) -> StorySeed:
    seed = _seed_or_404(story_key)
    if cast_key == seed.star or cast_key not in seed.cast:
        raise HTTPException(status_code=404, detail="Not Found")
    return seed


def _cast_url(story_key: str, cast_key: str, *, error: str = "") -> str:
    path = f"/admin/plates/{story_key}/cast/{cast_key}"
    return f"{path}?{urlencode({'error': error})}" if error else path


def _portrait_view(portrait: PortraitSummary) -> dict[str, Any]:
    base = f"/admin/plates/{portrait.story_key}/cast/{portrait.cast_key}/{portrait.id}"
    return {
        "id": portrait.id,
        "status": portrait.status,
        "url": f"{base}.png",
        "approve_url": f"{base}/approve",
        "retire_url": f"{base}/retire",
        "approved_by": portrait.approved_by,
        "created": portrait.created_at.strftime("%Y-%m-%d %H:%M"),
    }


def _portrait_on_cast(
    store: TemplateStore, story_key: str, cast_key: str, portrait_id: str
) -> None:
    """Answer 404 unless the portrait belongs to this story and character."""
    if not any(p.id == portrait_id for p in store.portraits(story_key, cast_key)):
        raise HTTPException(status_code=404, detail="Not Found")


@router.get("/plates/{story_key}/cast/{cast_key}", response_class=HTMLResponse)
def cast_portraits(
    story_key: str,
    cast_key: str,
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    store: TemplateDep,
    error: str = "",
) -> HTMLResponse:
    """One supporting character's candidate portraits, with the approved one first."""
    operator_or_404(container, session)
    seed = _cast_or_404(story_key, cast_key)
    portraits = [_portrait_view(p) for p in store.portraits(story_key, cast_key)]
    return render_admin(
        request,
        "admin_portraits.html",
        {
            **admin_chrome(container, session, request, "/admin/plates"),
            "story_key": story_key,
            "story_title": seed.title,
            "cast_key": cast_key,
            "label": seed.cast[cast_key].label,
            "portraits": portraits,
            "error": error,
        },
    )


@router.post("/plates/{story_key}/cast/{cast_key}")
async def upload_portrait(
    story_key: str,
    cast_key: str,
    container: ContainerDep,
    session: CurrentSessionDep,
    store: TemplateDep,
    file: Annotated[UploadFile, File()],
) -> RedirectResponse:
    """Add a candidate portrait as a draft. Nothing is approved by uploading."""
    operator_or_404(container, session)
    _cast_or_404(story_key, cast_key)
    data = await file.read(PORTRAIT_UPLOAD_LIMIT + 1)
    if len(data) > PORTRAIT_UPLOAD_LIMIT:
        return RedirectResponse(
            _cast_url(story_key, cast_key, error="That picture is larger than 8 MB."),
            status_code=303,
        )
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as decoded:
            fmt = decoded.format
    except Exception:
        fmt = None
    if fmt not in _PORTRAIT_FORMATS:
        return RedirectResponse(
            _cast_url(
                story_key, cast_key, error="That file is not a PNG, JPEG or WEBP picture."
            ),
            status_code=303,
        )
    try:
        store.add_portrait(story_key, cast_key, image=data, media_type=Image.MIME[str(fmt)])
    except TemplateError as exc:
        return RedirectResponse(
            _cast_url(story_key, cast_key, error=str(exc)), status_code=303
        )
    return RedirectResponse(_cast_url(story_key, cast_key), status_code=303)


@router.get("/plates/{story_key}/cast/{cast_key}/{portrait_id}.png")
def portrait_image(
    story_key: str,
    cast_key: str,
    portrait_id: str,
    container: ContainerDep,
    session: CurrentSessionDep,
    store: TemplateDep,
) -> Response:
    """A portrait's bytes, for the operator's own screen only."""
    operator_or_404(container, session)
    _cast_or_404(story_key, cast_key)
    _portrait_on_cast(store, story_key, cast_key, portrait_id)
    image, media_type = store.portrait_image(portrait_id)
    return no_store(image, media_type=media_type)


@router.post("/plates/{story_key}/cast/{cast_key}/{portrait_id}/approve")
def approve_portrait(
    story_key: str,
    cast_key: str,
    portrait_id: str,
    container: ContainerDep,
    session: CurrentSessionDep,
    store: TemplateDep,
) -> RedirectResponse:
    """Approve one portrait, retiring the character's previous one."""
    operator = operator_or_404(container, session)
    _cast_or_404(story_key, cast_key)
    _portrait_on_cast(store, story_key, cast_key, portrait_id)
    try:
        version = store.approve_portrait(portrait_id, operator=operator)
    except PlateNotApprovable as exc:
        logger.info("admin.portrait_refused portrait=%s reason=%s", portrait_id, exc)
        return RedirectResponse(
            _cast_url(story_key, cast_key, error=str(exc)), status_code=303
        )
    logger.info(
        "admin.portrait_approved story=%s cast=%s portrait=%s version=%s operator=%s",
        story_key,
        cast_key,
        portrait_id,
        version or "",
        operator,
    )
    return RedirectResponse(_cast_url(story_key, cast_key), status_code=303)


@router.post("/plates/{story_key}/cast/{cast_key}/{portrait_id}/retire")
def retire_portrait(
    story_key: str,
    cast_key: str,
    portrait_id: str,
    container: ContainerDep,
    session: CurrentSessionDep,
    store: TemplateDep,
) -> RedirectResponse:
    """Take a portrait out of future versions. Versions already written keep it."""
    operator = operator_or_404(container, session)
    _cast_or_404(story_key, cast_key)
    _portrait_on_cast(store, story_key, cast_key, portrait_id)
    store.retire_portrait(portrait_id)
    logger.info(
        "admin.portrait_retired story=%s cast=%s portrait=%s operator=%s",
        story_key,
        cast_key,
        portrait_id,
        operator,
    )
    return RedirectResponse(_cast_url(story_key, cast_key), status_code=303)
