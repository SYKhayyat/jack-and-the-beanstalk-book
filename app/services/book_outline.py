"""The pages of a finished book, in order: one list for every way it is read.

The PDF, the on-screen reader and the shared link used to build their pages
separately, and they drifted: the PDF had 24 pages, the reader 21 (audit
11-15 R1/R4). Each now walks `book_leaves`, so the cover, the copyright page,
the story, "The End" and the About page come in the same order everywhere,
and the copyright page says the same words on paper and on screen.

Page numbers follow one rule (P4): story page *n* prints "n". The cover, the
copyright page, "The End" and the About page carry no number.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import NamedTuple

__all__ = [
    "MATTER_LEAVES",
    "CopyrightLine",
    "Leaf",
    "book_leaves",
    "book_year",
    "copyright_lines",
]


class Leaf(StrEnum):
    COVER = "cover"
    COPYRIGHT = "copyright"
    STORY = "story"
    END = "end"
    ABOUT = "about"


def book_leaves(story_pages: int) -> list[Leaf]:
    """Every page of a book with `story_pages` story pages, front to back."""
    return [Leaf.COVER, Leaf.COPYRIGHT, *([Leaf.STORY] * story_pages), Leaf.END, Leaf.ABOUT]


#: The pages that are not story pages: cover, copyright, "The End", About.
MATTER_LEAVES = len(book_leaves(0))


class CopyrightLine(NamedTuple):
    """One line of the copyright page; an empty `text` is a gap."""

    text: str
    size: float
    italic: bool


def copyright_lines(about_name: str, publisher: str, year: int) -> list[CopyrightLine]:
    """The copyright page's words, with the size the PDF sets each at."""
    return [
        CopyrightLine(f"Copyright © {year} {publisher}", 13.0, False),
        CopyrightLine("All rights reserved.", 11.0, True),
        CopyrightLine("", 11.0, False),
        CopyrightLine(f"Made for {about_name}.", 13.0, True),
        CopyrightLine("", 11.0, False),
        CopyrightLine(
            "No part of this book may be reproduced or distributed"
            " without written permission.",
            10.0,
            False,
        ),
        CopyrightLine("", 10.0, False),
        CopyrightLine(f"Published by {publisher}.", 11.0, False),
        CopyrightLine(f"First edition, {year}.", 10.0, True),
    ]


def book_year(created_at: str) -> int:
    """The year a book was made, which its copyright page names.

    Taken from when the book was started, not when a file was built, so an
    edit in January does not move a December book's copyright year. An
    unreadable timestamp falls back to this year.
    """
    try:
        return datetime.fromisoformat(created_at).year
    except (TypeError, ValueError):
        return datetime.now(UTC).year
