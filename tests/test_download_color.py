"""Real image metadata tests use generated pixels and profiles, never CDN data."""

from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from unittest import TestCase

from PIL import Image, ImageCms, PngImagePlugin

from apple_music_artwork.download import validate_image
from apple_music_artwork.models import ImageRequest


REQUEST = ImageRequest("https://a.mzstatic.com/4x4.png", 4, 4, "png", "synthetic", "asset")


def png(mode="RGBA", *, icc=None, srgb=False):
    output = BytesIO()
    kwargs = {}
    if icc is not None:
        kwargs["icc_profile"] = icc
    if srgb:
        info = PngImagePlugin.PngInfo()
        info.add(b"sRGB", b"\x00")
        kwargs["pnginfo"] = info
    with Image.new(mode, (4, 4), (20, 40, 60, 128) if mode == "RGBA" else (20, 40, 60)) as image:
        image.save(output, format="PNG", **kwargs)
    return output.getvalue()


class ColorTests(TestCase):
    def test_alpha_mode_and_metadata_are_reported_without_reencoding(self):
        body = png()
        result = validate_image(body, REQUEST)
        self.assertEqual(result["actual_mode"], "RGBA")
        self.assertTrue(result["transparency"]["alpha_channel"])
        self.assertEqual(result["transparency"]["alpha_extrema"], [128, 128])
        self.assertTrue(result["transparency"]["has_transparency_data"])
        self.assertFalse(result["color"]["conversion_applied"])
        self.assertEqual(result["sha256"], sha256(body).hexdigest())

    def test_png_srgb_conflicts_with_p3_declaration(self):
        result = validate_image(png("RGB", srgb=True), REQUEST, declared_p3=(True,))
        self.assertEqual(result["color"]["png_srgb"], {"present": True, "rendering_intent": 0})
        self.assertEqual(result["color"]["declaration"]["status"], "conflict")
        self.assertEqual(result["color"]["p3_status"], "unverified")
        self.assertIn("COLOR_DECLARATION_CONFLICT", result["color"]["warnings"])

    def test_valid_icc_records_hash_and_description_but_does_not_prove_p3(self):
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        result = validate_image(png(icc=profile), REQUEST, declared_p3=(True,))
        actual = result["color"]["icc_profile"]
        self.assertTrue(actual["present"] and actual["valid"])
        self.assertEqual(actual["bytes"], len(profile))
        self.assertEqual(actual["sha256"], sha256(profile).hexdigest())
        self.assertIn("sRGB", actual["description"])
        self.assertEqual(result["color"]["declaration"]["status"], "conflict")

    def test_invalid_and_missing_icc_remain_visible_without_rejecting_pixels(self):
        invalid = validate_image(png(icc=b"synthetic-invalid-profile"), REQUEST, declared_p3=(True,))
        self.assertTrue(invalid["decoded"])
        self.assertFalse(invalid["color"]["icc_profile"]["valid"])
        self.assertIn("ICC_PROFILE_INVALID", invalid["color"]["warnings"])
        missing = validate_image(png(), REQUEST, declared_p3=(True,))
        self.assertFalse(missing["color"]["icc_profile"]["present"])
        self.assertEqual(missing["color"]["declaration"]["status"], "unverified")
        self.assertIn("P3_NOT_VERIFIED", missing["color"]["warnings"])

    def test_alias_contradictions_and_invalid_declarations_are_not_lost(self):
        result = validate_image(png(), REQUEST, declared_p3=(True, False, "invalid:string:true"))
        self.assertEqual(result["color"]["declaration"]["hasP3"], [True, False, "invalid:string:true"])
        self.assertEqual(result["color"]["declaration"]["status"], "conflict")
        self.assertIn("COLOR_DECLARATION_INVALID", result["color"]["warnings"])

    def test_palette_transparency_is_distinct_from_an_alpha_channel(self):
        output = BytesIO()
        with Image.new("P", (4, 4), 0) as image:
            image.putpalette([0, 0, 0] * 256)
            image.save(output, format="PNG", transparency=0)
        result = validate_image(output.getvalue(), REQUEST)
        self.assertEqual(result["actual_mode"], "P")
        self.assertFalse(result["transparency"]["alpha_channel"])
        self.assertTrue(result["transparency"]["transparency_metadata"])
        self.assertTrue(result["transparency"]["has_transparency_data"])

    def test_jpeg_opaque_pixels_and_wide_tall_png_are_decoded(self):
        for size, fmt, extension in (((8, 2), "PNG", "png"), ((2, 8), "PNG", "png"), ((4, 4), "JPEG", "jpg")):
            output = BytesIO()
            with Image.new("RGB", size, (20, 40, 60)) as image:
                image.save(output, format=fmt)
            request = replace(REQUEST, width=size[0], height=size[1], format=extension)
            result = validate_image(output.getvalue(), request)
            self.assertEqual(result["actual_dimensions"], {"width": size[0], "height": size[1]})
            self.assertFalse(result["transparency"]["has_transparency_data"])
