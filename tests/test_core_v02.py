"""Minimal synthetic regressions for observed v0.2 discovery boundaries."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from apple_music_artwork.discovery import discover, merge_discoveries
from apple_music_artwork.errors import ArtworkError
from apple_music_artwork.models import SelectionRequest
from apple_music_artwork.rendition import resolve_image
from apple_music_artwork.selection import select
from apple_music_artwork.urls import parse_url


URL = "https://music.apple.com/cn/playlist/example/pl.example?l=zh-Hans-CN"
FIELDS = ("artwork", "editorialArtwork", "editorialVideo")


def image(name="cover", *, width=1080, height=1080, **extra):
    return {"url": f"https://is1-ssl.mzstatic.com/image/thumb/Fixture/{name}.png/{{w}}x{{h}}{{c}}.{{f}}",
            "width": width, "height": height, **extra}


def payload(*, storefront="cn", **attributes):
    return {"data": [{"type": "playlists", "id": "pl.example",
                      "href": f"/v1/catalog/{storefront}/playlists/pl.example",
                      "attributes": {"artwork": image(), **attributes}}]}


class ConfirmedDiscoveryRegressionTests(unittest.TestCase):
    def test_explicit_response_storefront_is_not_relabelled_as_the_request(self):
        result = discover(payload(storefront="us"), parse_url(URL))
        self.assertEqual("cn", result.target.key.storefront)
        self.assertEqual({"us"}, {asset.owner.storefront for asset in result.assets})
        self.assertEqual("conflict", result.to_dict()["source_evidence"]["status"])
        with self.assertRaises(ArtworkError) as failure:
            select(result, SelectionRequest())
        self.assertEqual("RESOURCE_STOREFRONT_CONFLICT", failure.exception.code)

    def test_missing_null_empty_present_and_malformed_are_distinct(self):
        missing = object()
        cases = [(missing, "missing"), (None, "null"), ({}, "empty"),
                 ({"motionDetailSquare": {"video": "https://mvod.itunes.apple.com/example/master.m3u8"}}, "present"),
                 ([], "malformed")]
        for value, expected in cases:
            with self.subTest(expected=expected):
                data = payload()
                if value is not missing:
                    data["data"][0]["attributes"]["editorialVideo"] = value
                result = discover(data, parse_url(URL))
                self.assertEqual(expected, result.to_dict()["coverage"]["fields"]["editorialVideo"]["state"])

    def test_observed_super_hero_wide_is_known_and_selected_by_all_static(self):
        # Wide hero artwork remains a known candidate for all-static selection.
        data = payload(editorialArtwork={"superHeroWide": image("wide", width=4320, height=1800)})
        result = discover(data, parse_url(URL))
        wide = next(asset for asset in result.assets if asset.semantic_path.endswith("superHeroWide"))
        self.assertEqual("hero", wide.role)
        self.assertIn(wide, select(result, SelectionRequest(all_static=True)).selected)


class StorefrontEvidenceTests(unittest.TestCase):
    def test_missing_href_stays_unverified_even_when_a_public_url_exists(self):
        data = payload(url=URL.replace("/cn/", "/us/"))
        del data["data"][0]["href"]
        result = discover(data, parse_url(URL))
        evidence = result.to_dict()["source_evidence"]
        self.assertEqual("unverified", evidence["status"])
        self.assertEqual([], evidence["observed_storefronts"])
        self.assertIn("us", [item["storefront"] for item in evidence["observations"] if item["field"] == "attributes.url"])
        self.assertEqual("cn", result.assets[0].owner.storefront)
        self.assertEqual("artwork", select(result, SelectionRequest()).selected[0].semantic_path)

    def test_public_page_url_does_not_overrule_a_matching_catalog_href(self):
        result = discover(payload(url=URL.replace("/cn/", "/us/")), parse_url(URL))
        self.assertEqual("verified", result.source_evidence.status)
        self.assertEqual({"cn"}, {asset.owner.storefront for asset in result.assets})
        self.assertIn("PUBLIC_URL_STOREFRONT_CONFLICT", {item.code for item in result.warnings})
        self.assertEqual(1, len(select(result, SelectionRequest()).selected))

    def test_related_storefront_is_not_evidence_for_the_target(self):
        data = payload()
        data["data"][0]["relationships"] = {"tracks": {"data": [{
            "type": "songs", "id": "10", "href": "/v1/catalog/us/songs/10",
            "attributes": {"artwork": image("related", width=3000, height=3000)}}]}}
        result = discover(data, parse_url(URL))
        self.assertEqual("verified", result.source_evidence.status)
        self.assertEqual(("cn",), result.source_evidence.observed_storefronts)
        self.assertEqual("us", result.related_assets[0].owner.storefront)
        self.assertEqual((result.assets[0],), select(result, SelectionRequest(all_media=True)).selected)

    def test_resource_map_keys_do_not_impose_the_requested_storefront(self):
        data = {"resources": {"playlists": {"pl.example": {
            "href": "/v1/catalog/us/playlists/pl.example", "attributes": {"artwork": image()}}}}}
        result = discover(data, parse_url(URL))
        self.assertEqual("us", result.assets[0].owner.storefront)
        self.assertEqual("conflict", result.source_evidence.status)
        self.assertEqual(0, len(result.related_assets))

    def test_href_on_a_target_reference_is_not_lost_when_attributes_are_in_the_map(self):
        data = {"data": [{"type": "playlists", "id": "pl.example",
                          "href": "/v1/catalog/us/playlists/pl.example"}],
                "resources": {"playlists": {"pl.example": {"attributes": {"artwork": image()}}}}}
        result = discover(data, parse_url(URL))
        self.assertEqual("conflict", result.source_evidence.status)
        self.assertEqual("us", result.assets[0].owner.storefront)
        with self.assertRaises(ArtworkError) as failure:
            select(result, SelectionRequest())
        self.assertEqual("RESOURCE_STOREFRONT_CONFLICT", failure.exception.code)

    def test_malformed_href_is_unverified_and_is_not_reflected(self):
        malformed = [None, 4, "https://user:private-secret@api.music.apple.com/v1/catalog/us/playlists/pl.example",
                     "/v1/catalog/us/playlists/some-other-id", "https://evil.test/v1/catalog/us/playlists/pl.example"]
        for href in malformed:
            with self.subTest(href=href):
                data = payload()
                data["data"][0]["href"] = href
                result = discover(data, parse_url(URL))
                self.assertEqual("unverified", result.source_evidence.status)
                self.assertNotIn("private-secret", str(result.to_dict()))
                self.assertEqual("cn", result.assets[0].owner.storefront)

    def test_merge_retains_conflicting_href_observations_and_true_owners(self):
        target = parse_url(URL)
        first = discover(payload(), target, "cn-response")
        second = discover(payload(storefront="us"), target, "us-response")
        merged = merge_discoveries([first, second, first])
        self.assertEqual("conflict", merged.source_evidence.status)
        self.assertEqual({"cn", "us"}, {asset.owner.storefront for asset in merged.assets})
        self.assertEqual({"cn-response", "us-response"}, {item.variant for item in merged.source_evidence.observations})
        with self.assertRaises(ArtworkError) as failure:
            select(merged, SelectionRequest(all_media=True))
        self.assertEqual("RESOURCE_STOREFRONT_CONFLICT", failure.exception.code)


class CoverageTests(unittest.TestCase):
    def test_each_field_reports_its_own_state_and_the_requested_fields(self):
        data = payload(editorialArtwork=None, editorialVideo={})
        result = discover(data, parse_url(URL), "web", requested_fields=FIELDS)
        coverage = result.to_dict()["coverage"]
        self.assertEqual("returned_response_fields", coverage["scope"])
        self.assertEqual(list(FIELDS), coverage["requested_fields"])
        self.assertTrue(coverage["request_observations"][0]["known"])
        self.assertEqual({"artwork": "present", "editorialArtwork": "null", "editorialVideo": "empty"},
                         {key: value["state"] for key, value in coverage["fields"].items()})

    def test_offline_input_does_not_invent_a_request_shape(self):
        coverage = discover(payload(), parse_url(URL)).to_dict()["coverage"]
        self.assertEqual([], coverage["requested_fields"])
        self.assertFalse(coverage["request_observations"][0]["known"])
        self.assertEqual("missing", coverage["fields"]["editorialVideo"]["state"])

    def test_malformed_fields_remain_visible_alongside_usable_artwork(self):
        result = discover(payload(editorialArtwork={"staticDetailSquare": None}, editorialVideo="unexpected"), parse_url(URL))
        self.assertEqual("malformed", result.to_dict()["coverage"]["fields"]["editorialArtwork"]["state"])
        self.assertEqual("malformed", result.to_dict()["coverage"]["fields"]["editorialVideo"]["state"])
        self.assertIn("MALFORMED_VISUAL_FIELD", {item.code for item in result.warnings})
        self.assertEqual("artwork", select(result, SelectionRequest()).selected[0].semantic_path)

    def test_merge_preserves_missing_and_null_without_order_or_repeat_dependence(self):
        target = parse_url(URL)
        first = discover(payload(), target, "catalog", requested_fields=("artwork",))
        second = discover(payload(editorialVideo=None), target, "web", requested_fields=FIELDS)
        merged = merge_discoveries([second, first, first])
        coverage = merged.to_dict()["coverage"]
        self.assertEqual("mixed", coverage["fields"]["editorialVideo"]["state"])
        self.assertEqual({"missing", "null"}, {item["state"] for item in coverage["fields"]["editorialVideo"]["observations"]})
        self.assertEqual(2, len(coverage["request_observations"]))
        self.assertEqual(merge_discoveries([first, second]).to_dict(), merged.to_dict())

    def test_invalid_requested_field_contract_is_diagnostic(self):
        for fields in ("artwork", ("unknownField",), (None,)):
            with self.subTest(fields=fields), self.assertRaises(ArtworkError) as failure:
                discover(payload(), parse_url(URL), requested_fields=fields)
            self.assertEqual("INVALID_REQUEST_FIELDS", failure.exception.code)


class DeclarationTests(unittest.TestCase):
    def test_same_url_semantic_aliases_keep_their_own_conflicting_p3_declarations(self):
        # Same-URL aliases retain their conflicting P3 declarations.
        cover = image(hasP3=False, bgColor="123456")
        alias = {**cover, "hasP3": True}
        result = discover(payload(artwork=cover, editorialArtwork={"subscriptionCover": alias}), parse_url(URL))
        self.assertEqual(1, len({asset.url for asset in result.assets}))
        values = {asset.semantic_path: [item.value for item in asset.declarations if item.name == "hasP3"] for asset in result.assets}
        self.assertEqual({"artwork": [False], "editorialArtwork.subscriptionCover": [True]}, values)
        self.assertIn("VISUAL_DECLARATION_CONFLICT", {item.code for item in result.warnings})
        self.assertEqual(2, len(select(result, SelectionRequest(all_static=True)).selected))

    def test_changed_declarations_do_not_change_asset_id_and_are_not_overwritten_on_merge(self):
        target = parse_url(URL)
        first = discover(payload(artwork=image(hasP3=False)), target, "first")
        second = discover(payload(artwork=image(hasP3=True)), target, "second")
        self.assertEqual(first.assets[0].asset_id, second.assets[0].asset_id)
        merged = merge_discoveries([first, second, first])
        self.assertEqual(1, len(merged.assets))
        declarations = [item for item in merged.assets[0].declarations if item.name == "hasP3"]
        self.assertEqual({False, True}, {item.value for item in declarations})
        self.assertEqual({"first", "second"}, {observation.variant for item in declarations for observation in item.observations})
        self.assertIn("VISUAL_DECLARATION_CONFLICT", {item.code for item in merged.warnings})
        self.assertEqual(merge_discoveries([second, first]).to_dict(), merged.to_dict())

    def test_identical_declarations_merge_observations_and_reject_malformed_values(self):
        target = parse_url(URL)
        data = payload(artwork=image(hasP3=True, bgColor="abcdef", textColor1=44))
        first = discover(data, target, "first")
        second = discover(data, target, "second")
        result = merge_discoveries([first, second])
        declarations = result.assets[0].declarations
        self.assertEqual({"hasP3", "bgColor"}, {item.name for item in declarations})
        self.assertTrue(all(len(item.observations) == 2 for item in declarations))
        self.assertTrue(all(item.observations[0].json_pointer.endswith("/" + item.name) for item in declarations))
        self.assertIn("INVALID_VISUAL_DECLARATION", {item.code for item in result.warnings})


class UnknownCandidateTests(unittest.TestCase):
    def test_unconsumed_thumbnail_and_poster_are_visible_but_not_automatically_selected(self):
        data = payload(thumbnail=image("thumb"), metadata={"poster": image("poster")})
        result = discover(data, parse_url(URL))
        self.assertEqual({"artwork", "thumbnail", "metadata.poster"}, {asset.semantic_path for asset in result.assets})
        unknown = tuple(asset for asset in result.assets if asset.role == "unknown")
        self.assertEqual(2, len(unknown))
        self.assertEqual(2, result.to_dict()["coverage"]["unknown_candidate_count"])
        self.assertEqual((result.assets[0],), select(result, SelectionRequest(all_media=True)).selected)
        self.assertEqual((unknown[0],), select(result, SelectionRequest(asset_id=unknown[0].asset_id)).selected)

    def test_candidate_scan_excludes_audio_and_navigation(self):
        data = payload(metadata={
            "audio": "https://audio-ssl.itunes.apple.com/example/preview.m4a",
            "website": "https://example.test/about", "resource": URL,
            "song": "https://music.apple.com/cn/song/example/100",
            "poster": {"url": "https://example.test/poster.png", "width": 400, "height": 600}})
        result = discover(data, parse_url(URL))
        self.assertEqual({"artwork", "metadata.poster"}, {asset.semantic_path for asset in result.assets})
        poster = next(asset for asset in result.assets if asset.semantic_path.endswith("poster"))
        self.assertEqual("unknown", poster.kind)

    def test_candidate_scan_limit_is_reported_and_does_not_hide_known_artwork(self):
        data = payload(metadata={"padding": list(range(20)), "poster": image("late")})
        with patch("apple_music_artwork.discovery.MAX_CANDIDATE_NODES", 5):
            result = discover(data, parse_url(URL))
        self.assertEqual("artwork", select(result, SelectionRequest()).selected[0].semantic_path)
        scan = result.to_dict()["coverage"]["scan"]
        self.assertEqual({"status": "limited", "visited_nodes": 5, "limit": 5}, scan)
        self.assertIn("VISUAL_SCAN_LIMIT", {item.code for item in result.warnings})

    def test_an_unknown_visual_named_root_does_not_bypass_the_scan_bound(self):
        data = payload(newArtworkSurface={"padding": list(range(20)), "poster": image("late")})
        with patch("apple_music_artwork.discovery.MAX_CANDIDATE_NODES", 5):
            result = discover(data, parse_url(URL))
        self.assertEqual("limited", result.to_dict()["coverage"]["scan"]["status"])
        self.assertEqual({"artwork"}, {asset.semantic_path for asset in result.assets})


class MediaSelectionTests(unittest.TestCase):
    def make_result(self):
        return discover(payload(editorialVideo={
            "motionDetailSquare": {"video": "https://mvod.itunes.apple.com/example/square-one.m3u8"},
            "motionSquareVideo1x1": {"video": "https://mvod.itunes.apple.com/example/square-two.m3u8"},
            "motionDetailTall": {"video": "https://mvod.itunes.apple.com/example/square-one.m3u8"},
            "futureMotion": {"video": "https://mvod.itunes.apple.com/example/unknown.m3u8"}}), parse_url(URL))

    def test_all_motion_keeps_independent_urls_and_their_aliases(self):
        selected = select(self.make_result(), SelectionRequest(all_motion=True)).selected
        self.assertEqual(3, len(selected))
        self.assertEqual(2, len({asset.url for asset in selected}))
        self.assertTrue(all(asset.kind == "video" and asset.role == "motion" for asset in selected))

    def test_all_media_extends_all_static_without_enabling_unknown_fields(self):
        result = self.make_result()
        self.assertEqual(1, len(select(result, SelectionRequest(None, None, True)).selected))
        selected = select(result, SelectionRequest(all_media=True)).selected
        self.assertEqual(4, len(selected))
        self.assertEqual({"image", "video"}, {asset.kind for asset in selected})
        self.assertTrue(all(asset.role != "unknown" for asset in selected))
        self.assertEqual("artwork", select(result, SelectionRequest()).selected[0].semantic_path)

    def test_motion_can_be_selected_by_id_or_semantic_path(self):
        result = self.make_result()
        asset = next(item for item in result.assets if item.role == "motion")
        self.assertEqual((asset,), select(result, SelectionRequest(asset_id=asset.asset_id)).selected)
        self.assertEqual((asset,), select(result, SelectionRequest(semantic_path=asset.semantic_path)).selected)

    def test_semantic_motion_selection_does_not_merge_distinct_masters(self):
        target = parse_url(URL)
        data = payload(editorialVideo={"motionDetailSquare": {"video": "https://mvod.itunes.apple.com/example/one.m3u8"}})
        first = discover(data, target, "first")
        data["data"][0]["attributes"]["editorialVideo"]["motionDetailSquare"]["video"] = "https://mvod.itunes.apple.com/example/two.m3u8"
        second = discover(data, target, "second")
        selected = select(merge_discoveries([first, second]), SelectionRequest(semantic_path="editorialVideo.motionDetailSquare.video")).selected
        self.assertEqual(2, len(selected))
        self.assertEqual(2, len({asset.url for asset in selected}))

    def test_new_modes_are_mutually_exclusive_and_missing_motion_is_explicit(self):
        result = discover(payload(), parse_url(URL))
        for request in [SelectionRequest(all_media=True, all_static=True), SelectionRequest(all_media=True, all_motion=True),
                        SelectionRequest(semantic_path="artwork", all_motion=True), SelectionRequest(all_motion=1)]:
            with self.subTest(request=request), self.assertRaises(ArtworkError) as failure:
                select(result, request)
            self.assertEqual("INVALID_SELECTION_OPTIONS", failure.exception.code)
        with self.assertRaises(ArtworkError) as failure:
            select(result, SelectionRequest(all_motion=True))
        self.assertEqual("NO_MOTION_ASSET", failure.exception.code)

    def test_unusable_preferred_static_does_not_silently_change_the_primary_policy(self):
        square = image("square", width=3840, height=3840)
        del square["height"]
        result = discover(payload(editorialArtwork={"staticDetailSquare": square}), parse_url(URL))
        selected = select(result, SelectionRequest()).selected[0]
        self.assertEqual("editorialArtwork.staticDetailSquare", selected.semantic_path)
        with self.assertRaises(ArtworkError) as failure:
            resolve_image(selected)
        self.assertEqual("MISSING_DIMENSIONS", failure.exception.code)


if __name__ == "__main__":
    unittest.main()
