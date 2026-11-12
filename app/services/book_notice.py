"""The mail a parent gets when their book is finished (W2).

A book is minutes of drawing. Without this, a parent who closed the tab finds
out by coming back and hoping; the book is done, the fee is taken and the PDF is
written while nobody is looking. One transactional message fixes that: their
own book, their own address, a link, and the day the book goes.

Two rules this keeps:

* **It is about their book, and only their book.** No marketing, no list, no
  address kept anywhere new — the recipient is the signed-in person's own
  Google address, looked up at the moment the book is done, and never written
  to a log line.
* **A mail that cannot be sent is not a failure of the book.** The book is
  finished and downloadable either way, so every path here returns a bool and
  a warning line rather than raising into the render job.

An edit that redraws a page of a finished book does not re-announce it: the
message is "your book is ready", and it was ready before the redraw.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.services.book_store import BookRecord
from app.services.mailer import Mailer
from app.services.user_store import UserStore

__all__ = ["BookReadyNotice", "ready_mail"]

logger = logging.getLogger(__name__)


def ready_mail(record: BookRecord, *, link: str, days: int, name: str = "") -> tuple[str, str]:
    """The subject and body of "your book is ready", as plain text.

    Plain text on purpose: a product message is a sentence and a link, and a
    text body cannot render as a page that looks like this app's login.
    """
    title = record.title
    subject = f"Your book is ready: {title}"
    greeting = f"Hello {name}," if name else "Hello,"
    lines = [
        greeting,
        "",
        f"Your picture book “{title}” is finished. Every picture is drawn, and you "
        "can read it on screen or download it as a PDF.",
    ]
    if link:
        lines += ["", f"Read it here: {link}"]
    when = _goes_on(record.created_at, days)
    if when:
        lines += [
            "",
            f"It stays on the shelf until {when}. After that its pictures and PDF "
            "are deleted, so download what you want to keep.",
        ]
    lines += ["", "— Storybook Studio"]
    return subject, "\n".join(lines)


def _goes_on(created_at: str, days: int) -> str:
    """The day this book is swept, as "12 November", or "" if unknowable."""
    try:
        started = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return ""
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (started + timedelta(days=days)).strftime("%-d %B")


class BookReadyNotice:
    """Tells the person who made a book that it is finished."""

    def __init__(
        self,
        *,
        mailer: Mailer,
        users: UserStore,
        site_url: str = "",
        retention_days: int = 7,
    ) -> None:
        self._mailer = mailer
        self._users = users
        # No site URL means no link line: an email that says "come and find it"
        # is worse than one that says the book is ready.
        self._site_url = site_url.rstrip("/")
        self._days = retention_days

    async def send(self, *, record: BookRecord, user: str) -> bool:
        """Mail the book's owner. Never raises, and never logs the address."""
        if not user:
            return False
        person = await asyncio.to_thread(self._users.get, user)
        address = (person.email if person else "").strip()
        if not address:
            logger.info("book.ready_mail_skipped book=%s reason=no_address", record.book_id)
            return False
        subject, text = ready_mail(
            record,
            link=f"{self._site_url}/books/{record.book_id}" if self._site_url else "",
            days=self._days,
            name=(person.name.split()[0] if person and person.name else ""),
        )
        try:
            sent = await asyncio.to_thread(
                self._mailer.send, to=address, subject=subject, text=text
            )
        except Exception:  # a mail outage must never fail a finished book
            logger.warning("book.ready_mail_failed book=%s", record.book_id, exc_info=True)
            return False
        logger.info(
            "book.ready_mail user=%s book=%s sent=%s provider=%s",
            user,
            record.book_id,
            sent,
            self._mailer.name,
        )
        return sent
