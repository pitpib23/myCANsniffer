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
from ..discovery import DEFAULT_BITRATES, DiscoveryResult, DiscoveryThresholds


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


__all__ = ["SocketCanDiscoveryWorker"]
