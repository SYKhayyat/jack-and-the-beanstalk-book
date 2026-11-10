"""A page's words and its plate must agree.

The plate is painted from a page's `environment` and `camera`; the edit pass is
told the page's `caption` and `must_include`. If those say different things the
edit model repaints the plate to match the words (page 17: a stalk "falling"
over a plate whose stalk stands). So the words may only name scenery the plate
was painted with.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

BOOKS = sorted((Path(__file__).parent.parent / "app" / "books").glob("*.toml"))

#: Unambiguous scenery nouns (not "door" or "perch", which are also what a
#: character does). Naming one in the edit text without the plate's own text
#: naming it too means the edit would have to invent it.
SCENERY = ("stump", "hearth", "castle", "fence", "window", "nest", "table", "chair")


def _pages() -> list[tuple[str, dict]]:
    out = []
    for path in BOOKS:
        data = tomllib.loads(path.read_text())
        out += [(f"{path.stem}/{p['key']}", p) for p in data.get("pages", [])]
    return out


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


@pytest.mark.parametrize(("label", "page"), _pages(), ids=[p[0] for p in _pages()])
def test_the_edit_text_names_no_scenery_the_plate_lacks(label, page):
    plate_words = _words(page["environment"] + " " + page["camera"])
    edit_words = _words(page["caption"] + " " + " ".join(page["must_include"]))
    # Singular/plural: "chairs" is the chair.
    missing = sorted(
        w
        for w in SCENERY
        if w in edit_words and w not in plate_words and w + "s" not in plate_words
    )
    assert not missing, f"{label}: the words name {missing} but the plate's text does not"


def test_a_standing_stalk_is_never_described_falling_in_a_plate_page():
    for label, page in _pages():
        if page.get("staging") != "far_off_fall":
            continue
        text = (page["caption"] + page["environment"] + page["camera"]).lower()
        assert "sweeping down" not in text, label
        assert "still joined to the flat stump" in page["caption"], label


def test_the_openai_reviewer_compares_against_the_plate():
    from app.prompt_data import prompt_text

    rule = prompt_text()["review"]["plate_rule"]
    assert "SECOND picture" in rule and "plate" in rule
