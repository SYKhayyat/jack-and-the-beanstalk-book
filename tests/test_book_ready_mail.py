"""The one mail this app sends: "your book is ready" (W2).

A book takes minutes. Without a message, a parent who closed the tab learns
nothing until they come back — the fee is taken and the PDF is written while
nobody is looking. What these tests hold the feature to:

* the message names the book, the link and the day it goes;
* the address is the signed-in person's own, looked up at send time;
* a deployment with no key, or a provider that rejects the send, mails nothing
  and fails nothing — the book is finished either way;
* one mail a book: finishing it, not redrawing it.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.models.book import AboutMe
from app.services.book_notice import BookReadyNotice, ready_mail
from app.services.book_store import BookRecord
from app.services.mailer import ResendMailer
from app.services.user_store import User
from tests.test_book_budgets import OPERATOR, _book, _site

# --- the message ------------------------------------------------------------------


def _record(**overrides: Any) -> BookRecord:
    fields: dict[str, Any] = {
        "book_id": "book-1",
        # The message reads only the title and the day; the About page's own
        # text never enters a mail.
        "about": AboutMe(prompt_name="Shai", about_name="Shai"),
        "generation_id": "gen",
        "created_at": "2026-11-12T10:00:00+00:00",
        "story_key": "jack_and_the_beanstalk",
        "title": "Shai and the Beanstalk",
        "user_id": "sub-1",
    }
    fields.update(overrides)
    return BookRecord(**fields)


def test_the_mail_names_the_book_the_link_and_the_day_it_goes() -> None:
    subject, text = ready_mail(
        _record(), link="https://books.test/books/book-1", days=7, name="Shai"
    )

    assert subject == "Your book is ready: Shai and the Beanstalk"
    assert "Hello Shai," in text
    assert "Shai and the Beanstalk" in text
    assert "https://books.test/books/book-1" in text
    assert "19 November" in text, "seven days after the book started"
    assert "Storybook Studio" in text


def test_without_a_site_url_there_is_no_link_line() -> None:
    _subject, text = ready_mail(_record(), link="", days=7)
    assert "Read it here" not in text
    assert "Hello," in text, "an unknown name is not a guess"


def test_an_unreadable_created_at_does_not_invent_a_date() -> None:
    _subject, text = ready_mail(_record(created_at=""), link="", days=7)
    assert "stays on the shelf until" not in text


# --- the provider -----------------------------------------------------------------


def _resend(handler: Any) -> tuple[ResendMailer, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    mailer = ResendMailer(
        "re_test_key\n",
        "Storybook Studio <books@example.com>",
        transport=httpx.MockTransport(record),
    )
    return mailer, seen


def test_resend_gets_one_post_with_the_message() -> None:
    mailer, seen = _resend(lambda _request: httpx.Response(200, json={"id": "mail_1"}))

    assert mailer.send(to="parent@example.com", subject="Ready", text="Hello") is True

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url == "https://api.resend.com/emails"
    # A key pasted into a dashboard has a newline; httpx would reject it.
    assert request.headers["authorization"] == "Bearer re_test_key"
    body = request.read().decode()
    assert '"to":["parent@example.com"]' in body
    assert '"from":"Storybook Studio <books@example.com>"' in body


def test_a_rejected_send_is_a_warning_not_an_exception() -> None:
    """An unverified sender is the operator's problem, not the book's."""
    mailer, _seen = _resend(
        lambda _request: httpx.Response(422, json={"name": "validation_error"})
    )
    assert mailer.send(to="parent@example.com", subject="Ready", text="Hello") is False


def test_a_transport_failure_is_a_warning_not_an_exception() -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    mailer, _seen = _resend(boom)
    assert mailer.send(to="parent@example.com", subject="Ready", text="Hello") is False


# --- the notice -------------------------------------------------------------------


class FakeUsers:
    """A user store with one person in it, or nobody."""

    def __init__(self, email: str) -> None:
        self.email = email

    def get(self, sub: str) -> User | None:
        return User(sub=sub, email=self.email, name="Shai Cohen") if self.email else None


class FakeMailer:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, *, to: str, subject: str, text: str) -> bool:
        self.sent.append((to, subject, text))
        return True


def _notice(users: FakeUsers, mailer: FakeMailer) -> BookReadyNotice:
    return BookReadyNotice(
        mailer=mailer, users=users, site_url="https://books.test/", retention_days=7
    )


def test_the_mail_goes_to_the_books_owner() -> None:
    mailer = FakeMailer()
    sent = asyncio.run(
        _notice(FakeUsers("parent@example.com"), mailer).send(record=_record(), user="sub-1")
    )

    assert sent is True
    assert len(mailer.sent) == 1
    to, subject, text = mailer.sent[0]
    assert to == "parent@example.com"
    assert "Shai and the Beanstalk" in subject
    assert "https://books.test/books/book-1" in text


def test_somebody_with_no_address_is_not_mailed() -> None:
    mailer = FakeMailer()
    sent = asyncio.run(_notice(FakeUsers(""), mailer).send(record=_record(), user="sub-1"))

    assert sent is False
    assert mailer.sent == []


# --- the whole book ---------------------------------------------------------------


def _settle(client: TestClient, book_id: str, seconds: float = 120.0) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = dict(client.get(f"/api/book/{book_id}").json())
        if last["status"] in {"complete", "failed"}:
            return last
        time.sleep(0.05)
    return last


def test_a_finished_book_mails_its_owner_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, schema_template: Path
) -> None:
    sent: list[tuple[str, str, str]] = []

    def capture(_self: ResendMailer, *, to: str, subject: str, text: str) -> bool:
        sent.append((to, subject, text))
        return True

    monkeypatch.setattr(ResendMailer, "send", capture)
    with _site(
        tmp_path,
        schema_template,
        resend_api_key="re_test",
        mail_from="books@example.com",
        public_site_url="https://books.test",
    ) as client:
        book_id = _book(client)
        assert _settle(client, book_id)["status"] == "complete"

        # An edit of a finished book redraws nothing here and must not
        # announce the book a second time.
        edited = client.post(f"/api/book/{book_id}/edit", data={"action": "about"})
        assert edited.status_code == 202, edited.text
        assert _settle(client, book_id)["status"] == "complete"

    assert len(sent) == 1, sent
    to, subject, text = sent[0]
    assert to == f"{OPERATOR}@example.com"
    assert "Your book is ready" in subject
    assert f"https://books.test/books/{book_id}" in text


def test_a_deployment_with_no_key_mails_nothing(tmp_path: Path, schema_template: Path) -> None:
    with _site(tmp_path, schema_template) as client:
        runner = client.app.state.container.book_runner  # type: ignore[attr-defined]
        assert runner._notice is None
