"""Preflight has metadata authority only, and binds local observations."""

from argparse import Namespace
from contextlib import chdir
from importlib.util import find_spec
from pathlib import Path
import tempfile
from unittest import TestCase

from apple_music_artwork.client import HttpClient
from apple_music_artwork.execution import execute
from apple_music_artwork.jsonio import dumps_json
from test_execution import response, Images, NoNetwork


class PlanningTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.response = self.root / "response.json"
        self.response.write_text(dumps_json(response()), encoding="utf-8")
        self.args = Namespace(url="https://music.apple.com/us/album/synthetic/1?l=en-US", response=str(self.response),
                              output=str(self.root / "task"), list=False, semantic=None, asset_id=None, all_static=True,
                              all_media=False, all_motion=False, inspect_motion=False, compare_storefronts=None,
                              token_env="APPLE_MUSIC_DEVELOPER_TOKEN", provider="web", width=None, height=None,
                              format=None, motion_profile="best", jobs=2, progress="never", json=True,
                              library=None, new_version=False, no_input=True)

    def module(self):
        self.assertIsNotNone(find_spec("apple_music_artwork.planning"), "preflight is missing")
        from apple_music_artwork import planning
        return planning

    def test_new_plan_does_not_create_business_directory_or_fetch_media(self):
        planning = self.module()
        plan = planning.preflight(self.args, http=HttpClient(NoNetwork()), environ={})
        self.assertFalse(Path(self.args.output).exists())
        self.assertEqual(plan["items"][0]["action"], "download")
        self.assertEqual(plan["destination"]["layout_version"], 2)
        self.assertEqual(len(plan["plan_id"]), 64)
        self.assertEqual(len(plan["task_id"]), 64)
        self.assertEqual(plan["network_scope"]["media_bodies"], False)

    def test_complete_flat_task_is_readonly_and_zero_network(self):
        planning = self.module()
        first, code = execute(self.args, http=HttpClient(Images()), environ={})
        self.assertEqual(code, 0)
        before = {str(path.relative_to(self.args.output)): path.read_bytes()
                  for path in Path(self.args.output).rglob("*") if path.is_file()}
        plan = planning.preflight(self.args, http=HttpClient(NoNetwork()), environ={})
        after = {str(path.relative_to(self.args.output)): path.read_bytes()
                 for path in Path(self.args.output).rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(plan["items"][0]["action"], "reuse")
        self.assertEqual(plan["destination"]["layout_version"], 1)
        self.assertEqual(plan["local_observations"]["files"][0]["sha256"], first["downloads"][0]["validation"]["sha256"])

    def test_modified_local_media_changes_plan_and_blocks_reuse(self):
        planning = self.module()
        result, code = execute(self.args, http=HttpClient(Images()), environ={})
        first = planning.preflight(self.args, http=HttpClient(NoNetwork()), environ={})
        (Path(self.args.output) / result["downloads"][0]["path"]).write_bytes(b"changed file")
        second = planning.preflight(self.args, http=HttpClient(NoNetwork()), environ={})
        self.assertNotEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(second["items"][0]["action"], "needs_action")
        self.assertTrue(second["decisions"])

    def test_new_version_requires_a_new_exact_directory(self):
        planning = self.module()
        execute(self.args, http=HttpClient(Images()), environ={})
        self.args.new_version = True
        from apple_music_artwork.errors import ArtworkError
        with self.assertRaises(ArtworkError) as failure:
            planning.preflight(self.args, http=HttpClient(NoNetwork()), environ={})
        self.assertEqual(failure.exception.code, "NEW_OUTPUT_REQUIRED")

    def test_exact_derived_destination_is_frozen_when_plan_is_created(self):
        planning = self.module()
        from apple_music_artwork.workflow import dispatch
        with chdir(self.root):
            failed, code = dispatch(self.args, http=HttpClient(Images(bad={"synthetic-0"})), environ={})
            self.assertEqual(code, 8, failed)
            plan = planning.preflight(self.args, http=HttpClient(NoNetwork()), environ={})
        other = self.root / "other"
        other.mkdir()
        with chdir(other):
            planning.validate_plan(plan)
            option = next(option for option in plan["decisions"][0]["options"] if option["action"] == "derive")
            self.assertEqual(option["params"]["library"], str(self.root / "output"))
            self.assertIn("新任务全部图片宽度", option["label"])
            self.assertIn(str(self.root / "output"), option["label"])
        self.assertFalse((self.root / "output").exists())
