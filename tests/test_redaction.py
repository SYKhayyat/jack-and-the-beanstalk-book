"""The secret-redaction helpers: nothing that looks like a key reaches a handler."""

from __future__ import annotations

import logging
from pathlib import Path

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


class TestTheSecretsTheAppArmsAtStartup:
    """Every secret the app can read is armed the moment it boots.

    Sign-in added two: the Google client secret and the session key. Both get
    pasted whole into a dashboard, which is exactly how a secret ends up in a
    log line.
    """

    def test_the_google_client_secret_and_session_key_are_armed(
        self, tmp_path: Path, caplog
    ) -> None:
        from app.config import Settings
        from app.main import create_app

        secret = "google-client-secret-value"
        session_key = "session-secret-value"
        settings = Settings(
            image_provider="mock",  # type: ignore[arg-type]
            gemini_api_key="",
            app_env="test",
            storage_dir=tmp_path / "storage",
            google_client_id="a-client-id",
            google_client_secret=secret,
            session_secret=session_key,
        )
        create_app(settings)

        with caplog.at_level(logging.WARNING, logger="app.main"):
            logger = logging.getLogger("app.main")
            logger.warning("pasted the secret %s and the key %s", secret, session_key)

        assert secret not in caplog.text
        assert session_key not in caplog.text
        assert caplog.text.count("***REDACTED***") >= 2

    def test_a_deployment_without_google_arms_nothing_new(self, tmp_path: Path) -> None:
        from app.config import Settings
        from app.main import create_app

        redaction._installed = None
        _strip_redacting_filters()
        create_app(
            Settings(
                image_provider="mock",  # type: ignore[arg-type]
                gemini_api_key="",
                app_env="test",
                storage_dir=tmp_path / "storage",
            )
        )
        installed = [
            f for f in logging.getLogger().filters if isinstance(f, SecretRedactingFilter)
        ]
        assert not installed or not installed[0].secrets

    def test_the_database_url_is_armed_whole(self, tmp_path: Path, caplog) -> None:
        """A connection URL embeds its password; the whole URL is a secret."""
        from app.config import Settings
        from app.main import create_app

        url = "postgres://user:db-password@host/db"
        redaction._installed = None
        _strip_redacting_filters()
        create_app(
            Settings(
                image_provider="mock",  # type: ignore[arg-type]
                gemini_api_key="",
                app_env="test",
                storage_dir=tmp_path / "storage",
                database_url=url,
            )
        )
        with caplog.at_level(logging.WARNING, logger="app.main"):
            logging.getLogger("app.main").warning("tried %s", url)
        assert "db-password" not in caplog.text
        assert url not in caplog.text
