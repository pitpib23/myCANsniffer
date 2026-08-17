"""UI regression tests for payload block selection.

These run headless against the offscreen Qt platform. They construct frames
in-process and never open a CAN interface.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must be set before QApplication is created.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False

if HAVE_QT:
    from cansniff.config import Config
    from cansniff.model import CanFrame, FrameStats
    from cansniff.ui.interpret_view import InterpretView
    from cansniff.ui.theme import Theme

PAYLOAD = bytes.fromhex("81000340" "4CCCCD00")


def _frame(data: bytes = PAYLOAD, arb_id: int = 0x101) -> "CanFrame":
    return CanFrame(timestamp=0.0, arb_id=arb_id, data=data,
                    dlc=len(data), channel="1")


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class PayloadBlockSelectionTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Config.defaults(
            os.path.join(os.environ.get("TEMP", "."), "cansniff_view_test.json")
        )
        self.config.set("interpret.word_size", 4)
        for entry in self.config.get("interpret.decoders"):
            entry["enabled"] = entry["key"] == "u16_be"
        self.view = InterpretView(self.config, Theme())
        self.view.show_frame(_frame())
        self.app.processEvents()

    def tearDown(self):
        self.view.deleteLater()
        self.app.processEvents()

    # -- helpers --------------------------------------------------------

    def _selected_word(self):
        rows = self.view.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.view._result.words[rows[0].row()].word

    def _highlighted_indices(self):
        highlight = self.view.strip._highlight
        if highlight is None:
            return None
        start, length = highlight
        return list(range(start, start + length))

    # -- tests ----------------------------------------------------------

    def test_strip_highlight_matches_selected_block(self):
        for row in range(self.view.table.rowCount()):
            self.view.table.selectRow(row)
            self.app.processEvents()
            word = self._selected_word()
            self.assertIsNotNone(word)
            self.assertEqual(self._highlighted_indices(), word.indices)
            self.assertEqual(self.view.strip._highlight_label, word.span)

    def test_label_names_only_bytes_in_the_block(self):
        self.view.table.selectRow(3)
        self.app.processEvents()
        word = self._selected_word()
        self.assertEqual(word.span, "3-6")
        self.assertEqual(self.view.selection_label.text(), "bytes 3-6")
        self.assertNotIn(7, word.indices)

    def test_changing_block_size_does_not_leave_a_stale_highlight(self):
        """Regression: selection was restored by row number, so a rebuild left
        the strip bracketing a block that was no longer selected."""
        self.view.table.selectRow(4)
        self.app.processEvents()
        self.assertIsNotNone(self._highlighted_indices())

        self.view._on_block_size(1)          # 4 bytes -> 2 bytes
        self.app.processEvents()

        word = self._selected_word()
        if word is None:
            self.assertIsNone(self._highlighted_indices(),
                              "highlight survived a rebuild with nothing selected")
        else:
            self.assertEqual(self._highlighted_indices(), word.indices)

    def test_selection_follows_the_same_block_across_rebuilds(self):
        # Select the block at bytes 4-7, then narrow the byte range so every
        # row before it disappears. The block survives at a different row
        # number, and the selection must track the block, not the row.
        target = next(r for r, e in enumerate(self.view._result.words)
                      if e.word.first_index == 4)
        self.view.table.selectRow(target)
        self.app.processEvents()
        before = self._selected_word()
        self.assertEqual(before.first_index, 4)

        self.view.range_start.setValue(4)
        self.app.processEvents()

        after = self._selected_word()
        self.assertIsNotNone(after)
        self.assertEqual((after.first_index, after.length),
                         (before.first_index, before.length))
        self.assertNotEqual(self.view.table.currentRow(), target,
                            "row number should have changed, or this proves nothing")
        self.assertEqual(self._highlighted_indices(), after.indices)

    def test_switching_message_clears_the_previous_selection(self):
        self.view.table.selectRow(2)
        self.app.processEvents()
        self.view.show_frame(_frame(bytes.fromhex("0100010203040506"), arb_id=0x200))
        self.app.processEvents()
        self.assertIsNone(self._highlighted_indices())
        self.assertEqual(self.view.selection_label.text(), "")

    def test_clicking_a_byte_selects_the_block_starting_there(self):
        self.view.strip.byteClicked.emit(2)
        self.app.processEvents()
        word = self._selected_word()
        self.assertEqual(word.first_index, 2)
        self.assertEqual(self._highlighted_indices(), word.indices)

    def test_byte_range_control_is_inclusive(self):
        self.view.range_start.setValue(3)
        self.view.range_end.setValue(6)
        self.app.processEvents()
        indices = [i for e in self.view._result.words for i in e.word.indices]
        self.assertEqual(min(indices), 3)
        self.assertEqual(max(indices), 6, "byte 6 must be decodable when selected")

    def test_shorter_payload_drops_out_of_range_selection(self):
        self.view.table.selectRow(self.view.table.rowCount() - 1)
        self.app.processEvents()
        # Same ID, but a shorter payload: the old block no longer exists.
        self.view.show_frame(_frame(bytes.fromhex("810003")))
        self.app.processEvents()
        word = self._selected_word()
        if word is not None:
            self.assertEqual(self._highlighted_indices(), word.indices)
            self.assertLess(word.last_index, 3)


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class PanelPresentationTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Config.defaults(
            os.path.join(os.environ.get("TEMP", "."), "cansniff_view_test2.json")
        )
        self.view = InterpretView(self.config, Theme())

    def tearDown(self):
        self.view.deleteLater()
        self.app.processEvents()

    def test_no_recommendation_columns_exist(self):
        self.view.show_frame(_frame())
        self.app.processEvents()
        headers = [self.view.table.horizontalHeaderItem(c).text()
                   for c in range(self.view.table.columnCount())]
        for banned in ("Best fit", "Trend", "Score", "Rank"):
            self.assertNotIn(banned, headers)

    def test_dlc_chip_hidden_when_it_matches_the_byte_count(self):
        self.view.show_frame(_frame())
        self.app.processEvents()
        self.assertFalse(self.view.chip_dlc.isVisible())

    def test_unchanged_payload_is_not_re_rendered(self):
        frame = _frame()
        stats = FrameStats(key=frame.key, frame=frame)
        stats.update(frame)
        self.view.show_frame(frame, stats)
        self.app.processEvents()
        first_key = self.view._render_key
        self.assertIsNotNone(first_key)

        calls = []
        original = self.view._fill_table
        self.view._fill_table = lambda result: calls.append(result)
        self.view.show_frame(frame, stats)
        self.view._fill_table = original
        self.assertEqual(calls, [], "identical payload should not rebuild the table")

    def test_changed_payload_is_re_rendered(self):
        self.view.show_frame(_frame())
        self.app.processEvents()
        before = self.view._render_key
        self.view.show_frame(_frame(bytes.fromhex("8100034099CCCD00")))
        self.app.processEvents()
        self.assertNotEqual(self.view._render_key, before)


if __name__ == "__main__":
    unittest.main()
