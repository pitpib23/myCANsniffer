"""Receive-side filtering.

Filters only ever decide whether a received frame is kept for display and
logging. They never require, imply, or trigger any transmission.

Evaluation order for a frame:
  1. If any enabled ``block`` rule matches -> drop.
  2. If at least one ``allow`` rule is enabled, the frame must match one of
     them -> otherwise drop.
  3. Otherwise keep.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .model import CanFrame

ALLOW = "allow"
BLOCK = "block"


def parse_int(value: Any) -> Optional[int]:
    """Accept 0x1FF, 1FFh, 511 or None."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    if text.startswith("0x"):
        return int(text[2:], 16)
    if text.endswith("h"):
        return int(text[:-1], 16)
    return int(text, 10)


@dataclass
class BytePattern:
    """Per-byte hex pattern with nibble wildcards, e.g. ``81 ?? 0?``."""

    tokens: List[str]
    offset: int = 0

    @classmethod
    def parse(cls, text: str, offset: int = 0) -> Optional["BytePattern"]:
        text = (text or "").strip()
        if not text:
            return None
        tokens = text.replace(",", " ").split()
        cleaned: List[str] = []
        for token in tokens:
            token = token.lower()
            if len(token) == 1:
                token = "0" + token
            if len(token) != 2 or any(c not in "0123456789abcdef?" for c in token):
                raise ValueError("invalid pattern token: {!r}".format(token))
            cleaned.append(token)
        return cls(cleaned, offset)

    def matches(self, data: bytes) -> bool:
        if self.offset + len(self.tokens) > len(data):
            return False
        for index, token in enumerate(self.tokens):
            byte = data[self.offset + index]
            hi, lo = token[0], token[1]
            if hi != "?" and int(hi, 16) != (byte >> 4):
                return False
            if lo != "?" and int(lo, 16) != (byte & 0x0F):
                return False
        return True


@dataclass
class FilterRule:
    name: str = "rule"
    enabled: bool = True
    mode: str = ALLOW
    id_min: Optional[int] = None
    id_max: Optional[int] = None
    id_mask: Optional[int] = None
    id_value: Optional[int] = None
    channel: str = ""
    dlc_min: Optional[int] = None
    dlc_max: Optional[int] = None
    extended: Optional[bool] = None
    fd: Optional[bool] = None
    data_pattern: str = ""
    data_offset: int = 0

    _pattern: Optional[BytePattern] = None

    def __post_init__(self) -> None:
        self._pattern = BytePattern.parse(self.data_pattern, self.data_offset)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "FilterRule":
        def tri(key: str) -> Optional[bool]:
            value = raw.get(key, None)
            return None if value is None or value == "" else bool(value)

        mode = str(raw.get("mode", ALLOW)).lower()
        if mode not in (ALLOW, BLOCK):
            mode = ALLOW
        return cls(
            name=str(raw.get("name", "rule")),
            enabled=bool(raw.get("enabled", True)),
            mode=mode,
            id_min=parse_int(raw.get("id_min")),
            id_max=parse_int(raw.get("id_max")),
            id_mask=parse_int(raw.get("id_mask")),
            id_value=parse_int(raw.get("id_value")),
            channel=str(raw.get("channel", "")),
            dlc_min=parse_int(raw.get("dlc_min")),
            dlc_max=parse_int(raw.get("dlc_max")),
            extended=tri("extended"),
            fd=tri("fd"),
            data_pattern=str(raw.get("data_pattern", "")),
            data_offset=int(raw.get("data_offset", 0) or 0),
        )

    def to_dict(self) -> Dict[str, Any]:
        def hexed(value: Optional[int]) -> Optional[str]:
            return None if value is None else "0x{:X}".format(value)

        return {
            "name": self.name,
            "enabled": self.enabled,
            "mode": self.mode,
            "id_min": hexed(self.id_min),
            "id_max": hexed(self.id_max),
            "id_mask": hexed(self.id_mask),
            "id_value": hexed(self.id_value),
            "channel": self.channel,
            "dlc_min": self.dlc_min,
            "dlc_max": self.dlc_max,
            "extended": self.extended,
            "fd": self.fd,
            "data_pattern": self.data_pattern,
            "data_offset": self.data_offset,
        }

    def matches(self, frame: CanFrame) -> bool:
        if self.id_min is not None and frame.arb_id < self.id_min:
            return False
        if self.id_max is not None and frame.arb_id > self.id_max:
            return False
        if self.id_mask is not None and self.id_value is not None:
            if (frame.arb_id & self.id_mask) != (self.id_value & self.id_mask):
                return False
        if self.channel and self.channel != frame.channel:
            return False
        if self.dlc_min is not None and frame.dlc < self.dlc_min:
            return False
        if self.dlc_max is not None and frame.dlc > self.dlc_max:
            return False
        if self.extended is not None and self.extended != frame.is_extended:
            return False
        if self.fd is not None and self.fd != frame.is_fd:
            return False
        if self._pattern is not None and not self._pattern.matches(frame.data):
            return False
        return True

    def describe(self) -> str:
        parts: List[str] = []
        if self.id_min is not None or self.id_max is not None:
            low = "0x{:X}".format(self.id_min) if self.id_min is not None else "min"
            high = "0x{:X}".format(self.id_max) if self.id_max is not None else "max"
            parts.append("ID {}..{}".format(low, high))
        if self.id_mask is not None and self.id_value is not None:
            parts.append("ID & 0x{:X} == 0x{:X}".format(self.id_mask, self.id_value))
        if self.channel:
            parts.append("ch {}".format(self.channel))
        if self.dlc_min is not None or self.dlc_max is not None:
            parts.append("DLC {}..{}".format(
                self.dlc_min if self.dlc_min is not None else "min",
                self.dlc_max if self.dlc_max is not None else "max",
            ))
        if self.extended is not None:
            parts.append("extended" if self.extended else "standard")
        if self.fd is not None:
            parts.append("FD" if self.fd else "classic")
        if self.data_pattern:
            parts.append("data@{} ~ {}".format(self.data_offset, self.data_pattern))
        return ", ".join(parts) if parts else "matches everything"


#: Frame-type choices offered by the display filter, in menu order.
FRAME_TYPES = [
    ("any", "Any"),
    ("std", "Standard (11-bit ID)"),
    ("ext", "Extended (29-bit ID)"),
    ("fd", "CAN FD"),
    ("error", "Error frames"),
    ("remote", "Remote frames"),
]


@dataclass
class DisplayFilter:
    """Which already-received frames are shown.

    Distinct from :class:`FilterRule`, which decides what is *received*. This
    one only hides rows, is applied retroactively to everything captured, and
    is undone completely by clearing it.
    """

    text: str = ""                      # matches CAN ID or payload hex
    id_min: Optional[int] = None
    id_max: Optional[int] = None
    channel: str = ""
    frame_type: str = "any"
    len_min: Optional[int] = None
    len_max: Optional[int] = None

    # Derived from ``text``; excluded from equality so two filters with the
    # same text always compare equal.
    _needle: str = field(init=False, repr=False, compare=False, default="")
    _needle_packed: str = field(init=False, repr=False, compare=False, default="")

    def __post_init__(self) -> None:
        needle = (self.text or "").strip().lower()
        self._needle = needle
        # Payload hex is displayed as "4C CC", but people paste and type
        # "4CCC". Matching a space-stripped copy as well means both work,
        # as does a "0x" prefix pasted from the ID column.
        self._needle_packed = needle.replace(" ", "").replace("0x", "")

    #: Field -> label shown on the chip. Order defines chip order.
    LABELS = (
        ("text", "Search"),
        ("id_range", "CAN ID"),
        ("channel", "Channel"),
        ("frame_type", "Frame type"),
        ("len_range", "Payload size"),
    )

    @property
    def is_active(self) -> bool:
        return bool(self.active_chips())

    def matches(self, frame: CanFrame) -> bool:
        if self.id_min is not None and frame.arb_id < self.id_min:
            return False
        if self.id_max is not None and frame.arb_id > self.id_max:
            return False
        if self.channel and frame.channel != self.channel:
            return False

        kind = self.frame_type
        if kind == "std" and frame.is_extended:
            return False
        if kind == "ext" and not frame.is_extended:
            return False
        if kind == "fd" and not frame.is_fd:
            return False
        if kind == "error" and not frame.is_error_frame:
            return False
        if kind == "remote" and not frame.is_remote_frame:
            return False

        size = len(frame.data)
        if self.len_min is not None and size < self.len_min:
            return False
        if self.len_max is not None and size > self.len_max:
            return False

        if self._needle:
            identifier = frame.id_hex.lower()
            payload = frame.data_hex.lower()
            packed = self._needle_packed
            if not (self._needle in identifier
                    or self._needle in payload
                    or (packed and (packed in identifier
                                    or packed in payload.replace(" ", "")))):
                return False
        return True

    # -- chip presentation ----------------------------------------------

    def active_chips(self) -> List[Tuple[str, str, str]]:
        """Active filters as ``(field, name, value)`` for the chip row."""
        chips: List[Tuple[str, str, str]] = []
        if self.text:
            chips.append(("text", "Search", self.text))
        if self.id_min is not None or self.id_max is not None:
            low = "0x{:X}".format(self.id_min) if self.id_min is not None else "any"
            high = "0x{:X}".format(self.id_max) if self.id_max is not None else "any"
            chips.append(("id_range", "CAN ID",
                          low if low == high else "{} to {}".format(low, high)))
        if self.channel:
            chips.append(("channel", "Channel", self.channel))
        if self.frame_type != "any":
            label = dict(FRAME_TYPES).get(self.frame_type, self.frame_type)
            chips.append(("frame_type", "Frame type", label))
        if self.len_min is not None or self.len_max is not None:
            low = str(self.len_min) if self.len_min is not None else "any"
            high = str(self.len_max) if self.len_max is not None else "any"
            chips.append(("len_range", "Payload size",
                          "{} bytes".format(low) if low == high
                          else "{} to {} bytes".format(low, high)))
        return chips

    def cleared_field(self, field: str) -> "DisplayFilter":
        """Copy with one chip's worth of filtering removed."""
        clone = replace(self)
        if field == "text":
            clone.text = ""
        elif field == "id_range":
            clone.id_min = clone.id_max = None
        elif field == "channel":
            clone.channel = ""
        elif field == "frame_type":
            clone.frame_type = "any"
        elif field == "len_range":
            clone.len_min = clone.len_max = None
        return clone


class FilterSet:
    """Compiled set of rules, cheap enough to run in the capture path."""

    def __init__(self, rules: Sequence[FilterRule]):
        self.rules = list(rules)
        self._blocks = [r for r in self.rules if r.enabled and r.mode == BLOCK]
        self._allows = [r for r in self.rules if r.enabled and r.mode == ALLOW]

    @classmethod
    def from_config(cls, raw_rules: Sequence[Dict[str, Any]]) -> "FilterSet":
        rules: List[FilterRule] = []
        for raw in raw_rules or ():
            try:
                rules.append(FilterRule.from_dict(raw))
            except Exception:
                # A malformed rule must not take the capture down; it is skipped.
                continue
        return cls(rules)

    @property
    def active(self) -> bool:
        return bool(self._blocks or self._allows)

    def accepts(self, frame: CanFrame) -> bool:
        for rule in self._blocks:
            if rule.matches(frame):
                return False
        if not self._allows:
            return True
        for rule in self._allows:
            if rule.matches(frame):
                return True
        return False
