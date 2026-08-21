"""Factual session traffic and capture-integrity overview.

This dialog deliberately presents observation facts only.  It contains no
protocol labels or payload interpretation, and every bounded or unavailable
quantity says so explicitly.
"""

from __future__ import annotations

from typing import Dict

from PySide6.QtWidgets import (
    QFormLayout, QGroupBox, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from ..analysis.profile import (
    IntegrityStatus, SourceState, TrafficProfileSnapshot,
)
from .theme import SPACE_LG, SPACE_MD, SPACE_SM, Theme
from .widgets import ResponsiveDialog


def _count(value: int) -> str:
    return "{:,}".format(value)


def _optional_count(value) -> str:
    return "unavailable" if value is None else _count(value)


def _duration(seconds: float) -> str:
    return "{:.3f} s".format(seconds) if seconds < 10 else "{:.1f} s".format(seconds)


class BusOverviewDialog(ResponsiveDialog):
    """Read-only view of an immutable :class:`TrafficProfileSnapshot`."""

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.setWindowTitle("BUS overview — observed traffic")
        self.resize(620, 690)
        self.fields: Dict[str, QLabel] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        outer.setSpacing(SPACE_MD)

        heading = QLabel("BUS")
        heading.setObjectName("PanelTitle")
        outer.addWidget(heading)
        note = QLabel(
            "Session-wide, protocol-neutral observations since the last Clear. "
            "Retained figures describe the bounded analysis history."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        outer.addWidget(note)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, SPACE_SM, 0)
        body_layout.setSpacing(SPACE_MD)
        body_layout.addWidget(self._group("Source", (
            ("source", "Configured/current source"), ("source_state", "State"),
            ("mode", "Mode"), ("can", "CAN"), ("bitrate", "Bitrate"),
        )))
        body_layout.addWidget(self._group("Traffic — session horizon", (
            ("rate", "Average rate"), ("duration", "Observed duration"),
            ("processed", "Processed"), ("unique", "Unique message keys"),
            ("standard", "11-bit frames"), ("extended", "29-bit frames"),
            ("classic", "Classic CAN"), ("fd", "CAN FD"),
            ("brs", "BRS"), ("remote", "Remote"), ("errors", "Error frames"),
            ("channels", "Channels"),
        )))
        body_layout.addWidget(self._group("Retained horizon", (
            ("retained", "Frames retained"),
            ("retained_unique", "Retained message keys"),
            ("retained_duration", "Retained duration"),
            ("retained_complete", "Contains whole session"),
        )))
        body_layout.addWidget(self._group("Capture integrity", (
            ("integrity", "Summary"), ("received", "Received by source"),
            ("accepted", "Accepted by capture filters"),
            ("integrity_processed", "Processed by application"),
            ("ui_dropped", "UI-delivery dropped"),
            ("pause_hidden", "Hidden while paused"),
            ("source_errors", "Source/backend errors"),
            ("parse_errors", "File parse errors"),
            ("logger_failures", "Logger failures"),
            ("driver_overruns", "Driver overruns"),
            ("integrity_reasons", "Details"),
        )))
        body_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

    def _group(self, title, rows):
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_MD)
        form.setHorizontalSpacing(SPACE_LG)
        form.setVerticalSpacing(SPACE_SM)
        for name, label in rows:
            value = QLabel("—")
            value.setTextInteractionFlags(value.textInteractionFlags())
            value.setWordWrap(True)
            value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            value.setObjectName("Muted" if name == "integrity_reasons" else "")
            self.fields[name] = value
            form.addRow(label, value)
        return box

    def set_snapshot(self, snapshot: TrafficProfileSnapshot, source: str,
                     mode: str, bitrate: str) -> None:
        """Replace every displayed value from one immutable snapshot."""
        integrity = snapshot.integrity
        self.fields["source"].setText(source or "not configured")
        self.fields["source_state"].setText({
            SourceState.NOT_STARTED: "not started",
            SourceState.ACTIVE: "open / receiving",
            SourceState.STOPPED: "stopped",
            SourceState.END_OF_SOURCE: "ended normally",
            SourceState.ERROR: "source error",
        }[integrity.source_state])
        self.fields["mode"].setText(mode)
        if snapshot.fd_frames and snapshot.classic_frames:
            can_kind = "Classic + FD observed"
        elif snapshot.fd_frames:
            can_kind = "CAN FD observed"
        elif snapshot.classic_frames:
            can_kind = "Classic CAN observed"
        else:
            can_kind = "no frames observed"
        self.fields["can"].setText(can_kind)
        self.fields["bitrate"].setText(bitrate)

        self.fields["rate"].setText("{:,.1f} frames/s".format(
            snapshot.average_session_rate_hz))
        self.fields["duration"].setText(_duration(snapshot.session_horizon.duration))
        self.fields["processed"].setText(_count(snapshot.processed_frames))
        self.fields["unique"].setText(_count(snapshot.unique_message_keys))
        self.fields["standard"].setText(_count(snapshot.standard_frames))
        self.fields["extended"].setText(_count(snapshot.extended_frames))
        self.fields["classic"].setText(_count(snapshot.classic_frames))
        self.fields["fd"].setText(_count(snapshot.fd_frames))
        self.fields["brs"].setText(_count(snapshot.brs_frames))
        self.fields["remote"].setText(_count(snapshot.remote_frames))
        self.fields["errors"].setText(_count(snapshot.error_frames) + " observed")
        self.fields["channels"].setText(
            ", ".join(snapshot.channels) if snapshot.channels else "none observed")

        retained = snapshot.retained_horizon
        self.fields["retained"].setText(_count(retained.frame_count))
        self.fields["retained_unique"].setText(_count(snapshot.retained_message_keys))
        self.fields["retained_duration"].setText(_duration(retained.duration))
        self.fields["retained_complete"].setText("yes" if retained.complete else "no — bounded window")

        summary = {
            IntegrityStatus.NOT_STARTED: "Not started",
            IntegrityStatus.NO_APPLICATION_LOSS_OBSERVED:
                "No application-level loss observed",
            IntegrityStatus.DEGRADED: "Degraded",
        }[integrity.status]
        self.fields["integrity"].setText(summary)
        self.fields["received"].setText(_count(integrity.received))
        self.fields["accepted"].setText(_count(integrity.accepted))
        self.fields["integrity_processed"].setText(_count(integrity.processed))
        self.fields["ui_dropped"].setText(_count(integrity.ui_dropped))
        self.fields["pause_hidden"].setText(_count(integrity.pause_hidden))
        self.fields["source_errors"].setText(_count(integrity.source_errors))
        self.fields["parse_errors"].setText(_optional_count(integrity.parse_errors))
        self.fields["logger_failures"].setText(_count(integrity.logger_failures))
        self.fields["driver_overruns"].setText(_optional_count(integrity.driver_overruns))
        self.fields["integrity_reasons"].setText("; ".join(integrity.reasons))


__all__ = ["BusOverviewDialog"]
