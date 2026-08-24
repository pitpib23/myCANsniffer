"""Qt lifecycle for automatic SocketCAN bitrate detection on Start.

Covers the SocketCanDiscoveryWorker wrapper in isolation and its integration
into MainWindow.start_capture()/stop_capture() -- discovery runs off the UI
thread, Start does not begin capturing until it succeeds, a failed/cancelled
scan never starts capture, and Stop cancels an in-progress scan.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.config import Config
    from cansniff.discovery.model import (
        BitrateCandidateResult, CandidateStatus, DiscoveryProgress,
        DiscoveryResult, DiscoveryStatus,
    )
    from cansniff.model import CanFrame
    from cansniff.sources import CanFrameSource
    from cansniff.ui.discovery_worker import SocketCanDiscoveryWorker
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.theme import Theme


def _detected(bitrate=500000, interface="can0"):
    candidate = BitrateCandidateResult(
        bitrate, CandidateStatus.STABLE, frames=20, valid_frames=20,
        unique_ids=1, repeated_ids=1, best_id=0x123, best_id_observations=20,
        best_id_span=1.2, reasons=("synthetic stable evidence",))
    return DiscoveryResult(
        interface, DiscoveryStatus.DETECTED, bitrate, (candidate,),
        reasons=("Exactly one candidate produced stable, sustained traffic",))


def _no_traffic(interface="can0"):
    return DiscoveryResult(
        interface, DiscoveryStatus.NO_TRAFFIC, None, (),
        reasons=("No usable traffic was observed at any candidate bitrate",))


if HAVE_QT:
    class _FiniteSource(CanFrameSource):
        name = "post-discovery"

        def __init__(self):
            self.index = 0
            self.closed = False

        def open(self):
            pass

        def receive(self, timeout=0.1):
            if self.index >= 2:
                return None
            self.index += 1
            return CanFrame(self.index * 0.1, 0x123, b"\x01", 1)

        @property
        def exhausted(self):
            return self.index >= 2

        def close(self):
            self.closed = True


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class SocketCanDiscoveryWorkerTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _wait(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.002)
        return predicate()

    def test_run_calls_the_discoverer_with_the_configured_interface_and_candidates(self):
        calls = []

        def discoverer(interface, candidates=None, thresholds=None,
                       cancel_event=None, progress=None):
            calls.append((interface, tuple(candidates)))
            return _detected(interface=interface)

        worker = SocketCanDiscoveryWorker(
            "can1", candidates=(125000, 250000), discoverer=discoverer)
        results = []
        worker.resultReady.connect(results.append)
        worker.run()
        self.assertEqual(calls, [("can1", (125000, 250000))])
        self.assertEqual(results[0].status, DiscoveryStatus.DETECTED)

    def test_progress_is_relayed(self):
        def discoverer(interface, candidates=None, thresholds=None,
                       cancel_event=None, progress=None):
            progress(DiscoveryProgress("candidate-start", "Testing 500 kbit/s…", 0, 1))
            return _detected()

        worker = SocketCanDiscoveryWorker("can0", discoverer=discoverer)
        messages = []
        worker.progressChanged.connect(lambda item: messages.append(item.message))
        worker.run()
        self.assertEqual(messages, ["Testing 500 kbit/s…"])

    def test_cancel_sets_the_event_the_discoverer_observes(self):
        observed = []

        def discoverer(interface, candidates=None, thresholds=None,
                       cancel_event=None, progress=None):
            worker.cancel()
            observed.append(cancel_event.is_set())
            return DiscoveryResult(interface, DiscoveryStatus.CANCELLED, None, ())

        worker = SocketCanDiscoveryWorker("can0", discoverer=discoverer)
        worker.run()
        self.assertEqual(observed, [True])

    def test_exception_in_discoverer_emits_error_not_a_crash(self):
        def discoverer(*_args, **_kwargs):
            raise RuntimeError("boom")

        worker = SocketCanDiscoveryWorker("can0", discoverer=discoverer)
        errors = []
        worker.errorOccurred.connect(errors.append)
        finished = []
        worker.finished.connect(lambda: finished.append(True))
        worker.run()
        self.assertEqual(errors, ["boom"])
        self.assertEqual(finished, [True])


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class MainWindowAutoDiscoveryTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.path = os.path.join(
            tempfile.gettempdir(), "cansniff_discovery_main_{}.json".format(id(self)))
        self.config = Config.defaults(self.path)
        self.config.set("source.type", "live")
        self.config.set("source.live.channel", "can0")
        self.window = MainWindow(self.config, Theme())

    def tearDown(self):
        self.window._teardown_discovery_thread()
        self.window._teardown_thread()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _wait(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.002)
        return predicate()

    def test_start_runs_discovery_then_capture_on_success(self):
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        return_value=_detected(500000)), \
             mock.patch("cansniff.ui.main_window.build_source",
                        return_value=_FiniteSource()):
            self.window.start_capture()
            self.assertEqual(self.window._capture_state, self.window._DISCOVERING)
            self.assertFalse(self.window.start_button.isEnabled())
            # This scenario's discoverer returns immediately, so Discovering
            # can already have advanced to Running/Idle by the next line --
            # button-enabled timing around the debounce window is covered
            # separately by test_stop_cancels_an_in_progress_scan, which uses
            # a discoverer that actually blocks.
            self.assertTrue(self._wait(lambda: self.window._thread is None
                                       and self.window._capture_state == self.window._IDLE))
        self.assertEqual(len(self.window.frame_store.all_frames()), 2)
        self.assertEqual(self.config.get("source.live.bitrate"), 500000)

    def test_capture_does_not_start_after_failed_detection(self):
        started = []
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        return_value=_no_traffic()), \
             mock.patch("cansniff.ui.main_window.build_source",
                        side_effect=lambda *_a, **_k: started.append(True)), \
             mock.patch("cansniff.ui.main_window.QMessageBox.warning"):
            self.window.start_capture()
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))
        self.assertEqual(started, [], "build_source must never be called")
        self.assertIsNone(self.window._thread)

    def test_ambiguous_result_shows_a_warning_and_stays_idle(self):
        ambiguous = DiscoveryResult(
            "can0", DiscoveryStatus.AMBIGUOUS, None, (),
            reasons=("Multiple candidates produced stable evidence: 250000, 500000",))
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        return_value=ambiguous), \
             mock.patch("cansniff.ui.main_window.QMessageBox.warning") as warned:
            self.window.start_capture()
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))
        warned.assert_called_once()
        self.assertIsNone(self.window._thread)

    def test_stop_cancels_an_in_progress_scan(self):
        entered = threading.Event()

        def discoverer(interface, candidates=None, thresholds=None,
                       cancel_event=None, progress=None):
            entered.set()
            cancel_event.wait(2.0)
            return DiscoveryResult(interface, DiscoveryStatus.CANCELLED, None, ())

        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        side_effect=discoverer):
            self.window.start_capture()
            self.assertTrue(entered.wait(1.0))
            # Wait past Start's own brief post-click debounce -- see
            # _lock_interactions -- before Stop is available to cancel.
            self.assertTrue(self._wait(lambda: self.window.stop_button.isEnabled()))
            self.window.stop_capture()
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))
        self.assertIsNone(self.window._discovery_thread)
        self.assertIsNone(self.window._discovery_worker)
        self.assertIsNone(self.window._thread, "cancellation must never start capture")

    def test_main_window_close_cancels_active_discovery(self):
        entered = threading.Event()

        def discoverer(interface, candidates=None, thresholds=None,
                       cancel_event=None, progress=None):
            entered.set()
            cancel_event.wait(2.0)
            return DiscoveryResult(interface, DiscoveryStatus.CANCELLED, None, ())

        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        side_effect=discoverer):
            self.window.start_capture()
            self.assertTrue(entered.wait(1.0))
            self.window._teardown_discovery_thread()
        self.assertIsNone(self.window._discovery_thread)
        self.assertIsNone(self.window._discovery_worker)

    def test_disabling_auto_bitrate_skips_discovery_entirely(self):
        self.config.set("source.live.auto_bitrate", False)
        discoverer = mock.Mock()
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        discoverer), \
             mock.patch("cansniff.ui.main_window.build_source",
                        return_value=_FiniteSource()):
            self.window.start_capture()
            self.assertTrue(self._wait(lambda: self.window._thread is None
                                       and self.window._capture_state == self.window._IDLE))
        discoverer.assert_not_called()

    def test_can_fd_skips_auto_discovery_even_when_enabled(self):
        self.config.set("source.live.fd", True)
        discoverer = mock.Mock()
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        discoverer), \
             mock.patch("cansniff.ui.main_window.build_source",
                        return_value=_FiniteSource()):
            self.window.start_capture()
            self.assertTrue(self._wait(lambda: self.window._thread is None
                                       and self.window._capture_state == self.window._IDLE))
        discoverer.assert_not_called()

    def test_file_source_skips_discovery(self):
        self.config.set("source.type", "file")
        discoverer = mock.Mock()
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        discoverer), \
             mock.patch("cansniff.ui.main_window.build_source",
                        return_value=_FiniteSource()):
            self.window.start_capture()
            self.assertTrue(self._wait(lambda: self.window._thread is None
                                       and self.window._capture_state == self.window._IDLE))
        discoverer.assert_not_called()


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class ProductionUiHasNoBackendPickerTests(unittest.TestCase):
    """Requirement #22: removed backend choices no longer appear in the UI."""

    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_has_no_auto_discover_button_or_dialog_hooks(self):
        path = os.path.join(
            tempfile.gettempdir(), "cansniff_no_picker_main_{}.json".format(id(self)))
        window = MainWindow(Config.defaults(path), Theme())
        try:
            self.assertFalse(hasattr(window, "discover_button"))
            self.assertFalse(hasattr(window, "_discovery_dialog"))
            self.assertFalse(hasattr(window, "_auto_discover"))
        finally:
            window._teardown_thread()
            window.close()
            window.deleteLater()
            self.app.processEvents()
            if os.path.exists(path):
                os.remove(path)

    def test_config_dialog_has_no_backend_combo(self):
        from cansniff.ui.config_dialog import ConfigDialog

        path = os.path.join(
            tempfile.gettempdir(), "cansniff_no_picker_cfg_{}.json".format(id(self)))
        dialog = ConfigDialog(Config.defaults(path))
        try:
            self.assertFalse(hasattr(dialog, "live_interface"))
            self.assertTrue(hasattr(dialog, "live_channel"))
            self.assertTrue(hasattr(dialog, "live_auto_bitrate"))
        finally:
            dialog.deleteLater()
            self.app.processEvents()
            if os.path.exists(path):
                os.remove(path)

    def test_discovery_dialog_module_no_longer_exists(self):
        with self.assertRaises(ImportError):
            import cansniff.ui.discovery_dialog  # noqa: F401


if __name__ == "__main__":
    unittest.main()
