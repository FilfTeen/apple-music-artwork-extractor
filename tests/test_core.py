from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from pathlib import Path

from apple_music_artwork.discovery import discover, merge_discoveries
from apple_music_artwork.errors import ArtworkError
from apple_music_artwork.jsonio import dumps_json, load_json, loads_json
from apple_music_artwork.models import DiscoveryResult, ImageOptions, Observation, ResourceKey, SelectionRequest
from apple_music_artwork.rendition import resolve_image, source_identity
from apple_music_artwork.selection import select
from apple_music_artwork.urls import parse_url

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "response.json"
PLAYLIST_URL = "https://music.apple.com/us/playlist/example/pl.example"
ALBUM_URL = "https://music.apple.com/us/album/example/100"
ARTIST_URL = "https://music.apple.com/us/artist/example/200"


class CoreTestCase(unittest.TestCase):
    def assert_code(self, code, call, *args, **kwargs):
        with self.assertRaises(ArtworkError) as captured:
            call(*args, **kwargs)
        self.assertEqual(code, captured.exception.code)
        return captured.exception


class URLTests(CoreTestCase):
    def test_dotted_playlist_identity_and_locale(self):
        result = parse_url(PLAYLIST_URL + "?l=en-US")
        self.assertEqual(ResourceKey("us", "playlists", "pl.example"), result.key)
        self.assertEqual("en-US", result.locale)

    def test_album_song_query_changes_target_and_preserves_context(self):
        result = parse_url(ALBUM_URL + "?i=101&l=en")
        self.assertEqual("songs", result.key.resource_type)
        self.assertEqual("101", result.key.resource_id)
        self.assertEqual("100", result.album_id)
        self.assertEqual("en", result.locale)

    def test_itunes_host_and_direct_song(self):
        result = parse_url("https://itunes.apple.com/sg/song/example/101")
        self.assertEqual(ResourceKey("sg", "songs", "101"), result.key)

    def test_canonical_input_omits_unused_queries_and_fragment(self):
        result = parse_url(ALBUM_URL + "?tracking=private-fixture&i=101&l=en#unused")
        self.assertEqual(ALBUM_URL + "?i=101&l=en", result.input_url)
        self.assertNotIn("private-fixture", dumps_json(result))

    def test_rejects_host_auth_port_and_path_escape(self):
        invalid = [
            PLAYLIST_URL.replace("https:", "http:"),
            PLAYLIST_URL.replace("music.apple.com", "music.apple.com.example.test"),
            PLAYLIST_URL.replace("music.apple.com", "user:pass@music.apple.com"),
            PLAYLIST_URL.replace("music.apple.com", "music.apple.com:443"),
            PLAYLIST_URL.replace("example/pl", "%2e%2e/pl"),
            PLAYLIST_URL.replace("example/pl", "%252e%252e/pl"),
            PLAYLIST_URL.replace("example/pl", "a%2fb/pl"),
            PLAYLIST_URL.replace("example/pl", "a%5cb/pl"),
            PLAYLIST_URL.replace("example/pl", "a%00b/pl"),
            PLAYLIST_URL.replace("example/pl", "bad%ZZ/pl"),
            PLAYLIST_URL + "?l=en&l=zh",
            PLAYLIST_URL + "?i=123",
            PLAYLIST_URL + "\n",
            PLAYLIST_URL.replace("example/pl", "\ud800/pl"),
        ]
        for url in invalid:
            with self.subTest(url=ascii(url)):
                self.assert_code("INVALID_URL", parse_url, url)

    def test_unsupported_resource(self):
        self.assert_code("UNSUPPORTED_RESOURCE", parse_url, "https://music.apple.com/us/station/example/123")


class JSONTests(CoreTestCase):
    def test_allows_whitespace_and_key_order(self):
        self.assertEqual(loads_json('{"a":1,"b":2}'), loads_json(' { "b" : 2, "a" : 1 }\n'))

    def test_rejects_duplicate_keys(self):
        for text in ['{"a":1,"a":2}', '{"nested":{"x":1,"x":2}}']:
            self.assert_code("INVALID_JSON", loads_json, text)

    def test_rejects_nonfinite_and_overflow(self):
        for text in ["NaN", "Infinity", "-Infinity", "1e9999", "[1,-1e9999]"]:
            self.assert_code("INVALID_JSON", loads_json, text)

    def test_rejects_invalid_utf8_and_deep_input_without_echoing_it(self):
        for text in [b'"\xff"', "[" * 1500 + "0" + "]" * 1500, '{private-fixture-secret']:
            error = self.assert_code("INVALID_JSON", loads_json, text)
            self.assertNotIn("private-fixture-secret", str(error))

    def test_rejects_lone_surrogates_at_input_and_output(self):
        for text in [b'{"label":"\\ud800"}', b'{"\\udfff":0}', '"\ud800"']:
            self.assert_code("INVALID_JSON", loads_json, text)
        self.assert_code("OUTPUT_JSON_INVALID", dumps_json, {"label": "\ud800"})
        self.assertEqual("\U0001f680", loads_json('"\\ud83d\\ude80"'))

    def test_output_is_utf8_with_one_terminal_lf(self):
        rendered = dumps_json({"title": "封面", "b": 2, "a": 1})
        self.assertTrue(rendered.endswith("\n"))
        self.assertFalse(rendered.endswith("\n\n"))
        self.assertEqual("封面", loads_json(rendered.encode("utf-8"))["title"])
        self.assert_code("OUTPUT_JSON_INVALID", dumps_json, {"value": float("inf")})

    def test_depth_gate_ignores_escaped_strings_and_has_a_fixed_boundary(self):
        value = {"text": "[{" * 300 + '"\\}'}
        self.assertEqual(value, loads_json(dumps_json(value)))
        self.assertIsInstance(loads_json("[" * 128 + "0" + "]" * 128), list)
        self.assert_code("INVALID_JSON", loads_json, "[" * 129 + "0" + "]" * 129)

    def test_file_limit_and_read_failure(self):
        self.assertIsInstance(load_json(FIXTURES / "album.json"), dict)
        self.assert_code("INPUT_TOO_LARGE", load_json, FIXTURES / "album.json", 1)
        self.assert_code("INPUT_READ_FAILED", load_json, FIXTURES / "missing.json")


class DiscoveryTests(CoreTestCase):
    def setUp(self):
        self.target = parse_url(PLAYLIST_URL)
        self.payload = load_json(EXAMPLE)

    def test_target_ownership_audio_exclusion_and_preview_dimensions(self):
        result = discover(self.payload, self.target)
        self.assertEqual(6, len(result.assets))
        self.assertEqual(1, len(result.related_assets))
        self.assertTrue(all(asset.owner == self.target.key for asset in result.assets))
        self.assertEqual("songs", result.related_assets[0].owner.resource_type)
        self.assertFalse(any("audio-ssl" in asset.url for asset in (*result.assets, *result.related_assets)))
        video = next(asset for asset in result.assets if asset.kind == "video")
        self.assertIsNone(video.width)
        self.assertIsNone(video.height)
        self.assertEqual("unknown", video.shape)
        self.assertEqual(video.url, video.source_identity)
        self.assertTrue(video.url.endswith("?variant=fixture"))
        preview = next(asset for asset in result.assets if asset.role == "motion_preview")
        self.assertEqual((3840, 3840), (preview.width, preview.height))

    def test_map_dotted_id_and_missing_redundant_identity(self):
        result = discover(load_json(FIXTURES / "map.json"), self.target)
        self.assertEqual("pl.example", result.assets[0].owner.resource_id)
        self.assertEqual(1, len(result.assets))
        self.assertEqual(1, len(result.related_assets))
        pointer = result.assets[0].observations[0].json_pointer
        self.assertEqual("/resources/playlists/pl.example/attributes/artwork/url", pointer)

    def test_map_conflicting_type_or_id_is_not_silently_accepted(self):
        for field, value in [("id", "pl.other"), ("type", "songs")]:
            payload = load_json(FIXTURES / "map.json")
            payload["resources"]["playlists"]["pl.example"][field] = value
            self.assert_code("RESOURCE_IDENTITY_CONFLICT", discover, payload, self.target)

    def test_data_and_map_represent_same_resource(self):
        mapped = load_json(FIXTURES / "map.json")
        attrs = mapped["resources"]["playlists"]["pl.example"]["attributes"]
        nested = {"data": [{"id": "pl.example", "type": "playlists", "attributes": attrs}]}
        before, after = discover(nested, self.target), discover(mapped, self.target)
        self.assertEqual(before.assets[0].asset_id, after.assets[0].asset_id)
        self.assertNotEqual(before.assets[0].observations, after.assets[0].observations)

    def test_unknown_visual_field_is_retained_and_flagged(self):
        artwork = self.payload["data"][0]["attributes"]["artwork"]
        self.payload["data"][0]["attributes"]["editorialArtwork"]["futurePanel"] = copy.deepcopy(artwork)
        result = discover(self.payload, self.target)
        unknown = next(asset for asset in result.assets if asset.semantic_path.endswith("futurePanel"))
        self.assertEqual("unknown", unknown.role)
        self.assertEqual("image", unknown.kind)
        self.assertIn("UNKNOWN_VISUAL_FIELD", [warning.code for warning in result.warnings])
        self.assertNotIn(unknown, select(result, SelectionRequest(all_static=True)).selected)
        self.assertEqual((unknown,), select(result, SelectionRequest(asset_id=unknown.asset_id)).selected)

    def test_artist_motion_namespace_is_classified(self):
        result = discover(load_json(FIXTURES / "artist.json"), parse_url(ARTIST_URL))
        self.assertEqual({"primary_static", "banner", "motion_preview", "motion"}, {a.role for a in result.assets})

    def test_distinct_renditions_do_not_overwrite_each_other(self):
        before = discover(self.payload, self.target, "baseline")
        artwork = self.payload["data"][0]["attributes"]["artwork"]
        artwork["url"] = artwork["url"].replace("bb.jpg", "{c}.{f}")
        after = discover(self.payload, self.target, "template")
        merged = merge_discoveries([after, before])
        candidates = [asset for asset in merged.assets if asset.semantic_path == "artwork"]
        self.assertEqual(2, len(candidates))
        self.assertEqual(1, len({asset.source_identity for asset in candidates}))
        self.assertEqual(2, len({asset.asset_id for asset in candidates}))
        self.assertEqual(7, len(merged.assets))

    def test_duplicate_observations_are_merged_without_losing_provenance(self):
        first = discover(self.payload, self.target, "baseline")
        second = discover(self.payload, self.target, "extended")
        result = merge_discoveries([second, first, first])
        self.assertEqual(6, len(result.assets))
        self.assertTrue(all(len(asset.observations) == 2 for asset in result.assets))
        self.assertEqual(result.to_dict(), merge_discoveries([first, second]).to_dict())

    def test_json_pointer_escapes_keys_and_does_not_parse_dots(self):
        self.payload["data"][0]["attributes"]["editorialArtwork"]["new/a~b"] = {
            "url": "https://is1-ssl.mzstatic.com/image/thumb/Fixture/new.jpg/{w}x{h}bb.jpg",
            "width": 20, "height": 20,
        }
        result = discover(self.payload, self.target)
        asset = next(asset for asset in result.assets if "new/a~b" in asset.semantic_path)
        self.assertTrue(asset.observations[0].json_pointer.endswith("/new~1a~0b/url"))

    def test_missing_target_and_invalid_envelope_are_diagnostic(self):
        self.assert_code("RESOURCE_NOT_FOUND", discover, {"data": []}, self.target)
        self.assert_code("API_SCHEMA_CHANGED", discover, {"data": {}}, self.target)
        self.assert_code("API_SCHEMA_CHANGED", discover, {"error": "redacted"}, self.target)

    def test_boolean_dimensions_are_not_image_sizes(self):
        self.payload["data"][0]["attributes"]["artwork"]["width"] = True
        result = discover(self.payload, self.target)
        asset = next(asset for asset in result.assets if asset.semantic_path == "artwork")
        self.assertIsNone(asset.width)
        self.assertIn("INVALID_DIMENSIONS", [issue.code for issue in result.warnings])

    def test_schema_version_and_asset_ids_are_deterministic(self):
        result = discover(self.payload, self.target)
        data = loads_json(dumps_json(result))
        self.assertEqual(2, data["schema_version"])
        self.assertTrue(all(asset["asset_id"].startswith("asset_") for asset in data["assets"]))
        self.assertEqual(data, loads_json(dumps_json(discover(self.payload, self.target))))


class SelectionTests(CoreTestCase):
    def setUp(self):
        self.target = parse_url(PLAYLIST_URL)
        self.result = discover(load_json(EXAMPLE), self.target)

    def test_playlist_prefers_editorial_square_and_records_policy(self):
        decision = select(self.result, SelectionRequest())
        self.assertEqual("editorialArtwork.staticDetailSquare", decision.selected[0].semantic_path)
        self.assertEqual("playlist-primary-v1", decision.policy)

    def test_album_and_artist_use_canonical_artwork(self):
        for filename, url in [("album.json", ALBUM_URL), ("artist.json", ARTIST_URL)]:
            result = discover(load_json(FIXTURES / filename), parse_url(url))
            self.assertEqual("artwork", select(result, SelectionRequest()).selected[0].semantic_path)

    def test_song_query_does_not_select_the_album(self):
        target = parse_url(ALBUM_URL + "?i=101")
        payload = load_json(FIXTURES / "album.json")
        payload["data"].append({"type": "songs", "id": "101", "attributes": {
            "artwork": {"width": 500, "height": 500, "url": "https://is1-ssl.mzstatic.com/image/thumb/Fixture/song.jpg/{w}x{h}bb.jpg"}
        }})
        result = discover(payload, target)
        chosen = select(result, SelectionRequest()).selected[0]
        self.assertEqual("101", chosen.owner.resource_id)
        self.assertEqual("songs", chosen.owner.resource_type)
        self.assertEqual(3, len(result.related_assets))

    def test_playlist_fallback_and_alias_references(self):
        kept = tuple(asset for asset in self.result.assets if asset.semantic_path != "editorialArtwork.staticDetailSquare")
        result = replace(self.result, assets=kept)
        decision = select(result, SelectionRequest())
        self.assertEqual("artwork", decision.selected[0].semantic_path)
        self.assertEqual({"artwork", "editorialArtwork.subscriptionCover"}, {a.semantic_path for a in decision.references})
        result = replace(result, assets=tuple(a for a in kept if a.semantic_path != "artwork"))
        self.assertEqual("editorialArtwork.subscriptionCover", select(result, SelectionRequest()).selected[0].semantic_path)

    def test_equal_priority_different_sources_are_ambiguous(self):
        first = next(a for a in self.result.assets if a.semantic_path == "artwork")
        other = replace(first, url=first.url.replace("cover.jpg", "other.jpg"), source_identity=first.source_identity.replace("cover.jpg", "other.jpg"))
        result = replace(self.result, assets=(first, other))
        self.assert_code("AMBIGUOUS_PRIMARY_ASSET", select, result, SelectionRequest())
        self.assert_code("AMBIGUOUS_PRIMARY_ASSET", select, result, SelectionRequest(semantic_path="artwork"))
        self.assertEqual((other,), select(result, SelectionRequest(asset_id=other.asset_id)).selected)

    def test_same_source_templates_are_not_false_ambiguity(self):
        first = next(a for a in self.result.assets if a.semantic_path == "artwork")
        template = replace(first, url=first.url.replace("bb.jpg", "{c}.{f}"))
        result = replace(self.result, assets=(template, first))
        self.assertEqual((first,), select(result, SelectionRequest()).selected)
        self.assertEqual(2, len(select(result, SelectionRequest()).references))
        self.assertEqual(2, len(select(result, SelectionRequest(semantic_path="artwork")).selected))

    def test_all_static_preserves_distinct_renditions_and_semantic_refs(self):
        chosen = select(self.result, SelectionRequest(all_static=True))
        self.assertEqual(5, len(chosen.selected))
        self.assertTrue(all(a.kind == "image" for a in chosen.selected))
        self.assertIn("editorialArtwork.subscriptionCover", {a.semantic_path for a in chosen.selected})

    def test_all_static_keeps_same_source_square_and_tall_overlay(self):
        result = discover(load_json(FIXTURES / "album.json"), parse_url(ALBUM_URL))
        chosen = select(result, SelectionRequest(all_static=True)).selected
        cover = next(asset for asset in chosen if asset.semantic_path == "artwork")
        hero = next(asset for asset in chosen if asset.semantic_path.endswith("superHeroTall"))
        self.assertEqual(cover.source_identity, hero.source_identity)
        self.assertNotEqual(cover.asset_id, hero.asset_id)
        self.assertNotEqual(cover.shape, hero.shape)
        self.assertNotEqual(resolve_image(cover).url, resolve_image(hero).url)
        self.assertIn("1680x2240SHT.AMTPPS01.jpg", resolve_image(hero).url)

    def test_related_resource_cannot_be_selected_even_if_given_in_assets(self):
        polluted = replace(self.result, assets=self.result.related_assets)
        self.assert_code("NO_PRIMARY_STATIC_ASSET", select, polluted, SelectionRequest())
        self.assert_code("ASSET_NOT_FOUND", select, self.result, SelectionRequest(asset_id=self.result.related_assets[0].asset_id))

    def test_explicit_motion_is_selectable_and_conflicting_modes_fail(self):
        video = next(a for a in self.result.assets if a.kind == "video")
        self.assertEqual((video,), select(self.result, SelectionRequest(asset_id=video.asset_id)).selected)
        self.assert_code("INVALID_SELECTION_OPTIONS", select, self.result, SelectionRequest(semantic_path="artwork", all_static=True))


class RenditionTests(CoreTestCase):
    def setUp(self):
        result = discover(load_json(FIXTURES / "album.json"), parse_url(ALBUM_URL))
        self.asset = next(a for a in result.assets if a.semantic_path == "artwork")

    def test_requests_declared_dimensions_from_rendition_not_source_identity(self):
        request = resolve_image(self.asset)
        self.assertEqual((1200, 1200), (request.width, request.height))
        self.assertTrue(request.url.endswith("/1200x1200bb.jpg"))
        self.assertNotEqual(request.url, request.source_identity)

    def test_preserves_fixed_crop_overlay_and_query(self):
        for modifier in ["sr", "SHT.AMTPPS01.", "SC.DN01.", "SC.FPESS04."]:
            suffix = modifier + ("{f}" if modifier.endswith(".") else ".{f}")
            asset = replace(self.asset, url=self.asset.url.replace("bb.jpg", suffix) + "?variant=fixture")
            request = resolve_image(asset, ImageOptions(format="png"))
            self.assertIn("1200x1200" + modifier, request.url)
            self.assertTrue(request.url.endswith("png?variant=fixture"))
            self.assertEqual("png", request.format)

    def test_template_source_identity_and_hls_query(self):
        source = source_identity(self.asset.url, "image")
        flexible = self.asset.url.replace("bb.jpg", "{c}.{f}")
        self.assertEqual(source, source_identity(flexible + "?l=en", "image"))
        self.assertNotEqual(source, source_identity(flexible + "?variant=two", "image"))
        video = "https://mvod.itunes.apple.com/x/master.m3u8?variant=one&key=value#part"
        self.assertEqual(video, source_identity(video, "video"))

    def test_source_identity_removes_only_the_final_rendition(self):
        url = "https://is1-ssl.mzstatic.com/image/thumb/1200x1200-source/source.jpg/{w}x{h}bb.jpg"
        self.assertEqual(url.rsplit("/", 1)[0], source_identity(url, "image"))

    def test_missing_and_boolean_declared_sizes_fail(self):
        for bad in [None, 0, -1, True, "1200"]:
            self.assert_code("MISSING_DIMENSIONS", resolve_image, replace(self.asset, width=bad))

    def test_requested_size_rejects_bool_invalid_and_upscale(self):
        for value in [True, 0, -10, 12.5]:
            self.assert_code("INVALID_DIMENSIONS", resolve_image, self.asset, ImageOptions(width=value))
        self.assert_code("DIMENSIONS_EXCEED_DECLARED", resolve_image, self.asset, ImageOptions(width=1201))
        self.assert_code("DIMENSIONS_EXCEED_DECLARED", resolve_image, self.asset, ImageOptions(height=1201))

    def test_one_dimension_keeps_declared_aspect_ratio(self):
        request = resolve_image(replace(self.asset, width=320, height=240), ImageOptions(width=160))
        self.assertEqual((160, 120), (request.width, request.height))

    def test_unknown_or_broken_placeholders_fail(self):
        for url in [self.asset.url + "{quality}", self.asset.url + "{", self.asset.url.replace("{h}", "100")]:
            self.assert_code("IMAGE_TEMPLATE_INVALID", resolve_image, replace(self.asset, url=url))

    def test_unsafe_hosts_and_video_do_not_resolve(self):
        for url in [self.asset.url.replace("https:", "http:"), self.asset.url.replace("is1-ssl.mzstatic.com", "example.test"), self.asset.url.replace("is1-ssl.mzstatic.com", "user@is1-ssl.mzstatic.com")]:
            self.assert_code("UNSAFE_ASSET_URL", resolve_image, replace(self.asset, url=url))
        self.assert_code("UNSUPPORTED_ASSET_KIND", resolve_image, replace(self.asset, kind="video"))

    def test_fixed_format_is_not_rewritten(self):
        png = replace(self.asset, url=self.asset.url.replace(".jpg", ".png"))
        self.assertEqual("png", resolve_image(png).format)


if __name__ == "__main__":
    unittest.main()
