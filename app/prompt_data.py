"""The one place that reads ``prompts.toml``.

Every word the app sends to an image model lives in ``app/prompts.toml``. This
module parses that file once and hands the sections to the code that needs them.

It deliberately imports nothing from :mod:`app.models`. The models own the enums
the text is keyed by and read their own wording through here, so importing them
back would be a cycle.

Because the text is data rather than a typed mapping, exhaustiveness is enforced
at import time instead of by mypy: a section is indexed by the enum's value, so
a new enum member with no matching entry raises a ``KeyError`` on the first
import — a missing line fails loudly at boot, never silently at request time.
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = ["prompt_text"]

_PATH = Path(__file__).resolve().parent / "prompts.toml"


@lru_cache(maxsize=1)
def prompt_text() -> dict[str, Any]:
    """Parse and cache ``prompts.toml``."""
    with _PATH.open("rb") as handle:
        return tomllib.load(handle)
