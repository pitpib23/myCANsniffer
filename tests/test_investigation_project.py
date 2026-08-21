"""Project schema, entities, migration, and deterministic serialization."""

from __future__ import annotations

import json
import math
import unittest

from cansniff.investigation import (
    Annotation, Bookmark, BookmarkKind, ComparisonDefinition,
    InvestigationProject, ProfileMatchAction, ProfileMatchDecision,
    ProjectError, new_project,
)
from cansniff.investigation.io import serialize_project
from cansniff.investigation.model import new_id


class ProjectModelTests(unittest.TestCase):
    def test_new_projects_pin_current_analysis_versions(self):
        versions = dict(new_project(now="now").analysis_versions)
        self.assertEqual(versions["j1939_transport"], "1.0")
        self.assertEqual(versions["j1939_definition_parser"], "1.0")
        self.assertEqual(versions["j1939_decode"], "1.0")
        self.assertEqual(versions["diagnostic_conversation"], "1.0")
        self.assertEqual(versions["uds_structured_decode"], "1.0")

    def test_minimal_project_round_trip_is_deterministic(self):
        project = new_project("Machine A", "2026-01-01T00:00:00+00:00")
        text = serialize_project(project)
        restored = InvestigationProject.from_dict(json.loads(text))
        self.assertEqual(restored, project)
        self.assertEqual(serialize_project(restored), text)

    def test_full_user_state_round_trip(self):
        project = new_project(now="now")
        capture_id = new_id()
        from cansniff.investigation import CaptureReference
        capture = CaptureReference(capture_id, "capture.asc", "capture.asc", "asc",
                                   "a" * 64, 12, "now")
        annotation = Annotation(new_id(), capture_id, 1.5, 2.0, "เครื่องเริ่ม", "a", "b",
                                ("startup",))
        bookmark = Bookmark(new_id(), BookmarkKind.MESSAGE, "Interesting", capture_id,
                            (("message_key", "can0:201:S"),), "now")
        comparison = ComparisonDefinition(new_id(), capture_id, "Idle", 0, 1,
                                          "Running", 2, 3, "now", "note")
        project = project.changed(
            captures=(capture,), active_capture_id=capture_id,
            comparisons=(comparison,), annotations=(annotation,),
            bookmarks=(bookmark,), selected_message_keys=("can0:201:S",),
            active_workspace="Compare", display_filter=(("channel", "can0"),),
            diagnostic_selection=(("conversation_filter", "F190"),
                                  ("tab", "conversations")))
        self.assertEqual(InvestigationProject.from_dict(project.to_dict()), project)

    def test_user_accepted_match_decision_round_trip(self):
        decision = ProfileMatchDecision(
            new_id(), ProfileMatchAction.ASSOCIATE_DEFINITION, new_id(),
            "Local Drive", "a" * 64, "c" * 64, "1.0", "now",
            "b" * 64, 3, "can0")
        project = new_project(now="now").changed(
            profile_match_decisions=(decision,))
        restored = InvestigationProject.from_dict(project.to_dict())
        self.assertEqual(restored.profile_match_decisions, (decision,))

    def test_association_decision_requires_definition_and_node(self):
        raw = new_project(now="now").to_dict()
        raw["profile_match_decisions"] = [{
            "id": new_id(), "action": "ASSOCIATE_DEFINITION",
            "profile_id": new_id(), "profile_name": "x",
            "capture_hash": "a" * 64, "candidate_set_identity": "c" * 64,
            "algorithm_version": "1.0", "accepted_at": "now",
            "definition_hash": "", "node_id": None, "channel": "can0"}]
        with self.assertRaisesRegex(ProjectError, "needs a hash and node_id"):
            InvestigationProject.from_dict(raw)

    def test_future_and_missing_schema_fail_clearly(self):
        raw = new_project(now="now").to_dict()
        raw["schema_version"] = 99
        with self.assertRaisesRegex(ProjectError, "unsupported"):
            InvestigationProject.from_dict(raw)
        del raw["schema_version"]
        with self.assertRaisesRegex(ProjectError, "schema_version"):
            InvestigationProject.from_dict(raw)
        raw = new_project(now="now").to_dict()
        raw["schema_version"] = True
        with self.assertRaisesRegex(ProjectError, "integer"):
            InvestigationProject.from_dict(raw)

    def test_missing_optional_fields_use_documented_defaults(self):
        raw = new_project(now="now").to_dict()
        for field in ("captures", "active_capture_id", "comparisons", "annotations",
                      "bookmarks", "selections", "display_filter",
                      "profile_snapshot", "analysis_versions"):
            raw.pop(field)
        restored = InvestigationProject.from_dict(raw)
        self.assertEqual(restored.captures, ())
        self.assertEqual(restored.active_workspace, "Messages")
        self.assertTrue(restored.analysis_versions)

    def test_wrong_types_and_nonfinite_timestamps_fail(self):
        raw = new_project(now="now").to_dict()
        raw["annotations"] = "many"
        with self.assertRaises(ProjectError):
            InvestigationProject.from_dict(raw)
        raw = new_project(now="now").to_dict()
        raw["annotations"] = [{"id": new_id(), "capture_id": new_id(),
                               "start": math.inf, "text": "x",
                               "created_at": "a", "modified_at": "a"}]
        with self.assertRaisesRegex(ProjectError, "finite"):
            InvestigationProject.from_dict(raw)

    def test_duplicate_entity_ids_fail(self):
        project = new_project(now="now")
        identity, capture = new_id(), new_id()
        value = Annotation(identity, capture, 1, None, "x", "a", "a")
        raw = project.changed(annotations=(value, value)).to_dict()
        with self.assertRaisesRegex(ProjectError, "unique"):
            InvestigationProject.from_dict(raw)

    def test_long_annotation_is_bounded(self):
        raw = new_project(now="now").to_dict()
        raw["annotations"] = [{"id": new_id(), "capture_id": new_id(),
                               "start": 1, "end": None, "text": "x" * 10001,
                               "created_at": "a", "modified_at": "a", "tags": []}]
        with self.assertRaisesRegex(ProjectError, "exceeds"):
            InvestigationProject.from_dict(raw)

    def test_embedded_profile_rejects_nonfinite_and_excessive_nesting(self):
        raw = new_project(now="now").to_dict()
        raw["profile_snapshot"] = {"scale": math.nan}
        with self.assertRaisesRegex(ProjectError, "finite"):
            InvestigationProject.from_dict(raw)
        nested = {}
        cursor = nested
        for _ in range(40):
            cursor["child"] = {}
            cursor = cursor["child"]
        raw["profile_snapshot"] = nested
        with self.assertRaisesRegex(ProjectError, "deeply"):
            InvestigationProject.from_dict(raw)


if __name__ == "__main__":
    unittest.main()
