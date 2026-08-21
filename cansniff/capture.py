"""Capture worker.

Runs the receive loop off the UI thread so that slow UI rendering cannot stall
reception. Filtering and synchronous logging remain on the worker thread.
Frames are handed to the UI in batches through a bounded pipeline; when the UI
cannot keep up, the incoming batch is dropped and counted rather than allowed
to grow without limit.
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from typing import List, Optional

from PySide6.QtCore import QObject, Signal, Slot

from .filters import FilterSet
from .model import CanFrame
from .sources import CanFrameSource, SourceError


class FrameLogger:
    """Appends received frames to a CSV or JSONL file."""

    def __init__(self, directory: str, fmt: str = "csv"):
        self.format = fmt.lower() if fmt else "csv"
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        extension = "jsonl" if self.format == "jsonl" else "csv"
        self.path = os.path.join(directory, "capture-{}.{}".format(stamp, extension))
        self._fh = open(self.path, "w", encoding="utf-8", newline="")
        self._writer = None
        if self.format != "jsonl":
            self._writer = csv.writer(self._fh)
            self._writer.writerow(
                ["timestamp", "channel", "id_hex", "extended", "dlc", "fd",
                 "brs", "esi", "error", "data_hex"]
            )

    def write(self, frames: List[CanFrame]) -> None:
        if self._fh.closed:
            return
        if self._writer is not None:
            for frame in frames:
                self._writer.writerow([
                    "{:.6f}".format(frame.timestamp), frame.channel, frame.id_hex,
                    int(frame.is_extended), frame.dlc, int(frame.is_fd),
                    int(frame.is_bitrate_switch),
                    "" if frame.is_error_state_indicator is None
                    else int(frame.is_error_state_indicator),
                    int(frame.is_error_frame), frame.data_hex,
                ])
        else:
            for frame in frames:
                self._fh.write(json.dumps({
                    "timestamp": round(frame.timestamp, 6),
                    "channel": frame.channel,
                    "id": frame.id_hex,
                    "extended": frame.is_extended,
                    "dlc": frame.dlc,
                    "fd": frame.is_fd,
                    "brs": frame.is_bitrate_switch,
                    "esi": frame.is_error_state_indicator,
                    "error": frame.is_error_frame,
                    "data": frame.data_hex,
                }) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()


class CaptureWorker(QObject):
    """Owns the source and pushes received frames to the UI."""

    framesReady = Signal(list)          # List[CanFrame]
    statusChanged = Signal(str)
    errorOccurred = Signal(str)
    sourceFinished = Signal()
    started = Signal(str)               # source description

    def __init__(
        self,
        source: CanFrameSource,
        filter_set: FilterSet,
        refresh_ms: int = 100,
        batch_limit: int = 2000,
        max_pending_batches: int = 8,
        logger: Optional[FrameLogger] = None,
    ):
        super().__init__()
        self._source = source
        self._filters = filter_set
        self._refresh = max(10, int(refresh_ms)) / 1000.0
        self._batch_limit = max(1, int(batch_limit))
        self._max_pending = max(1, int(max_pending_batches))
        self._logger = logger

        self._running = False
        self._stop_requested = threading.Event()
        self._finished = False
        self._paused = False
        self._pending = 0
        # Incremented on this worker's thread and decremented on the UI thread,
        # so the counter itself needs a lock: "-= 1" is not atomic under the GIL.
        self._pending_lock = threading.Lock()

        self.received = 0
        self.accepted = 0
        self.dropped = 0
        #: Frames kept and logged but not shown, because the display was paused.
        self.display_skipped = 0
        #: Source/backend exceptions observed by this worker.  This is an
        #: application-visible source error count; it is not a CAN-controller
        #: hardware overrun counter.
        self.source_errors = 0
        self.logger_failures = 0
        #: One of stopped / end-of-source / error once the worker completes.
        self.completion_reason = ""

    # -- control (called from the UI thread) ----------------------------

    def request_stop(self) -> None:
        self._stop_requested.set()
        self._running = False

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)

    def set_filters(self, filter_set: FilterSet) -> None:
        self._filters = filter_set

    def reset_counters(self) -> None:
        """Zero the capture statistics without disturbing the receive loop.

        Used by Clear, which discards what has been collected so far; leaving
        the counters running would make them describe frames the operator can
        no longer see anywhere. The pipeline depth is deliberately *not* reset:
        it tracks batches genuinely in flight to the UI, and zeroing it would
        let more batches through than the UI has acknowledged.
        """
        self.received = 0
        self.accepted = 0
        self.dropped = 0
        self.display_skipped = 0
        self.source_errors = 0
        self.logger_failures = 0

    @property
    def pending(self) -> int:
        """Batches delivered to the UI but not yet acknowledged."""
        with self._pending_lock:
            return self._pending

    @Slot()
    def batch_consumed(self) -> None:
        """UI acknowledges a delivered batch, freeing pipeline capacity.

        Deliberately safe to call from the UI thread directly. ``run()`` is a
        blocking loop, so this worker's thread never reaches its event loop and
        a *queued* invocation of this slot would never execute — the pipeline
        would fill to ``_max_pending`` and drop every batch from then on.
        """
        with self._pending_lock:
            if self._pending > 0:
                self._pending -= 1

    # -- worker body ----------------------------------------------------

    @Slot()
    def run(self) -> None:
        # Set *before* open(), not after: open() can block for a while (a
        # real device, a slow file), and request_stop() — called from the
        # UI thread — can arrive during that wait. Setting this to True
        # only once open() returns would silently overwrite that stop
        # request back to "running" the instant open() finally completes,
        # and the receive loop below would then run forever, unstoppably:
        # nothing would ever emit sourceFinished, so the UI's Stopping
        # state (which disables Start, Stop, and Pause alike) would never
        # clear and Start would never re-enable. Setting it here first
        # means a stop requested mid-open() simply sticks.
        self._running = not self._stop_requested.is_set()
        if not self._running:
            self._finish()
            return
        try:
            self._source.open()
        except SourceError as exc:
            self.source_errors += 1
            self.completion_reason = "error"
            self.errorOccurred.emit(str(exc))
            self._finish()
            return
        except Exception as exc:  # unexpected driver failure
            self.source_errors += 1
            self.completion_reason = "error"
            self.errorOccurred.emit("Failed to open source: {}".format(exc))
            self._finish()
            return

        # A stop requested while still inside open() above must stick: skip
        # announcing a capture that is already being torn down before it
        # ever received anything. The `while` below then simply does not
        # run, and the existing `finally` handles cleanup and
        # sourceFinished exactly as it would for any other stop.
        if self._running and not self._stop_requested.is_set():
            self.started.emit(self._source.describe())

        batch: List[CanFrame] = []
        last_emit = time.monotonic()

        try:
            while self._running and not self._stop_requested.is_set():
                try:
                    frame = self._source.receive(timeout=0.05)
                except Exception as exc:
                    self.source_errors += 1
                    self.completion_reason = "error"
                    self.errorOccurred.emit("Receive failed: {}".format(exc))
                    break

                now = time.monotonic()

                if frame is not None:
                    self.received += 1
                    # Pause is deliberately NOT applied here. Pause freezes the
                    # display; it must not stop the frame being accepted or
                    # written to the capture log. Discarding here meant a
                    # recording taken while paused had a silent hole in it,
                    # which for a tool whose whole job is observing a bus is
                    # the worst possible way to lose data.
                    if self._filters.accepts(frame):
                        self.accepted += 1
                        batch.append(frame)
                elif self._source.exhausted:
                    if batch:
                        self._emit(batch)
                        batch = []
                    self.completion_reason = "end-of-source"
                    self.statusChanged.emit("End of capture file reached")
                    break

                if batch and (len(batch) >= self._batch_limit or now - last_emit >= self._refresh):
                    self._emit(batch)
                    batch = []
                    last_emit = now
        finally:
            if batch:
                self._emit(batch)
            self._finish()

    def _finish(self) -> None:
        """Close resources and announce completion exactly once."""
        if self._finished:
            return
        self._finished = True
        self._running = False
        if not self.completion_reason:
            self.completion_reason = "stopped"
        try:
            self._source.close()
        except Exception as exc:
            self.source_errors += 1
            self.completion_reason = "error"
            self.errorOccurred.emit("Source close failed: {}".format(exc))
        if self._logger is not None:
            try:
                self._logger.close()
            except Exception as exc:
                self.logger_failures += 1
                self.errorOccurred.emit("Logging close failed: {}".format(exc))
        self.sourceFinished.emit()

    def _emit(self, batch: List[CanFrame]) -> None:
        if self._logger is not None:
            try:
                self._logger.write(batch)
            except Exception as exc:
                self.logger_failures += 1
                self._logger = None
                self.errorOccurred.emit("Logging stopped: {}".format(exc))

        if self._paused:
            # Frozen display: the frames were received, filtered and logged;
            # they are simply not delivered to the views. Buffering them
            # instead would grow without bound for as long as the operator
            # left it paused.
            self.display_skipped += len(batch)
            return

        with self._pending_lock:
            if self._pending >= self._max_pending:
                # UI is behind: drop this batch instead of queueing without
                # bound. This must stay recoverable — see batch_consumed.
                self.dropped += len(batch)
                return
            self._pending += 1
        self.framesReady.emit(batch)
