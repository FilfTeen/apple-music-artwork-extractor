"""Machine selections are exact capabilities scoped to one plan revision."""

from importlib.util import find_spec
from unittest import TestCase
from apple_music_artwork.errors import ArtworkError


class ProtocolTests(TestCase):
    def test_bound_choices_reject_changed_plan_unknown_action_and_extra_parameters(self):
        self.assertIsNotNone(find_spec("apple_music_artwork.protocol"), "bound choices are missing")
        from apple_music_artwork.protocol import decision, validate_choice
        context = decision("a" * 64, "failed_items", "NETWORK_ERROR", ["file_one"], ["retry", "defer"])
        valid = {"plan_id": "a" * 64, "decision_id": context["decision_id"], "action": "retry", "params": {}}
        self.assertEqual(validate_choice(context, valid)["action"], "retry")
        for patch in ({"plan_id": "b" * 64}, {"action": "skip"}, {"params": {"url": "changed"}}, {"decision_id": "wrong"}):
            with self.assertRaises(ArtworkError):
                validate_choice(context, {**valid, **patch})
