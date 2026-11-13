"""Blob storage: pictures and PDFs live in Cloudflare R2, the database keeps a pointer.

Why: a Postgres free tier is a few hundred MB, and four books of full-size
plates, pages and PDFs filled it. The bytes belong in object storage; the
database keeps only the key.

How it plugs in without touching a store: `BlobRef` is a column type. Writing
a `bytes` value uploads it and stores a short pointer in the column; reading
a pointer fetches the bytes. Every store keeps reading and writing `bytes`
exactly as before. With no R2 configured the type is a pass-through, so local
development and the test suite keep their blobs inline, and a database that
still holds inline blobs reads correctly (migrate them with
`python -m app.scripts.migrate_blobs`).

Properties worth knowing:

* **Content-addressed.** The key is the SHA-256 of the original bytes, so the
  same picture is one object however many rows point at it, and a retry is an
  idempotent overwrite, not a second object.
* **Lossless.** A PNG is stored as lossless WebP (smaller, pixel-identical) and
  decoded back to PNG on read, so the PDF and the image provider see the same
  pixels. A write that does not round-trip pixel-for-pixel, or does not get
  smaller, is stored as the original bytes. JPEGs, WebP variants and PDFs are
  stored as they are: re-encoding a JPEG would be lossy.
* **Private.** The bucket is private and nothing here makes an object public
  or CDN-cacheable; the app serves pictures behind login with `private`
  caching headers.
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
from collections import OrderedDict
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Protocol

from PIL import Image
from sqlalchemy import LargeBinary
from sqlalchemy.types import TypeDecorator

from app.event_loop_guard import check_blocking
from app.transcoding import transcoding

if TYPE_CHECKING:
    from app.config import Settings

__all__ = [
    "BlobRef",
    "BlobStore",
    "MemoryBlobStore",
    "R2BlobStore",
    "blob_store",
    "decode_pointer",
    "is_pointer",
    "same_content",
    "set_blob_store",
    "stash",
]

logger = logging.getLogger(__name__)

#: A stored pointer. No picture (PNG, JPEG, WebP) or PDF begins with it, so a
#: column holding an old inline blob is told apart from one holding a pointer.
_PREFIX = b"R2REF1:"
_WEBP = ".webp"
_RAW = ".bin"
#: Decoded bytes kept in the process, so a plate read for every page of a book
#: is fetched and decoded once, not once per page.
_CACHE_BYTES = 48 * 1024 * 1024
#: Effort 1 of 6: a third of the time of the default effort for about 2% more
#: bytes on an illustration, and on a server with a fraction of a CPU the time
#: is what matters (see `app.transcoding`).
_LOSSLESS: dict[str, Any] = {"lossless": True, "quality": 50, "method": 1, "exact": True}


class ObjectClient(Protocol):
    """The two calls the store needs from an S3-compatible service."""

    def put_object(self, **kwargs: Any) -> Any: ...
    def get_object(self, **kwargs: Any) -> Any: ...


class BlobStore(Protocol):
    def put(self, data: bytes) -> bytes:
        """Store `data`; return the pointer to keep in the database."""
        ...

    def get(self, pointer: bytes) -> bytes:
        """The original bytes a pointer was made from."""
        ...

    def keys(self) -> Iterator[str]:
        """Every object key in the bucket (for the orphan sweep)."""
        ...

    def delete(self, key: str) -> None: ...


def same_content(original: bytes, restored: bytes) -> bool:
    """Whether `restored` is `original`: the same bytes, or for a PNG the same pixels.

    A PNG that went through lossless WebP comes back re-encoded, so its bytes
    differ while every pixel is identical.
    """
    if original == restored:
        return True
    try:
        with Image.open(io.BytesIO(original)) as a, Image.open(io.BytesIO(restored)) as b:
            return bool(a.mode == b.mode and a.size == b.size and a.tobytes() == b.tobytes())
    except Exception:
        return False


def is_pointer(value: bytes) -> bool:
    return value.startswith(_PREFIX)


def decode_pointer(pointer: bytes) -> str:
    """The object key a pointer names."""
    return pointer[len(_PREFIX) :].decode("ascii")


class _Codec:
    """Shared by every store: pick the encoding, build keys, undo the encoding."""

    def __init__(self) -> None:
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cached = 0
        self._lock = threading.Lock()

    @staticmethod
    def encode(data: bytes) -> tuple[str, bytes]:
        """(key, bytes to store) for `data`."""
        name = f"blobs/{hashlib.sha256(data).hexdigest()}"
        packed = _lossless_webp(data)
        if packed is not None:
            return name + _WEBP, packed
        return name + _RAW, data

    @staticmethod
    def decode(key: str, stored: bytes) -> bytes:
        if not key.endswith(_WEBP):
            return stored
        with transcoding(), Image.open(io.BytesIO(stored)) as image:
            image.load()
            out = io.BytesIO()
            image.save(out, format="PNG", compress_level=1)
        return out.getvalue()

    def remember(self, key: str, data: bytes) -> None:
        if len(data) > _CACHE_BYTES // 4:
            return
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return
            self._cache[key] = data
            self._cached += len(data)
            while self._cached > _CACHE_BYTES:
                _, dropped = self._cache.popitem(last=False)
                self._cached -= len(dropped)

    def recall(self, key: str) -> bytes | None:
        with self._lock:
            data = self._cache.get(key)
            if data is not None:
                self._cache.move_to_end(key)
            return data


def _lossless_webp(data: bytes) -> bytes | None:
    """`data` as lossless WebP when it is a PNG that this makes smaller, else None."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    try:
        with transcoding(), Image.open(io.BytesIO(data)) as source:
            source.load()
            if source.mode not in ("RGB", "RGBA", "L", "LA"):
                source = source.convert("RGBA" if "A" in source.getbands() else "RGB")
            out = io.BytesIO()
            source.save(out, format="WEBP", **_LOSSLESS)
            packed = out.getvalue()
            if len(packed) >= len(data):
                return None
            with Image.open(io.BytesIO(packed)) as check:
                check.load()
                if check.mode != source.mode or check.tobytes() != source.tobytes():
                    return None
            return packed
    except Exception:
        logger.warning("blobs.webp_skipped storing the original PNG", exc_info=True)
        return None


class R2BlobStore(_Codec):
    """Cloudflare R2 through its S3-compatible API."""

    def __init__(self, client: ObjectClient, bucket: str) -> None:
        super().__init__()
        self._client = client
        self._bucket = bucket

    @classmethod
    def connect(
        cls, *, account_id: str, bucket: str, access_key_id: str, secret_access_key: str
    ) -> R2BlobStore:
        import boto3  # type: ignore[import-untyped]
        from botocore.config import Config  # type: ignore[import-untyped]

        # Accept the bare account id or a pasted endpoint host/URL.
        account = account_id.strip().removeprefix("https://").split(".")[0].split("/")[0]
        client = boto3.client(
            "s3",
            endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=Config(
                retries={"max_attempts": 4, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
                max_pool_connections=20,
            ),
        )
        return cls(client, bucket)

    def put(self, data: bytes) -> bytes:
        check_blocking("an object-store upload")
        key, stored = self.encode(data)
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=stored,
            # Private on purpose: these are children's portraits.
            CacheControl="private, no-store",
        )
        self.remember(key, data)
        return _PREFIX + key.encode("ascii")

    def get(self, pointer: bytes) -> bytes:
        key = decode_pointer(pointer)
        cached = self.recall(key)
        if cached is not None:
            return cached
        check_blocking("an object-store download")
        stored = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        data = self.decode(key, stored)
        self.remember(key, data)
        return data

    def keys(self) -> Iterator[str]:
        paginator = self._client.get_paginator("list_objects_v2")  # type: ignore[attr-defined]
        for page in paginator.paginate(Bucket=self._bucket, Prefix="blobs/"):
            for item in page.get("Contents", []):
                yield item["Key"]

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)  # type: ignore[attr-defined]


class MemoryBlobStore(_Codec):
    """A dict standing in for the bucket: the same encoding, no network."""

    def __init__(self) -> None:
        super().__init__()
        self.objects: dict[str, bytes] = {}

    def put(self, data: bytes) -> bytes:
        # Checked like the bucket, so a test catches a handler that would
        # block on a real upload.
        check_blocking("an object-store upload")
        key, stored = self.encode(data)
        self.objects[key] = stored
        return _PREFIX + key.encode("ascii")

    def get(self, pointer: bytes) -> bytes:
        check_blocking("an object-store download")
        key = decode_pointer(pointer)
        return self.decode(key, self.objects[key])

    def keys(self) -> Iterator[str]:
        yield from list(self.objects)

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


_store: BlobStore | None = None


def set_blob_store(store: BlobStore | None) -> None:
    """Install (or, with None, remove) the process's blob store."""
    global _store
    _store = store


def blob_store() -> BlobStore | None:
    return _store


def stash(data: bytes) -> bytes:
    """Put the bytes in the blob store now and return the pointer to write.

    Call it before opening the transaction that writes the row. `BlobRef`
    would otherwise encode and upload while binding the statement, holding a
    database connection (and, on SQLite, the write lock) for the many seconds
    a lossless encode and an upload take. A pointer passes through `BlobRef`
    untouched. With no store installed the bytes are returned as they are.
    """
    store = _store
    if store is None or is_pointer(data):
        return data
    return store.put(bytes(data))


class BlobRef(TypeDecorator[bytes]):
    """A bytes column whose content lives in the blob store when there is one."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: bytes | None, _dialect: Any) -> bytes | None:
        if value is None:
            return None
        value = bytes(value)
        store = _store
        if store is None or is_pointer(value):
            return value
        return store.put(value)

    def process_result_value(self, value: bytes | None, _dialect: Any) -> bytes | None:
        if value is None:
            return None
        value = bytes(value)
        if not is_pointer(value):
            return value
        store = _store
        if store is None:
            raise RuntimeError(
                "this database holds pictures in R2 but no R2 settings are configured "
                "(R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY)"
            )
        return store.get(value)


def install_blob_store(settings: Settings) -> None:
    """Point the process at R2 when it is configured, else at nothing (inline).

    Every entry point that touches the database calls this first — the web app
    and the command-line scripts alike — so no path can write inline blobs by
    accident while the app is configured for R2.
    """
    if settings.r2_enabled:
        set_blob_store(
            R2BlobStore.connect(
                account_id=settings.r2_account_id,
                bucket=settings.r2_bucket,
                access_key_id=settings.r2_access_key_id,
                secret_access_key=settings.r2_secret_access_key,
            )
        )
    else:
        set_blob_store(None)
