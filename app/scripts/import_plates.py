"""Import plates painted somewhere else, and optionally approve them.

    python -m app.scripts.import_plates --story jack_and_the_beanstalk \
        --dir ~/plates/jack --approve

Why this exists: the plates are painted once, for a whole story, and they are
not user data — they can be made in any image tool at all, including a free web
UI with no API key. This command is the door back in. It reads image files from
a directory, matches each one to a page, stores it as a `draft` plate carrying
**the page's planned prompt and slots** (never anything invented here), and with
`--approve` approves it. Approving the last missing page writes a template
version, exactly as the admin screen does.

File naming: the page number is the file stem. `0` is the cover, `1`-`21` are
the pages in story order. `.png`, `.jpg`, `.jpeg` and `.webp` are accepted, in
any mix; a file whose stem is not a page number of this story is an error, not
a silent skip, because a plate on the wrong page is a book with the wrong
setting on it.

Two safety rules, both about not spending or losing anything:

* **An approved plate is never replaced.** If a page already has an approved
  plate, the import refuses that page unless `--replace` is given, and
  `--replace` is refused without `--approve` (replacing is what creates a new
  template version; half of that is a puzzle for nobody).
* **A file that is not a usable image is refused before anything is written.**
  Each file is decoded with Pillow first, so a broken download fails the run at
  the top rather than midway with half the pages imported.

No image call is made by this command, ever. It costs nothing.
"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from app.scripts._venv import ensure_venv

# Before Pillow and `app.services`: on a host python this hands the command to
# `.venv/bin/python` instead of failing with a bare ModuleNotFoundError.
ensure_venv()

from PIL import Image  # noqa: E402

from app.books import SEEDS, StorySeed  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.db import build_engine, prepare_database  # noqa: E402
from app.services.plates import PlatePlan, plate_plans  # noqa: E402
from app.services.template_store import (  # noqa: E402
    PlateStatus,
    TemplateError,
    TemplateStore,
)

__all__ = ["SUFFIXES", "ImportError_", "ImportResult", "run"]

#: Image types a plate may arrive as. Anything else is a mistake, not a plate.
SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


class ImportError_(RuntimeError):
    """The import cannot proceed. The message is safe to print for an operator."""


@dataclass(frozen=True, slots=True)
class ImportResult:
    """What one run did, so a test or a caller can check it without parsing text."""

    imported: tuple[int, ...]
    approved: tuple[int, ...]
    skipped: tuple[int, ...]
    version_id: str | None


def story_for(key: str) -> StorySeed:
    """The seed with that key, or a sentence naming the ones that exist."""
    for seed in SEEDS:
        if seed.key == key:
            return seed
    available = ", ".join(seed.key for seed in SEEDS)
    raise SystemExit(f"no story {key!r}; the seeds are: {available}")


def _page_of(path: Path) -> int | None:
    """The page number a file claims, or None if its stem is not a number."""
    try:
        return int(path.stem)
    except ValueError:
        return None


def collect(directory: Path, plans: Sequence[PlatePlan]) -> dict[int, Path]:
    """Map page number -> file, refusing anything ambiguous or off-story.

    Raises:
        ImportError_: a claimed page the story does not have, a duplicate page,
            or a directory with no plate files at all.
    """
    if not directory.is_dir():
        raise ImportError_(f"{directory} is not a directory")
    known = {plan.page_number for plan in plans}
    found: dict[int, Path] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        page = _page_of(path)
        if page is None:
            raise ImportError_(
                f"{path.name}: name the file after its page — 0 for the cover, "
                f"1 to {max(known)} for the story pages"
            )
        if page not in known:
            raise ImportError_(
                f"{path.name}: this story has pages 0-{max(known)}; there is no page {page}"
            )
        if page in found:
            raise ImportError_(
                f"pages {found[page].name} and {path.name} both claim page {page}"
            )
        found[page] = path
    if not found:
        raise ImportError_(
            f"no plate images in {directory}; expected files named 0.png … "
            f"{max(known)}.png (or .jpg/.jpeg/.webp)"
        )
    return found


def load_image(path: Path) -> tuple[bytes, str]:
    """Decode a plate file and re-encode it as PNG, or refuse it.

    Re-encoding is deliberate: whatever tool painted the plate, the stored
    bytes are one known format at one known range, so the PDF and the provider
    never have to cope with an exotic producer's output.
    """
    try:
        with Image.open(path) as image:
            image.load()
            rgb = image.convert("RGB")
    except (OSError, ValueError) as exc:
        raise ImportError_(f"{path.name}: not a readable image ({exc})") from exc
    buffer = io.BytesIO()
    rgb.save(buffer, format="PNG")
    return buffer.getvalue(), "image/png"


def run(
    *,
    story: StorySeed,
    store: TemplateStore,
    directory: Path,
    operator: str,
    approve: bool = False,
    replace: bool = False,
    out: TextIO | None = None,
) -> ImportResult:
    """Store the plates in `directory`; approve them when asked.

    Separate from `main` so a test can drive it against a temporary database
    with no terminal and no network.
    """
    out = out or sys.stdout
    if replace and not approve:
        raise ImportError_(
            "--replace only makes sense with --approve: replacing an approved "
            "plate is how a new template version is written"
        )
    plans = {plan.page_number: plan for plan in plate_plans(story)}
    files = collect(directory, list(plans.values()))

    approved_pages = {
        plate.page_number
        for plate in store.plates(story.key)
        if plate.status == PlateStatus.APPROVED
    }
    if not replace:
        collisions = sorted(approved_pages & files.keys())
        if collisions:
            listing = ", ".join(str(page) for page in collisions)
            raise ImportError_(
                f"page(s) {listing} already have an approved plate. Importing "
                "would retire it. Nothing was written: pass --replace --approve "
                "if that is genuinely what you want."
            )

    # Decode everything first: a broken file must not leave half a story imported.
    decoded: dict[int, tuple[bytes, str]] = {
        page: load_image(path) for page, path in files.items()
    }

    imported: list[int] = []
    approved: list[int] = []
    skipped: list[int] = []
    version_id: str | None = None
    for page in sorted(decoded):
        plan = plans[page]
        image, media_type = decoded[page]
        if page in approved_pages and not replace:
            skipped.append(page)
            print(f"page {page}: already approved, left alone", file=out)
            continue
        plate_id = store.add_plate(
            story.key,
            page,
            image=image,
            media_type=media_type,
            prompt=plan.prompt,
            slots=plan.slots,
        )
        imported.append(page)
        print(f"page {page} ({plan.key}): stored as draft {plate_id}", file=out)
        if approve:
            version_id = store.approve_plate(plate_id, operator=operator) or version_id
            approved.append(page)
            print(f"page {page}: approved", file=out)

    if approve:
        print(
            f"approved {len(approved)} plate(s) as {operator}; "
            f"template version: {version_id or 'incomplete'}",
            file=out,
        )
    else:
        print(
            f"stored {len(imported)} draft plate(s). Approve them in "
            f"/admin/plates/{story.key} or re-run with --approve.",
            file=out,
        )
    return ImportResult(
        imported=tuple(imported),
        approved=tuple(approved),
        skipped=tuple(skipped),
        version_id=version_id,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.import_plates",
        description=(
            "Store plates painted outside the app, and optionally approve them. "
            "Makes no image call: this command costs nothing."
        ),
    )
    parser.add_argument(
        "--story", required=True, help="the story key, e.g. little_red_riding_hood"
    )
    parser.add_argument(
        "--dir",
        type=Path,
        required=True,
        help="directory of plate images named by page: 0.png (cover) … 21.png",
    )
    parser.add_argument(
        "--operator",
        default="operator",
        help="who is approving; recorded on every plate (default: 'operator')",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="approve each plate after storing it; the last one writes a template version",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="overwrite an already-approved page (needs --approve)",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolved = settings or get_settings()
    story = story_for(args.story)
    engine = prepare_database(build_engine(resolved.effective_database_url))
    try:
        store = TemplateStore(engine)
        # The app seeds at boot; a terminal run may come first, so it seeds too —
        # a plate cannot be approved against page words that do not exist yet.
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
