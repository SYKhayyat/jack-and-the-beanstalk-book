"""Serving stored images and metadata."""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse

from app.dependencies import StoreDep

__all__ = ["router"]

router = APIRouter(prefix="/media", tags=["media"])

_STEMS = frozenset({"cartoon", "scene"})


def _not_found(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"error": "not_found", "detail": detail},
    )


# Registered before the image route so "meta.json" is not swallowed by the
# "/{generation_id}/{stem}" pattern as a stem named "meta.json".
@router.get("/{generation_id}/meta.json")
def get_meta(generation_id: str, store: StoreDep) -> Response:
    """Return the JSON sidecar: prompts, caption, provider and model used."""
    meta = store.read_meta(generation_id)
    if meta is None:
        return _not_found("No metadata for that generation.")
    return JSONResponse(meta)


@router.get("/{generation_id}/candidate-{index}")
def get_candidate(generation_id: str, index: int, store: StoreDep) -> Response:
    """Return one portrait alternative from a multi-candidate draft."""
    if index < 1:
        return _not_found("Candidate 0 is served as 'cartoon'.")
    stem = f"candidate-{index}"
    data = store.read_image(generation_id, stem)
    if data is None:
        return _not_found(f"That draft has no candidate {index}.")
    return _image_response(data)


@router.get("/{generation_id}/{stem}")
def get_image(generation_id: str, stem: str, store: StoreDep) -> Response:
    """Return a stored image. `stem` is either `cartoon` or `scene`."""
    if stem not in _STEMS:
        return _not_found(f"Unknown image {stem!r}. Use 'cartoon' or 'scene'.")

    data = store.read_image(generation_id, stem)
    if data is None:
        return _not_found("That image no longer exists. Generate it again.")
    return _image_response(data)


def _image_response(data: bytes) -> Response:
    return Response(
        content=data,
        media_type=_detect_media_type(data),
        # These files contain identifiable faces and the URL is a bearer token.
        # Do not let a shared browser or proxy cache retain them indefinitely.
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _detect_media_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    return "application/octet-stream"
