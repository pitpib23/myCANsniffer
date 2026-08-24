"""Signal values over time, extracted from already-observed frames.

A series is derived, never stored on the frame. Every sample keeps the
timestamp of the frame it came from, so a point on a plot can always be traced
back to the exact frame that produced it.

Two sample sources share this shape:

* a DBC signal, when a database is loaded;
* a raw byte block read with one of the built-in decoders, which is what the
  interpretation table already shows for traffic with no database.

Both produce the same ``Series`` so the plot does not care which it is looking
at, and neither requires a database to exist.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence, Tuple

from ..interpret import DECODERS
from ..model import CanFrame
from .dbc import DbcDatabase
from .store import StoreWindow

#: Ceiling on points handed to a chart. Redraw cost is linear in drawn points
#: — measured at ~11 us each — so 4000 points cost ~46 ms per resize while 2000
#: cost ~25 ms. Since min/max bucketing yields two points per bucket, 2000
#: points covers a 1000-pixel-wide plot at one bucket per pixel column, which
#: is the most a screen can resolve. Drawing more is pure cost.
DISPLAY_LIMIT = 2000

#: Floor, so a narrow panel still shows the shape of the signal.
MIN_DISPLAY_POINTS = 400


class Series(object):
    """Time-ordered samples of one signal, plus what could not be sampled."""

    __slots__ = ("name", "unit", "times", "values", "skipped", "source",
                 "cache_key", "choices")

    def __init__(self, name: str, times: List[float], values: List[float],
                 unit: str = "", skipped: int = 0, source: str = "",
                 cache_key: tuple = (), choices: Optional[List[str]] = None):
        self.name = name
        self.unit = unit
        self.times = times
        self.values = values
        #: Frames that matched but produced no usable value (bad DLC, failed
        #: decode, wrong multiplexor branch). Reported, never silently dropped.
        self.skipped = skipped
        self.source = source
        self.cache_key = cache_key
        #: Enumeration text per sample when the signal is a choice, else None.
        self.choices = choices

    def __len__(self) -> int:
        return len(self.times)

    @property
    def empty(self) -> bool:
        return not self.times

    @property
    def label(self) -> str:
        return "{} [{}]".format(self.name, self.unit) if self.unit else self.name

    @property
    def value_range(self) -> Tuple[float, float]:
        if not self.values:
            return (0.0, 1.0)
        low, high = min(self.values), max(self.values)
        if low == high:
            # A flat series still needs a visible axis; pad it symmetrically.
            pad = abs(low) * 0.05 or 0.5
            return (low - pad, high + pad)
        return (low, high)

    @property
    def time_range(self) -> Tuple[float, float]:
        if not self.times:
            return (0.0, 1.0)
        first, last = self.times[0], self.times[-1]
        return (first, last if last > first else first + 1.0)

    @staticmethod
    def limit_for_width(width: int) -> int:
        """Points worth drawing in a plot this wide.

        One min/max bucket per pixel column is the resolution ceiling; beyond
        it the extra points land on pixels that are already painted.
        """
        return max(MIN_DISPLAY_POINTS, min(DISPLAY_LIMIT, int(width) * 2))

    def decimated(self, limit: int = DISPLAY_LIMIT) -> "Series":
        """A display-sized copy. The full-resolution data stays untouched.

        Uses min/max bucketing rather than plain stride sampling: dropping
        every Nth point hides spikes, and a spike is exactly what an operator
        is looking for. Each bucket contributes its extremes, so the envelope
        of the signal survives.
        """
        count = len(self.times)
        if count <= limit:
            return self
        buckets = max(1, limit // 2)
        step = count / float(buckets)
        times: List[float] = []
        values: List[float] = []
        for index in range(buckets):
            start = int(index * step)
            end = min(count, int((index + 1) * step))
            if end <= start:
                continue
            chunk = self.values[start:end]
            low_at = start + chunk.index(min(chunk))
            high_at = start + chunk.index(max(chunk))
            for position in sorted((low_at, high_at)):
                times.append(self.times[position])
                values.append(self.values[position])
        return Series(self.name, times, values, self.unit, self.skipped,
                      self.source, self.cache_key)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Series({}, {} samples, {} skipped)".format(
            self.name, len(self.times), self.skipped)


def _relative(times: List[float], time_base: Optional[float]) -> List[float]:
    if time_base is None or not times:
        return times
    return [t - time_base for t in times]


def dbc_series(window: StoreWindow, database: DbcDatabase, key: str,
               signal_name: str, time_base: Optional[float] = None) -> Series:
    """Values of one DBC signal across the frames in ``window``.

    Frames that do not decode, or whose multiplexor branch does not include
    this signal, are counted as skipped rather than plotted as zero.
    """
    times: List[float] = []
    values: List[float] = []
    choices: List[str] = []
    unit = ""
    skipped = 0
    has_choice = False

    for frame in window:
        if frame.key != key:
            continue
        decoded = database.decode(frame)
        if not decoded.ok:
            skipped += 1
            continue
        for signal in decoded.signals:
            if signal.name != signal_name:
                continue
            value = signal.value
            if not isinstance(value, (int, float)):
                skipped += 1
                break
            times.append(frame.timestamp)
            values.append(float(value))
            if signal.choice:
                has_choice = True
            choices.append(signal.choice)
            unit = unit or signal.unit
            break
        else:
            skipped += 1                 # not in this multiplexor branch

    return Series(signal_name, _relative(times, time_base), values, unit,
                  skipped, source="dbc", cache_key=(key, signal_name) + window.cache_key,
                  choices=choices if has_choice else None)


def block_series(window: StoreWindow, key: str, offset: int, length: int,
                 decoder_key: str, time_base: Optional[float] = None) -> Series:
    """Values of a raw byte block, decoded with one of the built-in decoders.

    This is the no-database path: it plots exactly what the interpretation
    table already shows for a block, so traffic with no DBC is still
    inspectable over time.
    """
    decoder = DECODERS.get(decoder_key)
    if decoder is None or not decoder.numeric:
        return Series("bytes {}+{}".format(offset, length), [], [],
                      source="raw", cache_key=())

    times: List[float] = []
    values: List[float] = []
    skipped = 0
    end = offset + length

    for frame in window:
        if frame.key != key:
            continue
        data = frame.data
        if len(data) < end:
            skipped += 1                 # block absent from this frame
            continue
        value = decoder.value(data[offset:end])
        if value is None:
            skipped += 1
            continue
        times.append(frame.timestamp)
        values.append(float(value))

    span = str(offset) if length == 1 else "{}-{}".format(offset, end - 1)
    name = "bytes {} · {}".format(span, decoder.header)
    return Series(name, _relative(times, time_base), values, "", skipped,
                  source="raw",
                  cache_key=(key, offset, length, decoder_key) + window.cache_key)


class SeriesCache(object):
    """Memoises extracted series, keyed by window identity plus signal."""

    def __init__(self, limit: int = 32):
        self._limit = max(1, int(limit))
        self._entries = {}
        self._order: List[tuple] = []

    def get(self, builder: Callable[[], Series], cache_key: tuple) -> Series:
        hit = self._entries.get(cache_key)
        if hit is not None:
            return hit
        series = builder()
        self._entries[cache_key] = series
        self._order.append(cache_key)
        while len(self._order) > self._limit:
            del self._entries[self._order.pop(0)]
        return series

    def clear(self) -> None:
        self._entries.clear()
        del self._order[:]
