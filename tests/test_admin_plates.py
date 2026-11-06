"""The plate review screen (T6): who may use it, and what each action does."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import event

from app.books import SEEDS, StorySeed
from app.config import Settings
from app.main import create_app
from app.services.image_providers.base import ImageRequest
from app.services.plates import PlatePlan, plate_plans
from app.services.template_store import (
    COVER_PAGE,
    PAGE_NUMBERS,
    PlateStatus,
    TemplateStore,
)
from app.services.user_store import User
from app.sessions import SESSION_COOKIE
from tests.helpers import RecordingProvider, tiny_png_bytes

OPERATOR = "operator-sub"
VISITOR = "somebody-else"

RED: StorySeed = next(seed for seed in SEEDS if seed.key == "little_red_riding_hood")
STORY = RED.key
PLATES = "/admin/plates"


@pytest.fixture
def admin_settings(tmp_path: Path, schema_template: Path) -> Settings:
    """A Google deployment whose operator is one named account."""
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(schema_template, storage / "app.sqlite3")
    return Settings(
        image_provider="mock",  # type: ignore[arg-type]
        app_env="test",
        storage_dir=storage,
        google_client_id="cid",
        google_client_secret="cs",
        session_secret="session-secret",
        operator_sub=OPERATOR,
    )


@pytest.fixture
def admin(
    admin_settings: Settings, provider: RecordingProvider
) -> Iterator[tuple[TestClient, TemplateStore]]:
    """A signed-in operator, a seeded story, and its template store."""
    app = create_app(admin_settings, provider=provider)
    with TestClient(app) as client:
        store = TemplateStore(client.app.state.container.engine)
        store.seed(SEEDS)
        _sign_in(client, OPERATOR)
        yield client, store


def _sign_in(client: TestClient, sub: str) -> None:
    """Mint the session the Google callback would, without the network."""
    client.app.state.container.user_store.upsert(User(sub=sub, name=sub))
    token = client.app.state.sessions.issue_google(  # type: ignore[attr-defined]
        sub=sub, name=sub, ttl_seconds=3600
    )
    client.cookies.set(SESSION_COOKIE, token)


def _plan(page_number: int) -> PlatePlan:
    return next(plan for plan in plate_plans(RED) if plan.page_number == page_number)


def _paint(store: TemplateStore, page_number: int, *, image: bytes | None = None) -> str:
    """A draft plate on one page, with the layout the planner would make."""
    plan = _plan(page_number)
    return store.add_plate(
        STORY,
        page_number,
        image=image or tiny_png_bytes(),
        media_type="image/png",
        prompt=plan.prompt,
        slots=plan.slots,
    )


def _noisy_png(size: tuple[int, int] = (200, 150)) -> bytes:
    """A plate whose top band is busy: random luminance, no calm strip."""
    image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _plate_ids(store: TemplateStore, page_number: int) -> dict[str, str]:
    return {plate.status: plate.id for plate in store.plates(STORY, page_number)}


class TestTheGate:
    def test_a_signed_in_visitor_gets_the_same_404_as_a_missing_page(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, store = admin
        plate_id = _paint(store, 1)
        _sign_in(client, VISITOR)

        for url in (
            PLATES,
            f"{PLATES}/{STORY}",
            f"{PLATES}/{STORY}/1",
            f"{PLATES}/{STORY}/{plate_id}.png",
        ):
            response = client.get(url)
            assert response.status_code == 404, url
            assert "admin" not in response.text.lower()

        assert client.post(f"{PLATES}/{STORY}/1/{plate_id}/approve").status_code == 404
        assert client.post(f"{PLATES}/{STORY}/1/reroll").status_code == 404
        assert client.post(f"{PLATES}/{STORY}/1/{plate_id}/retire").status_code == 404
        # Nothing an outsider attempted changed anything.
        assert store.plate_status(plate_id) == PlateStatus.DRAFT

    def test_the_sign_in_gate_still_comes_first(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, _store = admin
        client.cookies.clear()

        response = client.get(PLATES, follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

    def test_the_operator_sees_the_stories_and_their_progress(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, _store = admin

        page = client.get(PLATES)

        assert page.status_code == 200
        assert RED.title in page.text
        assert f"0 of {len(PAGE_NUMBERS)} plates approved" in page.text
        assert "no complete version yet" in page.text

    def test_an_unknown_story_or_page_is_404(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, _store = admin

        assert client.get(f"{PLATES}/no-such-story").status_code == 404
        assert client.get(f"{PLATES}/{STORY}/21").status_code == 404
        assert client.get(f"{PLATES}/{STORY}/-1").status_code == 404


class TestContactSheet:
    def test_all_twenty_one_pages_are_on_the_sheet(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, _store = admin

        sheet = client.get(f"{PLATES}/{STORY}")

        assert sheet.status_code == 200
        assert sheet.text.count('class="sheet-tile') == len(PAGE_NUMBERS)
        assert "No plate yet" in sheet.text

    def test_every_slot_rectangle_is_the_stored_one(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, store = admin
        _paint(store, 1)
        stored = store.plates(STORY, 1)[0]

        sheet = client.get(f"{PLATES}/{STORY}")

        assert stored.slots
        for slot in stored.slots:
            rect = ",".join(f"{value:.4f}" for value in slot.rect)
            assert f'data-cast="{slot.cast_key}" data-rect="{rect}"' in sheet.text
            assert (
                f"left:{slot.rect[0] * 100:.2f}%;top:{slot.rect[1] * 100:.2f}%;"
                f"width:{slot.rect[2] * 100:.2f}%;height:{slot.rect[3] * 100:.2f}%"
            ) in sheet.text

    def test_the_cover_shows_its_reserved_title_band(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, store = admin
        _paint(store, COVER_PAGE)

        sheet = client.get(f"{PLATES}/{STORY}")

        assert 'class="title-band" style="height:22.00%"' in sheet.text

    def test_the_overlay_can_be_toggled_without_javascript(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, _store = admin

        sheet = client.get(f"{PLATES}/{STORY}")

        assert 'id="show-overlay"' in sheet.text
        assert 'class="overlay-host"' in sheet.text


class TestApprove:
    def test_approving_records_the_operator_and_the_time(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, store = admin
        plate_id = _paint(store, 1)

        response = client.post(
            f"{PLATES}/{STORY}/1/{plate_id}/approve", follow_redirects=False
        )

        assert response.status_code == 303
        plate = store.plates(STORY, 1)[0]
        assert plate.status == PlateStatus.APPROVED
        assert plate.approved_by == OPERATOR
        assert plate.approved_at is not None
        assert plate.prompt_hash == hashlib.sha256(_plan(1).prompt.encode()).hexdigest()

    def test_approve_is_idempotent(self, admin: tuple[TestClient, TemplateStore]) -> None:
        client, store = admin
        plate_id = _paint(store, 1)
        client.post(f"{PLATES}/{STORY}/1/{plate_id}/approve")
        first = store.current_version(STORY)

        again = client.post(f"{PLATES}/{STORY}/1/{plate_id}/approve", follow_redirects=False)

        assert again.status_code == 303
        assert store.current_version(STORY) == first
        assert _plate_ids(store, 1) == {PlateStatus.APPROVED: plate_id}

    def test_the_last_page_approves_the_story(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, store = admin
        assert store.current_version(STORY) is None

        for number in PAGE_NUMBERS:
            plate_id = _paint(store, number)
            client.post(f"{PLATES}/{STORY}/{number}/{plate_id}/approve")

        version = store.current_version(STORY)
        assert version is not None
        assert version.number == 1
        sheet = client.get(f"{PLATES}/{STORY}")
        assert f"template version {version.number}" in sheet.text
        assert f"{len(PAGE_NUMBERS)} of {len(PAGE_NUMBERS)} plates painted" in sheet.text
        assert f"{len(PAGE_NUMBERS)} approved" in sheet.text

    def test_replacing_an_approved_plate_writes_a_new_version(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, store = admin
        for number in PAGE_NUMBERS:
            client.post(f"{PLATES}/{STORY}/{number}/{_paint(store, number)}/approve")
        first = store.current_version(STORY)
        assert first is not None

        replacement = _paint(store, 3)
        client.post(f"{PLATES}/{STORY}/3/{replacement}/approve")

        second = store.current_version(STORY)
        assert second is not None
        assert second.number == first.number + 1
        # The old version still names the plate it was pinned with.
        assert next(p for p in first.pages if p.page_number == 3).plate_id != replacement

    def test_a_plate_cannot_be_approved_through_some_other_page(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, store = admin
        plate_id = _paint(store, 2)

        response = client.post(
            f"{PLATES}/{STORY}/5/{plate_id}/approve", follow_redirects=False
        )

        assert response.status_code == 404
        assert store.plate_status(plate_id) == PlateStatus.DRAFT

    def test_a_busy_cover_cannot_be_approved(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, store = admin
        plate_id = _paint(store, COVER_PAGE, image=_noisy_png())

        page = client.get(f"{PLATES}/{STORY}/{COVER_PAGE}")
        assert page.status_code == 200
        assert "Approve is off" in page.text
        assert "too busy for the title" in page.text
        assert f"/{plate_id}/approve" not in page.text

        response = client.post(
            f"{PLATES}/{STORY}/{COVER_PAGE}/{plate_id}/approve", follow_redirects=False
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        assert store.plate_status(plate_id) == PlateStatus.DRAFT

    def test_a_calm_cover_offers_approve(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, store = admin
        plate_id = _paint(store, COVER_PAGE)

        page = client.get(f"{PLATES}/{STORY}/{COVER_PAGE}")

        assert f"/{plate_id}/approve" in page.text
        assert "Approve is off" not in page.text


class TestReroll:
    def test_a_reroll_adds_a_draft_and_never_touches_the_approved_plate(
        self,
        admin: tuple[TestClient, TemplateStore],
        provider: RecordingProvider,
    ) -> None:
        client, store = admin
        approved_id = _paint(store, 1)
        client.post(f"{PLATES}/{STORY}/1/{approved_id}/approve")

        response = client.post(
            f"{PLATES}/{STORY}/1/reroll",
            data={"prompt": "a different path"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert provider.requests[-1].prompt == "a different path"
        assert provider.requests[-1].label == "plate"
        fresh = store.plates(STORY, 1)
        by_status: dict[str, list[str]] = {}
        for plate in fresh:
            by_status.setdefault(plate.status, []).append(plate.id)
        assert by_status[PlateStatus.APPROVED] == [approved_id]
        assert len(by_status[PlateStatus.DRAFT]) == 1

    def test_an_empty_prompt_repaints_the_planned_one(
        self,
        admin: tuple[TestClient, TemplateStore],
        provider: RecordingProvider,
    ) -> None:
        client, _store = admin

        client.post(f"{PLATES}/{STORY}/4/reroll", data={"prompt": ""})

        sent: ImageRequest = provider.requests[-1]
        assert sent.prompt == _plan(4).prompt

    def test_a_rerolled_plate_keeps_the_page_layout(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, store = admin
        client.post(f"{PLATES}/{STORY}/2/reroll", data={"prompt": "x"})

        plate = store.plates(STORY, 2)[0]

        assert plate.slots == _plan(2).slots

    def test_a_change_note_is_added_to_the_planned_prompt(
        self,
        admin: tuple[TestClient, TemplateStore],
        provider: RecordingProvider,
    ) -> None:
        """The operator says what to change; the page keeps its own spec."""
        client, store = admin

        client.post(f"{PLATES}/{STORY}/2/reroll", data={"change": "  warmer   light "})

        text = f"{_plan(2).prompt}\n\nCHANGE REQUESTED: warmer light"
        assert provider.requests[-1].prompt == text
        # Stored as sent, so the screen can always show what made the picture.
        assert store.plates(STORY, 2)[0].prompt == text
        assert store.plates(STORY, 2)[0].slots == _plan(2).slots

    def test_a_change_from_the_sheet_comes_back_to_the_sheet(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, _store = admin
        _paint(_store, 3)

        response = client.post(
            f"{PLATES}/{STORY}/3/reroll",
            data={"change": "a wider path", "back": "sheet"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == f"{PLATES}/{STORY}?painted=3"

    def test_each_painted_tile_has_its_own_change_box(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, store = admin
        _paint(store, 1)

        sheet = client.get(f"{PLATES}/{STORY}")

        assert sheet.text.count('placeholder="What should change?"') == 1
        assert f'action="{PLATES}/{STORY}/1/reroll"' in sheet.text
        assert 'name="back" value="sheet"' in sheet.text

    def test_the_review_screen_asks_what_should_change(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, store = admin
        _paint(store, 1)

        review = client.get(f"{PLATES}/{STORY}/1")

        assert 'name="change"' in review.text
        assert "What should change?" in review.text
        # The whole prompt is still reachable, one fold down.
        assert "Edit the whole prompt instead" in review.text
        assert '<textarea id="prompt" name="prompt"' in review.text
        assert "TASK: Paint one EMPTY background plate" in review.text


class TestRetire:
    def test_retiring_a_draft_leaves_the_approved_plate_alone(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, store = admin
        approved_id = _paint(store, 1)
        client.post(f"{PLATES}/{STORY}/1/{approved_id}/approve")
        draft_id = _paint(store, 1)

        response = client.post(f"{PLATES}/{STORY}/1/{draft_id}/retire", follow_redirects=False)

        assert response.status_code == 303
        assert store.plate_status(draft_id) == PlateStatus.RETIRED
        assert store.plate_status(approved_id) == PlateStatus.APPROVED


class TestPlateImage:
    def test_the_operator_can_see_a_plate_but_no_cache_may(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, store = admin
        image = tiny_png_bytes(colour="#a83a4e")
        plate_id = _paint(store, 1, image=image)

        response = client.get(f"{PLATES}/{STORY}/{plate_id}.png")

        assert response.status_code == 200
        assert response.content == image
        assert response.headers["content-type"] == "image/png"
        assert "no-store" in response.headers["cache-control"]

    def test_an_unknown_plate_is_404(self, admin: tuple[TestClient, TemplateStore]) -> None:
        client, _store = admin

        assert client.get(f"{PLATES}/{STORY}/deadbeef.png").status_code == 404


class TestTheLog:
    def test_an_action_is_logged_by_name_and_never_with_image_bytes(
        self, admin: tuple[TestClient, TemplateStore], caplog: pytest.LogCaptureFixture
    ) -> None:
        client, store = admin
        plate_id = _paint(store, 1)

        with caplog.at_level(logging.INFO, logger="app.routes.admin"):
            client.post(f"{PLATES}/{STORY}/1/{plate_id}/approve")

        lines = [record.getMessage() for record in caplog.records]
        approved = [line for line in lines if "admin.plate_approved" in line]
        assert len(approved) == 1
        assert STORY in approved[0]
        assert plate_id in approved[0]
        assert OPERATOR in approved[0]
        assert max(len(line) for line in lines) < 300


class TestPaintMissing:
    def test_one_press_paints_the_first_missing_page(
        self,
        admin: tuple[TestClient, TemplateStore],
        provider: RecordingProvider,
    ) -> None:
        client, store = admin

        response = client.post(f"{PLATES}/{STORY}/paint", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"].endswith("?painted=0")
        assert provider.requests[-1].prompt == _plan(0).prompt
        painted = store.plates(STORY, 0)
        assert [(p.status, p.slots) for p in painted] == [(PlateStatus.DRAFT, _plan(0).slots)]

    def test_the_loop_paints_every_page_and_then_stops(
        self,
        admin: tuple[TestClient, TemplateStore],
        provider: RecordingProvider,
    ) -> None:
        client, store = admin

        pages = []
        for _ in PAGE_NUMBERS:
            payload = client.post(f"{PLATES}/{STORY}/paint.json").json()
            pages.append(payload["page"])
        assert pages == list(PAGE_NUMBERS)
        assert len(provider.requests) == len(PAGE_NUMBERS)

        exhausted = client.post(f"{PLATES}/{STORY}/paint.json").json()
        assert exhausted["page"] is None
        assert exhausted["remaining"] == 0
        assert exhausted["total"] == len(PAGE_NUMBERS)
        assert len(provider.requests) == len(PAGE_NUMBERS)
        assert all(
            store.plates(STORY, number)[0].status == PlateStatus.DRAFT
            for number in PAGE_NUMBERS
        )

    def test_an_approved_page_is_never_repainted(
        self,
        admin: tuple[TestClient, TemplateStore],
        provider: RecordingProvider,
    ) -> None:
        client, store = admin
        approved_id = _paint(store, 0)
        client.post(f"{PLATES}/{STORY}/0/{approved_id}/approve")

        client.post(f"{PLATES}/{STORY}/paint.json")

        assert [p.id for p in store.plates(STORY, 0)] == [approved_id]
        assert provider.requests and provider.requests[-1].prompt == _plan(1).prompt
        assert len(store.plates(STORY, 1)) == 1

    def test_a_retired_page_is_painted_again(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, store = admin
        retired_id = _paint(store, 0)
        store.retire_plate(retired_id)

        client.post(f"{PLATES}/{STORY}/paint.json")

        statuses = {p.status for p in store.plates(STORY, 0)}
        assert statuses == {PlateStatus.RETIRED, PlateStatus.DRAFT}

    def test_painting_is_operator_only(
        self,
        admin: tuple[TestClient, TemplateStore],
        provider: RecordingProvider,
    ) -> None:
        client, store = admin
        before = len(provider.requests)
        _sign_in(client, VISITOR)

        response = client.post(f"{PLATES}/{STORY}/paint.json")

        assert response.status_code == 404
        assert len(provider.requests) == before
        assert store.plates(STORY, 0) == []

    def test_the_sheet_offers_the_button_and_its_count(
        self,
        admin: tuple[TestClient, TemplateStore],
    ) -> None:
        client, _store = admin

        sheet = client.get(f"{PLATES}/{STORY}")
        assert "data-paint" in sheet.text
        assert f"0 of {len(PAGE_NUMBERS)} plates painted" in sheet.text
        assert "Paint missing plates" in sheet.text

        client.post(f"{PLATES}/{STORY}/paint.json")
        sheet = client.get(f"{PLATES}/{STORY}")
        assert f"1 of {len(PAGE_NUMBERS)} plates painted" in sheet.text


class TestTheSheetStaysOneQueryPerKind:
    def test_it_does_not_query_once_per_page(
        self, admin: tuple[TestClient, TemplateStore]
    ) -> None:
        client, _store = admin
        engine = client.app.state.container.engine
        seen: list[str] = []

        def _record(*args: object) -> None:
            seen.append(str(args[2]))

        event.listen(engine, "before_cursor_execute", _record)
        try:
            assert client.get(f"{PLATES}/{STORY}").status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", _record)

        # Seven statements with the theme; twenty-one page queries would be the
        # regression this guards: on Postgres that was a ten-second screen.
        assert len(seen) <= 10, seen
