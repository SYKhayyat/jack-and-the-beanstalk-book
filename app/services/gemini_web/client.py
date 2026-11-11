"""Gemini in a real Chrome, driven over the DevTools protocol.

The free way to make a book: the same pictures, reviews and decisions the app
asks of paid APIs, asked instead of gemini.google.com in a Chrome profile kept
for this purpose and signed in to one Google account. Slower (one job at a
time, ~15 s a picture) and bounded by Gemini's daily limits, but free, which is
what a local test book needs.

How one job runs, every time the same way:

1. Make sure Chrome is up with its debugging port (start it if not), and that
   our one tab exists.
2. Open a fresh *temporary* chat: nothing from one job leaks into the next,
   and the account's chat history is never touched.
3. Check the signed-in account is the configured one.
4. Paste the images (a synthetic paste of real files, which is what the page's
   own uploader accepts), type the prompt, send.
5. Wait until Gemini has stopped writing, then read the answer: the text, or
   the full-size picture through the page's own "Download full size" button.

**Pause, never fail, on what a person can fix.** Signed out, the wrong
account, a daily limit reached, Chrome closed: the job waits — the log says
why, a desktop notification asks for help once — and checks again every
`poll_seconds` (a limit every `limit_wait_seconds`), then runs the same job
again. A book left overnight finishes the next day. What no waiting can fix (the
page changed shape, Gemini refused the request) raises `ProviderError`, so the
runner counts it as one failed attempt, as it would for an API.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from urllib.error import URLError

from app.services.gemini_web.attachment import Attachment
from app.services.gemini_web.cdp import CdpError, Connection, Session, browser_websocket_url
from app.services.image_providers.base import ProviderError

__all__ = ["Attachment", "GeminiWeb", "GeminiWebConfig", "PauseReason", "Paused"]

logger = logging.getLogger(__name__)

T = TypeVar("T")

APP_URL = "https://gemini.google.com/app"

_EDITOR = "rich-textarea .ql-editor"
_SEND = "button[aria-label*='Send']"
_STOP = "button[aria-label*='Stop']"
_TEMP_ON = "button[aria-label='Turn on temporary chat']"
_TEMP_OFF = "button[aria-label='Turn off temporary chat']"
_DOWNLOAD = "button[aria-label='Download full size image']"

#: Words Gemini uses when it will not do more today. Matched on an answer that
#: came back without the picture asked for (or on any answer, for text jobs
#: whose answer is this and nothing else).
_LIMIT_WORDS = re.compile(
    r"(reached|hit|used up|exceeded)\b.{0,40}\b(limit|quota)"
    r"|limit\b.{0,40}\b(reset|tomorrow|later)"
    r"|(can.t|cannot|unable to) (create|generate|make) (any )?more (images|pictures)"
    r"|come back (later|tomorrow)|try again (later|tomorrow)",
    re.IGNORECASE | re.DOTALL,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


class PauseReason:
    SIGNED_OUT = "signed out of Gemini"
    WRONG_ACCOUNT = "signed in to the wrong Google account"
    LIMIT = "Gemini's limit for now is reached"
    BROWSER = "Chrome is not reachable"


class Paused(Exception):
    """A condition a person (or the clock) fixes; the job waits, then runs again."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}{': ' + detail if detail else ''}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class GeminiWebConfig:
    #: The Google account that must be signed in; checked before every job.
    account: str
    debug_port: int = 9333
    profile_dir: Path = Path.home() / ".local/share/jack-and-the-beanstalk/gemini-chrome"
    chrome_binary: str = "google-chrome"
    #: How long one job may run once sent before it is a failure.
    job_timeout_seconds: float = 300.0
    #: How often a pause re-checks (signed out, wrong account, Chrome closed).
    poll_seconds: float = 30.0
    #: How long a reached limit waits before it is tried again.
    limit_wait_seconds: float = 1800.0

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.debug_port}"


class GeminiWeb:
    """One browser, one job at a time; safe to share between threads."""

    def __init__(
        self,
        config: GeminiWebConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        if not config.account.strip():
            raise ProviderError(
                "GEMINI_WEB_ACCOUNT is not set: name the Google account Gemini must use."
            )
        self.config = config
        self._sleep = sleep
        self._notify = notify or _desktop_notify
        self._lock = threading.Lock()
        self._connection: Connection | None = None
        self._session: Session | None = None
        self._downloads: dict[str, dict[str, Any]] = {}
        self._download_dir = Path(tempfile.mkdtemp(prefix="gemini-web-"))

    # --- the two jobs ---------------------------------------------------------

    def ask(self, prompt: str, images: Sequence[Attachment] = ()) -> str:
        """Gemini's text answer to `prompt` about `images`."""
        return self._job(lambda session: self._ask(session, prompt, images), label="ask")

    def draw(self, prompt: str, images: Sequence[Attachment] = ()) -> bytes:
        """The full-size picture Gemini draws for `prompt` from `images` (JPEG)."""
        return self._job(lambda session: self._draw(session, prompt, images), label="draw")

    # --- pause and resume -----------------------------------------------------

    def _job(self, run: Callable[[Session], T], *, label: str) -> T:
        with self._lock:
            paused: str | None = None
            while True:
                try:
                    session = self._ready_session()
                    self._open_chat(session)
                    result = run(session)
                except Paused as pause:
                    if paused != pause.reason:
                        logger.warning("gemini_web.paused job=%s reason=%s", label, pause)
                        self._notify(f"Book paused: {pause}. It resumes by itself.")
                        paused = pause.reason
                    self._sleep(
                        self.config.limit_wait_seconds
                        if pause.reason == PauseReason.LIMIT
                        else self.config.poll_seconds
                    )
                    continue
                if paused is not None:
                    logger.warning("gemini_web.resumed job=%s after=%s", label, paused)
                return result

    def _ready_session(self) -> Session:
        """Our tab, on a live connection; starts Chrome when it is not running."""
        if self._connection is not None and not self._connection.closed and self._session:
            return self._session
        self._session = None
        try:
            websocket = browser_websocket_url(self.config.http_url)
        except (URLError, OSError):
            self._launch_chrome()
            try:
                websocket = browser_websocket_url(self.config.http_url)
            except (URLError, OSError) as exc:
                raise Paused(PauseReason.BROWSER, str(exc)) from None
        try:
            connection = Connection(websocket)
            connection.subscribe(self._on_event)
            connection.send(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allowAndName",
                    "downloadPath": str(self._download_dir),
                    "eventsEnabled": True,
                },
            )
            session = Session(connection, self._our_tab(connection))
        except (CdpError, OSError) as exc:
            raise Paused(PauseReason.BROWSER, str(exc)) from None
        self._connection, self._session = connection, session
        return session

    @staticmethod
    def _our_tab(connection: Connection) -> str:
        """The Gemini tab already open, or a new one: never a tab per reconnect."""
        targets = connection.send("Target.getTargets")["targetInfos"]
        for target in targets:
            if target.get("type") == "page" and "gemini.google" in str(target.get("url")):
                return str(target["targetId"])
        return str(connection.send("Target.createTarget", {"url": "about:blank"})["targetId"])

    def _launch_chrome(self) -> None:
        binary = shutil.which(self.config.chrome_binary) or self.config.chrome_binary
        self.config.profile_dir.mkdir(parents=True, exist_ok=True)
        logger.info("gemini_web.launch_chrome port=%d", self.config.debug_port)
        try:
            subprocess.Popen(
                [
                    binary,
                    f"--remote-debugging-port={self.config.debug_port}",
                    f"--user-data-dir={self.config.profile_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    APP_URL,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise Paused(PauseReason.BROWSER, f"could not start {binary}: {exc}") from None
        for _ in range(30):
            self._sleep(1.0)
            try:
                browser_websocket_url(self.config.http_url, timeout=1.0)
                return
            except (URLError, OSError):
                continue

    def _on_event(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        if method in ("Browser.downloadWillBegin", "Browser.downloadProgress"):
            params = dict(message.get("params") or {})
            self._downloads.setdefault(str(params.get("guid")), {}).update(params)

    # --- one chat ---------------------------------------------------------------

    def _open_chat(self, session: Session) -> None:
        """A fresh temporary chat, on the right account; else a pause."""
        try:
            session.send("Page.navigate", {"url": APP_URL})
            state = session.wait_for(
                f"""(() => {{
                  if (!location.host.startsWith("gemini.google")) return "away";
                  if (document.querySelector("{_EDITOR}")) return "ready";
                  const signIn = [...document.querySelectorAll("a, button")]
                    .some(e => /^sign in$/i.test((e.innerText || "").trim()));
                  return signIn ? "signed_out" : "";
                }})()""",
                timeout=45,
            )
        except CdpError as exc:
            self._drop_connection()
            raise Paused(PauseReason.BROWSER, str(exc)) from None
        if state in ("signed_out", "away"):
            self._show_tab(session)
            raise Paused(PauseReason.SIGNED_OUT, f"sign in as {self.config.account}")
        account = self._signed_in_account(session)
        if account is None:
            self._show_tab(session)
            raise Paused(PauseReason.SIGNED_OUT, f"sign in as {self.config.account}")
        if account.lower() != self.config.account.strip().lower():
            self._show_tab(session)
            raise Paused(
                PauseReason.WRONG_ACCOUNT, f"switch to {self.config.account} in the Gemini tab"
            )
        try:
            # The button is drawn before it is wired up: a click that early is
            # lost, so click until it reads "turn off".
            session.wait_for(
                f"""(() => {{
                  if (document.querySelector("{_TEMP_OFF}")) return true;
                  document.querySelector("{_TEMP_ON}")?.click();
                  return false;
                }})()""",
                timeout=30,
                interval=1.0,
            )
        except CdpError as exc:
            raise ProviderError(f"Gemini's temporary chat did not open: {exc}") from None

    def _signed_in_account(self, session: Session) -> str | None:
        label = session.evaluate(
            """[...document.querySelectorAll("[aria-label^='Google Account']")]
                 .map(e => e.getAttribute("aria-label")).join(" ")"""
        )
        match = _EMAIL.search(str(label or ""))
        return match.group(0) if match else None

    def _show_tab(self, session: Session) -> None:
        with contextlib.suppress(CdpError):
            session.send("Page.bringToFront")

    def _drop_connection(self) -> None:
        if self._connection is not None:
            with contextlib.suppress(Exception):
                self._connection.close()
        self._connection = self._session = None

    def _send(self, session: Session, prompt: str, images: Sequence[Attachment]) -> int:
        """Attach, type, send; returns how many answers the chat had before."""
        try:
            before = int(
                session.evaluate('document.querySelectorAll("model-response").length')
            )
            if images:
                files = json.dumps(
                    [
                        [base64.b64encode(i.data).decode(), i.media_type, i.filename]
                        for i in images
                    ]
                )
                session.evaluate(
                    f"""(() => {{
                      const dt = new DataTransfer();
                      for (const [b64, type, name] of {files}) {{
                        const bin = atob(b64), bytes = new Uint8Array(bin.length);
                        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                        dt.items.add(new File([bytes], name, {{type}}));
                      }}
                      const editor = document.querySelector("{_EDITOR}");
                      editor.focus();
                      editor.dispatchEvent(new ClipboardEvent("paste",
                        {{clipboardData: dt, bubbles: true, cancelable: true}}));
                    }})()""",
                    gesture=True,
                )
                session.wait_for(
                    "document.querySelectorAll('uploader-file-preview').length"
                    f" >= {len(images)}",
                    timeout=60,
                )
            session.evaluate(f'document.querySelector("{_EDITOR}").focus()')
            session.send("Input.insertText", {"text": prompt})
            # The send button is disabled until every upload has finished.
            session.wait_for(
                f"""(() => {{ const b = document.querySelector("{_SEND}");
                     return !!b && !b.disabled
                       && b.getAttribute("aria-disabled") !== "true"; }})()""",
                timeout=120,
            )
            session.evaluate(f'document.querySelector("{_SEND}").click()', gesture=True)
        except CdpError as exc:
            raise ProviderError(f"Gemini did not take the request: {exc}") from None
        return before

    def _finished_answer(self, session: Session, before: int) -> Mapping[str, Any]:
        """Wait for the new answer to finish; its text and whether a picture is in it."""
        try:
            return dict(
                session.wait_for(
                    f"""(() => {{
                      const all = document.querySelectorAll("model-response");
                      if (all.length <= {before}) return null;
                      if (document.querySelector("{_STOP}")) return null;
                      const last = all[all.length - 1];
                      const text = (last.querySelector("message-content")
                        || last).innerText.trim();
                      const picture = [...last.querySelectorAll("img")]
                        .some(i => i.complete && i.naturalWidth >= 512);
                      const download = !!last.querySelector("{_DOWNLOAD}");
                      if (!text && !picture) return null;
                      return {{text, picture, download}};
                    }})()""",
                    timeout=self.config.job_timeout_seconds,
                    interval=1.0,
                )
            )
        except CdpError as exc:
            raise ProviderError(f"Gemini did not answer: {exc}") from None

    def _ask(self, session: Session, prompt: str, images: Sequence[Attachment]) -> str:
        before = self._send(session, prompt, images)
        # Text streams; once the Stop button is gone the answer is whole, but
        # give the last chunk a moment to render.
        self._finished_answer(session, before)
        self._sleep(1.0)
        answer = self._finished_answer(session, before)
        text = str(answer["text"])
        if _LIMIT_WORDS.search(text) and "{" not in text:
            raise Paused(PauseReason.LIMIT, text[:200])
        return text

    def _draw(self, session: Session, prompt: str, images: Sequence[Attachment]) -> bytes:
        before = self._send(session, prompt, images)
        answer = self._finished_answer(session, before)
        if not answer.get("picture"):
            text = str(answer.get("text", ""))
            if _LIMIT_WORDS.search(text):
                raise Paused(PauseReason.LIMIT, text[:200])
            raise ProviderError(f"Gemini answered without a picture: {text[:300]}")
        try:
            session.wait_for(
                f'!!([...document.querySelectorAll("model-response")].pop()'
                f'.querySelector("{_DOWNLOAD}"))',
                timeout=30,
            )
            known = set(self._downloads)
            session.evaluate(
                f'[...document.querySelectorAll("model-response")].pop()'
                f'.querySelector("{_DOWNLOAD}").click()',
                gesture=True,
            )
        except CdpError as exc:
            raise ProviderError(f"Gemini's picture could not be downloaded: {exc}") from None
        return self._downloaded(known)

    def _downloaded(self, known: set[str]) -> bytes:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            for guid, state in list(self._downloads.items()):
                if guid in known:
                    continue
                if state.get("state") == "canceled":
                    self._downloads.pop(guid, None)
                    raise ProviderError("Gemini's picture download was cancelled")
                if state.get("state") == "completed":
                    self._downloads.pop(guid, None)
                    path = self._download_dir / guid
                    data = path.read_bytes()
                    path.unlink(missing_ok=True)
                    return data
            time.sleep(0.5)
        raise ProviderError("Gemini's picture did not download in 120s")


def _desktop_notify(message: str) -> None:
    """A desktop notification when one can be shown; the log has it either way."""
    notifier = shutil.which("notify-send")
    if notifier is None:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [notifier, "--app-name=Jack and the Beanstalk", "Gemini needs you", message],
            check=False,
            timeout=5,
        )
