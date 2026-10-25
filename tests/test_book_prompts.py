"""The book prompt builder: the identity contract and the fixed style."""

from __future__ import annotations

from app.models import CartoonSettings, CartoonStyle
from app.models.book import BOOK_PAGES, get_book_page
from app.prompt_data import prompt_text
from app.services.prompts import build_book_page_prompt, build_cartoon_prompt


def _prompt(page, settings, number):
    """One page's prompt at its real catalogue position."""
    return build_book_page_prompt(
        page, settings, page_number=number, total_pages=len(BOOK_PAGES)
    )


def test_every_page_carries_the_same_style_sentence() -> None:
    """The brief's hard rule: one style string, worded identically on every call."""
    settings = CartoonSettings()
    # [book].style is the *key* of the shared style sentence, kept in one place
    # in the data — book pages and the portrait default read the same words.
    style_sentence = prompt_text()["styles"][prompt_text()["book"]["style"]]
    prompts = [
        build_book_page_prompt(page, settings, page_number=n, total_pages=len(BOOK_PAGES))
        for n, page in enumerate(BOOK_PAGES, start=1)
    ]
    assert len(prompts) == len(BOOK_PAGES)
    for prompt in prompts:
        assert "ART STYLE: " + style_sentence in prompt


def test_the_book_style_is_the_portrait_default() -> None:
    """The UAT sentence pins both hands: one shared sentence, one source."""
    settings = CartoonSettings()
    portrait = build_cartoon_prompt(settings)
    page = build_book_page_prompt(
        BOOK_PAGES[0], settings, page_number=1, total_pages=len(BOOK_PAGES)
    )
    assert "ART STYLE: Modern 3D animated feature: rounded sculpted forms," in portrait
    assert "ART STYLE: Modern 3D animated feature: rounded sculpted forms," in page


def test_a_page_never_changes_style_with_the_portrait_picker() -> None:
    """A book's style is fixed book data; the Advanced pick cannot move it.

    The style sentence is sourced from the shared default, so a portrait made
    in another style does not drag the book's pages away from the one style.
    """
    page = BOOK_PAGES[0]
    fixed = build_book_page_prompt(
        page, CartoonSettings(), page_number=1, total_pages=len(BOOK_PAGES)
    )
    for other in (CartoonStyle.MANGA, CartoonStyle.WATERCOLOUR, CartoonStyle.WESTERN_CARTOON):
        moved = build_book_page_prompt(
            page,
            CartoonSettings(cartoon_style=other),
            page_number=1,
            total_pages=len(BOOK_PAGES),
        )
        assert moved == fixed


def test_model_is_told_not_to_draw_text() -> None:
    settings = CartoonSettings()
    prompt = _prompt(BOOK_PAGES[0], settings, 1)
    assert "no writing" in prompt or "Do not draw any text" in prompt


def test_both_identity_anchors_are_named_in_the_task() -> None:
    settings = CartoonSettings()
    prompt = _prompt(BOOK_PAGES[1], settings, 2)
    assert "cartoon portrait" in prompt
    assert "original photograph" in prompt


def test_quiet_zone_appears_with_the_page_settling_area() -> None:
    settings = CartoonSettings()
    page = BOOK_PAGES[1]  # trade: the sky must stay quiet
    prompt = _prompt(page, settings, 2)
    assert "QUIET ZONE:" in prompt
    assert "open sky" in prompt


def test_page_number_and_title_frame_the_call() -> None:
    settings = CartoonSettings()
    prompt = build_book_page_prompt(BOOK_PAGES[2], settings, page_number=3, total_pages=3)
    assert "page 3 of 3" in prompt
    assert "Jack and the Beanstalk" in prompt


def test_pronouns_follow_the_child() -> None:
    settings = CartoonSettings(photo_gender="girl", character_gender="match")
    prompt = _prompt(BOOK_PAGES[0], settings, 1)
    assert "Jane" in prompt
    assert "The character's name is Jane" in prompt


# --- the UAT consistency guards -------------------------------------------------
#
# Every one of these is a fixed sentence in [book], carried by every page
# prompt. They are prompt engineering, not a guarantee: the tests pin the
# wording, the real-API run confirms the picture.


def _all_page_prompts() -> list[str]:
    settings = CartoonSettings()
    return [
        build_book_page_prompt(page, settings, page_number=n, total_pages=len(BOOK_PAGES))
        for n, page in enumerate(BOOK_PAGES, start=1)
    ]


def test_characters_stay_the_same_across_the_book() -> None:
    """UAT 5: the cow changes colours between pictures."""
    for prompt in _all_page_prompts():
        assert "Every character in this book — person, animal or object — is" in prompt
        assert "exactly the same character on every page" in prompt
        assert "the way real people, real animals and real things do" in prompt


def test_milky_white_has_a_pinned_coat() -> None:
    """The cow's white coat is named on every page she appears in."""
    settings = CartoonSettings()
    cow_pages = [BOOK_PAGES[0], BOOK_PAGES[1]]
    for page in cow_pages:
        prompt = build_book_page_prompt(
            page, settings, page_number=page.number, total_pages=len(BOOK_PAGES)
        )
        assert "plain white coat" in prompt
    # Every page carries the pinned sentence; the ones drawing her also name her.
    for prompt in _all_page_prompts():
        assert "Milky-White the cow is the same white cow everywhere she appears" in prompt


def test_the_child_is_one_coherent_figure() -> None:
    """UAT 6: grotesquely big face (bobblehead) in one scene."""
    for prompt in _all_page_prompts():
        assert "a natural head-to-body ratio in every scene" in prompt
        assert "no oversized faces and no bobblehead proportions" in prompt


def test_likeness_at_distance_is_carried_by_silhouette_not_a_big_head() -> None:
    """UAT 6 follow-up: the bobblehead surfaced on wide shots.

    The identity directive demands a recognisable face on every page; on pages
    where the child is small (page 3's wide cottage shot), the model used to
    satisfy that demand by enlarging the head. The guard now tells the model
    where likeness lives at distance, and the small-figure pages name their
    scale outright.
    """
    for prompt in _all_page_prompts():
        assert "at wide-shot and long-shot distances the likeness is carried by" in prompt
        assert "the face is drawn small and simplified in true scale" in prompt
        assert "never turned into a partial close-up" in prompt
    # The two pages that draw the child small in the frame tell the model so.
    doorway = build_book_page_prompt(
        get_book_page("beans-out-the-window"),  # type: ignore[arg-type]
        CartoonSettings(),
        page_number=3,
        total_pages=len(BOOK_PAGES),
    )
    assert "only a small part of the frame height" in doorway
    assert "an enlarged head, oversized face or close-up of Jack" in doorway
    gate = next(
        spec for spec in prompt_text()["book_pages"] if spec["key"] == "beanstalk-at-dawn"
    )
    assert (
        "tiny at the bottom" in gate["camera"]
        and "true proportion to the gate and the beanstalk" in gate["camera"]
    )
    assert "an enlarged head, oversized face or close-up of {name}" in gate["must_avoid"]


def test_the_whole_scene_keeps_one_art_style() -> None:
    """UAT 10: the whole scene is drawn in the same cartoon style."""
    for prompt in _all_page_prompts():
        assert "Draw the entire scene — character, animals, background, props" in prompt
        assert "in exactly the one art style" in prompt
        assert "Mix no styles" in prompt
