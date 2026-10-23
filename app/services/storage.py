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
from dataclasses import dataclass, replace
from pathlib import Path

from app.models import CartoonSettings, GenerationResult

__all__ = ["Draft", "GenerationStore", "StoredGeneration"]


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
        base = f"/media/{self.generation_id}"
        return {
            "cartoon": f"{base}/cartoon",
            "scene": f"{base}/scene",
            "meta": f"{base}/meta.json",
        }


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

    @property
    def cartoon_image(self) -> bytes:
        """The default portrait: candidate zero."""
        return self.candidates[0]

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
        return replace(self, candidates=(self.candidates[index],))


class GenerationStore:
    """Reads and writes generations under a root directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, result: GenerationResult) -> StoredGeneration:
        """Persist both images and a JSON sidecar describing the run."""
        stored = StoredGeneration(self.root, result.generation_id)
        stored.directory.mkdir(parents=True, exist_ok=True)

        cartoon_suffix = "jpg" if result.cartoon_image[:2] == b"\xff\xd8" else "png"
        scene_suffix = "jpg" if result.scene_image[:2] == b"\xff\xd8" else "png"

        stored.path(f"cartoon.{cartoon_suffix}").write_bytes(result.cartoon_image)
        stored.path(f"scene.{scene_suffix}").write_bytes(result.scene_image)

        meta = {
            "generation_id": result.generation_id,
            "provider": result.provider,
            "model": result.model,
            "scene_key": result.scene_key,
            "caption": result.caption,
            "settings": result.settings.model_dump(mode="json"),
            "cartoon_prompt": result.cartoon_prompt,
            "scene_prompt": result.scene_prompt,
            "candidate_count": result.candidate_count,
            "created_at": result.created_at.isoformat(),
            "warnings": list(result.warnings),
        }
        stored.path("meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return stored

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

        return Draft(
            generation_id=generation_id,
            settings=settings,
            cartoon_prompt=str(payload.get("cartoon_prompt", "")),
            candidates=tuple(candidates),
            photo_data=photo,
            photo_media_type=_media_type_for(photo),
            provider=str(payload.get("provider", "unknown")),
            model=str(payload.get("model", "unknown")),
        )

    def read_meta(self, generation_id: str) -> dict[str, object] | None:
        """Load a generation's metadata, or None if it is gone or malformed."""
        if not _is_safe_id(generation_id):
            return None
        path = StoredGeneration(self.root, generation_id).path("meta.json")
        if not path.is_file():
            return None
        try:
            parsed: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return parsed

    def read_image(self, generation_id: str, stem: str) -> bytes | None:
        """Load a stored image by stem, or None.

        Stems are "cartoon", "scene" or "candidate-N" for a draft's extra
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


def _filename(stem: str, data: bytes) -> str:
    """Pick the extension from the bytes themselves, not from a claim."""
    return f"{stem}.jpg" if data[:2] == b"\xff\xd8" else f"{stem}.png"


def _media_type_for(data: bytes) -> str:
    return "image/jpeg" if data[:2] == b"\xff\xd8" else "image/png"


def _is_known_stem(stem: str) -> bool:
    """Whitelist stems rather than trusting the URL, since they become filenames."""
    if stem in {"cartoon", "scene"}:
        return True
    prefix, _, digits = stem.partition("-")
    return prefix == "candidate" and digits.isdigit()


def _is_safe_id(generation_id: str) -> bool:
    """Generation ids become path segments, so they must not contain any."""
    if not generation_id or generation_id.startswith("."):
        return False
    return not any(part in generation_id for part in ("/", "\\", "..", "\x00"))
