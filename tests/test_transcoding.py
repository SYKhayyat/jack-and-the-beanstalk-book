"""The process-wide ceiling on picture conversions (app.transcoding)."""

from __future__ import annotations

import io
import threading
import time
from typing import Any

import pytest
from PIL import Image

from app import blobs, transcoding
from app.services import pictures


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.effect_noise((400, 300), 30).convert("RGB").save(buffer, "PNG")
    return buffer.getvalue()


def test_no_more_than_the_slots_run_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    running = 0
    most = 0
    lock = threading.Lock()
    real_save = Image.Image.save

    def slow_save(self: Image.Image, *args: Any, **kwargs: Any) -> None:
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
        time.sleep(0.05)
        try:
            return real_save(self, *args, **kwargs)
        finally:
            with lock:
                running -= 1

    data = _png()
    monkeypatch.setattr(Image.Image, "save", slow_save)
    stored = [threading.Thread(target=blobs._lossless_webp, args=(data,)) for _ in range(4)]
    shown = [
        threading.Thread(target=pictures.encode_variant, args=(data, 240)) for _ in range(4)
    ]
    jobs = stored + shown
    for job in jobs:
        job.start()
    for job in jobs:
        job.join()

    assert most == transcoding.TRANSCODE_SLOTS


def test_the_blob_codec_still_round_trips_pixel_for_pixel() -> None:
    data = _png()
    packed = blobs._lossless_webp(data)
    assert packed is not None and len(packed) < len(data)
    back = blobs._Codec.decode("blobs/x.webp", packed)
    with Image.open(io.BytesIO(data)) as a, Image.open(io.BytesIO(back)) as b:
        assert a.tobytes() == b.tobytes()
