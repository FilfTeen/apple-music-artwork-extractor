"""Real Pillow decoding and test-owned output scopes; no live HTTP traffic."""

from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import tempfile
from unittest import TestCase, skipUnless
from unittest.mock import patch

from PIL import Image

from apple_music_artwork.client import HttpClient, HttpResponse
from apple_music_artwork.download import OutputCommitError, OutputDirectory, download_image, output_filename, validate_image
from apple_music_artwork.errors import ArtworkError
from apple_music_artwork.models import ImageRequest, ResourceKey, VisualAsset


def image_bytes(size=(4, 4), format="JPEG"):
    stream = BytesIO()
    with Image.new("RGB", size, (72, 136, 180)) as image:
        image.save(stream, format=format)
    return stream.getvalue()


ASSET = VisualAsset(ResourceKey("us", "albums", "1"), "artwork", "image", "primary_static",
                    "https://is1-ssl.mzstatic.com/image/thumb/test/{w}x{h}bb.jpg", "synthetic-source", 4, 4)
REQUEST = ImageRequest("https://is1-ssl.mzstatic.com/image/thumb/test/4x4bb.jpg", 4, 4, "jpg", ASSET.source_identity, ASSET.asset_id)


class OneImageTransport:
    def __init__(self, body=None):
        self.body = image_bytes() if body is None else body
        self.count = 0

    def request(self, url, **kwargs):
        self.count += 1
        return HttpResponse(200, {"Content-Type": "image/jpeg"}, self.body)


class ValidationTests(TestCase):
    def test_real_decode_records_dimensions_format_hash_and_bytes(self):
        body = image_bytes()
        result = validate_image(body, REQUEST, content_type="image/jpeg; charset=binary")
        self.assertEqual(result["actual_dimensions"], {"width": 4, "height": 4})
        self.assertEqual(result["actual_format"], "JPEG")
        self.assertEqual(result["sha256"], sha256(body).hexdigest())
        self.assertEqual(result["bytes"], len(body))
        self.assertTrue(result["decoded"])

    def test_empty_html_and_truncated_images_fail(self):
        for body in (b"", b"<html>error</html>", image_bytes()[:-20]):
            with self.subTest(length=len(body)), self.assertRaises(ArtworkError) as failure:
                validate_image(body, REQUEST)
            self.assertEqual(failure.exception.category, "validation")

    def test_actual_dimensions_and_format_are_checked(self):
        for body, content_type, code in [(image_bytes((3, 4)), "image/jpeg", "ASSET_DIMENSION_MISMATCH"),
                                         (image_bytes(format="PNG"), "image/png", "MEDIA_TYPE_MISMATCH"),
                                         (image_bytes(), "text/html", "MEDIA_TYPE_MISMATCH")]:
            with self.subTest(code=code), self.assertRaises(ArtworkError) as failure:
                validate_image(body, REQUEST, content_type=content_type)
            self.assertEqual(failure.exception.code, code)

    def test_pixel_limit_is_checked_before_load(self):
        with self.assertRaises(ArtworkError) as failure:
            validate_image(image_bytes(), REQUEST, max_pixels=8)
        self.assertEqual(failure.exception.code, "IMAGE_TOO_LARGE")

    def test_verify_is_followed_by_actual_pixel_load(self):
        body = image_bytes()
        with patch("PIL.JpegImagePlugin.JpegImageFile.load", side_effect=OSError("synthetic decoder failure")):
            with self.assertRaises(ArtworkError) as failure:
                validate_image(body, REQUEST)
        self.assertEqual(failure.exception.code, "ASSET_VALIDATION_FAILED")


@skipUnless(os.name == "posix", "Safe file publication currently requires POSIX")
class OutputTests(TestCase):
    @classmethod
    def setUpClass(cls):
        runtime = Path(tempfile.gettempdir()).resolve()
        for ancestor in [runtime, *runtime.parents]:
            if ancestor.exists():
                assert not ancestor.is_symlink()
        runtime.mkdir(parents=True, exist_ok=True)
        cls.scope = Path(tempfile.mkdtemp(prefix="v03-core-download-", dir=runtime))

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="case-", dir=self.scope))

    def test_success_commits_decoded_file_and_leaves_no_part(self):
        body = image_bytes()
        with OutputDirectory(self.folder / "nested") as output:
            result = download_image(ASSET, REQUEST, output, HttpClient(OneImageTransport(body)))
        self.assertEqual((self.folder / "nested" / result["path"]).read_bytes(), body)
        self.assertFalse(list((self.folder / "nested").glob("*.part")))
        self.assertFalse(Path(result["path"]).is_absolute())
        self.assertEqual(result["validation"]["sha256"], sha256(body).hexdigest())

    def test_existing_file_is_never_overwritten_and_avoids_http(self):
        transport = OneImageTransport()
        name = output_filename(ASSET, REQUEST)
        with (self.folder / name).open("xb") as stream:
            stream.write(b"existing")
        with OutputDirectory(self.folder) as output:
            with self.assertRaises(ArtworkError) as failure:
                download_image(ASSET, REQUEST, output, HttpClient(transport))
        self.assertEqual(failure.exception.code, "OUTPUT_CONFLICT")
        self.assertEqual((self.folder / name).read_bytes(), b"existing")
        self.assertEqual(transport.count, 0)

    def test_symlink_directory_and_filename_are_rejected(self):
        target = self.folder / "real"
        target.mkdir()
        alias = self.folder / "alias"
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ArtworkError) as failure:
            with OutputDirectory(alias):
                pass
        self.assertEqual(failure.exception.code, "UNSAFE_OUTPUT_PATH")
        final = self.folder / "existing.jpg"
        final.symlink_to(target / "absent.jpg")
        with OutputDirectory(self.folder) as output:
            with self.assertRaises(ArtworkError) as failure:
                output.write_bytes(final.name, b"must not escape")
        self.assertEqual(failure.exception.code, "OUTPUT_CONFLICT")
        self.assertFalse((target / "absent.jpg").exists())

    def test_validation_failure_never_publishes(self):
        with OutputDirectory(self.folder) as output:
            with self.assertRaises(ArtworkError):
                download_image(ASSET, REQUEST, output, HttpClient(OneImageTransport(b"<html/>")))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_precommit_failure_reports_uncommitted_and_cleans_owned_part(self):
        with OutputDirectory(self.folder) as output:
            with patch("apple_music_artwork.download.os.link", side_effect=PermissionError("test")):
                with self.assertRaises(OutputCommitError) as failure:
                    output.write_bytes("cover.jpg", b"test")
        self.assertFalse(failure.exception.committed)
        self.assertEqual(failure.exception.residue, ())
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_wrapped_write_failure_preserves_controlled_recovery_context(self):
        context = {"reason": "server_cooldown", "scope": "item", "operation": "image", "retry_at": 1600}
        error = ArtworkError("RETRY_AFTER_DEADLINE", "Synthetic cooldown.", "download", True, context=context)
        with OutputDirectory(self.folder) as output:
            with self.assertRaises(OutputCommitError) as failure:
                output.write_stream("cover.jpg", lambda stream: (_ for _ in ()).throw(error))
        self.assertEqual(failure.exception.to_dict(), error.to_dict())
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_postcommit_cleanup_failure_retains_record_and_relative_residue(self):
        with OutputDirectory(self.folder) as output:
            with patch("apple_music_artwork.download.os.unlink", side_effect=PermissionError("test")):
                with self.assertRaises(OutputCommitError) as failure:
                    download_image(ASSET, REQUEST, output, HttpClient(OneImageTransport()))
        error = failure.exception
        self.assertEqual(error.code, "OUTPUT_CLEANUP_FAILED")
        self.assertTrue(error.committed)
        self.assertTrue((self.folder / error.path).is_file())
        self.assertEqual(error.artifact["status"], "downloaded")
        self.assertTrue(error.artifact["validation"]["decoded"])
        self.assertEqual(len(error.residue), 1)
        self.assertTrue((self.folder / error.residue[0]).is_file())
        self.assertFalse(Path(error.residue[0]).is_absolute())
