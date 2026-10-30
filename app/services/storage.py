"""Filesystem persistence for generation artefacts.

Kept behind a small interface so the pipeline never touches paths. In tests it is
backed by a tmp dir; in production by `settings.storage_dir`.

Note for deployment: Render's filesystem is ephemeral, so artefacts survive only
until the container is replaced. A completed one-shot result is returned in the
same response, but the optional review flow can lose its draft across a restart;
production deployments that need durable drafts should replace this store with
object storage or a database.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from time import time

from app.models import CartoonSettings

__all__ = ["Draft", "GenerationStore", "StoredGeneration"]

logger = logging.getLogger(__name__)


class StoredGeneration:
    """Where one generation's artefacts live on disk."""

    def __init__(self, root: Path, generation_id: str) -> None:
        self.root = root
        self.generation_id = generation_id

    @property
    def directory(self) -> Path:
        return self.root / self.generation_id

    def path(self, filename: str) -> Path:
        return self.directory / filename

    def read_candidates(self) -> list[bytes]:
        """Every stored portrait alternative, in order. Candidate 0 is the default."""
        found = []
        primary = self.read_image_file("cartoon")
        if primary is not None:
            found.append(primary)
        index = 1
        while (extra := self.read_image_file(f"candidate-{index}")) is not None:
            found.append(extra)
            index += 1
        return found

    def candidate_url(self, index: int) -> str:
        """URL for one portrait alternative."""
        stem = "cartoon" if index == 0 else f"candidate-{index}"
        return f"/media/{self.generation_id}/{stem}"

    def read_image_file(self, stem: str) -> bytes | None:
        """Read `stem.png` or `stem.jpg` if present."""
        for suffix in ("png", "jpg"):
            candidate = self.directory / f"{stem}.{suffix}"
            if candidate.is_file():
                return candidate.read_bytes()
        return None

    def to_urls(self) -> dict[str, str]:
        """The draft's URLs: the approved-looking cartoon and its alternatives."""
        base = f"/media/{self.generation_id}"
        return {"cartoon": f"{base}/cartoon"}


@dataclass(frozen=True, slots=True)
class Draft:
    """A finished pass 1 that is waiting to be composed into a scene.

    Holds the normalised photo as well as the cartoon portrait, because pass 2
    sends both as references. Persisting the photo is what makes "review the
    cartoon, tweak it, then compose" possible without re-uploading.
    """

    generation_id: str
    settings: CartoonSettings
    cartoon_prompt: str
    #: One or more portrait alternatives from the same API call. The first is the
    #: default; the user may pick another when composing.
    candidates: tuple[bytes, ...]
    photo_data: bytes
    photo_media_type: str
    provider: str
    model: str
    #: The media type of each candidate, in the same order. Empty on a draft
    #: written before this field existed, where the bytes are sniffed instead.
    candidate_media_types: tuple[str, ...] = ()

    @property
    def cartoon_image(self) -> bytes:
        """The default portrait: candidate zero."""
        return self.candidates[0]

    @property
    def cartoon_media_type(self) -> str:
        """The default portrait's own media type — never assumed to be PNG.

        Providers may answer with JPEG (Agnes explicitly can), so a book page
        must send the type the bytes actually are, not a hardcoded one.
        """
        if self.candidate_media_types:
            return self.candidate_media_types[0]
        return _media_type_for(self.cartoon_image)

    def with_candidate(self, index: int) -> Draft:
        """A view of this draft with a different portrait chosen.

        Raises:
            IndexError: if the index is out of range, which the route turns
                into a 422 rather than a confusing downstream failure.
        """
        if not 0 <= index < len(self.candidates):
            raise IndexError(
                f"candidate {index} out of range; this draft has {len(self.candidates)}"
            )
        media_types = (
            (self.candidate_media_types[index],)
            if len(self.candidate_media_types) == len(self.candidates)
            else ()
        )
        return replace(
            self, candidates=(self.candidates[index],), candidate_media_types=media_types
        )


class GenerationStore:
    """Reads and writes generations under a root directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save_draft(self, draft: Draft) -> StoredGeneration:
        """Persist a completed pass 1 so pass 2 can run later, on its own."""
        stored = StoredGeneration(self.root, draft.generation_id)
        stored.directory.mkdir(parents=True, exist_ok=True)

        for index, candidate in enumerate(draft.candidates):
            stem = "cartoon" if index == 0 else f"candidate-{index}"
            stored.path(_filename(stem, candidate)).write_bytes(candidate)
        stored.path(_filename("photo", draft.photo_data)).write_bytes(draft.photo_data)

        payload = {
            "generation_id": draft.generation_id,
            "settings": draft.settings.model_dump(mode="json"),
            "cartoon_prompt": draft.cartoon_prompt,
            "provider": draft.provider,
            "model": draft.model,
            "candidate_media_types": list(draft.candidate_media_types),
        }
        stored.path("draft.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return stored

    def load_draft(self, generation_id: str) -> Draft | None:
        """Reload a draft, or None if it is gone, malformed, or expired."""
        if not _is_safe_id(generation_id):
            return None
        stored = StoredGeneration(self.root, generation_id)

        try:
            payload: dict[str, object] = json.loads(
                stored.path("draft.json").read_text(encoding="utf-8")
            )
            settings = CartoonSettings.model_validate(payload["settings"])
            photo = stored.read_image_file("photo")
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

        if photo is None:
            return None

        candidates = stored.read_candidates()
        if not candidates:
            return None

        # A draft written before media types were stored (or one whose list no
        # longer matches its files) falls back to sniffing each candidate.
        stored_types = payload.get("candidate_media_types")
        media_types: tuple[str, ...] = ()
        if isinstance(stored_types, list) and len(stored_types) == len(candidates):
            media_types = tuple(str(item) for item in stored_types)

        return Draft(
            generation_id=generation_id,
            settings=settings,
            cartoon_prompt=str(payload.get("cartoon_prompt", "")),
            candidates=tuple(candidates),
            photo_data=photo,
            photo_media_type=_media_type_for(photo),
            provider=str(payload.get("provider", "unknown")),
            model=str(payload.get("model", "unknown")),
            candidate_media_types=media_types,
        )

    def read_image(self, generation_id: str, stem: str) -> bytes | None:
        """Load a stored image by stem, or None.

        Stems are "cartoon", "photo" or "candidate-N" for a draft's extra
        portrait alternatives.
        """
        if not _is_safe_id(generation_id) or not _is_known_stem(stem):
            return None
        return StoredGeneration(self.root, generation_id).read_image_file(stem)

    def delete(self, generation_id: str) -> None:
        if not _is_safe_id(generation_id):
            return
        stored = StoredGeneration(self.root, generation_id)
        if not stored.directory.is_dir():
            return
        for child in stored.directory.iterdir():
            if child.is_file():
                child.unlink()
        stored.directory.rmdir()

    def expire(self, *, older_than_seconds: float) -> int:
        """Delete every draft whose newest file is older than the window.

        Drafts are a child's uploaded photo and the portraits made from it, and
        they exist only so the review flow can survive a restart: keeping them
        after that is retention nobody asked for. The review flow needs
        minutes, so the window (`Settings.draft_retention_hours`) is hours.

        Age is the newest file's mtime, not the directory's: every write to a
        draft touches a file inside it, so the newest file is the last activity.
        A draft written while this runs is therefore never mistaken for stale.

        Only a directory holding a `draft.json` is a draft. The store's root
        also holds the app's other directories (`books/`, `users/`), and those
        must never be mistaken for a stale generation.

        Returns:
            How many drafts were removed.
        """
        if older_than_seconds <= 0:
            return 0
        # `st_mtime` is epoch seconds, so the window boundary must be too.
        cutoff = time() - older_than_seconds
        removed = 0
        for directory in self.root.iterdir():
            if not directory.is_dir() or not _is_safe_id(directory.name):
                continue
            if not (directory / "draft.json").is_file():
                continue
            if _newest_file_mtime(directory) > cutoff:
                continue
            self.delete(directory.name)
            removed += 1
            logger.info("draft.expired id=%s", directory.name)
        return removed


def _newest_file_mtime(directory: Path) -> float:
    """The most recent write inside `directory`, or 0 if it holds no files.

    A directory with no readable files must not read as "brand new": 0 is
    older than any cutoff, so it is cleaned up rather than kept forever.
    """
    newest = 0.0
    try:
        for child in directory.iterdir():
            if not child.is_file():
                continue
            try:
                newest = max(newest, child.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return 0.0
    return newest


def _filename(stem: str, data: bytes) -> str:
    """Pick the extension from the bytes themselves, not from a claim."""
    return f"{stem}.jpg" if data[:2] == b"\xff\xd8" else f"{stem}.png"


def _media_type_for(data: bytes) -> str:
    return "image/jpeg" if data[:2] == b"\xff\xd8" else "image/png"


def _is_known_stem(stem: str) -> bool:
    """Whitelist stems rather than trusting the URL, since they become filenames."""
    if stem in {"cartoon", "photo"}:
        return True
    prefix, _, digits = stem.partition("-")
    return prefix == "candidate" and digits.isdigit()


def _is_safe_id(generation_id: str) -> bool:
    """Generation ids become path segments, so they must not contain any."""
    if not generation_id or generation_id.startswith("."):
        return False
    return not any(part in generation_id for part in ("/", "\\", "..", "\x00"))
