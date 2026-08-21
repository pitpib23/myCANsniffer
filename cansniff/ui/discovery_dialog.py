"""Non-blocking UI for confirmed passive or one-scan non-passive discovery."""

from __future__ import annotations

import copy
import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout,
    QLabel, QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout,
)

from ..config import Config
from ..discovery import (
    DEFAULT_BITRATES, AdapterDescriptor, AdapterScanResult, DiscoveryProgress,
    DiscoveryResult, DiscoverySafetyMode, DiscoveryStatus, DiscoveryThresholds,
    PassiveCapability, discover_bitrate, enumerate_adapters,
    passive_scan_available,
)
from .widgets import ResponsiveDialog


class ScanConfirmationDialog(ResponsiveDialog):
    """Mandatory, non-persistent confirmation for exactly one scan."""

    def __init__(self, adapter: AdapterDescriptor, passive: bool, parent=None):
        super().__init__(parent)
        self.adapter = adapter
        self.passive = passive
        self.setWindowTitle(
            "Auto Discover" if passive else "WARNING — NON-PASSIVE AUTO DISCOVERY")
        self.resize(560, 390 if passive else 500)
        layout = QVBoxLayout(self)

        heading = QLabel(
            "PASSIVE / LISTEN-ONLY" if passive
            else "WARNING — NON-PASSIVE AUTO DISCOVERY")
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)
        self.details_label = QLabel(
            "Adapter: {}\nBackend: {}\nChannel: {}\n\n{}".format(
                adapter.display_name, adapter.interface, adapter.channel,
                (
                    "myCANsniffer will cycle through supported Classic CAN bitrate "
                    "candidates and observe traffic.\n\nNo CAN frames will "
                    "intentionally be transmitted.\n\nThe detected bitrate will "
                    "NOT be applied automatically."
                    if passive else
                    "This adapter/backend does not provide a verified listen-only "
                    "mode. If you continue, myCANsniffer will open the adapter "
                    "normally and cycle through candidate CAN bitrate configurations.\n\n"
                    "Although myCANsniffer will not intentionally call send(), a "
                    "non-listen-only CAN controller may participate electrically, "
                    "including ACK/error behavior. This scan may affect the CAN "
                    "network. Use only where you accept that risk.\n\n"
                    "This permission applies to THIS SCAN ONLY."
                ),
            ))
        self.details_label.setWordWrap(True)
        self.details_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.details_label)

        self.acknowledgement = QCheckBox(
            "I understand this scan is not guaranteed passive.")
        self.acknowledgement.setVisible(not passive)
        layout.addWidget(self.acknowledgement)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.start_button = QPushButton(
            "Start Passive Scan" if passive else "Start Non-Passive Scan")
        buttons.addButton(self.start_button, QDialogButtonBox.AcceptRole)
        self.start_button.setEnabled(passive)
        self.acknowledgement.toggled.connect(self.start_button.setEnabled)
        self.start_button.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_safety_mode(self) -> Optional[DiscoverySafetyMode]:
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        if self.passive:
            return DiscoverySafetyMode.PASSIVE_REQUIRED
        if self.acknowledgement.isChecked():
            return DiscoverySafetyMode.USER_CONFIRMED_NON_PASSIVE
        return None


class DiscoveryWorker(QObject):
    adaptersReady = Signal(object)
    resultReady = Signal(object)
    progressChanged = Signal(object)
    errorOccurred = Signal(str)
    finished = Signal()

    def __init__(self, mode: str, enumerator: Callable, discoverer: Callable,
                 adapter: Optional[AdapterDescriptor] = None,
                 base_settings=None, candidates=DEFAULT_BITRATES,
                 thresholds=DiscoveryThresholds(),
                 safety_mode=DiscoverySafetyMode.PASSIVE_REQUIRED,
                 passive_verifier=None):
        super().__init__()
        self.mode = mode
        self.enumerator = enumerator
        self.discoverer = discoverer
        self.adapter = adapter
        self.base_settings = dict(base_settings or {})
        self.candidates = tuple(candidates)
        self.thresholds = thresholds
        self.safety_mode = DiscoverySafetyMode(safety_mode)
        self.passive_verifier = passive_verifier
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    @Slot()
    def run(self) -> None:
        try:
            if self.mode == "enumerate":
                result = self.enumerator(cancel_event=self.cancel_event)
                self.adaptersReady.emit(result)
            else:
                if self.adapter is None:
                    raise ValueError("No adapter selected")
                operation_mode = self.safety_mode
                result = self.discoverer(
                    self.adapter,
                    candidates=self.candidates,
                    base_settings=self.base_settings,
                    thresholds=self.thresholds,
                    cancel_event=self.cancel_event,
                    progress=self.progressChanged.emit,
                    safety_mode=operation_mode,
                    passive_verifier=self.passive_verifier,
                )
                self.resultReady.emit(result)
        except Exception as exc:
            self.errorOccurred.emit(str(exc))
        finally:
            # Authorization is operation-local. Keep only the result's audit
            # label; the worker itself returns to the safe default.
            self.safety_mode = DiscoverySafetyMode.PASSIVE_REQUIRED
            self.finished.emit()


class DiscoveryDialog(ResponsiveDialog):
    """Adapter selection and evidence display; core discovery remains Qt-free."""

    def __init__(self, config: Config, parent=None,
                 enumerator: Callable = enumerate_adapters,
                 discoverer: Callable = discover_bitrate,
                 confirmer: Optional[Callable[[AdapterDescriptor, bool],
                                              Optional[DiscoverySafetyMode]]] = None,
                 passive_verifier=None):
        super().__init__(parent)
        self.setWindowTitle("Auto Discover — Classic CAN")
        self.resize(720, 560)
        self.config = config
        self._enumerator = enumerator
        self._discoverer = discoverer
        self._confirmer = confirmer or self._show_scan_confirmation
        self._passive_verifier = passive_verifier
        self._thread: Optional[QThread] = None
        self._worker: Optional[DiscoveryWorker] = None
        self._scan: Optional[AdapterScanResult] = None
        self._result: Optional[DiscoveryResult] = None
        self._selected_settings = None
        discovery_config = self.config.get("discovery", {}) or {}
        self._candidates = tuple(
            discovery_config.get("classic_bitrates", DEFAULT_BITRATES) or DEFAULT_BITRATES)
        self._thresholds = DiscoveryThresholds.from_mapping(discovery_config)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Adapters are enumerated without opening them. Every Auto Scan asks for "
            "confirmation. Passive mode is preferred; an unverified backend requires "
            "explicit authorization for that scan only."
        )
        intro.setWordWrap(True)
        intro.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(intro)

        form = QFormLayout()
        self.adapter_combo = QComboBox()
        self.adapter_combo.currentIndexChanged.connect(self._adapter_changed)
        form.addRow("Adapter", self.adapter_combo)
        self.capability_label = QLabel("Discovering adapters…")
        self.capability_label.setWordWrap(True)
        self.capability_label.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred)
        form.addRow("Capability", self.capability_label)
        layout.addLayout(form)

        self.status_label = QLabel("Discovering adapters…")
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.status_label)

        self.evidence = QPlainTextEdit()
        self.evidence.setReadOnly(True)
        self.evidence.setPlaceholderText("Per-candidate evidence will appear here.")
        layout.addWidget(self.evidence, 1)

        row = QGridLayout()
        self.rescan_button = QPushButton("Rescan adapters")
        self.rescan_button.clicked.connect(self.start_enumeration)
        row.addWidget(self.rescan_button, 0, 0)
        self.test_button = QPushButton("Auto Scan")
        self.test_button.clicked.connect(self.start_bitrate_discovery)
        self.test_button.setEnabled(False)
        row.addWidget(self.test_button, 0, 1)
        self.cancel_button = QPushButton("Cancel operation")
        self.cancel_button.clicked.connect(self.cancel_operation)
        self.cancel_button.setEnabled(False)
        row.addWidget(self.cancel_button, 0, 2)
        self.use_button = QPushButton("Use detected configuration")
        self.use_button.clicked.connect(self._accept_result)
        self.use_button.setEnabled(False)
        row.addWidget(self.use_button, 1, 0, 1, 2)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.reject)
        row.addWidget(close_button, 1, 2)
        row.setColumnStretch(1, 1)
        layout.addLayout(row)

        QTimer.singleShot(0, self.start_enumeration)

    def _start_worker(self, worker: DiscoveryWorker) -> None:
        if self._thread is not None:
            return
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.adaptersReady.connect(self._on_adapters)
        worker.resultReady.connect(self._on_result)
        worker.progressChanged.connect(self._on_progress)
        worker.errorOccurred.connect(self._on_error)
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        thread.finished.connect(self._on_thread_finished)
        self._worker = worker
        self._thread = thread
        self.cancel_button.setEnabled(True)
        self.rescan_button.setEnabled(False)
        self.test_button.setEnabled(False)
        thread.start()

    @Slot()
    def start_enumeration(self) -> None:
        if self._thread is not None:
            return
        self._result = None
        self._selected_settings = None
        self.use_button.setEnabled(False)
        self.adapter_combo.clear()
        self.status_label.setText("Discovering adapters without opening CAN buses…")
        self.evidence.clear()
        self._start_worker(DiscoveryWorker(
            "enumerate", self._enumerator, self._discoverer))

    @Slot()
    def start_bitrate_discovery(self) -> None:
        adapter = self.current_adapter()
        if self._thread is not None or adapter is None:
            return
        if not adapter.implementation_supported or not adapter.auto_bitrate_supported:
            self.status_label.setText(
                adapter.scan_unavailable_reason
                or "This detected interface is enumeration-only; Auto Scan is unavailable."
            )
            return
        passive = passive_scan_available(adapter, self._passive_verifier)
        safety_mode = self._confirmer(adapter, passive)
        if safety_mode is None:
            self.status_label.setText("Auto Scan cancelled before opening the adapter.")
            return
        safety_mode = DiscoverySafetyMode(safety_mode)
        if passive:
            safety_mode = DiscoverySafetyMode.PASSIVE_REQUIRED
        elif safety_mode != DiscoverySafetyMode.USER_CONFIRMED_NON_PASSIVE:
            self.status_label.setText(
                "Non-passive Auto Scan requires explicit acknowledgement.")
            return
        self._result = None
        self._selected_settings = None
        self.use_button.setEnabled(False)
        self.evidence.clear()
        self.status_label.setText(
            "Testing Classic CAN bitrates passively…"
            if safety_mode == DiscoverySafetyMode.PASSIVE_REQUIRED else
            "Testing Classic CAN bitrates — NON-PASSIVE, user authorized…")
        self._start_worker(DiscoveryWorker(
            "bitrate", self._enumerator, self._discoverer,
            adapter=adapter,
            base_settings=self.config.get("source.live", {}) or {},
            candidates=self._candidates,
            thresholds=self._thresholds,
            safety_mode=safety_mode,
            passive_verifier=self._passive_verifier,
        ))

    def _show_scan_confirmation(
        self, adapter: AdapterDescriptor, passive: bool,
    ) -> Optional[DiscoverySafetyMode]:
        prompt = ScanConfirmationDialog(adapter, passive, self)
        prompt.exec()
        return prompt.selected_safety_mode()

    def cancel_operation(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.status_label.setText("Cancelling safely; closing current source…")
            self.cancel_button.setEnabled(False)

    def _on_adapters(self, scan: AdapterScanResult) -> None:
        self._scan = scan
        self.adapter_combo.clear()
        for adapter in scan.adapters:
            if (adapter.interface == "slcan"
                    and adapter.enumeration_source.value == "backend-known"):
                suffix = " (serial port may support SLCAN; protocol unverified)"
            else:
                suffix = "" if adapter.is_physical else " (non-physical)"
            self.adapter_combo.addItem(
                "{} — {}:{}{}".format(
                    adapter.display_name, adapter.interface, adapter.channel, suffix),
                adapter,
            )
        if self.config.get("source.type") == "live":
            configured_key = (
                str(self.config.get("source.live.interface", "")).lower(),
                str(self.config.get("source.live.channel", "")),
            )
            for index in range(self.adapter_combo.count()):
                candidate = self.adapter_combo.itemData(index)
                if (isinstance(candidate, AdapterDescriptor)
                        and candidate.key == configured_key):
                    self.adapter_combo.setCurrentIndex(index)
                    break
        messages = ["{}: {}{}".format(
            item.interface, item.status.value,
            " — " + item.message if item.message else "")
            for item in scan.backends]
        self.evidence.setPlainText("Adapter enumeration\n" + "\n".join(messages))
        if scan.cancelled:
            self.status_label.setText("Adapter discovery cancelled.")
        elif scan.adapters:
            self.status_label.setText(
                "{} adapter/interface option(s) found. Select one to inspect."
                .format(len(scan.adapters)))
        else:
            self.status_label.setText("No concrete adapters were detected.")
        self._adapter_changed()

    def current_adapter(self) -> Optional[AdapterDescriptor]:
        value = self.adapter_combo.currentData()
        return value if isinstance(value, AdapterDescriptor) else None

    def _adapter_changed(self, *_args) -> None:
        adapter = self.current_adapter()
        if adapter is None:
            self.capability_label.setText("No adapter selected")
            self.test_button.setEnabled(False)
            return
        fd = "Unknown" if adapter.supports_fd is None else ("Yes" if adapter.supports_fd else "No")
        qualification = "hardware qualified" if adapter.hardware_qualified else "not hardware qualified"
        available = adapter.implementation_supported and adapter.auto_bitrate_supported
        if not available:
            auto = "unavailable (enumeration only)"
        elif adapter.passive_capability in (
                PassiveCapability.SUPPORTED_BY_BACKEND_POLICY,
                PassiveCapability.NOT_APPLICABLE):
            auto = "available (confirmation required)"
        else:
            auto = "available with warning and acknowledgement"
        capability = "Passive: {} | CAN FD: {} | Auto Scan: {} | {}".format(
            adapter.passive_capability.value, fd, auto, qualification)
        if adapter.scan_unavailable_reason:
            capability += " | " + adapter.scan_unavailable_reason
        self.capability_label.setText(capability)
        self.test_button.setText(
            "Auto Scan" if adapter.passive_capability in (
                PassiveCapability.SUPPORTED_BY_BACKEND_POLICY,
                PassiveCapability.NOT_APPLICABLE) else "Auto Scan ⚠")
        self.test_button.setEnabled(
            self._thread is None and available)

    def _on_progress(self, item: DiscoveryProgress) -> None:
        self.status_label.setText(item.message)
        if item.candidate is not None:
            self.evidence.appendPlainText(self._format_candidate(item.candidate))

    @staticmethod
    def _format_candidate(candidate) -> str:
        reasons = "; ".join(candidate.reasons)
        return ("{} bit/s — {} | frames={} valid={} errors={} IDs={} repeated={} | {}"
                .format(candidate.bitrate, candidate.status.value, candidate.frames,
                        candidate.valid_frames, candidate.error_frames,
                        candidate.unique_ids, candidate.repeated_ids, reasons))

    def _on_result(self, result: DiscoveryResult) -> None:
        self._result = result
        self.status_label.setText(
            "Detected {} bit/s".format(result.selected_bitrate)
            if result.status == DiscoveryStatus.DETECTED
            else "Discovery result: {}".format(result.status.value))
        observation_mode = (
            "NON-PASSIVE — user authorized for this scan"
            if result.safety_mode == DiscoverySafetyMode.USER_CONFIRMED_NON_PASSIVE
            else "PASSIVE / LISTEN-ONLY")
        self.evidence.appendPlainText(
            "\nObservation mode: {}\nResult: {}".format(
                observation_mode, result.status.value))
        for reason in result.reasons:
            self.evidence.appendPlainText("- " + reason)
        for warning in result.warnings:
            self.evidence.appendPlainText("WARNING: " + warning)
        # Enable only after QThread.finished confirms the worker is entirely
        # done; accepting the dialog must never orphan its final emission.
        self.use_button.setEnabled(False)

    def _on_error(self, message: str) -> None:
        self.status_label.setText("Discovery failed: {}".format(message))

    def _on_thread_finished(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._worker = None
        self._thread = None
        thread.deleteLater()
        self.cancel_button.setEnabled(False)
        self.rescan_button.setEnabled(True)
        self._adapter_changed()
        self.use_button.setEnabled(
            self._result is not None
            and self._result.status == DiscoveryStatus.DETECTED)

    def _accept_result(self) -> None:
        if self._result is None or self._result.selected_bitrate is None:
            return
        live = copy.deepcopy(self.config.get("source.live", {}) or {})
        live.update({
            "interface": self._result.adapter.interface,
            "channel": self._result.adapter.channel,
            "bitrate": self._result.selected_bitrate,
            "fd": False,
        })
        self._selected_settings = live
        self.accept()

    def selected_settings(self):
        return copy.deepcopy(self._selected_settings)

    def _shutdown_worker(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None:
            thread = self._thread
            thread.quit()
            thread.wait()
            self._worker = None
            self._thread = None

    def reject(self) -> None:
        self._shutdown_worker()
        super().reject()

    def closeEvent(self, event) -> None:
        self._shutdown_worker()
        super().closeEvent(event)


__all__ = ["DiscoveryDialog", "DiscoveryWorker", "ScanConfirmationDialog"]
