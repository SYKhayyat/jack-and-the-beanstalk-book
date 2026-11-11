"""Import pre-made pages painted elsewhere, and optionally approve them.

    python -m app.scripts.import_premades --story little_red_riding_hood \
        --dir plates/_premade/little_red_riding_hood --approve

A pre-made page is the finished picture of a page the star is not in, painted
once onto that page's approved plate. A book whose visitor gave no photo of
anyone on the page uses it instead of an image call. This command reads the
picture files in a directory, one per page, stores each as a `draft`, and with
`--approve` approves it. Approving writes a template version when the story is
complete, exactly as approving a plate or a portrait does.

File naming: the file stem is the page number (`7.jpg`) or the page key
(`the-wolf-runs.png`). `.png`, `.jpg`, `.jpeg` and `.webp` are accepted. A page
the star is in, a page with no approved plate, or a stem that names no page is
an error, not a silent skip.

Each picture belongs to the plate that is approved when it is imported. A later
plate for that page leaves the picture out of later versions, so paint a new one
from the new plate.

Safety rules, both about not losing anything:

* **An approved pre-made page is never replaced silently.** A page that already
  has one is refused unless `--replace` is given, and `--replace` is refused
  without `--approve`.
* **Every file is decoded and every page checked before anything is written.**
  A broken download or a wrong page fails the run at the top.

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
    page_number: int
    path: Path
    image: bytes
    media_type: str


@dataclass(slots=True)
class Result:
    imported: list[int] = field(default_factory=list)
    approved: list[int] = field(default_factory=list)
    version_id: str | None = None


def story_for(key: str) -> StorySeed:
    for seed in SEEDS:
        if seed.key == key:
            return seed
    available = ", ".join(seed.key for seed in SEEDS)
    raise SystemExit(f"no story {key!r}; the seeds are: {available}")


def _decode(path: Path) -> tuple[bytes, str]:
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
    return data, _FORMATS[fmt]


def read_candidates(store: TemplateStore, story_key: str, directory: Path) -> list[Candidate]:
    """Every picture in the directory, decoded and matched to a page it may be.

    Raises ImportError_ on the first problem, before the store is touched.
    """
    if not directory.is_dir():
        raise ImportError_(f"{directory} is not a directory")
    pages = store.pages(story_key)
    by_key = {page.page_key: number for number, page in pages.items() if page.page_key}
    found: list[Candidate] = []
    seen: set[int] = set()
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        stem = path.stem
        number = int(stem) if stem.isdigit() else by_key.get(stem)
        if number is None or number not in pages or number == 0:
            raise ImportError_(f"{path.name}: {stem!r} names no page of {story_key}")
        if pages[number].star_in_picture:
            raise ImportError_(
                f"{path.name}: page {number} shows the star; it is drawn from the photo"
            )
        if not any(p.status == PlateStatus.APPROVED for p in store.plates(story_key, number)):
            raise ImportError_(f"{path.name}: page {number} has no approved plate")
        if number in seen:
            raise ImportError_(f"two files claim page {number}")
        seen.add(number)
        data, media_type = _decode(path)
        found.append(Candidate(number, path, data, media_type))
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
    candidates = read_candidates(store, story.key, directory)
    result = Result()
    if approve and not replace:
        approved_now = {
            p.page_number
            for p in store.premades(story.key)
            if p.status == PlateStatus.APPROVED
        }
        clash = sorted(c.page_number for c in candidates if c.page_number in approved_now)
        if clash:
            pages = ", ".join(str(n) for n in clash)
            raise ImportError_(
                f"page {pages} already approved; pass --replace to replace them"
            )
    for candidate in candidates:
        premade_id = store.add_premade(
            story.key,
            candidate.page_number,
            image=candidate.image,
            media_type=candidate.media_type,
        )
        result.imported.append(candidate.page_number)
        if not approve:
            print(f"draft: page {candidate.page_number} ({candidate.path.name})", file=out)
            continue
        result.version_id = store.approve_premade(premade_id, operator=operator)
        result.approved.append(candidate.page_number)
        print(f"approved: page {candidate.page_number} ({candidate.path.name})", file=out)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.import_premades",
        description=(
            "Store finished pictures for the pages a story's star is not in, and "
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
        help="directory of pictures named by page number or key: 7.jpg, the-cupboard.png …",
    )
    parser.add_argument(
        "--operator",
        default="operator",
        help="who is approving; recorded (default: 'operator')",
    )
    parser.add_argument(
        "--approve", action="store_true", help="approve each page after storing it"
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an approved pre-made page (needs --approve)",
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
        # A terminal run may come before the first boot, so seed as the app does.
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
        print("the story's template version now pins these pages.")
    return 0


if __name__ == "__main__":  # pragma: no cover - the module's entry point
    raise SystemExit(main())
