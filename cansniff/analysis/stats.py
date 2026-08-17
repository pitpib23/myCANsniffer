"""Per-byte value statistics — "Range State".

Factual only: what values each byte position actually took across the observed
frames. Nothing here labels a byte a counter, a sensor or a checksum. The bit
matrix already shows *which bits move*; this shows *what range each byte
occupies*, and the two together are what let an operator draw their own
conclusion.

Absent bytes are never counted as zero. A message whose DLC varies genuinely
has fewer samples at the higher byte positions, and reporting a min of 0 for a
byte that was simply not present would be a fabricated observation.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from ..model import CanFrame
from .store import StoreWindow


class ByteStat(object):
    """Observed behaviour of one byte position."""

    __slots__ = ("index", "minimum", "maximum", "distinct", "samples")

    def __init__(self, index: int, minimum: int, maximum: int,
                 distinct: int, samples: int):
        self.index = index
        self.minimum = minimum
        self.maximum = maximum
        self.distinct = distinct
        self.samples = samples

    @property
    def span(self) -> int:
        """How much of the 0..255 range the byte moved through."""
        return self.maximum - self.minimum

    @property
    def constant(self) -> bool:
        return self.distinct == 1

    def __eq__(self, other) -> bool:
        if not isinstance(other, ByteStat):
            return NotImplemented
        return (self.index, self.minimum, self.maximum, self.distinct,
                self.samples) == (other.index, other.minimum, other.maximum,
                                  other.distinct, other.samples)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ByteStat(byte {}, {}..{}, {} distinct, {} samples)".format(
            self.index, self.minimum, self.maximum, self.distinct, self.samples)


class RangeState(object):
    """Per-byte statistics for one message key over one window of frames."""

    __slots__ = ("key", "frames", "bytes", "lengths", "cache_key")

    def __init__(self, key: str, frames: int, byte_stats: List[ByteStat],
                 lengths: Dict[int, int], cache_key: tuple = ()):
        self.key = key
        self.frames = frames
        self.bytes = byte_stats
        #: payload length -> how many frames had it. More than one entry means
        #: the message's DLC varies, which is why per-byte sample counts differ.
        self.lengths = lengths
        self.cache_key = cache_key

    @property
    def empty(self) -> bool:
        return self.frames == 0

    @property
    def varying_length(self) -> bool:
        return len(self.lengths) > 1

    @property
    def max_length(self) -> int:
        return max(self.lengths) if self.lengths else 0

    def stat_for(self, index: int) -> Optional[ByteStat]:
        for stat in self.bytes:
            if stat.index == index:
                return stat
        return None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "RangeState({}, {} frames, {} bytes)".format(
            self.key, self.frames, len(self.bytes))


def range_state(window: StoreWindow, key: Optional[str] = None) -> RangeState:
    """Compute per-byte statistics over the frames in ``window``.

    ``key`` restricts the calculation to one message; ``None`` uses every frame
    in the window, which only makes sense when the window is already one key.

    Error and remote frames carry no payload, so they contribute to the frame
    count but not to any byte position — they are observations of the message,
    just not of its data.
    """
    # Payloads are bucketed by length first. Within a bucket every frame has a
    # value at every index, so each byte column can be extracted with zip() and
    # reduced with min/max/set — three passes in C rather than a Python loop
    # per byte per frame, which was the difference between a visible stall and
    # an imperceptible one on a high-rate ID.
    buckets: Dict[int, List[bytes]] = {}
    frames = 0
    for frame in window:
        if key is not None and frame.key != key:
            continue
        frames += 1
        data = frame.data
        bucket = buckets.get(len(data))
        if bucket is None:
            bucket = buckets[len(data)] = []
        bucket.append(data)

    lengths = {length: len(payloads) for length, payloads in buckets.items()}
    max_length = max(lengths) if lengths else 0

    minima: Dict[int, int] = {}
    maxima: Dict[int, int] = {}
    seen: Dict[int, set] = {}
    counts: Dict[int, int] = {}

    for length, payloads in buckets.items():
        if not length:
            continue                        # error/remote frames: no data
        for index, column in enumerate(zip(*payloads)):
            low, high = min(column), max(column)
            values = set(column)
            if index in minima:
                minima[index] = min(minima[index], low)
                maxima[index] = max(maxima[index], high)
                seen[index] |= values
                counts[index] += len(column)
            else:
                minima[index] = low
                maxima[index] = high
                # Bounded by construction: a byte holds at most 256 values.
                seen[index] = values
                counts[index] = len(column)

    byte_stats = [
        ByteStat(index, minima[index], maxima[index],
                 len(seen[index]), counts[index])
        for index in sorted(minima)
    ]
    if max_length and not byte_stats:
        lengths = dict(lengths)
    resolved = key or (window.label if window.label else "")
    return RangeState(resolved, frames, byte_stats, lengths, window.cache_key)


class RangeStateCache(object):
    """Memoises Range State per (key, window identity).

    Recomputing over a large window on every panel resize was the failure mode
    to avoid; the window's ``cache_key`` carries the store revision, so an
    eviction or a new batch invalidates entries without any explicit signal.
    """

    def __init__(self, limit: int = 64):
        self._limit = max(1, int(limit))
        self._entries: Dict[tuple, RangeState] = {}
        self._order: List[tuple] = []

    def get(self, window: StoreWindow, key: Optional[str] = None) -> RangeState:
        cache_key = (key,) + window.cache_key
        hit = self._entries.get(cache_key)
        if hit is not None:
            return hit
        result = range_state(window, key)
        self._entries[cache_key] = result
        self._order.append(cache_key)
        while len(self._order) > self._limit:
            del self._entries[self._order.pop(0)]
        return result

    def clear(self) -> None:
        self._entries.clear()
        del self._order[:]
