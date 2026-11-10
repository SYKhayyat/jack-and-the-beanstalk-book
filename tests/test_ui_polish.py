"""The quick UI pass (UI.org U3, U6, U11).

Front-end behaviour pytest cannot click is pinned as a source contract, the
same way `test_frontend_structure.py` pins the scripts' ids:

* a signed-in shelf card turns through *every* cover that person has of a
  story, not only the newest one;
* the carousel's autoplay is not tied to focus, so a click cannot strand it;
* dates carry a machine-readable UTC moment and are rewritten in the reader's
  timezone;
* dark mode restates the paper/ink tokens, and muted text is dark enough.
"""

from __future__ import annotations

from pathlib import Path

from app.routes.web import _card
from app.services.book_store import BookStatus, BookSummary

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC = PROJECT_ROOT / "app" / "static"
TEMPLATES = PROJECT_ROOT / "app" / "templates"


class _Story:
    """Only the fields `_card` reads."""

    key = "jack_and_the_beanstalk"
    available = True
    ready = True


def _summary(book_id: str, title: str, *, created: str) -> BookSummary:
    return BookSummary(
        book_id=book_id,
        story_key="jack_and_the_beanstalk",
        title=title,
        status=BookStatus.COMPLETE,
        created_at=created,
        updated_at=created,
        page_count=21,
        pages_done=21,
        cover_url=f"/book-media/{book_id}/cover",
        pdf_url=f"/book-media/{book_id}/pdf",
        subtitle="",
    )


def test_a_shelf_card_turns_through_all_of_that_person_s_books() -> None:
    """U6: the newest cover used to win and the fader was never rendered."""
    card = _card(
        _Story(),  # type: ignore[arg-type]
        [
            _summary("new", "Mia and the Beanstalk", created="2026-11-09T19:32:00"),
            _summary("old", "Leo and the Beanstalk", created="2026-11-01T09:00:00"),
        ],
    )
    assert card["cover_url"] == "/book-media/new/cover"
    assert [cover["book_id"] for cover in card["covers"]] == ["new", "old"]
    template = (TEMPLATES / "_story_card.html").read_text()
    assert "{% if card.covers and not signed_out %}" in template
    assert "{% for cover in card.covers %}" in template


def test_the_carousel_cannot_be_stranded_by_a_click() -> None:
    """U6: focusin used to pause it for as long as the browser kept focus."""
    source = (STATIC / "carousel.js").read_text()
    assert "focusin" not in source
    assert 'root.addEventListener("pointerenter"' in source
    assert 'root.addEventListener("pointerleave"' in source


def test_dates_are_rewritten_in_the_reader_s_timezone() -> None:
    """U11: times are stored naive UTC; the page turns them into local time."""
    chrome = (STATIC / "chrome.js").read_text()
    assert 'querySelectorAll("time[datetime]")' in chrome
    assert "${value}Z" in chrome
    assert "Intl.DateTimeFormat" in chrome
    layout = (TEMPLATES / "_layout.html").read_text()
    assert "{{ static_url }}/chrome.js" in layout
    drawer = (TEMPLATES / "_books_drawer.html").read_text()
    assert 'datetime="{{ item.updated_at }}"' in drawer
    assert 'data-format="day-month"' in drawer


def test_dark_mode_restates_the_palette_and_keeps_the_art_warm() -> None:
    css = (STATIC / "style.css").read_text()
    assert "@media (prefers-color-scheme: dark)" in css
    assert "color-scheme: dark" in css
    assert "--paper: #17141c" in css
    # The cream title on a cover is part of the artefact, not the chrome.
    assert "--cream: #fff9eb" in css


def test_muted_text_is_darker_than_the_audited_value() -> None:
    """U3: #6b6577 read faint at 14 px on cream; help text is 15 px now."""
    css = (STATIC / "style.css").read_text()
    assert "--ink-soft: #4f4a5c" in css
    assert "#6b6577" not in css
    assert "font-size: 15px" in css


def test_the_header_has_a_real_mark_and_an_account_menu() -> None:
    layout = (TEMPLATES / "_layout.html").read_text()
    assert "wordmark-mark" in layout
    assert "user-menu" in layout
    assert 'href="/admin"' in layout
    assert "Sign out" in layout
