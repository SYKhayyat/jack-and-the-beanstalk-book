"""Upload handling: untrusted bytes in, safe normalised image out."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.services.images import MAX_EDGE_PIXELS, InvalidImage, decode_upload
from tests.helpers import make_noisy_photo_bytes, make_photo_bytes


class TestAcceptedUploads:
    def test_animated_webp_keeps_the_first_frame(self) -> None:
        frames = [Image.new("RGB", (20, 20), colour) for colour in ("#0000ff", "#00ff00")]
        buffer = io.BytesIO()
        frames[0].save(buffer, format="WEBP", save_all=True, append_images=frames[1:])

        decoded = decode_upload(buffer.getvalue())
        with Image.open(io.BytesIO(decoded.data)) as opened:
            assert opened.getpixel((10, 10))[2] > 200, "first frame should be the blue one"

    @pytest.mark.parametrize(
        ("image_format", "expected_type"),
        [("JPEG", "image/jpeg"), ("PNG", "image/jpeg"), ("WEBP", "image/jpeg")],
    )
    def test_supported_formats_are_normalised_to_jpeg(
        self, image_format: str, expected_type: str
    ) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (120, 90), "#88aa66").save(buffer, format=image_format)

        decoded = decode_upload(buffer.getvalue())
        assert decoded.media_type == expected_type
        assert decoded.suffix == "jpg"

    def test_rgba_input_is_flattened(self) -> None:
        # A PNG with alpha would otherwise fail or render with a hole in it.
        buffer = io.BytesIO()
        Image.new("RGBA", (50, 50), (10, 20, 30, 128)).save(buffer, format="PNG")

        decoded = decode_upload(buffer.getvalue())
        with Image.open(io.BytesIO(decoded.data)) as opened:
            assert opened.mode == "RGB"

    def test_large_images_are_downscaled(self) -> None:
        decoded = decode_upload(make_noisy_photo_bytes(size=(MAX_EDGE_PIXELS * 2, 100)))
        with Image.open(io.BytesIO(decoded.data)) as opened:
            assert max(opened.size) <= MAX_EDGE_PIXELS

    def test_small_images_keep_their_aspect_ratio(self) -> None:
        decoded = decode_upload(make_photo_bytes(size=(400, 200)))
        with Image.open(io.BytesIO(decoded.data)) as opened:
            assert opened.size == (400, 200)


class TestRejectedUploads:
    def test_unsafe_dimensions_are_rejected_before_decode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Exercise the dimension guard without allocating a genuinely huge image.
        monkeypatch.setattr("app.services.images.MAX_INPUT_PIXELS", 100)
        with pytest.raises(InvalidImage, match="too many pixels"):
            decode_upload(make_photo_bytes(size=(11, 11)))

    def test_empty_bytes(self) -> None:
        with pytest.raises(InvalidImage, match="empty"):
            decode_upload(b"")

    def test_not_an_image_at_all(self) -> None:
        with pytest.raises(InvalidImage, match="not a readable image"):
            decode_upload(b"<html><body>not a photo</body></html>")

    def test_executable_disguised_as_a_photo(self) -> None:
        with pytest.raises(InvalidImage):
            decode_upload(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    def test_truncated_image(self) -> None:
        truncated = make_photo_bytes()[:40]
        with pytest.raises(InvalidImage):
            decode_upload(truncated)

    def test_unsupported_format(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (20, 20), "#123456").save(buffer, format="BMP")

        with pytest.raises(InvalidImage, match="Unsupported image format"):
            decode_upload(buffer.getvalue())

    def test_animated_image_keeps_the_first_frame(self) -> None:
        # Someone who uploads an animated GIF of themselves means "here is my
        # photo". Rejecting was a papercut that taught them nothing.
        frames = [Image.new("RGB", (20, 20), colour) for colour in ("#ff0000", "#00ff00")]
        buffer = io.BytesIO()
        frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:])

        decoded = decode_upload(buffer.getvalue())
        with Image.open(io.BytesIO(decoded.data)) as opened:
            assert opened.getpixel((10, 10))[0] > 200, "first frame should be the red one"


class TestPrivacy:
    def test_exif_metadata_is_stripped(self) -> None:
        # Phone photos carry GPS and camera owner in EXIF. The output must not.
        source = Image.new("RGB", (60, 60), "#c0ffee")
        exif = source.getexif()
        exif[0x013B] = "Some Photographer"
        exif[0x010F] = "Some Camera"

        buffer = io.BytesIO()
        source.save(buffer, format="JPEG", exif=exif)

        with Image.open(io.BytesIO(buffer.getvalue())) as original:
            assert original.getexif()

        decoded = decode_upload(buffer.getvalue())
        with Image.open(io.BytesIO(decoded.data)) as opened:
            assert not dict(opened.getexif())

    def test_orientation_is_applied_not_merely_copied(self) -> None:
        # Exif orientation 6 means "rotate 90°". After normalisation the pixels
        # are upright, so width and height have swapped.
        source = Image.new("RGB", (80, 40), "#224466")
        exif = source.getexif()
        exif[0x0112] = 6
        buffer = io.BytesIO()
        source.save(buffer, format="JPEG", exif=exif)

        decoded = decode_upload(buffer.getvalue())
        with Image.open(io.BytesIO(decoded.data)) as opened:
            assert opened.size == (40, 80)
