"""Widget-level coverage for the rewritten AutoScanDialog: checkbox-only
candidate selection (no free-form bitrate entry), the duration field, the
numerical results table (no qualitative labels), and Start Listening's
gating -- independent of MainWindow's own orchestration (see
tests/test_discovery_ui.py for that integration layer).
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QLineEdit, QSpinBox
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.discovery.model import ScanProgress, ScanResult, ScoredCandidate
    from cansniff.discovery.scoring import ScoringConfig, score_candidate
    from cansniff.model import CanFrame
    from cansniff.ui.auto_scan_dialog import AutoScanDialog, validate_scan_request
    from cansniff.ui.theme import Theme

_CANDIDATES = (125000, 250000, 500000)


def _completed_candidate(bitrate, score=80.0, n_frames=10):
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
class ValidationTests(unittest.TestCase):
    def test_no_candidates_selected_is_rejected(self):
        self.assertIsNotNone(validate_scan_request((), 30.0))

    def test_duration_below_minimum_is_rejected(self):
        self.assertIsNotNone(validate_scan_request((500000,), 9.9))

    def test_zero_or_negative_duration_is_rejected(self):
        self.assertIsNotNone(validate_scan_request((500000,), 0))
        self.assertIsNotNone(validate_scan_request((500000,), -5))

    def test_non_numeric_duration_is_rejected(self):
        self.assertIsNotNone(validate_scan_request((500000,), "not-a-number"))

    def test_valid_request_is_accepted(self):
        self.assertIsNone(validate_scan_request((500000,), 30.0))
        self.assertIsNone(validate_scan_request((500000,), 10.0))  # exactly the minimum


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class AutoScanDialogTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, default_duration=30.0):
        return AutoScanDialog("can0", _CANDIDATES, Theme(), default_duration)

    def tearDown(self):
        self.app.processEvents()

    # -- checkbox-only candidate selection -------------------------------

    def test_one_checkbox_per_candidate_bitrate_no_free_text_field(self):
        dialog = self._dialog()
        try:
            self.assertEqual(set(dialog._checkboxes.keys()), set(_CANDIDATES))
            for box in dialog._checkboxes.values():
                self.assertFalse(box.isChecked())
            # No free-form bitrate entry anywhere in the dialog -- the only
            # QLineEdit that can legitimately exist is QSpinBox's own
            # internal editor for the numeric duration field, not a
            # comma-separated/free-typed bitrate list.
            stray_line_edits = [
                edit for edit in dialog.findChildren(QLineEdit)
                if edit.objectName() != "qt_spinbox_lineedit"
            ]
            self.assertEqual(stray_line_edits, [])
        finally:
            dialog.deleteLater()

    def test_only_checked_boxes_become_selected_candidates(self):
        dialog = self._dialog()
        try:
            dialog._checkboxes[125000].setChecked(True)
            dialog._checkboxes[500000].setChecked(True)
            self.assertEqual(dialog.selected_candidates(), (125000, 500000))
        finally:
            dialog.deleteLater()

    def test_no_selection_shows_validation_and_does_not_emit_scan_requested(self):
        dialog = self._dialog()
        try:
            emitted = []
            dialog.scanRequested.connect(lambda *args: emitted.append(args))
            dialog.start_scan_button.click()
            self.assertEqual(emitted, [])
            self.assertNotEqual(dialog.validation_label.text(), "")
        finally:
            dialog.deleteLater()

    def test_checking_a_box_and_clicking_start_scan_emits_the_selection(self):
        dialog = self._dialog()
        try:
            dialog._checkboxes[500000].setChecked(True)
            emitted = []
            dialog.scanRequested.connect(lambda cands, dur: emitted.append((cands, dur)))
            dialog.start_scan_button.click()
            self.assertEqual(len(emitted), 1)
            candidates, duration = emitted[0]
            self.assertEqual(tuple(candidates), (500000,))
            self.assertEqual(duration, 30.0)
        finally:
            dialog.deleteLater()

    # -- duration field ---------------------------------------------------

    def test_duration_default_is_30_seconds(self):
        dialog = self._dialog()
        try:
            self.assertEqual(dialog.duration_spin.value(), 30)
        finally:
            dialog.deleteLater()

    def test_duration_minimum_is_10_seconds_on_the_widget_itself(self):
        dialog = self._dialog()
        try:
            self.assertEqual(dialog.duration_spin.minimum(), 10)
            dialog.duration_spin.setValue(1)
            self.assertEqual(dialog.duration_spin.value(), 10)
        finally:
            dialog.deleteLater()

    def test_selected_duration_is_displayed(self):
        dialog = self._dialog()
        try:
            dialog.duration_spin.setValue(45)
            self.assertIn("45", dialog.duration_display.text())
        finally:
            dialog.deleteLater()

    # -- scanning phase disables selection ---------------------------------

    def test_set_scanning_disables_candidate_selection_and_start_scan(self):
        dialog = self._dialog()
        try:
            dialog.set_scanning(True)
            self.assertFalse(dialog.config_group.isEnabled())
            self.assertFalse(dialog.start_scan_button.isEnabled())
            self.assertEqual(dialog.action_button.text(), "Cancel")
        finally:
            dialog.deleteLater()

    def test_set_scanning_hides_the_now_unusable_config_group(self):
        """Found live, on real hardware, mid-scan: merely disabling
        config_group while leaving it visible let this dialog's outer
        layout shrink it toward the checkbox FlowLayout's own
        under-reported minimumSize() (see widgets.FlowLayout), clipping a
        checkbox row or overlapping the row below it depending on exactly
        how the squeeze landed. Hiding it entirely (every control in it is
        unusable while scanning anyway -- candidate_label already names
        the bitrate under test) removes the deficit that caused either
        symptom, rather than relocating it.
        """
        dialog = self._dialog()
        try:
            # isHidden(), not isVisible(): this dialog is never shown in
            # this offscreen test, so isVisible() would read False
            # throughout regardless -- isHidden() reflects config_group's
            # own explicit shown/hidden flag independent of that.
            self.assertFalse(dialog.config_group.isHidden())
            dialog.set_scanning(True)
            self.assertTrue(dialog.config_group.isHidden())
            dialog.set_scanning(False)
            self.assertFalse(dialog.config_group.isHidden())
        finally:
            dialog.deleteLater()

    def test_set_scanning_false_restores_selection_and_relabels_close(self):
        dialog = self._dialog()
        try:
            dialog.set_scanning(True)
            dialog.set_scanning(False)
            self.assertTrue(dialog.config_group.isEnabled())
            self.assertFalse(dialog.config_group.isHidden())
            self.assertTrue(dialog.start_scan_button.isEnabled())
            self.assertEqual(dialog.action_button.text(), "Close")
        finally:
            dialog.deleteLater()

    # -- one phase on screen at a time: configure, scan, or review results --

    def test_three_phases_show_exactly_one_of_config_progress_table(self):
        """Exactly one of config_group/progress_group/self.table is ever
        shown: pick candidates, watch it scan, read what it found -- never
        two of those at once. See set_scanning's own comment.
        """
        dialog = self._dialog()
        try:
            # Phase 1: configuring. Nothing scanned yet.
            self.assertFalse(dialog.config_group.isHidden())
            self.assertTrue(dialog.progress_group.isHidden())
            self.assertTrue(dialog.table.isHidden())

            # Phase 2: scanning. Rows can already be arriving in the
            # background (candidate-complete fires mid-scan, before the
            # scan as a whole finishes) -- the table stays hidden anyway
            # until the *whole* scan is done, not just its first candidate.
            dialog.set_scanning(True)
            dialog.on_progress(ScanProgress(
                "candidate-complete", "500 kbit/s: score 80.0/100", 1, 2,
                bitrate=500000, candidate=_completed_candidate(500000)))
            self.assertTrue(dialog.config_group.isHidden())
            self.assertFalse(dialog.progress_group.isHidden())
            self.assertTrue(dialog.table.isHidden())
            self.assertEqual(dialog.table.rowCount(), 1, "still collecting in the background")

            # Phase 3: results. The scan is over; config_group stays out of
            # the way (nothing left to configure until a fresh scan is
            # requested) and the table -- now with real rows -- takes over.
            dialog.set_scanning(False)
            self.assertTrue(dialog.config_group.isHidden())
            self.assertTrue(dialog.progress_group.isHidden())
            self.assertFalse(dialog.table.isHidden())
        finally:
            dialog.deleteLater()

    def test_a_scan_with_nothing_to_show_goes_back_to_configuring(self):
        """An error before any candidate completed leaves self._candidates
        empty -- reviewing an empty table would be useless, so this goes
        back to configuring (where the operator can try again) instead of
        finishing on a blank results phase.
        """
        dialog = self._dialog()
        try:
            dialog.set_scanning(True)
            dialog.on_error("Could not configure can0: denied")
            dialog.set_scanning(False)
            self.assertFalse(dialog.config_group.isHidden())
            self.assertTrue(dialog.table.isHidden())
        finally:
            dialog.deleteLater()

    # -- results table: numeric only, no qualitative labels -----------------

    def test_completed_candidate_row_shows_numeric_score_and_components(self):
        dialog = self._dialog()
        try:
            dialog.on_progress(ScanProgress(
                "candidate-complete", "500 kbit/s: score 80.0/100", 1, 1,
                bitrate=500000, candidate=_completed_candidate(500000)))
            self.assertEqual(dialog.table.rowCount(), 1)
            score_text = dialog.table.item(0, 1).text()
            self.assertRegex(score_text, r"^\d+\.\d$")
            for banned in ("strong", "weak", "possible", "high confidence",
                           "low confidence", "rejected", "stable"):
                for col in range(dialog.table.columnCount()):
                    item = dialog.table.item(0, col)
                    self.assertNotIn(banned, (item.text() if item else "").lower())
        finally:
            dialog.deleteLater()

    def test_candidate_with_no_usable_data_shows_na_and_a_technical_reason(self):
        dialog = self._dialog()
        try:
            dialog.on_progress(ScanProgress(
                "candidate-complete", "125 kbit/s: configuration rejected", 1, 1,
                bitrate=125000,
                candidate=_failed_candidate(125000, "Could not configure can0: denied")))
            self.assertEqual(dialog.table.item(0, 1).text(), "N/A")
            details = dialog.table.item(0, dialog.table.columnCount() - 1).text()
            self.assertIn("denied", details)
        finally:
            dialog.deleteLater()

    def test_results_are_selectable_rows(self):
        dialog = self._dialog()
        try:
            for bitrate in (125000, 500000):
                dialog.on_progress(ScanProgress(
                    "candidate-complete", "", 1, 2, bitrate=bitrate,
                    candidate=_completed_candidate(bitrate)))
            dialog.table.selectRow(1)
            self.assertEqual(dialog.selected_bitrate(), 500000)
        finally:
            dialog.deleteLater()

    # -- Start Listening gating --------------------------------------------

    def test_start_listening_disabled_with_no_selection(self):
        dialog = self._dialog()
        try:
            dialog.on_progress(ScanProgress(
                "candidate-complete", "", 1, 1, bitrate=500000,
                candidate=_completed_candidate(500000)))
            self.assertFalse(dialog.start_listening_button.isEnabled())
        finally:
            dialog.deleteLater()

    def test_selecting_a_completed_row_enables_start_listening(self):
        dialog = self._dialog()
        try:
            dialog.on_progress(ScanProgress(
                "candidate-complete", "", 1, 1, bitrate=500000,
                candidate=_completed_candidate(500000)))
            dialog.table.selectRow(0)
            self.assertTrue(dialog.start_listening_button.isEnabled())
        finally:
            dialog.deleteLater()

    def test_selecting_an_incomplete_row_does_not_enable_start_listening(self):
        dialog = self._dialog()
        try:
            dialog.on_progress(ScanProgress(
                "candidate-complete", "", 1, 1, bitrate=125000,
                candidate=_failed_candidate(125000)))
            dialog.table.selectRow(0)
            self.assertFalse(dialog.start_listening_button.isEnabled())
        finally:
            dialog.deleteLater()

    def test_start_listening_disabled_while_scanning_even_with_a_selection(self):
        dialog = self._dialog()
        try:
            dialog.on_progress(ScanProgress(
                "candidate-complete", "", 1, 1, bitrate=500000,
                candidate=_completed_candidate(500000)))
            dialog.table.selectRow(0)
            self.assertTrue(dialog.start_listening_button.isEnabled())
            dialog.set_scanning(True)
            self.assertFalse(dialog.start_listening_button.isEnabled())
        finally:
            dialog.deleteLater()

    def test_clicking_start_listening_emits_the_selected_rows_bitrate(self):
        dialog = self._dialog()
        try:
            for bitrate in (125000, 500000):
                dialog.on_progress(ScanProgress(
                    "candidate-complete", "", 1, 2, bitrate=bitrate,
                    candidate=_completed_candidate(bitrate)))
            dialog.table.selectRow(1)
            emitted = []
            dialog.startListening.connect(emitted.append)
            dialog.start_listening_button.click()
            self.assertEqual(emitted, [500000])
        finally:
            dialog.deleteLater()

    def test_scan_does_not_auto_select_the_highest_scoring_row(self):
        dialog = self._dialog()
        try:
            for bitrate in (125000, 500000):
                dialog.on_progress(ScanProgress(
                    "candidate-complete", "", 1, 2, bitrate=bitrate,
                    candidate=_completed_candidate(bitrate)))
            self.assertIsNone(dialog.selected_bitrate())
            self.assertFalse(dialog.start_listening_button.isEnabled())
        finally:
            dialog.deleteLater()

    # -- cancellation ------------------------------------------------------

    def test_close_emits_cancelled(self):
        dialog = self._dialog()
        try:
            emitted = []
            dialog.cancelled.connect(lambda: emitted.append(True))
            dialog.close()
            self.assertEqual(emitted, [True])
        finally:
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
