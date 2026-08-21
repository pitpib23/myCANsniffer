import math
import unittest

from cansniff.qualification.model import (
    EnvironmentReport, QualificationLevel, QualificationRecord,
    QualificationStatus, build_qualification_matrix,
)


ENV = EnvironmentReport("os", "py", "app", "commit", "qt", "can", "dbc", 1)


class QualificationModelTests(unittest.TestCase):
    def test_levels_are_ordered_and_matrix_uses_passing_evidence_only(self):
        records = [
            QualificationRecord("a", "capture", QualificationLevel.SOFTWARE_TESTED,
                                QualificationStatus.PASS, "t", "now", ENV),
            QualificationRecord("b", "capture", QualificationLevel.HARDWARE_TESTED,
                                QualificationStatus.FAIL, "t", "now", ENV),
        ]
        matrix = build_qualification_matrix(["capture", "electrical"], records)
        self.assertEqual(matrix[0].level, QualificationLevel.SOFTWARE_TESTED)
        self.assertEqual(matrix[1].level, QualificationLevel.UNTESTED)

    def test_record_round_trip_is_machine_readable(self):
        record = QualificationRecord("a", "x", QualificationLevel.CAPTURE_VALIDATED,
                                     QualificationStatus.PASS, "t", "now", ENV,
                                     limitations=("limited",), provenance=(("source", "lawful"),))
        self.assertEqual(QualificationRecord.from_dict(record.to_dict()), record)

    def test_nonfinite_duration_is_refused(self):
        raw = QualificationRecord("a", "x", QualificationLevel.SOFTWARE_TESTED,
                                  QualificationStatus.PASS, "t", "now", ENV).to_dict()
        raw["duration_seconds"] = math.nan
        with self.assertRaises(ValueError):
            QualificationRecord.from_dict(raw)


if __name__ == "__main__":
    unittest.main()
