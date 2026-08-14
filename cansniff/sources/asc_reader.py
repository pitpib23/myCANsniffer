"""Minimal Vector ASC parser.

Written in-house so that offline playback works predictably and without
depending on any library that also knows how to transmit. Lines that cannot
be parsed with confidence are counted and skipped rather than guessed at.
"""

from __future__ import annotations

import re
from typing import Iterator, List, Optional, Tuple

from ..model import CanFrame

_SKIP_PREFIXES = (
    "//", "date", "base", "no internal events", "begin triggerblock",
    "end triggerblock", "internal events logged", "measurement",
)
_SKIP_TOKENS = {"statistic:", "j1939tp", "previous", "start", "end"}
_TIMESTAMP_RE = re.compile(r"^\s*\d+(\.\d+)?\s")


class AscParseResult:
    def __init__(self) -> None:
        self.frames: List[CanFrame] = []
        self.skipped: int = 0
        self.base: str = "hex"


def _parse_id(token: str, base: str) -> Tuple[int, bool]:
    """Return (id, is_extended). A trailing 'x' marks an extended identifier."""
    extended = token.endswith("x") or token.endswith("X")
    if extended:
        token = token[:-1]
    return int(token, 16 if base == "hex" else 10), extended


def _parse_classic(tokens: List[str], base: str, raw_line: str) -> Optional[CanFrame]:
    # <time> <channel> <id> <dir> <d|r> <dlc> <bytes...>
    if len(tokens) < 3:
        return None
    timestamp = float(tokens[0])
    channel = tokens[1]

    # Error frames carry no ID or payload, so they are recognised before the
    # regular field-count check.
    if "errorframe" in raw_line.lower():
        return CanFrame(
            timestamp=timestamp, arb_id=0, data=b"", dlc=0,
            is_error_frame=True, channel=channel, raw_line=raw_line,
        )

    if len(tokens) < 5:
        return None

    try:
        arb_id, extended = _parse_id(tokens[2], base)
    except ValueError:
        return None

    kind = tokens[4].lower()
    if kind.startswith("r"):
        # Remote frame: observed on the bus, carries no payload.
        dlc = int(tokens[5], 16 if base == "hex" else 10) if len(tokens) > 5 else 0
        return CanFrame(
            timestamp=timestamp, arb_id=arb_id, data=b"", dlc=dlc,
            is_extended=extended, is_remote_frame=True,
            channel=channel, raw_line=raw_line,
        )
    if not kind.startswith("d"):
        return None

    if len(tokens) < 6:
        return None
    dlc = int(tokens[5], 16 if base == "hex" else 10)
    payload = bytes(int(t, 16) for t in tokens[6:6 + dlc])
    return CanFrame(
        timestamp=timestamp, arb_id=arb_id, data=payload, dlc=dlc,
        is_extended=extended, channel=channel, raw_line=raw_line,
    )


def _parse_fd(tokens: List[str], base: str, raw_line: str) -> Optional[CanFrame]:
    # <time> CANFD <channel> <dir> <id> <name> <brs> <esi> <dlc> <len> <bytes...>
    if len(tokens) < 11:
        return None
    timestamp = float(tokens[0])
    channel = tokens[2]
    try:
        arb_id, extended = _parse_id(tokens[4], base)
    except ValueError:
        return None
    try:
        brs = tokens[6] == "1"
        esi = tokens[7] == "1"
        length = int(tokens[9], 10)
    except ValueError:
        return None
    payload = bytes(int(t, 16) for t in tokens[10:10 + length])
    if len(payload) != length:
        return None
    # The ASC FD line carries both the 0-15 DLC code (token 8) and the byte
    # count (token 9). ``CanFrame.dlc`` is the byte count everywhere else in
    # the project — that is python-can's convention, and it is what the live
    # source already stores — so the code is deliberately not kept. Storing it
    # here made the same 64-byte frame report dlc 15 from a file and 64 from
    # hardware, which lit the "DLC" anomaly chip on every FD frame ever loaded
    # and made the capture filters' byte-range bounds mean two different things.
    return CanFrame(
        timestamp=timestamp, arb_id=arb_id, data=payload, dlc=length,
        is_extended=extended, is_fd=True, is_bitrate_switch=brs,
        is_error_frame=esi, channel=channel, raw_line=raw_line,
    )


def iter_asc(path: str) -> Iterator[CanFrame]:
    result = parse_asc(path)
    for frame in result.frames:
        yield frame


def parse_asc(path: str) -> AscParseResult:
    result = AscParseResult()
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\r\n")
            stripped = line.strip()
            if not stripped:
                continue

            lowered = stripped.lower()
            if lowered.startswith("base"):
                result.base = "dec" if " dec" in lowered else "hex"
                continue
            if lowered.startswith(_SKIP_PREFIXES):
                continue
            if not _TIMESTAMP_RE.match(line):
                continue

            tokens = stripped.split()
            if len(tokens) > 1 and tokens[1].lower() in _SKIP_TOKENS:
                continue

            try:
                if len(tokens) > 1 and tokens[1].upper() == "CANFD":
                    frame = _parse_fd(tokens, result.base, line)
                else:
                    frame = _parse_classic(tokens, result.base, line)
            except (ValueError, IndexError):
                frame = None

            if frame is None:
                result.skipped += 1
            else:
                result.frames.append(frame)
    return result
