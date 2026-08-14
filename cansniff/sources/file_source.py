"""Offline playback of a capture file.

This is *offline playback*: frames are fed into the application's parsing and
display path. Nothing is ever written to a CAN interface. `.asc` files are
handled by the in-house parser; other formats fall back to python-can's
read-only `LogReader`.
"""

from __future__ import annotations

import os
import time
from typing import List, Optional

from ..model import CanFrame
from . import CanFrameSource, SourceError
from .asc_reader import parse_asc


def _load_via_python_can(path: str) -> List[CanFrame]:
    try:
        import can  # noqa: WPS433 (imported lazily; only the reader is used)
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise SourceError(
            "python-can is required to read {!r}. Install it or use an .asc file.".format(
                os.path.basename(path)
            )
        ) from exc

    frames: List[CanFrame] = []
    reader = can.LogReader(path)
    try:
        for message in reader:
            frames.append(
                CanFrame(
                    timestamp=float(message.timestamp or 0.0),
                    arb_id=int(message.arbitration_id),
                    data=bytes(message.data or b""),
                    dlc=int(message.dlc or len(message.data or b"")),
                    is_extended=bool(message.is_extended_id),
                    is_fd=bool(getattr(message, "is_fd", False)),
                    is_bitrate_switch=bool(getattr(message, "bitrate_switch", False)),
                    is_error_frame=bool(message.is_error_frame),
                    is_remote_frame=bool(message.is_remote_frame),
                    channel=str(message.channel if message.channel is not None else ""),
                )
            )
    finally:
        stop = getattr(reader, "stop", None)
        if callable(stop):
            stop()
    return frames


class FileSource(CanFrameSource):
    """Replays a capture file into the receive path at a configurable speed."""

    def __init__(self, path: str, speed: float = 0.0, loop: bool = False):
        self.path = os.path.abspath(path)
        self.speed = max(0.0, float(speed))
        self.loop = bool(loop)
        self.name = "file:{}".format(os.path.basename(self.path))

        self._frames: List[CanFrame] = []
        self._index = 0
        self._skipped = 0
        self._opened = False
        self._wall_start = 0.0
        self._first_timestamp = 0.0

    # -- lifecycle ------------------------------------------------------

    def open(self) -> None:
        if not os.path.exists(self.path):
            raise SourceError("Capture file not found: {}".format(self.path))

        extension = os.path.splitext(self.path)[1].lower()
        if extension == ".asc":
            result = parse_asc(self.path)
            self._frames = result.frames
            self._skipped = result.skipped
        else:
            self._frames = _load_via_python_can(self.path)
            self._skipped = 0

        if not self._frames:
            raise SourceError("No frames could be parsed from {}".format(self.path))

        self._index = 0
        self._first_timestamp = self._frames[0].timestamp
        self._wall_start = time.monotonic()
        self._opened = True

    def close(self) -> None:
        self._opened = False
        self._frames = []
        self._index = 0

    # -- reading --------------------------------------------------------

    def receive(self, timeout: float = 0.1) -> Optional[CanFrame]:
        if not self._opened:
            return None

        if self._index >= len(self._frames):
            if not self.loop:
                return None
            self._index = 0
            self._wall_start = time.monotonic()

        frame = self._frames[self._index]

        if self.speed > 0.0:
            due = (frame.timestamp - self._first_timestamp) / self.speed
            elapsed = time.monotonic() - self._wall_start
            if elapsed < due:
                # Not due yet: wait a slice and let the caller poll again.
                time.sleep(min(timeout, due - elapsed))
                return None

        self._index += 1
        return frame

    # -- introspection --------------------------------------------------

    @property
    def exhausted(self) -> bool:
        return self._opened and not self.loop and self._index >= len(self._frames)

    @property
    def total_frames(self) -> int:
        return len(self._frames)

    @property
    def skipped_lines(self) -> int:
        return self._skipped

    @property
    def progress(self) -> float:
        if not self._frames:
            return 0.0
        return self._index / float(len(self._frames))

    def describe(self) -> str:
        speed = "max speed" if self.speed == 0.0 else "{:g}x".format(self.speed)
        return "{} — {} frames, {}{}".format(
            self.path, len(self._frames), speed, ", looping" if self.loop else ""
        )
