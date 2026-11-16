"""Handing memory back after a book or a print file (the 2026-11-16 OOM)."""

from __future__ import annotations

import gc

import pytest

from app.services import memory


def test_release_memory_collects_cycles_and_trims(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(gc, "collect", lambda: calls.append("collect") or 0)
    monkeypatch.setattr(memory, "_malloc_trim", lambda: lambda pad: calls.append(pad))
    memory.release_memory()
    assert calls == ["collect", 0]


def test_release_memory_without_malloc_trim_only_collects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(gc, "collect", lambda: calls.append("collect") or 0)
    monkeypatch.setattr(memory, "_malloc_trim", lambda: None)
    memory.release_memory()
    assert calls == ["collect"]


def test_release_memory_runs_for_real() -> None:
    memory.release_memory()
