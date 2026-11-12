"""The operator's stop buttons, and the mail an alert sends (Idea 10).

Two switches, because two different things can need stopping:

* `new_books` refuses a portrait, a new book and a redraw *before* anything is
  read or charged — but an About edit and a delete are text and a removal, so
  they keep working.
* `drawing` finishes the picture in flight, leaves the book resumable with the
  pictures it already has, and releasing it starts those books again.

And the alert post: the log and the operator page already carry every threshold;
this is the one message that says they changed, so a problem is not re-mailed
every hour and is said again if it comes back.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.alerts import Alert, OperatorPost, alert_mail
from app.services.mailer import Mailer
from app.services.user_store import User, UserStore
from app.sessions import SESSION_COOKIE
from tests.helpers import JACK_KEY, RecordingProvider, make_photo_bytes

OPERATOR = "operator-sub"


@contextmanager
def _site(
    tmp_path: Path, seeded_template: Path, **overrides: Any
) -> Iterator[tuple[TestClient, RecordingProvider]]:
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "app.sqlite3").write_bytes(seeded_template.read_bytes())
    settings = Settings(
        image_provider="mock",  # type: ignore[arg-type]
        app_env="test",
        storage_dir=storage,
        google_client_id="cid",
        google_client_secret="cs",
        session_secret="session-secret",
        operator_sub=OPERATOR,
        max_generations_per_minute=1000,
        **overrides,
    )
    provider = RecordingProvider()
    with TestClient(create_app(settings, provider=provider)) as client:
        _sign_in(client, OPERATOR)
        yield client, provider


def _sign_in(client: TestClient, sub: str) -> None:
    client.app.state.container.user_store.upsert(  # type: ignore[attr-defined]
        User(sub=sub, name=sub, email=f"{sub}@example.com")
    )
    token = client.app.state.sessions.issue_google(  # type: ignore[attr-defined]
        sub=sub, name=sub, email=f"{sub}@example.com", ttl_seconds=3600
    )
    client.cookies.set(SESSION_COOKIE, token)


def _pull(client: TestClient, name: str, on: bool = True, *, sub: str = OPERATOR) -> int:
    client.cookies.set(SESSION_COOKIE, "")  # a fresh sign-in below
    _sign_in(client, sub)
    response = client.post(
        "/admin/switches", data={"name": name, "on": "1" if on else "0"}, follow_redirects=False
    )
    return response.status_code


def _portrait(client: TestClient, story: str = JACK_KEY) -> dict[str, Any]:
    return client.post(
        "/api/cartoonify",
        files={"photo": ("me.png", make_photo_bytes(), "image/png")},
        data={"story": story, "photo_gender": "girl"},
    ).json()


def _wait_phase(client: TestClient, book_id: str, wanted: str, *, limit: float = 30) -> dict:
    deadline = time.monotonic() + limit
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = client.get(f"/api/book/{book_id}").json()
        if payload.get("phase") == wanted or payload["status"] in {"complete", "failed"}:
            return payload
        time.sleep(0.05)
    return payload


def _wait_done(client: TestClient, book_id: str, *, limit: float = 120) -> dict:
    deadline = time.monotonic() + limit
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = client.get(f"/api/book/{book_id}").json()
        if payload["status"] in {"complete", "failed"}:
            return payload
        time.sleep(0.05)
    return payload


# --- the stop buttons ---------------------------------------------------------------


def test_stopping_new_books_refuses_the_portrait_and_the_book(
    tmp_path: Path, seeded_template: Path
) -> None:
    """Nothing is read, drawn or charged while the switch is pulled."""
    with _site(tmp_path, seeded_template) as (client, provider):
        assert _pull(client, "new_books") == 303

        portrait = client.post(
            "/api/cartoonify",
            files={"photo": ("me.png", make_photo_bytes(), "image/png")},
            data={"story": JACK_KEY, "photo_gender": "girl"},
        )
        assert portrait.status_code == 503, portrait.text
        assert portrait.json()["error"] == "paused"
        assert "paused" in portrait.json()["detail"]
        assert portrait.headers.get("retry-after") == "60"
        assert provider.requests == []

        started = client.post(
            "/api/book/start", data={"story": JACK_KEY, "generation_id": "whatever"}
        )
        assert started.status_code == 503, started.text

        # Releasing it makes the studio work again, with no redeploy.
        assert _pull(client, "new_books", False) == 303
        assert _portrait(client)["generation_id"]


def test_stop_drawing_pauses_the_book_and_releasing_it_carries_on(
    tmp_path: Path, seeded_template: Path
) -> None:
    """The book is paused, not failed: its pictures are kept and it finishes."""
    with _site(tmp_path, seeded_template) as (client, provider):
        portrait = _portrait(client)
        drawn = len(provider.requests)
        assert _pull(client, "drawing") == 303

        started = client.post(
            "/api/book/start",
            data={"story": JACK_KEY, "generation_id": portrait["generation_id"]},
        )
        assert started.status_code == 201, started.text
        book_id = started.json()["book_id"]

        paused = _wait_phase(client, book_id, "paused")
        assert paused["status"] == "pending", paused
        assert paused["phase"] == "paused"
        assert "paused" in paused["error"].lower()
        # Not one page was drawn, so not one was paid for.
        assert len(provider.requests) == drawn

        assert _pull(client, "drawing", False) == 303
        final = _wait_done(client, book_id)

        assert final["status"] == "complete", final.get("error")
        assert len(provider.requests) > drawn


def test_only_the_operator_can_pull_a_switch(tmp_path: Path, seeded_template: Path) -> None:
    with _site(tmp_path, seeded_template) as (client, _provider):
        assert _pull(client, "new_books", sub="someone-else") == 404

        states = client.app.state.container.switches.all()  # type: ignore[attr-defined]
        assert states["new_books"].on is False


def test_an_about_edit_and_a_delete_still_work_while_new_books_are_stopped(
    tmp_path: Path, seeded_template: Path
) -> None:
    """The pause is about spending; words and removals spend nothing."""
    with _site(tmp_path, seeded_template) as (client, _provider):
        portrait = _portrait(client)
        started = client.post(
            "/api/book/start",
            data={"story": JACK_KEY, "generation_id": portrait["generation_id"], "child_name": "Mia"},
        )
        book_id = started.json()["book_id"]
        assert _wait_done(client, book_id)["status"] == "complete"

        assert _pull(client, "new_books") == 303
        about = client.post(
            f"/api/book/{book_id}/edit", data={"action": "about", "about_name": "Mia"}
        )
        assert about.status_code == 202, about.text
        redraw = client.post(
            f"/api/book/{book_id}/edit", data={"action": "cover"}
        )
        assert redraw.status_code == 503, redraw.text
        removed = client.post(f"/api/book/{book_id}/edit", data={"action": "delete"})
        assert removed.status_code == 202, removed.text
        assert removed.json()["deleted"] is True


# --- the alert mail ----------------------------------------------------------------


class _Mailer(Mailer):
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, *, to: str, subject: str, text: str) -> bool:
        self.sent.append((to, subject, text))
        return True


class _Users(UserStore):
    def __init__(self, people: dict[str, User] | None = None) -> None:
        self._people = people or {}

    def upsert(self, user: User) -> User:
        raise NotImplementedError

    def get(self, sub: str) -> User | None:
        return self._people.get(sub)


ALERTS = [Alert(code="refunds_waiting", message="1 refund waiting to be sent back.")]


def test_an_alert_is_mailed_once_and_again_only_when_it_changes() -> None:
    mailer = _Mailer()
    post = OperatorPost(
        mailer=mailer, users=_Users({"op-sub": User(sub="op-sub", email="op@example.com")}),
        operator="op-sub",
    )

    assert post.deliver(ALERTS) is True
    assert post.deliver(ALERTS) is False  # the same problem is not re-mailed
    assert len(mailer.sent) == 1
    to, subject, text = mailer.sent[0]
    assert to == "op@example.com"
    assert "needs a look" in subject.lower()
    assert ALERTS[0].message in text

    more = [*ALERTS, Alert(code="cost_per_book", message="A book costs more than it sells for.")]
    assert post.deliver(more) is True
    assert len(mailer.sent) == 2

    # Cleared, then back: worth saying again.
    assert post.deliver([]) is False
    assert post.deliver(ALERTS) is True
    assert len(mailer.sent) == 3


def test_a_listed_email_is_the_operator_address() -> None:
    post = OperatorPost(mailer=_Mailer(), users=_Users(), operator="a@example.com,op-sub")
    assert post.address() == "a@example.com"


def test_no_channel_or_no_address_sends_nothing() -> None:
    quiet = OperatorPost(mailer=None, users=_Users(), operator=OPERATOR)
    assert quiet.deliver(ALERTS) is False

    nowhere = OperatorPost(mailer=_Mailer(), users=_Users(), operator=OPERATOR)
    assert nowhere.address() == ""
    assert nowhere.deliver(ALERTS) is False


def test_the_mail_lists_every_alert() -> None:
    subject, text = alert_mail(ALERTS + [Alert("x", "second thing")], site_url="https://e.test")
    assert "2 things" in subject
    for alert in ALERTS:
        assert alert.message in text
    assert "https://e.test/admin/status" in text
