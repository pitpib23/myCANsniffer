"""Capture export.

CSV and JSONL are written in-house — they are this project's own formats and
the writers already existed. ASC and candump go through python-can's writers
rather than being hand-formatted: both formats have edge cases around extended
identifiers, CAN FD and timestamp bases that are not worth re-deriving, and the
library that already reads them is the one most likely to agree with itself on
a round trip.

Nothing here transmits. A writer is a file, and ``python-can``'s writers take a
``Message`` object and format it — no bus is created, opened or referenced.
"""

from __future__ import annotations

import csv
import json
import os
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .model import CanFrame


class ExportError(RuntimeError):
    """Raised when a capture cannot be written."""


class FormatSpec(object):
    """One export format and what it can carry."""

    __slots__ = ("key", "label", "extension", "importable", "loses")

    def __init__(self, key: str, label: str, extension: str,
                 importable: bool, loses: Sequence[str] = ()):
        self.key = key
        self.label = label
        self.extension = extension
        #: Whether this application can read the format back in.
        self.importable = importable
        #: Metadata the format cannot represent. Surfaced, never silently lost.
        self.loses = tuple(loses)

    @property
    def filter(self) -> str:
        return "{} (*{})".format(self.label, self.extension)


FORMATS: Dict[str, FormatSpec] = {
    "csv": FormatSpec("csv", "Comma-separated values", ".csv", False),
    "jsonl": FormatSpec("jsonl", "JSON lines", ".jsonl", False),
    "asc": FormatSpec(
        "asc", "Vector ASC", ".asc", True,
        loses=("error-frame detail beyond the marker", "unknown CAN FD ESI state"),
    ),
    "candump": FormatSpec(
        "candump", "candump / can-utils log", ".log", True,
        # candump records an *interface name*, not a bare channel number, so a
        # channel of "1" is written and read back as "can1". That is the
        # format working as designed, not a loss — but it is a rename the
        # operator should not have to discover for themselves.
        loses=("error frames", "remote frames", "channel naming",
               "unknown CAN FD ESI state"),
    ),
}

#: Order shown in a file dialog: the round-trippable ones first.
FORMAT_ORDER = ("asc", "candump", "csv", "jsonl")


def format_for_path(path: str) -> Optional[FormatSpec]:
    """Pick a format from a filename extension."""
    extension = os.path.splitext(path)[1].lower()
    if extension == ".log":
        return FORMATS["candump"]
    for spec in FORMATS.values():
        if spec.extension == extension:
            return spec
    return None


# ---------------------------------------------------------------------------
# python-can bridge
# ---------------------------------------------------------------------------


def _writer_channel(channel: str, adjust: bool):
    """Channel value a python-can writer should be given.

    ``ASCWriter`` assumes interfaces number channels from 0 and adds one before
    writing, because ASC channels are 1-based. This application's channels come
    *from* ASC files and are already 1-based, so a numeric channel is shifted
    down first — otherwise every export walks the channel number up by one and
    a round trip does not return what went in.

    Non-numeric channels ("can0", "vcan0") are passed through: python-can
    leaves those to its own resolution and there is nothing to compensate.
    """
    if not channel:
        return None
    if not adjust:
        return channel
    try:
        return int(channel) - 1
    except (TypeError, ValueError):
        return channel


def _to_message(frame: CanFrame, adjust_channel: bool = False):
    """Build a python-can Message for writing. Never sent anywhere."""
    import can

    return can.Message(
        timestamp=float(frame.timestamp),
        arbitration_id=int(frame.arb_id),
        is_extended_id=bool(frame.is_extended),
        is_remote_frame=bool(frame.is_remote_frame),
        is_error_frame=bool(frame.is_error_frame),
        is_fd=bool(frame.is_fd),
        bitrate_switch=bool(frame.is_bitrate_switch),
        error_state_indicator=bool(frame.is_error_state_indicator),
        channel=_writer_channel(frame.channel, adjust_channel),
        dlc=int(frame.dlc),
        data=bytearray(frame.data),
        check=False,
    )


def _write_with_can(frames: Iterable[CanFrame], path: str, writer_name: str,
                    adjust_channel: bool = False) -> int:
    try:
        import can
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ExportError(
            "python-can is required to write this format. Install the "
            "dependencies with: pip install -r requirements.txt") from exc

    writer_cls = getattr(can.io, writer_name, None)
    if writer_cls is None:  # pragma: no cover - depends on the version
        raise ExportError("This python-can build has no {}".format(writer_name))

    count = 0
    writer = writer_cls(path)
    try:
        for frame in frames:
            writer.on_message_received(_to_message(frame, adjust_channel))
            count += 1
    finally:
        try:
            writer.stop()
        except Exception:
            pass
    return count


def write_asc(frames: Iterable[CanFrame], path: str) -> int:
    return _write_with_can(frames, path, "ASCWriter", adjust_channel=True)


def write_candump(frames: Iterable[CanFrame], path: str) -> int:
    return _write_with_can(frames, path, "CanutilsLogWriter")


# ---------------------------------------------------------------------------
# in-house formats
# ---------------------------------------------------------------------------

CSV_HEADER = ("timestamp", "channel", "id", "extended", "dlc", "fd",
              "brs", "esi", "error", "remote", "data")


def write_csv(frames: Iterable[CanFrame], path: str) -> int:
    count = 0
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for frame in frames:
            writer.writerow([
                "{:.6f}".format(frame.timestamp), frame.channel, frame.id_hex,
                int(frame.is_extended), frame.dlc, int(frame.is_fd),
                int(frame.is_bitrate_switch),
                "" if frame.is_error_state_indicator is None
                else int(frame.is_error_state_indicator),
                int(frame.is_error_frame),
                int(frame.is_remote_frame), frame.data_hex,
            ])
            count += 1
    return count


def write_jsonl(frames: Iterable[CanFrame], path: str) -> int:
    count = 0
    with open(path, "w", encoding="utf-8") as handle:
        for frame in frames:
            handle.write(json.dumps({
                "timestamp": round(frame.timestamp, 6),
                "channel": frame.channel,
                "id": frame.id_hex,
                "extended": frame.is_extended,
                "dlc": frame.dlc,
                "fd": frame.is_fd,
                "brs": frame.is_bitrate_switch,
                "esi": frame.is_error_state_indicator,
                "error": frame.is_error_frame,
                "remote": frame.is_remote_frame,
                "data": frame.data_hex,
            }) + "\n")
            count += 1
    return count


_WRITERS: Dict[str, Callable[[Iterable[CanFrame], str], int]] = {
    "csv": write_csv,
    "jsonl": write_jsonl,
    "asc": write_asc,
    "candump": write_candump,
}


def export(frames: Sequence[CanFrame], path: str,
           format_key: Optional[str] = None) -> Tuple[int, FormatSpec]:
    """Write ``frames`` to ``path``. Returns (frames written, format used).

    Raises ExportError with a readable reason — an unwritable destination is a
    normal thing to hit and must not surface as a traceback.
    """
    spec = FORMATS.get(format_key or "") or format_for_path(path)
    if spec is None:
        raise ExportError(
            "Cannot tell which format {!r} should be. Use one of: {}.".format(
                os.path.basename(path),
                ", ".join(FORMATS[k].extension for k in FORMAT_ORDER)))

    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise ExportError("Cannot create {}: {}".format(directory, exc)) from exc

    try:
        count = _WRITERS[spec.key](frames, path)
    except ExportError:
        raise
    except OSError as exc:
        raise ExportError("Could not write {}: {}".format(
            os.path.basename(path), exc)) from exc
    except Exception as exc:
        raise ExportError("Export failed: {}".format(exc)) from exc
    return count, spec


def describe_losses(spec: FormatSpec, frames: Sequence[CanFrame]) -> str:
    """What this export will not carry, given what is actually in the frames.

    Only mentions a limitation the capture actually runs into — warning about
    error frames in a capture that has none is noise.
    """
    if not spec.loses:
        return ""
    notes: List[str] = []
    if "error frames" in spec.loses and any(f.is_error_frame for f in frames):
        notes.append("error frames")
    if "remote frames" in spec.loses and any(f.is_remote_frame for f in frames):
        notes.append("remote frames")
    if "unknown CAN FD ESI state" in spec.loses and any(
            f.is_fd and f.is_error_state_indicator is None for f in frames):
        notes.append("unknown CAN FD ESI state")
    message = ""
    if notes:
        message = ("{} cannot represent {}; those frames will not survive a "
                   "re-import.".format(spec.label, " or ".join(notes)))

    if "channel naming" in spec.loses:
        numeric = sorted({f.channel for f in frames
                          if f.channel and f.channel.isdigit()})
        if numeric:
            rename = ("Channels are written as interface names: {}."
                      .format(", ".join("{} becomes can{}".format(c, c)
                                        for c in numeric[:3])))
            message = (message + " " + rename).strip() if message else rename
    return message
