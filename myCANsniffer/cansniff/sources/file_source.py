"""Offline playback of a capture file.

This is *offline playback*: frames are fed into the application's parsing and
display path. Nothing is ever written to a CAN interface. `.asc` files are
handled by the in-house parser; other formats fall back to python-can's
read-only `LogReader`.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from typing import List, Optional

from ..model import CanFrame
from . import CanFrameSource, SourceError
from .asc_reader import parse_asc
from .candump_reader import parse_candump


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
                    is_error_state_indicator=(
                        bool(getattr(message, "error_state_indicator", False))
                        if bool(getattr(message, "is_fd", False)) else None
                    ),
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

    def __init__(self, path: str, speed: float = 0.0, loop: bool = False,
                 resume_from: Optional[float] = None):
        """``resume_from``: continue an existing playback-session timeline
        instead of starting a new one at this capture's own recorded
        timestamps.

        Set this to the highest continuous playback timestamp already in
        retained history (Plot/Trace/Range) when this source is replacing an
        earlier one *for the same session* -- Stop, then Start again, while
        that history is still on screen. ``None`` (the default) starts a
        fresh timeline at the capture's own first recorded timestamp, which
        is correct both for a capture's very first Start and for opening a
        capture while no history survives to stay consistent with: see
        MainWindow.start_capture / clear_views for how the caller decides
        which one applies. This source has no opinion on *why* it was asked
        to resume from a given value -- only that, once asked, its first
        emitted frame continues from there rather than restarting at 0.
        """
        self.path = os.path.abspath(path)
        self.speed = max(0.0, float(speed))
        self.loop = bool(loop)
        self.name = "file:{}".format(os.path.basename(self.path))
        self._resume_from = resume_from

        self._frames: List[CanFrame] = []
        self._index = 0
        self._skipped = 0
        self._opened = False
        self._wall_start = 0.0
        self._first_timestamp = 0.0
        #: Added to every emitted frame's timestamp; see _compute_loop_span
        #: and _initial_loop_offset. Zero for a fresh timeline (no
        #: resume_from), so a capture that never loops and was never asked
        #: to resume emits its recorded timestamps completely unchanged.
        #: Advanced further only by receive()'s own automatic end-of-file
        #: wraparound -- never by pause/resume, which never touches this
        #: source at all -- and reset only by open(), so re-opening the same
        #: file always begins from exactly what resume_from says, never a
        #: stale offset accumulated by a previous instance's own looping.
        self._loop_offset = 0.0
        #: How far the offset advances on each wraparound; see
        #: _compute_loop_span. Fixed once per open() — self._frames does not
        #: change during playback.
        self._loop_span = 0.0

    # -- lifecycle ------------------------------------------------------

    def open(self) -> None:
        if not os.path.exists(self.path):
            raise SourceError("Capture file not found: {}".format(self.path))

        extension = os.path.splitext(self.path)[1].lower()
        if extension == ".asc":
            result = parse_asc(self.path)
            self._frames = result.frames
            self._skipped = result.skipped
        elif extension == ".log":
            # In-house, not python-can's CanutilsLogReader: that reader only
            # tolerates one specific trailing field (an rx/tx " r"/" t"
            # flag) after id#data and hard-fails — ValueError: too many
            # values to unpack — on any other trailing token, which several
            # published capture datasets have (e.g. a plain 0/1 label).
            # See candump_reader.py.
            result = parse_candump(self.path)
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
        self._loop_span = self._compute_loop_span()
        self._loop_offset = self._initial_loop_offset()

    def _average_gap(self) -> float:
        """This capture's own mean inter-frame interval, or 0 for fewer than
        two frames (no recorded interval to derive one from)."""
        count = len(self._frames)
        if count < 2:
            return 0.0
        span = self._frames[-1].timestamp - self._frames[0].timestamp
        return max(0.0, span) / (count - 1)

    def _compute_loop_span(self) -> float:
        """How far every timestamp advances on each automatic loop.

        Chosen so a looped replay reads as one continuous, evenly-paced
        timeline rather than restarting from the recorded t=0 (the bug this
        exists to fix) or duplicating the first frame's timestamp at every
        loop boundary (span alone, with no gap added, would make loop N+1's
        first frame land exactly on loop N's last frame's timestamp).

        The gap carried across the boundary is this capture's own *average*
        inter-frame interval (see _average_gap) -- not a guess or a
        wall-clock measurement -- so the seam matches the capture's own
        typical pacing instead of an arbitrary constant. A capture of fewer
        than two frames has no recorded interval to derive anything from;
        receive() below simply repeats its one frame's timestamp unchanged
        on every loop, which is the same "non-decreasing, not strictly
        increasing" timing every capture's own equal-timestamp frames
        already get.
        """
        if len(self._frames) < 2:
            return 0.0
        span = self._frames[-1].timestamp - self._frames[0].timestamp
        return max(0.0, span + self._average_gap())

    def _initial_loop_offset(self) -> float:
        """The offset the very first emitted frame starts from.

        Zero for a fresh timeline. For a resumed one (see __init__), the
        first frame must continue the same "one capture-average gap past
        the last thing already on screen" boundary receive() applies at
        every automatic loop -- the exact same rule, just applied once at
        construction instead of on every wraparound, so a Stop -> Start
        transition and an automatic EOF loop read as the identical kind of
        seam. ``max(0.0, ...)`` only guards against a caller passing a
        resume point this capture's own first recorded timestamp already
        exceeds (nothing legitimate should do that); it is not how ordinary
        continuation is achieved.
        """
        if self._resume_from is None:
            return 0.0
        target = self._resume_from + self._average_gap()
        return max(0.0, target - self._frames[0].timestamp)

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
            # Advance the continuous timeline by one full loop -- never
            # reset it. self._frames itself, and every frame.timestamp still
            # in it, is untouched; only what gets *added* on the way out
            # changes, so the recorded capture stays intact for provenance
            # (re-export, a later re-open) while what receive() hands
            # onward keeps climbing.
            self._loop_offset += self._loop_span

        frame = self._frames[self._index]

        if self.speed > 0.0:
            # Paced from this frame's own *recorded* timestamp and this
            # loop's own start, exactly as before looping existed -- the
            # continuous offset below is a display/consumption concern, not
            # a pacing one, so speed keeps working the same whether this is
            # loop one or loop fifty.
            due = (frame.timestamp - self._first_timestamp) / self.speed
            elapsed = time.monotonic() - self._wall_start
            if elapsed < due:
                # Not due yet: wait a slice and let the caller poll again.
                time.sleep(min(timeout, due - elapsed))
                return None

        self._index += 1
        if self._loop_offset:
            # CanFrame is frozen (see model.py) -- and deliberately never
            # mutated -- so the continuous timestamp is a new instance, not
            # an edit of the stored one. Skipped entirely whenever the
            # offset is exactly 0.0 -- a fresh timeline (no resume_from)
            # that has not looped yet, the common case -- so a plain,
            # never-looped capture allocates nothing extra and emits its
            # frames exactly as recorded.
            frame = replace(frame, timestamp=frame.timestamp + self._loop_offset)
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
