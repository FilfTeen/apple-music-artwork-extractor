"""Coordinator-owned temporary identities and no-clobber publication."""

from hashlib import sha256
from pathlib import Path
import os
import tempfile
from unittest import TestCase, skipUnless
from unittest.mock import patch

from apple_music_artwork.download import OutputCommitError, OutputDirectory
from apple_music_artwork.errors import ArtworkError


@skipUnless(os.name == "posix", "Descriptor-relative publication requires POSIX")
class PreparedOutputTests(TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(tempfile.gettempdir()).resolve()
        assert root.resolve(strict=True) == root
        cls.scope = Path(tempfile.mkdtemp(prefix="v03-core-prepared-", dir=root))

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="case-", dir=self.scope))

    def test_worker_cannot_publish_and_coordinator_keeps_link_until_checkpoint(self):
        with OutputDirectory(self.folder) as output:
            temp = output.allocate_temp("cover.png")
            registered = temp.record()
            self.assertFalse(registered["sealed"])
            result = temp.write_stream(lambda stream: stream.write(b"owned"),
                                       lambda path, written: {"bytes": written, "body": path.read_bytes()})
            self.assertEqual(result["validation"], {"bytes": 5, "body": b"owned"})
            self.assertFalse((self.folder / "cover.png").exists())
            self.assertTrue(temp.record()["sealed"])
            output.publish_temp(temp)
            self.assertEqual((self.folder / "cover.png").read_bytes(), b"owned")
            self.assertTrue(temp.path.exists())
            output.cleanup_temp(temp)
            self.assertFalse(temp.path.exists())

    def test_restore_rejects_substituted_temporary_and_never_removes_it(self):
        with OutputDirectory(self.folder) as output:
            temp = output.allocate_temp("cover.png")
            registered = temp.record()
            original = self.folder / "original-owned-temp"
            temp.path.rename(original)
            temp.path.write_bytes(b"external replacement")
            with self.assertRaises(ArtworkError) as failure:
                output.restore_temp(registered)
            self.assertEqual(failure.exception.code, "OUTPUT_IDENTITY_CHANGED")
            self.assertEqual(temp.path.read_bytes(), b"external replacement")
            self.assertEqual(original.read_bytes(), b"")

    def test_unsealed_registered_partial_can_be_reclaimed_by_exact_inode(self):
        with OutputDirectory(self.folder) as output:
            temp = output.allocate_temp("cover.png")
            registered = temp.record()
            with temp.path.open("wb") as stream:
                stream.write(b"interrupted")
            restored = output.restore_temp(registered)
            output.cleanup_temp(restored)
            self.assertFalse(temp.path.exists())

    def test_media_never_replaces_an_existing_destination(self):
        (self.folder / "cover.png").write_bytes(b"first")
        with OutputDirectory(self.folder) as output:
            temp = output.allocate_temp("cover.png")
            temp.write_stream(lambda stream: stream.write(b"second"))
            for options in ({}, {"replace": True, "expected_destination": {"bytes": 5, "sha256": sha256(b"first").hexdigest()}}):
                with self.subTest(options=options), self.assertRaises(ArtworkError):
                    output.publish_temp(temp, **options)
            self.assertEqual((self.folder / "cover.png").read_bytes(), b"first")
            output.cleanup_temp(temp)

    def test_manifest_replace_requires_the_registered_previous_hash(self):
        old = b'{"version":1}\n'
        new = b'{"version":2}\n'
        (self.folder / "manifest.json").write_bytes(old)
        with OutputDirectory(self.folder) as output:
            temp = output.allocate_temp("manifest.json")
            temp.write_stream(lambda stream: stream.write(new))
            with self.assertRaises(ArtworkError):
                output.publish_temp(temp, replace=True, expected_destination={"bytes": len(old), "sha256": "0" * 64})
            self.assertEqual((self.folder / "manifest.json").read_bytes(), old)
            output.publish_temp(temp, replace=True,
                                expected_destination={"bytes": len(old), "sha256": sha256(old).hexdigest()})
            self.assertEqual((self.folder / "manifest.json").read_bytes(), new)
            self.assertFalse(temp.path.exists())

    def test_post_link_sync_failure_exposes_the_committed_file(self):
        with OutputDirectory(self.folder) as output:
            temp = output.allocate_temp("cover.png")
            temp.write_stream(lambda stream: stream.write(b"owned"))
            with patch("apple_music_artwork.download.os.fsync", side_effect=OSError("injected")):
                with self.assertRaises(OutputCommitError) as failure:
                    output.publish_temp(temp)
            self.assertTrue(failure.exception.committed)
            self.assertEqual((self.folder / "cover.png").read_bytes(), b"owned")
            self.assertEqual(output.restore_temp(temp.record()).inode, temp.inode)
            output.cleanup_temp(temp)

    def test_validated_temporary_cannot_change_before_publication(self):
        with OutputDirectory(self.folder) as output:
            temp = output.allocate_temp("cover.png")
            temp.write_stream(lambda stream: stream.write(b"owned"))
            temp.path.write_bytes(b"changed after validation")
            with self.assertRaises(ArtworkError) as failure:
                output.publish_temp(temp)
            self.assertEqual(failure.exception.code, "OUTPUT_IDENTITY_CHANGED")
            self.assertFalse((self.folder / "cover.png").exists())
