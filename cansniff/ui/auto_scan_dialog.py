"""Auto Scan popup: checkbox-selected candidates, a numerical score for
every one, and an explicit operator choice of which bitrate to listen on.

Purely a view: it never touches ``SocketCanLink``/``SocketCanSessionController``,
never runs on a worker thread, and never decides anything about the physical
link or which bitrate is "correct" -- it only renders the ``ScanProgress``/
``ScanResult`` events and the checkbox/duration selections the operator
makes, all of which ``cansniff/ui/main_window.py`` turns into actions
against ``BitrateScanWorker``/``cansniff.discovery.scan.
scan_bitrate_candidates`` (see ``discovery_worker.py``). ``on_progress``/
``on_result``/``on_error`` are meant to be connected directly to that
worker's Qt signals -- signal/slot delivery across threads is always queued
onto the GUI thread, so this dialog is only ever touched from there.

Three phases, one dialog, no modal blocking of the rest of the window:

  1. **Configuring** -- the operator ticks candidate bitrates (the existing
     checkboxes; there is no free-form bitrate entry anywhere here) and sets
     an observation duration, then clicks Start Scan. Nothing scans until
     this explicit click -- opening the popup alone never starts anything.
  2. **Scanning** -- candidate selection and duration are disabled; a
     progress readout shows the current bitrate, candidate N of M, and both
     per-candidate and overall progress; Cancel stops the worker safely.
  3. **Results** -- every scanned candidate appears as a selectable row with
     its numerical score and every metric that produced it (never a
     qualitative label like "strong"/"weak"/"possible"). Selecting a row
     whose observation actually completed enables Start Listening, which
     reads that row's bitrate, closes this popup, and asks MainWindow to
     configure and start live capture on it -- MainWindow never uses the
     highest score automatically.

Cancellation/closing has exactly one path out of this dialog: the
``cancelled`` signal, emitted identically by the action button (labelled
Cancel while scanning, Close otherwise) and by closing the window (the X).
``MainWindow`` connects it to ``stop_capture``, the same slot the main
window's own Stop button uses -- there is no second, dialog-local
cancellation mechanism.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QProgressBar, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..discovery import DEFAULT_SCAN_DURATION, MIN_SCAN_DURATION
from .theme import SPACE_LG, SPACE_MD, SPACE_SM, Theme
from .widgets import FlowLayout, ResponsiveDialog

_TABLE_HEADERS = (
    "Bitrate", "Score", "Persistent ID", "Bucket Stability", "Singleton/Churn",
    "Error", "Remote", "Format", "Structural", "Payload-ID", "Details",
)


def _kbit(bitrate: int) -> str:
    return "{:g}".format(bitrate / 1000.0)


def validate_scan_request(
    candidates: Sequence[int], duration: float,
) -> Optional[str]:
    """Pure validation, independently testable from the widgets above it.

    Returns a human-readable problem description, or ``None`` when the
    request is fine to start. Never raises on bad input -- a non-numeric or
    negative duration is exactly the kind of thing this exists to catch.
    """
    if not candidates:
        return "Select at least one bitrate to scan."
    try:
        value = float(duration)
    except (TypeError, ValueError):
        return "Observation duration must be a number."
    if value <= 0:
        return "Observation duration must be a positive number of seconds."
    if value < MIN_SCAN_DURATION:
        return "Observation duration must be at least {:g} seconds.".format(
            MIN_SCAN_DURATION)
    return None


def _score_cell(value: float, weight: float) -> str:
    return "{:.1f}/{:g}".format(value, weight)


class AutoScanDialog(ResponsiveDialog):
    """Non-modal Auto Scan popup: configure -> scan -> pick a result."""

    cancelled = Signal()
    #: (candidates, duration_seconds) -- emitted once, already validated,
    #: when the operator clicks Start Scan.
    scanRequested = Signal(tuple, float)
    #: The bitrate read from the selected, completed result row.
    startListening = Signal(int)

    def __init__(
        self, interface: str, candidate_bitrates: Sequence[int], theme: Theme,
        default_duration: float = DEFAULT_SCAN_DURATION, parent=None,
    ):
        super().__init__(parent)
        self.theme = theme
        self.interface = interface
        self.setWindowTitle("Auto Scan — {}".format(interface))
        self.resize(720, 640)
        # Non-modal for the same reason as every other non-blocking popup in
        # this project (see BusOverviewDialog): Start/Stop/Auto Scan are
        # already disabled for the whole popup's lifetime (see MainWindow.
        # _refresh_capture_controls), so nothing unsafe becomes reachable
        # while this is open.
        self.setModal(False)

        self._scanning = False
        self._candidates: List = []  # List[ScoredCandidate], parallel to table rows
        self._checkboxes: Dict[int, QCheckBox] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        outer.setSpacing(SPACE_MD)

        self.interface_label = QLabel("Interface: {}".format(interface))
        self.interface_label.setObjectName("PanelTitle")
        outer.addWidget(self.interface_label)

        # -- configuration: bitrate checkboxes (reused, not reinvented --
        # see the module docstring) + duration -----------------------------
        self.config_group = QGroupBox("Candidate bitrates")
        config_layout = QVBoxLayout(self.config_group)
        self._checkbox_flow = FlowLayout(spacing=SPACE_SM)
        checkbox_host = QWidget()
        checkbox_host.setLayout(self._checkbox_flow)
        for bitrate in candidate_bitrates:
            box = QCheckBox("{} kbit/s".format(_kbit(bitrate)))
            self._checkboxes[int(bitrate)] = box
            box.toggled.connect(self._update_start_scan_enabled)
            self._checkbox_flow.addWidget(box)
        config_layout.addWidget(checkbox_host)

        duration_row = QHBoxLayout()
        duration_row.addWidget(QLabel("Observation duration per candidate"))
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(int(MIN_SCAN_DURATION), 3600)
        self.duration_spin.setSingleStep(5)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setValue(max(int(MIN_SCAN_DURATION), int(default_duration)))
        duration_row.addWidget(self.duration_spin)
        self.duration_display = QLabel("")
        duration_row.addWidget(self.duration_display)
        duration_row.addStretch(1)
        config_layout.addLayout(duration_row)
        self.duration_spin.valueChanged.connect(self._update_duration_display)
        self._update_duration_display()

        action_row = QHBoxLayout()
        self.validation_label = QLabel("")
        self.validation_label.setObjectName("Muted")
        self.validation_label.setWordWrap(True)
        action_row.addWidget(self.validation_label, 1)
        self.start_scan_button = QPushButton("Start Scan")
        self.start_scan_button.clicked.connect(self._on_start_scan_clicked)
        action_row.addWidget(self.start_scan_button)
        config_layout.addLayout(action_row)
        outer.addWidget(self.config_group)

        # -- live scan progress ----------------------------------------------
        self.progress_group = QGroupBox("Scanning")
        progress_layout = QVBoxLayout(self.progress_group)
        self.phase_label = QLabel("")
        progress_layout.addWidget(self.phase_label)
        self.candidate_label = QLabel("")
        progress_layout.addWidget(self.candidate_label)

        overall_row = QHBoxLayout()
        overall_row.addWidget(QLabel("Overall"))
        self.overall_progress = QProgressBar()
        self.overall_progress.setFormat("Candidate %v of %m")
        overall_row.addWidget(self.overall_progress, 1)
        progress_layout.addLayout(overall_row)

        candidate_row = QHBoxLayout()
        candidate_row.addWidget(QLabel("Current candidate"))
        self.candidate_progress = QProgressBar()
        self.candidate_progress.setRange(0, 1000)
        candidate_row.addWidget(self.candidate_progress, 1)
        self.candidate_elapsed_label = QLabel("")
        candidate_row.addWidget(self.candidate_elapsed_label)
        progress_layout.addLayout(candidate_row)
        outer.addWidget(self.progress_group)
        self.progress_group.setVisible(False)

        # -- results -----------------------------------------------------------
        self.table = QTableWidget(0, len(_TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(list(_TABLE_HEADERS))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(len(_TABLE_HEADERS) - 1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.itemSelectionChanged.connect(self._update_start_listening_enabled)
        outer.addWidget(self.table, 1)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        bottom_row = QHBoxLayout()
        self.start_listening_button = QPushButton("Start Listening")
        self.start_listening_button.setEnabled(False)
        self.start_listening_button.clicked.connect(self._on_start_listening_clicked)
        bottom_row.addWidget(self.start_listening_button)
        bottom_row.addStretch(1)
        self.action_button = QPushButton("Close")
        self.action_button.clicked.connect(self.close)
        bottom_row.addWidget(self.action_button)
        outer.addLayout(bottom_row)

        self._update_start_scan_enabled()

    # -- configuration phase ------------------------------------------------

    def _update_duration_display(self, *_args) -> None:
        self.duration_display.setText(
            "({} s applied to every selected candidate)".format(self.duration_spin.value()))

    def selected_candidates(self) -> Tuple[int, ...]:
        return tuple(sorted(
            bitrate for bitrate, box in self._checkboxes.items() if box.isChecked()))

    def _update_start_scan_enabled(self, *_args) -> None:
        self.start_scan_button.setEnabled(not self._scanning)

    def _on_start_scan_clicked(self) -> None:
        candidates = self.selected_candidates()
        duration = float(self.duration_spin.value())
        problem = validate_scan_request(candidates, duration)
        if problem is not None:
            self.validation_label.setText(problem)
            return
        self.validation_label.setText("")
        self.table.setRowCount(0)
        self._candidates = []
        self.status_label.setText("")
        self.start_listening_button.setEnabled(False)
        self.scanRequested.emit(candidates, duration)

    # -- scan lifecycle (called by MainWindow around emitting scanRequested) --

    def set_scanning(self, scanning: bool) -> None:
        """MainWindow calls this exactly when it actually starts/stops the
        worker -- kept separate from _on_start_scan_clicked so the disabled/
        enabled state always reflects whether a worker is really running,
        never just "the button was clicked"."""
        self._scanning = scanning
        self.config_group.setEnabled(not scanning)
        self.progress_group.setVisible(scanning)
        self.start_scan_button.setEnabled(not scanning)
        self._update_start_listening_enabled()
        if scanning:
            self.overall_progress.setValue(0)
            self.candidate_progress.setValue(0)
            self.candidate_elapsed_label.setText("")
            self.action_button.setText("Cancel")
        else:
            self.action_button.setText("Close")

    # -- worker signal handlers (always run on the GUI thread) -----------

    def on_progress(self, item) -> None:
        self.phase_label.setText(item.message)
        self.overall_progress.setMaximum(max(1, item.total))
        self.overall_progress.setValue(min(item.completed, item.total))
        if item.bitrate is not None:
            self.candidate_label.setText("Testing {} kbit/s — candidate {} of {}".format(
                _kbit(item.bitrate), min(item.completed + 1, max(1, item.total)),
                item.total))
        if item.candidate_duration > 0:
            fraction = 0.0
            if item.stage == "candidate-progress":
                fraction = item.candidate_elapsed / item.candidate_duration
            elif item.stage == "candidate-complete":
                fraction = 1.0
            self.candidate_progress.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
            self.candidate_elapsed_label.setText("{:.0f}s / {:.0f}s".format(
                item.candidate_elapsed if item.stage == "candidate-progress"
                else item.candidate_duration,
                item.candidate_duration))
        if item.stage == "candidate-complete" and item.candidate is not None:
            self._append_row(item.candidate)

    def on_result(self, result) -> None:
        if result.cancelled:
            self.status_label.setText("Auto Scan was cancelled.")
            return
        if not result.candidates:
            text = "No candidates were scanned."
            if result.reasons:
                text += "\n\n" + "\n".join("- " + reason for reason in result.reasons)
            self.status_label.setText(text)
            return
        self.status_label.setText(
            "Scan complete — {} candidate(s) tested. Select a row, then Start "
            "Listening.".format(len(result.candidates)))

    def on_error(self, message: str) -> None:
        self.status_label.setText("Auto Scan failed: {}".format(message))

    # -- results table --------------------------------------------------

    def _append_row(self, candidate) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._candidates.append(candidate)
        # Row 0 is always the bitrate cell -- see selected_bitrate().
        self.table.setItem(row, 0, QTableWidgetItem("{} kbit/s".format(
            _kbit(candidate.bitrate))))

        components = candidate.components
        if components is None:
            score_text = "N/A"
            cells = ["N/A"] * 8
            details = "; ".join(candidate.reasons) or "Did not complete"
        else:
            score_text = "{:.1f}".format(candidate.total_score)
            cells = [
                _score_cell(components.persistent_id_score, 30),
                _score_cell(components.bucket_stability_score, 15),
                _score_cell(components.singleton_score + components.churn_score, 10),
                _score_cell(components.error_score, 15),
                _score_cell(components.remote_score, 10),
                _score_cell(components.format_score, 10),
                _score_cell(components.structural_score, 5),
                _score_cell(components.payload_migration_score, 5),
            ]
            details = (
                "records={} unique_ids={} persistent_ids={} singleton_ids={} "
                "error_rate={:.3f} remote_rate={:.3f} format_rate={:.3f} "
                "structural_validity={:.3f} payload_migration={:.3f}".format(
                    components.total_records, components.unique_ids,
                    components.persistent_ids, components.singleton_ids,
                    components.error_rate, components.remote_rate,
                    components.unexpected_format_rate, components.structural_validity,
                    components.multi_id_payload_ratio,
                )
            )
            if candidate.reasons:
                details += " — " + "; ".join(candidate.reasons)

        self.table.setItem(row, 1, QTableWidgetItem(score_text))
        for offset, text in enumerate(cells, start=2):
            self.table.setItem(row, offset, QTableWidgetItem(text))
        details_item = QTableWidgetItem(details)
        details_item.setToolTip(details)
        self.table.setItem(row, len(_TABLE_HEADERS) - 1, details_item)

    def _selected_row(self) -> Optional[int]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].row() if rows else None

    def selected_bitrate(self) -> Optional[int]:
        row = self._selected_row()
        if row is None or row >= len(self._candidates):
            return None
        return self._candidates[row].bitrate

    def _update_start_listening_enabled(self) -> None:
        row = self._selected_row()
        enabled = (
            not self._scanning and row is not None and row < len(self._candidates)
            and self._candidates[row].completed
        )
        self.start_listening_button.setEnabled(bool(enabled))

    def _on_start_listening_clicked(self) -> None:
        row = self._selected_row()
        if row is None or row >= len(self._candidates):
            return
        candidate = self._candidates[row]
        if not candidate.completed:
            return
        self.startListening.emit(int(candidate.bitrate))

    # -- cancellation: one path, whether Cancel/Close, X, or the button ---

    def closeEvent(self, event) -> None:
        self.cancelled.emit()
        super().closeEvent(event)


__all__ = ["AutoScanDialog", "validate_scan_request"]
