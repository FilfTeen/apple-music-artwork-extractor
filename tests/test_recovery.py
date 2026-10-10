"""Recovery guidance is controlled data, distinct from one retry window."""

import inspect
import json
from unittest import TestCase

from apple_music_artwork import errors
from apple_music_artwork.runtime import RetryAfterDeadline


class RecoveryTests(TestCase):
    def test_context_copies_only_registered_fields_and_rejects_private_text(self):
        self.assertIn("context", inspect.signature(errors.ArtworkError).parameters)
        context = {"reason": "server_cooldown", "scope": "item", "retry_at": 1600,
                   "http_status": 429, "operation": "motion", "attempt": 3}
        problem = errors.ArtworkError("HTTP_TRANSIENT_ERROR", "Controlled failure.", "download",
                                      True, context=context)
        context["retry_at"] = 0
        self.assertEqual(problem.to_dict()["context"]["retry_at"], 1600)
        for unsafe in ({"Authorization": "Bearer secret"}, {"reason": "/home/private/name"},
                       {"retry_at": float("nan")}, {"retry_at": 10 ** 400}, {"attempt": True}, {"http_status": 999}):
            with self.subTest(context=unsafe), self.assertRaises(ValueError):
                errors.ArtworkError("NETWORK_ERROR", "Controlled failure.", context=unsafe)
        serialized = problem.to_dict()
        serialized["context"]["retry_at"] = 0
        self.assertEqual(problem.to_dict()["context"]["retry_at"], 1600)

    def test_later_recovery_does_not_grant_another_automatic_retry(self):
        self.assertTrue(callable(getattr(errors, "recovery_for", None)))
        problem = RetryAfterDeadline(600, 1600, "download")
        result = errors.recovery_for(problem, scope="item", item_id="file_synthetic")
        self.assertFalse(problem.retryable)
        self.assertEqual(result["reason"], "server_cooldown")
        self.assertEqual(result["retry_at"], 1600)
        self.assertEqual(result["allowed_actions"], ["retry", "defer"])
        self.assertEqual(result["scope"], "item")
        self.assertEqual(result["item_id"], "file_synthetic")
        restored = errors.ArtworkError(problem.code, problem.message, problem.category,
                                       problem.retryable, context=problem.to_dict()["context"])
        self.assertEqual(errors.recovery_for(restored, item_id="file_synthetic"), result)

    def test_failure_classes_do_not_suggest_silent_quality_or_scope_changes(self):
        self.assertTrue(callable(getattr(errors, "recovery_for", None)))
        for code, category, scope, actions in (
            ("REQUEST_LIMIT", "download", "task", ["retry", "defer"]),
            ("MEDIA_TOOL_MISSING", "download", "environment", ["retry", "defer", "inspect"]),
            ("VIDEO_CODEC_MISMATCH", "validation", "item", ["inspect", "derive", "defer"]),
            ("HLS_ENCRYPTED", "rendition", "item", ["inspect", "derive", "defer"]),
            ("RUN_STATE_INVALID", "download", "task", ["inspect", "derive"]),
        ):
            with self.subTest(code=code):
                result = errors.recovery_for(errors.ArtworkError(code, "Controlled.", category))
                self.assertEqual(result["scope"], scope)
                self.assertEqual(result["allowed_actions"], actions)
        result = errors.recovery_for(errors.ArtworkError("NETWORK_ERROR", "secret /home/private", "download"))
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(result["allowed_actions"], ["retry", "defer"])
