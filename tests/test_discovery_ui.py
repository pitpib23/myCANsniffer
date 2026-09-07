"""Qt lifecycle for the explicit Auto Scan action.

Covers the SocketCanDiscoveryWorker wrapper in isolation (the older,
auto-selecting engine's Qt marshaling -- still real, still independently
tested, just no longer wired into MainWindow -- see cansniff/discovery/
bitrate.py's own docstring) and MainWindow's integration of the numerically-
scored engine into start_auto_scan()/stop_capture(): Auto Scan is a
distinct, explicit button/entry point from Start (start_capture), never
triggered implicitly by it and never gated by a Settings toggle (see
cansniff/ui/config_dialog.py). Opening the popup never scans anything by
itself -- only AutoScanDialog's own Start Scan button does, once the
operator has ticked candidate checkboxes and set a duration (see
tests/test_auto_scan_dialog.py for that widget-level gating). Start never
begins capturing until the operator explicitly picks a completed result row
and clicks Start Listening -- nothing here auto-selects a bitrate. Stop, the
dialog's own Cancel/Close button, and the dialog's window-close (X) all
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
        DiscoveryResult, DiscoveryStatus, ScanProgress, ScanResult, ScoredCandidate,
    )
    from cansniff.discovery.scoring import ScoringConfig, score_candidate
    from cansniff.model import CanFrame
    from cansniff.sources import CanFrameSource, SourceError
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


def _scored(bitrate=500000, score=80.0, completed=True, reasons=()):
    if not completed:
        return ScoredCandidate(
            bitrate=bitrate, requested_duration=10.0, observed_duration=0.0,
            settle_seconds=0.2, completed=False, total_score=0.0, components=None,
            reasons=reasons or ("Could not configure interface",))
    records = [
        (i * 0.5, CanFrame(timestamp=float(i), arb_id=0x100 + (i % 3), data=b"\x01\x02",
                           dlc=2, channel="0"))
        for i in range(int(score))  # more frames for a higher requested score
    ]
    components = score_candidate(records, 10.0, ScoringConfig())
    return ScoredCandidate(
        bitrate=bitrate, requested_duration=10.0, observed_duration=10.0,
        settle_seconds=0.2, completed=True, total_score=components.total_score,
        components=components, reasons=reasons)


def _scan_result(*candidates, interface="can0", cancelled=False, reasons=()):
    return ScanResult(interface, tuple(candidates), cancelled=cancelled, reasons=reasons)


def _run_scan(progress, *candidates, interface="can0"):
    """Fake-scanner helper: emits a realistic candidate-complete progress
    event for every candidate before returning -- exactly like the real
    scan_bitrate_candidates does, and exactly what actually populates
    AutoScanDialog's results table (on_progress, not on_result -- see its
    own module docstring)."""
    total = len(candidates)
    for index, candidate in enumerate(candidates):
        if progress is not None:
            progress(ScanProgress(
                "candidate-complete", "", index + 1, total,
                bitrate=candidate.bitrate, candidate=candidate))
    return _scan_result(*candidates, interface=interface)


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
        self.window._teardown_scan_thread()
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

    def _open_dialog_and_check(self, *bitrates):
        self.window.start_auto_scan()
        dialog = self.window._auto_scan_dialog
        for bitrate in bitrates:
            dialog._checkboxes[bitrate].setChecked(True)
        # Past start_auto_scan's own brief post-click debounce -- see
        # _lock_interactions -- so Stop/a second start_auto_scan is not
        # silently ignored by a test that acts on the dialog immediately.
        self.assertTrue(self._wait(lambda: not self.window._interaction_locked))
        return dialog

    # -- Start is never gated by scanning -----------------------------------

    def test_start_never_triggers_a_scan_regardless_of_legacy_config(self):
        """There is no Settings toggle for this any more (see
        cansniff/ui/config_dialog.py) -- Start is always the manually
        configured bitrate, even if an old config file still carries
        auto_bitrate: true from before this was removed."""
        self.config.set("source.live.auto_bitrate", True)
        scanner = mock.Mock()
        with mock.patch("cansniff.discovery.scan_bitrate_candidates", scanner), \
             mock.patch("cansniff.ui.main_window.build_source",
                        return_value=_FiniteSource()):
            self.window.start_capture()
            self.assertTrue(self._wait(lambda: self.window._thread is None
                                       and self.window._capture_state == self.window._IDLE))
        scanner.assert_not_called()
        self.assertIsNone(self.window._auto_scan_dialog)

    # -- opening the popup never scans anything by itself -------------------

    def test_auto_scan_opens_the_popup_without_scanning(self):
        scanner = mock.Mock()
        with mock.patch("cansniff.discovery.scan_bitrate_candidates", scanner):
            dialog = self._open_dialog_and_check()
            self.assertIsInstance(dialog, AutoScanDialog)
            self.assertEqual(dialog.interface, "can0")
            self.app.processEvents()
        scanner.assert_not_called()
        self.assertEqual(self.window._capture_state, self.window._DISCOVERING)
        self.assertIsNone(self.window._scan_worker)

    def test_dialog_shows_the_configured_channel(self):
        self.config.set("source.live.channel", "can1")
        dialog = self._open_dialog_and_check()
        self.assertEqual(dialog.interface_label.text(), "Interface: can1")

    def test_start_and_auto_scan_buttons_disabled_for_the_whole_popup_lifetime(self):
        """Disabled from the moment the popup opens -- not just while a
        worker happens to be running -- covering configuring, scanning, and
        showing finished results alike."""
        dialog = self._open_dialog_and_check()
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertFalse(self.window.auto_scan_button.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())
        self.window.stop_capture()
        self.assertTrue(self._wait(lambda: self.window.start_button.isEnabled()))
        self.assertTrue(self.window.auto_scan_button.isEnabled())

    def test_can_fd_refuses_auto_scan_with_an_explanatory_message(self):
        self.config.set("source.live.fd", True)
        scanner = mock.Mock()
        with mock.patch("cansniff.discovery.scan_bitrate_candidates", scanner), \
             mock.patch("cansniff.ui.main_window.QMessageBox.information") as informed:
            self.window.start_auto_scan()
        scanner.assert_not_called()
        informed.assert_called_once()
        self.assertIsNone(self.window._auto_scan_dialog)
        self.assertEqual(self.window._capture_state, self.window._IDLE)

    def test_auto_scan_targets_the_live_channel_even_when_source_type_is_file(self):
        """Auto Scan always operates on source.live.channel -- it is not
        gated by which Source Type Start would currently use."""
        self.config.set("source.type", "file")
        dialog = self._open_dialog_and_check()
        self.assertEqual(dialog.interface, "can0")

    # -- no scan without an explicit checkbox + duration + Start Scan -------

    def test_no_candidates_checked_shows_validation_and_never_calls_the_scanner(self):
        scanner = mock.Mock()
        with mock.patch("cansniff.discovery.scan_bitrate_candidates", scanner):
            dialog = self._open_dialog_and_check()  # nothing checked
            dialog.start_scan_button.click()
            self.app.processEvents()
        scanner.assert_not_called()
        self.assertNotEqual(dialog.validation_label.text(), "")

    def test_checking_boxes_and_start_scan_calls_the_scanner_with_the_selection(self):
        calls = []

        def scanner(interface, candidates=None, duration=None, settle_seconds=None,
                   scoring_config=None, cancel_event=None, progress=None):
            calls.append((interface, tuple(candidates), duration))
            return _run_scan(progress, _scored(500000))

        with mock.patch("cansniff.discovery.scan_bitrate_candidates", side_effect=scanner):
            dialog = self._open_dialog_and_check(125000, 500000)
            dialog.duration_spin.setValue(45)
            dialog.start_scan_button.click()
            self.assertTrue(self._wait(lambda: bool(calls)))
            self.assertTrue(self._wait(
                lambda: self.window._scan_worker is None))
        self.assertEqual(calls, [("can0", (125000, 500000), 45.0)])

    # -- progress reaches the dialog and populates the results table --------

    def test_progress_populates_the_results_table_for_every_candidate(self):
        def scanner(interface, candidates=None, duration=None, settle_seconds=None,
                   scoring_config=None, cancel_event=None, progress=None):
            for index, bitrate in enumerate(candidates):
                progress(ScanProgress(
                    "candidate-start", "Testing…", index, len(candidates), bitrate=bitrate))
                progress(ScanProgress(
                    "candidate-complete", "done", index + 1, len(candidates),
                    bitrate=bitrate, candidate=_scored(bitrate, score=40.0)))
            return _scan_result(*(_scored(b, score=40.0) for b in candidates))

        with mock.patch("cansniff.discovery.scan_bitrate_candidates", side_effect=scanner):
            dialog = self._open_dialog_and_check(125000, 250000, 500000)
            dialog.start_scan_button.click()
            self.assertTrue(self._wait(lambda: dialog.table.rowCount() == 3))
            self.assertTrue(self._wait(lambda: self.window._scan_worker is None))
        self.assertEqual(dialog.overall_progress.value(), 3)

    # -- Start Listening: never automatic --------------------------------

    def test_finished_scan_never_auto_starts_capture_or_selects_a_row(self):
        """Do not automatically select the highest-scoring bitrate; do not
        automatically start listening just because the scan finished."""
        def scanner(interface, candidates=None, duration=None, settle_seconds=None,
                   scoring_config=None, cancel_event=None, progress=None):
            return _run_scan(progress, _scored(125000, score=20.0), _scored(500000, score=90.0))

        with mock.patch("cansniff.discovery.scan_bitrate_candidates", side_effect=scanner), \
             mock.patch("cansniff.ui.main_window.build_source") as build_source:
            dialog = self._open_dialog_and_check(125000, 500000)
            dialog.start_scan_button.click()
            self.assertTrue(self._wait(lambda: dialog.status_label.text() != ""))
            self.app.processEvents()
            build_source.assert_not_called()
        self.assertIsNone(self.window._thread)
        self.assertEqual(self.window._capture_state, self.window._DISCOVERING)
        self.assertIsNone(dialog.selected_bitrate())
        self.assertFalse(dialog.start_listening_button.isEnabled())
        self.assertIsNotNone(self.window._auto_scan_dialog)

    def test_start_listening_reads_the_selected_rows_bitrate_reuses_capture_and_updates_chip(self):
        def scanner(interface, candidates=None, duration=None, settle_seconds=None,
                   scoring_config=None, cancel_event=None, progress=None):
            return _run_scan(progress, _scored(125000, score=10.0), _scored(500000, score=90.0))

        with mock.patch("cansniff.discovery.scan_bitrate_candidates", side_effect=scanner), \
             mock.patch("cansniff.ui.main_window.build_source",
                        return_value=_FiniteSource()):
            dialog = self._open_dialog_and_check(125000, 500000)
            dialog.start_scan_button.click()
            self.assertTrue(self._wait(lambda: dialog.table.rowCount() == 2))
            # The operator picks the *lower*-scoring row on purpose -- proves
            # the chosen bitrate is whatever was selected, never the highest
            # score automatically.
            row_for_125k = next(
                row for row in range(dialog.table.rowCount())
                if dialog.table.item(row, 0).text().startswith("125"))
            dialog.table.selectRow(row_for_125k)
            self.assertTrue(dialog.start_listening_button.isEnabled())
            dialog.start_listening_button.click()
            self.assertTrue(self._wait(lambda: self.window._thread is None
                                       and self.window._capture_state == self.window._IDLE))
        self.assertEqual(len(self.window.frame_store.all_frames()), 2)
        self.assertEqual(self.config.get("source.live.bitrate"), 125000)
        self.assertTrue(self.config.get("source.live.auto_bitrate"))
        self.assertIsNone(self.window._auto_scan_dialog)

    def test_configuration_error_after_start_listening_is_shown_and_recoverable(self):
        """If reconfiguration fails after the popup has already closed: show
        the error in the main window, never crash, and leave Auto Scan
        reopenable."""
        def scanner(interface, candidates=None, duration=None, settle_seconds=None,
                   scoring_config=None, cancel_event=None, progress=None):
            return _run_scan(progress, _scored(500000, score=90.0))

        with mock.patch("cansniff.discovery.scan_bitrate_candidates", side_effect=scanner), \
             mock.patch("cansniff.ui.main_window.SocketCanSessionController") as ctrl_cls, \
             mock.patch("cansniff.ui.main_window.QMessageBox.critical") as critical:
            ctrl_cls.return_value.prepare_manual.side_effect = SourceError(
                "Listen-only could not be confirmed")
            dialog = self._open_dialog_and_check(500000)
            dialog.start_scan_button.click()
            self.assertTrue(self._wait(lambda: dialog.table.rowCount() == 1))
            dialog.table.selectRow(0)
            dialog.start_listening_button.click()
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))
        critical.assert_called_once()
        self.assertIsNone(self.window._thread)
        self.assertIsNone(self.window._auto_scan_dialog)
        # Past _capture_state == Idle *and* past _lock_interactions' own
        # brief post-action debounce -- see _refresh_capture_controls.
        self.assertTrue(self._wait(lambda: self.window.auto_scan_button.isEnabled()))

    # -- cancellation ---------------------------------------------------

    def test_stop_before_any_scan_starts_just_closes_the_popup(self):
        dialog = self._open_dialog_and_check()  # configuring, no worker yet
        self.window.stop_capture()
        self.assertTrue(self._wait(
            lambda: self.window._capture_state == self.window._IDLE))
        self.assertIsNone(self.window._auto_scan_dialog)
        self.assertTrue(dialog.isHidden())

    def test_stop_cancels_an_in_progress_scan(self):
        entered = threading.Event()

        def scanner(interface, candidates=None, duration=None, settle_seconds=None,
                   scoring_config=None, cancel_event=None, progress=None):
            entered.set()
            cancel_event.wait(2.0)
            return _scan_result(cancelled=True)

        with mock.patch("cansniff.discovery.scan_bitrate_candidates", side_effect=scanner):
            dialog = self._open_dialog_and_check(500000)
            dialog.start_scan_button.click()
            self.assertTrue(entered.wait(1.0))
            self.assertTrue(self._wait(lambda: self.window.stop_button.isEnabled()))
            self.window.stop_capture()
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))
        self.assertIsNone(self.window._scan_thread)
        self.assertIsNone(self.window._scan_worker)
        self.assertIsNone(self.window._thread, "cancellation must never start capture")
        # Cancellation closes the popup -- it must not linger invisibly.
        self.assertIsNone(self.window._auto_scan_dialog)

    def test_dialog_cancel_button_uses_the_same_path_as_main_window_stop(self):
        """Requirement: popup Cancel, the popup's own X, and the main Stop
        button all invoke exactly one cancellation mechanism."""
        entered = threading.Event()

        def scanner(interface, candidates=None, duration=None, settle_seconds=None,
                   scoring_config=None, cancel_event=None, progress=None):
            entered.set()
            cancel_event.wait(2.0)
            return _scan_result(cancelled=True)

        with mock.patch("cansniff.discovery.scan_bitrate_candidates", side_effect=scanner), \
             mock.patch.object(MainWindow, "stop_capture") as stop_spy:
            dialog = self._open_dialog_and_check(500000)
            dialog.start_scan_button.click()
            self.assertTrue(entered.wait(1.0))
            dialog.action_button.click()
        stop_spy.assert_called_once()

    def test_closing_the_dialog_window_cancels_safely(self):
        entered = threading.Event()

        def scanner(interface, candidates=None, duration=None, settle_seconds=None,
                   scoring_config=None, cancel_event=None, progress=None):
            entered.set()
            cancel_event.wait(2.0)
            return _scan_result(cancelled=True)

        with mock.patch("cansniff.discovery.scan_bitrate_candidates", side_effect=scanner):
            dialog = self._open_dialog_and_check(500000)
            dialog.start_scan_button.click()
            self.assertTrue(entered.wait(1.0))
            self.assertTrue(self._wait(lambda: self.window.stop_button.isEnabled()))
            self.window._auto_scan_dialog.close()
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))
        self.assertIsNone(self.window._thread, "cancellation must never start capture")

    def test_stale_finished_dialog_close_does_not_cancel_a_later_scan(self):
        """The identity check in start_auto_scan's cancel wiring: an
        operator who leaves a finished results dialog open and then starts
        -- and later closes the stale window during -- a *second* scan must
        not have that second scan cancelled."""
        def quick_scanner(interface, candidates=None, duration=None, settle_seconds=None,
                          scoring_config=None, cancel_event=None, progress=None):
            return _run_scan(progress, _scored(500000))

        with mock.patch("cansniff.discovery.scan_bitrate_candidates",
                        side_effect=quick_scanner):
            first_dialog = self._open_dialog_and_check(500000)
            first_dialog.start_scan_button.click()
            self.assertTrue(self._wait(lambda: first_dialog.table.rowCount() == 1))
            self.assertTrue(self._wait(lambda: self.window.auto_scan_button.isEnabled()
                                       is False))
        self.assertIsNotNone(first_dialog)
        self.assertTrue(first_dialog.isVisible() or not first_dialog.isHidden())

        entered = threading.Event()

        def blocking_scanner(interface, candidates=None, duration=None, settle_seconds=None,
                             scoring_config=None, cancel_event=None, progress=None):
            entered.set()
            cancel_event.wait(2.0)
            return _scan_result(cancelled=True)

        with mock.patch("cansniff.discovery.scan_bitrate_candidates",
                        side_effect=blocking_scanner):
            # A fresh Auto Scan while the first, finished dialog is still
            # open showing its own results.
            second_dialog = AutoScanDialog("can0", (500000,), self.window.theme, 30.0,
                                           self.window)
            second_dialog.cancelled.connect(
                lambda d=second_dialog: self.window.stop_capture()
                if d is self.window._auto_scan_dialog else None)
            second_dialog.scanRequested.connect(
                lambda cands, dur, d=second_dialog: self.window._on_scan_requested(
                    d, cands, dur))
            second_dialog.startListening.connect(self.window._on_scan_start_listening)
            self.window._auto_scan_dialog = second_dialog
            self.window._apply_capture_state(self.window._DISCOVERING)
            second_dialog._checkboxes[500000].setChecked(True)
            second_dialog.start_scan_button.click()
            self.assertTrue(entered.wait(1.0))
            # Close the *first*, already-finished (and no longer referenced)
            # dialog -- must not cancel the second scan, which is still
            # actively running.
            first_dialog.close()
            self.app.processEvents()
            self.assertEqual(self.window._capture_state, self.window._DISCOVERING)
            self.assertTrue(self._wait(lambda: self.window.stop_button.isEnabled()))
            self.window.stop_capture()
            self.assertTrue(self._wait(
                lambda: self.window._capture_state == self.window._IDLE))

    def test_main_window_close_cancels_active_scan(self):
        entered = threading.Event()

        def scanner(interface, candidates=None, duration=None, settle_seconds=None,
                   scoring_config=None, cancel_event=None, progress=None):
            entered.set()
            cancel_event.wait(2.0)
            return _scan_result(cancelled=True)

        with mock.patch("cansniff.discovery.scan_bitrate_candidates", side_effect=scanner):
            dialog = self._open_dialog_and_check(500000)
            dialog.start_scan_button.click()
            self.assertTrue(entered.wait(1.0))
            self.window._teardown_scan_thread()
        self.assertIsNone(self.window._scan_thread)
        self.assertIsNone(self.window._scan_worker)
        self.assertIsNone(self.window._auto_scan_dialog)
        self.assertTrue(dialog.isHidden())


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
