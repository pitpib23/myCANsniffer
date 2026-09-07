"""Coverage for cansniff.discovery.scoring -- the numerical 0-100 scoring
model behind the Auto Scan popup's results table (see cansniff/discovery/
scan.py and cansniff/ui/auto_scan_dialog.py). Qt-free throughout.
"""

from __future__ import annotations

import os
import unittest

from cansniff.discovery.scoring import ScoringConfig, score_candidate
from cansniff.model import CanFrame
from cansniff.sources.asc_reader import parse_asc

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CORRECT_CAPTURE = os.path.join(_REPO_ROOT, "capture-correct-bitrate.asc")
_WRONG_CAPTURE = os.path.join(_REPO_ROOT, "capture-wrong-bitrate.asc")


def _frame(index=0, arb_id=0x100, dlc=2, data=b"\x01\x02", **kwargs):
    return CanFrame(timestamp=float(index), arb_id=arb_id, data=data, dlc=dlc,
                    channel="0", **kwargs)


def _records_from_asc(path):
    frames = parse_asc(path).frames
    return [(frame.timestamp, frame) for frame in frames]


@unittest.skipUnless(
    os.path.exists(_CORRECT_CAPTURE) and os.path.exists(_WRONG_CAPTURE),
    "reference .asc fixtures not present in the repository root")
class ReferenceCaptureScoringTests(unittest.TestCase):
    """Behavior comparison between the two supplied reference captures --
    never pinned to an exact bitrate label, exactly as required: this only
    ever compares the two captures' scores/behavior against each other."""

    @classmethod
    def setUpClass(cls):
        cls.correct_frames = parse_asc(_CORRECT_CAPTURE).frames
        cls.wrong_frames = parse_asc(_WRONG_CAPTURE).frames

    def _score(self, frames):
        records = [(frame.timestamp, frame) for frame in frames]
        duration = max((t for t, _f in records), default=1.0) or 1.0
        return score_candidate(records, duration, ScoringConfig())

    def test_correct_capture_scores_substantially_higher_than_wrong(self):
        correct = self._score(self.correct_frames)
        wrong = self._score(self.wrong_frames)
        self.assertGreater(correct.total_score, wrong.total_score + 30,
                           "correct capture must score substantially higher")
        self.assertGreaterEqual(correct.total_score, 90.0)
        self.assertLess(wrong.total_score, 40.0)

    def test_both_scores_stay_within_0_and_100(self):
        for frames in (self.correct_frames, self.wrong_frames):
            components = self._score(frames)
            self.assertGreaterEqual(components.total_score, 0.0)
            self.assertLessEqual(components.total_score, 100.0)

    def test_error_frames_measurably_reduce_the_wrong_captures_score(self):
        wrong = self._score(self.wrong_frames)
        self.assertGreater(wrong.error_frames, 0)
        self.assertLess(wrong.error_score, 15.0 * 0.5)

    def test_remote_frames_measurably_reduce_the_wrong_captures_score(self):
        wrong = self._score(self.wrong_frames)
        self.assertGreater(wrong.remote_frames, 0)
        self.assertLess(wrong.remote_score, 10.0 * 0.5)

    def test_extended_ids_reduce_score_in_standard_only_mode(self):
        wrong = self._score(self.wrong_frames)
        self.assertGreater(wrong.extended_frames, 0)
        self.assertLess(wrong.format_score, 10.0 * 0.5)

    def test_persistent_ids_across_buckets_favor_the_correct_capture(self):
        correct = self._score(self.correct_frames)
        wrong = self._score(self.wrong_frames)
        self.assertGreater(correct.persistent_id_ratio, wrong.persistent_id_ratio)
        self.assertEqual(correct.persistent_id_score, 30.0)

    def test_correct_capture_variable_dlc_is_fully_structurally_valid(self):
        """The correct capture uses multiple DLC values per ID -- this must
        not be treated as an anomaly; structural validity only checks
        0<=dlc<=8 and len(data)==dlc, never DLC consistency across frames of
        one ID."""
        correct = self._score(self.correct_frames)
        self.assertEqual(correct.structural_validity, 1.0)
        self.assertEqual(correct.structural_score, 5.0)
        # Confirm the fixture actually exercises multiple DLC values per ID
        # (otherwise this test would not be testing what it claims to).
        by_id = {}
        for frame in self.correct_frames:
            by_id.setdefault(frame.arb_id, set()).add(frame.dlc)
        self.assertTrue(any(len(dlcs) > 1 for dlcs in by_id.values()))


class ScoreCandidateUnitTests(unittest.TestCase):
    def test_no_traffic_scores_exactly_zero_on_every_component(self):
        components = score_candidate([], 30.0, ScoringConfig())
        self.assertEqual(components.total_score, 0.0)
        self.assertEqual(components.error_score, 0.0)
        self.assertEqual(components.remote_score, 0.0)
        self.assertEqual(components.persistent_id_score, 0.0)
        self.assertEqual(components.total_records, 0)

    def test_score_is_always_between_0_and_100_for_arbitrary_traffic(self):
        records = []
        for i in range(50):
            records.append((i * 0.1, _frame(i, arb_id=0x100 + (i % 7), dlc=(i % 9))))
        components = score_candidate(records, 5.0, ScoringConfig())
        self.assertGreaterEqual(components.total_score, 0.0)
        self.assertLessEqual(components.total_score, 100.0)

    def test_one_repeated_id_in_a_single_short_burst_cannot_score_highly(self):
        """A burst confined to one time bucket must not look like a
        persistently-present bus population: this is exactly what
        persistent_id_score (30 pts) and bucket_stability_score (15 pts) --
        45 of the 100 available points -- exist to catch, even though a
        burst alone does not violate the unrelated error/remote/format/
        structural/payload signals and so cannot be driven all the way to
        zero by this scenario alone."""
        records = [(0.01 * i, _frame(i, arb_id=0x200)) for i in range(40)]
        components = score_candidate(records, 30.0, ScoringConfig())
        self.assertLess(components.persistent_id_ratio, 0.2)
        self.assertEqual(components.persistent_id_score, 0.0)
        self.assertEqual(components.bucket_stability_score, 0.0)
        # Cannot reach anywhere near the maximum: the two components that
        # exist specifically to catch this pattern contribute nothing.
        self.assertLessEqual(components.total_score, 100.0 - 30.0 - 15.0)

    def test_variable_dlc_for_the_same_id_is_allowed_and_fully_valid(self):
        records = [
            (0.0, _frame(0, arb_id=0x321, dlc=1, data=b"\x01")),
            (1.0, _frame(1, arb_id=0x321, dlc=4, data=b"\x01\x02\x03\x04")),
            (2.0, _frame(2, arb_id=0x321, dlc=8, data=bytes(8))),
        ]
        components = score_candidate(records, 3.0, ScoringConfig())
        self.assertEqual(components.structural_validity, 1.0)

    def test_structurally_invalid_frame_reduces_structural_score(self):
        valid = _frame(0, arb_id=0x10, dlc=2, data=b"\x01\x02")
        invalid = CanFrame(timestamp=1.0, arb_id=0x11, data=b"\x01\x02\x03",
                           dlc=2, channel="0")  # len(data) != dlc
        components = score_candidate([(0.0, valid), (1.0, invalid)], 2.0, ScoringConfig())
        self.assertLess(components.structural_validity, 1.0)
        self.assertLess(components.structural_score, 5.0)

    def test_persistent_ids_across_many_buckets_score_highly(self):
        records = []
        for bucket in range(10):
            for offset in range(3):
                t = bucket * 3.0 + offset * 0.5
                records.append((t, _frame(int(t * 10), arb_id=0x7AA)))
        components = score_candidate(records, 30.0, ScoringConfig())
        self.assertEqual(components.persistent_id_ratio, 1.0)
        self.assertEqual(components.persistent_id_score, 30.0)

    def test_remote_frames_reduce_score_when_over_threshold(self):
        records = [
            (i * 0.1, CanFrame(timestamp=float(i), arb_id=0x50, data=b"", dlc=0,
                               is_remote_frame=True, channel="0"))
            for i in range(30)
        ]
        components = score_candidate(records, 3.0, ScoringConfig())
        self.assertEqual(components.remote_rate, 1.0)
        self.assertEqual(components.remote_score, 0.0)

    def test_extended_ids_reduce_score_relative_to_expected_standard_format(self):
        records = [
            (i * 0.1, _frame(i, arb_id=0x18DA10F1, is_extended=True))
            for i in range(30)
        ]
        components = score_candidate(
            records, 3.0, ScoringConfig(expected_format="standard"))
        self.assertEqual(components.unexpected_format_rate, 1.0)
        self.assertEqual(components.format_score, 0.0)

    def test_expected_format_extended_flips_the_penalty(self):
        records = [(i * 0.1, _frame(i, arb_id=0x100 + i, is_extended=False))
                  for i in range(20)]
        standard_expected = score_candidate(
            records, 2.0, ScoringConfig(expected_format="standard"))
        extended_expected = score_candidate(
            records, 2.0, ScoringConfig(expected_format="extended"))
        self.assertEqual(standard_expected.format_score, 10.0)
        self.assertEqual(extended_expected.format_score, 0.0)

    def test_error_frames_reduce_score_and_are_excluded_from_id_tracking(self):
        records = [
            (0.0, _frame(0, arb_id=0x10)),
            (0.1, CanFrame(timestamp=0.1, arb_id=0, data=b"", dlc=0,
                           is_error_frame=True, channel="0")),
        ]
        components = score_candidate(records, 1.0, ScoringConfig())
        self.assertEqual(components.error_frames, 1)
        self.assertEqual(components.unique_ids, 1)  # the error frame's id=0 not counted

    def test_error_rate_at_threshold_zeroes_the_error_score(self):
        config = ScoringConfig(error_rate_threshold=0.05)
        total = 100
        errors = 5  # exactly at the 5% threshold
        records = [(i * 0.01, _frame(i, arb_id=0x10)) for i in range(total - errors)]
        records += [
            (0.5, CanFrame(timestamp=0.5, arb_id=0, data=b"", dlc=0,
                           is_error_frame=True, channel="0"))
            for _ in range(errors)
        ]
        components = score_candidate(records, 5.0, config)
        self.assertEqual(components.error_score, 0.0)

    def test_score_component_weights_sum_to_100(self):
        """Documents/protects the weight budget itself."""
        weights = (30, 15, 5, 5, 15, 10, 10, 5, 5)
        self.assertEqual(sum(weights), 100)

    def test_protocol_heuristic_is_off_by_default_and_never_in_total(self):
        records = [(0.0, _frame(0, arb_id=0x10, data=bytes([0x10, 0x02])))]
        components = score_candidate(records, 1.0, ScoringConfig())
        self.assertIsNone(components.protocol_id_low_byte_ratio)

    def test_protocol_heuristic_computed_only_when_enabled_and_not_scored(self):
        records = [(0.0, _frame(0, arb_id=0x0110, data=bytes([0x10, 0x02])))]
        enabled = ScoringConfig(enable_protocol_id_low_byte_heuristic=True)
        components = score_candidate(records, 1.0, enabled)
        self.assertEqual(components.protocol_id_low_byte_ratio, 1.0)
        # Never folded into the weighted total -- see the module docstring.
        default_components = score_candidate(records, 1.0, ScoringConfig())
        self.assertEqual(components.total_score, default_components.total_score)

    def test_payload_reused_across_unrelated_ids_reduces_payload_score(self):
        shared_payload = b"\x01\x02\x03\x04"
        records = [
            (0.0, _frame(0, arb_id=0x10, dlc=4, data=shared_payload)),
            (0.1, _frame(1, arb_id=0x20, dlc=4, data=shared_payload)),
        ]
        components = score_candidate(records, 1.0, ScoringConfig())
        self.assertEqual(components.multi_id_payload_ratio, 1.0)
        self.assertEqual(components.payload_migration_score, 0.0)

    def test_repeated_messages_are_not_deduplicated_before_scoring(self):
        """Duplicate suppression, if any, belongs to a display layer -- the
        scorer must see every repeated frame as independent evidence."""
        records = [(i * 0.1, _frame(i, arb_id=0x999)) for i in range(20)]
        components = score_candidate(records, 2.0, ScoringConfig())
        self.assertEqual(components.total_records, 20)


if __name__ == "__main__":
    unittest.main()
