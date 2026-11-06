"""What an image's bytes are, decided once.

Image providers answer with PNG or JPEG, sometimes regardless of what was
asked for, and nothing in the app may trust a label. Five modules used to ask
this question with their own copy of the same two-line function; they had
already drifted (one accepted a bare ``\\x89PNG`` prefix, one did not). This is
the one answer.

``extension_for`` defaults to ``.png`` for anything that is not a JPEG, which is
what every caller wants for a filename it chose itself. ``media_type_for``
answers ``application/octet-stream`` for bytes that are neither, so a response
never claims a type the bytes do not have.
"""

from __future__ import annotations

__all__ = ["extension_for", "image_filename", "media_type_for"]


def extension_for(data: bytes) -> str:
    """The extension these bytes deserve: ``.jpg`` only for a real JPEG."""
    return ".jpg" if data.startswith(b"\xff\xd8") else ".png"


def media_type_for(data: bytes) -> str:
    """The media type these bytes really are, or ``application/octet-stream``."""
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    return "application/octet-stream"


def image_filename(stem: str, data: bytes) -> str:
    """``stem`` plus the extension the bytes deserve — never a claim."""
    return f"{stem}{extension_for(data)}"
