"""Import fixed portraits painted elsewhere, and optionally approve them.

    python -m app.scripts.import_portraits --story jack_and_the_beanstalk \
        --dir plates/_masters/jack --approve

A fixed portrait is the face a supporting character is drawn with in any book
whose visitor gave no photograph of them. This command reads the picture files
in a directory, one per character, stores each as a `draft` portrait, and with
`--approve` approves it. Approving writes a template version when the story is
otherwise complete, exactly as the admin screen does.

File naming: the file stem is the character's cast key (`giant.png`, `mother.jpg`).
`.png`, `.jpg`, `.jpeg` and `.webp` are accepted, in any mix. A stem that is not a
supporting character of this story is an error, not a silent skip. The star has
no fixed portrait, so a file named for the star is refused too.

Safety rules, both about not losing anything:

* **An approved portrait is never replaced silently.** If a character already has
  an approved portrait, the run refuses it unless `--replace` is given, and
  `--replace` is refused without `--approve`.
* **Every file is decoded before anything is written.** A broken download fails
  the run at the top, not midway through the cast.

No image call is made by this command. It costs nothing.
"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from app.scripts._venv import ensure_venv

# Before Pillow and `app.services`: on a host python this hands the command to
# `.venv/bin/python` instead of failing with a bare ModuleNotFoundError.
ensure_venv()

from PIL import Image  # noqa: E402

from app.blobs import install_blob_store  # noqa: E402
from app.books import SEEDS, StorySeed  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.db import build_engine, prepare_database  # noqa: E402
from app.services.template_store import (  # noqa: E402
    PlateStatus,
    TemplateError,
    TemplateStore,
)

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class ImportError_(Exception):
    """The directory cannot be imported as it stands. Nothing has been written."""


@dataclass(frozen=True, slots=True)
class Candidate:
    cast_key: str
    path: Path
    image: bytes
    media_type: str


@dataclass(slots=True)
class Result:
    imported: list[str] = field(default_factory=list)
    approved: list[str] = field(default_factory=list)
    version_id: str | None = None


def story_for(key: str) -> StorySeed:
    for seed in SEEDS:
        if seed.key == key:
            return seed
    available = ", ".join(seed.key for seed in SEEDS)
    raise SystemExit(f"no story {key!r}; the seeds are: {available}")


def read_candidates(story: StorySeed, directory: Path) -> list[Candidate]:
    """Every picture in the directory, decoded and matched to a supporting character.

    Raises ImportError_ on the first problem, before the store is touched.
    """
    if not directory.is_dir():
        raise ImportError_(f"{directory} is not a directory")
    found: list[Candidate] = []
    seen: set[str] = set()
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        cast_key = path.stem
        if cast_key == story.star:
            raise ImportError_(
                f"{path.name}: the star has no fixed portrait; remove this file"
            )
        if cast_key not in story.cast:
            names = ", ".join(sorted(k for k in story.cast if k != story.star))
            raise ImportError_(
                f"{path.name}: {cast_key!r} is not a supporting character ({names})"
            )
        if cast_key in seen:
            raise ImportError_(f"two files claim {cast_key!r}")
        seen.add(cast_key)
        data = path.read_bytes()
        try:
            with Image.open(io.BytesIO(data)) as probe:
                probe.verify()
            with Image.open(io.BytesIO(data)) as decoded:
                fmt = str(decoded.format)
        except Exception as exc:  # Pillow raises several types for a bad file
            raise ImportError_(f"{path.name} is not a usable picture: {exc}") from exc
        if fmt not in _FORMATS:
            raise ImportError_(f"{path.name} is {fmt}; use PNG, JPEG or WEBP")
        found.append(Candidate(cast_key, path, data, _FORMATS[fmt]))
    if not found:
        raise ImportError_(f"no picture files in {directory}")
    return found


def run(
    *,
    story: StorySeed,
    store: TemplateStore,
    directory: Path,
    operator: str,
    approve: bool,
    replace: bool,
    out: TextIO = sys.stdout,
) -> Result:
    if replace and not approve:
        raise ImportError_("--replace needs --approve; a replacement is what writes a version")
    candidates = read_candidates(story, directory)
    result = Result()
    if not approve:
        for candidate in candidates:
            store.add_portrait(
                story.key,
                candidate.cast_key,
                image=candidate.image,
                media_type=candidate.media_type,
            )
            result.imported.append(candidate.cast_key)
            print(f"draft: {candidate.cast_key} ({candidate.path.name})", file=out)
        return result

    approved_now = {
        p.cast_key for p in store.portraits(story.key) if p.status == PlateStatus.APPROVED
    }
    if not replace:
        clash = sorted(c.cast_key for c in candidates if c.cast_key in approved_now)
        if clash:
            raise ImportError_(
                f"{', '.join(clash)} already approved; pass --replace to replace them"
            )
    for candidate in candidates:
        portrait_id = store.add_portrait(
            story.key,
            candidate.cast_key,
            image=candidate.image,
            media_type=candidate.media_type,
        )
        result.imported.append(candidate.cast_key)
        result.version_id = store.approve_portrait(portrait_id, operator=operator)
        result.approved.append(candidate.cast_key)
        print(f"approved: {candidate.cast_key} ({candidate.path.name})", file=out)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.import_portraits",
        description=(
            "Store the fixed portraits of a story's supporting characters, and "
            "optionally approve them. Makes no image call: this command costs nothing."
        ),
    )
    parser.add_argument(
        "--story", required=True, help="the story key, e.g. little_red_riding_hood"
    )
    parser.add_argument(
        "--dir",
        type=Path,
        required=True,
        help="directory of portrait images named by cast key: giant.png, mother.jpg …",
    )
    parser.add_argument(
        "--operator",
        default="operator",
        help="who is approving; recorded (default: 'operator')",
    )
    parser.add_argument(
        "--approve", action="store_true", help="approve each portrait after storing it"
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an approved portrait (needs --approve)",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolved = settings or get_settings()
    story = story_for(args.story)
    install_blob_store(resolved)
    engine = prepare_database(build_engine(resolved.effective_database_url))
    try:
        store = TemplateStore(engine)
        # A terminal run may come before the first boot, so seed as the app does:
        # a portrait cannot be stored for a character the story does not have.
        store.seed(SEEDS)
        try:
            result = run(
                story=story,
                store=store,
                directory=args.dir,
                operator=args.operator,
                approve=args.approve,
                replace=args.replace,
            )
        except (ImportError_, TemplateError) as exc:
            print(f"stopped: {exc}")
            return 1
    finally:
        engine.dispose()
    if result.approved and result.version_id:
        print("the story now has a complete template version.")
    return 0


if __name__ == "__main__":  # pragma: no cover - the module's entry point
    raise SystemExit(main())
