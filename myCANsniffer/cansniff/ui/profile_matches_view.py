"""Focused, read-only profile suggestions with explicit activation actions."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..analysis.matching import ProfileMatchLevel, ProfileMatchSnapshot
from .theme import SPACE_MD, SPACE_SM, Theme


def _item(value):
    result = QTableWidgetItem(str(value))
    result.setFlags(result.flags() & ~Qt.ItemIsEditable)
    return result


class ProfileMatchesView(QWidget):
    matchRequested = Signal()
    useProfileRequested = Signal(str)
    associateRequested = Signal(str, str, int, str)

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.snapshot: Optional[ProfileMatchSnapshot] = None
        self._visible_candidates = []
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_MD)
        root.setSpacing(SPACE_SM)

        title = QLabel("PROFILE MATCHES")
        title.setObjectName("PanelTitle")
        root.addWidget(title)
        self.status_label = QLabel(
            "Run structural matching to compare observed traffic with local profiles. "
            "Suggestions are never activated automatically.")
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        root.addWidget(self.status_label)

        filters = QGridLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter profile name or provenance")
        self.search_edit.textChanged.connect(self._apply_filter)
        filters.addWidget(self.search_edit, 0, 0, 1, 3)
        self.source_filter = QComboBox()
        self.source_filter.addItems(("All sources", "DBC", "MANUAL", "EDS/DCF"))
        self.source_filter.currentIndexChanged.connect(self._apply_filter)
        filters.addWidget(self.source_filter, 1, 0)
        self.level_filter = QComboBox()
        self.level_filter.addItems(("All levels", "Weak or better",
                                    "Possible or better", "Strong only"))
        self.level_filter.currentIndexChanged.connect(self._apply_filter)
        filters.addWidget(self.level_filter, 1, 1)
        self.run_button = QPushButton("Run matching")
        self.run_button.clicked.connect(self.matchRequested.emit)
        filters.addWidget(self.run_button, 1, 2)
        filters.setColumnStretch(0, 2)
        filters.setColumnStretch(1, 1)
        root.addLayout(filters)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels((
            "Profile", "Suggestion", "Observed IDs", "Defined IDs",
            "Weighted frames", "Conflicts", "Sources"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column in range(6):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.Interactive)
        self.table.currentCellChanged.connect(self._selection_changed)
        root.addWidget(self.table, 2)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Select a candidate to inspect its structural facts.")
        root.addWidget(self.details, 3)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.inspect_button = QPushButton("Inspect")
        self.inspect_button.clicked.connect(self._show_selected_details)
        actions.addWidget(self.inspect_button)
        self.associate_button = QPushButton("Associate definition")
        self.associate_button.clicked.connect(self._associate_selected)
        actions.addWidget(self.associate_button)
        self.use_button = QPushButton("Use this profile")
        self.use_button.clicked.connect(self._use_selected)
        actions.addWidget(self.use_button)
        root.addLayout(actions)
        self._update_actions(None)

    def clear(self, reason: str = "No profile match has been generated.") -> None:
        self.snapshot = None
        self._visible_candidates = []
        self.table.setRowCount(0)
        self.details.clear()
        self.status_label.setText(reason)
        self.run_button.setEnabled(True)
        self._update_actions(None)

    def show_loading(self, profile_count: int, observed_count: int) -> None:
        self.run_button.setEnabled(False)
        self.status_label.setText(
            "Matching {:,} observed message keys against {:,} local profiles "
            "off the UI thread…".format(observed_count, profile_count))

    def show_error(self, message: str) -> None:
        self.run_button.setEnabled(True)
        self.status_label.setText("Profile matching failed: {}".format(message))

    def set_snapshot(self, snapshot: ProfileMatchSnapshot) -> None:
        self.snapshot = snapshot
        self.run_button.setEnabled(True)
        useful = sum(item.level is not ProfileMatchLevel.NONE
                     for item in snapshot.candidates)
        if not snapshot.candidates:
            status = next((item for item in snapshot.caveats
                           if ("No matching possible" in item
                               or "No local profiles" in item)),
                          "No useful profile match found.")
        elif not useful:
            status = ("No useful profile match found. Available profiles had no "
                      "structural overlap or conflicted materially.")
        else:
            status = (
                "{} useful suggestion(s) from {} local profile(s). Structural "
                "similarity is not device identity; activation remains explicit."
                .format(useful, len(snapshot.candidates)))
        self.status_label.setText(status)
        self._apply_filter()

    def _apply_filter(self, *_args) -> None:
        candidates = list(self.snapshot.candidates) if self.snapshot is not None else []
        query = self.search_edit.text().strip().casefold()
        source = self.source_filter.currentText()
        minimum = {0: 0, 1: ProfileMatchLevel.WEAK.rank,
                   2: ProfileMatchLevel.POSSIBLE.rank,
                   3: ProfileMatchLevel.STRONG.rank}[self.level_filter.currentIndex()]
        visible = []
        for candidate in candidates:
            provenance_text = " ".join(
                "{} {} {}".format(item.kind, item.display_name, item.state)
                for item in candidate.provenance)
            if query and query not in (candidate.display_name + " " + provenance_text).casefold():
                continue
            kinds = {item.kind.upper() for item in candidate.provenance}
            if source == "DBC" and "DBC" not in kinds:
                continue
            if source == "MANUAL" and "MANUAL" not in kinds:
                continue
            if source == "EDS/DCF" and not kinds.intersection(("EDS", "DCF")):
                continue
            if candidate.level.rank < minimum:
                continue
            visible.append(candidate)
        self._visible_candidates = visible
        self.table.setRowCount(len(visible))
        for row, candidate in enumerate(visible):
            coverage = candidate.coverage
            candidate_kinds = {item.kind.upper()
                               for item in candidate.provenance}
            values = (
                candidate.display_name, candidate.level.value,
                "{}/{} ({:.1%})".format(
                    coverage.matched_observed_keys, coverage.observed_keys,
                    coverage.observed_key_coverage),
                "{}/{} ({:.1%})".format(
                    coverage.matched_defined_keys, coverage.defined_keys,
                    coverage.definition_key_coverage),
                "{:.1%} raw / {:.1%} balanced".format(
                    coverage.weighted_frame_coverage,
                    coverage.balanced_frame_coverage),
                len(candidate.conflicts),
                ", ".join(sorted(candidate_kinds)),
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, _item(value))
            self.table.item(row, 0).setData(Qt.UserRole, candidate.profile_id)
        if visible:
            self.table.selectRow(0)
            self._selection_changed(0, 0, -1, -1)
        else:
            self.details.clear()
            self._update_actions(None)

    def _selected_candidate(self):
        row = self.table.currentRow()
        return self._visible_candidates[row] if 0 <= row < len(self._visible_candidates) else None

    def _selection_changed(self, *_args) -> None:
        candidate = self._selected_candidate()
        self._update_actions(candidate)
        self._show_details(candidate)

    def _update_actions(self, candidate) -> None:
        available = candidate is not None and candidate.level is not ProfileMatchLevel.NONE
        self.inspect_button.setEnabled(candidate is not None)
        self.use_button.setEnabled(available)
        self.associate_button.setEnabled(
            available and bool(candidate.association_suggestions))

    def _show_selected_details(self) -> None:
        self._show_details(self._selected_candidate())
        self.details.setFocus()

    def _show_details(self, candidate) -> None:
        if candidate is None:
            self.details.clear()
            return
        coverage = candidate.coverage
        lines = [
            "SUMMARY", candidate.display_name + " — " + candidate.level.value,
            "Structural rank is used only for ordering; it is not a probability.", "",
            "PROVENANCE",
        ]
        for item in candidate.provenance:
            digest = " / SHA-256 " + item.content_hash if item.content_hash else ""
            lines.append("• {} / {} / {}{} / {}".format(
                item.kind, item.display_name, item.state, digest, item.details or "no caveat"))
        lines.extend(("", "COVERAGE",
                      "• Observed IDs: {}/{} = {:.1%}".format(
                          coverage.matched_observed_keys, coverage.observed_keys,
                          coverage.observed_key_coverage),
                      "• Defined IDs: {}/{} = {:.1%}".format(
                          coverage.matched_defined_keys, coverage.defined_keys,
                          coverage.definition_key_coverage),
                      "• Weighted frames: {}/{} = {:.1%}".format(
                          coverage.matched_frames, coverage.observed_frames,
                          coverage.weighted_frame_coverage),
                      "• Median-capped frame coverage: {:.1%}".format(
                          coverage.balanced_frame_coverage),
                      "", "AGREEMENTS"))
        lines.extend("• " + value for value in candidate.reasons)
        lines.extend(("", "MATCHED MESSAGES"))
        lines.extend("• " + value for value in candidate.matched_messages[:200])
        if not candidate.matched_messages:
            lines.append("• none")
        lines.extend(("", "UNMATCHED OBSERVED"))
        lines.extend("• " + value for value in candidate.unmatched_observed[:200])
        if not candidate.unmatched_observed:
            lines.append("• none")
        lines.extend(("", "UNSEEN DEFINED"))
        lines.extend("• " + value for value in candidate.unmatched_defined[:200])
        if not candidate.unmatched_defined:
            lines.append("• none")
        lines.extend(("", "CONFLICTS"))
        lines.extend("• {}: {}".format(value.kind.value, value.explanation)
                     for value in candidate.conflicts)
        if not candidate.conflicts:
            lines.append("• none observed")
        lines.extend(("", "CANOPEN NODE / MAPPING AGREEMENT"))
        for suggestion in candidate.association_suggestions:
            lines.append("• {} → Node {} / {} of {} mapped PDOs observed".format(
                suggestion.definition_name, suggestion.node_id,
                suggestion.matched_pdos, suggestion.defined_pdos))
            lines.extend("  conflict: " + item for item in suggestion.conflicts)
        if not candidate.association_suggestions:
            lines.append("• no unaccepted association suggestion")
        lines.extend(("", "ASSUMPTIONS AND INTEGRITY / HORIZON CAVEATS"))
        lines.extend("• " + value for value in candidate.assumptions)
        if self.snapshot is not None:
            lines.extend("• " + value for value in self.snapshot.caveats)
        self.details.setPlainText("\n".join(lines))

    def _use_selected(self) -> None:
        candidate = self._selected_candidate()
        if candidate is not None and candidate.level is not ProfileMatchLevel.NONE:
            self.useProfileRequested.emit(candidate.profile_id)

    def _associate_selected(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None or not candidate.association_suggestions:
            return
        suggestion = candidate.association_suggestions[0]
        self.associateRequested.emit(
            candidate.profile_id, suggestion.definition_hash,
            suggestion.node_id, suggestion.channel)


__all__ = ["ProfileMatchesView"]
