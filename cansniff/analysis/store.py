"""Time-ordered index over observed frames, shared by every analysis layer.

The store holds *references* to the same immutable ``CanFrame`` objects the
tables already show — one pointer per frame, not a copy. That matters: a long
capture retains hundreds of thousands of frames, and giving each analysis
feature its own copy of the payloads would multiply memory by the number of
features.

It is bounded exactly like the trace view, so a capture that runs for hours
cannot grow without limit. Eviction is the sharp edge for anything that caches
derived results, so every mutation bumps ``revision``: a cache that stores the
revision it was computed at can tell, in O(1), whether its inputs still hold.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import OrderedDict, deque
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..model import CanFrame

#: Matches the trace view's default retention so the two stay in step.
DEFAULT_MAX_FRAMES = 200000


class StoreWindow(object):
    """An immutable slice of observed frames plus the identity it came from.

    Analysis functions take one of these rather than a bare list so a cached
    result can be checked against ``revision`` without re-scanning anything.
    """

    __slots__ = ("frames", "revision", "start", "end", "label")

    def __init__(self, frames: Sequence[CanFrame], revision: int,
                 start: Optional[float] = None, end: Optional[float] = None,
                 label: str = ""):
        self.frames = frames
        self.revision = revision
        self.start = start
        self.end = end
        self.label = label

    def __len__(self) -> int:
        return len(self.frames)

    def __iter__(self) -> Iterator[CanFrame]:
        return iter(self.frames)

    def __bool__(self) -> bool:
        return bool(self.frames)

    @property
    def cache_key(self) -> tuple:
        """Identity of the exact sample set this window represents."""
        return (self.revision, self.start, self.end, self.label, len(self.frames))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "StoreWindow({} frames, rev {})".format(len(self.frames), self.revision)


class FrameStore(object):
    """Bounded, time-ordered store of observed frames with a per-ID index.

    Frames are appended in arrival order. ``CanFrame.key`` (channel + id +
    standard/extended) is the grouping identity, which keeps a standard 0x100
    and an extended 0x100 on separate keys — they are different messages.
    """

    def __init__(self, max_frames: int = DEFAULT_MAX_FRAMES):
        self._max_frames = max(100, int(max_frames))
        self._frames: "deque[CanFrame]" = deque()
        # Insertion-ordered so the UI can list IDs in the order first seen.
        self._by_key: "OrderedDict[str, List[CanFrame]]" = OrderedDict()
        self._revision = 0
        self._total_seen = 0
        # Materialising the deque and its timestamps costs O(n); a window diff
        # asks for several slices in a row, so the flattened view is built once
        # per revision instead of once per call.
        self._flat_revision = -1
        self._flat: List[CanFrame] = []
        self._flat_stamps: List[float] = []

    # -- identity -------------------------------------------------------

    @property
    def revision(self) -> int:
        """Bumped on every mutation. Caches key off this to detect staleness."""
        return self._revision

    @property
    def total_seen(self) -> int:
        """Frames ever added, including those since evicted."""
        return self._total_seen

    @property
    def max_frames(self) -> int:
        return self._max_frames

    def set_max_frames(self, value: int) -> None:
        self._max_frames = max(100, int(value))
        self._evict()

    def __len__(self) -> int:
        return len(self._frames)

    # -- ingest ---------------------------------------------------------

    def add(self, frames: Iterable[CanFrame]) -> None:
        added = 0
        for frame in frames:
            self._frames.append(frame)
            bucket = self._by_key.get(frame.key)
            if bucket is None:
                bucket = self._by_key[frame.key] = []
            bucket.append(frame)
            added += 1
        if not added:
            return
        self._total_seen += added
        self._evict()
        self._revision += 1

    def _evict(self) -> None:
        """Drop the oldest frames, and any key that no longer has samples.

        The per-key lists are pruned from the front, which is correct because
        frames arrive in time order within a key.
        """
        overflow = len(self._frames) - self._max_frames
        if overflow <= 0:
            return
        dropped: Dict[str, int] = {}
        for _ in range(overflow):
            frame = self._frames.popleft()
            dropped[frame.key] = dropped.get(frame.key, 0) + 1
        for key, count in dropped.items():
            bucket = self._by_key.get(key)
            if bucket is None:
                continue
            del bucket[:count]
            if not bucket:
                del self._by_key[key]

    def clear(self) -> None:
        self._frames.clear()
        self._by_key.clear()
        self._total_seen = 0
        self._revision += 1

    # -- access ---------------------------------------------------------

    def keys(self) -> List[str]:
        """Message keys in the order they were first observed."""
        return list(self._by_key)

    def frames_for(self, key: str) -> List[CanFrame]:
        return self._by_key.get(key, [])

    def time_span(self) -> Tuple[Optional[float], Optional[float]]:
        if not self._frames:
            return (None, None)
        return (self._frames[0].timestamp, self._frames[-1].timestamp)

    # -- windows --------------------------------------------------------

    def all_frames(self, label: str = "") -> StoreWindow:
        return StoreWindow(list(self._frames), self._revision, label=label)

    def window_for_key(self, key: str, label: str = "") -> StoreWindow:
        return StoreWindow(list(self._by_key.get(key, ())), self._revision,
                           label=label or key)

    def window(self, start: Optional[float] = None, end: Optional[float] = None,
               key: Optional[str] = None, label: str = "") -> StoreWindow:
        """Frames between ``start`` and ``end`` (inclusive), optionally one key.

        Bounds are timestamps as observed. Either may be ``None`` for
        open-ended. A window whose bounds are reversed yields no frames rather
        than silently swapping them — an empty comparison window is a real
        answer, a quietly corrected one is a lie.
        """
        if start is not None and end is not None and end < start:
            return StoreWindow([], self._revision, start, end, label)

        if key is not None:
            frames = self._by_key.get(key, [])
            stamps = None
        else:
            frames, stamps = self._flatten()

        if start is None and end is None:
            selected = list(frames)
        else:
            selected = self._slice(frames, stamps, start, end)
        return StoreWindow(selected, self._revision, start, end, label)

    def _flatten(self) -> Tuple[List[CanFrame], List[float]]:
        """The deque as a list plus its timestamps, rebuilt only on change."""
        if self._flat_revision != self._revision:
            self._flat = list(self._frames)
            self._flat_stamps = [f.timestamp for f in self._flat]
            self._flat_revision = self._revision
        return self._flat, self._flat_stamps

    @staticmethod
    def _slice(frames: List[CanFrame], stamps: Optional[List[float]],
               start: Optional[float], end: Optional[float]) -> List[CanFrame]:
        # Frames are time-ordered within both the global deque and each key
        # bucket, so the bounds are found by bisection rather than a scan.
        if stamps is None:
            stamps = [f.timestamp for f in frames]
        low = 0 if start is None else bisect_left(stamps, start)
        high = len(frames) if end is None else bisect_right(stamps, end)
        return frames[low:high]
