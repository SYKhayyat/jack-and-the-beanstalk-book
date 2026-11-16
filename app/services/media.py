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

import base64
import hashlib
import io
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import TYPE_CHECKING, TypeVar, cast

if TYPE_CHECKING:
    from PIL import Image

__all__ = [
    "FAST_PNG_LEVEL",
    "PAGE_JPEG_QUALITY",
    "ShrunkCache",
    "encode_page",
    "extension_for",
    "image_filename",
    "jpeg_data_url",
    "judge_jpeg",
    "media_type_for",
    "remember_judge_copy",
]

#: Pictures sent to a judging model (the reviewer, the decision judge) are at
#: most this many pixels on their long side. They read composition, people and
#: lettering, none of which needs more, and the bill is per input token.
JUDGE_MAX_SIDE = 1024

_V = TypeVar("_V")

#: The zlib level for PNGs the app writes on a render's way through. Still
#: lossless; about 15% larger than Pillow's default 6 and about seven times
#: faster to write (0.35 s against 2.3 s for one 1536x1024 page on a dev
#: machine, and ten times both on the free plan's sliver of a CPU, where the
#: default encode was the costliest step of a whole render).
FAST_PNG_LEVEL = 1

#: JPEG quality of a stored book picture (a page or the cover after its plate
#: is pasted back). With no chroma subsampling a red cloak's edge stays sharp,
#: and at this quality the picture is indistinguishable from a PNG of it at
#: print size. Encoding it costs a tenth of even the fast PNG (about 12 ms
#: against 110 ms for a 1536 px page on a dev machine), and the screen PDF
#: embeds it as it is instead of decoding and re-encoding every page.
PAGE_JPEG_QUALITY = 90


class ShrunkCache:
    """The last few downscaled copies of reference pictures, by content.

    A book sends the same portrait, photograph and cast faces with every page
    and every review, and the same plate with each of a page's attempts.
    Shrinking each one again per call was the same Pillow work, on the same
    bytes, up to sixty times a book. Keyed by a digest of the bytes rather than
    the bytes, so the cache holds only the small results; bounded, so a busy
    server holds at most `size` of them. Thread-safe: the calls that use it
    run on worker threads.
    """

    def __init__(self, size: int = 48) -> None:
        self._size = size
        self._entries: OrderedDict[Hashable, object] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, data: bytes, params: Hashable, make: Callable[[], _V]) -> _V:
        key = (hashlib.blake2b(data, digest_size=16).digest(), params)
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return cast("_V", self._entries[key])
        # Made outside the lock: two threads may shrink the same picture once
        # each, which is cheaper than every other picture waiting on one.
        value = make()
        self.put(data, params, value)
        return value

    def put(self, data: bytes, params: Hashable, value: object) -> None:
        """Remember `value` for `data`, made by a caller that already had it."""
        key = (hashlib.blake2b(data, digest_size=16).digest(), params)
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._size:
                self._entries.popitem(last=False)


_JUDGE_COPIES = ShrunkCache()


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


def judge_jpeg(image: bytes, *, max_side: int = JUDGE_MAX_SIDE) -> bytes:
    """The picture as a JPEG for a judging model, at most `max_side` on its long side.

    Remembered by content (`ShrunkCache`): the portrait and the plate go with
    every review of a book, and are shrunk once.
    """
    return _JUDGE_COPIES.get(image, ("judge", max_side), lambda: _judge_jpeg(image, max_side))


def remember_judge_copy(image: bytes, picture: Image.Image) -> None:
    """File the judge's copy of `image`, made from `picture`, its decoded pixels.

    A page is decoded once on its way through a render (`page_picture`); the
    copy both judges are later sent is made from those pixels there, so
    neither judge decodes the page again.
    """
    _JUDGE_COPIES.put(image, ("judge", JUDGE_MAX_SIDE), _shrunk_jpeg(picture, JUDGE_MAX_SIDE))


def encode_page(picture: Image.Image) -> bytes:
    """A book picture as the JPEG it is stored as (`PAGE_JPEG_QUALITY`)."""
    buffer = io.BytesIO()
    # Baseline on purpose (no optimize=, no progressive=): Pillow then encodes
    # in chunks that release the GIL, and the event loop keeps running.
    picture.convert("RGB").save(
        buffer, format="JPEG", quality=PAGE_JPEG_QUALITY, subsampling=0
    )
    return buffer.getvalue()


def _judge_jpeg(image: bytes, max_side: int) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(image)) as opened:
        # A JPEG is decoded straight at the size it is shrunk to.
        opened.draft("RGB", (max_side, max_side))
        return _shrunk_jpeg(opened.convert("RGB"), max_side)


def _shrunk_jpeg(picture: Image.Image, max_side: int) -> bytes:
    copy = picture.convert("RGB") if picture.mode != "RGB" else picture.copy()
    copy.thumbnail((max_side, max_side))
    buffer = io.BytesIO()
    copy.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def jpeg_data_url(image: bytes, *, max_side: int = JUDGE_MAX_SIDE) -> str:
    """The picture as an inline JPEG data URL, at most `max_side` on its long side."""
    encoded = base64.b64encode(judge_jpeg(image, max_side=max_side)).decode("ascii")
    return "data:image/jpeg;base64," + encoded
