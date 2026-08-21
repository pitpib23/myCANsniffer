import hashlib
import json
import os
import tempfile
import unittest

from cansniff.qualification.manifest import load_qualification_manifest
from cansniff.qualification.model import QualificationLevel
from cansniff.qualification.runner import run_manifest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASELINE = os.path.join(ROOT, "qualification", "manifests", "software-baseline.json")


class ManifestAndRunnerTests(unittest.TestCase):
    def test_golden_manifest_runs_offline_and_is_software_only(self):
        report = run_manifest(BASELINE)
        self.assertTrue(report.passed)
        self.assertEqual(len(report.entries), 5)
        self.assertEqual({record.level for record in report.records},
                         {QualificationLevel.SOFTWARE_TESTED})
        self.assertEqual(report.metrics["protocol_false_positives"]["count"], 0)
        self.assertNotIn("HARDWARE_TESTED", report.to_markdown())

    def test_hash_mismatch_is_refused_before_file_parser(self):
        manifest = load_qualification_manifest(BASELINE).to_dict()
        manifest["entries"] = [dict(manifest["entries"][0])]
        manifest["entries"][0]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "manifest.json")
            manifest["entries"][0]["path"] = os.path.abspath(
                os.path.join(ROOT, "tests", "qualification", "fixtures",
                             "synthetic_protocols.log"))
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle)
            report = run_manifest(path)
        self.assertFalse(report.passed)
        self.assertIn("mismatch", report.entries[0].error)
        self.assertEqual(report.entries[0].facts, {})

    def test_manifest_rejects_remote_paths_and_missing_permission(self):
        raw = load_qualification_manifest(BASELINE).to_dict()
        entry = dict(raw["entries"][0]); entry["path"] = "https://example.invalid/a.asc"
        raw["entries"] = [entry]
        with self.assertRaises(ValueError):
            load_from_raw(raw)
        entry["path"] = "\\\\server\\share\\capture.asc"
        with self.assertRaises(ValueError):
            load_from_raw(raw)
        entry["path"] = "a.asc"; entry["license_or_permission"] = ""
        with self.assertRaises(ValueError):
            load_from_raw(raw)

    def test_manifest_rejects_duplicate_json_keys(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as handle:
            handle.write('{"schema_version":1,"schema_version":1}')
            path = handle.name
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with self.assertRaises(ValueError):
            load_qualification_manifest(path)


def load_from_raw(raw):
    from cansniff.qualification.manifest import QualificationManifest
    return QualificationManifest.from_dict(raw)


if __name__ == "__main__":
    unittest.main()
