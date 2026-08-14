"""Payload interpretation.

Only the *data* bytes of a frame reach this module — no arbitration ID, no
DLC, no CRC, no stuffing. The payload is split into words (2 bytes by
default) and every enabled decoder is applied to every word, so the operator
can compare all byte orders and interpretations side by side and decide which
one is the real signal.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

NA = "—"


@dataclass(frozen=True)
class Word:
    """A slice of the payload that decoders are applied to.

    A word is identified by where it starts and how long it is. Which split
    produced it is not recorded: the two splits are merged and de-duplicated,
    so ``(offset, length)`` already names exactly one block.
    """

    offset: int     # index of the first byte inside the payload
    raw: bytes

    @property
    def length(self) -> int:
        return len(self.raw)

    @property
    def first_index(self) -> int:
        """Index of the first payload byte in this block."""
        return self.offset

    @property
    def last_index(self) -> int:
        """Index of the last payload byte in this block (inclusive)."""
        return self.offset + self.length - 1

    @property
    def indices(self) -> List[int]:
        """Every payload byte index this block covers."""
        return list(range(self.first_index, self.last_index + 1))

    @property
    def span(self) -> str:
        """Inclusive, user-facing byte range, e.g. ``3-6`` or ``5``.

        Deliberately not Python slice notation. The payload strip labels its
        cells with inclusive indices 0..n-1, so a half-open ``[3:7]`` label
        names byte 7 — which the block does not contain — and ``[4:8]`` on an
        8-byte payload names a byte that does not exist at all.
        """
        if self.length == 1:
            return str(self.first_index)
        return "{}-{}".format(self.first_index, self.last_index)

    @property
    def hex(self) -> str:
        return " ".join("{:02X}".format(b) for b in self.raw)


# --------------------------------------------------------------------------
# splitting
# --------------------------------------------------------------------------


def split_fixed(data: bytes, word_size: int = 2, include_remainder: bool = True) -> List[Word]:
    """Non-overlapping words: [0:2], [2:4], [4:6] ..."""
    if word_size < 1:
        raise ValueError("word_size must be >= 1")
    words: List[Word] = []
    for offset in range(0, len(data), word_size):
        chunk = data[offset:offset + word_size]
        if len(chunk) < word_size and not include_remainder:
            break
        words.append(Word(offset, chunk))
    return words


def split_sliding(data: bytes, word_size: int = 2, step: int = 1) -> List[Word]:
    """Overlapping words at every offset: [0:2], [1:3], [2:4] ...

    Catches signals that are not aligned to the fixed word grid.
    """
    if word_size < 1:
        raise ValueError("word_size must be >= 1")
    if step < 1:
        raise ValueError("step must be >= 1")
    words: List[Word] = []
    last_start = len(data) - word_size
    for offset in range(0, last_start + 1, step):
        words.append(Word(offset, data[offset:offset + word_size]))
    return words


def split_payload(
    data: bytes,
    word_size: int = 2,
    sliding_step: int = 1,
    include_remainder: bool = True,
    byte_range: Optional[Sequence[int]] = None,
) -> List[Word]:
    """Apply both splits, honouring the byte-range restriction.

    The aligned and sliding splits are always merged: an operator hunting for
    an unknown signal has no way to know in advance whether it sits on the
    word grid, so offering one without the other only hides candidate blocks.

    Offsets in the returned words are absolute within the original payload,
    so they stay comparable when the range changes.
    """
    start = 0
    end = len(data)
    if byte_range and len(byte_range) == 2:
        start = max(0, int(byte_range[0]))
        end = min(len(data), int(byte_range[1]))
    if start >= end:
        return []

    region = data[start:end]
    words: List[Word] = split_fixed(region, word_size, include_remainder)
    existing = {(w.offset, w.length) for w in words}
    for word in split_sliding(region, word_size, sliding_step):
        # A sliding word identical to an aligned one is not worth a second row.
        if (word.offset, word.length) not in existing:
            words.append(word)

    if start:
        words = [Word(w.offset + start, w.raw) for w in words]
    words.sort(key=lambda w: (w.offset, w.length))
    return words


# --------------------------------------------------------------------------
# decoders
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Decoder:
    key: str
    label: str
    text: Callable[[bytes], str]
    number: Optional[Callable[[bytes], float]] = None
    exact_len: Optional[int] = None     # None = any length
    description: str = ""
    #: Compact header used in the table; the full label names it elsewhere.
    #: Presentation only — it has no effect on decoding.
    short: str = ""

    @property
    def header(self) -> str:
        return self.short or self.label

    @property
    def numeric(self) -> bool:
        """Whether a scale/offset rule can use this decoder.

        A rule multiplies a number; a decoder that only produces text (byte
        pairs, ASCII) has nothing to multiply. Rules must not offer those, or
        they save happily and then produce nothing at all.
        """
        return self.number is not None

    def applies(self, raw: bytes) -> bool:
        if not raw:
            return False
        return self.exact_len is None or len(raw) == self.exact_len

    def render(self, raw: bytes) -> str:
        if not self.applies(raw):
            return NA
        try:
            return self.text(raw)
        except Exception:  # a decoder must never break the capture view
            return NA

    def value(self, raw: bytes) -> Optional[float]:
        if self.number is None or not self.applies(raw):
            return None
        try:
            return self.number(raw)
        except Exception:
            return None


def _hex_be(raw: bytes) -> str:
    return "0x" + raw.hex().upper()


def _hex_le(raw: bytes) -> str:
    return "0x" + raw[::-1].hex().upper()


def _u8_pair(raw: bytes) -> str:
    return " / ".join(str(b) for b in raw)


def _i8_pair(raw: bytes) -> str:
    return " / ".join(str(b - 256 if b > 127 else b) for b in raw)


def _ascii(raw: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in raw)


def _bcd(raw: bytes) -> str:
    digits = []
    for b in raw:
        hi, lo = b >> 4, b & 0x0F
        if hi > 9 or lo > 9:
            return NA          # not valid packed BCD
        digits.append("{}{}".format(hi, lo))
    return "".join(digits)


def _bcd_value(raw: bytes) -> float:
    text = _bcd(raw)
    if text == NA:
        raise ValueError("not BCD")
    return float(text)


def _int_decoder(key: str, label: str, size: int, signed: bool, big: bool) -> Decoder:
    order = "big" if big else "little"

    def number(raw: bytes) -> float:
        return float(int.from_bytes(raw, order, signed=signed))

    return Decoder(
        key=key,
        label=label,
        text=lambda raw: str(int.from_bytes(raw, order, signed=signed)),
        number=number,
        exact_len=size,
        description="{}-bit {} integer, {}-endian".format(
            size * 8, "signed" if signed else "unsigned", order
        ),
        short="{}{} {}".format("u" if not signed else "i", size * 8,
                               "BE" if big else "LE"),
    )


def _byte_decoder(key: str, label: str, signed: bool) -> Decoder:
    """A single byte. Endianness is meaningless at this width, so there is one
    of each rather than a BE/LE pair."""

    def number(raw: bytes) -> float:
        return float(int.from_bytes(raw, "big", signed=signed))

    return Decoder(
        key=key,
        label=label,
        text=lambda raw: str(int.from_bytes(raw, "big", signed=signed)),
        number=number,
        exact_len=1,
        description="8-bit {} integer".format("signed" if signed else "unsigned"),
        short="i8" if signed else "u8",
    )


def _float_decoder(key: str, label: str, big: bool, size: int = 4) -> Decoder:
    fmt = (">" if big else "<") + ("f" if size == 4 else "d")
    precision = 6 if size == 4 else 15

    def number(raw: bytes) -> float:
        return float(struct.unpack(fmt, raw)[0])

    return Decoder(
        key=key,
        label=label,
        text=lambda raw: "{:.{p}g}".format(struct.unpack(fmt, raw)[0], p=precision),
        number=number,
        exact_len=size,
        description="IEEE-754 {} precision, {}-endian".format(
            "single" if size == 4 else "double", "big" if big else "little"
        ),
        short="f{} {}".format(size * 8, "BE" if big else "LE"),
    )


def _hex_decoder(key: str, label: str, big: bool) -> Decoder:
    """Hex text that also carries a value, at whatever width the block is.

    This is the only decoder that works at *any* width, which makes it the
    one way to read a 3-, 5-, 6- or 7-byte field — none of which has a
    fixed-width decoder of its own.
    """
    order = "big" if big else "little"
    return Decoder(
        key=key,
        label=label,
        text=_hex_be if big else _hex_le,
        number=lambda raw: float(int.from_bytes(raw, order)),
        description="Bytes {}; unsigned value at any width".format(
            "in received order" if big else "reversed"
        ),
    )


DECODERS: Dict[str, Decoder] = {}


def _register(decoder: Decoder) -> None:
    DECODERS[decoder.key] = decoder


_register(_hex_decoder("hex_be", "Hex (BE)", True))
_register(_hex_decoder("hex_le", "Hex (LE)", False))
_register(_byte_decoder("u8", "uint8", False))
_register(_byte_decoder("i8", "int8", True))
_register(_int_decoder("u16_be", "uint16 BE", 2, False, True))
_register(_int_decoder("u16_le", "uint16 LE", 2, False, False))
_register(_int_decoder("i16_be", "int16 BE", 2, True, True))
_register(_int_decoder("i16_le", "int16 LE", 2, True, False))
_register(Decoder("u8_pair", "uint8 a/b", _u8_pair, description="Each byte unsigned"))
_register(Decoder("i8_pair", "int8 a/b", _i8_pair, description="Each byte signed"))
_register(_int_decoder("u32_be", "uint32 BE", 4, False, True))
_register(_int_decoder("u32_le", "uint32 LE", 4, False, False))
_register(_int_decoder("i32_be", "int32 BE", 4, True, True))
_register(_int_decoder("i32_le", "int32 LE", 4, True, False))
_register(_float_decoder("f32_be", "float32 BE", True))
_register(_float_decoder("f32_le", "float32 LE", False))
_register(_int_decoder("u64_be", "uint64 BE", 8, False, True))
_register(_int_decoder("u64_le", "uint64 LE", 8, False, False))
_register(_int_decoder("i64_be", "int64 BE", 8, True, True))
_register(_int_decoder("i64_le", "int64 LE", 8, True, False))
_register(_float_decoder("f64_be", "float64 BE", True, 8))
_register(_float_decoder("f64_le", "float64 LE", False, 8))
_register(Decoder("ascii", "ASCII", _ascii, description="Printable characters, '.' otherwise"))
_register(Decoder("bcd", "BCD", _bcd, number=_bcd_value, description="Packed binary-coded decimal"))


def decoder_keys() -> List[str]:
    return list(DECODERS.keys())


def numeric_decoder_keys() -> List[str]:
    """Decoders a scale/offset rule can actually use."""
    return [key for key, decoder in DECODERS.items() if decoder.numeric]


def get_decoder(key: str) -> Optional[Decoder]:
    return DECODERS.get(key)


# --------------------------------------------------------------------------
# scale / offset rules
# --------------------------------------------------------------------------


@dataclass
class SignalRule:
    """User-defined physical value: raw * scale + add."""

    name: str = "signal"
    enabled: bool = True
    can_id: Optional[int] = None      # None = any ID
    channel: str = ""                 # "" = any channel
    offset: int = 0
    length: int = 2
    decoder: str = "u16_be"
    scale: float = 1.0
    add: float = 0.0
    unit: str = ""
    precision: int = 3

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SignalRule":
        can_id = raw.get("can_id", None)
        if isinstance(can_id, str):
            can_id = can_id.strip()
            can_id = int(can_id, 16 if can_id.lower().startswith("0x") else 10) if can_id else None
        return cls(
            name=str(raw.get("name", "signal")),
            enabled=bool(raw.get("enabled", True)),
            can_id=can_id,
            channel=str(raw.get("channel", "")),
            offset=int(raw.get("offset", 0)),
            length=int(raw.get("length", 2)),
            decoder=str(raw.get("decoder", "u16_be")),
            scale=float(raw.get("scale", 1.0)),
            add=float(raw.get("add", 0.0)),
            unit=str(raw.get("unit", "")),
            precision=int(raw.get("precision", 3)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "can_id": None if self.can_id is None else "0x{:X}".format(self.can_id),
            "channel": self.channel,
            "offset": self.offset,
            "length": self.length,
            "decoder": self.decoder,
            "scale": self.scale,
            "add": self.add,
            "unit": self.unit,
            "precision": self.precision,
        }

    @staticmethod
    def parse_all(entries: Optional[Sequence[Any]]) -> List["SignalRule"]:
        """Parse configured rules, dropping any entry that is not usable.

        The configuration is hand-editable by design, so one malformed rule
        must be ignored rather than take the interpretation panel down with
        it — the same tolerance ``Decoder.render`` applies to a bad value.
        """
        rules: List["SignalRule"] = []
        for raw in entries or ():
            if not isinstance(raw, dict):
                continue
            try:
                rules.append(SignalRule.from_dict(raw))
            except Exception:
                continue
        return rules

    def matches(self, arb_id: int, channel: str, word: Word) -> bool:
        if not self.enabled:
            return False
        if self.can_id is not None and self.can_id != arb_id:
            return False
        if self.channel and self.channel != channel:
            return False
        return word.offset == self.offset and word.length == self.length

    def apply(self, raw: bytes) -> Optional[float]:
        decoder = DECODERS.get(self.decoder)
        if decoder is None:
            return None
        base = decoder.value(raw)
        if base is None:
            return None
        return base * self.scale + self.add

    def render(self, raw: bytes) -> Optional[str]:
        value = self.apply(raw)
        if value is None:
            return None
        text = "{:.{p}f}".format(value, p=max(0, self.precision))
        return "{} = {}{}".format(self.name, text, (" " + self.unit) if self.unit else "")


# --------------------------------------------------------------------------
# assembled result
# --------------------------------------------------------------------------


@dataclass
class WordRow:
    word: Word
    values: Dict[str, str]          # decoder key -> rendered text
    physical: str = ""              # matching signal rules, joined


@dataclass
class Interpretation:
    payload: bytes
    words: List[WordRow]
    decoders: List[Decoder]


def interpret_payload(
    data: bytes,
    decoder_keys_enabled: Sequence[str],
    word_size: int = 2,
    sliding_step: int = 1,
    include_remainder: bool = True,
    byte_range: Optional[Sequence[int]] = None,
    signals: Optional[Sequence[SignalRule]] = None,
    arb_id: int = -1,
    channel: str = "",
) -> Interpretation:
    """Run every enabled decoder over every word of the payload."""
    decoders = [DECODERS[k] for k in decoder_keys_enabled if k in DECODERS]
    words = split_payload(
        data,
        word_size=word_size,
        sliding_step=sliding_step,
        include_remainder=include_remainder,
        byte_range=byte_range,
    )

    rows: List[WordRow] = []
    for word in words:
        values = {d.key: d.render(word.raw) for d in decoders}
        physical_parts: List[str] = []
        for rule in signals or ():
            if rule.matches(arb_id, channel, word):
                rendered = rule.render(word.raw)
                if rendered:
                    physical_parts.append(rendered)
        rows.append(WordRow(word=word, values=values, physical="; ".join(physical_parts)))

    return Interpretation(payload=data, words=rows, decoders=decoders)
