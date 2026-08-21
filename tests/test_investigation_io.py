"""Atomic project I/O and external capture identity/recovery."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from cansniff.investigation import (
    CaptureReferenceStatus, ComparisonDefinition, ProjectError, attach_capture,
    ProfileMatchAction, ProfileMatchDecision, load_project, new_project,
    relocate_capture, save_project, verify_capture,
)
from cansniff.analysis.signals import Profile
from cansniff.investigation.model import new_id


class ProjectIoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def path(self, name):
        return os.path.join(self.directory.name, name)

    def write(self, name, data):
        path = self.path(name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_atomic_save_and_load(self):
        path = self.path("case.cansniff-project")
        project = new_project("Case", "now")
        save_project(project, path)
        self.assertEqual(load_project(path).project, project)
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(self.directory.name)))

    def test_failed_replace_preserves_existing_project(self):
        path = self.path("case.cansniff-project")
        original = new_project("Original", "now")
        save_project(original, path)
        with patch("cansniff.investigation.io.os.replace", side_effect=OSError("interrupted")):
            with self.assertRaises(ProjectError):
                save_project(new_project("New", "later"), path)
        self.assertEqual(load_project(path).project.title, "Original")

    def test_malformed_json_and_oversized_file_fail(self):
        path = self.write("bad.cansniff-project", b"{broken")
        with self.assertRaisesRegex(ProjectError, "parse"):
            load_project(path)
        with patch("cansniff.investigation.io.os.path.getsize", return_value=40*1024*1024):
            with self.assertRaisesRegex(ProjectError, "safety limit"):
                load_project(path)

    def test_duplicate_keys_and_nonstandard_numbers_fail(self):
        duplicate = self.path("duplicate.cansniff-project")
        with open(duplicate, "w", encoding="utf-8") as handle:
            handle.write('{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(ProjectError, "duplicate JSON key"):
            load_project(duplicate)
        nonfinite = self.path("nan.cansniff-project")
        with open(nonfinite, "w", encoding="utf-8") as handle:
            handle.write('{"schema_version":1,"unknown":NaN}')
        with self.assertRaisesRegex(ProjectError, "invalid numeric"):
            load_project(nonfinite)

    def test_schema_zero_migrates_deterministically(self):
        project = new_project("Old", "now").to_dict()
        project["schema_version"] = 0
        path = self.path("old.cansniff-project")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(project, handle)
        result = load_project(path)
        self.assertEqual(result.project.schema_version, 3)
        self.assertEqual(result.migrations, (
            "migrated project schema 0 to schema 1",
            "migrated project schema 1 to schema 2",
            "migrated project schema 2 to schema 3"))

    def test_schema_two_adds_diagnostic_state_without_derived_results(self):
        raw = new_project("Phase 9", "now").to_dict()
        raw["schema_version"] = 2
        raw["format_version"] = "2.0"
        raw["selections"].pop("diagnostics", None)
        raw["analysis_versions"].pop("diagnostic_conversation", None)
        raw["analysis_versions"].pop("uds_structured_decode", None)
        path = self.path("phase9.cansniff-project")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)
        result = load_project(path)
        self.assertEqual(result.project.schema_version, 3)
        self.assertEqual(result.project.diagnostic_selection, ())
        self.assertEqual(result.migrations,
                         ("migrated project schema 2 to schema 3",))
        self.assertEqual(dict(result.project.analysis_versions)[
            "diagnostic_conversation"], "1.0")

    def test_capture_available_missing_changed_and_moved(self):
        first = self.write("first.asc", b"capture A")
        reference = attach_capture(first, now="now")
        self.assertEqual(verify_capture(reference).status, CaptureReferenceStatus.AVAILABLE)
        moved = self.write("moved.asc", b"capture A")
        self.assertEqual(verify_capture(reference, moved).status, CaptureReferenceStatus.MOVED)
        changed = self.write("changed.asc", b"capture B")
        self.assertEqual(verify_capture(reference, changed).status,
                         CaptureReferenceStatus.CHANGED)
        with open(first, "wb") as handle:
            handle.write(b"capture Z")
        self.assertEqual(verify_capture(reference).status,
                         CaptureReferenceStatus.CHANGED)
        os.remove(first)
        self.assertEqual(verify_capture(reference).status, CaptureReferenceStatus.MISSING)

    def test_changed_capture_requires_explicit_acceptance(self):
        first = self.write("first.asc", b"one")
        changed = self.write("changed.asc", b"two")
        reference = attach_capture(first, now="now")
        kept, status = relocate_capture(reference, changed)
        self.assertEqual(kept, reference)
        self.assertEqual(status.status, CaptureReferenceStatus.CHANGED)
        accepted, status = relocate_capture(reference, changed, accept_changed=True)
        self.assertNotEqual(accepted.content_hash, reference.content_hash)
        self.assertEqual(status.status, CaptureReferenceStatus.AVAILABLE)
        self.assertEqual(dict(accepted.source_metadata)["evidence_replaced"], "true")
        self.assertEqual(dict(accepted.source_metadata)["previous_content_hash"],
                         reference.content_hash)

    def test_project_load_without_capture_succeeds(self):
        path = self.path("empty.cansniff-project")
        save_project(new_project(now="now"), path)
        self.assertEqual(load_project(path).capture_verifications, ())

    def test_accepted_profile_choice_survives_save_and_reopen(self):
        profile = Profile("Investigation choice")
        decision = ProfileMatchDecision(
            new_id(), ProfileMatchAction.USE_PROFILE, profile.profile_id,
            profile.name, "a" * 64, "c" * 64, "1.0", "now")
        project = new_project(now="now").changed(
            profile_snapshot_json=json.dumps(
                profile.to_dict(), sort_keys=True, separators=(",", ":")),
            profile_match_decisions=(decision,))
        path = self.path("accepted.cansniff-project")
        save_project(project, path)
        restored = load_project(path).project
        self.assertEqual(restored.profile_snapshot["id"], profile.profile_id)
        self.assertEqual(restored.profile_match_decisions, (decision,))

    def test_out_of_range_comparison_is_reported_without_adjustment(self):
        capture_path = self.write("range.asc", b"capture")
        capture = attach_capture(capture_path, {
            "retained_start": "10", "retained_end": "20"}, now="now")
        comparison = ComparisonDefinition(
            new_id(), capture.capture_id, "Baseline", 5, 12,
            "Event", 14, 25, "now")
        project = new_project(now="now").changed(
            captures=(capture,), active_capture_id=capture.capture_id,
            comparisons=(comparison,))
        path = self.path("range.cansniff-project")
        save_project(project, path)
        result = load_project(path)
        self.assertIn("outside", result.comparison_issues[0])
        self.assertEqual(result.project.comparisons[0], comparison)


if __name__ == "__main__":
    unittest.main()
