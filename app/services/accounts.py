"""Deleting an account: everything the person made, gone at their request (A8).

What goes, in one transaction:

* every book they own, with its pages, pictures, uploaded photograph and PDF;
* every print order's shipping address and print files, and every approval
  never placed;
* the child they asked the studio to remember;
* the log of portraits drawn for them;
* their name, email and avatar.

What stays, and why:

* **Placed print orders, without their address or files.** What was ordered,
  when, for how much and the printer's order number are the financial record
  of a sale, kept like the ledger (A9).
* **The wallet ledger.** It is a financial record (what was paid, what was
  spent, what was refunded) and is filed under Google's opaque account id,
  not a name or an email. The balance is not paid out: the page says so before
  the button, and a refund request is how money comes back.
* **The `users` row itself, emptied.** A browser on another device may still
  hold a valid session until it expires; a book it starts must still have an
  owner to point at. The row keeps only the opaque id, so signing in again is
  a fresh, empty account.

The pictures themselves live in the object store under content-addressed keys
that several rows may share, so deleting rows does not delete them: the hourly
sweep (`app/blob_sweep.py`) removes every object no row points at any more,
within about two hours.

Two things block a deletion, because tearing the rows out from under them
would leave work half-done: a book still being drawn, and a print order being
placed with the printer right now or whose outcome is still unknown (A4). The
first two finish within minutes; an unknown order is resolved from the print
page.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select, update
from sqlalchemy.engine import Engine

from app.db import BookRow, ChildProfileRow, PortraitAttemptRow, PrintOrderRow, UserRow
from app.services.book_store import BookStatus
from app.services.db_book_store import delete_book_rows

__all__ = ["AccountBusy", "delete_account"]

logger = logging.getLogger(__name__)

#: Book states with a render job working on them.
_BUSY_BOOKS = (BookStatus.PENDING, BookStatus.RENDERING)


class AccountBusy(Exception):
    """Something of this account's is mid-flight; try again in a few minutes."""


def delete_account(engine: Engine, user_id: str) -> int:
    """Delete everything `user_id` made; return how many books went.

    Raises `AccountBusy` (and changes nothing) while a book is being drawn or a
    print order is being placed. The check and the deletes share one
    transaction. A book started in the same instant from another device is
    simply a new book under the emptied account.
    """
    if not user_id:
        raise ValueError("an account deletion needs the account's id")
    with engine.begin() as conn:
        books = conn.execute(
            select(BookRow.id, BookRow.status).where(BookRow.user_id == user_id)
        ).all()
        if any(status in _BUSY_BOOKS for _, status in books):
            raise AccountBusy(
                "A book is still being drawn. Wait for it to finish, then delete the account."
            )
        book_ids = [book_id for book_id, _ in books]
        placing = conn.execute(
            select(PrintOrderRow.id).where(
                PrintOrderRow.user_id == user_id,
                PrintOrderRow.state.in_(("placing", "unknown")),
            )
        ).first()
        if placing is not None:
            raise AccountBusy(
                "A print order is still being sent to the printer, or the printer has "
                "not confirmed it yet. Open the book's print page to settle it, then "
                "delete the account."
            )
        conn.execute(
            delete(PrintOrderRow).where(
                PrintOrderRow.user_id == user_id,
                PrintOrderRow.state.in_(("approved", "superseded")),
            )
        )
        conn.execute(
            update(PrintOrderRow)
            .where(PrintOrderRow.user_id == user_id)
            .values(address=None, interior=None, cover=None)
        )
        for book_id in book_ids:
            delete_book_rows(conn, book_id)
        conn.execute(delete(ChildProfileRow).where(ChildProfileRow.user_id == user_id))
        conn.execute(delete(PortraitAttemptRow).where(PortraitAttemptRow.user_id == user_id))
        conn.execute(
            update(UserRow).where(UserRow.sub == user_id).values(email="", name="", avatar="")
        )
    logger.info("account.deleted user=%s books=%d", user_id, len(book_ids))
    return len(book_ids)
