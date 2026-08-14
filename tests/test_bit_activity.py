"""Tests for the per-bit activity window and the matrix that displays it.

Every fixture is constructed in-process from synthetic payloads whose field
shapes are known in advance. Nothing here opens a CAN interface.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False

from cansniff.model import CanFrame, FrameStats  # noqa: E402

if HAVE_QT:
    from cansniff.config import Config
    from cansniff.ui.interpret_view import InterpretView
    from cansniff.ui.theme import Theme
    from cansniff.ui.widgets import BitMatrix


def _feed(payloads, bit_window=512):
    """Run payloads through a FrameStats and return it."""
    frames = [
        CanFrame(timestamp=i * 0.01, arb_id=0x2A0, data=data,
                 dlc=len(data), channel="1")
        for i, data in enumerate(payloads)
    ]
    stats = FrameStats(key=frames[0].key, frame=frames[0], bit_window=bit_window)
    for frame in frames:
        stats.update(frame)
    return stats


def _flips(stats, byte_index):
    """Flip counts for one byte, most significant bit first."""
    base = byte_index * 8
    return [stats.bit_flips[base + bit] for bit in range(7, -1, -1)]


class BitFlipCountTests(unittest.TestCase):
    """The counts have to be exactly right; the whole display rests on them."""

    def test_rolling_counter_halves_each_bit_up(self):
        # A byte counting 0..255 flips bit 0 on every increment, bit 1 on every
        # second one, and so on: bit k flips 2**(8-k) - 1 times. That halving
        # staircase is the signature the matrix exists to make visible, so it
        # is worth pinning exactly.
        stats = _feed([bytes([i & 0xFF]) for i in range(256)], bit_window=1024)
        self.assertEqual(_flips(stats, 0), [1, 3, 7, 15, 31, 63, 127, 255])

    def test_constant_payload_never_flips(self):
        stats = _feed([b"\x5A\x00"] * 200)
        self.assertEqual(stats.bit_flips, [0] * 16)

    def test_single_flag_bit_is_isolated(self):
        payloads = [bytes([0x08 if (i // 10) % 2 else 0x00]) for i in range(100)]
        stats = _feed(payloads)
        counts = _flips(stats, 0)
        self.assertEqual(counts[4], 9, "bit 3 should be the only one moving")
        self.assertEqual([counts[i] for i in (0, 1, 2, 3, 5, 6, 7)], [0] * 7)

    def test_counts_are_per_bit_not_per_byte(self):
        # 0x01 -> 0x02 changes two bits in one byte.
        stats = _feed([b"\x01", b"\x02"])
        self.assertEqual(_flips(stats, 0), [0, 0, 0, 0, 0, 0, 1, 1])

    def test_window_is_bounded_and_reported(self):
        stats = _feed([bytes([i & 0xFF]) for i in range(4000)], bit_window=100)
        self.assertGreaterEqual(stats.window_frames, 100)
        self.assertLessEqual(stats.window_frames, 200,
                             "window must not grow without bound")
        # Bit 0 flips every frame, so its count tracks the reported window.
        self.assertEqual(stats.bit_flips[0], stats.window_frames)

    def test_old_activity_ages_out(self):
        """A bit that stops moving must fade, or the display never recovers."""
        busy = [bytes([i & 0x01]) for i in range(400)]
        quiet = [b"\x00"] * 4000
        stats = _feed(busy + quiet, bit_window=200)
        self.assertEqual(stats.bit_flips[0], 0)

    def test_payload_length_change_restarts_cleanly(self):
        stats = _feed([b"\xFF\xFF", b"\x00\x00", b"\xFF"])
        self.assertEqual(len(stats.bit_flips), 8)
        self.assertEqual(stats.bit_flips, [0] * 8)

    def test_zero_window_disables_tracking_but_keeps_byte_mask(self):
        stats = _feed([bytes([i & 0xFF, 0x00]) for i in range(50)], bit_window=0)
        self.assertEqual(stats.bit_flips, [])
        self.assertEqual(list(stats.changed_mask), [1, 0],
                         "per-byte change tracking must survive")

    def test_window_memory_does_not_grow_with_capture_length(self):
        def churn(n):
            return [bytes((i + j) % 256 for j in range(8)) for i in range(n)]

        short = _feed(churn(600))
        long = _feed(churn(20000))
        self.assertEqual(len(long._current) + len(long._previous),
                         len(short._current) + len(short._previous))


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class BitMatrixTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Config.defaults(
            os.path.join(os.environ.get("TEMP", "."), "cansniff_bits_test.json")
        )
        self.view = InterpretView(self.config, Theme())

    def tearDown(self):
        self.view.deleteLater()
        self.app.processEvents()

    def _show(self, payloads):
        stats = _feed(payloads)
        self.view.show_frame(stats.frame, stats)
        self.app.processEvents()
        return stats

    def test_matrix_receives_the_flip_counts(self):
        stats = self._show([bytes([i & 0xFF, 0x00]) for i in range(300)])
        self.assertEqual(list(self.view.bit_matrix._flips), stats.bit_flips)
        self.assertEqual(self.view.bit_matrix._window, stats.window_frames)

    def test_never_flipped_and_flipped_once_are_distinguishable(self):
        """A rare bit must not render identically to a dead one."""
        matrix = self.view.bit_matrix
        matrix.set_payload(b"\x00", [0] * 8, 500)
        dead = matrix._intensity(0)
        rare = matrix._intensity(1)
        self.assertEqual(dead, 0.0)
        self.assertGreater(rare, 0.12)

    def test_intensity_rises_with_flip_count(self):
        matrix = self.view.bit_matrix
        matrix.set_payload(b"\x00", [0] * 8, 512)
        steps = [matrix._intensity(n) for n in (1, 8, 64, 256, 512)]
        self.assertEqual(steps, sorted(steps))
        self.assertEqual(steps[-1], 1.0)

    def test_selecting_a_block_brackets_the_same_bytes_as_the_strip(self):
        self._show([bytes([i & 0xFF] * 8) for i in range(50)])
        self.view.table.selectRow(2)
        self.app.processEvents()
        self.assertEqual(self.view.bit_matrix._highlight,
                         self.view.strip._highlight)

    def test_toggle_persists_to_config(self):
        self.view.bits_toggle.setChecked(False)
        self.app.processEvents()
        self.assertFalse(self.config.get("ui.show_bit_activity"))
        self.assertFalse(self.view.bit_matrix.isVisibleTo(self.view))
        self.view.bits_toggle.setChecked(True)
        self.assertTrue(self.config.get("ui.show_bit_activity"))

    def test_collapsing_hides_the_matrix_but_keeps_the_raw_bytes(self):
        self._show([bytes(range(8))] * 20)
        self.view.bits_toggle.setChecked(False)
        self.app.processEvents()
        self.assertFalse(self.view.matrix_scroll.isVisibleTo(self.view))
        self.assertTrue(self.view.strip.isVisibleTo(self.view),
                        "the raw bytes must survive a collapse")
        self.assertEqual(len(self.view.strip._data), 8)

        self.view.bits_toggle.setChecked(True)
        self.app.processEvents()
        self.assertTrue(self.view.matrix_scroll.isVisibleTo(self.view))

    def test_the_disclosure_stays_reachable_when_collapsed(self):
        """It is the only way back, so it must not hide with its contents."""
        self._show([bytes(range(8))] * 20)
        self.view.bits_toggle.setChecked(False)
        self.app.processEvents()
        self.assertTrue(self.view.bits_toggle.isVisibleTo(self.view))
        self.assertFalse(self.view.bits_legend.isVisibleTo(self.view))
        self.assertFalse(self.view.bits_caption.isVisibleTo(self.view))

    def test_the_chevron_reflects_the_state(self):
        self.view.bits_toggle.setChecked(True)
        self.assertIn("▾", self.view.bits_toggle.text())
        self.view.bits_toggle.setChecked(False)
        self.assertIn("▸", self.view.bits_toggle.text())

    def test_collapsing_gives_its_height_back(self):
        self.view.resize(1100, 700)
        self.view.show()
        self._show([bytes(range(8))] * 20)
        tall = self.view.payload_detail.height()
        self.assertGreater(tall, 0)
        self.view.bits_toggle.setChecked(False)
        self.app.processEvents()
        self.assertLess(self.view.payload_detail.height(), tall)
        self.view.hide()

    def test_matrix_columns_align_with_the_strip(self):
        self._show([bytes(range(8))] * 20)
        self.assertEqual(self.view.bit_matrix._cell_width,
                         self.view.strip._cell_width)
        self.assertEqual(self.view.bit_matrix.sizeHint().width(),
                         self.view.strip.sizeHint().width())

    def test_long_fd_payload_scrolls_instead_of_clipping(self):
        self._show([bytes((i + j) % 256 for j in range(64)) for i in range(40)])
        self.app.processEvents()
        matrix = self.view.bit_matrix
        self.assertEqual(len(matrix._data), 64)
        self.assertGreater(matrix.sizeHint().width(),
                           self.view.matrix_scroll.viewport().width())

    def test_strip_and_matrix_scroll_together(self):
        """They only stay lined up if one bar drives the other."""
        self.view.resize(700, 700)
        self.view.show()
        self._show([bytes((i + j) % 256 for j in range(64)) for i in range(40)])
        self.app.processEvents()

        strip_bar = self.view.strip_scroll.horizontalScrollBar()
        matrix_bar = self.view.matrix_scroll.horizontalScrollBar()
        self.assertGreater(matrix_bar.maximum(), 0, "payload should overflow")
        self.assertEqual(strip_bar.maximum(), matrix_bar.maximum(),
                         "different ranges would drift out of alignment")

        matrix_bar.setValue(120)
        self.app.processEvents()
        self.assertEqual(strip_bar.value(), 120)

        strip_bar.setValue(40)
        self.app.processEvents()
        self.assertEqual(matrix_bar.value(), 40)
        self.view.hide()

    def test_binary_column_is_gone(self):
        """The matrix replaced it, so it must not linger as a decoder."""
        from cansniff.interpret import DECODERS
        self.assertNotIn("bin_be", DECODERS)
        self._show([bytes(range(8))] * 10)
        headers = [self.view.table.horizontalHeaderItem(c).text()
                   for c in range(self.view.table.columnCount())]
        self.assertNotIn("Binary", headers)


if __name__ == "__main__":
    unittest.main()
