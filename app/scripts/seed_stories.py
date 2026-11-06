"""Load the reviewed story seeds (`app/books/*.toml`) into the database.

    DATABASE_URL=postgres://... python -m app.scripts.seed_stories

Idempotent: a second run with unchanged seeds writes nothing. A changed seed
adds new revisions (and a new template version once a story's plates are
approved); nothing a book already pinned is rewritten. Run it after
`make migrate`.
"""

from __future__ import annotations

import os
import sys

from app.books import SEEDS
from app.db import build_engine
from app.services.template_store import TemplateStore


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set; export it and run again.", file=sys.stderr)
        return 2
    engine = build_engine(url)
    try:
        report = TemplateStore(engine).seed(SEEDS)
    finally:
        engine.dispose()
    print(
        f"stories added: {report.stories}, page revisions: {report.page_revisions}, "
        f"character revisions: {report.character_revisions}, "
        f"new template versions: {len(report.versions)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
