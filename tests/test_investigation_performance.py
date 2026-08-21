"""Scale guards for project persistence, hashing, and report generation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from cansniff.investigation import (
    Annotation, Bookmark, BookmarkKind, ComparisonDefinition, ReportContext,
    attach_capture, generate_markdown_report, load_project, new_project,
    save_project,
)
from cansniff.investigation.io import hash_file
from cansniff.investigation.model import new_id


class InvestigationPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.capture_path = os.path.join(self.directory.name, "evidence.asc")
        with open(self.capture_path, "wb") as handle:
            handle.write(b"capture evidence\n" * 8192)
        self.capture = attach_capture(self.capture_path, now="fixed")

    def large_project(self):
        annotations = tuple(
            Annotation(new_id(), self.capture.capture_id, index / 10.0, None,
                       "note {}".format(index), "fixed", "fixed", ("review",))
            for index in range(1000))
        bookmarks = tuple(
            Bookmark(new_id(), BookmarkKind.MESSAGE, "message {}".format(index),
                     self.capture.capture_id,
                     (("message_key", "can0:{:03X}:S".format(index % 2048)),),
                     "fixed")
            for index in range(1000))
        comparisons = tuple(
            ComparisonDefinition(new_id(), self.capture.capture_id,
                                 "Idle", index, index + 0.5,
                                 "Event", index + 1, index + 1.5, "fixed")
            for index in range(20))
        profile = {
            "name": "project profile",
            "signals": [{"name": "signal {}".format(index), "start": index % 64}
                        for index in range(500)],
            "definitions": [
                {"source": {"kind": "EDS",
                            "display_name": "node-{}.eds".format(index),
                            "content_hash": "{:064x}".format(index + 1)}}
                for index in range(12)],
        }
        project = new_project("Large investigation", "fixed")
        return project.changed(
            captures=(self.capture,), active_capture_id=self.capture.capture_id,
            comparisons=comparisons, annotations=annotations, bookmarks=bookmarks,
            selected_message_keys=("can0:123:S",), active_workspace="Compare",
            profile_snapshot_json=json.dumps(profile, sort_keys=True,
                                             separators=(",", ":")))

    def test_large_project_round_trip_remains_bounded(self):
        project = self.large_project()
        path = os.path.join(self.directory.name, "large.cansniff-project")
        started = time.perf_counter()
        save_project(project, path)
        loaded = load_project(path)
        elapsed = time.perf_counter() - started
        self.assertEqual(loaded.project, project)
        self.assertLess(os.path.getsize(path), 4 * 1024 * 1024)
        self.assertLess(elapsed, 5.0)

    def test_report_with_many_user_items_is_linear_enough(self):
        project = self.large_project()
        started = time.perf_counter()
        report = generate_markdown_report(project, ReportContext("fixed"))
        elapsed = time.perf_counter() - started
        self.assertEqual(report.count("_(User annotation)_"), 1000)
        self.assertIn("message 999", report)
        self.assertLess(elapsed, 3.0)

    def test_capture_hash_is_cached_by_identity_metadata(self):
        # attach_capture has already populated this path's cache. A repeated
        # request therefore must not instantiate another digest calculation.
        with patch("cansniff.investigation.io.hashlib.sha256",
                   wraps=hashlib.sha256) as digest:
            expected = self.capture.content_hash
            self.assertEqual(hash_file(self.capture_path), expected)
            self.assertEqual(hash_file(self.capture_path), expected)
        self.assertEqual(digest.call_count, 0)


if __name__ == "__main__":
    unittest.main()
