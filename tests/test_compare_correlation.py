"""Nearest-timestamp Pearson correlation with cautious edge handling."""

from __future__ import annotations

import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis.compare.correlation import (  # noqa: E402
    align_nearest, correlate_candidates, pearson_correlation,
)
from cansniff.analysis.compare.model import (  # noqa: E402
    CandidateEvidence, CandidateKind,
)
from cansniff.analysis.protocols.model import EvidenceLevel  # noqa: E402
from cansniff.analysis.series import Series  # noqa: E402
from cansniff.analysis.store import FrameStore  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402


def series(name, values, times=None, skipped=0):
    if times is None:
        times = [index * 0.1 for index in range(len(values))]
    return Series(name, list(times), list(values), skipped=skipped)


class PearsonTests(unittest.TestCase):
    def test_strong_positive_and_negative_correlation(self):
        x = series("x", list(range(20)))
        positive = pearson_correlation(x, series("y", [2 * i + 3 for i in range(20)]))
        negative = pearson_correlation(x, series("z", [-3 * i for i in range(20)]))
        self.assertAlmostEqual(positive.coefficient, 1.0)
        self.assertAlmostEqual(negative.coefficient, -1.0)
        self.assertEqual(positive.paired_samples, 20)
        self.assertTrue(any("not evidence of causation" in reason
                            for reason in positive.reasons))

    def test_unrelated_series_is_low_correlation(self):
        rng = random.Random(22)
        x = series("x", [rng.random() for _ in range(200)])
        y = series("y", [rng.random() for _ in range(200)])
        result = pearson_correlation(x, y)
        self.assertIsNotNone(result.coefficient)
        self.assertLess(abs(result.coefficient), 0.2)

    def test_constant_series_is_explicitly_undefined(self):
        result = pearson_correlation(series("x", [1] * 20),
                                     series("y", list(range(20))))
        self.assertIsNone(result.coefficient)
        self.assertTrue(any("constant" in reason for reason in result.reasons))

    def test_too_few_pairs_is_not_a_coefficient(self):
        result = pearson_correlation(series("x", [1, 2, 3]),
                                     series("y", [2, 4, 6]))
        self.assertIsNone(result.coefficient)
        self.assertEqual(result.paired_samples, 3)

    def test_alignment_uses_nearest_timestamp_with_tolerance_not_index(self):
        left = series("x", [1, 2, 3, 4, 5], [0, 1, 2, 3, 4])
        right = series("y", [10, 20, 30, 40, 50],
                       [0.02, 1.02, 2.02, 3.02, 4.02])
        xs, ys = align_nearest(left, right, 0.05)
        self.assertEqual((xs, ys), ([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]))
        self.assertEqual(align_nearest(left, right, 0.001), ([], []))

    def test_right_sample_is_never_reused(self):
        left = series("x", [1, 2], [0.99, 1.01])
        right = series("y", [10], [1.0])
        xs, ys = align_nearest(left, right, 0.1)
        self.assertEqual(len(xs), 1)
        self.assertEqual(len(ys), 1)

    def test_nonfinite_values_are_ignored(self):
        x = series("x", [1, 2, math.nan, 4, 5, 6])
        y = series("y", [2, 4, 6, math.inf, 10, 12])
        result = pearson_correlation(x, y, minimum_samples=4)
        self.assertEqual(result.paired_samples, 4)
        self.assertAlmostEqual(result.coefficient, 1.0)


class CandidateCorrelationTests(unittest.TestCase):
    def test_selected_fields_are_extracted_with_missing_counts(self):
        store = FrameStore()
        frames = []
        for index in range(10):
            frames.append(CanFrame(index * 0.1, 0x100,
                                   bytes((index,)), 1, channel="can0"))
            payload = bytes((index * 2,)) if index != 4 else b""
            frames.append(CanFrame(index * 0.1 + 0.01, 0x200,
                                   payload, len(payload), channel="can0"))
        store.add(sorted(frames, key=lambda item: item.timestamp))
        left = CandidateEvidence(
            "can0:100:S", CandidateKind.NUMERIC, EvidenceLevel.POSSIBLE,
            0, 1, ("fixture",), 10, 0, 50, "u8", "u8")
        right = CandidateEvidence(
            "can0:200:S", CandidateKind.NUMERIC, EvidenceLevel.POSSIBLE,
            0, 1, ("fixture",), 9, 1, 50, "u8", "u8")
        result = correlate_candidates(store.all_frames(), left, right, 0.02)
        self.assertEqual(result.paired_samples, 9)
        self.assertEqual(result.right_missing, 1)
        self.assertAlmostEqual(result.coefficient, 1.0)


if __name__ == "__main__":
    unittest.main()
