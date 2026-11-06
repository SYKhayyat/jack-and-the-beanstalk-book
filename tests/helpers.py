"""Plain test helpers, kept out of conftest so tests can import them normally."""

from __future__ import annotations

import io
import os
from functools import lru_cache

from PIL import Image
from sqlalchemy.engine import Engine

from app.books import SEEDS
from app.models.story import StoryEdition
from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
)
from app.services.template_store import TemplateStore, seed_edition


def make_photo_bytes(size: tuple[int, int] = (640, 480), colour: str = "#3f6f8f") -> bytes:
    """A tiny but genuine PNG, standing in for a user's upload."""
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def make_noisy_photo_bytes(size: tuple[int, int] = (1200, 1200)) -> bytes:
    """A large PNG that resists compression, for exercising upload limits.

    A flat-colour image compresses to a few kilobytes, so repeating one cannot
    stand in for a file that genuinely exceeds a size cap.
    """
    image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def tiny_png_bytes(size: tuple[int, int] = (8, 8), colour: str = "#2f7d4f") -> bytes:
    """The smallest valid PNG, used as canned provider output."""
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


class RecordingProvider(ImageProvider):
    """Records every request so tests can assert on prompts and references."""

    name = "recording"

    @property
    def draws_placeholders(self) -> bool:
        return True

    def __init__(self, image: bytes | None = None) -> None:
        self.requests: list[ImageRequest] = []
        self._image = image or tiny_png_bytes()

    def generate(self, request: ImageRequest) -> GeneratedImage:
        self.requests.append(request)
        return GeneratedImage(data=self._image, media_type="image/png", model="recording-v1")


class StubProvider(ImageProvider):
    """Returns a fixed, possibly degenerate, result. For failure-path tests."""

    name = "stub"

    @property
    def draws_placeholders(self) -> bool:
        return True

    def __init__(self, data: bytes, model: str = "stub-v1") -> None:
        self._data = data
        self._model = model
        self.calls = 0

    def generate(self, request: ImageRequest) -> GeneratedImage:
        self.calls += 1
        return GeneratedImage(data=self._data, media_type="image/png", model=self._model)


def multipart(form: dict[str, object]) -> dict[str, dict[str, object]]:
    """Split a form spec into httpx's `data` and `files` keyword arguments.

    A tuple value means "a file"; everything else is a plain form field. Without
    this split httpx would send every value as a file part, and FastAPI would
    reject the scalar fields.
    """
    files = {key: value for key, value in form.items() if isinstance(value, tuple)}
    data = {key: value for key, value in form.items() if not isinstance(value, tuple)}
    return {"files": files, "data": data}


# --- stories ---------------------------------------------------------------------

JACK_KEY = "jack_and_the_beanstalk"
RED_KEY = "little_red_riding_hood"


def edition_for(story_key: str) -> StoryEdition:
    """A story's edition straight from its seed: what a new book would hold.

    Built by the same constructor the database path uses, so a prompt or page
    test sees exactly the words and looks a book of that story is made from.
    """
    return _seed_editions()[story_key]


def jack() -> StoryEdition:
    return edition_for(JACK_KEY)


def red() -> StoryEdition:
    return edition_for(RED_KEY)


@lru_cache(maxsize=1)
def _seed_editions() -> dict[str, StoryEdition]:
    return {seed.key: seed_edition(seed) for seed in SEEDS}


def stored_edition(engine: Engine, story_key: str = JACK_KEY) -> StoryEdition:
    """The edition a new book would pin, read from a seeded database."""
    store = TemplateStore(engine)
    store.seed(SEEDS)
    return store.edition_for_new_book(story_key)


#: Keyed by database URL, never by `id(engine)`: every test has its own file,
#: and an id is reused as soon as an old engine is garbage-collected.
_STORED: dict[str, dict[str, StoryEdition]] = {}


def edition_in(store: object, story_key: str = JACK_KEY) -> StoryEdition:
    """The stored edition a book saved through `store` pins (seeding once per database)."""
    engine: Engine = store._engine  # type: ignore[attr-defined]
    per_engine = _STORED.setdefault(engine.url.render_as_string(hide_password=False), {})
    if story_key not in per_engine:
        per_engine[story_key] = stored_edition(engine, story_key)
    return per_engine[story_key]
