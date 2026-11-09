"""Load the reviewed story seeds (`app/books/*.toml`) into the database.

    python -m app.scripts.seed_stories          # the database the app would use

The app does this itself at every boot, so this is for preparing a database
ahead of a deploy (or checking what a changed seed would write). It uses the
same database the app would — `DATABASE_URL`, or the local SQLite file when
that is empty — and migrates it first.

Idempotent: a second run with unchanged seeds writes nothing. A changed seed
adds new revisions (and a new template version once a story's plates are
approved); nothing a book already pinned is rewritten.
"""

from __future__ import annotations

from app.scripts._venv import ensure_venv

# Before `app.services`: on a host python this hands the command to
# `.venv/bin/python` instead of failing with a bare ModuleNotFoundError.
ensure_venv()

from app.books import SEEDS  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import build_engine, prepare_database  # noqa: E402
from app.services.template_store import TemplateStore  # noqa: E402


def main() -> int:
    engine = prepare_database(build_engine(get_settings().effective_database_url))
    try:
        store = TemplateStore(engine)
        report = store.seed(SEEDS)
        pinned = store.pin_unpinned_books()
    finally:
        engine.dispose()
    print(
        f"stories added: {report.stories}, page revisions: {report.page_revisions}, "
        f"character revisions: {report.character_revisions}, "
        f"new template versions: {len(report.versions)}, older books pinned: {pinned}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
