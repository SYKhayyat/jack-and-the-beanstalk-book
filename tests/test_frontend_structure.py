"""Structural contract for the four screens: shelf, studio, About, book.

The UI (camera, downscale, takes, the page flip) is front-end work pytest
cannot click. What pytest *can* police is the wiring a hand-written script
depends on — every id a script reaches for exists on the page it runs on, every
page loads its one module, `fetch` lives in one module — and the shape the
plan promises (``docs/ui-redesign-plan.md``): the star first and required, every
setting behind "Adjust the look", the page shape and photo choice on the About
screen, and a book stage that shows every picture and the About page.
"""

from __future__ import annotations

import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from app.models import CartoonSettings, RejectionReason
from app.stories import NOT_READY, shelf
from tests.helpers import jack, make_photo_bytes, multipart

JACK = jack()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC = PROJECT_ROOT / "app" / "static"

STUDIO = "/stories/jack_and_the_beanstalk/studio"
ABOUT = "/stories/jack_and_the_beanstalk/about"

# One concern per file. The page controllers are the four screen scripts plus
# the drawer; the rest are shared seams.
MODULES = (
    "api.js",
    "photo.js",
    "downscale.js",
    "studio-draft.js",
    "shelf.js",
    "studio.js",
    "about.js",
    "book-live.js",
    "drawer.js",
)
RETIRED = ("book.js", "book-view.js", "book-detail.js", "book-state.js")


def _read(name: str) -> str:
    return (STATIC / name).read_text()


def _strip_js_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", source)


def _ids_read_by(script: str) -> set[str]:
    return set(re.findall(r'getElementById\("([^"]+)"\)', _read(script)))


def _rendered_ids(html: str) -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', html))


def _book(client: TestClient, **fields: object) -> str:
    response = client.post(
        "/api/cartoonify",
        **multipart(
            {
                "story": "jack_and_the_beanstalk",
                "photo": ("me.png", make_photo_bytes(), "image/png"),
            }
        ),
    )
    assert response.status_code == 201, response.text
    data = {"generation_id": response.json()["generation_id"], "child_name": "Mia"}
    data.update(fields)
    started = client.post("/api/book/start", data={"story": "jack_and_the_beanstalk", **data})
    assert started.status_code == 201, started.text
    return str(started.json()["book_id"])


def _wait(client: TestClient, book_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 20
    payload: dict[str, object] = {}
    while time.monotonic() < deadline:
        payload = client.get(f"/api/book/{book_id}").json()
        if payload["status"] in {"complete", "failed"}:
            break
        time.sleep(0.03)
    return payload


@pytest.fixture
def shelf_html(client: TestClient) -> str:
    response = client.get("/")
    assert response.status_code == 200
    return response.text


@pytest.fixture
def studio_html(client: TestClient) -> str:
    response = client.get(STUDIO)
    assert response.status_code == 200
    return response.text


@pytest.fixture
def about_html(client: TestClient) -> str:
    response = client.get(ABOUT)
    assert response.status_code == 200
    return response.text


@pytest.fixture
def book_html(client: TestClient) -> str:
    book_id = _book(client)
    _wait(client, book_id)
    response = client.get(f"/books/{book_id}")
    assert response.status_code == 200
    return response.text


# --- the module split ------------------------------------------------------------


def test_every_module_file_exists_and_the_old_ones_are_gone() -> None:
    for name in MODULES:
        assert (STATIC / name).is_file(), f"{name} is missing"
    for name in RETIRED:
        assert not (STATIC / name).exists(), f"{name} should have been retired"


def test_fetch_is_called_only_from_the_api_client() -> None:
    for name in MODULES:
        uses = len(re.findall(r"\bfetch\(", _strip_js_comments(_read(name))))
        if name == "api.js":
            assert uses >= 1
        else:
            assert uses == 0, f"{name} should not fetch"


def test_api_errors_carry_the_http_status() -> None:
    """A 401 must be tellable apart from a failure, or the resume flow is dead."""
    assert "error.status = status" in _read("api.js")
    for name in ("studio.js", "about.js", "book-live.js", "drawer.js"):
        assert "error.status === 401" in _read(name), f"{name} ignores an expired session"


def test_storage_is_always_guarded() -> None:
    """Private windows throw on localStorage; the studio must still work."""
    source = _strip_js_comments(_read("studio-draft.js"))
    assert source.count("try {") >= 3
    for name in ("studio.js", "about.js"):
        source = _strip_js_comments(_read(name))
        assert "localStorage" not in source, f"{name} must go through studio-draft.js"


def test_the_book_poller_gives_up_after_consecutive_failures() -> None:
    source = _strip_js_comments(_read("book-live.js"))
    assert "function poll(failures)" in source
    assert "failures + 1 < MAX_QUIET_POLLS" in source
    assert "poll(failures + 1);" in source


def test_replacing_a_face_names_the_slot_it_is_for() -> None:
    """Without `character` the server draws a star, and the edit refuses it."""
    source = _strip_js_comments(_read("book-live.js"))
    assert 'if (key !== "star") portraitForm.append("character", key);' in source


def test_a_studio_comic_names_its_character() -> None:
    source = _strip_js_comments(_read("studio.js"))
    assert 'if (key !== "star") body.append("character", key);' in source


def test_checkbox_settings_are_sent_explicitly() -> None:
    """An unchecked box sends nothing, and the server's default is True.

    The studio serialises its own settings so "Keep glasses" off really is off.
    """
    source = _strip_js_comments(_read("studio.js"))
    assert '(field.checked ? "true" : "false")' in source


# --- every page loads one module, and every id its script reads ---------------------


@pytest.mark.parametrize(
    ("fixture", "script"),
    [
        ("shelf_html", "shelf.js"),
        ("studio_html", "studio.js"),
        ("about_html", "about.js"),
        ("book_html", "book-live.js"),
    ],
)
def test_each_screen_loads_its_module_and_has_every_id_it_reads(
    request: pytest.FixtureRequest, fixture: str, script: str
) -> None:
    html: str = request.getfixturevalue(fixture)
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.DOTALL)
    assert all(not body.strip() for _attrs, body in scripts), "no inline JavaScript"
    assert f'type="module" src="/static/{script}"' in html
    missing = _ids_read_by(script) - _rendered_ids(html)
    assert not missing, f"{script} reads ids the page does not render: {missing}"


# --- the shelf ----------------------------------------------------------------------


def test_the_shelf_offers_both_stories(shelf_html: str) -> None:
    assert "Which adventure is it today?" in shelf_html
    assert f'href="{STUDIO}"' in shelf_html
    red = re.search(
        r'<article[^>]*data-story="little_red_riding_hood".*?</article>', shelf_html, re.S
    )
    assert red is not None
    assert 'href="/stories/little_red_riding_hood/studio"' in red.group(0)
    assert "Start this book" in red.group(0)
    assert "Coming soon" not in red.group(0)
    # Without a book of their own, a card cycles through finished sample covers.
    assert 'src="/static/covers/little_red_riding_hood/' in red.group(0)
    assert "starring " in red.group(0)


def test_a_story_held_by_the_start_gate_says_so_and_has_no_way_in(
    client: TestClient,
) -> None:
    """T6.5: with the gate on and no approved plates, the card is "not ready yet"."""
    client.app.state.container.templates.set_requires_template("little_red_riding_hood", True)
    shelf = client.get("/").text
    red = re.search(
        r'<article[^>]*data-story="little_red_riding_hood".*?</article>', shelf, re.S
    )
    assert red is not None
    assert "Not ready yet" in red.group(0)
    assert str(escape(NOT_READY)) in red.group(0)
    assert "<a " not in red.group(0)
    # Jack's card is untouched: the gate is per story.
    assert f'href="{STUDIO}"' in shelf
    for screen in ("studio", "about"):
        response = client.get(f"/stories/little_red_riding_hood/{screen}")
        assert response.status_code == 409
        assert NOT_READY in response.json()["detail"]


def test_reds_studio_offers_her_own_cast_and_no_outfit(client: TestClient) -> None:
    html = client.get("/stories/little_red_riding_hood/studio").text
    keys = re.findall(r'<section class="character[^"]*"[^>]*data-section="([a-z_]+)"', html)
    assert keys == ["star", "mother", "grandmother", "woodcutter"]
    assert 'data-section="wolf"' not in html
    # Red wears the story's red hood, so there is no outfit to pick.
    assert 'name="outfit"' not in html
    assert 'name="outfit"' in client.get(STUDIO).text


def test_every_story_has_finished_sample_covers_that_are_served(client: TestClient) -> None:
    for story in shelf(client.app.state.container.templates):
        assert len(story.samples) >= 2, story.key
        for sample in story.samples:
            response = client.get(sample.url)
            assert response.status_code == 200, sample.url
            assert response.headers["content-type"] == "image/jpeg"
            assert sample.name in sample.title + sample.byline


def test_a_sample_cover_is_titled_as_its_book_would_be(client: TestClient) -> None:
    by_key = {story.key: story for story in shelf(client.app.state.container.templates)}
    jack = next(s for s in by_key["jack_and_the_beanstalk"].samples if s.name == "Leo")
    assert (jack.title, jack.byline) == ("Leo and the Beanstalk", "A story for Leo")
    red = next(s for s in by_key["little_red_riding_hood"].samples if s.name == "Ava")
    assert (red.title, red.byline) == ("Little Red Riding Hood", "starring Ava")


def test_an_unknown_story_has_no_studio(client: TestClient) -> None:
    assert client.get("/stories/no_such_story/studio").status_code == 404
    assert client.get("/stories/no_such_story/about").status_code == 404


def test_one_header_on_every_screen(
    shelf_html: str, studio_html: str, about_html: str, book_html: str
) -> None:
    for html in (shelf_html, studio_html, about_html, book_html):
        assert 'class="site-header"' in html
        assert 'class="wordmark" href="/"' in html
        assert "Books are kept for 7 days." in html


# --- the studio ------------------------------------------------------------------------


def test_the_studio_has_one_section_per_character_star_first(studio_html: str) -> None:
    keys = re.findall(
        r'<section class="character[^"]*"[^>]*data-section="([a-z_]+)"', studio_html
    )
    assert keys == [
        "star",
        *(key for key, _ in [(member.key, member.label) for member in JACK.photoable_cast]),
    ]
    star = re.search(r'data-section="star"[^>]*data-required="([a-z]+)"', studio_html)
    assert star is not None and star.group(1) == "true"
    for key, _ in [(member.key, member.label) for member in JACK.photoable_cast]:
        assert re.search(rf'data-section="{key}"[^>]*data-required="false"', studio_html)


def test_every_section_offers_camera_upload_and_a_comic(studio_html: str) -> None:
    sections = re.findall(r'<section class="character.*?</section>', studio_html, flags=re.S)
    assert len(sections) == 1 + len(
        [(member.key, member.label) for member in JACK.photoable_cast]
    )
    for section in sections:
        assert 'data-action="camera"' in section
        assert 'type="file"' in section and "required" not in re.search(
            r'<input type="file"[^>]*>', section
        ).group(0)
        assert re.search(r'data-action="comic"[^>]*disabled', section), (
            "no photo, no comic yet"
        )
        # "Not quite right?" only once a comic exists.
        assert re.search(r'class="review hidden"', section)
        assert section.count('name="reason"') == len(RejectionReason)
        assert 'data-action="advanced"' in section
        assert "Adjust the look" in section


def test_the_camera_and_the_look_are_real_dialogs(studio_html: str) -> None:
    for dialog_id in ("camera-dialog", "advanced-dialog"):
        assert re.search(rf'<dialog class="modal" id="{dialog_id}"', studio_html)


class _InsideDialog(HTMLParser):
    """Which named controls sit inside the Advanced dialog."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.names: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "dialog" and attributes.get("id") == "advanced-dialog":
            self.depth = 1
        elif self.depth and tag == "dialog":
            self.depth += 1
        elif self.depth and tag in {"select", "input"} and attributes.get("name"):
            self.names.add(str(attributes["name"]))

    def handle_endtag(self, tag: str) -> None:
        if self.depth and tag == "dialog":
            self.depth -= 1


def test_every_portrait_setting_lives_behind_adjust_the_look(studio_html: str) -> None:
    parser = _InsideDialog()
    parser.feed(studio_html)
    # Book-wide or identity choices live elsewhere: the page shape on the About
    # screen, the photo's gender beside the star's photo; the book has no
    # separate character_gender.
    expected = set(CartoonSettings.model_fields) - {
        "aspect_ratio",
        "photo_gender",
        "character_gender",
    }
    assert parser.names == expected
    assert "one_step" not in studio_html, "make-it-in-one-go is gone"
    assert 'name="photo_gender-star"' in studio_html


def test_the_look_defaults_match_the_server(studio_html: str) -> None:
    defaults = CartoonSettings()
    for name, value in (
        ("cartoon_style", defaults.cartoon_style),
        ("cartoon_intensity", defaults.cartoon_intensity),
        ("likeness_strength", defaults.likeness_strength),
        ("portrait_background", defaults.portrait_background),
        ("apparent_age", defaults.apparent_age),
        ("skin_tone", defaults.skin_tone),
        ("hair_style", defaults.hair_style),
        ("hair_color", defaults.hair_color),
        ("outfit", defaults.outfit),
        ("expression", defaults.expression),
        ("candidates", defaults.candidates),
    ):
        assert re.search(
            rf'name="{name}".*?<option value="{re.escape(str(value))}" selected',
            studio_html,
            flags=re.S,
        ), f"{name} does not default to {value}"
    assert re.search(r'name="keep_glasses"[^>]*checked', studio_html)
    assert re.search(r'name="keep_facial_hair"[^>]*checked', studio_html)


def test_the_intensity_dial_speaks_plain_english(studio_html: str) -> None:
    select = re.search(r'<select id="adv-cartoon_intensity".*?</select>', studio_html, re.S)
    options = re.findall(r'<option value="(\d+)"[^>]*>([^<]+)</option>', select.group(0))
    assert len(options) == 10
    assert ("1", "Almost a photo") in options
    assert ("10", "Fully graphic") in options


def test_the_studio_speaks_no_cost(studio_html: str) -> None:
    assert "A bright, face-on photo works best." in studio_html
    assert "Each comic is one picture" not in studio_html
    assert "99" not in re.sub(r"<[^>]+>", "", studio_html)


def test_the_size_limit_is_a_data_attribute(studio_html: str) -> None:
    assert re.search(r'<body[^>]*data-max-upload-mb="\d+"', studio_html)


def test_next_starts_disabled_and_start_over_exists(studio_html: str) -> None:
    assert re.search(r'id="studio-next"[^>]*aria-disabled="true"', studio_html)
    assert f'href="{ABOUT}"' in studio_html
    assert 'id="studio-start-over"' in studio_html


# --- the About screen ----------------------------------------------------------------


def test_the_about_screen_holds_the_book_wide_choices(about_html: str) -> None:
    assert "The last page is about" in about_html
    for value in ("cartoon", "photo", "both"):
        assert f'name="about_photo" value="{value}"' in about_html
    assert re.search(r'name="about_photo" value="cartoon" checked', about_html)
    shapes = re.findall(r'name="aspect_ratio" value="([^"]+)"', about_html)
    assert set(shapes) == {"4:3", "1:1", "3:4", "16:9"}
    default = CartoonSettings().aspect_ratio
    assert re.search(rf'name="aspect_ratio" value="{re.escape(default)}" checked', about_html)
    assert 'id="make-book"' in about_html
    assert "What do they want to be?" in about_html


def test_no_fee_line_without_money(about_html: str) -> None:
    """A deployment with no payments makes free books, and says nothing of fees."""
    assert "fee of" not in about_html


# --- the book ----------------------------------------------------------------------------


def test_the_book_stage_shows_every_picture_and_the_about_page(book_html: str) -> None:
    tiles = re.findall(r'class="map-tile ([a-z]+)', book_html)
    assert len(tiles) == 21, "cover + twenty story pages"
    assert len(re.findall(r'class="spread[ "]', book_html)) == 22
    assert 'data-number="about"' in book_html
    assert "The End" in book_html


def test_a_finished_book_offers_both_downloads_and_hides_progress(book_html: str) -> None:
    assert re.search(r'class="progress-block hidden"', book_html)
    assert re.search(r'class="finished-block"', book_html)
    assert "Download the book (PDF)" in book_html
    assert "Keep the photos and comics (zip)" in book_html
    assert "/sources.zip" in book_html
    assert "prompts.json" in book_html
    assert "Changes are included — the book's fee was taken once." in book_html
    assert book_html.count('name="reason"') >= len(RejectionReason)


def test_a_drawing_book_shows_progress_not_downloads(client: TestClient) -> None:
    from app.services.book_store import BookStatus

    book_id = _book(client)
    _wait(client, book_id)
    container = client.app.state.container  # type: ignore[attr-defined]
    container.book_store.save_status(
        book_id, BookStatus.RENDERING, [], title="Mia and the Beanstalk"
    )
    html = client.get(f"/books/{book_id}").text
    assert re.search(r'class="progress-block"', html)
    assert re.search(r'class="finished-block hidden"', html)
    assert re.search(r'class="edit-strip hidden"', html)
    assert 'aria-live="polite"' in html


# --- nothing technical on a visitor's screen ------------------------------------------------


def test_the_pages_show_no_provider_internals(
    shelf_html: str, studio_html: str, about_html: str, book_html: str
) -> None:
    for html in (shelf_html, studio_html, about_html, book_html):
        for jargon in ("Provider:", "provider-badge", "quality=", "model=", "mock mode"):
            assert jargon not in html, f"the page leaks {jargon!r}"
        for field in ("provider", "model", "image_provider", "openai_model", "gemini_model"):
            assert f'name="{field}"' not in html
        for jargon in ("image call", "per-minute allowance", "cost-note"):
            assert jargon not in html
    assert "Demo mode" in shelf_html, "a placeholder run says so plainly"


def test_the_options_endpoint_offers_no_backend_choice(client: TestClient) -> None:
    payload = client.get("/api/settings-options").json()
    for key in ("provider", "providers", "model", "models", "image_provider"):
        assert key not in payload


def test_the_open_health_endpoint_names_neither_provider_nor_model(client: TestClient) -> None:
    payload = client.get("/healthz").json()
    assert "name" not in payload["provider"]
    for leak in ("recording", "mock", "openai", "gemini"):
        assert leak not in json.dumps(payload).lower()


def test_the_generation_response_does_not_name_the_backend(
    client: TestClient, photo: bytes
) -> None:
    response = client.post(
        "/api/cartoonify",
        files={"photo": ("me.png", photo, "image/png")},
        data={"story": "jack_and_the_beanstalk", "aspect_ratio": "4:3"},
    )
    assert response.status_code == 201
    assert "provider" not in response.json()
    assert "model" not in response.json()


def test_static_files_are_revalidated_so_a_deploy_is_never_stale(client: TestClient) -> None:
    for path in ("/static/studio.js", "/static/fonts/Literata-Regular.ttf"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-cache"
        assert "etag" in response.headers


def test_every_request_that_spends_for_a_book_names_its_story() -> None:
    """The studio, the About screen and the face edit all say which story.

    The API refuses a portrait or a start without one, so a script that forgot
    it would fail for every visitor; this keeps the three callers honest.
    """
    static = Path(__file__).resolve().parent.parent / "app" / "static"
    assert 'body.append("story", story);' in (static / "studio.js").read_text()
    assert 'body.append("story", story);' in (static / "about.js").read_text()
    live = (static / "book-live.js").read_text()
    assert "const bookStory = body.dataset.bookStory;" in live
    assert 'portraitForm.append("story", bookStory);' in live


def test_the_book_page_tells_its_script_which_story_it_is(book_html: str) -> None:
    assert 'data-book-story="jack_and_the_beanstalk"' in book_html
