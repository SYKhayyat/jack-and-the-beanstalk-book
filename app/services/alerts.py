"""What the operator should be told without having to open the page (W7).

`/admin/status` already carries the numbers — review pass rate, what a book
costs to make, refunds waiting, books over the cost alert — but only for
somebody who happens to look. These are the few thresholds worth saying out
loud: they are logged at boot and on the hourly sweep, so the platform's log
keeps them, and `/admin/status` shows the same list at the top of the page.

The rules are deliberately blunt and few: an alerting system that cries about
noise gets ignored, which is why each rule fires only on a sample big enough to
mean something.

Where an alert goes (Idea 10): the log, the operator page, and — when the
deployment has a mail channel and the operator's own address is known — one
message to the operator, sent only when *what is wrong has changed*. A steady
problem is not re-mailed every hour, and an alert that clears is forgotten so
its next appearance speaks again. A boot only logs: a restart loop must not
become a mail loop.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.engine import Engine

from app.services.costs import CostLedger, format_microusd
from app.services.mailer import Mailer
from app.services.operator_status import review_health
from app.services.user_store import UserStore
from app.services.wallet import REFUND_REQUESTED, format_cents

logger = logging.getLogger(__name__)

__all__ = [
    "REVIEW_FLOOR",
    "REVIEW_SAMPLE",
    "Alert",
    "OperatorPost",
    "alert_mail",
    "cost_alert",
    "log_alerts",
    "operator_alerts",
    "review_alert",
]

#: Below this share of pictures passing their first check, with a sample this
#: size, the review loop is either failing the painter or failing itself.
REVIEW_FLOOR = 0.5

#: How many reviewed pictures the pass-rate rule needs before it will speak.
#: One book is 21; two books is a pattern.
REVIEW_SAMPLE = 40


@dataclass(frozen=True, slots=True)
class Alert:
    """One thing the operator should know, in the words they should read."""

    code: str
    message: str


def review_alert(*, pictures: int, passed_first_try: int) -> Alert | None:
    """The review loop failing its own promise, or nothing.

    A book whose pictures are mostly redrawn is the shape of both a bad painter
    and a broken reviewer; either way it costs the operator money, so the
    operator hears about it. A small sample says nothing: the first shaky book
    on a new deployment is not news.
    """
    if pictures < REVIEW_SAMPLE:
        return None
    share = passed_first_try / pictures
    if share >= REVIEW_FLOOR:
        return None
    return Alert(
        code="review_pass_rate",
        message=(
            f"Only {passed_first_try} of {pictures} pictures passed their first check "
            f"({share:.0%}, floor {REVIEW_FLOOR:.0%}). The painter and the reviewer "
            "disagree; both spend money."
        ),
    )


def cost_alert(*, average_microusd: int, price_cents: int, books: int) -> Alert | None:
    """A book costing more than it sells for, or nothing.

    The same number the cost panel shows, said once rather than discovered:
    the price is the threshold, because that is the one that matters.
    """
    if books <= 0 or price_cents <= 0:
        return None
    if average_microusd <= price_cents * 10_000:
        return None
    return Alert(
        code="cost_per_book",
        message=(
            f"A book costs {format_microusd(average_microusd)} to make and sells for "
            f"{format_cents(price_cents)} (over {books} book{'s' if books != 1 else ''})."
        ),
    )


def operator_alerts(engine: Engine, *, price_cents: int, wallet: object) -> list[Alert]:
    """Every alert this deployment trips right now, most urgent first.

    Read-only and cheap enough to run on every `/admin/status` load: the health
    query reads verdict columns, the ledger one grouped sum, the wallet one
    small table. `wallet` is the Wallet, passed rather than built so the page
    and the sweep share the deployment's own object.
    """
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=7)
    health = review_health(engine, since=since)
    summary = CostLedger(engine).summary()
    alerts = [
        alert
        for alert in (
            review_alert(pictures=health.reviewed, passed_first_try=health.passed_first_try),
            cost_alert(
                average_microusd=summary.average_book_microusd,
                price_cents=price_cents,
                books=summary.books_measured,
            ),
        )
        if alert is not None
    ]
    waiting = getattr(wallet, "all_refund_requests", None)
    if callable(waiting):
        try:
            pending = [item for item in waiting(limit=100) if item.status == REFUND_REQUESTED]
        except Exception:
            pending = []
        if pending:
            alerts.append(
                Alert(
                    code="refunds_waiting",
                    message=(
                        f"{len(pending)} refund{'s' if len(pending) != 1 else ''} "
                        "waiting to be sent back."
                    ),
                )
            )
    return alerts


def log_alerts(alerts: Iterable[Alert]) -> None:
    """Say each alert once, at the one level this deployment can keep (W7)."""
    for alert in alerts:
        logger.warning("operator.alert code=%s %s", alert.code, alert.message)


def alert_mail(alerts: list[Alert], *, site_url: str = "") -> tuple[str, str]:
    """The subject and body of "something on the site needs a look".

    Plain text, like every message this app sends: a sentence per alert, and
    the page that has the numbers — not a dashboard rendered in mail.
    """
    count = len(alerts)
    subject = (
        "Storybook Studio: 1 thing needs a look"
        if count == 1
        else f"Storybook Studio: {count} things need a look"
    )
    lines = [
        "The operator's page is saying:",
        "",
        *[f"· {alert.message}" for alert in alerts],
    ]
    if site_url:
        lines += ["", f"The numbers behind this: {site_url.rstrip('/')}/admin/status"]
    lines += ["", "— Storybook Studio"]
    return subject, "\n".join(lines)


class OperatorPost:
    """Mails the operator when what is wrong changes (Idea 10).

    Deliberately stateful and small. The set of alert codes is the memory: the
    same set twice in a row is one message, not one an hour, and an empty set
    clears it so a problem that comes back is worth saying again. Nothing is
    stored outside the process — a restart at worst re-sends once, which is
    cheaper than a deduplication table nobody asked for.

    With no mail channel (`mailer` is None), no configured operator, or no
    address on the operator's account, this is a complete no-op: the log and
    the page still say everything, exactly as before.
    """

    def __init__(
        self,
        *,
        mailer: Mailer | None,
        users: UserStore,
        operator: str = "",
        site_url: str = "",
    ) -> None:
        self._mailer = mailer
        self._users = users
        self._operator = operator
        self._site_url = site_url.rstrip("/")
        self._sent: tuple[str, ...] | None = None

    def deliver(self, alerts: list[Alert]) -> bool:
        """Send one message if the list has changed; True when one went out."""
        codes = tuple(alert.code for alert in alerts)
        if codes == self._sent:
            return False
        self._sent = codes
        if not codes or self._mailer is None:
            return False
        address = self.address()
        if not address:
            logger.info("operator.alert_mail_skipped reason=no_address")
            return False
        subject, text = alert_mail(alerts, site_url=self._site_url)
        try:
            sent = self._mailer.send(to=address, subject=subject, text=text)
        except Exception:  # a mail outage must never break the sweep
            logger.warning("operator.alert_mail_failed", exc_info=True)
            return False
        logger.info(
            "operator.alert_mail sent=%s codes=%s provider=%s",
            sent,
            ",".join(codes),
            self._mailer.name,
        )
        return sent

    def address(self) -> str:
        """The operator's own address, from `OPERATOR_SUB`.

        A listed email is used as it is; a listed Google subject id is looked
        up, which needs that person to have signed in once. No address means no
        mail — never a guess.
        """
        tokens = [part.strip() for part in self._operator.split(",") if part.strip()]
        for token in tokens:
            if "@" in token:
                return token
        for token in tokens:
            person = self._users.get(token)
            if person is not None and person.email:
                return person.email
        return ""
