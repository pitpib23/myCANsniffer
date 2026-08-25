"""Settings dialog.

Common options get proper widgets; everything else — including keys this
dialog does not know about — is editable in the Raw JSON tab.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from ..config import Config
from ..discovery import DEFAULT_INTERFACE
from ..sources.live import listen_only_support
from .widgets import ResponsiveDialog, scrollable


class ConfigDialog(ResponsiveDialog):
    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(760, 620)
        self.config = config
        self._result: Dict[str, Any] = {}

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(scrollable(self._build_source_tab()), "Source")
        self.tabs.addTab(scrollable(self._build_general_tab()), "Capture && Display")
        self.tabs.addTab(self._build_raw_tab(), "Raw JSON")
        layout.addWidget(self.tabs, 1)

        path_label = QLabel("Stored in: {}".format(config.path))
        path_label.setWordWrap(True)
        layout.addWidget(path_label)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._on_accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        self._on_source_type_changed()
        self._on_interface_changed()

    # -- source tab -----------------------------------------------------

    def _build_source_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Source type"))
        self.source_type = QComboBox()
        self.source_type.addItems(["file", "live"])
        self.source_type.setCurrentText(str(self.config.get("source.type", "file")))
        self.source_type.currentTextChanged.connect(self._on_source_type_changed)
        type_row.addWidget(self.source_type)
        type_row.addStretch(1)
        layout.addLayout(type_row)

        # -- file
        self.file_box = QGroupBox("Offline capture file (playback into the parser only)")
        file_form = QFormLayout(self.file_box)
        path_row = QHBoxLayout()
        self.file_path = QLineEdit(str(self.config.get("source.file.path", "baseline.asc")))
        path_row.addWidget(self.file_path, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)
        container = QWidget()
        container.setLayout(path_row)
        file_form.addRow("File", container)

        self.file_speed = QDoubleSpinBox()
        self.file_speed.setRange(0.0, 100.0)
        self.file_speed.setSingleStep(0.25)
        self.file_speed.setDecimals(2)
        self.file_speed.setValue(float(self.config.get("source.file.speed", 0.0) or 0.0))
        self.file_speed.setToolTip("0 = load as fast as possible, 1 = original timing")
        file_form.addRow("Playback speed", self.file_speed)

        self.file_loop = QCheckBox("Restart when the end of the file is reached")
        self.file_loop.setChecked(bool(self.config.get("source.file.loop", False)))
        file_form.addRow("", self.file_loop)
        layout.addWidget(self.file_box)

        # -- live. The physical backend is always SocketCAN -- there is
        # nothing to choose here, see cansniff/sources/live.py.
        self.live_box = QGroupBox("Live capture — SocketCAN (listen-only)")
        live_form = QFormLayout(self.live_box)

        self.live_channel = QLineEdit(
            str(self.config.get("source.live.channel", DEFAULT_INTERFACE)))
        self.live_channel.setPlaceholderText(DEFAULT_INTERFACE)
        self.live_channel.setToolTip(
            "The SocketCAN network interface, as shown by `ip link` -- "
            "normally can0 on a Raspberry Pi with a single CAN HAT.")
        live_form.addRow("Interface", self.live_channel)

        # Configuration only -- not a workflow toggle. Whether a scan runs
        # at all is a distinct action (the Auto Scan button/F8 in the main
        # window), never something Settings decides; Start always uses
        # this bitrate as-is, deterministically applied to the interface --
        # see cansniff/session.py and cansniff/ui/main_window.py's
        # start_capture/start_auto_scan.
        self.live_bitrate = QSpinBox()
        self.live_bitrate.setRange(1000, 8000000)
        self.live_bitrate.setSingleStep(50000)
        self.live_bitrate.setValue(int(self.config.get("source.live.bitrate", 500000)))
        self.live_bitrate.setToolTip(
            "Used by Start, applied to the interface exactly as configured "
            "here. Auto Scan (the main window's own button) finds and "
            "applies a bitrate automatically instead, and updates this "
            "value to match when it does."
        )
        live_form.addRow("Manual bitrate (bit/s)", self.live_bitrate)

        self.live_fd = QCheckBox(
            "CAN FD (manual bitrate only -- Auto Scan is Classic CAN only)")
        self.live_fd.setChecked(bool(self.config.get("source.live.fd", False)))
        live_form.addRow("", self.live_fd)

        self.live_data_bitrate = QSpinBox()
        self.live_data_bitrate.setRange(1000, 12000000)
        self.live_data_bitrate.setSingleStep(100000)
        self.live_data_bitrate.setValue(int(self.config.get("source.live.data_bitrate", 2000000)))
        live_form.addRow("FD data bitrate", self.live_data_bitrate)

        self.require_listen_only = QCheckBox(
            "Require confirmed listen-only mode"
        )
        self.require_listen_only.setChecked(bool(
            self.config.get("source.live.require_listen_only", True)))
        self.require_listen_only.setToolTip(
            "Recommended. When disabled, unverified adapters may be opened "
            "for receive-only application use, but the CAN controller may "
            "still acknowledge frames or otherwise affect the physical bus."
        )
        self.require_listen_only.toggled.connect(self._on_require_toggled)
        live_form.addRow("", self.require_listen_only)

        self.support_label = QLabel()
        self.support_label.setWordWrap(True)
        live_form.addRow("Passive support", self.support_label)

        self.live_extra = QPlainTextEdit(
            json.dumps(self.config.get("source.live.extra_kwargs", {}) or {}, indent=2)
        )
        self.live_extra.setMinimumHeight(70)
        self.live_extra.setMaximumHeight(140)
        self.live_extra.setToolTip(
            "Harmless backend-specific python-can keyword arguments, as JSON. "
            "Interface, channel, bitrate/FD, receive-own-messages, driver mode, "
            "and passive/listen-only settings cannot be overridden here."
        )
        live_form.addRow("Extra kwargs", self.live_extra)

        layout.addWidget(self.live_box)
        layout.addStretch(1)
        return page

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select capture file", self.file_path.text(),
            "CAN captures (*.asc *.blf *.log *.csv *.trc);;All files (*)",
        )
        if path:
            self.file_path.setText(path)

    def _on_source_type_changed(self, *_args) -> None:
        is_file = self.source_type.currentText() == "file"
        self.file_box.setVisible(is_file)
        self.live_box.setVisible(not is_file)

    def _on_interface_changed(self, *_args) -> None:
        support = listen_only_support("socketcan")
        if not self.require_listen_only.isChecked():
            self.support_label.setText(
                "<b style='color:#c08020'>{}</b> Listen-only will still be "
                "attempted, but an unverified fallback is allowed because "
                "confirmation is disabled.".format(support))
        else:
            self.support_label.setText("<b style='color:#3a8a3a'>{}</b>".format(support))

    def _on_require_toggled(self, checked: bool) -> None:
        if not checked:
            answer = QMessageBox.warning(
                self,
                "Allow unverified CAN hardware?",
                "Without confirmed listen-only mode, the adapter may acknowledge "
                "CAN frames, emit controller/error traffic, or otherwise affect "
                "the physical bus. The application will still never call a send "
                "API, but receive-only software is not the same as electrically "
                "passive hardware.\n\nAllow unverified hardware anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self.require_listen_only.setChecked(True)
                return
        self._on_interface_changed()

    # -- general tab ----------------------------------------------------

    def _build_general_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.queue_size = QSpinBox()
        self.queue_size.setRange(100, 1000000)
        self.queue_size.setSingleStep(1000)
        self.queue_size.setValue(int(self.config.get("capture.queue_size", 20000)))
        form.addRow("Batch sizing target (frames)", self.queue_size)

        self.refresh_ms = QSpinBox()
        self.refresh_ms.setRange(10, 2000)
        self.refresh_ms.setValue(int(self.config.get("capture.ui_refresh_ms", 100)))
        form.addRow("UI refresh (ms)", self.refresh_ms)

        self.max_retained = QSpinBox()
        self.max_retained.setRange(1000, 5000000)
        self.max_retained.setSingleStep(10000)
        self.max_retained.setValue(int(self.config.get("capture.max_frames_retained", 200000)))
        form.addRow("Trace history (frames)", self.max_retained)

        self.relative_ts = QCheckBox("Show timestamps relative to the first frame")
        self.relative_ts.setChecked(bool(self.config.get("ui.relative_timestamps", True)))
        form.addRow("", self.relative_ts)

        self.highlight_changed = QCheckBox("Highlight bytes that have changed")
        self.highlight_changed.setChecked(bool(self.config.get("ui.highlight_changed_bytes", True)))
        form.addRow("", self.highlight_changed)

        self.font_family = QLineEdit(str(self.config.get("ui.font_family", "")))
        self.font_family.setPlaceholderText("automatic — Cascadia Mono, then Consolas")
        form.addRow("Monospace font", self.font_family)

        self.font_size = QSpinBox()
        self.font_size.setRange(7, 18)
        self.font_size.setValue(int(self.config.get("ui.font_size", 9)))
        form.addRow("Base font size", self.font_size)

        self.log_enabled = QCheckBox("Write received frames to a capture file")
        self.log_enabled.setChecked(bool(self.config.get("logging.enabled", False)))
        form.addRow("", self.log_enabled)

        self.log_dir = QLineEdit(str(self.config.get("logging.directory", "captures")))
        form.addRow("Log directory", self.log_dir)

        self.log_format = QComboBox()
        self.log_format.addItems(["csv", "jsonl"])
        self.log_format.setCurrentText(str(self.config.get("logging.format", "csv")))
        form.addRow("Log format", self.log_format)

        return page

    # -- raw tab --------------------------------------------------------

    def _build_raw_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            "Full configuration document. Edits here are applied on OK and override "
            "the widgets on the other tabs."
        )
        note.setWordWrap(True)
        note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(note)
        self.raw_edit = QPlainTextEdit(self.config.as_json())
        self.raw_edit.setFont(QFont("Consolas", 10))
        self.raw_edit.setMinimumWidth(0)
        self.raw_edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        layout.addWidget(self.raw_edit, 1)
        self._raw_original = self.raw_edit.toPlainText()
        return page

    # -- accept ---------------------------------------------------------

    def _on_accept(self) -> None:
        raw_text = self.raw_edit.toPlainText()
        raw_edited = raw_text != self._raw_original

        if raw_edited:
            try:
                data = json.loads(raw_text)
                if not isinstance(data, dict):
                    raise ValueError("the root must be a JSON object")
            except Exception as exc:
                QMessageBox.warning(self, "Invalid JSON", "Raw configuration: {}".format(exc))
                self.tabs.setCurrentIndex(self.tabs.count() - 1)
                return
            self._result = data
            self.accept()
            return

        try:
            extra = json.loads(self.live_extra.toPlainText() or "{}")
            if not isinstance(extra, dict):
                raise ValueError("extra kwargs must be a JSON object")
        except Exception as exc:
            QMessageBox.warning(self, "Invalid JSON", "Extra kwargs: {}".format(exc))
            return

        data = copy.deepcopy(self.config.data)
        data.setdefault("source", {})
        data["source"]["type"] = self.source_type.currentText()
        data["source"]["file"] = {
            "path": self.file_path.text().strip(),
            "speed": float(self.file_speed.value()),
            "loop": bool(self.file_loop.isChecked()),
        }
        data["source"]["live"] = {
            "interface": "socketcan",
            "channel": self.live_channel.text().strip() or DEFAULT_INTERFACE,
            "bitrate": int(self.live_bitrate.value()),
            "data_bitrate": int(self.live_data_bitrate.value()),
            "fd": bool(self.live_fd.isChecked()),
            # No longer surfaced in this dialog -- there is no "auto
            # bitrate" workflow toggle; Auto Scan (the main window's own
            # button) is a separate, explicit action -- see
            # cansniff/ui/main_window.py and cansniff/config.py's own note
            # on this key. Carried over as-is rather than silently reset to
            # the DEFAULTS value, for an older config file that still has
            # it; nothing reads it anymore either way.
            "auto_bitrate": self.config.get("source.live.auto_bitrate", True),
            "require_listen_only": bool(self.require_listen_only.isChecked()),
            "extra_kwargs": extra,
        }
        data.setdefault("capture", {})
        data["capture"]["queue_size"] = int(self.queue_size.value())
        data["capture"]["ui_refresh_ms"] = int(self.refresh_ms.value())
        data["capture"]["max_frames_retained"] = int(self.max_retained.value())
        data.setdefault("ui", {})
        data["ui"]["relative_timestamps"] = bool(self.relative_ts.isChecked())
        data["ui"]["highlight_changed_bytes"] = bool(self.highlight_changed.isChecked())
        data["ui"]["font_family"] = self.font_family.text().strip()
        data["ui"]["font_size"] = int(self.font_size.value())
        data.setdefault("logging", {})
        data["logging"]["enabled"] = bool(self.log_enabled.isChecked())
        data["logging"]["directory"] = self.log_dir.text().strip() or "captures"
        data["logging"]["format"] = self.log_format.currentText()

        self._result = data
        self.accept()

    def updated_config(self) -> Dict[str, Any]:
        return self._result
