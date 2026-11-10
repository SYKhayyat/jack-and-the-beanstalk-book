"""The operator's status screens (UI.org Q6) and the studio history behind them (Q8).

Who may see them, what they say about the live backends and the review loop,
and that the provider's name stays off every visitor surface.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.user_store import User
from app.sessions import SESSION_COOKIE
from tests.helpers import JACK_KEY, RecordingProvider, make_photo_bytes

OPERATOR = "operator-sub"
OPERATOR_EMAIL = "the-operator@example.com"
VISITOR = "somebody-else"


@pytest.fixture
def operator_settings(tmp_path: Path, schema_template: Path) -> Settings:
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
        operator_sub=f"{OPERATOR}, {OPERATOR_EMAIL}",
    )


@pytest.fixture
def site(operator_settings: Settings, provider: RecordingProvider) -> Iterator[TestClient]:
    app = create_app(operator_settings, provider=provider)
    with TestClient(app) as client:
        yield client


def _sign_in(client: TestClient, sub: str, *, email: str | None = None) -> None:
    address = email or f"{sub}@example.com"
    client.app.state.container.user_store.upsert(  # type: ignore[attr-defined]
        User(sub=sub, name=sub, email=address)
    )
    token = client.app.state.sessions.issue_google(  # type: ignore[attr-defined]
        sub=sub, name=sub, email=address, ttl_seconds=3600
    )
    client.cookies.set(SESSION_COOKIE, token)


def _portrait(client: TestClient, reasons: tuple[str, ...] = ()) -> str:
    data: dict[str, object] = {"story": JACK_KEY, "photo_gender": "girl"}
    if reasons:
        data["reason"] = list(reasons)
    response = client.post(
        "/api/cartoonify",
        files={"photo": ("me.png", make_photo_bytes(), "image/png")},
        data=data,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["generation_id"])


def _book(client: TestClient) -> str:
    started = client.post(
        "/api/book/start", data={"story": JACK_KEY, "generation_id": _portrait(client)}
    )
    assert started.status_code == 201, started.text
    book_id = str(started.json()["book_id"])
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if client.get(f"/api/book/{book_id}").json()["status"] in {"complete", "failed"}:
            return book_id
        time.sleep(0.05)
    raise AssertionError("the book never settled")


class TestWhoMaySeeIt:
    @pytest.mark.parametrize(
        "path", ["/admin", "/admin/status", "/admin/books/x", "/admin/books/x/art/0"]
    )
    def test_a_visitor_gets_the_404_an_unknown_url_gets(
        self, site: TestClient, path: str
    ) -> None:
        _sign_in(site, VISITOR)
        response = site.get(path, follow_redirects=False)
        assert response.status_code == 404

    def test_the_operator_lands_on_the_status_page(self, site: TestClient) -> None:
        _sign_in(site, OPERATOR)
        response = site.get("/admin", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/status"

    def test_a_visitor_cannot_see_another_book_s_pictures_through_it(
        self, site: TestClient
    ) -> None:
        _sign_in(site, OPERATOR)
        book_id = _book(site)
        _sign_in(site, VISITOR)
        assert site.get(f"/admin/books/{book_id}/art/1").status_code == 404


class TestWhoCountsAsTheOperator:
    def test_an_account_listed_by_email_is_the_operator(self, site: TestClient) -> None:
        """`OPERATOR_SUB` takes emails too, so nobody has to decode a OAuth sub."""
        _sign_in(site, VISITOR, email=OPERATOR_EMAIL)
        page = site.get("/admin/status")
        assert page.status_code == 200
        assert "the-operator@example.com" in page.text

    def test_an_email_nobody_listed_is_a_404(self, site: TestClient) -> None:
        _sign_in(site, VISITOR, email="not-the-operator@example.com")
        assert site.get("/admin/status").status_code == 404

    def test_the_operator_gets_a_way_in_and_a_visitor_does_not(self, site: TestClient) -> None:
        _sign_in(site, OPERATOR)
        assert 'href="/admin"' in site.get("/").text
        _sign_in(site, VISITOR)
        assert 'href="/admin"' not in site.get("/").text

    def test_the_operator_can_remove_a_book(self, site: TestClient) -> None:
        """The delete the visitor has, available for the copies they cannot reach."""
        _sign_in(site, OPERATOR)
        book_id = _book(site)
        response = site.post(f"/admin/books/{book_id}/delete", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/status"
        assert site.app.state.container.book_store.load_record(book_id) is None  # type: ignore[attr-defined]

    def test_the_workshop_says_what_it_can_do(self, site: TestClient) -> None:
        _sign_in(site, OPERATOR)
        page = site.get("/admin/status").text
        assert "Everything, at a glance" in page
        assert "Review plates" in page
        assert "OPERATOR_SUB" in page


class TestWhatItSays:
    def test_it_names_the_live_backends_and_says_when_the_reviewer_is_off(
        self, site: TestClient
    ) -> None:
        _sign_in(site, OPERATOR)
        page = site.get("/admin/status")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "private, no-store"
        assert "recording" in page.text  # the provider, printed for the operator only
        assert "OFF — pictures get tier 1 only" in page.text
        assert "OFF — every picture goes to the reviewer" in page.text  # no judge in tests
        assert "no plates — pages are composed from scratch" in page.text

    def test_a_book_shows_every_attempt_with_what_drew_it(self, site: TestClient) -> None:
        _sign_in(site, OPERATOR)
        book_id = _book(site)

        status = site.get("/admin/status").text
        assert f"/admin/books/{book_id}" in status
        assert f"{OPERATOR}@example.com" in status

        detail = site.get(f"/admin/books/{book_id}")
        assert detail.status_code == 200
        # Every picture has its verdict row, stamped with the backend that drew it.
        assert detail.text.count("recording · recording-v1") >= 21
        assert "Page 20" in detail.text and "Cover" in detail.text

        art = site.get(f"/admin/books/{book_id}/art/1")
        assert art.status_code == 200
        assert art.headers["cache-control"] == "private, no-store"
        assert site.get(f"/admin/books/{book_id}/art/0").status_code == 200

    def test_an_unknown_book_is_a_404(self, site: TestClient) -> None:
        _sign_in(site, OPERATOR)
        assert site.get("/admin/books/nope").status_code == 404


class TestTheStudioHistory:
    def test_every_portrait_and_its_chips_are_kept_and_a_book_leads_back_to_them(
        self, site: TestClient
    ) -> None:
        _sign_in(site, OPERATOR)
        first = _portrait(site)
        second = _portrait(site, reasons=("face_changed", "hair_wrong"))
        chosen = site.post(f"/api/cartoonify/{second}/choose", data={"index": 0})
        assert chosen.status_code == 201, chosen.text
        chosen_id = chosen.json()["generation_id"]

        log = site.app.state.container.portraits  # type: ignore[attr-defined]
        lineage = log.lineage(chosen_id)
        assert [item.kind for item in lineage] == ["choose", "draw"]
        assert lineage[0].chosen_index == 0 and lineage[0].source_id == second
        assert lineage[1].rejection == ("face_changed", "hair_wrong")
        assert lineage[1].settings["photo_gender"] in {"boy", "girl"}
        assert lineage[1].provider == "recording"
        assert {item.id for item in log.recent()} >= {first, second, chosen_id}
        assert all(item.user_id == OPERATOR for item in log.recent())


class TestVisitorSurfaces:
    def test_the_wallet_never_names_the_provider(
        self, tmp_path: Path, schema_template: Path, provider: RecordingProvider
    ) -> None:
        from tests.test_wallet_routes import FakeStripe

        storage = tmp_path / "wallet-storage"
        storage.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(schema_template, storage / "app.sqlite3")
        settings = Settings(
            image_provider="mock",  # type: ignore[arg-type]
            app_env="test",
            storage_dir=storage,
            google_client_id="cid",
            google_client_secret="cs",
            session_secret="session-secret",
        )
        app = create_app(settings, provider=provider, payments=FakeStripe())
        with TestClient(app) as client:
            _sign_in(client, VISITOR)
            page = client.get("/wallet")
        assert page.status_code == 200
        assert "recording" not in page.text
        assert "active provider" not in page.text
