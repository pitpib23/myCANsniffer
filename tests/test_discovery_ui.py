"""Qt lifecycle for the explicit Auto Scan action.

Covers the SocketCanDiscoveryWorker wrapper in isolation and its
integration into MainWindow.start_auto_scan()/stop_capture() -- Auto Scan
is a distinct, explicit button/entry point from Start (start_capture),
never triggered implicitly by it and never gated by a Settings toggle (see
cansniff/ui/config_dialog.py). Discovery runs off the UI thread behind a
dedicated AutoScanDialog popup; Start never begins capturing until a
successful scan says so; a failed/cancelled scan never starts capture;
Stop, the dialog's own Cancel button, and the dialog's window-close (X) all
cancel through the identical path.
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
    from cansniff.ui.auto_scan_dialog import AutoScanDialog
    from cansniff.ui.discovery_worker import SocketCanDiscoveryWorker
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.theme import Theme


def _stable_candidate(bitrate=500000):
    return BitrateCandidateResult(
        bitrate, CandidateStatus.STABLE, frames=386, valid_frames=386,
        unique_ids=17, repeated_ids=17, best_id=0x123, best_id_observations=42,
        best_id_span=2.31, reasons=("synthetic stable evidence",))


def _detected(bitrate=500000, interface="can0"):
    return DiscoveryResult(
        interface, DiscoveryStatus.DETECTED, bitrate, (_stable_candidate(bitrate),),
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
class MainWindowAutoScanTests(unittest.TestCase):
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

    # -- Start is never gated by discovery ---------------------------------

    def test_start_never_triggers_discovery_regardless_of_legacy_config(self):
        """There is no Settings toggle for this any more (see
        cansniff/ui/config_dialog.py) -- Start is always the manually
        configured bitrate, even if an old config file still carries
        auto_bitrate: true from before this was removed."""
        self.config.set("source.live.auto_bitrate", True)
        discoverer = mock.Mock()
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate", discoverer), \
             mock.patch("cansniff.ui.main_window.build_source",
                        return_value=_FiniteSource()):
            self.window.start_capture()
            self.assertTrue(self._wait(lambda: self.window._thread is None
                                       and self.window._capture_state == self.window._IDLE))
        discoverer.assert_not_called()
        self.assertIsNone(self.window._auto_scan_dialog)

    # -- Auto Scan opens a dedicated popup ---------------------------------

    def test_auto_scan_opens_a_dedicated_progress_dialog(self):
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        return_value=_no_traffic()):
            self.window.start_auto_scan()
            dialog = self.window._auto_scan_dialog
            self.assertIsInstance(dialog, AutoScanDialog)
            self.assertEqual(dialog.interface, "can0")
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))

    def test_dialog_shows_the_configured_channel(self):
        self.config.set("source.live.channel", "can1")
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        return_value=_no_traffic(interface="can1")):
            self.window.start_auto_scan()
            self.assertEqual(self.window._auto_scan_dialog.interface_label.text(),
                             "Interface: can1")
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))

    def test_auto_scan_button_disabled_while_scanning_start_disabled_too(self):
        entered = threading.Event()

        def discoverer(interface, candidates=None, thresholds=None,
                       cancel_event=None, progress=None):
            entered.set()
            cancel_event.wait(2.0)
            return DiscoveryResult(interface, DiscoveryStatus.CANCELLED, None, ())

        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        side_effect=discoverer):
            self.window.start_auto_scan()
            self.assertTrue(entered.wait(1.0))
            self.assertFalse(self.window.start_button.isEnabled())
            self.assertFalse(self.window.auto_scan_button.isEnabled())
            self.assertTrue(self._wait(lambda: self.window.stop_button.isEnabled()))
            self.window.stop_capture()
            # Past _capture_state == Idle *and* past _lock_interactions'
            # own brief post-action debounce (see _refresh_capture_controls)
            # -- both buttons' *enabled* bit depends on both.
            self.assertTrue(self._wait(lambda: self.window.start_button.isEnabled()))
        self.assertTrue(self.window.auto_scan_button.isEnabled())

    def test_can_fd_refuses_auto_scan_with_an_explanatory_message(self):
        self.config.set("source.live.fd", True)
        discoverer = mock.Mock()
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate", discoverer), \
             mock.patch("cansniff.ui.main_window.QMessageBox.information") as informed:
            self.window.start_auto_scan()
        discoverer.assert_not_called()
        informed.assert_called_once()
        self.assertIsNone(self.window._auto_scan_dialog)
        self.assertEqual(self.window._capture_state, self.window._IDLE)

    def test_auto_scan_targets_the_live_channel_even_when_source_type_is_file(self):
        """Auto Scan always operates on source.live.channel -- it is not
        gated by which Source Type Start would currently use."""
        self.config.set("source.type", "file")
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        return_value=_no_traffic()) as discoverer:
            self.window.start_auto_scan()
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))
        discoverer.assert_called_once()
        self.assertEqual(discoverer.call_args[0][0], "can0")

    # -- progress reaches the dialog -----------------------------------

    def test_progress_updates_the_dialog_for_every_candidate(self):
        def discoverer(interface, candidates=None, thresholds=None,
                       cancel_event=None, progress=None):
            for index, bitrate in enumerate(candidates):
                progress(DiscoveryProgress(
                    "candidate-start", "Testing {} kbit/s…".format(bitrate // 1000),
                    index, len(candidates)))
                progress(DiscoveryProgress(
                    "candidate-complete", "{} kbit/s: no-traffic".format(bitrate // 1000),
                    index + 1, len(candidates),
                    BitrateCandidateResult(bitrate, CandidateStatus.NO_TRAFFIC)))
            return _no_traffic()

        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        side_effect=discoverer):
            self.window.start_auto_scan()
            dialog = self.window._auto_scan_dialog
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))
        self.assertEqual(dialog.table.rowCount(), 11)
        self.assertEqual(dialog.progress_bar.value(), 11)

    def test_current_bitrate_and_stats_are_shown_for_a_stable_candidate(self):
        def discoverer(interface, candidates=None, thresholds=None,
                       cancel_event=None, progress=None):
            progress(DiscoveryProgress(
                "candidate-listening", "Listening at 500 kbit/s…", 8, 11))
            progress(DiscoveryProgress(
                "candidate-complete", "500 kbit/s: stable", 9, 11, _stable_candidate()))
            return _detected()

        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        side_effect=discoverer), \
             mock.patch("cansniff.ui.main_window.build_source",
                        return_value=_FiniteSource()):
            self.window.start_auto_scan()
            dialog = self.window._auto_scan_dialog
            self.assertTrue(self._wait(
                lambda: dialog.bitrate_label.text().startswith("Testing:")))
            self.assertIn("500", dialog.bitrate_label.text())
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))
        self.assertEqual(dialog._stat_labels["valid"].text(), "386")
        self.assertEqual(dialog._stat_labels["unique_ids"].text(), "17")
        self.assertEqual(dialog._stat_labels["best_id"].text(), "0x123")

    # -- outcomes -----------------------------------------------------------

    def test_start_runs_discovery_then_capture_on_success(self):
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        return_value=_detected(500000)), \
             mock.patch("cansniff.ui.main_window.build_source",
                        return_value=_FiniteSource()):
            self.window.start_auto_scan()
            self.assertEqual(self.window._capture_state, self.window._DISCOVERING)
            self.assertFalse(self.window.start_button.isEnabled())
            self.assertTrue(self._wait(lambda: self.window._thread is None
                                       and self.window._capture_state == self.window._IDLE))
        self.assertEqual(len(self.window.frame_store.all_frames()), 2)
        self.assertEqual(self.config.get("source.live.bitrate"), 500000)
        # The popup closes automatically once capture is live.
        self.assertIsNone(self.window._auto_scan_dialog)

    def test_capture_does_not_start_after_failed_detection(self):
        started = []
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        return_value=_no_traffic()), \
             mock.patch("cansniff.ui.main_window.build_source",
                        side_effect=lambda *_a, **_k: started.append(True)):
            self.window.start_auto_scan()
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))
        self.assertEqual(started, [], "build_source must never be called")
        self.assertIsNone(self.window._thread)

    def test_no_traffic_leaves_the_dialog_open_with_results(self):
        """POPUP — NO TRAFFIC: results stay visible; the dialog provides
        its own Close (the same button, relabelled) rather than being
        auto-closed out from under the operator."""
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        return_value=_no_traffic()):
            self.window.start_auto_scan()
            dialog = self.window._auto_scan_dialog
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))
        self.assertIn("No stable CAN traffic", dialog.status_label.text())
        self.assertEqual(dialog.action_button.text(), "Close")
        self.assertTrue(dialog.isVisible() or not dialog.isHidden())

    def test_systemic_configuration_error_shows_the_actual_reason_not_just_no_traffic(self):
        """Regression for "PROGRESS DIALOG LISTEN-ONLY STATUS": a systemic
        configuration failure (most commonly listen-only verification
        failing -- see cansniff/socketcan.py's SYSTEMIC_ERROR_KINDS) means
        the scan never reached the listening phase for any candidate. The
        dialog must show *why* (result.reasons), never just a bare generic
        template that could be misread as "no traffic was heard"."""
        configuration_error = DiscoveryResult(
            "can0", DiscoveryStatus.CONFIGURATION_ERROR, None, (),
            reasons=(
                "Could not configure can0: Listen-only mode could not be "
                "confirmed on can0 after configuration. Capture was not "
                "started. (listen-only-unconfirmed)",
                "Remaining candidates were not attempted",
            ))
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        return_value=configuration_error):
            self.window.start_auto_scan()
            dialog = self.window._auto_scan_dialog
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))
        text = dialog.status_label.text()
        self.assertIn("Could not configure can0", text)
        self.assertIn("listen-only", text.lower())
        self.assertIn("Remaining candidates were not attempted", text)
        self.assertEqual(dialog.action_button.text(), "Close")

    def test_ambiguous_result_leaves_the_dialog_open_with_results_and_stays_idle(self):
        stable_candidates = (_stable_candidate(250000), _stable_candidate(500000))
        ambiguous = DiscoveryResult(
            "can0", DiscoveryStatus.AMBIGUOUS, None, stable_candidates,
            reasons=("Multiple candidates produced stable evidence: 250000, 500000",))

        def discoverer(interface, candidates=None, thresholds=None,
                       cancel_event=None, progress=None):
            # Realistic worker behavior: a candidate-complete progress event
            # always precedes the final result -- see
            # cansniff/discovery/bitrate.py -- which is what actually
            # populates the dialog's table (on_progress, not on_result).
            for index, candidate in enumerate(ambiguous.candidate_results):
                progress(DiscoveryProgress(
                    "candidate-complete", "{} kbit/s: stable".format(candidate.bitrate // 1000),
                    index + 1, len(ambiguous.candidate_results), candidate))
            return ambiguous

        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        side_effect=discoverer):
            self.window.start_auto_scan()
            dialog = self.window._auto_scan_dialog
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))
        self.assertIn("will not guess", dialog.status_label.text())
        self.assertEqual(dialog.table.rowCount(), 2)
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
            self.window.start_auto_scan()
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
        # Cancellation closes the popup -- it must not linger invisibly.
        self.assertIsNone(self.window._auto_scan_dialog)

    def test_dialog_cancel_button_uses_the_same_path_as_main_window_stop(self):
        """Requirement: popup Cancel, the popup's own X, and the main Stop
        button all invoke exactly one cancellation mechanism."""
        entered = threading.Event()

        def discoverer(interface, candidates=None, thresholds=None,
                       cancel_event=None, progress=None):
            entered.set()
            cancel_event.wait(2.0)
            return DiscoveryResult(interface, DiscoveryStatus.CANCELLED, None, ())

        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        side_effect=discoverer), \
             mock.patch.object(MainWindow, "stop_capture") as stop_spy:
            self.window.start_auto_scan()
            self.assertTrue(entered.wait(1.0))
            dialog = self.window._auto_scan_dialog
            dialog.action_button.click()
        stop_spy.assert_called_once()

    def test_closing_the_dialog_window_cancels_safely(self):
        entered = threading.Event()

        def discoverer(interface, candidates=None, thresholds=None,
                       cancel_event=None, progress=None):
            entered.set()
            cancel_event.wait(2.0)
            return DiscoveryResult(interface, DiscoveryStatus.CANCELLED, None, ())

        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        side_effect=discoverer):
            self.window.start_auto_scan()
            self.assertTrue(entered.wait(1.0))
            self.assertTrue(self._wait(lambda: self.window.stop_button.isEnabled()))
            self.window._auto_scan_dialog.close()
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))
        self.assertIsNone(self.window._thread, "cancellation must never start capture")

    def test_stale_finished_dialog_close_does_not_cancel_a_later_scan(self):
        """The identity check in start_auto_scan's cancel wiring: an
        operator who leaves a finished (no-traffic) results dialog open and
        then starts -- and later closes the stale window during -- a
        *second* scan must not have that second scan cancelled."""
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        return_value=_no_traffic()):
            self.window.start_auto_scan()
            first_dialog = self.window._auto_scan_dialog
            # Past _capture_state == Idle *and* past the post-action
            # debounce -- see _lock_interactions -- so the second
            # start_auto_scan() below is not silently ignored.
            self.assertTrue(self._wait(lambda: self.window.auto_scan_button.isEnabled()))
        self.assertIsNotNone(first_dialog)
        self.assertTrue(first_dialog.isVisible() or not first_dialog.isHidden())

        entered = threading.Event()

        def discoverer(interface, candidates=None, thresholds=None,
                       cancel_event=None, progress=None):
            entered.set()
            cancel_event.wait(2.0)
            return DiscoveryResult(interface, DiscoveryStatus.CANCELLED, None, ())

        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        side_effect=discoverer):
            self.window.start_auto_scan()
            self.assertTrue(entered.wait(1.0))
            # Close the *first*, already-finished dialog -- must not cancel
            # the second scan, which is still actively running.
            first_dialog.close()
            self.app.processEvents()
            self.assertEqual(self.window._capture_state, self.window._DISCOVERING)
            self.assertTrue(self._wait(lambda: self.window.stop_button.isEnabled()))
            self.window.stop_capture()
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))

    def test_main_window_close_cancels_active_scan(self):
        entered = threading.Event()

        def discoverer(interface, candidates=None, thresholds=None,
                       cancel_event=None, progress=None):
            entered.set()
            cancel_event.wait(2.0)
            return DiscoveryResult(interface, DiscoveryStatus.CANCELLED, None, ())

        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        side_effect=discoverer):
            self.window.start_auto_scan()
            self.assertTrue(entered.wait(1.0))
            dialog = self.window._auto_scan_dialog
            self.window._teardown_discovery_thread()
        self.assertIsNone(self.window._discovery_thread)
        self.assertIsNone(self.window._discovery_worker)
        self.assertIsNone(self.window._auto_scan_dialog)
        self.assertTrue(dialog.isHidden())

    # -- the critical winner-race regression -------------------------------

    def test_winner_race_discovery_finished_signal_cannot_bring_capture_down(self):
        """Regression: winner chosen -> final bitrate configured -> normal
        Bus opened -> discovery worker emits finished/completed. That must
        never bring the just-started capture's interface back down or stop
        the just-started capture. See cansniff/discovery/bitrate.py's own
        DETECTED path (never calls _best_effort_down) and
        MainWindow._on_discovery_thread_finished (closes the dialog *after*
        _start_capture_now, and never calls SocketCanSessionController.down
        on the proceed path at all).
        """
        with mock.patch("cansniff.discovery.discover_socketcan_bitrate",
                        return_value=_detected(500000)), \
             mock.patch("cansniff.ui.main_window.build_source",
                        return_value=_FiniteSource()), \
             mock.patch("cansniff.ui.main_window.SocketCanSessionController") as ctrl_cls:
            self.window.start_auto_scan()
            self.assertTrue(self._wait(
                lambda: self.window._thread is None
                and self.window._capture_state == self.window._IDLE))
        # _start_capture_now(configure_link=False) must not have constructed
        # a controller at all for the final capture (discovery already
        # configured the winner) -- and nothing on the proceed path calls
        # .down()/.down_best_effort() while capture was live.
        for call in ctrl_cls.mock_calls:
            self.assertNotIn("down", str(call))
        self.assertEqual(len(self.window.frame_store.all_frames()), 2,
                         "capture must have actually run to completion, undisturbed")


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

    def test_auto_scan_is_a_primary_visible_button_beside_start_and_stop(self):
        """Requirement: Auto Scan is a primary application operation, not
        buried in Settings/menus/context menus/hidden advanced controls."""
        path = os.path.join(
            tempfile.gettempdir(), "cansniff_auto_scan_button_{}.json".format(id(self)))
        window = MainWindow(Config.defaults(path), Theme())
        try:
            self.assertTrue(hasattr(window, "auto_scan_button"))
            self.assertEqual(window.auto_scan_button.text(), "Auto Scan")
            self.assertTrue(window.auto_scan_button.isEnabled())
            self.assertTrue(window.auto_scan_button.isVisibleTo(window))
        finally:
            window._teardown_thread()
            window.close()
            window.deleteLater()
            self.app.processEvents()
            if os.path.exists(path):
                os.remove(path)

    def test_config_dialog_has_no_backend_combo_or_auto_bitrate_toggle(self):
        from cansniff.ui.config_dialog import ConfigDialog

        path = os.path.join(
            tempfile.gettempdir(), "cansniff_no_picker_cfg_{}.json".format(id(self)))
        dialog = ConfigDialog(Config.defaults(path))
        try:
            self.assertFalse(hasattr(dialog, "live_interface"))
            self.assertFalse(hasattr(dialog, "live_auto_bitrate"),
                             "Settings must hold configuration, not a workflow toggle "
                             "-- Auto Scan is the main window's own button")
            self.assertTrue(hasattr(dialog, "live_channel"))
            self.assertTrue(hasattr(dialog, "live_bitrate"))
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
