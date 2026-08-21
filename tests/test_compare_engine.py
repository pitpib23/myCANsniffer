"""Factual window comparison, ranking, validation, and caching."""

from __future__ import annotations

import math
import os
import sys
import unittest
from dataclasses import FrozenInstanceError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis.compare import (  # noqa: E402
    ComparisonCache, ComparisonCancelled, ComparisonInput,
    ComparisonValidationError, ComparisonWindow, PresenceChange,
    build_comparison,
)
from cansniff.analysis.profile import (  # noqa: E402
    CaptureIntegrityAccumulator, TrafficProfileAccumulator,
)
from cansniff.analysis.store import FrameStore  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402


def frame(timestamp, arb, data, channel="can0", extended=False, fd=False,
          error=False):
    payload = bytes(data)
    return CanFrame(timestamp, arb, payload, len(payload),
                    is_extended=extended, is_fd=fd,
                    is_error_frame=error, channel=channel)


def compare(frames, baseline=(0.0, 9.0), event=(20.0, 29.0),
            max_frames=10000, ui_dropped=0):
    frames = sorted(frames, key=lambda item: item.timestamp)
    store = FrameStore(max_frames)
    store.add(frames)
    profiler = TrafficProfileAccumulator()
    profiler.update(frames)
    integrity_builder = CaptureIntegrityAccumulator()
    integrity = integrity_builder.snapshot(
        len(frames), len(store),
        current={"received": len(frames) + ui_dropped,
                 "accepted": len(frames) + ui_dropped,
                 "ui_dropped": ui_dropped},
    )
    profile = profiler.snapshot(store, integrity)
    request = ComparisonInput(
        ComparisonWindow("Baseline", baseline[0], baseline[1], store.revision),
        ComparisonWindow("Event", event[0], event[1], store.revision),
    )
    return build_comparison(store, request, profile), store, profile, request


class ComparisonModelTests(unittest.TestCase):
    def test_snapshots_and_nested_models_are_immutable(self):
        frames = [frame(i, 0x100, [1]) for i in range(10)]
        frames += [frame(20 + i, 0x100, [2]) for i in range(10)]
        result, _store, _profile, _request = compare(frames)
        with self.assertRaises(FrozenInstanceError):
            result.generated_from_revision = 99
        with self.assertRaises(FrozenInstanceError):
            result.messages[0].ranking_score = 0

    def test_identical_conditions_rank_none_with_factual_counts(self):
        frames = [frame(i, 0x100, [7, 8]) for i in range(10)]
        frames += [frame(20 + i, 0x100, [7, 8]) for i in range(10)]
        result, _store, _profile, _request = compare(frames)
        message = result.messages[0]
        self.assertEqual(message.ranking_score, 0.0)
        self.assertEqual((message.baseline_count, message.event_count), (10, 10))
        self.assertEqual(message.presence_change, PresenceChange.UNCHANGED)

    def test_changed_byte_and_bit_are_explicit(self):
        frames = [frame(i, 0x101, [0x00, 0]) for i in range(10)]
        frames += [frame(20 + i, 0x101, [0x20, 0]) for i in range(10)]
        result, _store, _profile, _request = compare(frames)
        message = result.messages[0]
        self.assertEqual(message.bytes[0].distribution_distance, 1.0)
        bit = next(item for item in message.bits
                   if item.byte_index == 0 and item.bit_index == 5)
        self.assertEqual((bit.baseline_one_fraction, bit.event_one_fraction),
                         (0.0, 1.0))
        self.assertTrue(any("bit 5" in reason for reason in message.reasons))

    def test_new_and_removed_ids_are_both_reported(self):
        frames = [frame(i, 0x100, [1]) for i in range(5)]
        frames += [frame(20 + i, 0x200, [2]) for i in range(5)]
        result, _store, _profile, _request = compare(frames, event=(20, 24))
        self.assertEqual(result.message_for("can0:100:S").presence_change,
                         PresenceChange.REMOVED)
        self.assertEqual(result.message_for("can0:200:S").presence_change,
                         PresenceChange.NEW)

    def test_rate_and_period_shift_use_normalized_metrics(self):
        baseline_frames = [frame(i, 0x100, [1]) for i in range(10)]
        event_frames = [frame(20 + i * 0.1, 0x100, [1]) for i in range(10)]
        result, _store, _profile, _request = compare(
            baseline_frames + event_frames, event=(20, 20.9))
        message = result.messages[0]
        self.assertAlmostEqual(message.baseline_rate_hz, 1.0)
        self.assertAlmostEqual(message.event_rate_hz, 10.0)
        self.assertAlmostEqual(message.normalized_rate_delta, 0.9)
        self.assertAlmostEqual(message.period_delta, -0.9)
        decreased, _store, _profile, _request = compare(
            [frame(i * 0.1, 0x100, [1]) for i in range(10)]
            + [frame(20 + i, 0x100, [1]) for i in range(10)])
        self.assertLess(decreased.messages[0].rate_delta_hz, 0)

    def test_variable_dlc_records_missing_bytes_without_padding_zero(self):
        baseline_frames = [frame(i, 0x100, [1, 9] if i % 2 else [1])
                           for i in range(10)]
        event_frames = [frame(20 + i, 0x100, [1, 7]) for i in range(10)]
        result, _store, _profile, _request = compare(
            baseline_frames + event_frames)
        byte = result.messages[0].bytes[1]
        self.assertEqual((byte.baseline_samples, byte.baseline_missing), (5, 5))
        self.assertEqual(byte.baseline_minimum, 9)
        self.assertNotEqual(byte.baseline_minimum, 0)

    def test_channel_format_and_fd_identity_remain_distinct(self):
        frames = []
        for i in range(5):
            frames += [frame(i, 0x123, [i], "a"),
                       frame(i, 0x123, [i], "b", extended=True, fd=True),
                       frame(20 + i, 0x123, [i + 1], "a"),
                       frame(20 + i, 0x123, [i + 1], "b", extended=True, fd=True)]
        result, _store, _profile, _request = compare(frames, event=(20, 24))
        self.assertEqual(len(result.messages), 2)
        self.assertEqual({item.key for item in result.messages},
                         {"a:123:S", "b:00000123:X"})
        self.assertTrue(result.message_for("b:00000123:X").is_fd)


class RankingTests(unittest.TestCase):
    def test_repeatable_payload_change_outranks_unchanged_high_rate_noise(self):
        frames = []
        for i in range(100):
            frames.append(frame(i * 0.09, 0x100, [0xAA]))
            frames.append(frame(20 + i * 0.09, 0x100, [0xAA]))
        for i in range(10):
            frames.append(frame(i, 0x200, [0]))
            frames.append(frame(20 + i, 0x200, [1]))
        result, _store, _profile, _request = compare(frames)
        self.assertEqual(result.messages[0].key, "can0:200:S")
        self.assertEqual(result.message_for("can0:100:S").ranking_score, 0.0)

    def test_one_new_frame_does_not_outrank_strong_repeatable_change(self):
        frames = [frame(i, 0x100, [0]) for i in range(10)]
        frames += [frame(20 + i, 0x100, [0xFF]) for i in range(10)]
        frames.append(frame(25, 0x050, [1]))
        result, _store, _profile, _request = compare(frames)
        self.assertEqual(result.messages[0].key, "can0:100:S")
        self.assertTrue(any("sparse" in reason for reason in
                            result.message_for("can0:050:S").reasons))

    def test_ties_are_deterministic_by_message_key(self):
        frames = []
        for arb in (0x200, 0x100):
            frames += [frame(i, arb, [0]) for i in range(5)]
            frames += [frame(20 + i, arb, [1]) for i in range(5)]
        first, _store, _profile, _request = compare(frames, event=(20, 24))
        second, _store, _profile, _request = compare(
            list(reversed(frames)), event=(20, 24))
        self.assertEqual([item.key for item in first.messages],
                         ["can0:100:S", "can0:200:S"])
        self.assertEqual([item.key for item in first.messages],
                         [item.key for item in second.messages])


class ValidationAndCacheTests(unittest.TestCase):
    def test_evicted_baseline_is_refused_not_silently_compared(self):
        frames = [frame(float(i), 0x100, [i & 0xFF]) for i in range(150)]
        store = FrameStore(100)
        store.add(frames)
        profiler = TrafficProfileAccumulator()
        profiler.update(frames)
        request = ComparisonInput(
            ComparisonWindow("Baseline", 0, 20, store.revision),
            ComparisonWindow("Event", 120, 140, store.revision),
        )
        with self.assertRaisesRegex(ComparisonValidationError, "evicted"):
            build_comparison(store, request, profiler.snapshot(store))

    def test_zero_frame_interval_is_rejected(self):
        frames = [frame(0, 0x100, [1]), frame(1, 0x100, [1]),
                  frame(2, 0x100, [1])]
        with self.assertRaisesRegex(ComparisonValidationError, "no usable"):
            compare(frames, baseline=(0.25, 0.75), event=(1, 2))

    def test_nonfinite_and_reversed_bounds_are_rejected(self):
        frames = [frame(0, 0x100, [1]), frame(10, 0x100, [1])]
        with self.assertRaisesRegex(ComparisonValidationError, "non-finite"):
            compare(frames, baseline=(math.nan, 1), event=(2, 10))
        with self.assertRaisesRegex(ComparisonValidationError, "precedes"):
            compare(frames, baseline=(5, 2), event=(5, 10))

    def test_integrity_loss_and_unavailable_driver_visibility_are_caveated(self):
        frames = [frame(i, 0x100, [0]) for i in range(10)]
        frames += [frame(20 + i, 0x100, [1]) for i in range(10)]
        result, _store, _profile, _request = compare(frames, ui_dropped=2)
        text = " ".join(result.caveats)
        self.assertIn("dropped", text)
        self.assertIn("unavailable", text)

    def test_cache_reuses_exact_inputs_and_invalidates_on_revision(self):
        frames = [frame(i, 0x100, [0]) for i in range(10)]
        frames += [frame(20 + i, 0x100, [1]) for i in range(10)]
        _result, store, profile, request = compare(frames)
        cache = ComparisonCache()
        first = cache.build(store, request, profile)
        self.assertIs(cache.build(store, request, profile), first)
        extra = frame(30, 0x100, [1])
        store.add([extra])
        profiler = TrafficProfileAccumulator()
        profiler.update(frames + [extra])
        updated_request = ComparisonInput(
            ComparisonWindow("Baseline", 0, 9, store.revision),
            ComparisonWindow("Event", 20, 29, store.revision),
        )
        second = cache.build(store, updated_request, profiler.snapshot(store))
        self.assertIsNot(second, first)

    def test_cooperative_cancellation_returns_no_partial_snapshot(self):
        frames = [frame(i, 0x100, [i & 0xFF]) for i in range(20)]
        frames += [frame(30 + i, 0x100, [i & 0xFF]) for i in range(20)]
        _result, store, profile, request = compare(
            frames, baseline=(0, 19), event=(30, 49))
        with self.assertRaises(ComparisonCancelled):
            build_comparison(store, request, profile, cancelled=lambda: True)


if __name__ == "__main__":
    unittest.main()
