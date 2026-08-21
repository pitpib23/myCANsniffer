"""Exact metrics, ranking, caching, and structural false-positive guards."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from cansniff.analysis.matching import (
    MatchConflictKind, ProfileMatchCache, ProfileMatchLevel,
    build_observed_facts, candidate_set_identity,
)
from cansniff.analysis.profile import TrafficProfileAccumulator
from cansniff.analysis.signals import Profile, ProfileStore, Signal
from cansniff.analysis.store import FrameStore
from cansniff.model import CanFrame


def frame(timestamp, arb_id, length=8, channel="can0", extended=False,
          fd=False, value=0):
    return CanFrame(timestamp, arb_id, bytes([value & 0xFF]) * length,
                    length, channel=channel, is_extended=extended, is_fd=fd)


def traffic(frames):
    accumulator = TrafficProfileAccumulator()
    store = FrameStore(max(100, len(frames)))
    accumulator.update(frames)
    store.add(frames)
    return accumulator.snapshot(store)


def profile(name, messages, source_kind="MANUAL"):
    result = Profile(name)
    for arb_id, extended, length, fd in messages:
        result.signals.append(Signal(
            "M{:X}".format(arb_id), can_id=arb_id, is_extended=extended,
            start=0, length=8, message_name="M{:X}".format(arb_id),
            message_length=length, message_is_fd=fd,
            source_kind=source_kind))
    return result


class ProfileMatchingTests(unittest.TestCase):
    def test_exact_match_exposes_each_coverage_formula(self):
        observed = traffic([
            frame(0, 0x100), frame(1, 0x100), frame(2, 0x200),
        ])
        candidate = profile("Exact", [
            (0x100, False, 8, False), (0x200, False, 8, False)])
        snapshot = ProfileMatchCache().build(observed, ProfileStore([candidate]))
        match = snapshot.candidates[0]
        self.assertEqual(match.level, ProfileMatchLevel.STRONG)
        self.assertEqual((match.coverage.matched_observed_keys,
                          match.coverage.observed_keys), (2, 2))
        self.assertEqual((match.coverage.matched_defined_keys,
                          match.coverage.defined_keys), (2, 2))
        self.assertEqual((match.coverage.matched_frames,
                          match.coverage.observed_frames), (3, 3))
        self.assertEqual(match.coverage.observed_key_coverage, 1.0)
        self.assertEqual(match.coverage.definition_key_coverage, 1.0)
        self.assertEqual(match.coverage.weighted_frame_coverage, 1.0)

    def test_unmatched_proprietary_traffic_remains_visible(self):
        observed = traffic([frame(0, 0x100), frame(1, 0x315), frame(2, 0x420)])
        candidate = profile("Partial", [(0x100, False, 8, False)])
        match = ProfileMatchCache().build(
            observed, ProfileStore([candidate])).candidates[0]
        self.assertEqual(set(match.unmatched_observed),
                         {"can0:315:S", "can0:420:S"})
        self.assertAlmostEqual(match.coverage.observed_key_coverage, 1 / 3)

    def test_one_high_rate_id_does_not_outrank_full_correct_profile(self):
        frames = [frame(index / 1000, 0x100) for index in range(1000)]
        for offset in range(20):
            frames.append(frame(2 + offset, 0x200 + offset))
        observed = traffic(frames)
        high_rate_only = profile("One fast ID", [(0x100, False, 8, False)])
        full = profile("Full", [(0x100, False, 8, False)] + [
            (0x200 + offset, False, 8, False) for offset in range(20)])
        matches = ProfileMatchCache().build(
            observed, ProfileStore([high_rate_only, full])).candidates
        self.assertEqual(matches[0].profile_id, full.profile_id)
        high = next(item for item in matches if item.profile_id == high_rate_only.profile_id)
        self.assertGreater(high.coverage.weighted_frame_coverage, 0.95)
        self.assertLess(high.coverage.balanced_frame_coverage, 0.2)
        self.assertEqual(high.level, ProfileMatchLevel.WEAK)

    def test_same_numeric_id_wrong_standard_extended_is_conflict_not_match(self):
        observed = traffic([frame(0, 0x123, extended=True)])
        candidate = profile("Wrong format", [(0x123, False, 8, False)])
        match = ProfileMatchCache().build(
            observed, ProfileStore([candidate])).candidates[0]
        self.assertEqual(match.level, ProfileMatchLevel.NONE)
        self.assertEqual(match.coverage.matched_observed_keys, 0)
        self.assertIn(MatchConflictKind.ID_FORMAT,
                      {item.kind for item in match.conflicts})

    def test_dlc_and_fd_mismatches_are_explicit(self):
        observed = traffic([frame(0, 0x123, length=32, fd=True)])
        candidate = profile("Classic short", [(0x123, False, 8, False)])
        match = ProfileMatchCache().build(
            observed, ProfileStore([candidate])).candidates[0]
        kinds = {item.kind for item in match.conflicts}
        self.assertIn(MatchConflictKind.PAYLOAD_LENGTH, kinds)
        self.assertIn(MatchConflictKind.FRAME_FORMAT, kinds)
        self.assertEqual(match.coverage.structural_compatibility, 0.0)

    def test_channel_specific_manual_definition_does_not_cross_channels(self):
        observed = traffic([frame(0, 0x123, channel="can1")])
        candidate = profile("Channel-specific", [(0x123, False, 8, False)])
        candidate.signals[0].channel = "can0"
        match = ProfileMatchCache().build(
            observed, ProfileStore([candidate])).candidates[0]
        self.assertEqual(match.coverage.matched_observed_keys, 0)

    def test_silent_capture_produces_no_suggestions(self):
        snapshot = ProfileMatchCache().build(
            traffic([]), ProfileStore([profile("A", [(0x100, False, 8, False)])]))
        self.assertEqual(snapshot.candidates, ())
        self.assertTrue(any("no usable traffic" in item.lower()
                            for item in snapshot.caveats))

    def test_ranking_and_tie_break_are_deterministic_by_stable_identity(self):
        observed = traffic([frame(0, 0x100)])
        left = profile("Same", [(0x100, False, 8, False)])
        right = profile("Same", [(0x100, False, 8, False)])
        store = ProfileStore([right, left])
        cache = ProfileMatchCache()
        first = cache.build(observed, store)
        second = cache.build(observed, store)
        self.assertIs(first, second)
        self.assertEqual([item.profile_id for item in first.candidates],
                         sorted((left.profile_id, right.profile_id)))

    def test_cache_invalidates_after_profile_change(self):
        observed = traffic([frame(0, 0x100), frame(1, 0x200)])
        candidate = profile("Mutable", [(0x100, False, 8, False)])
        store = ProfileStore([candidate])
        cache = ProfileMatchCache()
        before_identity = candidate_set_identity(store)
        first = cache.build(observed, store)
        candidate.add_signal(0x200, signal=Signal(
            "Second", start=0, length=8, message_length=8))
        after_identity = candidate_set_identity(store)
        second = cache.build(observed, store)
        self.assertNotEqual(before_identity, after_identity)
        self.assertIsNot(first, second)
        self.assertEqual(second.candidates[0].coverage.matched_observed_keys, 2)

    def test_cache_does_not_reuse_provenance_across_capture_identities(self):
        observed = traffic([frame(0, 0x100)])
        candidate = profile("Candidate", [(0x100, False, 8, False)])
        cache, store = ProfileMatchCache(), ProfileStore([candidate])
        first = cache.build(observed, store, capture_identity="a" * 64)
        second = cache.build(observed, store, capture_identity="b" * 64)
        self.assertIsNot(first, second)
        self.assertEqual(second.capture_identity, "b" * 64)

    def test_models_are_immutable_and_names_do_not_affect_scoring(self):
        observed = traffic([frame(0, 0x100)])
        candidate = profile("Engine magic words", [(0x100, False, 8, False)])
        match = ProfileMatchCache().build(
            observed, ProfileStore([candidate])).candidates[0]
        candidate.name = "Nothing semantic"
        renamed = ProfileMatchCache().build(
            observed, ProfileStore([candidate])).candidates[0]
        self.assertEqual(match.rank_value, renamed.rank_value)
        with self.assertRaises(FrozenInstanceError):
            match.rank_value = 0


if __name__ == "__main__":
    unittest.main()
