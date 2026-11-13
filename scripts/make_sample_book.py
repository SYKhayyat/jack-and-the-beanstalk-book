#!/usr/bin/env python3
"""Make the hand-in sample PDF, offline and for free.

The sample has to be a file the app assembled, not a mock-up drawn by hand.
This drives the real HTTP flow against the real SQLite store with the
`MockImageProvider`, so no API key is read and nothing is billed:

* one Red Riding Hood book starring Sarah;
* the mock artist draws the twenty interior pages (labelled demo pictures);
* the front cover and the About portrait are the story's own public cover
  art, so the handout shows the layout a real book gets, not a placeholder
  rectangle;
* the PDF is written to `docs/sample-book.pdf`.

    python -m scripts.make_sample_book
"""

from __future__ import annotations

import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from fastapi.testclient import TestClient

from app.books import SEEDS
from app.config import Settings
from app.db import build_engine, prepare_database
from app.main import create_app
from app.services.db_book_store import UnreviewedImages
from app.services.image_providers.mock import MockImageProvider
from app.services.template_store import TemplateStore

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "sample-book.pdf"
COVER = ROOT / "app" / "static" / "covers" / "little_red_riding_hood" / "ava.jpg"
STORY = "little_red_riding_hood"


def main() -> int:
    if not COVER.exists():
        print(f"missing cover art: {COVER}", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="sample-book-") as tmp:
        storage = Path(tmp) / "storage"
        storage.mkdir()
        engine = build_engine(f"sqlite:///{storage / 'app.sqlite3'}")
        try:
            prepare_database(engine)
            TemplateStore(engine).seed(SEEDS)
        finally:
            engine.dispose()

        # Never read the developer's .env: this script must not touch a real
        # database, a real Stripe key or a real image provider.
        Settings.model_config["env_file"] = None
        settings = Settings(
            image_provider="mock",
            app_env="test",
            storage_dir=storage,
            database_url="",
            session_secret="sample-book-only",
            google_client_id="",
            google_client_secret="",
            stripe_secret_key="",
            gemini_api_key="",
            openai_api_key="",
            agnes_api_key="",
            peecho_api_key="",
            max_generations_per_minute=1000,
        )
        app = create_app(settings, provider=MockImageProvider())
        with TestClient(app) as client:
            photo = COVER.read_bytes()
            portrait = client.post(
                "/api/cartoonify",
                files={"photo": ("me.jpg", photo, "image/jpeg")},
                data={"story": STORY, "photo_gender": "girl"},
            )
            portrait.raise_for_status()
            started = client.post(
                "/api/book/start",
                data={
                    "story": STORY,
                    "generation_id": portrait.json()["generation_id"],
                    "child_name": "Sarah",
                    "about_age": 6,
                    "about_title": "a forest ranger",
                    "details": "Loves wolves, red boots and pancakes.",
                    "dedication": "For Sarah, who is not afraid of the dark.",
                    "photo_gender": "girl",
                },
            )
            started.raise_for_status()
            book_id = str(started.json()["book_id"])

            deadline = time.monotonic() + 300
            status: dict[str, object] = {}
            while time.monotonic() < deadline:
                status = dict(client.get(f"/api/book/{book_id}").json())
                if status.get("status") in {"complete", "failed"}:
                    break
                time.sleep(0.05)
            if status.get("status") != "complete":
                print(
                    f"the book did not finish: {status.get('error', status)}",
                    file=sys.stderr,
                )
                return 1

            container = client.app.state.container
            # The public cover plate is a finished illustration; the mock
            # artist only has placeholders. Use it for the two places a real
            # book sells itself, and keep the interior honestly labelled.
            container.book_store.save_cover(book_id, photo)
            container.book_store.save_portrait(book_id, photo)
            # The new cover's bytes have no verdict, so the *status* write is
            # refused; the PDF itself is already written, which is what a
            # handout needs. The book's status stays as it was.
            with suppress(UnreviewedImages):
                container.book_runner.rebuild_pdf(book_id)

            pdf = client.get(f"/book-media/{book_id}/pdf")
            pdf.raise_for_status()
            OUT.write_bytes(pdf.content)
            print(f"wrote {OUT.relative_to(ROOT)} ({len(pdf.content):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
