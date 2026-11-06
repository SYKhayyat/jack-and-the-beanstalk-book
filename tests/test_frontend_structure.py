"""Structural contract for the book's front door.

The UI (camera capture, client-side downscale, options behind "Advanced",
ES-module split) is mostly front-end work that pytest cannot click. What
pytest *can* police — what the plan called its grep-able claims — is the layer
split and the landing-page shape. These tests do that, so the split cannot
silently drift back.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.models import CartoonSettings, RejectionReason
from app.services.prompts import photoable_cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC = PROJECT_ROOT / "app" / "static"

# The module layout: one concern per file, exactly as planned.
MODULES = ("book-state.js", "api.js", "photo.js", "downscale.js", "book-view.js", "book.js")

# DOM reads/writes live in the view alone. Downscale and the camera source are
# allowed the two documented exceptions: creating offscreen canvases and
# attaching/reading the injected elements they were handed.
DOM_TOKENS = (
    r"document\.|querySelector|getElementById"
    r"|classList|innerHTML|textContent|\.style\b"
)
DOM_EXCEPTIONS = {
    "downscale.js": ('document.createElement("canvas")',),
    "photo.js": (
        'document.createElement("canvas")',
        "video.srcObject",
        'fileInput.setAttribute("capture"',
    ),
    # The upload cap is a body data attribute; the controller reads it once.
    "book.js": ("document.body.dataset.maxUploadMb",),
}


def _read(name: str) -> str:
    return (STATIC / name).read_text()


def _strip_js_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", source)


def _forbidden_dom_hits(name: str, source: str) -> list[str]:
    source = _strip_js_comments(source)
    hits = []
    for match in re.finditer(DOM_TOKENS, source):
        start = max(0, match.start() - 40)
        snippet = " ".join(source[start : match.end() + 40].split())
        allowed = DOM_EXCEPTIONS.get(name, ())
        if not any(exception in snippet for exception in allowed):
            hits.append(f"{name}: …{snippet}…")
    return hits


def _view_ids() -> set[str]:
    """Every element id the view module reaches for."""
    return set(re.findall(r'document\.getElementById\("([^"]+)"\)', _read("book-view.js")))


# --- the module split --------------------------------------------------------


def test_every_module_file_exists() -> None:
    for name in MODULES:
        assert (STATIC / name).is_file(), f"{name} is missing"


@pytest.mark.parametrize(
    ("module", "forbidden"),
    [("book-state.js", r"document\."), ("api.js", r"document\."), ("book.js", DOM_TOKENS)],
)
def test_no_dom_access_outside_the_view(module: str, forbidden: str) -> None:
    source = _strip_js_comments(_read(module))
    for exception in DOM_EXCEPTIONS.get(module, ()):
        source = source.replace(exception, "")
    assert not re.search(forbidden, source), f"{module} touches the DOM"


def test_dom_access_outside_the_view_is_only_the_documented_exceptions() -> None:
    for name, source in ((name, _read(name)) for name in MODULES if name != "book-view.js"):
        assert not _forbidden_dom_hits(name, source)


def test_fetch_is_called_only_from_the_api_client() -> None:
    book_js_fetches = ["fetch("]  # seedTotalPages seeds the page count gracefully
    for name in MODULES:
        uses = len(re.findall(r"\bfetch\(", _read(name)))
        if name == "api.js":
            assert uses >= 1
        elif name == "book.js":
            assert uses == len(book_js_fetches)
        else:
            assert uses == 0, f"{name} should not fetch"


def test_book_js_is_a_thin_controller() -> None:
    source = _read("book.js")
    assert "renderPhotoPreview" not in source.split("import")[1], (
        "the controller must call view functions, not build DOM"
    )
    assert "createBookClient" in source, "HTTP is swapped in via the client"


def test_the_book_poller_gives_up_after_consecutive_failures() -> None:
    """A persistent 404/401/5xx must stop, not retry every 1.5s forever.

    `MAX_QUIET_POLLS` only works if the failure count survives the retry: a
    fresh `started` timestamp per call measured one request, never the run.
    """
    source = _strip_js_comments(_read("book.js"))
    assert "function pollBook(failures = 0)" in source
    assert "const failedPolls = failures + 1;" in source
    assert "failedPolls < MAX_QUIET_POLLS" in source
    assert "pollBook(failedPolls);" in source
    assert "Date.now() - started" not in source


def test_approving_a_portrait_drops_the_previous_book() -> None:
    """UAT: after a failed book, a fresh portrait said "make a portrait first".

    `isApproved` requires no `bookId`, so approving a new portrait must clear
    the stale id from the failed attempt — otherwise the user is stuck.
    """
    source = _strip_js_comments(_read("book-state.js"))
    body = source.split("export function approveDraft")[1].split("export function")[0]
    assert "state.bookId = null;" in body
    assert "state.bookStatus = null;" in body


def test_a_portrait_still_being_drawn_cannot_start_a_book() -> None:
    """The retry call is slow; approving while it runs must say so, not lie."""
    book = _strip_js_comments(_read("book.js"))
    assert "state.beginPortrait(book);" in book
    assert "state.endPortrait(book);" in book
    assert "book.portraitBusy" in book
    assert "The portrait is still being made" in book
    view = _strip_js_comments(_read("book-view.js"))
    assert "export function setReviewBusy" in view
    assert "elements.approve().disabled = busy;" in view


def test_the_library_can_delete_a_book_without_opening_it() -> None:
    """The sidebar cross posts the same DELETE, then takes the card away."""
    detail = _strip_js_comments(_read("book-detail.js"))
    assert "function deleteLibraryBook" in detail
    assert "api.removeOther(" in detail
    assert 'window.location.assign("/")' in detail
    api = _strip_js_comments(_read("api.js"))
    assert "removeOther:" in api


def test_the_detail_page_also_fetches_only_from_the_api_client() -> None:
    """The one-book page is its own controller, but `fetch` still lives in api.js."""
    source = _read("book-detail.js")
    assert not re.search(r"\bfetch\(", source), "book-detail.js must not fetch"
    assert 'from "./api.js"' in source
    assert "createBookDetailClient" in source


def test_replacing_a_face_names_the_slot_it_is_for() -> None:
    """A replacement portrait must be drawn for the chosen character.

    Without the `character` field the server makes a star portrait, and the
    edit endpoint refuses to file it under anyone else — so every non-star
    replacement failed. The key in the form is the contract.
    """
    detail = _strip_js_comments(_read("book-detail.js"))
    assert 'if (key !== "star") portraitForm.append("character", key);' in detail


def test_api_errors_carry_the_http_status() -> None:
    """A 401 must be tellable apart from a failure, or the resume flow is dead.

    The poller sends the visitor to sign-in and back to the same book when the
    session expires mid-render. That only works if the thrown error still
    knows the status code.
    """
    source = _read("api.js")
    assert "error.status = status" in source
    for name in ("book.js", "book-detail.js"):
        assert "error.status === 401" in _read(name)


def test_photo_sources_satisfy_one_seam() -> None:
    source = _read("photo.js")
    assert source.count("getBlob") >= 2, "both sources answer the same getBlob seam"
    for kind in ("file-picker", "camera"):
        assert f'kind: "{kind}"' in source


# --- the page loads one module script ----------------------------------------


@pytest.fixture
def page_html(client) -> str:
    response = client.get("/")
    assert response.status_code == 200
    return response.text


def test_landing_loads_exactly_one_module_script(page_html: str) -> None:
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", page_html, flags=re.DOTALL)
    assert len(scripts) == 1, "the page must carry its behaviour in exactly one script tag"
    assert 'type="module" src="/static/book.js"' in page_html
    assert not scripts[0].strip(), "no JavaScript may be inlined in the template"


def test_every_element_the_view_reads_exists_on_the_page(page_html: str) -> None:
    rendered_ids = set(re.findall(r'id="([^"]+)"', page_html))
    missing = _view_ids() - rendered_ids
    assert not missing, f"book-view.js reads ids the template does not render: {missing}"


# --- the landing page shape ---------------------------------------------------


class DepthRecorder(HTMLParser):
    """Records how deep inside <details> each identifiable element sits."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.details_depth = 0
        self.depth_by_id: dict[str, int] = {}
        self.radio_name_depth: dict[str, int] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "details":
            self.details_depth += 1
        element_id = attributes.get("id")
        if element_id:
            self.depth_by_id[element_id] = self.details_depth
        name = attributes.get("name")
        if tag == "input" and name and name not in self.radio_name_depth:
            self.radio_name_depth[name] = self.details_depth


@pytest.fixture
def depths(page_html: str) -> DepthRecorder:
    recorder = DepthRecorder()
    recorder.feed(page_html)
    return recorder


def test_landing_keeps_photo_star_and_about_up_front(
    page_html: str, depths: DepthRecorder
) -> None:
    assert re.search(r'<button[^>]*id="make-portrait"[^>]*class="[^"]*primary', page_html)
    # Photo, star and About all sit up front, in the reading order.
    for element_id in (
        "photo",
        "child-name",
        "photo_gender",
        "about-name",
        "about-age",
        "about-title",
        "details",
    ):
        assert depths.depth_by_id[element_id] == 0, f"{element_id} must not hide in Advanced"
    # Everything else — every art knob, the alternatives count — folds away.
    for element_id in (
        "cartoon_intensity",
        "cartoon_style",
        "likeness_strength",
        "aspect_ratio",
        "portrait_background",
        "candidates",
        "apparent_age",
        "skin_tone",
        "hair_style",
        "hair_color",
        "outfit",
        "expression",
        "keep_glasses",
        "keep_facial_hair",
        "one_step",
    ):
        assert depths.depth_by_id[element_id] == 1, f"{element_id} must live inside Advanced"
    advanced = re.search(r'<details[^>]*id="book-advanced"[^>]*>', page_html).group(0)
    assert "open" not in advanced, "Advanced must start collapsed"


def test_no_setting_was_dropped_from_the_page(page_html: str) -> None:
    # The book is the person in the photo, so it offers no separate
    # `character_gender` — photo_gender covers it everywhere.
    fields = set(CartoonSettings.model_fields) - {"character_gender"}
    for name in fields | {"photo", "one_step"}:
        assert f'name="{name}"' in page_html, f"the {name} control went missing"


def test_the_landing_defaults_match_the_server(page_html: str) -> None:
    defaults = CartoonSettings()
    # Zero changes should send exactly the server's defaults, so the rendered
    # page must preselect them.
    assert f'<option value="{defaults.cartoon_intensity}" selected' in page_html
    for select_name, value in (
        ("cartoon_style", defaults.cartoon_style),
        ("likeness_strength", defaults.likeness_strength),
        ("aspect_ratio", defaults.aspect_ratio),
        ("portrait_background", defaults.portrait_background),
        ("apparent_age", defaults.apparent_age),
        ("photo_gender", defaults.photo_gender),
        ("skin_tone", defaults.skin_tone),
        ("hair_style", defaults.hair_style),
        ("hair_color", defaults.hair_color),
        ("outfit", defaults.outfit),
        ("expression", defaults.expression),
    ):
        option = re.search(
            rf'name="{select_name}".*?<option value="{re.escape(value)}" selected',
            page_html,
            flags=re.DOTALL,
        )
        assert option is not None, f"{select_name} does not default to {value}"
    assert re.search(r'name="keep_glasses"[^>]*checked', page_html)
    assert re.search(r'name="keep_facial_hair"[^>]*checked', page_html)


def test_the_about_fields_show_no_hints(page_html: str) -> None:
    section = re.search(r"About</legend>(.*?)</fieldset>", page_html, flags=re.DOTALL).group(1)
    assert (
        "field-help" not in section.split('id="details"')[0].split('id="about-title"')[-1]
    ), "Title gets no hint text"


def test_no_scene_picker_on_the_landing(page_html: str) -> None:
    assert 'name="scene"' not in page_html
    assert 'href="/book"' not in page_html, "no self-link to the old book route"


# --- camera and downscale surfaces --------------------------------------------


def test_the_photo_fieldset_offers_a_camera(page_html: str) -> None:
    for element_id in (
        "use-camera",
        "camera-pane",
        "camera-video",
        "camera-shutter",
        "camera-cancel",
    ):
        assert f'id="{element_id}"' in page_html
    # The in-page pane and its status line start hidden, so first paint is quiet.
    assert re.search(r'id="camera-pane"[^>]*class="[^"]*hidden', page_html)
    assert re.search(r'id="camera-message"[^>]*class="[^"]*hidden', page_html)


def test_every_character_slot_offers_the_same_camera(page_html: str) -> None:
    """The extra cast photos get the in-page camera too, not just the star."""
    for key, _label in photoable_cast():
        assert f'id="use-camera-{key}"' in page_html
        assert f'id="character-preview-{key}"' in page_html
        assert f'id="character-message-{key}"' in page_html
    for element_id in (
        "character-camera-pane",
        "character-camera-video",
        "character-camera-shutter",
        "character-camera-cancel",
        "character-camera-message",
    ):
        assert f'id="{element_id}"' in page_html


def test_every_character_slot_makes_its_own_cartoon(page_html: str) -> None:
    """UAT: each extra cast photo gets a cartoon before the pages are drawn.

    The button and its result preview live in the slot, so a face can be judged
    (and remade) without leaving the page.
    """
    for key, _label in photoable_cast():
        assert f'id="make-character-cartoon-{key}"' in page_html
        assert f'id="character-cartoon-wrap-{key}"' in page_html
        assert f'id="character-cartoon-{key}"' in page_html
    # Every slot's button starts disabled: there is no photo to draw yet.
    assert re.search(r'id="make-character-cartoon-mother"[^>]*\bdisabled\b', page_html)


def test_photo_input_no_longer_blocks_camera_only_submission(page_html: str) -> None:
    file_input = re.search(r'<input type="file"[^>]*>', page_html).group(0)
    assert "required" not in file_input


def test_size_limit_is_a_data_attribute_backstop(page_html: str) -> None:
    assert re.search(r'<body[^>]*data-max-upload-mb="\d+"[^>]*>', page_html)
    help_text = re.search(
        r"<p class=\"field-help\">[^<]*shrink it for you[^<]*</p>", page_html
    ).group(0)
    assert "MB" not in help_text, "the help text must not parade the old upload cap"


# --- the review and book stages ------------------------------------------------


def test_review_offers_approve_or_redo(page_html: str) -> None:
    for element_id in (
        "review-stage",
        "review-portrait",
        "approve-portrait",
        "reject-portrait",
    ):
        assert f'id="{element_id}"' in page_html
    assert re.search(r'id="review-stage"[^>]*class="[^"]*hidden', page_html)


def test_review_offers_feedback_chips_on_the_cartoon(page_html: str) -> None:
    """UAT 3: report feedback to the model — the closed-set chip flow.

    The chips are rendered server-side from the rejection vocabulary, hidden
    behind the "Not right" path. There is no free-text input to the model on
    this page at all: the only way to send feedback is a fixed chip.
    """
    for element_id in ("reject-body", "reject-chips", "reject-retry"):
        assert f'id="{element_id}"' in page_html
    assert re.search(r'id="reject-body"[^>]*class="[^"]*hidden', page_html)
    # The full closed set is offered, and every chip posts `reason`:
    assert page_html.count('name="reason"') >= len(RejectionReason)
    assert re.search(r'id="reject-retry"[^>]*>Try again with these fixes', page_html)
    assert 'aria-controls="reject-body"' in page_html, "the toggle wires the panel"


def test_book_stage_has_progress_viewer_and_pdf_link(page_html: str) -> None:
    """The finished book is the PDF in one big box, plus a download link.

    The book stage carries a progress line, a progress bar, an optional
    per-page grid, the PDF viewport, and the download/secondary controls.
    (UAT 8 retired the static *four-little-pictures catalogue*; Step 5
    re-lands a grid whose only job is showing the running job's per-page
    state — pending, making, done — so the code id `book-pages` stays
    retired and the new grid is `page-grid`.)
    """
    for element_id in (
        "book-stage",
        "book-progress",
        "book-progress-track",
        "book-progress-bar",
        "page-grid",
        "book-pdf-panel",
        "book-pdf-frame",
        "book-pdf-link",
        "book-restart",
    ):
        assert f'id="{element_id}"' in page_html
    assert "book-pages" not in page_html, "the four-little-pictures grid is retired"
    assert re.search(r'id="book-stage"[^>]*class="[^"]*hidden', page_html)
    assert re.search(r'id="book-pdf-panel"[^>]*class="[^"]*hidden', page_html)
    assert re.search(r'id="book-pdf-link"[^>]*class="[^"]*hidden', page_html)


def test_prompts_are_inspectable(page_html: str) -> None:
    """The wording sent to the model is visible to the visitor."""
    for element_id in ("prompt-panel", "prompt-cartoon", "prompt-json-link"):
        assert f'id="{element_id}"' in page_html
    assert "Show the prompts that were sent" in page_html


def test_no_step_numbers_anywhere(page_html: str) -> None:
    assert not re.search(r"Step \d", page_html), "step numbering is gone"


# --- nothing technical on the visitor's screen ---------------------------------


def test_the_page_shows_no_provider_internals(page_html: str) -> None:
    """Which model, which quality, which provider — the operator's business.

    The details stay in the server log for the operator; the page carries at
    most a plain note that the pictures are placeholders.
    """
    for jargon in ("Provider:", "provider-badge", "quality=", "model=", "mock mode"):
        assert jargon not in page_html, f"the page leaks {jargon!r}"
    assert "Demo mode" in page_html, "a placeholder run should say so plainly"


def test_the_page_offers_no_backend_choice(page_html: str) -> None:
    """No control for the provider or the model — not even a hidden one.

    Which backend a deployment runs on is an environment variable, so a
    visitor cannot switch it from the browser.
    """
    for field in ("provider", "model", "image_provider", "openai_model", "gemini_model"):
        assert f'name="{field}"' not in page_html, f"the page offers a {field!r} control"
        assert f'"{field}"' not in page_html, f"the page mentions {field!r}"


def test_the_options_endpoint_offers_no_backend_choice(client) -> None:
    """The same answer for the machine-readable list of choices."""
    payload = client.get("/api/settings-options").json()
    for key in ("provider", "providers", "model", "models", "image_provider"):
        assert key not in payload, f"the options endpoint offers {key!r}"


def test_the_open_health_endpoint_names_neither_provider_nor_model(client) -> None:
    """`/healthz` is open to anyone, so it must not identify the backend."""
    payload = client.get("/healthz").json()
    provider_payload = payload["provider"]

    assert "name" not in provider_payload
    assert "reason" not in provider_payload
    for leak in ("recording", "mock", "openai", "gemini"):
        assert leak not in json.dumps(payload).lower()


def test_the_generation_response_does_not_name_the_backend(client, photo: bytes) -> None:
    """The browser's network tab is still a visitor's screen."""
    response = client.post(
        "/api/cartoonify",
        files={"photo": ("me.png", photo, "image/png")},
        data={"aspect_ratio": "4:3"},
    )
    assert response.status_code == 201

    payload = response.json()
    assert "provider" not in payload
    assert "model" not in payload


def test_the_page_shows_no_billing_jargon(page_html: str) -> None:
    """'One image call' is how the code talks, not how a visitor thinks."""
    for jargon in ("image call", "per-minute allowance", "cost-note"):
        assert jargon not in page_html, f"the page leaks {jargon!r}"


def test_the_intensity_dial_speaks_plain_english(page_html: str) -> None:
    """The dial shows what each step means, not a bare number."""
    options = re.findall(
        r'<option value="(\d+)"[^>]*>([^<]+)</option>',
        re.search(r'<select id="cartoon_intensity".*?</select>', page_html, re.S).group(0),
    )
    assert len(options) == 10
    for value, label in options:
        assert label.strip(), f"step {value} has no label"
    assert ("1", "Almost a photo") in options
    assert ("8", "Very strongly a cartoon") in options
    assert ("10", "Fully graphic") in options


def test_static_files_are_revalidated_so_a_deploy_is_never_stale(client: TestClient) -> None:
    response = client.get("/static/book-detail.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert "etag" in response.headers


def test_book_detail_picks_up_progress_on_load() -> None:
    """A page opened mid-render polls on its own and reports at the top."""
    root = Path(__file__).resolve().parents[1] / "app"
    template = (root / "templates" / "book_detail.html").read_text()
    script = (root / "static" / "book-detail.js").read_text()
    assert 'data-book-status="{{ status.status }}"' in template
    assert "document.body.dataset.bookStatus" in script
    assert '"book-status-line"' in script
