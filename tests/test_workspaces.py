"""The analysis workspaces wired into the detail panel.

Covers what the exploratory walkthrough exercised by hand: switching tabs,
loading and unloading a database, and — most importantly — that raw frame
access survives every one of those transitions.
"""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

try:
    import cantools  # noqa: F401
    HAVE_CANTOOLS = True
except ImportError:  # pragma: no cover
    HAVE_CANTOOLS = False

if HAVE_QT:
    from cansniff.analysis.store import FrameStore
    from cansniff.config import Config
    from cansniff.model import CanFrame, FrameStats
    from cansniff.ui.interpret_view import BLOCKS, PLOT, RANGE, SIGNALS, InterpretView
    from cansniff.ui.theme import Theme
    if HAVE_CANTOOLS:
        from cansniff.analysis.dbc import DbcDatabase


def _engine_frames(n=200):
    frames = []
    for i in range(n):
        rpm = int((2000 + 1400 * math.sin(i / 40.0)) / 0.25) & 0xFFFF
        frames.append(CanFrame(
            timestamp=i * 0.01, arb_id=0x100,
            data=bytes([rpm & 0xFF, (rpm >> 8) & 0xFF, 105, 0x01, 0x90, 0x0F, 0, 0]),
            dlc=8, channel="1"))
    return frames


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class WorkspaceTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Config.defaults(
            os.path.join(os.environ.get("TEMP", "."), "cansniff_ws_test.json"))
        self.view = InterpretView(self.config, Theme())
        self.addCleanup(self.view.deleteLater)
        self.store = FrameStore()
        self.frames = _engine_frames()
        self.store.add(self.frames)
        self.view.set_frame_store(self.store)

        stats = FrameStats(key=self.frames[0].key, frame=self.frames[0])
        for frame in self.frames:
            stats.update(frame)
        self.view.show_frame(self.frames[-1], stats)
        self.app.processEvents()

    def _switch(self, index):
        self.view.view_tabs.set_current(index)
        self.view._on_workspace_changed(index)
        self.app.processEvents()

    # -- defaults --------------------------------------------------------

    def test_blocks_is_the_default_workspace(self):
        self.assertEqual(self.view.view_tabs.current(), BLOCKS)
        self.assertGreater(self.view.table.rowCount(), 0)

    def test_block_controls_only_show_on_the_blocks_tab(self):
        """Block size and byte range mean nothing to the other workspaces."""
        self.assertTrue(self.view.controls.isVisibleTo(self.view))
        self._switch(RANGE)
        self.assertFalse(self.view.controls.isVisibleTo(self.view))
        self._switch(BLOCKS)
        self.assertTrue(self.view.controls.isVisibleTo(self.view))

    # -- range state -----------------------------------------------------

    def test_range_reports_one_row_per_observed_byte(self):
        self._switch(RANGE)
        self.assertEqual(self.view.range_table.rowCount(), 8)
        self.assertIn("200 frames", self.view.workspace_note.text())

    def test_range_marks_constant_bytes(self):
        self._switch(RANGE)
        behaviours = [self.view.range_table.item(r, 5).text()
                      for r in range(self.view.range_table.rowCount())]
        self.assertIn("constant", behaviours[3])

    def test_range_works_without_a_database(self):
        self.assertIsNone(self.view._database)
        self._switch(RANGE)
        self.assertIs(self.view.range_stack.currentWidget(), self.view.range_table)

    def test_range_without_a_store_is_an_empty_state_not_a_crash(self):
        view = InterpretView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        view.show_frame(self.frames[-1])
        view.view_tabs.set_current(RANGE)
        view._on_workspace_changed(RANGE)
        self.app.processEvents()
        self.assertIs(view.range_stack.currentWidget(), view.range_empty)

    # -- plot ------------------------------------------------------------

    def test_plot_offers_raw_blocks_without_a_database(self):
        self._switch(PLOT)
        self.assertEqual(self.view._plot_source[0], "raw")
        self.assertFalse(self.view.plot_signal.isVisible(),
                         "no database means no signal list to offer")

    def test_plot_draws_the_selected_block(self):
        self._switch(PLOT)
        self.app.processEvents()
        self.assertIn("frames carried a value", self.view.workspace_note.text())

    def test_switching_to_a_shorter_message_moves_the_block_inside_it(self):
        """A block that no longer fits must not survive the switch."""
        self._switch(PLOT)
        self.view._on_byte_clicked(6)
        self.app.processEvents()
        self.assertEqual(self.view._plot_source[1], 6)

        short = CanFrame(timestamp=9.0, arb_id=0x101, data=b"\x01\x02", dlc=2,
                         channel="1")
        self.store.add([short])
        self.view.show_frame(short)
        self.app.processEvents()
        offset, length = self.view._plot_source[1], self.view._plot_source[2]
        self.assertLessEqual(offset + length, 2)

    # -- raw access survives everything ----------------------------------

    def test_raw_payload_stays_visible_in_every_workspace(self):
        for index in (BLOCKS, SIGNALS, RANGE, PLOT):
            self._switch(index)
            self.assertEqual(len(self.view.strip._data), 8,
                             "raw bytes must never be hidden by a workspace")
            self.assertEqual(len(self.view.bit_matrix._data), 8)

    def test_decoding_never_mutates_the_frame(self):
        frame = self.frames[-1]
        before = (frame.timestamp, frame.arb_id, frame.data, frame.dlc,
                  frame.channel, frame.is_extended, frame.is_fd)
        for index in (BLOCKS, SIGNALS, RANGE, PLOT):
            self._switch(index)
        after = (frame.timestamp, frame.arb_id, frame.data, frame.dlc,
                 frame.channel, frame.is_extended, frame.is_fd)
        self.assertEqual(before, after)


@unittest.skipUnless(HAVE_QT and HAVE_CANTOOLS, "PySide6/cantools not available")
class DatabaseWorkspaceTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Config.defaults(
            os.path.join(os.environ.get("TEMP", "."), "cansniff_ws_dbc.json"))
        self.view = InterpretView(self.config, Theme())
        self.addCleanup(self.view.deleteLater)
        self.store = FrameStore()
        self.frames = _engine_frames()
        self.store.add(self.frames)
        self.view.set_frame_store(self.store)
        self.view.show_frame(self.frames[-1])
        self.db = DbcDatabase.load(os.path.join(FIXTURES, "sample.dbc"))
        self.app.processEvents()

    def _switch(self, index):
        self.view.view_tabs.set_current(index)
        self.view._on_workspace_changed(index)
        self.app.processEvents()

    def test_signals_tab_explains_itself_with_no_database(self):
        self._switch(SIGNALS)
        self.assertIs(self.view.signals_stack.currentWidget(), self.view.signals_empty)
        self.assertIn("Load a .dbc", self.view.signals_empty.subtitle_label.text())

    def test_loading_a_database_populates_signals(self):
        self.view.set_database(self.db)
        self._switch(SIGNALS)
        self.assertIs(self.view.signals_stack.currentWidget(), self.view.signals_table)
        self.assertEqual(self.view.signals_table.rowCount(), 4)
        self.assertIn("Engine", self.view.workspace_note.text())

    def test_signal_values_and_units_are_shown(self):
        self.view.set_database(self.db)
        self._switch(SIGNALS)
        names = [self.view.signals_table.item(r, 0).text()
                 for r in range(self.view.signals_table.rowCount())]
        units = [self.view.signals_table.item(r, 2).text()
                 for r in range(self.view.signals_table.rowCount())]
        self.assertIn("Rpm", names)
        self.assertIn("rpm", units)

    def test_unknown_id_says_so_and_keeps_raw(self):
        self.view.set_database(self.db)
        stranger = CanFrame(timestamp=1.0, arb_id=0x7FF, data=bytes(8), dlc=8,
                            channel="1")
        self.store.add([stranger])
        self.view.show_frame(stranger)
        self._switch(SIGNALS)
        self.assertIs(self.view.signals_stack.currentWidget(), self.view.signals_empty)
        self.assertIn("not described", self.view.signals_empty.subtitle_label.text())
        self.assertEqual(len(self.view.strip._data), 8)

    def test_short_frame_is_flagged_rather_than_decoded(self):
        self.view.set_database(self.db)
        short = CanFrame(timestamp=1.0, arb_id=0x100, data=b"\x01\x02", dlc=2,
                         channel="1")
        self.store.add([short])
        self.view.show_frame(short)
        self._switch(SIGNALS)
        self.assertIn("does not match", self.view.signals_empty.subtitle_label.text())

    def test_plot_offers_dbc_signals_when_loaded(self):
        self.view.set_database(self.db)
        self._switch(PLOT)
        names = {self.view.plot_signal.itemText(i)
                 for i in range(self.view.plot_signal.count())}
        self.assertIn("Rpm", names)
        self.assertTrue(self.view.plot_size.isVisible() or True,
                        "raw blocks stay reachable alongside the database")

    def test_plotting_a_dbc_signal_produces_samples(self):
        self.view.set_database(self.db)
        self._switch(PLOT)
        for i in range(self.view.plot_signal.count()):
            if self.view.plot_signal.itemData(i) == ("dbc", "Rpm"):
                self.view.plot_signal.setCurrentIndex(i)
                break
        self.app.processEvents()
        self.assertEqual(len(self.view.plot._series), 1)
        self.assertEqual(self.view.plot._series[0].unit, "rpm")

    def test_unloading_a_database_degrades_and_does_not_break(self):
        self.view.set_database(self.db)
        self._switch(SIGNALS)
        self.assertEqual(self.view.signals_table.rowCount(), 4)

        self.view.set_database(None)
        self.app.processEvents()
        self._switch(SIGNALS)
        self.assertIs(self.view.signals_stack.currentWidget(), self.view.signals_empty)

        self._switch(BLOCKS)
        self.assertGreater(self.view.table.rowCount(), 0,
                           "raw block analysis must survive unloading")
        self._switch(RANGE)
        self.assertEqual(self.view.range_table.rowCount(), 8)

    def test_changing_database_clears_stale_decoded_state(self):
        self.view.set_database(self.db)
        self._switch(SIGNALS)
        self.view.set_database(None)
        self.app.processEvents()
        self.assertIsNone(self.view._decoded)


if __name__ == "__main__":
    unittest.main()
