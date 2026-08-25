"""Auto Scan progress dialog.

Purely a view: it never touches ``SocketCanLink``/``SocketCanSessionController``,
never runs on a worker thread, and never decides anything about the physical
link -- it only renders the ``DiscoveryProgress``/``DiscoveryResult``/error
events ``cansniff/ui/main_window.py`` hands it, which themselves originate
from ``SocketCanDiscoveryWorker`` on a background ``QThread`` (see
``discovery_worker.py``). All three public slots below (``on_progress``,
``on_result``, ``on_error``) are meant to be connected directly to that
worker's Qt signals -- signal/slot delivery across threads is always queued
onto the GUI thread, so this dialog is only ever touched from there.

Cancellation has exactly one path out of this dialog: the ``cancelled``
signal, emitted identically by the Cancel/Close button and by closing the
window (the X). ``MainWindow`` connects it straight to ``stop_capture``,
the same slot the main window's own Stop button uses -- there is no second,
dialog-local cancellation mechanism.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QProgressBar,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..discovery.model import CandidateStatus, DiscoveryStatus
from .theme import SPACE_LG, SPACE_MD, Theme
from .widgets import ResponsiveDialog

#: Human-facing label for each terminal candidate status -- kept here (a
#: view concern) rather than duplicated as a second string table elsewhere.
_CANDIDATE_LABELS = {
    CandidateStatus.NO_TRAFFIC: "No traffic",
    CandidateStatus.WEAK: "Weak",
    CandidateStatus.POSSIBLE: "Possible",
    CandidateStatus.STABLE: "Stable",
    CandidateStatus.ERROR: "Error",
    CandidateStatus.CONFIGURATION_ERROR: "Unsupported",
    CandidateStatus.CANCELLED: "Cancelled",
}

_RESULT_MESSAGES = {
    DiscoveryStatus.NO_TRAFFIC: "No stable CAN traffic detected on {interface}.",
    DiscoveryStatus.AMBIGUOUS: (
        "Multiple CAN bitrates produced credible traffic.\n"
        "Automatic detection will not guess."),
    DiscoveryStatus.CONFIGURATION_ERROR: "Could not configure {interface}.",
    DiscoveryStatus.INCONCLUSIVE: "No candidate produced sustained stable traffic.",
    DiscoveryStatus.CANCELLED: "Auto Scan was cancelled.",
}


def _kbit(bitrate: int) -> str:
    return "{:g}".format(bitrate / 1000.0)


class AutoScanDialog(ResponsiveDialog):
    """Non-modal progress/result popup for one Auto Scan attempt."""

    cancelled = Signal()

    def __init__(self, interface: str, total_candidates: int, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.interface = interface
        self.setWindowTitle("Auto Scan — {}".format(interface))
        self.resize(560, 520)
        # Non-modal: Start/Auto Scan/Stop are already disabled for the
        # whole scan (see MainWindow._refresh_capture_controls), so nothing
        # unsafe becomes reachable while this is open -- and a non-modal
        # dialog keeps the rest of the window (Messages/Trace/etc. on
        # already-retained history) usable, matching _show_bus_overview's
        # convention for this project's other non-blocking popups.
        self.setModal(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        outer.setSpacing(SPACE_MD)

        self.interface_label = QLabel("Interface: {}".format(interface))
        self.interface_label.setObjectName("PanelTitle")
        outer.addWidget(self.interface_label)

        self.phase_label = QLabel("Checking {}…".format(interface))
        outer.addWidget(self.phase_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, max(1, total_candidates))
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Candidate %v of %m")
        outer.addWidget(self.progress_bar)

        self.bitrate_label = QLabel("")
        outer.addWidget(self.bitrate_label)

        stats_box = QGroupBox("Current candidate")
        stats_form = QHBoxLayout(stats_box)
        stats_form.setSpacing(SPACE_LG)
        self._stat_labels = {}
        for key, caption in (
            ("valid", "Valid frames"), ("errors", "Error frames"),
            ("unique_ids", "Unique IDs"), ("stable_ids", "Stable IDs"),
            ("best_id", "Strongest ID"), ("best_id_observations", "Observations"),
        ):
            column = QVBoxLayout()
            column.setSpacing(0)
            caption_label = QLabel(caption)
            caption_label.setObjectName("Muted")
            value_label = QLabel("—")
            column.addWidget(caption_label)
            column.addWidget(value_label)
            stats_form.addLayout(column)
            self._stat_labels[key] = value_label
        outer.addWidget(stats_box)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Bitrate", "Result"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        outer.addWidget(self.table, 1)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.action_button = QPushButton("Cancel")
        self.action_button.clicked.connect(self.close)
        button_row.addWidget(self.action_button)
        outer.addLayout(button_row)

        self._finished = False

    # -- worker signal handlers (always run on the GUI thread) -----------

    def on_progress(self, item) -> None:
        self.phase_label.setText(item.message)
        self.progress_bar.setMaximum(max(1, item.total))
        self.progress_bar.setValue(min(item.completed, item.total))
        if item.stage in ("candidate-start", "candidate-listening"):
            # item.message already names the candidate rate for these two
            # stages ("Testing… "/"Listening at… "); mirror it in the
            # dedicated bitrate label so it stays visible once the phase
            # label moves on to "Evaluating candidates…" etc.
            self.bitrate_label.setText("Testing: {}".format(item.message))
        if item.stage == "candidate-complete" and item.candidate is not None:
            self._update_stats(item.candidate)
            self._append_row(item.candidate)

    def on_result(self, result) -> None:
        self._finished = True
        if result.status == DiscoveryStatus.DETECTED:
            self.status_label.setText(
                "Detected {} kbit/s — starting capture…".format(_kbit(result.selected_bitrate)))
            # MainWindow closes this dialog itself once capture has
            # actually started (see _on_discovery_thread_finished) --
            # nothing further to do here for the success path.
            return
        template = _RESULT_MESSAGES.get(result.status, "Auto Scan finished: {}".format(
            result.status.value))
        text = template.format(interface=self.interface)
        # The summary line alone cannot distinguish "no traffic observed"
        # from "scan never reached the listening phase at all" (for example
        # a systemic listen-only verification failure -- see
        # cansniff/socketcan.py's SYSTEMIC_ERROR_KINDS) -- both can produce
        # a CONFIGURATION_ERROR/generic-template result. result.reasons
        # always carries that distinction; show it here so this, the
        # primary result surface during a real scan, never hides it the way
        # a bare templated one-liner would. Mirrors the wording
        # MainWindow's own (dialog-absent) fallback path already uses.
        if result.reasons:
            text += "\n\n" + "\n".join("- " + reason for reason in result.reasons)
        self.status_label.setText(text)
        self._show_close_button()

    def on_error(self, message: str) -> None:
        self._finished = True
        self.status_label.setText(message)
        self._show_close_button()

    # -- internal -----------------------------------------------------------

    def _update_stats(self, candidate) -> None:
        self._stat_labels["valid"].setText(str(candidate.valid_frames))
        self._stat_labels["errors"].setText(str(candidate.error_frames))
        self._stat_labels["unique_ids"].setText(str(candidate.unique_ids))
        self._stat_labels["stable_ids"].setText(str(candidate.repeated_ids))
        self._stat_labels["best_id"].setText(
            "0x{:X}".format(candidate.best_id) if candidate.best_id is not None else "—")
        self._stat_labels["best_id_observations"].setText(
            str(candidate.best_id_observations) if candidate.best_id is not None else "—")

    def _append_row(self, candidate) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem("{} kbit/s".format(_kbit(candidate.bitrate))))
        label = _CANDIDATE_LABELS.get(candidate.status, candidate.status.value)
        if candidate.status == CandidateStatus.STABLE:
            label = "{} — {} frames — {} IDs".format(
                label, candidate.valid_frames, candidate.unique_ids)
        self.table.setItem(row, 1, QTableWidgetItem(label))

    def _show_close_button(self) -> None:
        self.action_button.setText("Close")

    # -- cancellation: one path, whether Cancel, X, or the button ---------

    def closeEvent(self, event) -> None:
        self.cancelled.emit()
        super().closeEvent(event)


__all__ = ["AutoScanDialog"]
