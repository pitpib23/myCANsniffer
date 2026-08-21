"""Compare workspace rendering and deterministic worker lifecycle."""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QCoreApplication, QEvent, QThread
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.analysis.compare import ComparisonCancelled, CorrelationResult
    from cansniff.config import Config
    from cansniff.ui.compare_view import CompareView
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.theme import Theme
    from tests.test_compare_candidates import run
    from tests.test_compare_engine import frame


def changed_capture():
    frames = [frame(float(i), 0x100, [0, i]) for i in range(10)]
    frames += [frame(20.0 + i, 0x100, [1, i + 20]) for i in range(10)]
    return sorted(frames, key=lambda item: item.timestamp)


class _SlowComparisonCache:
    def lookup(self, _store, _comparison_input, _profile):
        return None

    def build(self, _store, _comparison_input, _profile, cancelled):
        while not cancelled():
            time.sleep(0.001)
        raise ComparisonCancelled()

    def clear(self):
        pass


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class CompareViewTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = CompareView(Theme())
        self.addCleanup(self.view.deleteLater)

    def test_empty_view_has_no_invented_results(self):
        self.assertEqual(self.view.ranking_table.rowCount(), 0)
        self.view.set_capture_context(0, None, None)
        self.view._mark(self.view.baseline_start)
        self.assertIn("No retained timestamp", self.view.status_label.text())

    def test_capture_context_seeds_two_editable_intervals(self):
        self.view.set_capture_context(7, 10.0, 30.0)
        request = self.view.comparison_input()
        self.assertEqual((request.baseline.start_timestamp,
                          request.baseline.end_timestamp), (10.0, 20.0))
        self.assertEqual((request.event.start_timestamp,
                          request.event.end_timestamp), (20.0, 30.0))
        self.view.baseline_label.setText("User supplied idle label")
        self.assertEqual(self.view.comparison_input().baseline.label,
                         "User supplied idle label")

    def test_valid_snapshot_populates_ranking_bytes_bits_and_candidates(self):
        snapshot, _candidates = run(
            [[0, value] for value in range(20)],
            [[0x20, value + 20] for value in range(20)])
        self.view.set_snapshot(snapshot)
        self.assertEqual(self.view.ranking_table.rowCount(), 1)
        self.assertIn("byte", self.view.ranking_table.item(0, 7).text())
        self.assertGreater(self.view.byte_table.rowCount(), 0)
        self.assertGreater(self.view.bit_table.rowCount(), 0)
        self.assertGreater(self.view.candidate_table.rowCount(), 0)
        self.assertIn("hardware/driver", self.view.status_label.text())

    def test_correlation_result_states_alignment_and_noncausation(self):
        result = CorrelationResult(
            "field A", "field B", 0.95, 42, 0.05,
            ("nearest-timestamp alignment", "correlation does not establish causation"),
            1, 2)
        self.view.show_correlation(result)
        text = self.view.correlation_detail.toPlainText()
        self.assertIn("0.950000", text)
        self.assertIn("42", text)
        self.assertIn("does not establish causation", text)


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class MainWindowCompareTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        config = Config.defaults(os.path.join(
            os.environ.get("TEMP", "."), "cansniff_compare_ui.json"))
        self.window = MainWindow(config, Theme())
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.window._compare_closing = True
        self.window._cancel_compare_workers(wait=True)
        self.window._protocol_closing = True
        self.window._cancel_protocol_survey(wait=True)
        self.window.deleteLater()

    def _wait_comparison(self, timeout=3.0):
        end = time.time() + timeout
        while self.window._comparison_thread is not None and time.time() < end:
            self.app.processEvents()
            time.sleep(0.003)
        self.app.processEvents()
        self.assertIsNone(self.window._comparison_thread)

    def _wait_correlation(self, timeout=3.0):
        end = time.time() + timeout
        while self.window._correlation_thread is not None and time.time() < end:
            self.app.processEvents()
            time.sleep(0.003)
        self.app.processEvents()
        self.assertIsNone(self.window._correlation_thread)

    def _configure_intervals(self):
        view = self.window.compare_view
        view.baseline_start.setValue(0)
        view.baseline_end.setValue(9)
        view.event_start.setValue(20)
        view.event_end.setValue(29)

    def test_compare_nav_runs_analysis_off_thread_and_populates_detail(self):
        self.window._on_frames(changed_capture())
        self.window._activate_compare()
        self.assertEqual(self.window.top_stack.currentIndex(),
                         self.window._STACK_COMPARE)
        self._configure_intervals()
        self.window.compare_view.compare_button.click()
        self.assertIsNotNone(self.window._comparison_thread)
        self._wait_comparison()
        self.assertIsNotNone(self.window._comparison_snapshot)
        self.assertEqual(self.window.compare_view.ranking_table.rowCount(), 1)

    def test_compare_navigation_is_non_toggleable(self):
        button = self.window.nav.group.button(self.window._NAV_COMPARE)
        self.assertIsNotNone(button)
        self.assertEqual(button.accessibleName(), "Compare")

    def test_incomplete_evicted_interval_is_visible_error(self):
        self.window.frame_store.set_max_frames(100)
        frames = [frame(float(i), 0x100, [i & 0xFF]) for i in range(150)]
        self.window._on_frames(frames)
        view = self.window.compare_view
        view.baseline_start.setValue(0)
        view.baseline_end.setValue(20)
        view.event_start.setValue(120)
        view.event_end.setValue(140)
        view.compare_button.click()
        self._wait_comparison()
        self.assertIn("evicted", view.status_label.text())

    def test_selected_correlation_runs_in_its_own_worker(self):
        self.window._on_frames(changed_capture())
        self._configure_intervals()
        self.window.compare_view.compare_button.click()
        self._wait_comparison()
        view = self.window.compare_view
        self.assertGreaterEqual(view.correlation_left.count(), 2)
        view.correlate_button.click()
        self.assertIsNotNone(self.window._correlation_thread)
        self._wait_correlation()
        self.assertIn("Pearson r:", view.correlation_detail.toPlainText())

    def test_clear_and_close_cancel_without_stale_rows(self):
        self.window._comparison_cache = _SlowComparisonCache()
        self.window._on_frames(changed_capture())
        self._configure_intervals()
        self.window.compare_view.compare_button.click()
        self.app.processEvents()
        self.assertIsNotNone(self.window._comparison_thread)
        self.window.clear_views()
        self._wait_comparison()
        self.assertEqual(self.window.compare_view.ranking_table.rowCount(), 0)
        self.assertIsNone(self.window._comparison_snapshot)
        self.window._comparison_cache = _SlowComparisonCache()
        self.window._start_comparison(
            self.window.compare_view.comparison_input())
        self.app.processEvents()
        self.window._compare_closing = True
        self.window._cancel_compare_workers(wait=True)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertIsNone(self.window._comparison_thread)
        self.assertEqual(len(self.window.findChildren(QThread)), 0)

    def test_repeated_comparisons_leave_no_qthread_children(self):
        self.window._on_frames(changed_capture())
        self._configure_intervals()
        for index in range(4):
            self.window.compare_view.baseline_label.setText(
                "Baseline {}".format(index))
            self.window.compare_view.compare_button.click()
            self._wait_comparison()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertIsNone(self.window._comparison_worker)
        self.assertIsNone(self.window._comparison_thread)
        self.assertEqual(len(self.window.findChildren(QThread)), 0)


if __name__ == "__main__":
    unittest.main()
