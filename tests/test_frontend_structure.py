"""Structural contract for "the simpler front door".

The UI refactor (camera capture, client-side downscale, options behind
"Advanced", ES-module split) is mostly front-end work that pytest cannot click.
What pytest *can* police — and what the plan called its grep-able claims — is
the layer split and the landing-page shape. These tests do that, so the split
cannot silently drift back.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app.models import CartoonSettings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC = PROJECT_ROOT / "app" / "static"

# The module layout: one concern per file, exactly as planned.
MODULES = ("state.js", "api.js", "view.js", "photo.js", "downscale.js", "app.js")

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
    return set(re.findall(r'byId\("([^"]+)"\)', _read("view.js")))


# --- the module split --------------------------------------------------------


def test_every_module_file_exists() -> None:
    for name in MODULES:
        assert (STATIC / name).is_file(), f"{name} is missing"


@pytest.mark.parametrize(
    ("module", "forbidden"),
    [("state.js", r"document\."), ("api.js", r"document\."), ("app.js", DOM_TOKENS)],
)
def test_no_dom_access_outside_the_view(module: str, forbidden: str) -> None:
    assert not re.search(forbidden, _read(module)), f"{module} touches the DOM"


def test_dom_access_outside_the_view_is_only_the_documented_exceptions() -> None:
    for name, source in ((name, _read(name)) for name in MODULES if name != "view.js"):
        assert not _forbidden_dom_hits(name, source)


def test_fetch_is_called_only_from_the_api_client() -> None:
    for name in MODULES:
        uses = len(re.findall(r"\bfetch\(", _read(name)))
        if name == "api.js":
            assert uses >= 1
        else:
            assert uses == 0, f"{name} should not fetch"


def test_app_js_is_a_thin_controller() -> None:
    source = _read("app.js")
    assert "renderPhotoPreview" not in source.split("import")[1], (
        "the controller must call view functions, not build DOM"
    )
    assert "createCameraSource" in source, "camera is swapped in via the controller"


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


def test_index_loads_exactly_one_module_script(page_html: str) -> None:
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", page_html, flags=re.DOTALL)
    assert len(scripts) == 1, "the page must carry its behaviour in exactly one script tag"
    assert 'type="module" src="/static/app.js"' in page_html
    assert not scripts[0].strip(), "no JavaScript may be inlined in the template"


def test_every_element_the_view_reads_exists_on_the_page(page_html: str) -> None:
    rendered_ids = set(re.findall(r'id="([^"]+)"', page_html))
    missing = _view_ids() - rendered_ids
    assert not missing, f"view.js reads ids the template does not render: {missing}"


# --- the landing page shows almost nothing -----------------------------------


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


def test_landing_keeps_only_photo_scene_and_the_button_up_front(
    page_html: str, depths: DepthRecorder
) -> None:
    assert depths.depth_by_id["photo"] == 0
    assert depths.radio_name_depth["scene"] == 0
    assert re.search(r'<button[^>]*id="submit"[^>]*class="[^"]*primary', page_html)
    # Everything else — all settings, all character details, the one-step box —
    # folds away into one collapsed disclosure.
    for element_id in (
        "cartoon_intensity",
        "cartoon_style",
        "likeness_strength",
        "aspect_ratio",
        "portrait_background",
        "candidates",
        "apparent_age",
        "character_gender",
        "photo_gender",
        "skin_tone",
        "hair_style",
        "hair_color",
        "outfit",
        "expression",
        "one_step",
    ):
        assert depths.depth_by_id[element_id] == 1, f"{element_id} must live inside Advanced"
    advanced = re.search(r'<details[^>]*id="advanced-options"[^>]*>', page_html).group(0)
    assert "open" not in advanced, "Advanced must start collapsed"


def test_no_setting_was_dropped_from_the_page(page_html: str) -> None:
    for name in set(CartoonSettings.model_fields) | {"photo", "scene", "one_step"}:
        assert f'name="{name}"' in page_html, f"the {name} control went missing"


def test_the_landing_defaults_match_the_server(page_html: str) -> None:
    defaults = CartoonSettings()
    # Zero changes should send exactly the server's defaults, so the rendered
    # page must preselect them.
    assert f'value="{defaults.cartoon_intensity}"' in page_html
    assert (
        f'<output id="intensity-output" for="cartoon_intensity">'
        f"{defaults.cartoon_intensity}</output>"
    ) in page_html
    for select_name, value in (
        ("cartoon_style", defaults.cartoon_style),
        ("likeness_strength", defaults.likeness_strength),
        ("aspect_ratio", defaults.aspect_ratio),
        ("portrait_background", defaults.portrait_background),
        ("apparent_age", defaults.apparent_age),
        ("character_gender", defaults.character_gender),
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
    assert 'name="keep_glasses" value="true" checked' in page_html
    assert 'name="keep_facial_hair" value="true" checked' in page_html


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
    assert re.search(r'<body data-max-upload-mb="\d+">', page_html)
    help_text = re.search(
        r"<p class=\"field-help\">[^<]*shrink it for you[^<]*</p>", page_html
    ).group(0)
    assert "MB" not in help_text, "the help text must not parade the old upload cap"


# --- the review stage ---------------------------------------------------------


def test_review_offers_three_choices_with_chips_hidden_behind_not_right(
    page_html: str,
) -> None:
    for element_id in ("approve-cartoon", "reject-toggle", "start-over-review"):
        assert f'id="{element_id}"' in page_html
    assert re.search(r'id="reject-body"[^>]*class="[^"]*hidden', page_html), (
        "the reason chips stay collapsed until Not right is pressed"
    )
    assert re.search(r'id="reject-toggle"[^>]*aria-expanded="false"', page_html)


def test_both_start_over_buttons_are_wired_the_same_way(page_html: str) -> None:
    assert len(re.findall(r"data-start-over", page_html)) == 2, (
        "start over must sit in both the review stage and the result stage"
    )
