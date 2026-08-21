"""Structural high-rate checks; deliberately no fragile wall-clock limits."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis.profile import TrafficProfileAccumulator  # noqa: E402
from cansniff.analysis.store import FrameStore  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402


class TrafficProfileScaleTests(unittest.TestCase):
    def test_many_frames_on_one_id_keep_only_bounded_payload_cardinality(self):
        profile = TrafficProfileAccumulator(unique_payload_limit=256)
        batch_size = 1000
        for start in range(0, 50000, batch_size):
            profile.update(
                CanFrame((start + i) * 0.0001, 0x123,
                         (start + i).to_bytes(4, "little"), 4, channel="can0")
                for i in range(batch_size)
            )
        result = profile.snapshot()
        self.assertEqual(result.processed_frames, 50000)
        self.assertEqual(result.unique_message_keys, 1)
        self.assertEqual(result.messages[0].count, 50000)
        self.assertEqual(result.messages[0].unique_payloads, 256)
        self.assertFalse(result.messages[0].unique_payloads_complete)

    def test_many_unique_extended_ids_have_one_accumulator_per_key(self):
        profile = TrafficProfileAccumulator()
        profile.update(
            CanFrame(i * 0.001, i, b"\x00", 1, is_extended=True, channel="can0")
            for i in range(5000)
        )
        result = profile.snapshot()
        self.assertEqual(result.unique_message_keys, 5000)
        self.assertEqual(len(result.messages), 5000)
        self.assertEqual(result.extended_frames, 5000)
        self.assertTrue(all(item.count == 1 for item in result.messages))

    def test_mixed_fd_batches_and_store_at_capacity_remain_consistent(self):
        profile = TrafficProfileAccumulator()
        store = FrameStore(10000)
        for start in range(0, 20000, 500):
            batch = [
                CanFrame((start + i) * 0.00001, 0x100 + i % 32,
                         bytes([(start + i) & 0xFF]) * 64, 15,
                         is_extended=bool(i % 2), is_fd=True,
                         is_bitrate_switch=bool(i % 3), channel="can0")
                for i in range(500)
            ]
            profile.update(batch)
            store.add(batch)
        result = profile.snapshot(store)
        self.assertEqual(result.processed_frames, 20000)
        self.assertEqual(result.fd_frames, 20000)
        self.assertEqual(result.retained_horizon.frame_count, 10000)
        self.assertFalse(result.retained_horizon.complete)
        self.assertEqual(store.total_seen, 20000)


if __name__ == "__main__":
    unittest.main()
