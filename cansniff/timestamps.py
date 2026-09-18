"""Timestamp formatting at presentation/export boundaries, never at receive.

Unix timestamps denote instants: Trace uses the system's local timezone and
CSV uses explicit UTC. Relative/unknown log times have no invented date/zone.
Microsecond text is a representation of the backend float, not a guarantee
of clock accuracy or resolution. Raw float export retains its full precision.
"""

from datetime import datetime, timezone, tzinfo
from decimal import Decimal
from typing import Optional

from .model import CanFrame


def text_precision(token: str) -> int:
    """Decimal places actually present in a parsed text log timestamp."""
    value = Decimal(token)
    return max(0, -int(value.as_tuple().exponent)) if value.is_finite() else 0


def elapsed_us(timestamp: float, reference: float) -> int:
    # Subtract round-trip decimal values before rounding. Subtracting large
    # epoch floats and then truncating can turn the requested 721 us into 720.
    return int(((Decimal(str(timestamp)) - Decimal(str(reference)))
                * 1_000_000).to_integral_value())


def _precision(frame: CanFrame, lite: bool = False) -> int:
    available = frame.timestamp_precision
    return min(3 if lite else 6, max(0, available) if available is not None else 6)


def _instant(frame: CanFrame) -> Optional[datetime]:
    if frame.timestamp_basis != "unix":
        return None
    try:
        return datetime.fromtimestamp(frame.receive_timestamp, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _fraction(instant: datetime, digits: int) -> str:
    return (".{:06d}".format(instant.microsecond)[:digits + 1]
            if digits else "")


def format_trace_time(frame: CanFrame, lite: bool = False,
                      local_zone: Optional[tzinfo] = None) -> str:
    """Full local date/time or compact time-of-day from the very same frame.

    Milliseconds are truncated from datetime's microsecond representation.
    A known coarse source uses only its recorded decimal places.
    """
    digits = _precision(frame, lite)
    instant = _instant(frame)
    if instant is None:
        # Explicit seconds marker avoids disguising an offset as wall time.
        return "{:.{digits}f} s".format(frame.receive_timestamp, digits=digits)
    local = instant.astimezone(local_zone)
    pattern = "%H:%M:%S" if lite else "%Y-%m-%d %H:%M:%S"
    return local.strftime(pattern) + _fraction(local, digits)


def timestamp_iso(frame: CanFrame) -> str:
    """ISO-8601 UTC when the source has an epoch; blank otherwise."""
    instant = _instant(frame)
    if instant is None:
        return ""
    return (instant.strftime("%Y-%m-%dT%H:%M:%S")
            + _fraction(instant, _precision(frame)) + "+00:00")
