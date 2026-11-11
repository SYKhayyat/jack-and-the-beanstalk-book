"""A small, synchronous Chrome DevTools Protocol client.

Just enough of CDP to drive one tab: send a command and wait for its answer,
and wait for an event. It speaks to the browser-level websocket and attaches to
tabs with flattened sessions, so one connection serves every tab.

Deliberately not Playwright: Playwright's sync API needs a native greenlet
build (and its Node driver), which is one more thing to break on a dev machine;
`websockets` is already a runtime dependency of the app.
"""

from __future__ import annotations

import itertools
import json
import logging
import queue
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.request import urlopen

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

__all__ = ["CdpError", "Connection", "Session", "browser_websocket_url"]

logger = logging.getLogger(__name__)

#: A screenshot or a downloaded picture can be several megabytes of base64.
_MAX_MESSAGE_BYTES = 256 * 1024 * 1024


class CdpError(RuntimeError):
    """The browser answered a command with an error, or the connection died."""


def browser_websocket_url(http_url: str, timeout: float = 5.0) -> str:
    """The browser-level websocket of a Chrome started with a debugging port."""
    with urlopen(f"{http_url.rstrip('/')}/json/version", timeout=timeout) as response:
        return str(json.loads(response.read())["webSocketDebuggerUrl"])


class Connection:
    """One websocket to the browser, shared by every tab session."""

    def __init__(self, websocket_url: str) -> None:
        self._ws: ClientConnection = connect(
            websocket_url, max_size=_MAX_MESSAGE_BYTES, open_timeout=10
        )
        self._ids = itertools.count(1)
        self._pending: dict[int, queue.Queue[Mapping[str, Any]]] = {}
        self._listeners: list[Callable[[Mapping[str, Any]], None]] = []
        self._lock = threading.Lock()
        self._closed = False
        self._reader = threading.Thread(target=self._read, name="cdp-reader", daemon=True)
        self._reader.start()

    @property
    def closed(self) -> bool:
        return self._closed

    def _read(self) -> None:
        try:
            for raw in self._ws:
                message = json.loads(raw)
                if "id" in message:
                    with self._lock:
                        waiter = self._pending.pop(int(message["id"]), None)
                    if waiter is not None:
                        waiter.put(message)
                    continue
                with self._lock:
                    listeners = list(self._listeners)
                for listener in listeners:
                    listener(message)
        except ConnectionClosed:
            pass
        finally:
            self._closed = True
            with self._lock:
                waiters = list(self._pending.values())
                self._pending.clear()
            for waiter in waiters:
                waiter.put({"error": {"message": "the browser connection closed"}})

    def send(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        if self._closed:
            raise CdpError("the browser connection is closed")
        message_id = next(self._ids)
        waiter: queue.Queue[Mapping[str, Any]] = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[message_id] = waiter
        payload: dict[str, Any] = {
            "id": message_id,
            "method": method,
            "params": dict(params or {}),
        }
        if session_id is not None:
            payload["sessionId"] = session_id
        self._ws.send(json.dumps(payload))
        try:
            answer = waiter.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(message_id, None)
            raise CdpError(f"{method} got no answer in {timeout:.0f}s") from None
        if "error" in answer:
            raise CdpError(f"{method}: {answer['error'].get('message', answer['error'])}")
        return dict(answer.get("result") or {})

    def subscribe(self, listener: Callable[[Mapping[str, Any]], None]) -> Callable[[], None]:
        """Call `listener` with every event; returns the unsubscribe function."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def close(self) -> None:
        self._ws.close()


class Session:
    """One tab, attached over the shared connection."""

    def __init__(self, connection: Connection, target_id: str) -> None:
        self.connection = connection
        self.target_id = target_id
        attached = connection.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        self.session_id = str(attached["sessionId"])

    def send(
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float = 60.0
    ) -> dict[str, Any]:
        return self.connection.send(
            method, params, session_id=self.session_id, timeout=timeout
        )

    def evaluate(
        self, expression: str, *, timeout: float = 60.0, gesture: bool = False
    ) -> Any:
        """Run JavaScript in the page and return its (JSON-able) value.

        A promise is awaited. `gesture` makes the call count as a user
        activation, which some page actions (clipboard, file pickers) need.
        """
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": gesture,
            },
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text")
            raise CdpError(f"page script failed: {text}")
        return result.get("result", {}).get("value")

    def wait_for(
        self,
        expression: str,
        *,
        timeout: float,
        interval: float = 0.5,
    ) -> Any:
        """Poll `expression` until it is truthy; return its value or raise CdpError."""
        deadline = time.monotonic() + timeout
        while True:
            value = self.evaluate(expression)
            if value:
                return value
            if time.monotonic() >= deadline:
                raise CdpError(f"timed out after {timeout:.0f}s waiting for the page")
            time.sleep(interval)
