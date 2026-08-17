"""Tests for the shared frame store and per-byte Range State.

All fixtures are constructed in-process. Nothing here opens a CAN interface.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis.stats import RangeStateCache, range_state  # noqa: E402
from cansniff.analysis.store import FrameStore  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402


def _frame(t, arb=0x100, data=b"\x00", ch="1", ext=False, fd=False):
    return CanFrame(timestamp=t, arb_id=arb, data=bytes(data), dlc=len(data),
                    channel=ch, is_extended=ext, is_fd=fd)


class FrameStoreTests(unittest.TestCase):
    def test_groups_by_channel_id_and_addressing(self):
        store = FrameStore()
        store.add([
            _frame(0.0, 0x100, ch="1"),
            _frame(0.1, 0x100, ch="2"),          # different channel
            _frame(0.2, 0x100, ch="1", ext=True),  # different addressing
            _frame(0.3, 0x100, ch="1"),
        ])
        self.assertEqual(len(store.keys()), 3)
        self.assertEqual(len(store.frames_for("1:100:S")), 2)
        self.assertEqual(len(store.frames_for("2:100:S")), 1)
        self.assertEqual(len(store.frames_for("1:00000100:X")), 1)

    def test_keys_are_in_first_seen_order(self):
        store = FrameStore()
        store.add([_frame(0.0, 0x300), _frame(0.1, 0x100), _frame(0.2, 0x200)])
        self.assertEqual(store.keys(), ["1:300:S", "1:100:S", "1:200:S"])

    def test_revision_changes_only_on_mutation(self):
        store = FrameStore()
        start = store.revision
        store.add([_frame(0.0)])
        after = store.revision
        self.assertNotEqual(after, start)
        store.add([])                       # nothing added
        self.assertEqual(store.revision, after)
        store.clear()
        self.assertNotEqual(store.revision, after)

    def test_eviction_is_bounded_and_prunes_empty_keys(self):
        store = FrameStore(max_frames=100)
        store.add([_frame(i * 0.01, 0x100) for i in range(60)])
        store.add([_frame(1.0 + i * 0.01, 0x200) for i in range(80)])
        self.assertEqual(len(store), 100)
        # The oldest 40 of 0x100 are gone; the key survives with what is left.
        self.assertEqual(len(store.frames_for("1:100:S")), 20)
        self.assertEqual(len(store.frames_for("1:200:S")), 80)
        self.assertEqual(store.total_seen, 140)

        store.add([_frame(2.0 + i * 0.01, 0x300) for i in range(100)])
        self.assertEqual(len(store), 100)
        self.assertNotIn("1:100:S", store.keys(), "exhausted key must be dropped")

    def test_window_bounds_are_inclusive(self):
        store = FrameStore()
        store.add([_frame(i / 10.0) for i in range(10)])
        window = store.window(start=0.2, end=0.5)
        self.assertEqual([round(f.timestamp, 1) for f in window],
                         [0.2, 0.3, 0.4, 0.5])

    def test_reversed_window_yields_nothing_rather_than_swapping(self):
        store = FrameStore()
        store.add([_frame(i / 10.0) for i in range(10)])
        self.assertEqual(len(store.window(start=0.8, end=0.2)), 0)

    def test_open_ended_windows(self):
        store = FrameStore()
        store.add([_frame(i / 10.0) for i in range(10)])
        self.assertEqual(len(store.window(start=0.7)), 3)
        self.assertEqual(len(store.window(end=0.2)), 3)
        self.assertEqual(len(store.window()), 10)

    def test_window_can_restrict_to_one_key(self):
        store = FrameStore()
        store.add([_frame(0.1, 0x100), _frame(0.2, 0x200), _frame(0.3, 0x100)])
        window = store.window(key="1:100:S")
        self.assertEqual(len(window), 2)

    def test_time_span_and_empty_store(self):
        store = FrameStore()
        self.assertEqual(store.time_span(), (None, None))
        store.add([_frame(1.5), _frame(9.5)])
        self.assertEqual(store.time_span(), (1.5, 9.5))

    def test_store_holds_references_not_copies(self):
        """Frames are shared with the tables; copying them would multiply memory."""
        store = FrameStore()
        frame = _frame(0.0)
        store.add([frame])
        self.assertIs(store.frames_for("1:100:S")[0], frame)


class RangeStateTests(unittest.TestCase):
    def _window(self, frames):
        store = FrameStore()
        store.add(frames)
        return store.all_frames()

    def test_exact_min_max_distinct_and_samples(self):
        window = self._window([
            _frame(0.0, data=[0x10, 0x00]),
            _frame(0.1, data=[0x20, 0x00]),
            _frame(0.2, data=[0x10, 0xFF]),
        ])
        state = range_state(window)
        self.assertEqual(state.frames, 3)

        first = state.stat_for(0)
        self.assertEqual((first.minimum, first.maximum), (0x10, 0x20))
        self.assertEqual(first.distinct, 2)
        self.assertEqual(first.samples, 3)

        second = state.stat_for(1)
        self.assertEqual((second.minimum, second.maximum), (0x00, 0xFF))
        self.assertEqual(second.distinct, 2)

    def test_absent_bytes_are_not_counted_as_zero(self):
        """A short frame has no sample at the higher positions — not a zero."""
        window = self._window([
            _frame(0.0, data=[0x05, 0x05, 0x05]),
            _frame(0.1, data=[0x07]),          # bytes 1 and 2 absent
        ])
        state = range_state(window)
        byte0, byte1 = state.stat_for(0), state.stat_for(1)
        self.assertEqual(byte0.samples, 2)
        self.assertEqual(byte1.samples, 1, "absent byte must not add a sample")
        self.assertEqual(byte1.minimum, 0x05,
                         "minimum must not be dragged to 0 by an absent byte")
        self.assertEqual(byte1.distinct, 1)

    def test_varying_length_is_reported(self):
        window = self._window([
            _frame(0.0, data=[0] * 8),
            _frame(0.1, data=[0] * 3),
            _frame(0.2, data=[0] * 8),
        ])
        state = range_state(window)
        self.assertTrue(state.varying_length)
        self.assertEqual(state.lengths, {8: 2, 3: 1})
        self.assertEqual(state.max_length, 8)

    def test_constant_byte_is_identifiable(self):
        window = self._window([_frame(i / 10.0, data=[0xAA, i]) for i in range(20)])
        state = range_state(window)
        self.assertTrue(state.stat_for(0).constant)
        self.assertFalse(state.stat_for(1).constant)
        self.assertEqual(state.stat_for(1).distinct, 20)

    def test_no_samples_is_empty_not_an_error(self):
        state = range_state(self._window([]))
        self.assertTrue(state.empty)
        self.assertEqual(state.bytes, [])
        self.assertEqual(state.max_length, 0)

    def test_payloadless_frames_count_as_observations_only(self):
        """Error and remote frames are seen, but say nothing about the data."""
        window = self._window([
            _frame(0.0, data=[0x11, 0x22]),
            CanFrame(timestamp=0.1, arb_id=0x100, data=b"", dlc=0,
                     channel="1", is_error_frame=True),
        ])
        state = range_state(window)
        self.assertEqual(state.frames, 2)
        self.assertEqual(state.stat_for(0).samples, 1)

    def test_key_restriction_ignores_other_messages(self):
        window = self._window([
            _frame(0.0, 0x100, data=[0x01]),
            _frame(0.1, 0x200, data=[0xFF]),
            _frame(0.2, 0x100, data=[0x03]),
        ])
        state = range_state(window, key="1:100:S")
        self.assertEqual(state.frames, 2)
        self.assertEqual(state.stat_for(0).maximum, 0x03)

    def test_full_byte_range(self):
        window = self._window([_frame(i / 100.0, data=[i]) for i in range(256)])
        stat = range_state(window).stat_for(0)
        self.assertEqual((stat.minimum, stat.maximum, stat.distinct),
                         (0, 255, 256))

    def test_extended_and_standard_are_separate_statistics(self):
        window = self._window([
            _frame(0.0, 0x100, data=[0x01]),
            _frame(0.1, 0x100, data=[0xF0], ext=True),
        ])
        std = range_state(window, key="1:100:S")
        ext = range_state(window, key="1:00000100:X")
        self.assertEqual(std.stat_for(0).maximum, 0x01)
        self.assertEqual(ext.stat_for(0).maximum, 0xF0)


class RangeStateCacheTests(unittest.TestCase):
    def test_repeat_request_is_served_from_cache(self):
        store = FrameStore()
        store.add([_frame(i / 10.0, data=[i]) for i in range(50)])
        cache = RangeStateCache()
        window = store.all_frames()
        first = cache.get(window)
        self.assertIs(cache.get(window), first, "identical window must not recompute")

    def test_new_frames_invalidate_the_cache(self):
        store = FrameStore()
        store.add([_frame(0.0, data=[1])])
        cache = RangeStateCache()
        before = cache.get(store.all_frames())
        store.add([_frame(0.1, data=[9])])
        after = cache.get(store.all_frames())
        self.assertIsNot(after, before)
        self.assertEqual(after.stat_for(0).maximum, 9)

    def test_cache_is_bounded(self):
        store = FrameStore()
        cache = RangeStateCache(limit=4)
        for i in range(20):
            store.add([_frame(i / 10.0, data=[i])])
            cache.get(store.all_frames())
        self.assertLessEqual(len(cache._entries), 4)


if __name__ == "__main__":
    unittest.main()
