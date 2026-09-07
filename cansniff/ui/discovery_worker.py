"""Qt worker driving passive SocketCAN bitrate discovery off the UI thread.

The discovery algorithm itself (``cansniff.discovery.discover_socketcan_bitrate``)
is Qt-free and independently testable; this wrapper only marshals it onto a
QThread and turns its progress/result into signals, exactly like
``cansniff.capture.CaptureWorker`` does for ordinary capture. Owned and torn
down by MainWindow -- see ``_start_discovery``/``_teardown_discovery_thread``.
"""

from __future__ import annotations

import threading
from typing import Callable, Iterable, Optional

from PySide6.QtCore import QObject, Signal, Slot

from .. import discovery as discovery_module
from ..discovery import (
    DEFAULT_BITRATES, DEFAULT_SCAN_DURATION, DEFAULT_SETTLE_SECONDS, DiscoveryResult,
    DiscoveryThresholds, ScanResult, ScoringConfig,
)


class SocketCanDiscoveryWorker(QObject):
    progressChanged = Signal(object)   # DiscoveryProgress
    resultReady = Signal(object)       # DiscoveryResult
    errorOccurred = Signal(str)
    finished = Signal()

    def __init__(
        self,
        interface: str,
        candidates: Iterable[int] = DEFAULT_BITRATES,
        thresholds: DiscoveryThresholds = DiscoveryThresholds(),
        discoverer: Optional[Callable[..., DiscoveryResult]] = None,
    ):
        super().__init__()
        self.interface = interface
        self.candidates = tuple(candidates)
        self.thresholds = thresholds
        # Resolved lazily in run(), not bound here: a plain default-argument
        # value is captured once at class-definition time, which would make
        # `discoverer=discover_socketcan_bitrate` permanently reference the
        # function object that existed at import time -- invisible to a test
        # that patches cansniff.discovery.discover_socketcan_bitrate (or this
        # module's re-export of it) afterwards. Looking it up by name inside
        # run() instead reads whatever the module attribute currently is.
        self._discoverer = discoverer
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    @Slot()
    def run(self) -> None:
        discoverer = self._discoverer or discovery_module.discover_socketcan_bitrate
        try:
            result = discoverer(
                self.interface,
                candidates=self.candidates,
                thresholds=self.thresholds,
                cancel_event=self.cancel_event,
                progress=self.progressChanged.emit,
            )
            self.resultReady.emit(result)
        except Exception as exc:
            self.errorOccurred.emit(str(exc))
        finally:
            self.finished.emit()


class BitrateScanWorker(QObject):
    """Qt worker for the numerically-scored Auto Scan popup -- the same
    QThread-marshaling role ``SocketCanDiscoveryWorker`` plays for the
    older, auto-selecting engine, wrapping ``cansniff.discovery.scan.
    scan_bitrate_candidates`` instead of ``discover_socketcan_bitrate``.
    Owned and torn down by MainWindow -- see ``_start_scan``/
    ``_teardown_scan_thread``.
    """

    progressChanged = Signal(object)   # ScanProgress
    resultReady = Signal(object)       # ScanResult
    errorOccurred = Signal(str)
    finished = Signal()

    def __init__(
        self,
        interface: str,
        candidates: Iterable[int],
        duration: float = DEFAULT_SCAN_DURATION,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        scoring_config: ScoringConfig = ScoringConfig(),
        scanner: Optional[Callable[..., ScanResult]] = None,
    ):
        super().__init__()
        self.interface = interface
        self.candidates = tuple(candidates)
        self.duration = duration
        self.settle_seconds = settle_seconds
        self.scoring_config = scoring_config
        # Resolved lazily in run(), same reasoning as SocketCanDiscoveryWorker
        # above: a plain default-argument value is bound once at class
        # definition time and would be invisible to a test that patches
        # cansniff.discovery.scan_bitrate_candidates afterwards.
        self._scanner = scanner
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    @Slot()
    def run(self) -> None:
        scanner = self._scanner or discovery_module.scan_bitrate_candidates
        try:
            result = scanner(
                self.interface,
                candidates=self.candidates,
                duration=self.duration,
                settle_seconds=self.settle_seconds,
                scoring_config=self.scoring_config,
                cancel_event=self.cancel_event,
                progress=self.progressChanged.emit,
            )
            self.resultReady.emit(result)
        except Exception as exc:
            self.errorOccurred.emit(str(exc))
        finally:
            self.finished.emit()


__all__ = ["BitrateScanWorker", "SocketCanDiscoveryWorker"]
