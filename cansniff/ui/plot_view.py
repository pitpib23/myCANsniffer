"""Time-series plot for decoded signals and raw byte blocks.

Built on QtCharts, which ships with PySide6-Addons — adding a second plotting
stack to a Qt application would have been the wrong trade.

Two things this deliberately does not do:

* It does not plot a signal against another signal's scale. Two series with
  different units get separate axes; a third unit is refused with a reason
  rather than silently drawn on someone else's scale, which is how a plot
  starts telling lies.
* It does not draw full-resolution data. The series decimates for display and
  keeps its own full resolution, so zooming re-derives from the real samples
  instead of from what happened to survive the last redraw.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

try:
    from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
    HAVE_CHARTS = True
except ImportError:  # pragma: no cover - depends on the PySide6 install
    HAVE_CHARTS = False

from ..analysis.series import DISPLAY_LIMIT, Series
from .theme import SPACE_MD, SPACE_SM, Theme
from .widgets import EmptyState

#: Distinct series the plot will draw at once. Beyond this the chart stops
#: being readable long before it stops being possible.
MAX_SERIES = 6

#: Unit groups that can share the chart: one per Y axis, left and right.
MAX_UNIT_GROUPS = 2

_PALETTE = ["accent", "success", "warning", "danger", "text_secondary", "accent_pressed"]

#: Ceiling on axis significant digits. Past this the labels are wider than the
#: numbers are informative, and a double cannot back them up anyway.
_MAX_AXIS_DIGITS = 15


def _label_format(low: float, high: float) -> str:
    """Axis number format with enough digits to tell the gridlines apart.

    A fixed "%.3g" is wrong whenever the range is small next to the magnitude:
    a signal wandering by a few thousand around 2.16e9 rendered as five
    identical "2.16e+09" labels, which says nothing about the values and
    actively hides that the axis is moving at all. The precision needed is the
    gap between the two exponents, plus enough digits to resolve the steps
    within the span.
    """
    span = abs(high - low)
    magnitude = max(abs(low), abs(high))
    if span <= 0 or magnitude <= 0:
        return "%.3g"
    try:
        digits = int(math.floor(math.log10(magnitude))) \
            - int(math.floor(math.log10(span))) + 3
    except ValueError:  # pragma: no cover - guarded by the checks above
        return "%.3g"
    return "%.{}g".format(max(3, min(_MAX_AXIS_DIGITS, digits)))


class SignalPlot(QWidget):
    """A real time-series chart with zoom, pan and a reset.

    Emits ``pointPicked`` with a timestamp when the operator clicks a sample,
    so the surrounding UI can jump to the frame that produced it — a plotted
    value must always lead back to the frame it came from.
    """

    pointPicked = Signal(float)

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._theme = theme
        self._series: List[Series] = []
        self._lines: List[object] = []
        self._axis_x = None
        self._axes_y: List[object] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(SPACE_SM)
        root.addLayout(self._build_toolbar())

        if not HAVE_CHARTS:  # pragma: no cover - depends on the install
            self.chart = None
            self.view = EmptyState(
                "Plotting unavailable",
                "QtCharts is not present in this PySide6 installation. "
                "Install PySide6-Addons to enable signal plots.",
                theme,
            )
            root.addWidget(self.view, 1)
            return

        self.chart = QChart()
        self.chart.setBackgroundVisible(False)
        self.chart.setMargins(self.chart.margins().__class__(4, 4, 4, 4))
        self.chart.legend().setVisible(True)
        self.chart.legend().setAlignment(Qt.AlignBottom)
        self.chart.setAnimationOptions(QChart.NoAnimation)  # redraws must be cheap

        # Axes are created once and reused for the life of the widget, never
        # rebuilt per plot. Removing an axis and adding a replacement left the
        # old one's tick labels painted in the scene, so every redraw stacked
        # another set of numbers on the previous one — the axis read
        # "1515e+09" where two labels overlapped, and long labels made it
        # worse. Reusing the objects means there is only ever one set.
        self._axis_x = QValueAxis()
        self._axis_x.setTitleText("time (s)")
        self._axis_x.setLabelFormat("%.2f")
        self.chart.addAxis(self._axis_x, Qt.AlignBottom)

        for alignment in (Qt.AlignLeft, Qt.AlignRight)[:MAX_UNIT_GROUPS]:
            axis_y = QValueAxis()
            axis_y.setLabelFormat("%.3g")
            self.chart.addAxis(axis_y, alignment)
            axis_y.setVisible(False)
            self._axes_y.append(axis_y)

        self.view = QChartView(self.chart)
        self.view.setRenderHint(QPainter.Antialiasing, True)
        self.view.setRubberBand(QChartView.RectangleRubberBand)
        self.view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.view.setMinimumHeight(220)
        root.addWidget(self.view, 1)

        self.empty = EmptyState(
            "No signal selected",
            "Choose a signal from the decoded message, or a payload block, "
            "to see how its value moves over time.",
            theme,
        )
        root.addWidget(self.empty, 1)
        self.view.setVisible(False)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(SPACE_SM)

        self.title_label = QLabel("")
        self.title_label.setFont(self._theme.ui_font(0.5, bold=True))
        row.addWidget(self.title_label)

        self.detail_label = QLabel("")
        self.detail_label.setObjectName("Muted")
        row.addWidget(self.detail_label)
        row.addStretch(1)

        self.reset_button = QPushButton("Reset zoom")
        self.reset_button.setObjectName("Ghost")
        self.reset_button.setCursor(Qt.PointingHandCursor)
        self.reset_button.setToolTip("Fit every sample back into view")
        self.reset_button.clicked.connect(self.reset_zoom)
        row.addWidget(self.reset_button)
        return row

    # ------------------------------------------------------------------
    # content
    # ------------------------------------------------------------------

    def clear(self) -> None:
        self._series = []
        self._lines = []
        if self.chart is None:
            return
        self.chart.removeAllSeries()
        # The axes stay; only their content goes. See __init__ for why they are
        # never torn down.
        for axis in self._axes_y:
            axis.setVisible(False)
        self.title_label.setText("")
        self.detail_label.setText("")
        self.view.setVisible(False)
        self.empty.setVisible(True)

    def set_series(self, series: Sequence[Series]) -> str:
        """Draw these series. Returns a warning string, empty when all is well.

        The return value is a report, not an exception: a plot that silently
        drops a signal the operator asked for is worse than one that says why.
        """
        if self.chart is None:  # pragma: no cover - depends on the install
            return "QtCharts is not available in this installation."

        wanted = [s for s in series if s is not None and not s.empty]
        if not wanted:
            self.clear()
            if series:
                self.empty.set_text(
                    "Nothing to plot",
                    "The selected signal produced no samples in this capture. "
                    "It may never have been observed, or every frame carrying "
                    "it failed to decode.",
                )
            return ""

        warning = ""
        if len(wanted) > MAX_SERIES:
            warning = "Showing the first {} of {} signals.".format(
                MAX_SERIES, len(wanted))
            wanted = wanted[:MAX_SERIES]

        # Group by unit so nothing is drawn against a scale that is not its own.
        groups: List[str] = []
        for item in wanted:
            if item.unit not in groups:
                groups.append(item.unit)
        if len(groups) > MAX_UNIT_GROUPS:
            kept = groups[:MAX_UNIT_GROUPS]
            dropped = [s.name for s in wanted if s.unit not in kept]
            wanted = [s for s in wanted if s.unit in kept]
            groups = kept
            extra = ("Left out {} — a chart can carry two unit scales, and "
                     "plotting a third against someone else's axis would "
                     "misrepresent it.".format(", ".join(dropped)))
            warning = (warning + " " + extra).strip()

        self._render(wanted, groups)
        return warning

    def _render(self, wanted: List[Series], groups: List[str]) -> None:
        self.chart.removeAllSeries()
        self._series = list(wanted)
        self._lines = []

        # X axis: capture time, shared by everything on the chart. Reused, not
        # rebuilt — see __init__.
        axis_x = self._axis_x
        axis_x.setRange(min(s.time_range[0] for s in wanted),
                        max(s.time_range[1] for s in wanted))

        for index, axis_y in enumerate(self._axes_y):
            if index >= len(groups):
                # No second unit on the chart: hide the axis rather than leave
                # it showing a scale nothing is drawn against.
                axis_y.setVisible(False)
                continue
            unit = groups[index]
            members = [s for s in wanted if s.unit == unit]
            low = min(s.value_range[0] for s in members)
            high = max(s.value_range[1] for s in members)
            axis_y.setTitleText(unit or "value")
            axis_y.setLabelFormat(_label_format(low, high))
            axis_y.setRange(low, high)
            axis_y.setVisible(True)

        # Sized to the plot, not to a fixed constant: redraw cost is linear in
        # drawn points and anything finer than one bucket per pixel column is
        # invisible. Deliberately computed once here rather than on every
        # resize — re-deriving points while the user drags an edge is the
        # behaviour this is avoiding.
        limit = Series.limit_for_width(max(self.view.width(), 320))

        for position, item in enumerate(wanted):
            display = item.decimated(limit)
            line = QLineSeries()
            line.setName(item.label)
            pen = QPen(self._colour(position))
            pen.setWidthF(1.6)
            line.setPen(pen)
            # replace() with a prepared list is the fast path; appending point
            # by point re-emits a change signal for every sample.
            line.replace([QPointF(t, v)
                          for t, v in zip(display.times, display.values)])
            self.chart.addSeries(line)
            line.attachAxis(axis_x)
            line.attachAxis(self._axes_y[groups.index(item.unit)])
            line.clicked.connect(self._on_point_clicked)
            self._lines.append(line)

        self.title_label.setText(" · ".join(s.name for s in wanted))
        self.detail_label.setText(self._describe(wanted))
        self.chart.legend().setVisible(len(wanted) > 1)
        self.empty.setVisible(False)
        self.view.setVisible(True)
        self.reset_zoom()

    def _describe(self, wanted: List[Series]) -> str:
        total = sum(len(s) for s in wanted)
        skipped = sum(s.skipped for s in wanted)
        shown = sum(line.count() for line in self._lines)
        parts = ["{:,} samples".format(total)]
        if shown < total:
            parts.append("{:,} drawn".format(shown))
        if skipped:
            parts.append("{:,} frames had no value".format(skipped))
        return "  ·  ".join(parts)

    def _colour(self, index: int) -> QColor:
        return self._theme.color(_PALETTE[index % len(_PALETTE)])

    def _on_point_clicked(self, point: QPointF) -> None:
        self.pointPicked.emit(float(point.x()))

    # ------------------------------------------------------------------
    # view control
    # ------------------------------------------------------------------

    def reset_zoom(self) -> None:
        if self.chart is None:
            return
        self.chart.zoomReset()
        if self._axis_x is not None and self._series:
            self._axis_x.setRange(
                min(s.time_range[0] for s in self._series),
                max(s.time_range[1] for s in self._series),
            )

    def restyle(self) -> None:
        self.title_label.setFont(self._theme.ui_font(0.5, bold=True))
        for index, line in enumerate(self._lines):
            pen = QPen(self._colour(index))
            pen.setWidthF(1.6)
            line.setPen(pen)
