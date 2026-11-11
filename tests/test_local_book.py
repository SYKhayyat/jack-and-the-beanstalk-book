"""`make local-book`: a local store copied from live, served with Gemini in Chrome.

The live side here is a second throwaway database standing in for Supabase:
the copy only reads it, brings every approved picture across once, and a
second run downloads nothing it already has.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path

from sqlalchemy import Engine

from app.books import SEEDS, StorySeed
from app.db import build_engine
from app.scripts.local_book import (
    LOCAL_ENV,
    CopyReport,
    copy_approved_art,
    local_environment,
)
from app.services.template_store import PlateStatus, TemplateStore

from .test_template_portraits import _approve_every_plate

RED: StorySeed = next(seed for seed in SEEDS if seed.key == "little_red_riding_hood")
CUPBOARD = 9  # a page without Red


def _second_engine(tmp_path: Path, schema_template: Path) -> Engine:
    path = tmp_path / "local.sqlite3"
    shutil.copyfile(schema_template, path)
    return build_engine(f"sqlite:///{path}")


def _live(engine: Engine) -> TemplateStore:
    live = TemplateStore(engine)
    live.seed(SEEDS)
    _approve_every_plate(live, RED)
    portrait = live.add_portrait(RED.key, "wolf", image=b"wolf", media_type="image/png")
    live.approve_portrait(portrait, operator="owner")
    premade = live.add_premade(RED.key, CUPBOARD, image=b"cupboard", media_type="image/jpeg")
    live.approve_premade(premade, operator="owner")
    # A draft is the owner's work in progress: never copied.
    live.add_portrait(RED.key, "mother", image=b"draft", media_type="image/png")
    return live


def _approved(items: list) -> list:  # type: ignore[type-arg]
    return [item for item in items if item.status == PlateStatus.APPROVED]


def test_copies_every_approved_picture_once(
    engine: Engine, tmp_path: Path, schema_template: Path
) -> None:
    live = _live(engine)
    local_engine = _second_engine(tmp_path, schema_template)
    local = TemplateStore(local_engine)
    local.seed(SEEDS)
    manifest: dict[str, dict[str, str]] = {}
    switches: list[str] = []
    out = io.StringIO()

    def run() -> CopyReport:
        return copy_approved_art(
            live,
            local,
            manifest,
            on_live=lambda: switches.append("live"),
            on_local=lambda: switches.append("local"),
            out=out,
        )

    first = run()

    live_plates = _approved(live.plates(RED.key))
    assert first.copied == len(live_plates) + 2
    local_plates = _approved(local.plates(RED.key))
    assert [p.prompt for p in local_plates] == [p.prompt for p in live_plates]
    assert local.plate_image(local_plates[0].id) == live.plate_image(live_plates[0].id)
    portraits = _approved(local.portraits(RED.key))
    assert [(p.cast_key, local.portrait_image(p.id)[0]) for p in portraits] == [
        ("wolf", b"wolf")
    ]
    premades = _approved(local.premades(RED.key))
    assert [local.premade_image(p.id)[0] for p in premades] == [b"cupboard"]
    assert local.story_state(RED.key).version_number is not None
    # Every read of live happened with live's blob store in place.
    assert switches[0] == "live"

    second = run()
    assert second.copied == 0
    assert second.kept == first.copied
    local_engine.dispose()


def test_a_newly_approved_live_picture_replaces_the_local_copy(
    engine: Engine, tmp_path: Path, schema_template: Path
) -> None:
    live = _live(engine)
    local_engine = _second_engine(tmp_path, schema_template)
    local = TemplateStore(local_engine)
    local.seed(SEEDS)
    manifest: dict[str, dict[str, str]] = {}
    out = io.StringIO()
    copy_approved_art(
        live, local, manifest, on_live=lambda: None, on_local=lambda: None, out=out
    )

    newer = live.add_portrait(RED.key, "wolf", image=b"wolf 2", media_type="image/png")
    live.approve_portrait(newer, operator="owner")
    again = copy_approved_art(
        live, local, manifest, on_live=lambda: None, on_local=lambda: None, out=out
    )

    assert again.copied == 1
    portraits = _approved(local.portraits(RED.key, "wolf"))
    assert [local.portrait_image(p.id)[0] for p in portraits] == [b"wolf 2"]
    local_engine.dispose()


def test_the_served_app_touches_nothing_live_and_pays_for_nothing(tmp_path: Path) -> None:
    environment = local_environment(
        tmp_path,
        {
            "DATABASE_URL": "postgresql://live",
            "R2_BUCKET": "plates",
            "STRIPE_SECRET_KEY": "sk_live",
            "GEMINI_WEB_ACCOUNT": "owner@example.com",
        },
    )

    for name in ("DATABASE_URL", "R2_BUCKET", "STRIPE_SECRET_KEY", "GOOGLE_CLIENT_ID"):
        assert environment[name] == ""
    assert environment["STORAGE_DIR"] == str(tmp_path)
    assert environment["GEMINI_WEB_ACCOUNT"] == "owner@example.com"
    for backend in ("IMAGE_PROVIDER", "REVIEWER_PROVIDER", "DECISION_JUDGE_PROVIDER"):
        assert LOCAL_ENV[backend] == "gemini_web"
