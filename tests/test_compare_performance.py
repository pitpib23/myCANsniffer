"""Structural scale guards without environment-dependent time limits."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis.compare import (  # noqa: E402
    ComparisonCache, ComparisonInput, ComparisonWindow, pearson_correlation,
)
from cansniff.analysis.compare.candidates import (  # noqa: E402
    MAX_CANDIDATES_PER_MESSAGE, MAX_INTERESTING_BYTES,
)
from cansniff.analysis.profile import TrafficProfileAccumulator  # noqa: E402
from cansniff.analysis.series import Series  # noqa: E402
from cansniff.analysis.store import FrameStore  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402


def build(frames, baseline, event):
    store = FrameStore(len(frames))
    store.add(frames)
    profile_builder = TrafficProfileAccumulator()
    profile_builder.update(frames)
    request = ComparisonInput(
        ComparisonWindow("Baseline", baseline[0], baseline[1], store.revision),
        ComparisonWindow("Event", event[0], event[1], store.revision),
    )
    profile = profile_builder.snapshot(store)
    cache = ComparisonCache()
    return cache, store, profile, request


class ComparisonScaleTests(unittest.TestCase):
    def test_many_frames_and_ids_remain_revision_cached(self):
        frames = []
        for index in range(20000):
            condition = 0 if index < 10000 else 1
            local = index if not condition else index - 10000
            arb = 0x100 + local % 20
            timestamp = local * 0.001 + condition * 20.0
            payload = bytes((condition, local & 0xFF, (local >> 8) & 0xFF, 0))
            frames.append(CanFrame(timestamp, arb, payload, 4, channel="can0"))
        frames.sort(key=lambda item: item.timestamp)
        cache, store, profile, request = build(frames, (0, 9.999), (20, 29.999))
        first = cache.build(store, request, profile)
        second = cache.build(store, request, profile)
        self.assertIs(second, first)
        self.assertEqual(len(first.messages), 20)
        self.assertTrue(all(len(first.candidates_for(message.key))
                            <= MAX_CANDIDATES_PER_MESSAGE
                            for message in first.messages))

    def test_many_unique_ids_bound_candidate_ownership_to_top_messages(self):
        frames = []
        for index in range(500):
            frames.append(CanFrame(index * 0.001, index,
                                   bytes((index & 0xFF,)), 1,
                                   is_extended=True, channel="can0"))
            frames.append(CanFrame(10 + index * 0.001, index,
                                   bytes(((index + 1) & 0xFF,)), 1,
                                   is_extended=True, channel="can0"))
        frames.sort(key=lambda item: item.timestamp)
        cache, store, profile, request = build(frames, (0, 0.499), (10, 10.499))
        result = cache.build(store, request, profile)
        self.assertEqual(len(result.messages), 500)
        self.assertLessEqual(len({item.message_key for item in result.candidates}), 32)

    def test_fd_analysis_considers_only_bounded_interesting_regions(self):
        frames = []
        for condition, start in ((0, 0.0), (1, 20.0)):
            for sample in range(1000):
                data = bytes(((sample + byte + condition * 17) & 0xFF)
                             for byte in range(64))
                frames.append(CanFrame(start + sample * 0.001, 0x123, data, 15,
                                       is_fd=True, channel="can0"))
        cache, store, profile, request = build(frames, (0, 0.999), (20, 20.999))
        result = cache.build(store, request, profile)
        self.assertLessEqual(len(result.candidates), MAX_CANDIDATES_PER_MESSAGE)
        covered = {item.byte_start for item in result.candidates}
        self.assertLessEqual(len(covered), MAX_INTERESTING_BYTES)

    def test_long_selected_correlation_is_linear_not_all_pairs(self):
        count = 20000
        times = [index * 0.001 for index in range(count)]
        left = Series("A", times, [float(index) for index in range(count)])
        right = Series("B", [time + 0.0001 for time in times],
                       [float(index * 2) for index in range(count)])
        result = pearson_correlation(left, right, tolerance=0.0002)
        self.assertEqual(result.paired_samples, count)
        self.assertAlmostEqual(result.coefficient, 1.0)


if __name__ == "__main__":
    unittest.main()
