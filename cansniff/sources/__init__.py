"""Receive-only CAN frame sources.

Every source exposes exactly three operations — ``open``, ``receive`` and
``close``. There is deliberately no transmit entry point anywhere in this
package, and no source ever hands a raw bus/driver handle to callers.
"""

from __future__ import annotations

import abc
from typing import Optional

from ..model import CanFrame


class SourceError(RuntimeError):
    """Raised when a source cannot be opened or read safely."""


class CanFrameSource(abc.ABC):
    """Narrow receive-only abstraction over a stream of CAN frames."""

    #: Short human-readable name shown in the UI status bar.
    name: str = "source"

    @abc.abstractmethod
    def open(self) -> None:
        """Prepare the source. Must fail loudly rather than degrade."""

    @abc.abstractmethod
    def receive(self, timeout: float = 0.1) -> Optional[CanFrame]:
        """Return the next frame, or ``None`` if none arrived within timeout."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release the source. Safe to call more than once."""

    def describe(self) -> str:
        return self.name

    @property
    def exhausted(self) -> bool:
        """True when no further frames can ever arrive (finite sources)."""
        return False


def build_source(config) -> CanFrameSource:
    """Instantiate the source selected in the configuration."""
    from .file_source import FileSource
    from .live import LiveSource

    kind = str(config.get("source.type", "file")).lower()
    if kind == "file":
        return FileSource(
            path=config.get("source.file.path", "baseline.asc"),
            speed=float(config.get("source.file.speed", 0.0) or 0.0),
            loop=bool(config.get("source.file.loop", False)),
        )
    if kind == "live":
        return LiveSource(config.get("source.live", {}) or {})
    raise SourceError("Unknown source type: {!r} (expected 'file' or 'live')".format(kind))
