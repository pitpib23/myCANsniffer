"""The plot's time window and its block-style selection.

A plot drawn over a whole capture collapses every signal into an unreadable
band, so the window defaults to the last minute and is sliced from the store by
timestamp. The selection works the way the Blocks table does: pick a block
size, click a block, choose how to read it.

Nothing here opens a CAN interface.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cansniff.analysis.store import FrameStore  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False

if HAVE_QT:
    from cansniff.config import Config
    from cansniff.ui.interpret_view import (
        _PLOT_WINDOWS, PLOT, InterpretView,
    )
    from cansniff.ui.theme import Theme


def _ramp(count, step=0.1, arb=0x100, size=8):
    """Frames one step apart, with a counter in bytes 0-1."""
    frames = []
    for i in range(count):
        payload = bytearray(size)
        payload[0] = (i >> 8) & 0xFF
        payload[1] = i & 0xFF
        frames.append(CanFrame(timestamp=i * step, arb_id=arb,
                               data=bytes(payload), dlc=size, channel="1"))
    return frames


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class PlotWindowTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Config.defaults(
            os.path.join(os.environ.get("TEMP", "."), "cansniff_plotwin.json"))
        self.store = FrameStore(100000)
        # 4000 frames at 10 Hz: 400 seconds, the span that made the axis
        # unreadable in the first place.
        self.frames = _ramp(4000, step=0.1)
        self.store.add(self.frames)
        self.view = InterpretView(self.config, Theme())
        self.addCleanup(self.view.deleteLater)
        self.view.set_frame_store(self.store)
        self.view.show_frame(self.frames[-1])
        self._switch()

    def _switch(self):
        self.view.view_tabs.set_current(PLOT)
        self.view._on_workspace_changed(PLOT)
        self.app.processEvents()

    def _set_window(self, seconds):
        for index, (_label, value) in enumerate(_PLOT_WINDOWS):
            if value == seconds:
                self.view._on_plot_window(index)
                self.app.processEvents()
                return
        self.fail("no such window: {}".format(seconds))

    # -- the window ------------------------------------------------------

    def test_the_default_window_is_sixty_seconds(self):
        self.assertEqual(self.view._plot_window_seconds(), 60.0)

    def test_the_default_does_not_draw_the_whole_capture(self):
        """400 seconds of capture must not land on the axis by default."""
        window = self.view._plot_window()
        covered = list(window)[-1].timestamp - list(window)[0].timestamp
        self.assertLess(covered, 61.0)
        self.assertGreater(covered, 58.0)
        self.assertLess(len(window), len(self.frames))

    def test_a_shorter_window_covers_less(self):
        self._set_window(10.0)
        window = self.view._plot_window()
        frames = list(window)
        self.assertLess(frames[-1].timestamp - frames[0].timestamp, 11.0)

    def test_the_all_window_covers_everything(self):
        self._set_window(0.0)
        self.assertEqual(len(self.view._plot_window()), len(self.frames))

    def test_the_window_ends_at_the_newest_frame(self):
        """Counted back from the newest frame, not from the clock."""
        window = self.view._plot_window()
        self.assertEqual(list(window)[-1].timestamp,
                         self.frames[-1].timestamp)

    def test_the_choice_is_remembered_in_the_config(self):
        self._set_window(30.0)
        self.assertEqual(self.config.get("interpret.plot_window_s"), 30.0)
        view = InterpretView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        self.assertEqual(view._plot_window_seconds(), 30.0)

    def test_a_corrupt_stored_window_falls_back_to_the_default(self):
        self.config.set("interpret.plot_window_s", 9999.0)
        self.assertEqual(self.view._plot_window_seconds(), 60.0)

    # -- what the note claims --------------------------------------------

    def test_the_note_reports_the_window_actually_covered(self):
        self._set_window(60.0)
        self.assertIn("last 60s", self.view.workspace_note.text())

    def test_the_note_admits_when_the_capture_is_shorter(self):
        """A 60s window over a 4s capture covers 4s, and must say so."""
        store = FrameStore(1000)
        frames = _ramp(40, step=0.1)
        store.add(frames)
        view = InterpretView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        view.set_frame_store(store)
        view.show_frame(frames[-1])
        view.view_tabs.set_current(PLOT)
        view._on_workspace_changed(PLOT)
        self.app.processEvents()
        note = view.workspace_note.text()
        self.assertIn("available", note)

    def test_the_note_counts_the_frames_in_the_window(self):
        self._set_window(10.0)
        note = self.view.workspace_note.text()
        self.assertIn("frames carried a value", note)

    # -- block-style selection -------------------------------------------

    def test_the_block_size_sets_the_width_taken_from_the_strip(self):
        self.view._on_plot_block_size(1)          # 2-byte blocks
        self.view._on_byte_clicked(0)
        self.app.processEvents()
        self.assertEqual(self.view._plot_source[:3], ("raw", 0, 2))

        self.view._on_plot_block_size(2)          # 4-byte blocks
        self.app.processEvents()
        self.assertEqual(self.view._plot_source[:3], ("raw", 0, 4))

    def test_changing_the_size_keeps_the_byte_being_looked_at(self):
        self.view._on_plot_block_size(0)          # 1 byte
        self.view._on_byte_clicked(2)
        self.app.processEvents()
        self.view._on_plot_block_size(1)          # 2 bytes
        self.app.processEvents()
        self.assertEqual(self.view._plot_source[:3], ("raw", 2, 2))

    def test_a_block_that_would_run_off_the_end_is_pulled_back(self):
        """The width is what the decoder reads; narrowing it silently is worse."""
        self.view._on_plot_block_size(2)          # 4 bytes, payload is 8
        self.view._on_byte_clicked(6)
        self.app.processEvents()
        self.assertEqual(self.view._plot_source[:3], ("raw", 4, 4))

    def test_a_payload_shorter_than_the_block_size_still_plots(self):
        frame = CanFrame(timestamp=500.0, arb_id=0x102, data=b"\x01\x02\x03",
                         dlc=3, channel="1")
        self.store.add([frame])
        self.view.show_frame(frame)
        self.view._on_plot_block_size(2)          # 4-byte blocks over 3 bytes
        self.app.processEvents()
        offset, length = self.view._plot_source[1], self.view._plot_source[2]
        self.assertLessEqual(offset + length, 3)

    def test_clicking_a_byte_plots_that_block_immediately(self):
        self.view._on_plot_block_size(1)
        self.view._on_byte_clicked(0)
        self.app.processEvents()
        self.assertEqual(self.view._plot_source[:3], ("raw", 0, 2))
        self.assertEqual(len(self.view.plot._series), 1)
        self.assertGreater(len(self.view.plot._series[0]), 0)

    def test_the_strip_brackets_the_block_being_plotted(self):
        self.view._on_plot_block_size(1)
        self.view._on_byte_clicked(2)
        self.app.processEvents()
        self.assertIn("2-3", self.view.selection_label.text())

    def test_the_plotted_values_are_the_selected_bytes(self):
        self.view._on_plot_block_size(1)
        self.view._on_byte_clicked(0)
        self.app.processEvents()
        series = self.view.plot._series[0]
        # Bytes 0-1 carry the frame counter, big endian.
        window = self.view._plot_window()
        expected = [float((f.data[0] << 8) | f.data[1]) for f in window]
        self.assertEqual(series.values[:5], expected[:5])

    def test_the_decoder_list_only_offers_readings_that_fit(self):
        self.view._on_plot_block_size(1)          # 2 bytes
        self.view._on_byte_clicked(0)
        self.app.processEvents()
        keys = {self.view.plot_decoder.itemData(i)
                for i in range(self.view.plot_decoder.count())}
        self.assertIn("u16_be", keys)
        self.assertNotIn("u32_be", keys, "a 32-bit read cannot fit two bytes")
        self.assertNotIn("f32_be", keys)

    def test_a_wider_block_unlocks_the_wider_decoders(self):
        self.view._on_plot_block_size(2)          # 4 bytes
        self.view._on_byte_clicked(0)
        self.app.processEvents()
        keys = {self.view.plot_decoder.itemData(i)
                for i in range(self.view.plot_decoder.count())}
        self.assertIn("u32_be", keys)
        self.assertIn("f32_be", keys)

    def test_changing_the_decoder_replots(self):
        self.view._on_plot_block_size(1)
        self.view._on_byte_clicked(0)
        self.app.processEvents()
        first = list(self.view.plot._series[0].values[:5])

        index = self.view.plot_decoder.findData("u16_le")
        self.assertGreaterEqual(index, 0)
        self.view.plot_decoder.setCurrentIndex(index)
        self.app.processEvents()
        self.assertEqual(self.view._plot_source[3], "u16_le")
        self.assertNotEqual(list(self.view.plot._series[0].values[:5]), first)

    def test_switching_message_keeps_the_block_inside_the_payload(self):
        short = CanFrame(timestamp=500.0, arb_id=0x199, data=b"\x01\x02",
                         dlc=2, channel="1")
        self.store.add([short])
        self.view.show_frame(short)
        self.app.processEvents()
        offset, length = self.view._plot_source[1], self.view._plot_source[2]
        self.assertLessEqual(offset + length, 2)

    def test_no_database_means_no_signal_list(self):
        self.assertEqual(self.view.plot_signal.count(), 0)
        self.assertFalse(self.view.plot_signal.isVisible())

    def test_the_raw_payload_stays_visible_while_plotting(self):
        self.view._on_byte_clicked(0)
        self.app.processEvents()
        self.assertEqual(len(self.view.strip._data), 8)


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class AxisLabelTests(unittest.TestCase):
    """Gridlines have to show distinct numbers.

    Regression: a signal moving by a few thousand around 2.16e9 rendered as
    five identical "2.16e+09" labels under a fixed "%.3g".
    """

    def _formats(self):
        from cansniff.ui.plot_view import _label_format
        return _label_format

    def _labels(self, low, high, count=5):
        fmt = self._formats()(low, high).replace("%.", "").replace("g", "")
        digits = int(fmt)
        step = (high - low) / (count - 1)
        return ["%.{}g".format(digits) % (low + i * step) for i in range(count)]

    def test_a_narrow_range_high_above_zero_gets_distinct_labels(self):
        labels = self._labels(2164259000.0, 2164261000.0)
        self.assertEqual(len(set(labels)), len(labels),
                         "gridlines are indistinguishable: {}".format(labels))

    def test_an_ordinary_range_keeps_short_labels(self):
        from cansniff.ui.plot_view import _label_format
        self.assertEqual(_label_format(0.0, 100.0), "%.3g")

    def test_a_flat_series_does_not_divide_by_zero(self):
        from cansniff.ui.plot_view import _label_format
        self.assertEqual(_label_format(5.0, 5.0), "%.3g")

    def test_a_range_spanning_zero_is_handled(self):
        from cansniff.ui.plot_view import _label_format
        self.assertTrue(_label_format(-50.0, 50.0).startswith("%."))
        self.assertTrue(_label_format(0.0, 0.0).startswith("%."))

    def test_precision_is_capped(self):
        """A double cannot back more than ~15 significant digits anyway."""
        from cansniff.ui.plot_view import _label_format
        self.assertEqual(_label_format(1e15, 1e15 + 1.0), "%.15g")

    def test_a_span_too_small_for_a_double_falls_back(self):
        """1e12 + 1e-6 *is* 1e12; there is no range to resolve."""
        from cansniff.ui.plot_view import _label_format
        self.assertEqual(_label_format(1e12, 1e12 + 1e-6), "%.3g")


if __name__ == "__main__":
    unittest.main()
