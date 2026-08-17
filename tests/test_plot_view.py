"""Tests for the QtCharts signal plot.

Runs headless against the offscreen platform. Frames and series are built
in-process; nothing here opens a CAN interface.
"""

from __future__ import annotations

import os
import random
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
    from cansniff.analysis.series import Series
    from cansniff.ui.plot_view import (
        HAVE_CHARTS, MAX_SERIES, MAX_UNIT_GROUPS, SignalPlot,
    )
    from cansniff.ui.theme import Theme


def _series(name, n=100, unit="", start=0.0, value=lambda i: float(i), skipped=0):
    times = [start + i * 0.01 for i in range(n)]
    return Series(name, times, [value(i) for i in range(n)], unit, skipped)


@unittest.skipUnless(HAVE_QT and HAVE_CHARTS, "PySide6 QtCharts not available")
class SignalPlotTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.plot = SignalPlot(Theme())
        self.plot.resize(700, 380)
        self.addCleanup(self.plot.deleteLater)

    def test_starts_empty_with_an_explanation(self):
        self.assertFalse(self.plot.view.isVisible())
        self.assertEqual(self.plot._series, [])

    def test_draws_a_single_series(self):
        warning = self.plot.set_series([_series("Rpm", unit="rpm")])
        self.assertEqual(warning, "")
        self.assertEqual(len(self.plot.chart.series()), 1)
        self.assertIn("Rpm", self.plot.title_label.text())

    def _visible_y(self):
        """Y axes actually carrying a scale, not the reusable pool."""
        return [a for a in self.plot._axes_y if a.isVisible()]

    def test_axis_titles_carry_the_unit(self):
        self.plot.set_series([_series("Speed", unit="km/h")])
        titles = [a.titleText() for a in self._visible_y()]
        self.assertIn("km/h", titles)
        self.assertEqual(self.plot._axis_x.titleText(), "time (s)")

    def test_unitless_series_still_gets_a_labelled_axis(self):
        self.plot.set_series([_series("Counter")])
        titles = [a.titleText() for a in self._visible_y()]
        self.assertIn("value", titles)

    def test_two_units_get_two_axes(self):
        self.plot.set_series([_series("Rpm", unit="rpm"),
                              _series("Temp", unit="degC")])
        self.assertEqual(len(self._visible_y()), 2)
        self.assertEqual(len(self.plot.chart.series()), 2)

    def test_same_unit_shares_one_axis(self):
        self.plot.set_series([_series("A", unit="degC"), _series("B", unit="degC")])
        self.assertEqual(len(self._visible_y()), 1,
                         "the second axis must be hidden, not left showing a "
                         "scale nothing is drawn against")
        self.assertEqual(len(self.plot.chart.series()), 2)

    def test_third_unit_is_refused_with_a_reason_not_silently_dropped(self):
        warning = self.plot.set_series([
            _series("A", unit="rpm"), _series("B", unit="degC"),
            _series("C", unit="volts"),
        ])
        self.assertIn("C", warning)
        self.assertIn("misrepresent", warning)
        self.assertLessEqual(len(self._visible_y()), MAX_UNIT_GROUPS)

    def test_too_many_series_is_capped_and_reported(self):
        many = [_series("s{}".format(i), n=10) for i in range(MAX_SERIES + 3)]
        warning = self.plot.set_series(many)
        self.assertIn("Showing the first", warning)
        self.assertEqual(len(self.plot.chart.series()), MAX_SERIES)

    def test_empty_series_shows_why_rather_than_a_blank_chart(self):
        warning = self.plot.set_series([Series("Ghost", [], [])])
        self.assertEqual(warning, "")
        self.assertFalse(self.plot.view.isVisible())
        self.assertIn("no samples", self.plot.empty.subtitle_label.text())

    def test_flat_series_still_has_a_visible_axis_range(self):
        self.plot.set_series([_series("Flat", value=lambda i: 5.0)])
        y = self.plot._axes_y[0]
        self.assertLess(y.min(), y.max())

    def test_sparse_series_is_drawn(self):
        sparse = Series("Sparse", [0.0, 5.0, 11.0], [1.0, 7.0, 3.0])
        self.plot.set_series([sparse])
        self.assertEqual(len(self.plot.chart.series()), 1)

    def test_large_series_is_decimated_for_display_only(self):
        random.seed(3)
        big = _series("Noise", n=60000, value=lambda i: float(random.getrandbits(8)))
        self.plot.set_series([big])
        drawn = self.plot.chart.series()[0].count()
        self.assertLess(drawn, 60000, "display data must be decimated")
        self.assertEqual(len(self.plot._series[0]), 60000,
                         "full resolution must be retained")

    def test_detail_reports_samples_drawn_and_skipped(self):
        self.plot.set_series([_series("S", n=50, skipped=7)])
        text = self.plot.detail_label.text()
        self.assertIn("50 samples", text)
        self.assertIn("7 frames had no value", text)

    def test_clear_removes_the_data_and_hides_the_scales(self):
        self.plot.set_series([_series("A", unit="x")])
        self.plot.clear()
        self.assertEqual(self.plot.chart.series(), [])
        self.assertEqual(self._visible_y(), [],
                         "no scale should be shown with nothing drawn")
        self.assertFalse(self.plot.view.isVisible())

    def test_replacing_series_does_not_accumulate_axes(self):
        """Regression: rebuilt axes left their old tick labels in the scene.

        Every redraw stacked another set of numbers on the previous one, so a
        Y axis read "1515e+09" where two labels overlapped. The axes are now
        created once and reused, so the count cannot grow.
        """
        for _ in range(5):
            self.plot.set_series([_series("A", unit="rpm"),
                                  _series("B", unit="degC")])
        self.assertEqual(len(self.plot.chart.axes()), 3)   # x + two y

    def test_axes_survive_a_clear_and_replot(self):
        self.plot.set_series([_series("A", unit="rpm")])
        first = self.plot._axis_x
        self.plot.clear()
        self.plot.set_series([_series("B", unit="rpm")])
        self.assertIs(self.plot._axis_x, first, "the axis must be reused")
        self.assertEqual(len(self.plot.chart.axes()), 3)

    def test_reset_zoom_restores_the_full_time_range(self):
        self.plot.set_series([_series("A", n=200)])
        axis = self.plot._axis_x
        axis.setRange(0.5, 0.6)
        self.plot.reset_zoom()
        self.assertAlmostEqual(axis.min(), 0.0, places=6)
        self.assertGreater(axis.max(), 1.0)

    def test_point_click_emits_its_timestamp(self):
        from PySide6.QtCore import QPointF
        picked = []
        self.plot.pointPicked.connect(picked.append)
        self.plot.set_series([_series("A", n=20)])
        self.plot._on_point_clicked(QPointF(0.07, 7.0))
        self.assertEqual(picked, [0.07])

    def test_repeated_resize_does_not_rebuild_the_series(self):
        """Redraw on resize must not re-derive points."""
        self.plot.set_series([_series("A", n=500)])
        line = self.plot.chart.series()[0]
        before = line.count()
        for width in (400, 900, 650, 1000):
            self.plot.resize(width, 320)
            self.app.processEvents()
        self.assertIs(self.plot.chart.series()[0], line)
        self.assertEqual(self.plot.chart.series()[0].count(), before)

    def test_restyle_does_not_lose_the_plot(self):
        self.plot.set_series([_series("A", unit="rpm")])
        self.plot.restyle()
        self.assertEqual(len(self.plot.chart.series()), 1)


if __name__ == "__main__":
    unittest.main()
