import unittest

from cansniff.qualification.runner import EntryResult, ExpectationOutcome, _metrics


def outcome(fact, mode, expected, actual, passed):
    return ExpectationOutcome(fact, mode, expected, actual, passed)


class QualificationMetricTests(unittest.TestCase):
    def test_false_positive_levels_are_reported(self):
        entry = EntryResult("negative", "FAIL", "CAPTURE", True, "a" * 64, 0,
                            {}, (outcome("protocol.UDS.level", "ABSENT",
                                         "Possible", "Strong", False),))
        metric = _metrics((entry,))["protocol_false_positives"]
        self.assertEqual(metric["count"], 1)
        self.assertEqual(metric["observed_levels"]["Strong"], 1)
        self.assertEqual(metric["unexpected_levels"]["Strong"], 1)

    def test_lower_confidence_positive_is_partial_not_missed(self):
        entry = EntryResult("positive", "FAIL", "CAPTURE", True, "a" * 64, 0,
                            {}, (outcome("protocol.CANopen.level", "MINIMUM",
                                         "Strong", "Weak", False),))
        metric = _metrics((entry,))["protocol_false_negatives"]
        self.assertEqual((metric["detected"], metric["partial"], metric["missed"]),
                         (0, 1, 0))

    def test_labeled_pair_counts_are_separate_from_decode(self):
        facts = {"diagnostics.pairs": 2,
                 "diagnostics.conversations.ambiguous_correlation": 1}
        entry = EntryResult("diag", "FAIL", "CAPTURE", True, "a" * 64, 0,
                            facts, (outcome("diagnostics.pairs", "EXACT",
                                            1, 2, False),))
        metric = _metrics((entry,))["diagnostic_correlation"]
        self.assertEqual((metric["tp"], metric["fp"], metric["fn"],
                          metric["ambiguous"]), (1, 1, 0, 1))


if __name__ == "__main__":
    unittest.main()
