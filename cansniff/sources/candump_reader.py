"""Minimal SocketCAN candump (.log) parser.

Written in-house, mirroring the well-documented "candump -L" line syntax
(``(timestamp) interface id#data``) that python-can's own CanutilsLogReader
also implements — but tolerant of trailing fields that reader does not
expect. Some published capture datasets (this module exists because of one
literally named for the attack it records) append a fourth token after
id#data, such as a plain ``0``/``1`` normal/attack label — not the trailing
`` r``/`` t`` rx/tx flag CanutilsLogReader tolerates, which is the *only*
trailing case it handles; anything else makes it raise ``ValueError: too
many values to unpack``, since it splits the whole line and requires
exactly 3 or 4 tokens. Whatever a dataset's own extra token means, nothing
here needs it — it is simply ignored, the same way this application
already treats channel/attack-provenance labels it has no use for.

Lines that cannot be parsed with confidence are counted and skipped rather
than guessed at, the same convention asc_reader.py uses.
"""

from __future__ import annotations

from typing import Iterator, List, Optional

from ..model import CanFrame

#: Bit layout of a SocketCAN arbitration/error ID, matching what candump
#: itself (and python-can's CanutilsLogReader) uses to recognise these.
_CAN_ERR_FLAG = 0x20000000
_CAN_ERR_BUSERROR = 0x00000080
_CAN_ID_MASK = 0x1FFFFFFF

#: FD flag nibble following the second '#' in an FD line's id##flags+data.
_CANFD_BRS = 0x01


class CandumpParseResult:
    def __init__(self) -> None:
        self.frames: List[CanFrame] = []
        self.skipped: int = 0


def _parse_line(stripped: str) -> Optional[CanFrame]:
    tokens = stripped.split()
    if len(tokens) < 3:
        return None
    timestamp_token, channel, frame_token = tokens[0], tokens[1], tokens[2]
    # Any further tokens — an rx/tx flag, a dataset's own attack/normal
    # label, anything else — are deliberately not inspected. This is the
    # one difference from CanutilsLogReader that this module exists for.

    if not (timestamp_token.startswith("(") and timestamp_token.endswith(")")):
        return None
    try:
        timestamp = float(timestamp_token[1:-1])
    except ValueError:
        return None

    if "#" not in frame_token:
        return None
    can_id_string, data = frame_token.split("#", 1)
    if not can_id_string:
        return None
    try:
        can_id = int(can_id_string, 16)
    except ValueError:
        return None
    is_extended = len(can_id_string) > 3

    if can_id & _CAN_ERR_FLAG and can_id & _CAN_ERR_BUSERROR:
        # A genuine SocketCAN error frame carries no payload of its own.
        return CanFrame(
            timestamp=timestamp, arb_id=0, data=b"", dlc=0,
            is_error_frame=True, channel=channel, raw_line=stripped,
        )

    is_fd = False
    brs = False
    if data.startswith("#"):
        # CAN FD: a one-hex-digit flags nibble follows the second '#'
        # (bit 0 is BRS; bit 1 is ESI, which this project's CanFrame has no
        # field for and so is not tracked — see model.py).
        if len(data) < 2:
            return None
        try:
            fd_flags = int(data[1], 16)
        except ValueError:
            return None
        is_fd = True
        brs = bool(fd_flags & _CANFD_BRS)
        data = data[2:]

    if data[:1].lower() == "r":
        # Remote frame: observed on the bus, carries no payload.
        try:
            dlc = int(data[1:]) if len(data) > 1 else 0
        except ValueError:
            return None
        return CanFrame(
            timestamp=timestamp, arb_id=can_id & _CAN_ID_MASK, data=b"", dlc=dlc,
            is_extended=is_extended, is_remote_frame=True, is_fd=is_fd,
            is_bitrate_switch=brs, channel=channel, raw_line=stripped,
        )

    if len(data) % 2 != 0:
        return None
    try:
        payload = bytes(int(data[i:i + 2], 16) for i in range(0, len(data), 2))
    except ValueError:
        return None

    return CanFrame(
        timestamp=timestamp, arb_id=can_id & _CAN_ID_MASK, data=payload,
        dlc=len(payload), is_extended=is_extended, is_fd=is_fd,
        is_bitrate_switch=brs, channel=channel, raw_line=stripped,
    )


def parse_candump(path: str) -> CandumpParseResult:
    result = CandumpParseResult()
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            stripped = raw_line.strip()
            if not stripped:
                continue
            frame = _parse_line(stripped)
            if frame is None:
                result.skipped += 1
            else:
                result.frames.append(frame)
    return result


def iter_candump(path: str) -> Iterator[CanFrame]:
    result = parse_candump(path)
    for frame in result.frames:
        yield frame
