"""Immutable representation of a received CAN frame.

Frames are stored exactly as received. Nothing in the application mutates
a CanFrame; every decoding step produces new objects instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

#: Frames the bit-activity window covers. Bounded on purpose: the counts are
#: meant to describe what the bus is doing *now*, and a cumulative count
#: saturates — after a long capture every bit that ever moved looks equally
#: busy. It also caps the memory one ID can hold.
DEFAULT_BIT_WINDOW = 512

#: Set-bit positions for every byte value. Walking a precomputed tuple beats
#: recovering each bit with ``diff & -diff`` on the capture path, which runs
#: once per received frame.
_BIT_POSITIONS = tuple(
    tuple(bit for bit in range(8) if value >> bit & 1) for value in range(256)
)


@dataclass(frozen=True)
class CanFrame:
    """A single received CAN / CAN FD frame.

    Attributes mirror what a driver can tell us at reception time. Fields the
    source cannot supply keep their defaults rather than being guessed.
    """

    timestamp: float
    arb_id: int
    data: bytes
    dlc: int
    is_extended: bool = False
    is_fd: bool = False
    is_bitrate_switch: bool = False
    is_error_frame: bool = False
    is_remote_frame: bool = False
    channel: str = ""
    raw_line: Optional[str] = None  # original text, when read from a log file

    @property
    def id_hex(self) -> str:
        width = 8 if self.is_extended else 3
        return "{:0{w}X}".format(self.arb_id, w=width)

    @property
    def data_hex(self) -> str:
        return " ".join("{:02X}".format(b) for b in self.data)

    @property
    def key(self) -> str:
        """Grouping key for the per-ID view: channel + id + frame format."""
        return "{}:{}:{}".format(self.channel, self.id_hex, "X" if self.is_extended else "S")


@dataclass
class FrameStats:
    """Accumulated statistics for one arbitration ID."""

    key: str
    frame: CanFrame
    count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    changed_mask: bytearray = field(default_factory=bytearray)
    #: Frames the bit-flip counts are averaged over.
    bit_window: int = DEFAULT_BIT_WINDOW
    _prev_data: bytes = b""
    #: Flips per bit, indexed byte * 8 + bit (bit 0 = LSB). Two buckets: the
    #: filling one and the one before it. See _advance_window.
    _current: List[int] = field(default_factory=list, repr=False)
    _previous: List[int] = field(default_factory=list, repr=False)
    _current_n: int = 0
    _previous_n: int = 0

    # -- bit activity ---------------------------------------------------

    @property
    def bit_flips(self) -> List[int]:
        """How often each bit flipped inside the window.

        Indexed ``byte * 8 + bit`` with bit 0 the least significant. This is a
        count of observed transitions and nothing more — it does not claim a
        bit is a counter, a flag or a checksum, only how often it moved.
        """
        if not self._previous_n:
            return list(self._current)
        return [a + b for a, b in zip(self._previous, self._current)]

    @property
    def window_frames(self) -> int:
        """Transitions the flip counts actually cover.

        Between ``bit_window`` and twice it, because the window is retired in
        whole buckets rather than a frame at a time. Reported rather than
        assumed, so the panel can state the real figure.
        """
        return self._previous_n + self._current_n

    def _reset_history(self, size: int) -> None:
        self.changed_mask = bytearray(size)
        # bit_window 0 turns per-bit tracking off. The per-byte changed_mask is
        # unaffected. Worth having: on a saturated CAN FD bus whose every byte
        # changes every frame, counting bits is the most expensive thing the
        # receive path does, and not everyone needs it.
        self._current = [0] * (size * 8) if self.bit_window > 0 else []
        self._previous = []
        self._current_n = 0
        self._previous_n = 0

    def _advance_window(self) -> None:
        """Age the window by one transition.

        Retiring a whole bucket at a time, rather than evicting the oldest
        frame on every new one, is what keeps this affordable: an exact ring
        needs a second per-bit walk per frame, which on churning 64-byte CAN FD
        traffic cost more than the receive path could afford.
        """
        if not self._current:
            return
        self._current_n += 1
        if self._current_n < self.bit_window:
            return
        self._previous = self._current
        self._previous_n = self._current_n
        self._current = [0] * len(self._current)
        self._current_n = 0

    @property
    def rate_hz(self) -> float:
        """Exact average frame rate over everything seen for this ID.

        Deliberately not a smoothed inter-frame period. Plenty of CAN messages
        are bursty — three frames 2 ms apart, then idle for a second — and
        smoothing blends the two gaps into a figure that describes neither.
        Counting over the observed window is both correct and O(1).
        """
        elapsed = self.last_seen - self.first_seen
        if self.count < 2 or elapsed <= 0:
            return 0.0
        return (self.count - 1) / elapsed

    def update(self, frame: CanFrame) -> None:
        data = frame.data
        if self.count == 0 or len(self._prev_data) != len(data):
            # First frame, or the payload changed length: byte and bit indices
            # no longer refer to the same fields, so the history restarts
            # rather than mixing two layouts together.
            if self.count == 0:
                self.first_seen = frame.timestamp
            self._reset_history(len(data))
        elif self._prev_data != data:
            # Only walk the payload when something actually changed; on a
            # steady bus most frames are byte-identical to the previous one.
            # The XOR itself is done as one integer so the per-byte work is
            # limited to the bytes that actually differ.
            size = len(data)
            delta = (int.from_bytes(self._prev_data, "big")
                     ^ int.from_bytes(data, "big")).to_bytes(size, "big")
            mask = self.changed_mask
            counts = self._current
            track_bits = bool(counts)
            for index, diff in enumerate(delta):
                if not diff:
                    continue
                mask[index] = 1
                if track_bits:
                    base = index * 8
                    for bit in _BIT_POSITIONS[diff]:
                        counts[base + bit] += 1
            self._advance_window()
        else:
            # Identical payload: still a transition, and it still ages the
            # window, so a bit that stops moving fades out of the counts.
            self._advance_window()

        self._prev_data = data
        self.last_seen = frame.timestamp
        self.count += 1
        self.frame = frame
