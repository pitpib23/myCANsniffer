"""Factual BUS overview rendering and MainWindow profile ownership."""

from __future__ import annotations

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

if HAVE_QT:
    from cansniff.analysis.profile import (
        CaptureIntegrityAccumulator, SourceState, TrafficProfileAccumulator,
    )
    from cansniff.config import Config
    from cansniff.model import CanFrame
    from cansniff.ui.bus_overview import BusOverviewDialog
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.theme import Theme


def _frames(count=4):
    return [CanFrame(timestamp=i * 0.1, arb_id=0x100 + i % 2,
                     data=bytes([i]), dlc=1, channel="can0")
            for i in range(count)]


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class BusOverviewDialogTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialog = BusOverviewDialog(Theme())
        self.addCleanup(self.dialog.deleteLater)

    def test_empty_snapshot_states_no_frames_and_unavailable_metrics(self):
        snapshot = TrafficProfileAccumulator().snapshot()
        self.dialog.set_snapshot(snapshot, "capture.asc", "offline playback",
                                 "unavailable")
        self.assertEqual(self.dialog.fields["can"].text(), "no frames observed")
        self.assertEqual(self.dialog.fields["processed"].text(), "0")
        self.assertEqual(self.dialog.fields["integrity"].text(), "Not started")
        self.assertEqual(self.dialog.fields["parse_errors"].text(), "unavailable")
        self.assertEqual(self.dialog.fields["driver_overruns"].text(), "unavailable")

    def test_zero_available_is_visibly_different_from_unavailable(self):
        traffic = TrafficProfileAccumulator()
        traffic.update(_frames())
        integrity = CaptureIntegrityAccumulator()
        integrity.set_source_state(SourceState.ACTIVE)
        facts = integrity.snapshot(4, 4, current={"received": 4, "accepted": 4},
                                   current_parse_errors=0, driver_overruns=0)
        self.dialog.set_snapshot(traffic.snapshot(integrity=facts), "can0",
                                 "passive / listen-only verified", "500 kbit/s")
        self.assertEqual(self.dialog.fields["parse_errors"].text(), "0")
        self.assertEqual(self.dialog.fields["driver_overruns"].text(), "0")
        self.assertEqual(self.dialog.fields["integrity"].text(),
                         "No application-level loss observed")

    def test_degraded_snapshot_names_application_loss_without_calling_it_bus_loss(self):
        traffic = TrafficProfileAccumulator()
        traffic.update(_frames(2))
        integrity = CaptureIntegrityAccumulator()
        integrity.set_source_state(SourceState.ACTIVE)
        facts = integrity.snapshot(2, 2, current={
            "received": 10, "accepted": 8, "ui_dropped": 2,
        })
        self.dialog.set_snapshot(traffic.snapshot(integrity=facts), "can0",
                                 "passive", "500 kbit/s")
        self.assertEqual(self.dialog.fields["integrity"].text(), "Degraded")
        self.assertEqual(self.dialog.fields["ui_dropped"].text(), "2")
        self.assertIn("UI-delivery", self.dialog.fields["integrity_reasons"].text())
        self.assertNotIn("bus loss", self.dialog.fields["integrity_reasons"].text().lower())


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class MainWindowProfileIntegrationTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        config = Config.defaults(os.path.join(
            os.environ.get("TEMP", "."), "cansniff_bus_overview.json"))
        self.window = MainWindow(config, Theme())
        self.addCleanup(self.window.deleteLater)

    def test_same_ingestion_batch_updates_tables_store_and_profile(self):
        frames = _frames(7)
        self.window._on_frames(frames)
        result = self.window._profile_snapshot()
        self.assertEqual(result.processed_frames, 7)
        self.assertEqual(result.retained_horizon.frame_count, 7)
        self.assertEqual(result.unique_message_keys, 2)
        self.assertEqual(self.window.trace_model.total_rows, 7)

    def test_bus_button_opens_live_factual_snapshot(self):
        self.window._on_frames(_frames(5))
        self.window._show_bus_overview()
        self.app.processEvents()
        self.assertIsNotNone(self.window._bus_overview)
        self.assertTrue(self.window._bus_overview.isVisible())
        self.assertEqual(self.window._bus_overview.fields["processed"].text(), "5")
        self.assertEqual(self.window._bus_overview.fields["unique"].text(), "2")

    def test_clear_resets_profile_payload_history_and_visible_overview(self):
        self.window._on_frames(_frames(5))
        self.window._show_bus_overview()
        self.window.clear_views()
        self.app.processEvents()
        result = self.window._profile_snapshot()
        self.assertEqual(result.processed_frames, 0)
        self.assertEqual(result.messages, ())
        self.assertEqual(result.integrity.received, 0)
        self.assertEqual(self.window._bus_overview.fields["processed"].text(), "0")


if __name__ == "__main__":
    unittest.main()
