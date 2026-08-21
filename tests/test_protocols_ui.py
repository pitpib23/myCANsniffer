"""Protocols workspace rendering and asynchronous worker lifecycle."""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.analysis.profile import TrafficProfileAccumulator
    from cansniff.analysis.protocols import SurveyCancelled, build_protocol_survey
    from cansniff.analysis.store import FrameStore
    from cansniff.config import Config
    from cansniff.model import CanFrame
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.protocols_view import ProtocolsView
    from cansniff.ui.theme import Theme


def f(arb, data, t=0.0, extended=False):
    payload = bytes(data)
    return CanFrame(t, arb, payload, len(payload), is_extended=extended,
                    channel="can0")


def canopen_capture():
    frames = [f(0x701, [5], i) for i in range(3)]
    frames += [f(0x181, [1, i], 3 + i * 0.1) for i in range(2)]
    frames += [f(0x201, [2, i], 4 + i * 0.1) for i in range(2)]
    frames += [f(0x601, [0x40, 0, 0x20, 0, 0, 0, 0, 0], 5),
               f(0x581, [0x43, 0, 0x20, 0, 1, 2, 3, 4], 5.1)]
    return frames


def uds_capture():
    return [f(0x7E0, [3, 0x22, 0xF1, 0x90], 10),
            f(0x7E8, [4, 0x62, 0xF1, 0x90, 1], 10.1)]


def j1939_capture(source=3):
    def identifier(pf, ps):
        return (6 << 26) | (pf << 16) | (ps << 8) | source
    frames = []
    for group, (pf, ps, payload) in enumerate((
            (0xF0, 0x04, [1] * 8), (0xF1, 0x00, [2] * 8),
            (0xFE, 0xEE, [3] * 8), (0xEA, 0xFF, [0, 0xEE, 0]))):
        for repeat in range(3):
            frames.append(f(identifier(pf, ps), payload,
                            20 + group + repeat * 0.1, extended=True))
    return frames


def snapshot(frames):
    store = FrameStore(10000)
    store.add(frames)
    traffic = TrafficProfileAccumulator()
    traffic.update(frames)
    return build_protocol_survey(store.all_frames(), traffic.snapshot(store))


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class ProtocolsViewTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = ProtocolsView(Theme())
        self.addCleanup(self.view.deleteLater)

    def test_empty_survey_shows_all_results_and_silence_reason(self):
        self.view.set_snapshot(snapshot([]))
        self.assertEqual(self.view.summary_table.rowCount(), 5)
        self.assertIn("no usable traffic", self.view.summary_table.item(4, 2).text().lower())

    def test_strong_canopen_has_visible_reason_and_node_detail(self):
        self.view.set_snapshot(snapshot(canopen_capture()))
        self.assertEqual(self.view.summary_table.item(0, 1).text(), "Strong")
        self.assertIn("correlated", self.view.summary_table.item(0, 2).text())
        self.assertEqual(self.view.canopen_table.rowCount(), 1)
        self.assertIn("Heartbeat", self.view.canopen_table.item(0, 3).text())
        self.assertIn("mean", self.view.canopen_table.item(0, 5).text())
        self.assertIn("can0:701:S", self.view.canopen_table.item(0, 9).text())

    def test_mixed_known_and_unknown_are_both_visible(self):
        self.view.set_snapshot(snapshot(canopen_capture() + [f(0x555, [0xAA], 20)]))
        self.assertEqual(self.view.summary_table.item(0, 1).text(), "Strong")
        self.assertEqual(self.view.summary_table.item(4, 1).text(), "Present")

    def test_uds_observations_show_service_and_related_isotp_key(self):
        self.view.set_snapshot(snapshot(uds_capture()))
        self.assertEqual(self.view.uds_table.rowCount(), 2)
        self.assertIn("ReadDataByIdentifier", self.view.uds_table.item(0, 3).text())
        self.assertIn("7E0", self.view.uds_table.item(0, 5).text())

    def test_strong_j1939_populates_sources_pgns_and_message_destinations(self):
        self.view.set_snapshot(snapshot(j1939_capture()))
        self.assertEqual(self.view.summary_table.item(1, 1).text(), "Strong")
        self.assertEqual(self.view.j1939_sources_table.rowCount(), 1)
        self.assertIn("61444", self.view.j1939_sources_table.item(0, 2).text())
        self.assertEqual(self.view.j1939_sources_table.item(0, 6).text(),
                         "20.000000")
        destinations = {self.view.j1939_messages_table.item(row, 3).text()
                        for row in range(self.view.j1939_messages_table.rowCount())}
        self.assertIn("Broadcast", destinations)
        self.assertIn("0xFF", destinations)

    def test_evidence_reasons_horizon_and_caveats_are_not_tooltip_only(self):
        self.view.set_snapshot(snapshot(canopen_capture()))
        self.view.summary_table.selectRow(0)
        self.app.processEvents()
        text = self.view.evidence_detail.toPlainText()
        self.assertIn("Reasons:", text)
        self.assertIn("Integrity / horizon caveats:", text)
        self.assertIn("Related message keys:", text)
        self.assertIn("Retained", self.view.summary_table.item(0, 4).text())


class _SlowCache(object):
    def lookup(self, _window, _profile):
        return None

    def build(self, _window, _profile, cancelled):
        while not cancelled():
            time.sleep(0.001)
        raise SurveyCancelled()


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class MainWindowProtocolsTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        config = Config.defaults(os.path.join(
            os.environ.get("TEMP", "."), "cansniff_protocols_ui.json"))
        self.window = MainWindow(config, Theme())
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.window._protocol_closing = True
        self.window._cancel_protocol_survey(wait=True)
        self.window.deleteLater()

    def _wait(self, timeout=3.0):
        end = time.time() + timeout
        while self.window._protocol_thread is not None and time.time() < end:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertIsNone(self.window._protocol_thread)

    def test_protocol_nav_opens_workspace_and_populates_off_thread(self):
        self.window._on_frames(canopen_capture())
        self.window._activate_protocols()
        self.assertEqual(self.window.top_stack.currentIndex(),
                         self.window._STACK_PROTOCOLS)
        self.assertIsNotNone(self.window._protocol_thread)
        self._wait()
        self.assertEqual(self.window.protocols_view.summary_table.item(0, 1).text(),
                         "Strong")

    def test_navigation_button_exists_as_non_toggleable_destination(self):
        button = self.window.nav.group.button(self.window._NAV_PROTOCOLS)
        self.assertIsNotNone(button)
        self.assertEqual(button.accessibleName(), "Protocols")

    def test_clear_cancels_scan_and_removes_stale_protocol_rows(self):
        self.window._protocol_cache = _SlowCache()
        self.window._activate_protocols()
        self.app.processEvents()
        self.assertIsNotNone(self.window._protocol_thread)
        self.window.clear_views()
        self._wait()
        self.assertEqual(self.window.protocols_view.summary_table.rowCount(), 0)
        self.assertIsNone(self.window._protocol_snapshot)

    def test_close_path_cooperatively_cancels_and_joins_worker(self):
        self.window._protocol_cache = _SlowCache()
        self.window._activate_protocols()
        self.app.processEvents()
        self.window._protocol_closing = True
        self.window._cancel_protocol_survey(wait=True)
        self.assertIsNone(self.window._protocol_thread)
        self.assertIsNone(self.window._protocol_worker)


if __name__ == "__main__":
    unittest.main()
