"""Signal time-series extraction.

Synthetic sequences with known shapes: constant, ramp, counter, sawtooth,
toggle, noise and sparse. Nothing here opens a CAN interface.
"""

from __future__ import annotations

import math
import os
import random
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis.series import (  # noqa: E402
    DISPLAY_LIMIT, Series, SeriesCache, block_series, dbc_series,
)
from cansniff.analysis.store import FrameStore  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

try:
    import cantools  # noqa: F401
    from cansniff.analysis.dbc import DbcDatabase
    HAVE_CANTOOLS = True
except ImportError:  # pragma: no cover
    HAVE_CANTOOLS = False

KEY = "1:100:S"


def _window(payloads, arb=0x100, step=0.01):
    store = FrameStore()
    store.add([
        CanFrame(timestamp=i * step, arb_id=arb, data=bytes(p), dlc=len(p),
                 channel="1")
        for i, p in enumerate(payloads)
    ])
    return store.all_frames()


class BlockSeriesTests(unittest.TestCase):
    def test_constant_signal(self):
        series = block_series(_window([[0x00, 0x64]] * 50), KEY, 0, 2, "u16_be")
        self.assertEqual(len(series), 50)
        self.assertEqual(set(series.values), {100.0})
        low, high = series.value_range
        self.assertLess(low, high, "a flat series still needs a visible axis")

    def test_ramp_is_monotonic(self):
        payloads = [[0x00, i] for i in range(200)]
        series = block_series(_window(payloads), KEY, 0, 2, "u16_be")
        self.assertEqual(series.values, sorted(series.values))
        self.assertEqual(series.values[0], 0.0)
        self.assertEqual(series.values[-1], 199.0)

    def test_counter_wraps(self):
        payloads = [[i & 0xFF] for i in range(600)]
        series = block_series(_window(payloads), KEY, 0, 1, "u8")
        self.assertEqual(len(series), 600)
        self.assertEqual(max(series.values), 255.0)
        self.assertEqual(min(series.values), 0.0)

    def test_sawtooth_and_toggle_shapes_survive(self):
        saw = block_series(_window([[i % 32] for i in range(320)]), KEY, 0, 1, "u8")
        self.assertEqual(saw.values[:3], [0.0, 1.0, 2.0])
        toggle = block_series(_window([[i % 2] for i in range(100)]), KEY, 0, 1, "u8")
        self.assertEqual(set(toggle.values), {0.0, 1.0})

    def test_timestamps_are_ordered_and_match_frames(self):
        series = block_series(_window([[i] for i in range(20)]), KEY, 0, 1, "u8")
        self.assertEqual(series.times, sorted(series.times))
        self.assertAlmostEqual(series.times[0], 0.0)
        self.assertAlmostEqual(series.times[-1], 0.19)

    def test_time_base_makes_timestamps_relative(self):
        series = block_series(_window([[i] for i in range(5)]), KEY, 0, 1, "u8",
                              time_base=0.02)
        self.assertAlmostEqual(series.times[0], -0.02)

    def test_absent_block_is_skipped_not_zero(self):
        """A short frame has no value there; plotting 0 would be a fabrication."""
        payloads = [[0x00, 0x05], [0x01], [0x00, 0x09]]
        series = block_series(_window(payloads), KEY, 0, 2, "u16_be")
        self.assertEqual(len(series), 2)
        self.assertEqual(series.skipped, 1)
        self.assertNotIn(0.0, series.values)

    def test_other_messages_are_ignored(self):
        store = FrameStore()
        store.add([
            CanFrame(timestamp=0.0, arb_id=0x100, data=b"\x05", dlc=1, channel="1"),
            CanFrame(timestamp=0.1, arb_id=0x200, data=b"\xFF", dlc=1, channel="1"),
        ])
        series = block_series(store.all_frames(), KEY, 0, 1, "u8")
        self.assertEqual(series.values, [5.0])

    def test_non_numeric_decoder_yields_an_empty_series(self):
        series = block_series(_window([[0x41, 0x42]] * 5), KEY, 0, 2, "ascii")
        self.assertTrue(series.empty)

    def test_unknown_decoder_is_not_fatal(self):
        series = block_series(_window([[0x01]] * 3), KEY, 0, 1, "nope")
        self.assertTrue(series.empty)

    def test_empty_window(self):
        series = block_series(_window([]), KEY, 0, 2, "u16_be")
        self.assertTrue(series.empty)
        self.assertEqual(series.time_range, (0.0, 1.0))


class DecimationTests(unittest.TestCase):
    def _noisy(self, n):
        random.seed(11)
        return _window([[random.getrandbits(8)] for _ in range(n)])

    def test_small_series_is_returned_unchanged(self):
        series = block_series(_window([[i] for i in range(100)]), KEY, 0, 1, "u8")
        self.assertIs(series.decimated(), series)

    def test_large_series_is_reduced_for_display(self):
        series = block_series(self._noisy(50000), KEY, 0, 1, "u8")
        self.assertEqual(len(series), 50000)
        small = series.decimated()
        self.assertLessEqual(len(small), DISPLAY_LIMIT)
        self.assertGreater(len(small), 0)

    def test_decimation_preserves_the_extremes(self):
        """Stride sampling hides spikes; a spike is what the user is hunting."""
        payloads = [[0x00] for _ in range(20000)]
        payloads[7777] = [0xFF]                     # a single isolated spike
        series = block_series(_window(payloads), KEY, 0, 1, "u8")
        small = series.decimated(limit=200)
        self.assertIn(255.0, small.values, "the spike must survive decimation")

    def test_decimation_keeps_time_order(self):
        series = block_series(self._noisy(20000), KEY, 0, 1, "u8")
        small = series.decimated()
        self.assertEqual(small.times, sorted(small.times))

    def test_full_resolution_data_is_not_mutated(self):
        series = block_series(self._noisy(20000), KEY, 0, 1, "u8")
        before = len(series)
        series.decimated()
        self.assertEqual(len(series), before)


@unittest.skipUnless(HAVE_CANTOOLS, "cantools not installed")
class DbcSeriesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = DbcDatabase.load(os.path.join(FIXTURES, "sample.dbc"))

    def _engine_window(self, rpms):
        store = FrameStore()
        for i, rpm in enumerate(rpms):
            raw = int(rpm / 0.25) & 0xFFFF
            payload = bytes([raw & 0xFF, (raw >> 8) & 0xFF,
                             105, 0x00, 0x00, 0x00, 0x00, 0x00])
            store.add([CanFrame(timestamp=i * 0.01, arb_id=0x100, data=payload,
                                dlc=8, channel="1")])
        return store.all_frames()

    def test_extracts_scaled_signal_with_unit(self):
        window = self._engine_window([1000, 2000, 3000])
        series = dbc_series(window, self.db, KEY, "Rpm")
        self.assertEqual(series.unit, "rpm")
        self.assertEqual([round(v) for v in series.values], [1000, 2000, 3000])
        self.assertEqual(series.label, "Rpm [rpm]")

    def test_smooth_shape_is_preserved(self):
        rpms = [2000 + 800 * math.sin(i / 20.0) for i in range(400)]
        series = dbc_series(self._engine_window(rpms), self.db, KEY, "Rpm")
        self.assertEqual(len(series), 400)
        low, high = series.value_range
        self.assertGreater(high - low, 1000)

    def test_undecodable_frames_are_skipped_not_zeroed(self):
        store = FrameStore()
        store.add([
            CanFrame(timestamp=0.0, arb_id=0x100, data=bytes([0x40, 0x1F] + [0] * 6),
                     dlc=8, channel="1"),
            CanFrame(timestamp=0.1, arb_id=0x100, data=b"\x01\x02", dlc=2,
                     channel="1"),          # too short to decode
        ])
        series = dbc_series(store.all_frames(), self.db, KEY, "Rpm")
        self.assertEqual(len(series), 1)
        self.assertEqual(series.skipped, 1)

    def test_multiplexed_signal_only_samples_its_own_branch(self):
        store = FrameStore()
        for i in range(10):
            selector = i % 2
            store.add([CanFrame(timestamp=i * 0.01, arb_id=0x200,
                                data=bytes([selector, 0xE8, 0x03, 0, 0, 0, 0, 0]),
                                dlc=8, channel="1")])
        window = store.all_frames()
        temp = dbc_series(window, self.db, "1:200:S", "TempA")
        volt = dbc_series(window, self.db, "1:200:S", "VoltB")
        self.assertEqual(len(temp), 5)
        self.assertEqual(len(volt), 5)
        self.assertEqual(temp.skipped, 5, "frames on the other branch are skipped")

    def test_enumerated_signal_carries_choice_text(self):
        store = FrameStore()
        for i in range(4):
            store.add([CanFrame(timestamp=i * 0.01, arb_id=0x101,
                                data=bytes([i, 0, 0, 0]), dlc=4, channel="1")])
        series = dbc_series(store.all_frames(), self.db, "1:101:S", "Mode")
        self.assertIsNotNone(series.choices)
        self.assertIn("Running", series.choices)

    def test_unknown_signal_name_yields_empty_series(self):
        series = dbc_series(self._engine_window([1000]), self.db, KEY, "NoSuch")
        self.assertTrue(series.empty)


class SeriesCacheTests(unittest.TestCase):
    def test_repeat_request_is_served_from_cache(self):
        window = _window([[i] for i in range(100)])
        cache = SeriesCache()
        key = ("x",) + window.cache_key
        built = []

        def build():
            built.append(1)
            return block_series(window, KEY, 0, 1, "u8")

        cache.get(build, key)
        cache.get(build, key)
        self.assertEqual(len(built), 1)

    def test_cache_is_bounded(self):
        cache = SeriesCache(limit=3)
        for i in range(10):
            cache.get(lambda: Series("s", [], []), ("k", i))
        self.assertLessEqual(len(cache._entries), 3)


if __name__ == "__main__":
    unittest.main()
