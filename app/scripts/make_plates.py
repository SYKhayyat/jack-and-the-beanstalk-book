"""Paint the 21 empty plates for one story, offline (T5).

    python -m app.scripts.make_plates --story little_red_riding_hood

Writes one `draft` plate per page (cover included) to the database, each with
the exact prompt and one slot per character on that page. Plates are approved
by the operator in the admin screen (T6); this command never approves one.

Cost: one image call per plate per candidate. The command says how many calls
it is about to make before it makes any, and a paid provider additionally asks
for a typed confirmation — a whole story's plates are 21 billable pictures.

    --contact-sheet out.png   also write a contact sheet of what was painted
"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Sequence
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
from app.services.image_providers.base import ImageProvider, ImageRequest  # noqa: E402
from app.services.image_providers.registry import build_provider  # noqa: E402
from app.services.plates import PlatePlan, plate_plans  # noqa: E402
from app.services.template_store import TemplateError, TemplateStore  # noqa: E402

__all__ = ["ContactSheetError", "main", "run"]

#: Thumbnail width in a contact sheet; seven fit a comfortable row.
_THUMB_WIDTH = 320
_COLUMNS = 7


class ContactSheetError(RuntimeError):
    """A painted plate could not be decoded, so no sheet can be built."""


def story_for(key: str) -> StorySeed:
    """The seed with that key, or a sentence naming the ones that exist."""
    for seed in SEEDS:
        if seed.key == key:
            return seed
    available = ", ".join(seed.key for seed in SEEDS)
    raise SystemExit(f"no story {key!r}; the seeds are: {available}")


def run(
    *,
    story: StorySeed,
    store: TemplateStore,
    provider: ImageProvider,
    candidates: int = 1,
    contact_sheet: Path | None = None,
    out: TextIO | None = None,
) -> list[str]:
    """Paint a story's plates. Returns the ids of the plates written.

    Separate from `main` so the flow can be driven without a database or a
    network of its own: the caller supplies the store and the provider.
    """
    out = out or sys.stdout
    if candidates < 1:
        raise ValueError("candidates must be at least 1")
    plans = plate_plans(story)
    calls = len(plans) * candidates
    print(
        f"{calls} image call(s): {len(plans)} plate(s) x {candidates} candidate(s)"
        f" for {story.key}.",
        file=out,
    )
    written: list[str] = []
    painted: list[tuple[PlatePlan, bytes]] = []
    for plan in plans:
        for _ in range(candidates):
            image = provider.generate(_request_for(plan))
            written.append(
                store.add_plate(
                    story.key,
                    plan.page_number,
                    image=image.data,
                    media_type=image.media_type,
                    prompt=plan.prompt,
                    slots=plan.slots,
                )
            )
            painted.append((plan, image.data))
    print(f"stored {len(written)} draft plate(s) for {story.key}.", file=out)
    if contact_sheet is not None:
        contact_sheet.parent.mkdir(parents=True, exist_ok=True)
        contact_sheet.write_bytes(build_contact_sheet(painted))
        print(f"contact sheet: {contact_sheet}", file=out)
    return written


def build_contact_sheet(painted: Sequence[tuple[PlatePlan, bytes]]) -> bytes:
    """One image per page, in page order, so the set can be checked as a set."""
    thumbs: list[Image.Image] = []
    for _plan, data in painted:
        try:
            image = Image.open(io.BytesIO(data))
        except OSError as exc:
            raise ContactSheetError(f"a painted plate is not an image: {exc}") from exc
        with image:
            thumbs.append(image.convert("RGB"))
    if not thumbs:
        raise ContactSheetError("no plates were painted, so there is no sheet")
    height = round(thumbs[0].height * _THUMB_WIDTH / thumbs[0].width)
    scaled = [thumb.resize((_THUMB_WIDTH, height)) for thumb in thumbs]
    rows = (len(scaled) + _COLUMNS - 1) // _COLUMNS
    sheet = Image.new("RGB", (_THUMB_WIDTH * _COLUMNS, height * rows), (250, 246, 236))
    for index, thumb in enumerate(scaled):
        sheet.paste(thumb, ((index % _COLUMNS) * _THUMB_WIDTH, (index // _COLUMNS) * height))
    buffer = io.BytesIO()
    sheet.save(buffer, format="PNG")
    return buffer.getvalue()


def _request_for(plan: PlatePlan) -> ImageRequest:
    return ImageRequest(prompt=plan.prompt, aspect_ratio="4:3", label="plate")


def _confirm(provider: ImageProvider, calls: int, *, assume_yes: bool) -> bool:
    """A paid provider needs a typed yes; a placeholder one does not."""
    print(
        f"provider: {provider.name}"
        f" ({'paid' if not provider.draws_placeholders else 'free placeholder art'})"
    )
    if provider.draws_placeholders or assume_yes:
        return True
    answer = input(f"This will make {calls} paid image call(s). Type 'yes' to continue: ")
    return answer.strip().lower() == "yes"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.make_plates",
        description="Paint the empty plates for one story and store them as drafts.",
    )
    parser.add_argument("--story", required=True, help="the story key, e.g. red's key")
    parser.add_argument(
        "--candidates",
        type=int,
        default=1,
        help="how many alternatives to paint per page (default 1)",
    )
    parser.add_argument(
        "--contact-sheet",
        type=Path,
        default=None,
        help="also write a PNG contact sheet of every painted page",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the paid-provider confirmation (for an attended scripted run)",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolved = settings or get_settings()
    story = story_for(args.story)
    plans = plate_plans(story)
    calls = len(plans) * args.candidates
    provider = build_provider(resolved)
    if not _confirm(provider, calls, assume_yes=args.yes):
        print("stopped: nothing was painted.")
        return 1

    engine = prepare_database(build_engine(resolved.effective_database_url))
    try:
        store = TemplateStore(engine)
        # The app seeds at boot; a terminal run may come first, so it seeds too.
        store.seed(SEEDS)
        try:
            written = run(
                story=story,
                store=store,
                provider=provider,
                candidates=args.candidates,
                contact_sheet=args.contact_sheet,
            )
        except TemplateError as exc:
            print(f"stopped: {exc}")
            return 1
    finally:
        engine.dispose()
    print(f"done: {len(written)} draft plate(s). Approve them in the admin screen.")
    return 0


if __name__ == "__main__":  # pragma: no cover - the module's entry point
    raise SystemExit(main())
