"""Focused baseline/event investigation UI; presentation and user intent only."""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox,
    QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSizePolicy,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..analysis.compare import (
    CandidateKind, ComparisonInput, ComparisonSnapshot, ComparisonWindow,
    CorrelationResult,
)
from .theme import SPACE_MD, SPACE_SM, Theme


MAX_DETAIL_ROWS = 2000


def _item(value):
    item = QTableWidgetItem(str(value))
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


def _table(headers: Sequence[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    header = table.horizontalHeader()
    header.setStretchLastSection(True)
    for column in range(max(0, len(headers) - 1)):
        header.setSectionResizeMode(column, QHeaderView.Interactive)
    return table


class CompareView(QWidget):
    compareRequested = Signal(object)
    correlationRequested = Signal(object, object, float)
    intervalsChanged = Signal()

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.snapshot: Optional[ComparisonSnapshot] = None
        self._revision = 0
        self._current_timestamp: Optional[float] = None
        self._seeded = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_MD)
        layout.setSpacing(SPACE_SM)
        title = QLabel("COMPARE / STRUCTURAL INVESTIGATION")
        title.setObjectName("PanelTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Compare two retained intervals. Results describe structural change; "
            "they never assign signal meaning.")
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        controls = QGroupBox("Baseline and event intervals")
        grid = QGridLayout(controls)
        self.baseline_label = QLineEdit("Baseline")
        self.event_label = QLineEdit("Event")
        self.baseline_start = self._time_spin()
        self.baseline_end = self._time_spin()
        self.event_start = self._time_spin()
        self.event_end = self._time_spin()
        for widget in (self.baseline_label, self.event_label):
            widget.textChanged.connect(lambda *_args: self.intervalsChanged.emit())
        for widget in (self.baseline_start, self.baseline_end,
                       self.event_start, self.event_end):
            widget.valueChanged.connect(lambda *_args: self.intervalsChanged.emit())
        grid.addWidget(QLabel("Baseline"), 0, 0)
        grid.addWidget(self.baseline_label, 0, 1, 1, 4)
        grid.addWidget(QLabel("Start (s)"), 1, 0)
        grid.addWidget(self.baseline_start, 1, 1)
        grid.addWidget(self._mark_button("Mark current", self.baseline_start), 1, 2)
        grid.addWidget(QLabel("End (s)"), 1, 3)
        grid.addWidget(self.baseline_end, 1, 4)
        grid.addWidget(self._mark_button("Mark current", self.baseline_end), 1, 5)
        grid.addWidget(QLabel("Event"), 2, 0)
        grid.addWidget(self.event_label, 2, 1, 1, 4)
        grid.addWidget(QLabel("Start (s)"), 3, 0)
        grid.addWidget(self.event_start, 3, 1)
        grid.addWidget(self._mark_button("Mark current", self.event_start), 3, 2)
        grid.addWidget(QLabel("End (s)"), 3, 3)
        grid.addWidget(self.event_end, 3, 4)
        grid.addWidget(self._mark_button("Mark current", self.event_end), 3, 5)
        self.compare_button = QPushButton("Compare")
        self.compare_button.setObjectName("PrimaryButton")
        self.compare_button.clicked.connect(self._request_compare)
        grid.addWidget(self.compare_button, 0, 5)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(4, 1)
        layout.addWidget(controls)

        self.status_label = QLabel("Select two retained intervals, then Compare.")
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.status_label)

        self.ranking_table = _table((
            "Rank", "Message", "Evidence", "Score", "Presence", "Baseline rate",
            "Event rate", "Why",
        ))
        self.ranking_table.setMinimumHeight(190)
        self.ranking_table.currentCellChanged.connect(self._message_selected)
        layout.addWidget(self.ranking_table, 2)

        self.tabs = QTabWidget()
        self.byte_table = _table((
            "Byte", "Baseline samples/missing", "Event samples/missing",
            "Baseline range", "Event range", "Distribution difference",
            "Entropy B/E", "Change frequency B/E",
        ))
        self.tabs.addTab(self.byte_table, "Changes")
        self.bit_table = _table((
            "Byte", "Bit", "Baseline 1", "Event 1", "State difference",
            "Transitions B/E", "Samples B/E",
        ))
        self.tabs.addTab(self.bit_table, "Bits")
        self.candidate_table = _table((
            "Pattern", "Range", "Evidence", "Samples", "Missing", "Why",
        ))
        self.tabs.addTab(self.candidate_table, "Candidates")
        self.tabs.addTab(self._build_correlation_page(), "Correlation")
        layout.addWidget(self.tabs, 3)

    @staticmethod
    def _time_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(6)
        spin.setRange(-1e12, 1e12)
        spin.setSingleStep(0.1)
        return spin

    def _mark_button(self, text: str, target: QDoubleSpinBox) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(lambda: self._mark(target))
        return button

    def _mark(self, target: QDoubleSpinBox) -> None:
        if self._current_timestamp is None:
            self.status_label.setText("No retained timestamp is available to mark.")
            return
        target.setValue(self._current_timestamp)

    def _build_correlation_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, SPACE_SM, 0, 0)
        note = QLabel(
            "Pearson correlation uses nearest timestamps in the Event interval. "
            "It measures linear association and never establishes causation.")
        note.setWordWrap(True)
        note.setObjectName("Muted")
        layout.addWidget(note)
        row = QGridLayout()
        self.correlation_left = QComboBox()
        self.correlation_right = QComboBox()
        self.correlation_tolerance = QDoubleSpinBox()
        self.correlation_tolerance.setDecimals(6)
        self.correlation_tolerance.setRange(0.0, 60.0)
        self.correlation_tolerance.setValue(0.05)
        self.correlation_tolerance.setSuffix(" s")
        self.correlate_button = QPushButton("Correlate selected fields")
        self.correlate_button.clicked.connect(self._request_correlation)
        row.addWidget(QLabel("A"), 0, 0)
        row.addWidget(self.correlation_left, 0, 1)
        row.addWidget(QLabel("B"), 1, 0)
        row.addWidget(self.correlation_right, 1, 1)
        row.addWidget(QLabel("Tolerance"), 2, 0)
        row.addWidget(self.correlation_tolerance, 2, 1)
        row.addWidget(self.correlate_button, 2, 2)
        row.setColumnStretch(1, 1)
        layout.addLayout(row)
        self.correlation_detail = QPlainTextEdit()
        self.correlation_detail.setReadOnly(True)
        self.correlation_detail.setPlaceholderText(
            "Select two numeric candidates and request a timestamp-aligned correlation.")
        layout.addWidget(self.correlation_detail, 1)
        return page

    def set_capture_context(self, revision: int, first: Optional[float],
                            last: Optional[float]) -> None:
        self._revision = revision
        self._current_timestamp = last
        if self._seeded or first is None or last is None:
            return
        midpoint = first + (last - first) / 2.0
        self.baseline_start.setValue(first)
        self.baseline_end.setValue(midpoint)
        self.event_start.setValue(midpoint)
        self.event_end.setValue(last)
        self._seeded = True

    def comparison_input(self) -> ComparisonInput:
        return ComparisonInput(
            ComparisonWindow(
                self.baseline_label.text().strip() or "Baseline",
                self.baseline_start.value(), self.baseline_end.value(), self._revision),
            ComparisonWindow(
                self.event_label.text().strip() or "Event",
                self.event_start.value(), self.event_end.value(), self._revision),
        )

    def apply_project_intervals(self, comparison) -> None:
        if comparison is None:
            return
        widgets = (self.baseline_label, self.baseline_start, self.baseline_end,
                   self.event_label, self.event_start, self.event_end)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.baseline_label.setText(comparison.baseline_label)
            self.baseline_start.setValue(comparison.baseline_start)
            self.baseline_end.setValue(comparison.baseline_end)
            self.event_label.setText(comparison.event_label)
            self.event_start.setValue(comparison.event_start)
            self.event_end.setValue(comparison.event_end)
            self._seeded = True
        finally:
            for widget in widgets:
                widget.blockSignals(False)

    def _request_compare(self) -> None:
        self.compareRequested.emit(self.comparison_input())

    def _request_correlation(self) -> None:
        left = self.correlation_left.currentData()
        right = self.correlation_right.currentData()
        if left is None or right is None:
            self.correlation_detail.setPlainText("Two numeric candidates are required.")
            return
        self.correlationRequested.emit(
            left, right, self.correlation_tolerance.value())

    def show_loading(self, text: str = "Comparing selected retained intervals off the UI thread...") -> None:
        self.status_label.setText(text)
        self.compare_button.setEnabled(False)

    def show_error(self, message: str) -> None:
        self.status_label.setText("Comparison unavailable: " + message)
        self.compare_button.setEnabled(True)

    def clear(self) -> None:
        self.snapshot = None
        self._seeded = False
        self.status_label.setText("No retained comparison is available.")
        self.compare_button.setEnabled(True)
        self.correlate_button.setEnabled(True)
        for table in (self.ranking_table, self.byte_table, self.bit_table,
                      self.candidate_table):
            table.setRowCount(0)
        self.correlation_left.clear()
        self.correlation_right.clear()
        self.correlation_detail.clear()

    def set_snapshot(self, snapshot: ComparisonSnapshot) -> None:
        self.snapshot = snapshot
        self.compare_button.setEnabled(True)
        self.status_label.setText(
            "Revision {} | {}: {:,} frames | {}: {:,} frames | {}".format(
                snapshot.generated_from_revision, snapshot.baseline.window.label,
                snapshot.baseline.frame_count, snapshot.event.window.label,
                snapshot.event.frame_count,
                "; ".join(snapshot.caveats) if snapshot.caveats else
                "no comparison caveats"))
        self._set_ranking(snapshot)
        self._set_correlation_choices(snapshot)

    def _set_ranking(self, snapshot: ComparisonSnapshot) -> None:
        rows = snapshot.messages[:MAX_DETAIL_ROWS]
        self.ranking_table.setRowCount(len(rows))
        for row, message in enumerate(rows):
            values = (
                row + 1, message.key, message.evidence_level.value,
                "{:.1f}".format(message.ranking_score), message.presence_change.value,
                "{:.3f} Hz".format(message.baseline_rate_hz),
                "{:.3f} Hz".format(message.event_rate_hz),
                "; ".join(message.reasons),
            )
            for column, value in enumerate(values):
                self.ranking_table.setItem(row, column, _item(value))
        if rows:
            self.ranking_table.selectRow(0)
            self._message_selected(0, 0, -1, -1)

    def _message_selected(self, row, _column, _old_row, _old_column) -> None:
        if self.snapshot is None or row < 0 or row >= len(self.snapshot.messages):
            return
        message = self.snapshot.messages[row]
        self._set_bytes(message)
        self._set_bits(message)
        self._set_candidates(message.key)

    def _set_bytes(self, message) -> None:
        rows = message.bytes[:MAX_DETAIL_ROWS]
        self.byte_table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            baseline_range = ("-" if item.baseline_minimum is None else
                              "{}..{}".format(item.baseline_minimum, item.baseline_maximum))
            event_range = ("-" if item.event_minimum is None else
                           "{}..{}".format(item.event_minimum, item.event_maximum))
            values = (
                item.index,
                "{} / {}".format(item.baseline_samples, item.baseline_missing),
                "{} / {}".format(item.event_samples, item.event_missing),
                baseline_range, event_range,
                "{:.1%}".format(item.distribution_distance),
                "{:.2f} / {:.2f}".format(item.baseline_entropy, item.event_entropy),
                "{:.1%} / {:.1%}".format(
                    item.baseline_change_fraction, item.event_change_fraction),
            )
            for column, value in enumerate(values):
                self.byte_table.setItem(row, column, _item(value))

    def _set_bits(self, message) -> None:
        rows = sorted(message.bits, key=lambda item: (
            -item.state_difference, item.byte_index, item.bit_index))[:MAX_DETAIL_ROWS]
        self.bit_table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            values = (
                item.byte_index, item.bit_index,
                "{:.1%}".format(item.baseline_one_fraction),
                "{:.1%}".format(item.event_one_fraction),
                "{:.1%}".format(item.state_difference),
                "{} / {}".format(item.baseline_transitions, item.event_transitions),
                "{} / {}".format(item.baseline_samples, item.event_samples),
            )
            for column, value in enumerate(values):
                self.bit_table.setItem(row, column, _item(value))

    def _set_candidates(self, key: str) -> None:
        assert self.snapshot is not None
        rows = self.snapshot.candidates_for(key)[:MAX_DETAIL_ROWS]
        self.candidate_table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            end = item.byte_start + item.byte_length - 1
            span = str(item.byte_start) if end == item.byte_start else "{}-{}".format(
                item.byte_start, end)
            pattern = (item.interpretation if item.kind is CandidateKind.NUMERIC
                       else item.kind.value)
            values = (pattern, span, item.evidence_level.value,
                      item.sample_count, item.missing_samples,
                      "; ".join(item.reasons))
            for column, value in enumerate(values):
                self.candidate_table.setItem(row, column, _item(value))

    def _set_correlation_choices(self, snapshot: ComparisonSnapshot) -> None:
        self.correlation_left.clear()
        self.correlation_right.clear()
        numeric = [item for item in snapshot.candidates
                   if item.kind is CandidateKind.NUMERIC and item.decoder_key]
        for candidate in numeric:
            self.correlation_left.addItem(candidate.field_label, candidate)
            self.correlation_right.addItem(candidate.field_label, candidate)
        if len(numeric) > 1:
            self.correlation_right.setCurrentIndex(1)

    def show_correlation_loading(self) -> None:
        self.correlation_detail.setPlainText(
            "Correlating selected numeric series off the UI thread...")
        self.correlate_button.setEnabled(False)

    def show_correlation(self, result: CorrelationResult) -> None:
        self.correlate_button.setEnabled(True)
        coefficient = ("undefined" if result.coefficient is None else
                       "{:.6f}".format(result.coefficient))
        lines = [
            "{}\nvs\n{}".format(result.left_label, result.right_label), "",
            "Pearson r: {}".format(coefficient),
            "Paired observations: {}".format(result.paired_samples),
            "Nearest-timestamp tolerance: {:.6f}s".format(result.tolerance_seconds),
            "Missing/invalid A/B: {} / {}".format(
                result.left_missing, result.right_missing), "", "Reasons:",
        ]
        lines.extend("- " + reason for reason in result.reasons)
        self.correlation_detail.setPlainText("\n".join(lines))

    def show_correlation_error(self, message: str) -> None:
        self.correlate_button.setEnabled(True)
        self.correlation_detail.setPlainText("Correlation unavailable: " + message)


__all__ = ["CompareView", "MAX_DETAIL_ROWS"]
