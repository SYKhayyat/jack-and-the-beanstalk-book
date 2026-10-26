"""Structural contract for the book's front door.

The UI (camera capture, client-side downscale, options behind "Advanced",
ES-module split) is mostly front-end work that pytest cannot click. What
pytest *can* police — what the plan called its grep-able claims — is the layer
split and the landing-page shape. These tests do that, so the split cannot
silently drift back.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app.models import CartoonSettings, RejectionReason

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


def test_photo_input_no_longer_blocks_camera_only_submission(page_html: str) -> None:
    file_input = re.search(r'<input type="file"[^>]*>', page_html).group(0)
    assert "required" not in file_input


def test_size_limit_is_a_data_attribute_backstop(page_html: str) -> None:
    assert re.search(r'<body data-max-upload-mb="\d+"[^>]*>', page_html)
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

    The little page grid is gone (UAT 8): the stage carries a progress line,
    the PDF viewport, and the download/secondary controls.
    """
    for element_id in (
        "book-stage",
        "book-progress",
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

    The details stay in /healthz for the operator; the page carries at most a
    plain note that the pictures are placeholders.
    """
    for jargon in ("Provider:", "provider-badge", "quality=", "model=", "mock mode"):
        assert jargon not in page_html, f"the page leaks {jargon!r}"
    assert "Demo mode" in page_html, "a placeholder run should say so plainly"


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
