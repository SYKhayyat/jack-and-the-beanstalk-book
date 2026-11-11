"""Serve the app on this machine with Gemini in Chrome doing every paid job, free.

    python -m app.scripts.local_book              # prepare, then serve on :8001
    python -m app.scripts.local_book --prepare    # prepare only
    make local-book

A book made here goes through exactly the pipeline the deployed app runs:
the same stories, the same approved plates, portraits and pre-made pages, the
same runner, reviewer and decision judge. The only difference is that every
picture, every review and every judgement comes from gemini.google.com driven
in a dedicated Chrome profile (`app/services/gemini_web`), so it costs nothing.
It is slower, and it pauses (and resumes by itself) while Gemini needs a
person or a daily limit runs out.

Preparing builds a separate store under `var/local-book` (its own SQLite file,
no R2), seeds the stories from `app/books`, and copies the approved art from
the database `.env` points at. **The live database and R2 are only read.**
A manifest remembers which live picture each local one came from, so a second
run downloads only what was approved on live since.

Serving starts uvicorn with the local store, no sign-in, no payments and the
three `gemini_web` backends. `GEMINI_WEB_ACCOUNT` must name the Google account
the Chrome profile is signed into; the first job opens Chrome and waits for you
to sign in if it is not.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TextIO

from app.scripts._venv import ensure_venv

# Before `app.services`: on a host python this hands the command to
# `.venv/bin/python` instead of failing with a bare ModuleNotFoundError.
ensure_venv()

from sqlalchemy.engine import Engine  # noqa: E402

from app.blobs import install_blob_store, set_blob_store  # noqa: E402
from app.books import SEEDS  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import build_engine, local_database_url, prepare_database  # noqa: E402
from app.services.template_store import (  # noqa: E402
    PlateStatus,
    PlateSummary,
    PortraitSummary,
    PremadeSummary,
    TemplateStore,
)

__all__ = ["LOCAL_ENV", "copy_approved_art", "local_environment", "main"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORE = PROJECT_ROOT / "var" / "local-book"
MANIFEST = "live-art.json"
OPERATOR = "local-book copy"

#: What the served app is told, over whatever `.env` says. Empty means off:
#: no Postgres, no R2, no Google sign-in, no password, no Stripe.
LOCAL_ENV: dict[str, str] = {
    "APP_ENV": "development",
    "DATABASE_URL": "",
    "R2_ACCOUNT_ID": "",
    "R2_BUCKET": "",
    "R2_ACCESS_KEY_ID": "",
    "R2_SECRET_ACCESS_KEY": "",
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_CLIENT_SECRET": "",
    "GOOGLE_REDIRECT_URI": "",
    "APP_PASSWORD": "",
    "STRIPE_SECRET_KEY": "",
    "IMAGE_PROVIDER": "gemini_web",
    "REVIEWER_PROVIDER": "gemini_web",
    "DECISION_JUDGE_PROVIDER": "gemini_web",
    "DECISION_JUDGE": "true",
    "IMAGE_CALL_COST_USD": "",
    "REVIEW_CALL_COST_USD": "",
    # Every job waits its turn for the one browser, and a reached limit waits
    # until it lifts: a page may take hours, never fail for the wait.
    "GENERATION_TIMEOUT_SECONDS": str(24 * 3600),
}


def local_environment(store_dir: Path, base: dict[str, str]) -> dict[str, str]:
    """The process environment the local app runs with."""
    return base | LOCAL_ENV | {"STORAGE_DIR": str(store_dir)}


@dataclass(frozen=True, slots=True)
class _Art:
    """One approved live picture, named by what it belongs to."""

    kind: str
    live_id: str
    story_key: str
    label: str
    read: Callable[[], tuple[bytes, str]]
    #: Store the picture locally, approve it, and return its local id.
    copy: Callable[[bytes, str], str]


@dataclass(slots=True)
class CopyReport:
    copied: int = 0
    kept: int = 0
    skipped_stories: tuple[str, ...] = ()


def _approved_art(live: TemplateStore, local: TemplateStore, story_key: str) -> Iterable[_Art]:
    """Plates first: a portrait or a pre-made page is approved against them."""
    approved = PlateStatus.APPROVED
    for plate in live.plates(story_key):
        if plate.status == approved:
            yield _Art(
                "plates",
                plate.id,
                story_key,
                f"plate {plate.page_number}",
                partial(live.plate_image, plate.id),
                partial(_copy_plate, local, plate),
            )
    for portrait in live.portraits(story_key):
        if portrait.status == approved:
            yield _Art(
                "portraits",
                portrait.id,
                story_key,
                f"portrait {portrait.cast_key}",
                partial(live.portrait_image, portrait.id),
                partial(_copy_portrait, local, portrait),
            )
    for premade in live.premades(story_key):
        if premade.status == approved:
            yield _Art(
                "premades",
                premade.id,
                story_key,
                f"pre-made page {premade.page_number}",
                partial(live.premade_image, premade.id),
                partial(_copy_premade, local, premade),
            )


def _copy_plate(
    local: TemplateStore, plate: PlateSummary, image: bytes, media_type: str
) -> str:
    local_id = local.add_plate(
        plate.story_key,
        plate.page_number,
        image=image,
        media_type=media_type,
        prompt=plate.prompt,
        slots=plate.slots,
    )
    local.approve_plate(local_id, operator=OPERATOR)
    return local_id


def _copy_portrait(
    local: TemplateStore, portrait: PortraitSummary, image: bytes, media_type: str
) -> str:
    local_id = local.add_portrait(
        portrait.story_key, portrait.cast_key, image=image, media_type=media_type
    )
    local.approve_portrait(local_id, operator=OPERATOR)
    return local_id


def _copy_premade(
    local: TemplateStore, premade: PremadeSummary, image: bytes, media_type: str
) -> str:
    local_id = local.add_premade(
        premade.story_key, premade.page_number, image=image, media_type=media_type
    )
    local.approve_premade(local_id, operator=OPERATOR)
    return local_id


def _still_approved(local: TemplateStore, art: _Art, local_id: str) -> bool:
    listing: Sequence[PlateSummary | PortraitSummary | PremadeSummary]
    if art.kind == "plates":
        listing = local.plates(art.story_key)
    elif art.kind == "portraits":
        listing = local.portraits(art.story_key)
    else:
        listing = local.premades(art.story_key)
    return any(item.id == local_id and item.status == PlateStatus.APPROVED for item in listing)


def copy_approved_art(
    live: TemplateStore,
    local: TemplateStore,
    manifest: dict[str, dict[str, str]],
    *,
    on_live: Callable[[], None],
    on_local: Callable[[], None],
    out: TextIO,
) -> CopyReport:
    """Bring every approved live picture the local store lacks; update `manifest`.

    `on_live`/`on_local` switch the process's blob store before each read and
    each write: the live rows point into R2, the local rows hold their bytes.
    """
    report = CopyReport()
    on_live()
    live_stories = [state.key for state in live.stories()]
    on_local()
    local_stories = {state.key for state in local.stories()}
    report.skipped_stories = tuple(key for key in live_stories if key not in local_stories)
    for story_key in live_stories:
        if story_key not in local_stories:
            continue
        on_live()
        arts = list(_approved_art(live, local, story_key))
        for art in arts:
            copied = manifest.setdefault(art.kind, {})
            local_id = copied.get(art.live_id)
            on_local()
            if local_id is not None and _still_approved(local, art, local_id):
                report.kept += 1
                continue
            on_live()
            image, media_type = art.read()
            on_local()
            local_id = art.copy(image, media_type)
            copied[art.live_id] = local_id
            report.copied += 1
            print(f"  copied {story_key} {art.label}", file=out)
    return report


def prepare(store_dir: Path, *, out: TextIO = sys.stdout) -> CopyReport:
    """Seed the local store and copy the live approved art into it."""
    store_dir.mkdir(parents=True, exist_ok=True)
    source = get_settings()
    if not source.effective_database_url.startswith("postgres"):
        raise SystemExit(
            "DATABASE_URL in .env must point at the live database to copy its art from"
        )
    local_engine = prepare_database(build_engine(local_database_url(store_dir)))
    live_engine: Engine = build_engine(source.effective_database_url)
    manifest_path = store_dir / MANIFEST
    manifest: dict[str, dict[str, str]] = (
        json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    )
    try:
        set_blob_store(None)
        local = TemplateStore(local_engine)
        local.seed(SEEDS)
        report = copy_approved_art(
            TemplateStore(live_engine),
            local,
            manifest,
            on_live=lambda: install_blob_store(source),
            on_local=lambda: set_blob_store(None),
            out=out,
        )
    finally:
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        set_blob_store(None)
        live_engine.dispose()
        local_engine.dispose()
    for key in report.skipped_stories:
        print(f"  {key} is on live but has no seed here; left out", file=out)
    print(f"local store ready: {report.copied} copied, {report.kept} already here", file=out)
    return report


def serve(store_dir: Path, port: int) -> None:
    """Replace this process with uvicorn running the app on the local store."""
    account = get_settings().gemini_web_account.strip()
    if not account:
        raise SystemExit("set GEMINI_WEB_ACCOUNT in .env to the Google account Chrome uses")
    environment = local_environment(store_dir, dict(os.environ))
    uvicorn = Path(sys.executable).with_name("uvicorn")
    arguments = [str(uvicorn), "app.main:create_app", "--factory", "--port", str(port)]
    print(f"serving the local book on http://localhost:{port} (Gemini in Chrome)")
    os.execve(arguments[0], arguments, environment)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument(
        "--prepare", action="store_true", help="prepare the store, don't serve"
    )
    args = parser.parse_args(argv)
    store_dir = args.store.resolve()
    prepare(store_dir)
    if not args.prepare:
        serve(store_dir, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
