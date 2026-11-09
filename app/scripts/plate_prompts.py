"""Print the exact plate prompts, so they can be painted anywhere.

    python -m app.scripts.plate_prompts --story jack_and_the_beanstalk
    python -m app.scripts.plate_prompts --story jack_and_the_beanstalk --page 4
    python -m app.scripts.plate_prompts --story jack_and_the_beanstalk --out ~/plates/jack

The plates are painted outside the app as often as inside it (a free web image
tool costs nothing and a whole story is 21 calls). This command hands you the
exact words `make_plates` would have sent — the same prompt, built by the same
`plate_plans()` — so a plate painted elsewhere is the plate the app planned.

`--out DIR` writes one `.txt` per page, named by the page number to match the
files `import_plates` expects (`0.txt` … `20.txt`), plus a `README.txt` with the
shared instructions and the slot each page reserves. It writes no images and
makes no API call: this command costs nothing.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from app.scripts._venv import ensure_venv

# Before any `app.services` import: on a host python this hands the command to
# `.venv/bin/python` instead of failing with a bare ModuleNotFoundError.
ensure_venv()

from app.books import SEEDS, StorySeed  # noqa: E402
from app.services.plates import PlatePlan, plate_plans  # noqa: E402

__all__ = ["main", "render_index", "write_prompts"]

#: What every page's file says beside its prompt: the rules that are not in the
#: prompt itself because they are about the file, not the picture.
_HOW_TO = """\
HOW TO USE THIS PROMPT
1. Paint it in any image tool: a free web UI (paste the prompt, generate), or
   `python -m app.scripts.make_plates` which sends it to the configured API.
2. The picture must have NOBODY in it and NO text of any kind. It is the empty
   stage; the characters are painted in later by the book's edit pass.
3. Keep the aspect ratio at 4:3 (1152x864 or any 4:3 size). The PDF and the
   character slots assume it.
4. Save the result as <page-number>.png (0 for the cover, 1-20 for the pages),
   all of them in one directory.
5. Import them:  python -m app.scripts.import_plates --story {story} \\
        --dir <that directory> --approve
   Add --replace only when you mean to retire an already approved plate.
"""


def story_for(key: str) -> StorySeed:
    for seed in SEEDS:
        if seed.key == key:
            return seed
    available = ", ".join(seed.key for seed in SEEDS)
    raise SystemExit(f"no story {key!r}; the seeds are: {available}")


def _slots_lines(plan: PlatePlan, story: StorySeed) -> str:
    """The slot each character will later be painted into, as percentages."""
    labels = {key: member.label for key, member in story.cast.items()}
    lines = []
    for slot in plan.slots:
        x, y, w, h = slot.rect
        label = (
            "the child"
            if slot.cast_key == story.star
            else labels.get(slot.cast_key, slot.cast_key)
        )
        lines.append(
            f"  - {label}: {round(x * 100)}%-{round((x + w) * 100)}% across, "
            f"{round(y * 100)}%-{round((y + h) * 100)}% down "
            f"(leave this patch of ground clear and uncluttered)"
        )
    return "\n".join(lines)


def render_index(story: StorySeed, plans: Sequence[PlatePlan]) -> str:
    """The README.txt that goes beside the prompt files."""
    rows = [
        f"{plan.page_number:>2}  {plan.key:<28} {len(plan.slots)} slot(s)" for plan in plans
    ]
    return (
        f"{story.title.upper()} — THE {len(plans)} PLATES\n\n"
        + _HOW_TO.format(story=story.key)
        + "\nTHE PAGES, IN ORDER (file name = page number):\n"
        + "\n".join(rows)
        + "\n\nNote: page 0 is the cover. Its top fifth must stay empty — the "
        "book's title is printed there afterwards.\n"
    )


def write_prompts(
    story: StorySeed, plans: Sequence[PlatePlan], directory: Path, *, out: TextIO
) -> list[Path]:
    """Write one `.txt` per page plus the README. Returns the paths written."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for plan in plans:
        target = directory / f"{plan.page_number}.txt"
        target.write_text(
            f"PAGE {plan.page_number} — {plan.key}\n\n"
            f"{plan.prompt}\n\n"
            "DO NOT PASTE THIS PART. Slots the edit pass will use later:\n"
            f"{_slots_lines(plan, story)}\n",
            encoding="utf-8",
        )
        written.append(target)
    readme = directory / "README.txt"
    readme.write_text(render_index(story, plans), encoding="utf-8")
    written.append(readme)
    print(f"wrote {len(written)} file(s) to {directory}", file=out)
    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.plate_prompts",
        description="Print or save the exact prompt for every plate of a story.",
    )
    parser.add_argument("--story", required=True, help="the story key")
    parser.add_argument(
        "--page",
        type=int,
        default=None,
        help="only this page (0 is the cover); prints to stdout",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write one .txt per page into this directory instead of printing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    story = story_for(args.story)
    plans = plate_plans(story)
    if args.page is not None:
        chosen = [plan for plan in plans if plan.page_number == args.page]
        if not chosen:
            print(f"no page {args.page}; the story has 0-{len(plans) - 1}")
            return 1
        print(f"=== page {chosen[0].page_number}: {chosen[0].key} ===")
        print(chosen[0].prompt)
        print("\nSLOTS (for the edit pass later):")
        print(_slots_lines(chosen[0], story))
        return 0
    if args.out is not None:
        write_prompts(story, plans, args.out, out=sys.stdout)
        return 0
    for plan in plans:
        print(f"=== page {plan.page_number}: {plan.key} ===")
        print(plan.prompt)
        print()
    print(_HOW_TO.format(story=story.key))
    return 0


if __name__ == "__main__":  # pragma: no cover - the module's entry point
    raise SystemExit(main())
