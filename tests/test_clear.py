"""The Clear action.

Clear resets the *view* of the bus, not the observation of it: the tables, the
shared frame store, the analysis caches, the selection and the counters go; the
capture log already on disk, the loaded database and the filters stay.

Nothing here opens a CAN interface.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cansniff.model import CanFrame  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False

if HAVE_QT:
    from cansniff.analysis.dbc import DbcDatabase
    from cansniff.config import Config
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.theme import Theme

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample.dbc")


def _frames(count=40):
    return [CanFrame(timestamp=i * 0.01, arb_id=0x100 + (i % 3),
                     data=bytes([i & 0xFF] * 8), dlc=8, channel="1")
            for i in range(count)]


class _FakeWorker(object):
    """Just enough of CaptureWorker for the counter reset."""

    def __init__(self):
        self.received = 500
        self.accepted = 480
        self.dropped = 20
        self.display_skipped = 3
        self._paused = False

    def reset_counters(self):
        self.received = self.accepted = self.dropped = self.display_skipped = 0

    def set_paused(self, paused):
        self._paused = bool(paused)

    def request_stop(self):
        """Present because closeEvent calls it; a stub without it kills Qt."""
        self._paused = False


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class ClearTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Config.defaults(
            os.path.join(os.environ.get("TEMP", "."), "cansniff_clear.json"))
        self.window = MainWindow(self.config, Theme())
        self.addCleanup(self.window.deleteLater)
        self.window._on_frames(_frames())
        self.app.processEvents()

    # -- what it clears --------------------------------------------------

    def test_the_button_exists_and_is_reachable(self):
        self.assertEqual(self.window.clear_button.text(), "Clear")
        self.assertTrue(self.window.clear_button.isEnabled())

    def test_tables_and_store_are_emptied(self):
        self.assertGreater(self.window.id_model.id_count, 0)
        self.assertGreater(len(self.window.frame_store), 0)

        self.window.clear_button.click()
        self.app.processEvents()

        self.assertEqual(self.window.id_model.id_count, 0)
        self.assertEqual(self.window.trace_model.total_rows, 0)
        self.assertEqual(len(self.window.frame_store), 0)

    def test_the_selection_and_the_panel_are_reset(self):
        self.window.id_view.selectRow(0)
        self.app.processEvents()
        self.assertIsNotNone(self.window.interpret_view.current_frame())

        self.window.clear_views()
        self.app.processEvents()
        self.assertIsNone(self.window.interpret_view.current_frame())
        self.assertIsNone(self.window._selected_key)

    def test_a_held_panel_is_released_so_it_does_not_stay_blank(self):
        """A hold rejects new frames; leaving it set would silently stick."""
        view = self.window.interpret_view
        self.window.id_view.selectRow(0)
        view.freeze_check.setChecked(True)
        self.app.processEvents()

        self.window.clear_views()
        self.app.processEvents()
        self.assertFalse(view.freeze_check.isChecked())
        self.assertIsNone(view.current_frame())

        # And the panel accepts frames again afterwards.
        self.window._on_frames(_frames(5))
        self.window.id_view.selectRow(0)
        self.app.processEvents()
        self.assertIsNotNone(view.current_frame())

    def test_the_packet_counters_are_reset(self):
        self.window._worker = _FakeWorker()
        self.window._update_status()
        self.app.processEvents()

        self.window.clear_views()
        self.app.processEvents()
        self.assertEqual(self.window._worker.received, 0)
        self.assertEqual(self.window._worker.accepted, 0)
        self.assertEqual(self.window._worker.dropped, 0)
        self.assertEqual(self.window.metric_received.value.text(), "0")
        self.assertEqual(self.window.metric_dropped.value.text(), "0")

    def test_clearing_with_no_capture_running_does_not_raise(self):
        self.assertIsNone(self.window._worker)
        self.window.clear_views()

    def test_the_channel_list_is_forgotten(self):
        self.window._on_frames([CanFrame(timestamp=1.0, arb_id=0x200,
                                         data=b"\x01", dlc=1, channel="7")])
        self.assertIn("7", self.window._seen_channels)
        self.window.clear_views()
        self.assertEqual(self.window._seen_channels, set())

    # -- what it must not touch ------------------------------------------

    def test_the_loaded_database_survives(self):
        database = DbcDatabase.load(FIXTURE)
        self.window._set_database(database)
        self.window.clear_views()
        self.assertIs(self.window.database, database)
        self.assertIs(self.window.interpret_view._database, database)

    def test_the_capture_filters_survive(self):
        rules = [{"name": "keep 0x100", "enabled": True, "mode": "allow",
                  "id_min": "0x100", "id_max": "0x100"}]
        self.config.data["filters"] = rules
        self.window.clear_views()
        self.assertEqual(self.config.filters, rules)

    def test_the_display_filter_survives(self):
        self.window.filter_bar.search_box.setText("100")
        self.app.processEvents()
        self.window.clear_views()
        self.app.processEvents()
        self.assertEqual(self.window.filter_bar.search_box.text(), "100")

    def test_a_running_capture_is_not_stopped(self):
        worker = _FakeWorker()
        self.window._worker = worker
        self.window.clear_views()
        self.assertIs(self.window._worker, worker,
                      "Clear resets the view, it does not stop the capture")

    def test_clearing_while_paused_keeps_the_pause(self):
        self.window._worker = _FakeWorker()
        # Pause only does anything while a capture is actually running/
        # paused (see MainWindow._apply_capture_state) — a real Start
        # would have made this transition; this test simulates it directly
        # rather than standing up a real capture thread.
        self.window._apply_capture_state(self.window._RUNNING)
        self.window.pause_button.setChecked(True)
        self.app.processEvents()
        self.window.clear_views()
        self.app.processEvents()
        self.assertTrue(self.window.pause_button.isChecked())
        self.assertTrue(self.window._worker._paused)

    def test_collecting_again_after_a_clear_works(self):
        self.window.clear_views()
        self.window._on_frames(_frames(12))
        self.app.processEvents()
        self.assertGreater(self.window.id_model.id_count, 0)
        self.assertGreater(len(self.window.frame_store), 0)


if __name__ == "__main__":
    unittest.main()
