"""Step 9: the hand-in package is complete and leaks nothing.

Three things a grader (and a leak) checks without running the app:

* Every environment variable the code reads has a placeholder, and no secret
  placeholder carries a value.
* No live credential shape, private key or committed `.env` exists anywhere in
  the repository.
* The links the hand-in tells people to open are either absolute URLs or
  relative paths that actually resolve.

The last one is the closest a test can get to the private-window click-through;
the live Render page still has to be opened by a person.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "storage",
    "htmlcov",
}

TEXT_SUFFIXES = {
    "",
    ".css",
    ".example",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

SECRET_KEYS = (
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "AGNES_API_KEY",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "SESSION_SECRET",
    "APP_PASSWORD",
    "DATABASE_URL",
)


def _repo_files() -> list[Path]:
    return [
        path
        for path in PROJECT_ROOT.rglob("*")
        if path.is_file() and not any(part in SKIP_DIRS for part in path.parts)
    ]


def _text_files() -> list[Path]:
    return [
        path
        for path in _repo_files()
        if path.suffix in TEXT_SUFFIXES or path.name.startswith(".env")
    ]


class TestEnvExample:
    @pytest.fixture
    def example(self) -> str:
        return (PROJECT_ROOT / ".env.example").read_text()

    def test_every_setting_the_code_reads_has_a_placeholder(self, example: str) -> None:
        documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", example, re.M))
        expected = {name.upper() for name in Settings.model_fields}
        assert expected - documented == set(), "a settings field has no env placeholder"

    def test_every_placeholder_is_a_setting_the_code_reads(self, example: str) -> None:
        documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", example, re.M))
        expected = {name.upper() for name in Settings.model_fields}
        assert documented - expected == set(), "a documented variable is not read"

    @pytest.mark.parametrize("key", SECRET_KEYS)
    def test_secret_placeholders_are_empty(self, example: str, key: str) -> None:
        assert re.search(rf"^{key}=\s*$", example, re.M), f"{key} must stay blank"


class TestNoCommittedSecrets:
    def test_only_the_example_env_file_exists(self) -> None:
        env_files = [path for path in _repo_files() if path.name.startswith(".env")]
        assert [path.name for path in env_files] == [".env.example"]

    @pytest.mark.parametrize(
        "pattern",
        [
            pytest.param(r"\bsk-[A-Za-z0-9_-]{20,}", id="openai-key"),
            pytest.param(r"\bAIza[0-9A-Za-z_-]{30,}", id="google-api-key"),
            pytest.param(r"\bAKIA[0-9A-Z]{16}\b", id="aws-key"),
            pytest.param(r"\bxox[baprs]-[A-Za-z0-9-]{10,}", id="slack-token"),
            pytest.param(r"\bghp_[A-Za-z0-9]{30,}", id="github-token"),
            pytest.param(
                r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", id="private-key"
            ),
        ],
    )
    def test_no_live_credential_shape_appears(self, pattern: str) -> None:
        offenders = []
        for path in _text_files():
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            if re.search(pattern, text):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
        assert offenders == [], f"{pattern} found in {offenders}"


class TestHandInLinks:
    def _documents(self) -> list[Path]:
        return [
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "to-do.md",
            *sorted((PROJECT_ROOT / "docs").rglob("*.md")),
        ]

    def test_relative_links_resolve(self) -> None:
        missing: list[str] = []
        for document in self._documents():
            if not document.exists():
                continue
            for target in re.findall(r"\]\(([^)]+)\)", document.read_text()):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                path_part = target.split("#", 1)[0]
                if not path_part:
                    continue
                local = document.parent / path_part
                rooted = PROJECT_ROOT / path_part
                if not (local.exists() or rooted.exists()):
                    missing.append(f"{document.name} -> {target}")
        assert missing == [], "links point at files that do not exist: " + str(missing)

    def test_the_three_deliverables_are_named(self) -> None:
        text = (PROJECT_ROOT / "to-do.md").read_text()
        assert "https://jack-and-the-beanstalk-book.onrender.com/" in text
        assert "https://github.com/SYKhayyat/jack-and-the-beanstalk-book" in text
        assert "blob/main/to-do.md" in text

    def test_every_document_uses_https_for_external_links(self) -> None:
        insecure = []
        for document in self._documents():
            if not document.exists():
                continue
            for target in re.findall(r"\]\((http://[^)]+)\)", document.read_text()):
                insecure.append(f"{document.name} -> {target}")
        assert insecure == [], insecure
