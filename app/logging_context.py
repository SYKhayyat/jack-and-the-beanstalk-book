"""Correlate log lines with the request that caused them.

A book is rendered in a background task, so its log lines are not adjacent to
the HTTP request that started it. A request id carried in a `ContextVar` travels
into that task (``asyncio.create_task`` copies the current context) and is
appended to every record by a logging filter, which gives the operator one
greppable thread from "book started" through each page to "book complete".

The id is generated here, never taken from an inbound header: a visitor cannot
choose what appears in the operator's log. It is short because it is meant to be
read and typed, not parsed.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from uuid import uuid4

__all__ = [
    "RequestIdFilter",
    "current_request_id",
    "install_request_id_logging",
    "new_request_id",
    "reset_request_id",
    "set_request_id",
]

_REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="")


def new_request_id() -> str:
    """A fresh, short id for one request (12 hex characters)."""
    return uuid4().hex[:12]


def current_request_id() -> str:
    """The id of the request being served, or an empty string outside one."""
    return _REQUEST_ID.get()


def set_request_id(request_id: str) -> Token[str]:
    """Make `request_id` current on this context; returns the reset token."""
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: Token[str]) -> None:
    """Restore the previous request id (end of the request)."""
    _REQUEST_ID.reset(token)


class RequestIdFilter(logging.Filter):
    """Append `request=<id>` to every record logged while a request is active.

    The filter is shared by the root logger and its handlers, so the same
    record can pass through it twice. It checks before appending, which keeps
    one id per line. Startup lines have no request and are left untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        request_id = current_request_id()
        if not request_id:
            return True
        message = record.getMessage()
        if f"request={request_id}" in message:
            return True
        record.msg = f"{message} request={request_id}"
        record.args = ()
        return True


# One filter instance, shared by the root logger and every handler, so repeated
# startup calls (tests build many apps) never stack duplicate filters.
_installed: RequestIdFilter | None = None


def install_request_id_logging() -> None:
    """Attach the shared request-id filter to the root logger and handlers."""
    global _installed
    if _installed is None:
        _installed = RequestIdFilter()
    root = logging.getLogger()
    if _installed not in root.filters:
        root.addFilter(_installed)
    for handler in root.handlers:
        if _installed not in handler.filters:
            handler.addFilter(_installed)
