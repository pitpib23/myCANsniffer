"""Passive analysis layers over observed CAN frames.

Everything in this package reads frames that have already been received. No
module here opens a bus, transmits, or holds a driver handle — the only way
frames enter is by being handed in from the capture path.

    raw capture / live observation
              |
        CanFrame (frozen)
              |
          FrameStore            time-ordered index, one pointer per frame
              |
      +-------+--------+-------- ... ------+
      |                |                   |
  byte stats       DBC decode          (later layers)

The raw frame stays the source of truth. Every layer here produces a *separate*
presentation object and never mutates, replaces or hides the frame it came
from.
"""

from __future__ import annotations

from .store import FrameStore, StoreWindow
from .stats import ByteStat, RangeState, range_state

__all__ = [
    "FrameStore",
    "StoreWindow",
    "ByteStat",
    "RangeState",
    "range_state",
]
