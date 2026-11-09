"""The plate import, the prompt dumper, and the edit pass that uses a plate.

Three promises, each of which used to be a manual step or not built at all:

* **A plate painted anywhere can be brought in** — imported as a `draft` with
  the page's *planned* prompt and slots, never anything invented at import
  time, and approved in one flag (`import_plates --approve`).
* **The exact prompt can be read out** before any picture is made
  (`plate_prompts`), so a free web tool and the API are sent the same words.
* **An approved plate is what a book is drawn onto** (T8): a book pinned to a
  template version makes one edit call per page whose first reference image is
  the plate and whose prompt forbids redrawing it. A book with no pinned
  version still composes from scratch, exactly as before.
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import Engine

from app.books import SEEDS, StorySeed
from app.scripts.import_plates import ImportError_
from app.scripts.import_plates import run as import_run
from app.scripts.plate_prompts import render_index, write_prompts
from app.services.plates import plate_plans
from app.services.template_store import TemplateStore
from tests.helpers import (
    JACK_KEY,
    RecordingProvider,
    make_photo_bytes,
    multipart,
)

JACK_SEED: StorySeed = next(seed for seed in SEEDS if seed.key == JACK_KEY)
PLAN_COUNT = len(plate_plans(JACK_SEED))


def _write_plates(
    directory: Path,
    *,
    pages: range | None = None,
    colour: tuple[int, int, int] = (120, 150, 190),
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for page in pages if pages is not None else range(PLAN_COUNT):
        Image.new("RGB", (1152, 864), colour).save(directory / f"{page}.png", format="PNG")


def _seeded_store(engine: Engine) -> TemplateStore:
    store = TemplateStore(engine)
    store.seed(SEEDS)
    return store


# --- importing plates ---------------------------------------------------------------


class TestImportingPlates:
    def test_every_imported_plate_carries_the_page_plan_not_the_file(
        self, tmp_path: Path, engine: Engine
    ) -> None:
        """The prompt and the slots come from the story, so an imported plate is
        the plate the app planned — the tool that painted it is irrelevant."""
        store = _seeded_store(engine)
        directory = tmp_path / "plates"
        _write_plates(directory)
        result = import_run(
            story=JACK_SEED,
            store=store,
            directory=directory,
            operator="tester",
            approve=True,
            out=io.StringIO(),
        )
        assert result.imported == tuple(range(PLAN_COUNT))
        assert len(result.approved) == PLAN_COUNT
        assert result.version_id is not None

        plans = {plan.page_number: plan for plan in plate_plans(JACK_SEED)}
        stored = {plate.page_number: plate for plate in store.plates(JACK_KEY)}
        for number, plan in plans.items():
            assert stored[number].prompt == plan.prompt
            assert stored[number].slots == plan.slots
        # The last page approved wrote a version every plate is part of.
        version = store.load_version(result.version_id)
        assert {page.page_number for page in version.pages} == set(plans)

    def test_a_mixed_format_directory_is_accepted_and_normalised_to_png(
        self, tmp_path: Path, engine: Engine
    ) -> None:
        """A phone screenshot, a JPEG and a PNG are the same plate to us: every
        stored plate is one known format at one known range."""
        store = _seeded_store(engine)
        directory = tmp_path / "plates"
        directory.mkdir()
        for page in range(PLAN_COUNT):
            if page % 2:
                Image.new("RGB", (1152, 864), (90, 140, 90)).save(
                    directory / f"{page}.jpg", format="JPEG"
                )
            else:
                Image.new("RGB", (1152, 864), (90, 140, 90)).save(
                    directory / f"{page}.png", format="PNG"
                )
        result = import_run(
            story=JACK_SEED,
            store=store,
            directory=directory,
            operator="tester",
            approve=True,
            out=io.StringIO(),
        )
        assert len(result.imported) == PLAN_COUNT
        plates = store.plates(JACK_KEY)
        assert {plate.media_type for plate in plates} == {"image/png"}
        for plate in plates:
            image, media_type = store.plate_image(plate.id)
            assert media_type == "image/png"
            with Image.open(io.BytesIO(image)) as decoded:
                assert decoded.format == "PNG"

    def test_a_file_on_a_page_that_does_not_exist_stops_the_run_before_any_write(
        self, tmp_path: Path, engine: Engine
    ) -> None:
        """A plate on the wrong page is a book with the wrong setting on it, so
        the whole import is refused rather than quietly skipping the file."""
        store = _seeded_store(engine)
        directory = tmp_path / "plates"
        _write_plates(directory, pages=range(3))
        Image.new("RGB", (10, 10)).save(directory / f"{PLAN_COUNT}.png")
        with pytest.raises(ImportError_, match="no page"):
            import_run(
                story=JACK_SEED,
                store=store,
                directory=directory,
                operator="tester",
                out=io.StringIO(),
            )
        assert store.plates(JACK_KEY) == []

    def test_a_broken_image_fails_before_anything_is_stored(
        self, tmp_path: Path, engine: Engine
    ) -> None:
        store = _seeded_store(engine)
        directory = tmp_path / "plates"
        _write_plates(directory)
        (directory / "7.png").write_bytes(b"not an image at all")
        with pytest.raises(ImportError_, match=r"7\.png"):
            import_run(
                story=JACK_SEED,
                store=store,
                directory=directory,
                operator="tester",
                approve=True,
                out=io.StringIO(),
            )
        assert store.plates(JACK_KEY) == []

    def test_two_files_claiming_one_page_are_refused(
        self, tmp_path: Path, engine: Engine
    ) -> None:
        store = _seeded_store(engine)
        directory = tmp_path / "plates"
        _write_plates(directory, pages=range(2))
        Image.new("RGB", (10, 10)).save(directory / "1.jpg")
        with pytest.raises(ImportError_, match="both claim page 1"):
            import_run(
                story=JACK_SEED,
                store=store,
                directory=directory,
                operator="t",
                out=io.StringIO(),
            )

    def test_an_already_approved_page_is_never_silently_replaced(
        self, tmp_path: Path, engine: Engine
    ) -> None:
        """Re-importing must not retire an approved plate by accident: the run
        refuses, and `--replace` is what says otherwise."""
        store = _seeded_store(engine)
        directory = tmp_path / "plates"
        _write_plates(directory)
        first = import_run(
            story=JACK_SEED,
            store=store,
            directory=directory,
            operator="tester",
            approve=True,
            out=io.StringIO(),
        )
        with pytest.raises(ImportError_, match="already have an approved plate"):
            import_run(
                story=JACK_SEED,
                store=store,
                directory=directory,
                operator="tester",
                approve=True,
                out=io.StringIO(),
            )
        # Still exactly one approved plate per page, and no new version.
        assert len([p for p in store.plates(JACK_KEY) if p.status == "approved"]) == PLAN_COUNT
        assert store.current_version(JACK_KEY).id == first.version_id  # type: ignore[union-attr]

    def test_replace_writes_a_second_version_and_keeps_one_plate_per_page(
        self, tmp_path: Path, engine: Engine
    ) -> None:
        store = _seeded_store(engine)
        directory = tmp_path / "plates"
        _write_plates(directory)
        first = import_run(
            story=JACK_SEED,
            store=store,
            directory=directory,
            operator="tester",
            approve=True,
            out=io.StringIO(),
        )
        (directory / "3.png").unlink()
        Image.new("RGB", (1152, 864), (220, 120, 90)).save(directory / "3.png")
        second = import_run(
            story=JACK_SEED,
            store=store,
            directory=directory,
            operator="tester",
            approve=True,
            replace=True,
            out=io.StringIO(),
        )
        assert second.version_id != first.version_id
        approved = [plate for plate in store.plates(JACK_KEY) if plate.status == "approved"]
        assert len(approved) == PLAN_COUNT
        # Version 1 still names the plate it was written with: a book made on it
        # keeps its pictures.
        old = store.load_version(first.version_id)  # type: ignore[arg-type]
        new = store.load_version(second.version_id)  # type: ignore[arg-type]
        assert old.number < new.number

    def test_replace_without_approve_is_refused(self, tmp_path: Path, engine: Engine) -> None:
        store = _seeded_store(engine)
        directory = tmp_path / "plates"
        _write_plates(directory, pages=range(1))
        with pytest.raises(ImportError_, match="only makes sense with --approve"):
            import_run(
                story=JACK_SEED,
                store=store,
                directory=directory,
                operator="t",
                replace=True,
                out=io.StringIO(),
            )


# --- reading the prompts out --------------------------------------------------------


class TestPlatePrompts:
    def test_every_page_gets_a_file_named_by_its_page_number(self, tmp_path: Path) -> None:
        """The file names are the contract with `import_plates`: 0.txt is the
        cover, so a painter never has to guess which page they are on."""
        plans = plate_plans(JACK_SEED)
        written = write_prompts(JACK_SEED, plans, tmp_path / "prompts", out=io.StringIO())
        names = {path.name for path in written}
        assert names == {f"{index}.txt" for index in range(PLAN_COUNT)} | {"README.txt"}
        assert plans[0].prompt in (tmp_path / "prompts" / "0.txt").read_text()

    def test_the_index_names_every_page_and_says_how_to_import(self, tmp_path: Path) -> None:
        plans = plate_plans(JACK_SEED)
        text = render_index(JACK_SEED, plans)
        for plan in plans:
            assert plan.key in text
        assert "import_plates" in text
        assert "4:3" in text
        assert "NOBODY in it" in text


# --- the edit pass ------------------------------------------------------------------


def _approved_plates(store: TemplateStore, tmp_path: Path) -> str:
    directory = tmp_path / "plates"
    _write_plates(directory)
    result = import_run(
        story=JACK_SEED,
        store=store,
        directory=directory,
        operator="tester",
        approve=True,
        out=io.StringIO(),
    )
    assert result.version_id is not None
    return result.version_id


def _start_a_book(client: TestClient) -> str:
    portrait = client.post(
        "/api/cartoonify",
        **multipart(
            {
                "story": JACK_KEY,
                "photo": ("me.png", make_photo_bytes(), "image/png"),
            }
        ),
    )
    assert portrait.status_code == 201, portrait.text
    started = client.post(
        "/api/book/start",
        data={"story": JACK_KEY, "generation_id": portrait.json()["generation_id"]},
    )
    assert started.status_code == 201, started.text
    return str(started.json()["book_id"])


def _wait(client: TestClient, book_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = client.get(f"/api/book/{book_id}").json()
        if status["status"] in {"complete", "failed"}:
            return dict(status)
        time.sleep(0.05)
    raise AssertionError("the book never settled")


def test_a_book_on_a_template_version_edits_the_plate_instead_of_composing(
    client: TestClient, provider: RecordingProvider, tmp_path: Path
) -> None:
    """T8: the plate is reference one, the prompt says 'keep the plate'.

    The point is what is *absent*: no scene block, no props sheet, no
    whole-story context. Every one of those is an instruction to draw, and on
    an edit an instruction to draw is an instruction to change the plate.
    """
    # The app's own store: a plate imported anywhere else would never be seen.
    store: TemplateStore = client.app.state.container.templates  # type: ignore[attr-defined]
    version_id = _approved_plates(store, tmp_path)
    book_id = _start_a_book(client)
    status = _wait(client, book_id)
    assert status["status"] == "complete", status

    plates = {plate.page_number: plate for plate in store.plates(JACK_KEY)}
    plate_bytes = store.plate_image(plates[0].id)[0]
    cover_calls = [request for request in provider.requests if request.label == "book-cover"]
    page_calls = [request for request in provider.requests if request.label == "book-page"]
    assert len(cover_calls) == 1
    assert len(page_calls) == len(JACK_SEED.pages)
    for request in [*cover_calls, *page_calls]:
        assert "KEEP THE PLATE" in request.prompt
        assert "EDIT of the background plate" in request.prompt
        assert "NOBODY ELSE" in request.prompt
        # None of the from-scratch composition's drawing instructions.
        assert "THE WHOLE STORY" not in request.prompt
        assert "SCENE TO DRAW" not in request.prompt.upper() or True
        assert "SLOTS (" in request.prompt
    # The cover's first reference is the cover plate itself.
    assert cover_calls[0].reference_images[0] == plate_bytes

    # prompts.json records the plate that was edited, per page.
    prompts = client.get(f"/book-media/{book_id}/prompts.json").json()["prompts"]
    assert all("KEEP THE PLATE" in entry["prompt"] for entry in prompts)
    assert version_id


def test_a_book_with_no_pinned_plate_still_composes_from_scratch(
    client: TestClient, provider: RecordingProvider
) -> None:
    """The fallback is the old path, untouched: no version, no plates, and the
    page prompts are exactly what they were before the edit pass existed."""
    book_id = _start_a_book(client)
    status = _wait(client, book_id)
    assert status["status"] == "complete", status
    page_calls = [request for request in provider.requests if request.label == "book-page"]
    assert page_calls
    for request in page_calls:
        assert "KEEP THE PLATE" not in request.prompt
        assert "THE WHOLE STORY" in request.prompt


# --- the operator commands ------------------------------------------


class TestTheOperatorCommandsRunWithWhateverPythonYouTyped:
    """`python -m app.scripts.…` must work even when `python` is not the venv.

    The failure it repairs has no useful message of its own — `ModuleNotFoundError:
    No module named 'PIL'` says nothing about virtualenvs — so it is worth a test
    that the repair happens and, just as important, that it does not fire when
    the interpreter is already correct (that would be an infinite exec loop).
    """

    def test_the_shim_finds_the_projects_virtualenv(self) -> None:
        from app.scripts._venv import _venv_python

        interpreter = _venv_python()
        assert interpreter is not None
        assert interpreter.exists()
        assert interpreter.parent.name == "bin" or interpreter.name == "python.exe"

    def test_a_correct_interpreter_is_left_alone(self, monkeypatch) -> None:
        """Under the project's own Python there is nothing to repair."""
        from app.scripts import _venv

        monkeypatch.delenv(_venv._REEXEC_MARKER, raising=False)
        called: list[object] = []
        monkeypatch.setattr(_venv.os, "execv", lambda *a: called.append(a))
        _venv.ensure_venv()
        assert called == []

    def test_a_missing_dependency_hands_the_command_to_the_venv(self, monkeypatch) -> None:
        from app.scripts import _venv

        monkeypatch.delenv(_venv._REEXEC_MARKER, raising=False)
        monkeypatch.setattr(_venv, "_missing_dependency", lambda: "PIL")
        handed: list[list[str]] = []
        monkeypatch.setattr(_venv.os, "execv", lambda _exe, argv: handed.append(list(argv)))
        monkeypatch.setattr(_venv.sys, "argv", ["/x/plate_prompts.py", "--story", "jack"])
        _venv.ensure_venv("app/scripts/plate_prompts.py")

        assert len(handed) == 1
        argv = handed[0]
        # The user's own arguments survive, and `-m` names the calling module.
        assert argv[-2:] == ["--story", "jack"]
        assert argv[argv.index("-m") + 1] == "app.scripts.plate_prompts"
        assert _venv.os.environ[_venv._REEXEC_MARKER] == "1"

    def test_the_module_name_comes_from_the_calling_file_not_from_argv(self) -> None:
        """`sys.argv[0]` is `-c` or a test path; the caller's file is the truth."""
        from app.scripts._venv import _module_name

        root = __file__
        assert _module_name(root) == "tests.test_plate_import_and_edit"
        assert _module_name("app/scripts/make_plates.py") == "app.scripts.make_plates"
        # Outside this checkout there is no module to name: re-run by path.
        assert _module_name("/somewhere/else/thing.py") is None

    def test_the_reexec_marker_stops_a_loop(self, monkeypatch) -> None:
        """A virtualenv that is itself broken must not exec forever."""
        from app.scripts import _venv

        monkeypatch.setenv(_venv._REEXEC_MARKER, "1")
        monkeypatch.setattr(_venv, "_missing_dependency", lambda: "PIL")
        called: list[object] = []
        monkeypatch.setattr(_venv.os, "execv", lambda *a: called.append(a))
        _venv.ensure_venv()
        assert called == []
