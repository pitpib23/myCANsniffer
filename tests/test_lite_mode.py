"""Regression coverage for the Lite (800x480, Messages/Trace-only) edition.

``MainWindow(..., lite=True)`` and ``AutoScanDialog(..., lite=True)`` are
additive, default-``False`` constructor parameters (see their own
docstrings in cansniff/ui/main_window.py and cansniff/ui/auto_scan_dialog.py)
-- every test in tests/test_discovery_ui.py, tests/test_auto_scan_dialog.py,
tests/test_responsive_layout.py etc. already covers the full edition's own
retained behavior, and none of it changed. This file instead pins down the
two things that actually differ in Lite:

  1. Navigation is trimmed to Messages/Trace -- ISO-TP/Protocols/Compare/
     Matches are still built and still live on ``top_stack`` (nothing about
     their own construction, wiring or backend changes -- only the nav
     rail's own button list does), simply unreachable from the rail.
  2. AutoScanDialog's results table shows only Bitrate/Score, with the
     identical score value/text the full edition's own extra-columns table
     would show for the same ``ScoredCandidate`` -- see the score-
     compatibility tests below (success criterion #8).

Both defaults stay ``False``, so this file also spot-checks that the full
edition -- the actual default, unconditionally used by every other test
file -- is unaffected by their mere existence.
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    import main as main_module
    from cansniff.config import Config
    from cansniff.discovery.model import ScanProgress, ScoredCandidate
    from cansniff.discovery.scoring import ScoringConfig, score_candidate
    from cansniff.model import CanFrame
    from cansniff.ui.auto_scan_dialog import AutoScanDialog
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.theme import Theme


def _completed_candidate(bitrate, n_frames=10):
    records = [
        (i * 0.5, CanFrame(timestamp=float(i), arb_id=0x100 + (i % 3), data=b"\x01\x02",
                           dlc=2, channel="0"))
        for i in range(n_frames)
    ]
    components = score_candidate(records, 10.0, ScoringConfig())
    return ScoredCandidate(
        bitrate=bitrate, requested_duration=10.0, observed_duration=10.0,
        settle_seconds=0.2, completed=True, total_score=components.total_score,
        components=components)


def _failed_candidate(bitrate, reason="Could not configure interface"):
    return ScoredCandidate(
        bitrate=bitrate, requested_duration=10.0, observed_duration=0.0,
        settle_seconds=0.2, completed=False, total_score=0.0, components=None,
        reasons=(reason,))


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class MainWindowNavigationTests(unittest.TestCase):
    """Success criteria #1/#2/#4: Lite exposes only Messages/Trace, and the
    removed pages neither disappear from the backend nor break anything
    that still reaches them directly (e.g. project restore).
    """

    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, lite):
        path = os.path.join(
            tempfile.gettempdir(),
            "cansniff_lite_nav_{}_{}.json".format(lite, id(self)))
        window = MainWindow(Config.defaults(path), Theme(), lite=lite)
        self.addCleanup(self._clean, window, path)
        return window

    @classmethod
    def _clean(cls, window, path):
        window._teardown_thread()
        window.close()
        window.deleteLater()
        cls.app.processEvents()
        if os.path.exists(path):
            os.remove(path)

    def test_default_edition_keeps_all_six_destinations(self):
        """The actual default (lite=False, what every other test file
        constructs) must be byte-for-byte what pi always built."""
        window = self._window(lite=False)
        self.assertFalse(window.lite)
        for index, label in enumerate(
                ("Messages", "Trace", "ISO-TP", "Protocols", "Compare", "Matches")):
            button = window.nav.group.button(index)
            self.assertIsNotNone(button, label)
            self.assertEqual(button.accessibleName(), label)
        self.assertEqual(window.windowTitle(), "CAN Sniffer — passive receive-only")

    def test_lite_edition_exposes_only_messages_and_trace(self):
        window = self._window(lite=True)
        self.assertTrue(window.lite)
        self.assertEqual(window.nav.group.button(0).accessibleName(), "Messages")
        self.assertEqual(window.nav.group.button(1).accessibleName(), "Trace")
        for index in (2, 3, 4, 5):
            self.assertIsNone(window.nav.group.button(index))
        self.assertIn("Lite", window.windowTitle())

    def test_lite_backend_pages_still_exist_just_unreachable_from_nav(self):
        window = self._window(lite=True)
        self.assertIsNotNone(window.isotp_view)
        self.assertIsNotNone(window.protocols_view)
        self.assertIsNotNone(window.compare_view)
        self.assertIsNotNone(window.profile_matches_view)
        self.assertEqual(window.top_stack.count(), 5)
        # Still directly reachable/functional, exactly like the full
        # edition's -- e.g. a project saved outside Lite that names one of
        # them as its active_workspace (_restore_project_context) keeps
        # working rather than raising/crashing in Lite.
        window._activate_isotp()
        self.assertEqual(window.top_stack.currentIndex(), window._STACK_ISOTP)
        window._activate_protocols()
        self.assertEqual(window.top_stack.currentIndex(), window._STACK_PROTOCOLS)
        window._activate_compare()
        self.assertEqual(window.top_stack.currentIndex(), window._STACK_COMPARE)
        window._activate_profile_matches()
        self.assertEqual(window.top_stack.currentIndex(), window._STACK_PROFILE_MATCHES)
        window._activate_browser(window._NAV_MESSAGES)
        self.assertEqual(window.top_stack.currentIndex(), window._STACK_BROWSER)

    def test_full_edition_messages_nav_click_still_toggles_the_sidebar(self):
        """Unchanged full-edition behavior: clicking the already-open
        Messages/Trace destination toggles the packet-list sidebar (see
        _on_nav_clicked/set_sidebar_collapsed) -- same _sidebar_collapsed
        bookkeeping as before this iteration's Lite-only layout changes.
        """
        window = self._window(lite=False)
        window.show()
        self.app.processEvents()
        self.assertFalse(window._sidebar_collapsed)
        window.nav.group.button(window._NAV_MESSAGES).click()
        self.assertTrue(window._sidebar_collapsed)
        window.nav.group.button(window._NAV_MESSAGES).click()
        self.assertFalse(window._sidebar_collapsed)
        window.nav.group.button(window._NAV_TRACE).click()
        self.assertEqual(window.browser_stack.currentIndex(), window._NAV_TRACE)
        self.assertEqual(window.section_label.text(), "Trace")

    def test_lite_messages_nav_click_switches_sections_not_a_sidebar(self):
        """Lite's own redesigned single-page layout (see
        cansniff/ui/main_window.py's Lite section): there is no partial
        sidebar split to toggle any more -- see tests/test_lite_layout.py
        for the full single-page-workspace coverage; this only pins down
        that the *nav click* path still switches sections correctly.
        """
        window = self._window(lite=True)
        window.show()
        self.app.processEvents()
        self.assertTrue(window.browser_panel.isVisible())
        self.assertTrue(window.interpret_view.isVisible())
        window.nav.group.button(window._NAV_TRACE).click()
        self.assertEqual(window.browser_stack.currentIndex(), window._NAV_TRACE)
        self.assertEqual(window.section_label.text(), "Trace")
        self.assertTrue(window.browser_panel.isVisible())


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class AutoScanDialogLiteColumnsTests(unittest.TestCase):
    """Success criteria #7/#8/#9/#10: Lite's results table shows only
    Bitrate/Score, the score text is identical to the full edition's own,
    and every scan/selection/cancellation semantic is unchanged.
    """

    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self):
        self.app.processEvents()

    def test_lite_table_shows_only_bitrate_and_score_headers(self):
        dialog = AutoScanDialog("can0", (125000, 500000), Theme(), 30.0, lite=True)
        try:
            headers = [dialog.table.horizontalHeaderItem(i).text()
                      for i in range(dialog.table.columnCount())]
            self.assertEqual(headers, ["Bitrate", "Score"])
        finally:
            dialog.deleteLater()

    def test_full_edition_table_is_unaffected_by_lite_existing(self):
        dialog = AutoScanDialog("can0", (125000, 500000), Theme(), 30.0)
        try:
            self.assertFalse(dialog.lite)
            self.assertEqual(dialog.table.columnCount(), 11)
        finally:
            dialog.deleteLater()

    def test_lite_completed_row_has_exactly_two_cells(self):
        dialog = AutoScanDialog("can0", (500000,), Theme(), 30.0, lite=True)
        try:
            dialog.on_progress(ScanProgress(
                "candidate-complete", "", 1, 1, bitrate=500000,
                candidate=_completed_candidate(500000)))
            self.assertEqual(dialog.table.columnCount(), 2)
            self.assertRegex(dialog.table.item(0, 1).text(), r"^\d+\.\d$")
        finally:
            dialog.deleteLater()

    def test_lite_incomplete_candidate_still_shows_na(self):
        """Never a silently different/hidden failure state -- same N/A the
        full edition shows for a candidate that did not complete."""
        dialog = AutoScanDialog("can0", (125000,), Theme(), 30.0, lite=True)
        try:
            dialog.on_progress(ScanProgress(
                "candidate-complete", "", 1, 1, bitrate=125000,
                candidate=_failed_candidate(125000)))
            self.assertEqual(dialog.table.item(0, 1).text(), "N/A")
        finally:
            dialog.deleteLater()

    def test_score_text_is_identical_between_lite_and_full_editions(self):
        """Success criterion #8, verbatim: the same ScoredCandidate must
        produce the same displayed score text in both editions -- Lite only
        makes it bigger/bolder, never a different number, rounding or
        scale.
        """
        candidate = _completed_candidate(500000)
        full = AutoScanDialog("can0", (500000,), Theme(), 30.0)
        lite = AutoScanDialog("can0", (500000,), Theme(), 30.0, lite=True)
        try:
            for dialog in (full, lite):
                dialog.on_progress(ScanProgress(
                    "candidate-complete", "", 1, 1, bitrate=500000, candidate=candidate))
            self.assertEqual(full.table.item(0, 1).text(), lite.table.item(0, 1).text())
            self.assertEqual(
                lite.table.item(0, 1).text(), "{:.1f}".format(candidate.total_score))
        finally:
            full.deleteLater()
            lite.deleteLater()

    def test_lite_selection_and_start_listening_match_full_edition(self):
        for lite in (False, True):
            with self.subTest(lite=lite):
                dialog = AutoScanDialog("can0", (125000, 500000), Theme(), 30.0, lite=lite)
                try:
                    for bitrate in (125000, 500000):
                        dialog.on_progress(ScanProgress(
                            "candidate-complete", "", 1, 2, bitrate=bitrate,
                            candidate=_completed_candidate(bitrate)))
                    # Never auto-selected -- same explicit-choice requirement
                    # as the full edition's own (see
                    # test_scan_does_not_auto_select_the_highest_scoring_row).
                    self.assertIsNone(dialog.selected_bitrate())
                    self.assertFalse(dialog.start_listening_button.isEnabled())
                    dialog.table.selectRow(1)
                    self.assertEqual(dialog.selected_bitrate(), 500000)
                    self.assertTrue(dialog.start_listening_button.isEnabled())
                    emitted = []
                    dialog.startListening.connect(emitted.append)
                    dialog.start_listening_button.click()
                    self.assertEqual(emitted, [500000])
                finally:
                    dialog.deleteLater()

    def test_lite_incomplete_row_does_not_enable_start_listening(self):
        dialog = AutoScanDialog("can0", (125000,), Theme(), 30.0, lite=True)
        try:
            dialog.on_progress(ScanProgress(
                "candidate-complete", "", 1, 1, bitrate=125000,
                candidate=_failed_candidate(125000)))
            dialog.table.selectRow(0)
            self.assertFalse(dialog.start_listening_button.isEnabled())
        finally:
            dialog.deleteLater()

    def test_lite_cancellation_still_emits_cancelled_on_close(self):
        dialog = AutoScanDialog("can0", (500000,), Theme(), 30.0, lite=True)
        try:
            emitted = []
            dialog.cancelled.connect(lambda: emitted.append(True))
            dialog.close()
            self.assertEqual(emitted, [True])
        finally:
            dialog.deleteLater()

    def test_lite_scanning_phase_still_disables_configuration(self):
        dialog = AutoScanDialog("can0", (500000,), Theme(), 30.0, lite=True)
        try:
            dialog.set_scanning(True)
            self.assertFalse(dialog.config_group.isEnabled())
            self.assertFalse(dialog.start_scan_button.isEnabled())
            self.assertEqual(dialog.action_button.text(), "Cancel")
            dialog.set_scanning(False)
            self.assertTrue(dialog.config_group.isEnabled())
            self.assertEqual(dialog.action_button.text(), "Close")
        finally:
            dialog.deleteLater()

    def test_lite_row_height_and_button_height_are_touch_sized(self):
        """7-inch display requirement: ~44-48px touch targets."""
        dialog = AutoScanDialog("can0", (500000,), Theme(), 30.0, lite=True)
        try:
            self.assertGreaterEqual(dialog.table.verticalHeader().defaultSectionSize(), 44)
            for button in (dialog.start_scan_button, dialog.start_listening_button,
                           dialog.action_button):
                self.assertGreaterEqual(button.minimumHeight(), 44)
        finally:
            dialog.deleteLater()

    def test_full_edition_row_height_keeps_its_original_qt_default(self):
        """Lite's own touch-height row floor (_LITE_ROW_HEIGHT) must never
        leak into the full edition's popup -- its row height stays
        whatever Qt's own default section size is, exactly as before this
        change, not the value Lite happens to use."""
        default_dialog = AutoScanDialog("can0", (500000,), Theme(), 30.0)
        lite_dialog = AutoScanDialog("can0", (500000,), Theme(), 30.0, lite=True)
        try:
            self.assertLess(
                default_dialog.table.verticalHeader().defaultSectionSize(),
                lite_dialog.table.verticalHeader().defaultSectionSize())
        finally:
            default_dialog.deleteLater()
            lite_dialog.deleteLater()

    def test_lite_initial_size_fits_within_800x480(self):
        dialog = AutoScanDialog("can0", (500000,), Theme(), 30.0, lite=True)
        try:
            self.assertLessEqual(dialog.width(), 800)
            self.assertLessEqual(dialog.height(), 480)
        finally:
            dialog.deleteLater()


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class MainWindowLiteAutoScanIntegrationTests(unittest.TestCase):
    """MainWindow.start_auto_scan actually hands ``lite=`` through to the
    popup -- see cansniff/ui/main_window.py's own AutoScanDialog(...) call.
    """

    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, lite):
        path = os.path.join(
            tempfile.gettempdir(),
            "cansniff_lite_scan_main_{}_{}.json".format(lite, id(self)))
        config = Config.defaults(path)
        config.set("source.type", "live")
        config.set("source.live.channel", "can0")
        window = MainWindow(config, Theme(), lite=lite)
        self.addCleanup(self._clean, window, path)
        return window

    @classmethod
    def _clean(cls, window, path):
        window._teardown_scan_thread()
        window._teardown_thread()
        window.close()
        window.deleteLater()
        cls.app.processEvents()
        if os.path.exists(path):
            os.remove(path)

    def test_lite_main_window_opens_a_lite_auto_scan_dialog(self):
        window = self._window(lite=True)
        window.start_auto_scan()
        self.assertTrue(window._auto_scan_dialog.lite)
        self.assertEqual(window._auto_scan_dialog.table.columnCount(), 2)
        self.assertEqual(window._auto_scan_dialog.interface, "can0")

    def test_full_main_window_still_opens_the_full_auto_scan_dialog(self):
        window = self._window(lite=False)
        window.start_auto_scan()
        self.assertFalse(window._auto_scan_dialog.lite)
        self.assertEqual(window._auto_scan_dialog.table.columnCount(), 11)


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class LiteEntryPointArgsTests(unittest.TestCase):
    """main.py's --lite flag -- argument parsing only. The rest of main()
    (Qt/event-loop) is exercised through MainWindow/AutoScanDialog directly
    by every test above and elsewhere in this suite, not by calling main()
    itself, which blocks on app.exec().
    """

    def test_lite_flag_defaults_to_false(self):
        args = main_module.parse_args([])
        self.assertFalse(args.lite)

    def test_lite_flag_can_be_set(self):
        args = main_module.parse_args(["--lite"])
        self.assertTrue(args.lite)

    def test_lite_flag_is_independent_of_other_arguments(self):
        args = main_module.parse_args(["--lite", "--monitor"])
        self.assertTrue(args.lite)
        self.assertTrue(args.monitor)


if __name__ == "__main__":
    unittest.main()
