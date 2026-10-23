"""The secret-redaction helpers: nothing that looks like a key reaches a handler."""

from __future__ import annotations

import logging

import pytest

import app.redaction as redaction
from app.redaction import (
    SecretRedactingFilter,
    install_secret_redaction,
    redact,
)


def make_record(message: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("test", logging.ERROR, __file__, 0, message, args, None)


def _strip_redacting_filters() -> None:
    root = logging.getLogger()
    for target in (root, *root.handlers):
        target.filters[:] = [
            f for f in target.filters if not isinstance(f, SecretRedactingFilter)
        ]


@pytest.fixture(autouse=True)
def _fresh_redaction_state():
    """The install function keeps module-level state; isolate each test."""
    redaction._installed = None
    _strip_redacting_filters()
    yield
    redaction._installed = None
    _strip_redacting_filters()


class TestRedact:
    def test_replaces_every_occurrence(self) -> None:
        assert redact("a sk-x b sk-x", ["sk-x"]) == "a ***REDACTED*** b ***REDACTED***"

    def test_leaves_text_without_the_secret_alone(self) -> None:
        assert redact("nothing to hide", ["sk-x"]) == "nothing to hide"

    def test_ignores_empty_secrets(self) -> None:
        # An unset key is the empty string; it must not "match" everywhere and
        # turn every message into REDACTED.
        assert redact("keep me", ["", ""]) == "keep me"


class TestSecretRedactingFilter:
    def test_scrubs_the_formatted_message(self) -> None:
        record = make_record("Authorization: Bearer %s\n", "sk-leaky")
        assert SecretRedactingFilter(["sk-leaky"]).filter(record) is True
        assert record.getMessage() == "Authorization: Bearer ***REDACTED***\n"

    def test_scrubs_a_secret_that_arrives_only_in_args(self) -> None:
        record = make_record("key=%s", "sk-leaky")
        SecretRedactingFilter(["sk-leaky"]).filter(record)
        assert "sk-leaky" not in record.getMessage()

    def test_passes_records_through_unchanged_without_secrets(self) -> None:
        record = make_record("plain")
        SecretRedactingFilter([]).filter(record)
        assert record.getMessage() == "plain"

    def test_add_secrets_accumulates(self) -> None:
        filt = SecretRedactingFilter(["sk-a"])
        filt.add_secrets(["sk-b", "", "sk-a"])
        assert set(filt.secrets) == {"sk-a", "sk-b"}


class TestInstallSecretRedaction:
    def test_attaches_to_existing_root_handlers(self) -> None:
        handler = logging.StreamHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            install_secret_redaction(["sk-x"])
            assert any(isinstance(f, SecretRedactingFilter) for f in handler.filters)
            assert any(isinstance(f, SecretRedactingFilter) for f in root.filters)
        finally:
            root.removeHandler(handler)

    def test_is_idempotent(self) -> None:
        root = logging.getLogger()
        install_secret_redaction(["sk-x"])
        install_secret_redaction(["sk-x"])
        installed = [f for f in root.filters if isinstance(f, SecretRedactingFilter)]
        assert len(installed) == 1

    def test_a_later_call_extends_the_same_filter(self) -> None:
        # A second startup with a new key must widen redaction, not be ignored.
        install_secret_redaction(["sk-a"])
        install_secret_redaction(["sk-b"])
        root = logging.getLogger()
        installed = [f for f in root.filters if isinstance(f, SecretRedactingFilter)]
        assert len(installed) == 1
        assert set(installed[0].secrets) == {"sk-a", "sk-b"}

    def test_no_secrets_installs_nothing(self) -> None:
        root = logging.getLogger()
        before = len(root.filters)
        install_secret_redaction(["", ""])
        assert len(root.filters) == before
