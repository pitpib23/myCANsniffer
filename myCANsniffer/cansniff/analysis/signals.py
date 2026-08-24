"""The unified Signal Database: one editable model, one decode path.

Before this module, the application had two entirely separate ways to turn
raw bytes into a named physical value: a **DBC**, with signals bound to a real
CAN message and decoded through ``cantools``; and freeform **Scaled Values**
rules matched by byte offset, usable on "any ID" traffic a DBC cannot
describe, feeding a dedicated "Scaled value" column in the Blocks table.

Both are unified here into one ``Signal`` model and one decode path: applying
a profile builds a single ``DbcDatabase`` (see ``build_dbc_database``) that
feeds the Signals workspace tab exactly the way a loaded ``.dbc`` file always
has. There is deliberately no second artifact and no separate "Scaled value"
column anymore — a signal's scale shows up exactly one place, through the
database that is applied, whether that signal came from an imported ``.dbc``
or was typed in by hand.

"Any ID" signals (``can_id is None``) and the legacy ``BCD`` encoding are
still supported for editing and preview — mainly so a migrated Scaled Value
rule is never silently discarded — but neither can be exported to a ``.dbc``,
and neither has a live decode path in the main window beyond the editor's own
preview: DBC has no way to describe an unbound signal, and BCD is not a linear
function of the raw bits.

Design, briefly:

* A signal with a concrete ``can_id`` is grouped with its siblings sharing
  that id into a synthetic DBC message on demand (see ``build_dbc_database``),
  the same way a real ``.dbc``'s messages group signals. "Which message" is
  therefore a *view*, computed from the flat list, not a separate mutable
  structure to keep in sync.
* Bit-precise extraction (start bit, length, byte order, signed, float32/64)
  is done by handing cantools a throwaway single-message ``Database`` built
  just for the call and asking it to decode — reusing the library's own,
  already-correct bit-numbering and scaling math (including the Motorola
  "sawtooth" convention, which is notorious to get right by hand) rather than
  re-deriving it. Nothing here keeps that throwaway database around; a fresh
  one is built per decode.

Nothing here transmits, encodes a frame for sending, or exposes cantools'
frame-construction API — see ``dbc.py`` for the same containment applied to
the decode path this module builds.
"""

from __future__ import annotations

import os
import hashlib
import json
import uuid
from dataclasses import dataclass, field, replace as _replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..model import CanFrame
from .dbc import DbcDatabase
from .definitions import CanopenNodeAssociation, DefinitionReference


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()

#: Byte orders, in the spelling cantools uses.
LITTLE_ENDIAN = "little_endian"
BIG_ENDIAN = "big_endian"
BYTE_ORDERS = (LITTLE_ENDIAN, BIG_ENDIAN)

#: How the raw bits become a number before scale/offset is applied.
INT = "int"
FLOAT32 = "float32"
FLOAT64 = "float64"
BCD = "bcd"
ENCODINGS = (INT, FLOAT32, FLOAT64, BCD)

#: decoder key -> (encoding, byte_order, is_signed, width_bits), for migrating
#: an old Scaled Value rule. Only the decoders that old rule's combo could
#: actually select are listed — see interpret.numeric_decoder_keys().
_LEGACY_DECODER_MAP: Dict[str, Tuple[str, str, bool, int]] = {
    "u8": (INT, LITTLE_ENDIAN, False, 8),
    "i8": (INT, LITTLE_ENDIAN, True, 8),
    "u16_be": (INT, BIG_ENDIAN, False, 16),
    "u16_le": (INT, LITTLE_ENDIAN, False, 16),
    "i16_be": (INT, BIG_ENDIAN, True, 16),
    "i16_le": (INT, LITTLE_ENDIAN, True, 16),
    "u32_be": (INT, BIG_ENDIAN, False, 32),
    "u32_le": (INT, LITTLE_ENDIAN, False, 32),
    "i32_be": (INT, BIG_ENDIAN, True, 32),
    "i32_le": (INT, LITTLE_ENDIAN, True, 32),
    "u64_be": (INT, BIG_ENDIAN, False, 64),
    "u64_le": (INT, LITTLE_ENDIAN, False, 64),
    "i64_be": (INT, BIG_ENDIAN, True, 64),
    "i64_le": (INT, LITTLE_ENDIAN, True, 64),
    "f32_be": (FLOAT32, BIG_ENDIAN, False, 32),
    "f32_le": (FLOAT32, LITTLE_ENDIAN, False, 32),
    "f64_be": (FLOAT64, BIG_ENDIAN, False, 64),
    "f64_le": (FLOAT64, LITTLE_ENDIAN, False, 64),
    # hex_be/hex_le were an unsigned integer of the rule's own byte width,
    # displayed as hex text. The physical value they produced in a
    # scaled-value rule was already plain decimal, not hex — the "hex" was a
    # display choice on top of the same underlying number — so nothing is
    # lost migrating them to a plain unsigned integer of the same width.
    "hex_be": (INT, BIG_ENDIAN, False, 0),   # width filled in from rule.length
    "hex_le": (INT, LITTLE_ENDIAN, False, 0),
    "bcd": (BCD, LITTLE_ENDIAN, False, 0),
}


class SignalError(RuntimeError):
    """Raised when a database file cannot be read, written or represented."""


def _as_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Signal(object):
    """One named value: where to read it, how to scale it, what it means.

    Bit-precise by default (``start``/``length`` in bits, matching DBC), so a
    signal imported from a real ``.dbc`` and one created from scratch share
    the exact same shape and the exact same editor.

    ``can_id is None`` means "any ID" — the old Scaled Values behaviour DBC
    cannot express. ``channel`` is an application-only receive filter with no
    DBC equivalent at all; it narrows matching without affecting export
    (DBC never sees it, so there is nothing to lose on export beyond the
    filtering itself, which is why it is warned about rather than blocking
    export the way "any ID" does).
    """

    name: str = "signal"
    enabled: bool = True
    can_id: Optional[int] = None
    is_extended: bool = False
    channel: str = ""                 # "" = any
    start: int = 0                    # bit, DBC numbering
    length: int = 8                   # bits
    byte_order: str = LITTLE_ENDIAN
    is_signed: bool = False
    encoding: str = INT
    scale: float = 1.0
    offset: float = 0.0
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    unit: str = ""
    decimals: int = 3
    comment: str = ""
    #: Enumeration entries preserved from an imported DBC. Shown, not edited:
    #: reconstructing them from scratch risks silently losing what a real
    #: tool authored, for a control this window has no real need to offer.
    choices: Tuple[Tuple[int, str], ...] = ()
    #: Denormalised message context, present when can_id is not None. Several
    #: signals sharing a can_id are expected to agree; the group's export uses
    #: whichever signal it meets first. Kept in agreement by the UI layer
    #: (Profile.set_message_meta), which edits a message's fields across
    #: every signal in its group at once rather than one at a time.
    message_name: str = ""
    message_length: Optional[int] = None
    message_comment: str = ""
    message_senders: Tuple[str, ...] = ()
    message_cycle_time: Optional[int] = None
    #: CAN FD, at message granularity (cantools' own Message.is_fd). Receive
    #: metadata only — this project never sends, so nothing here selects a
    #: data bitrate or a BRS flag; it only records/exports what a frame's
    #: message declares.
    message_is_fd: bool = False
    #: Provenance of this interpretation. Imported DBC signals are marked by
    #: import_dbc; signals created in the editor remain explicitly MANUAL.
    source_kind: str = "MANUAL"

    def __post_init__(self) -> None:
        self.start = int(self.start)
        self.length = max(1, int(self.length))
        if self.byte_order not in BYTE_ORDERS:
            self.byte_order = LITTLE_ENDIAN
        if self.encoding not in ENCODINGS:
            self.encoding = INT
        if self.encoding == FLOAT32:
            self.length = 32
        elif self.encoding == FLOAT64:
            self.length = 64

    # -- identity ---------------------------------------------------------

    @property
    def id_hex(self) -> str:
        if self.can_id is None:
            return ""
        width = 8 if self.is_extended else 3
        return "{:0{w}X}".format(self.can_id, w=width)

    @property
    def id_label(self) -> str:
        return "0x" + self.id_hex if self.can_id is not None else "any ID"

    @property
    def bit_span(self) -> str:
        return "{}..{}".format(self.start, self.start + self.length - 1)

    @property
    def byte_aligned(self) -> bool:
        """Whether this signal spans whole bytes with no sub-byte offset.

        DBC's Motorola ("big_endian") bit numbering is not sequential from
        byte 0 — it is the "sawtooth" convention, where the *start* of a
        byte-aligned, most-significant-bit-first field sits at bit position
        ``byte_index*8 + 7``, not ``byte_index*8``. A signal whose byte_order
        is little_endian (or whose encoding is BCD, which never goes through
        DBC bit numbering at all) uses the plain sequential check instead.
        """
        if self.length % 8 != 0:
            return False
        if self.encoding == BCD or self.byte_order == LITTLE_ENDIAN:
            return self.start % 8 == 0
        return self.start % 8 == 7

    def describe(self) -> str:
        parts = ["{} bit{} at {}".format(
            self.length, "" if self.length == 1 else "s", self.start)]
        if self.encoding == INT:
            parts.append("Intel" if self.byte_order == LITTLE_ENDIAN else "Motorola")
            if self.is_signed:
                parts.append("signed")
        elif self.encoding in (FLOAT32, FLOAT64):
            parts.append("float32" if self.encoding == FLOAT32 else "float64")
            parts.append("Intel" if self.byte_order == LITTLE_ENDIAN else "Motorola")
        else:
            parts.append("packed BCD")
        if self.scale != 1.0 or self.offset != 0.0:
            parts.append("raw x {:g} + {:g}".format(self.scale, self.offset))
        if self.unit:
            parts.append(self.unit)
        if not self.enabled:
            parts.append("disabled")
        return " · ".join(parts)

    # -- matching -----------------------------------------------------------

    def matches(self, frame: CanFrame) -> bool:
        if not self.enabled:
            return False
        if self.can_id is not None and (self.can_id != frame.arb_id
                                        or bool(self.is_extended) != bool(frame.is_extended)):
            return False
        if self.channel and self.channel != frame.channel:
            return False
        return True

    # -- copying --------------------------------------------------------

    def duplicated(self, name: Optional[str] = None) -> "Signal":
        return _replace(self, name=name or "{} copy".format(self.name))

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "can_id": None if self.can_id is None else "0x{:X}".format(self.can_id),
            "is_extended": self.is_extended,
            "channel": self.channel,
            "start": self.start,
            "length": self.length,
            "byte_order": self.byte_order,
            "is_signed": self.is_signed,
            "encoding": self.encoding,
            "scale": self.scale,
            "offset": self.offset,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "unit": self.unit,
            "decimals": self.decimals,
            "comment": self.comment,
            "choices": [[v, t] for v, t in self.choices],
            "message_name": self.message_name,
            "message_length": self.message_length,
            "message_comment": self.message_comment,
            "message_senders": list(self.message_senders),
            "message_cycle_time": self.message_cycle_time,
            "message_is_fd": self.message_is_fd,
            "source_kind": self.source_kind,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Signal":
        can_id = raw.get("can_id")
        if isinstance(can_id, str):
            can_id = can_id.strip()
            can_id = (int(can_id, 16 if can_id.lower().startswith("0x") else 10)
                      if can_id else None)
        elif isinstance(can_id, (int, float)):
            can_id = int(can_id)
        else:
            can_id = None
        choices = tuple(
            (int(v), str(t)) for v, t in (raw.get("choices") or ())
            if isinstance(v, (int, float))
        )
        return cls(
            name=str(raw.get("name", "signal")),
            enabled=bool(raw.get("enabled", True)),
            can_id=can_id,
            is_extended=bool(raw.get("is_extended", False)),
            channel=str(raw.get("channel", "") or ""),
            start=int(raw.get("start", 0) or 0),
            length=int(raw.get("length", 8) or 8),
            byte_order=str(raw.get("byte_order", LITTLE_ENDIAN)),
            is_signed=bool(raw.get("is_signed", False)),
            encoding=str(raw.get("encoding", INT)),
            scale=_as_float(raw.get("scale", 1.0), 1.0),
            offset=_as_float(raw.get("offset", 0.0), 0.0),
            minimum=_as_float(raw.get("minimum"), None),
            maximum=_as_float(raw.get("maximum"), None),
            unit=str(raw.get("unit", "") or ""),
            decimals=int(raw.get("decimals", 3) or 0),
            comment=str(raw.get("comment", "") or ""),
            choices=choices,
            message_name=str(raw.get("message_name", "") or ""),
            message_length=(int(raw["message_length"])
                            if raw.get("message_length") is not None else None),
            message_comment=str(raw.get("message_comment", "") or ""),
            message_senders=tuple(raw.get("message_senders", ()) or ()),
            message_cycle_time=raw.get("message_cycle_time"),
            message_is_fd=bool(raw.get("message_is_fd", False)),
            source_kind=str(raw.get("source_kind", "MANUAL") or "MANUAL"),
        )


# ---------------------------------------------------------------------------
# cantools bridge — building throwaway Signal/Message/Database objects
# ---------------------------------------------------------------------------


def _cantools_signal(signal: Signal):
    """One cantools ``Signal`` reproducing this signal's extraction and scale.

    A fresh object every call. Nothing here is retained — the whole point is
    to borrow the library's own bit-numbering and scaling math for one
    decode, including the Motorola "sawtooth" convention, rather than
    re-deriving it by hand.
    """
    from cantools.database.can.signal import Signal as CtSignal
    from cantools.database.conversion import BaseConversion

    if signal.encoding == BCD:
        raise SignalError("BCD has no cantools bit-signal equivalent")
    is_float = signal.encoding in (FLOAT32, FLOAT64)
    conversion = BaseConversion.factory(
        scale=signal.scale, offset=signal.offset, is_float=is_float)
    return CtSignal(
        name=signal.name or "value",
        start=signal.start,
        length=signal.length,
        byte_order=signal.byte_order,
        is_signed=False if is_float else signal.is_signed,
        conversion=conversion,
        minimum=signal.minimum,
        maximum=signal.maximum,
        unit=signal.unit or None,
    )


def extract_little_endian_bits(data: bytes, start: int,
                               length: int) -> Optional[int]:
    """Shared sequential LSB-first extraction for mapped industrial fields.

    DBC Motorola fields still belong to cantools' sawtooth implementation;
    CANopen PDO mapping is sequential little-endian bit packing, for which
    this bounded extractor avoids constructing a throwaway DBC per value.
    """
    integer = int.from_bytes(data, "little", signed=False)
    return extract_little_endian_bits_from_integer(
        integer, len(data) * 8, start, length)


def extract_little_endian_bits_from_integer(
        integer: int, data_bits: int, start: int,
        length: int) -> Optional[int]:
    """Same normalized extraction with a caller-reused payload integer."""
    if start < 0 or length <= 0 or start + length > data_bits:
        return None
    return (integer >> start) & ((1 << length) - 1)


def _cantools_message(frame_id: int, is_extended: bool, name: str,
                      length: int, cantools_signals: Sequence[Any],
                      is_fd: bool = False):
    from cantools.database.can.message import Message
    return Message(
        frame_id=frame_id, name=name or "Msg_{:X}".format(frame_id),
        length=max(1, int(length)), signals=list(cantools_signals),
        is_extended_frame=bool(is_extended), is_fd=bool(is_fd), strict=False,
    )


def preview(signal: Signal, data: bytes) -> Optional[float]:
    """Decode ``data`` against one signal in isolation. None on any failure.

    Used by the editor to show a live value before the signal is saved
    anywhere; never raises into the caller.
    """
    if signal.encoding == BCD:
        from ..interpret import DECODERS
        start = signal.start // 8
        end = start + max(1, signal.length // 8)
        if end > len(data):
            return None
        try:
            base = DECODERS["bcd"].value(bytes(data[start:end]))
        except Exception:
            return None
        if base is None:
            return None
        return base * signal.scale + signal.offset
    try:
        ct_signal = _cantools_signal(signal)
        needed = (signal.start + signal.length + 7) // 8
        message = _cantools_message(
            signal.can_id or 0x001, signal.is_extended, "preview",
            max(needed, len(data)), [ct_signal])
        values = message.decode(bytes(data), decode_choices=False,
                                scaling=True, allow_truncated=True)
    except Exception:
        return None
    value = values.get(ct_signal.name)
    return float(value) if isinstance(value, (int, float)) else None


def decode_for_frame(signal: Signal, frame: CanFrame) -> Optional[float]:
    """``preview`` restricted to a frame this signal actually matches."""
    if not signal.matches(frame):
        return None
    return preview(signal, frame.data)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def validate_signal(signal: Signal) -> List[str]:
    """Problems with one signal, in isolation. Empty when sound."""
    problems: List[str] = []
    where = signal.name or "unnamed signal"
    if not signal.name:
        problems.append("a signal has no name")
    if signal.length < 1:
        problems.append("{}: length must be at least 1".format(where))
    if signal.scale == 0:
        problems.append(
            "{}: a scale of 0 makes every reading the offset".format(where))
    if signal.minimum is not None and signal.maximum is not None \
            and signal.minimum > signal.maximum:
        problems.append("{}: minimum is above maximum".format(where))
    if signal.encoding == BCD and not signal.byte_aligned:
        problems.append(
            "{}: BCD needs a byte-aligned start and length".format(where))
    return problems


# ---------------------------------------------------------------------------
# profile: one document, its signals
# ---------------------------------------------------------------------------


@dataclass
class Profile(object):
    """One editable document: a name, an optional backing ``.dbc`` file, and
    the flat list of signals it holds.

    ``path`` is empty for a profile that has never been exported — "New DBC"
    is exactly this: a profile with no file yet. ``dirty`` tracks edits since
    the last successful export/import, purely for the "unsaved changes"
    prompt; it is not persisted.
    """

    name: str = "untitled.dbc"
    path: str = ""
    signals: List[Signal] = field(default_factory=list)
    definitions: List[DefinitionReference] = field(default_factory=list)
    canopen_associations: List[CanopenNodeAssociation] = field(default_factory=list)
    dirty: bool = False
    profile_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source_hash: str = ""

    def describe(self) -> str:
        return "{} — {} signal{}".format(
            self.name, len(self.signals), "" if len(self.signals) == 1 else "s")

    # -- grouping (a view, not stored state) -----------------------------

    def message_groups(self) -> List[Tuple[Optional[int], bool, List[Signal]]]:
        """Signals grouped by (can_id, is_extended). ``None`` group is "any ID".

        Order: real messages by id, then the any-ID group last — matching the
        example in the task, where free-standing signals read as an appendix
        to the messages, not scattered among them.
        """
        groups: Dict[Tuple[Optional[int], bool], List[Signal]] = {}
        order: List[Tuple[Optional[int], bool]] = []
        for signal in self.signals:
            key = (signal.can_id, signal.is_extended)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(signal)
        order.sort(key=lambda k: (k[0] is None, k[0] if k[0] is not None else 0))
        return [(cid, ext, groups[(cid, ext)]) for cid, ext in order]

    # -- editing ----------------------------------------------------------

    def add(self, signal: Optional[Signal] = None) -> Signal:
        signal = signal or Signal(name="signal {}".format(len(self.signals) + 1))
        self.signals.append(signal)
        self.dirty = True
        return signal

    def has_message(self, can_id: int, is_extended: bool) -> bool:
        return any(s.can_id == can_id and s.is_extended == is_extended
                  for s in self.signals)

    def add_signal(self, can_id: Optional[int], is_extended: bool = False,
                   signal: Optional[Signal] = None) -> Signal:
        """Add a signal under a specific message (or "any ID" if ``can_id``
        is None) — the message-oriented replacement for ``add()``: the
        caller says *which* message a new signal belongs to instead of
        adding it loose and leaving it to be assigned afterwards.

        Inherits the group's shared message metadata (name, length, ...)
        from an existing sibling signal, if there is one, so a signal added
        to an existing message always agrees with the rest of its group —
        message_groups() and export() both trust that agreement rather than
        reconciling it themselves.
        """
        signal = signal or Signal(name="signal {}".format(len(self.signals) + 1))
        signal.can_id = can_id
        signal.is_extended = bool(is_extended) if can_id is not None else False
        if can_id is not None:
            sibling = next((s for s in self.signals if s.can_id == can_id
                           and s.is_extended == signal.is_extended), None)
            if sibling is not None:
                signal.message_name = sibling.message_name
                signal.message_length = sibling.message_length
                signal.message_comment = sibling.message_comment
                signal.message_senders = sibling.message_senders
                signal.message_cycle_time = sibling.message_cycle_time
                signal.message_is_fd = sibling.message_is_fd
            elif not signal.message_name:
                signal.message_name = "Msg_{:X}".format(can_id)
        else:
            signal.message_name = ""
        self.signals.append(signal)
        self.dirty = True
        return signal

    def set_message_meta(self, can_id: int, is_extended: bool, *,
                         name: Optional[str] = None,
                         length: Optional[int] = None,
                         is_fd: Optional[bool] = None,
                         senders: Optional[Tuple[str, ...]] = None,
                         comment: Optional[str] = None,
                         cycle_time: Optional[int] = None) -> bool:
        """Update message-level fields across every signal that shares this
        CAN ID at once. A ``None`` argument leaves that field alone.

        A message's signals are expected to agree on its name, length, and
        so on — this is the one place that assumption is upheld: editing a
        message updates its *entire* group in one step, rather than one
        signal's copy at a time the way the old per-signal editor did.
        Returns whether anything actually changed.
        """
        changed = False
        for sig in self.signals:
            if sig.can_id != can_id or sig.is_extended != is_extended:
                continue
            if name is not None and sig.message_name != name:
                sig.message_name = name
                changed = True
            if length is not None and sig.message_length != length:
                sig.message_length = length
                changed = True
            if is_fd is not None and sig.message_is_fd != is_fd:
                sig.message_is_fd = is_fd
                changed = True
            if senders is not None and sig.message_senders != senders:
                sig.message_senders = senders
                changed = True
            if comment is not None and sig.message_comment != comment:
                sig.message_comment = comment
                changed = True
            if cycle_time is not None and sig.message_cycle_time != cycle_time:
                sig.message_cycle_time = cycle_time
                changed = True
        if changed:
            self.dirty = True
        return changed

    def move_message(self, can_id: int, is_extended: bool,
                     new_can_id: int, new_is_extended: bool) -> bool:
        """Re-key an entire message's CAN ID / extended-ness in one step.

        The message, not the signal, is the unit that moves: every signal
        in the group is re-keyed together, so they can never end up
        disagreeing about which message they belong to the way independent
        per-signal CAN ID edits once could. Raises SignalError if the
        destination is already a different message.
        """
        if (new_can_id, new_is_extended) == (can_id, is_extended):
            return False
        if self.has_message(new_can_id, new_is_extended):
            raise SignalError(
                "0x{:X} is already used by another message.".format(new_can_id))
        moved = False
        for sig in self.signals:
            if sig.can_id == can_id and sig.is_extended == is_extended:
                sig.can_id = new_can_id
                sig.is_extended = new_is_extended
                moved = True
        if moved:
            self.dirty = True
        return moved

    def remove_message(self, can_id: int, is_extended: bool) -> int:
        """Delete a whole message: every signal that belongs to it.

        Returns how many signals were removed. "Any ID" is not a message —
        callers must not pass ``can_id=None`` here; individual any-ID
        signals are removed one at a time with ``remove()``, same as before.
        """
        before = len(self.signals)
        self.signals = [s for s in self.signals
                        if not (s.can_id == can_id and s.is_extended == is_extended)]
        removed = before - len(self.signals)
        if removed:
            self.dirty = True
        return removed

    def duplicate(self, index: int) -> Optional[Signal]:
        if not (0 <= index < len(self.signals)):
            return None
        copy = self.signals[index].duplicated()
        self.signals.append(copy)
        self.dirty = True
        return copy

    def remove(self, index: int) -> None:
        if 0 <= index < len(self.signals):
            del self.signals[index]
            self.dirty = True

    # -- industrial definitions -------------------------------------------

    def add_definition(self, reference: DefinitionReference) -> bool:
        """Add by content identity; never overwrite an existing import."""
        if any(item.identity == reference.identity for item in self.definitions):
            return False
        self.definitions.append(reference)
        return True

    def replace_definition(self, old_hash: str,
                           reference: DefinitionReference) -> bool:
        """Explicit replacement, preserving node associations deliberately."""
        for index, item in enumerate(self.definitions):
            if item.identity != old_hash:
                continue
            self.definitions[index] = reference
            self.canopen_associations = [
                CanopenNodeAssociation(
                    reference.identity if association.definition_hash == old_hash
                    else association.definition_hash,
                    association.node_id, association.channel, association.method)
                for association in self.canopen_associations
            ]
            return True
        return False

    def remove_definition(self, content_hash: str) -> None:
        self.definitions = [item for item in self.definitions
                            if item.identity != content_hash]
        self.canopen_associations = [item for item in self.canopen_associations
                                     if item.definition_hash != content_hash]

    def associate_canopen(self, content_hash: str, node_id: int,
                          channel: str = "", method: str = "MANUAL") -> None:
        if not 1 <= int(node_id) <= 127:
            raise ValueError("CANopen node ID must be in 1..127")
        if not any(item.identity == content_hash for item in self.definitions):
            raise ValueError("definition is not part of this profile")
        association = CanopenNodeAssociation(
            content_hash, int(node_id), str(channel), str(method))
        self.canopen_associations = [
            item for item in self.canopen_associations
            if not (item.node_id == association.node_id
                    and item.channel == association.channel
                    and item.definition_hash == association.definition_hash)
        ]
        self.canopen_associations.append(association)

    def remove_canopen_association(self, content_hash: str, node_id: int,
                                   channel: str = "") -> None:
        self.canopen_associations = [
            item for item in self.canopen_associations
            if not (item.definition_hash == content_hash
                    and item.node_id == int(node_id)
                    and item.channel == channel)
        ]

    # -- validation ---------------------------------------------------------

    def validate(self) -> List[str]:
        problems: List[str] = []
        for signal in self.signals:
            problems.extend(validate_signal(signal))
        names: Dict[Tuple[Optional[int], str], int] = {}
        for signal in self.signals:
            key = (signal.can_id, signal.name)
            names[key] = names.get(key, 0) + 1
        for (can_id, name), count in names.items():
            if count > 1 and name:
                where = "0x{:X}".format(can_id) if can_id is not None else "any ID"
                problems.append(
                    "{}: {} signals share the name {}".format(where, count, name))
        return problems

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.profile_id, "name": self.name, "path": self.path,
               "source_hash": self.source_hash,
               "signals": [s.to_dict() for s in self.signals],
               "definitions": [item.to_dict() for item in self.definitions],
               "canopen_associations": [
                   item.to_dict() for item in self.canopen_associations],
               }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Profile":
        path = str(raw.get("path", "") or "")
        identity = str(raw.get("id", "") or "")
        try:
            uuid.UUID(identity)
        except (ValueError, AttributeError):
            # Stable compatibility identity for profiles saved before IDs
            # existed. The next normal config save persists it explicitly.
            legacy = dict(raw)
            legacy.pop("id", None)
            identity = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                json.dumps(legacy, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)))
        signals = []
        for item in raw.get("signals", ()):
            if not isinstance(item, dict):
                continue
            signal = Signal.from_dict(item)
            # Phase-5 configs predate per-signal provenance. A backed .dbc
            # profile's legacy entries necessarily came through DBC import;
            # new/manual entries persist their explicit source_kind.
            if "source_kind" not in item and path.lower().endswith(".dbc"):
                signal.source_kind = "DBC"
            signals.append(signal)
        return cls(
            name=str(raw.get("name", "untitled.dbc")),
            path=path,
            signals=signals,
            definitions=[DefinitionReference.from_dict(item)
                         for item in raw.get("definitions", ())
                         if isinstance(item, dict)],
            canopen_associations=[CanopenNodeAssociation.from_dict(item)
                                  for item in raw.get("canopen_associations", ())
                                  if isinstance(item, dict)],
            dirty=False,
            profile_id=identity,
            source_hash=str(raw.get("source_hash", "") or ""),
        )


# ---------------------------------------------------------------------------
# import / export
# ---------------------------------------------------------------------------


def import_dbc(path: str) -> Profile:
    """Read a ``.dbc`` into a new profile. Raises SignalError on failure."""
    try:
        import cantools.database
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise SignalError(
            "cantools is not installed; DBC import is unavailable. "
            "Install it with: pip install -r requirements.txt") from exc
    if not os.path.exists(path):
        raise SignalError("No such file: {}".format(path))
    try:
        database = cantools.database.load_file(path, strict=False)
    except Exception as exc:
        raise SignalError("Could not read {}: {}".format(
            os.path.basename(path), exc)) from exc

    source_hash = _sha256_file(path)
    profile = Profile(name=os.path.basename(path), path=path,
                      source_hash=source_hash)
    for message in getattr(database, "messages", ()) or ():
        frame_id = getattr(message, "frame_id", None)
        if frame_id is None:
            continue
        is_extended = bool(getattr(message, "is_extended_frame", False))
        for sig in getattr(message, "signals", ()) or ():
            conversion = getattr(sig, "conversion", None)
            is_float = bool(getattr(conversion, "is_float", False))
            length = int(getattr(sig, "length", 1) or 1)
            encoding = (FLOAT32 if is_float and length == 32
                       else FLOAT64 if is_float and length == 64
                       else INT)
            choices_raw = getattr(conversion, "choices", None) or {}
            choices = tuple(sorted(
                (int(v), str(t)) for v, t in choices_raw.items()
                if isinstance(v, (int, float))))
            profile.signals.append(Signal(
                name=str(getattr(sig, "name", "") or ""),
                can_id=int(frame_id),
                is_extended=is_extended,
                start=int(getattr(sig, "start", 0) or 0),
                length=length,
                byte_order=str(getattr(sig, "byte_order", LITTLE_ENDIAN)),
                is_signed=bool(getattr(sig, "is_signed", False)),
                encoding=encoding,
                scale=_as_float(getattr(conversion, "scale", 1.0), 1.0),
                offset=_as_float(getattr(conversion, "offset", 0.0), 0.0),
                minimum=getattr(sig, "minimum", None),
                maximum=getattr(sig, "maximum", None),
                unit=str(getattr(sig, "unit", "") or ""),
                comment=str(getattr(sig, "comment", "") or ""),
                choices=choices,
                message_name=str(getattr(message, "name", "") or ""),
                message_length=int(getattr(message, "length", 0) or 0),
                message_comment=str(getattr(message, "comment", "") or ""),
                message_senders=tuple(getattr(message, "senders", ()) or ()),
                message_cycle_time=getattr(message, "cycle_time", None),
                message_is_fd=bool(getattr(message, "is_fd", False)),
                source_kind="DBC",
            ))
    return profile


def export_dbc(profile: Profile, path: str) -> Tuple[int, List[str]]:
    """Write the profile's message-bound signals to ``path``.

    Returns ``(signals written, warnings)``. Never writes a definition that
    would misrepresent the profile: a signal this format cannot express is
    left out and named in the warnings, rather than dropped silently.
    """
    problems = profile.validate()
    if problems:
        raise SignalError("This database cannot be written yet:\n  - "
                          + "\n  - ".join(problems[:8]))

    try:
        import cantools.database
        from cantools.database.can.database import Database as CtDatabase
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise SignalError("cantools is not installed.") from exc

    warnings: List[str] = []
    any_id = [s for s in profile.signals if s.can_id is None]
    if any_id:
        warnings.append(
            "{} signal{} with no CAN ID (\"any ID\") cannot be written to a "
            ".dbc — every DBC signal must belong to one message. Excluded: {}"
            .format(len(any_id), "" if len(any_id) == 1 else "s",
                    ", ".join(s.name or "unnamed" for s in any_id[:6])
                    + (", ..." if len(any_id) > 6 else "")))
    bcd = [s for s in profile.signals if s.can_id is not None and s.encoding == BCD]
    if bcd:
        warnings.append(
            "{} BCD signal{} cannot be written to a .dbc — packed decimal is "
            "not a linear function of the raw bits. Excluded: {}".format(
                len(bcd), "" if len(bcd) == 1 else "s",
                ", ".join(s.name or "unnamed" for s in bcd[:6])
                + (", ..." if len(bcd) > 6 else "")))
    channelled = [s for s in profile.signals
                 if s.can_id is not None and s.encoding != BCD and s.channel]
    if channelled:
        warnings.append(
            "{} signal{} restricted to channel \"{}\" will apply to every "
            "channel once exported — DBC has no channel concept.".format(
                len(channelled), "" if len(channelled) == 1 else "s",
                channelled[0].channel)
            if len({s.channel for s in channelled}) == 1 else
            "{} signal{} are channel-restricted; DBC has no channel concept, "
            "so exported signals apply to every channel.".format(
                len(channelled), "" if len(channelled) == 1 else "s"))
    fd_ids: List[int] = []
    for s in profile.signals:
        if s.can_id is not None and s.message_is_fd and s.can_id not in fd_ids:
            fd_ids.append(s.can_id)
    if fd_ids:
        warnings.append(
            "{} message{} marked CAN FD will be written as classic CAN — "
            "this writer does not yet emit the VFrameFormat attribute a "
            "real CAN FD .dbc needs to carry that flag. The flag itself is "
            "not lost from this application, only from the exported file. "
            "Affected: {}".format(
                len(fd_ids), "" if len(fd_ids) == 1 else "s",
                ", ".join("0x{:X}".format(i) for i in fd_ids[:6])
                + (", ..." if len(fd_ids) > 6 else "")))

    messages = []
    written = 0
    for can_id, is_extended, group in profile.message_groups():
        if can_id is None:
            continue
        exportable = [s for s in group if s.encoding != BCD]
        if not exportable:
            continue
        ct_signals = [_cantools_signal(s) for s in exportable]
        first = exportable[0]
        length = first.message_length or max(
            (s.start + s.length + 7) // 8 for s in exportable)
        messages.append(_cantools_message(
            can_id, is_extended, first.message_name, length, ct_signals,
            is_fd=first.message_is_fd))
        written += len(exportable)

    db = CtDatabase(messages=messages, strict=False)
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise SignalError("Cannot create {}: {}".format(directory, exc)) from exc
    try:
        cantools.database.dump_file(db, path)
    except Exception as exc:
        raise SignalError("Could not write {}: {}".format(
            os.path.basename(path), exc)) from exc
    try:
        profile.source_hash = _sha256_file(path)
    except OSError:
        profile.source_hash = ""
    return written, warnings


# ---------------------------------------------------------------------------
# apply: build the two artifacts the rest of the application already consumes
# ---------------------------------------------------------------------------


def build_dbc_database(profile: Profile) -> Optional[DbcDatabase]:
    """A ``DbcDatabase`` for the profile's message-bound signals, or None.

    Feeds exactly the same path a loaded ``.dbc`` file always has —
    ``interpret_view.set_database()`` neither knows nor needs to know the
    database came from a profile instead of a file on disk.
    """
    messages = []
    for can_id, is_extended, group in profile.message_groups():
        if can_id is None:
            continue
        usable = [s for s in group if s.enabled and s.encoding != BCD]
        if not usable:
            continue
        try:
            ct_signals = [_cantools_signal(s) for s in usable]
        except SignalError:
            continue
        first = usable[0]
        length = first.message_length or max(
            (s.start + s.length + 7) // 8 for s in usable)
        try:
            messages.append(_cantools_message(
                can_id, is_extended, first.message_name, length, ct_signals,
                is_fd=first.message_is_fd))
        except Exception:
            continue          # one malformed message must not sink the rest
    if not messages:
        return None
    from cantools.database.can.database import Database as CtDatabase
    db = CtDatabase(messages=messages, strict=False)
    wrapper = DbcDatabase.__new__(DbcDatabase)
    wrapper.path = profile.path
    wrapper.name = profile.name
    wrapper._db = db
    wrapper._index = {}
    wrapper._build_index()
    return wrapper


# ---------------------------------------------------------------------------
# the store: every profile, which one is active
# ---------------------------------------------------------------------------


class ProfileStore(object):
    """Every known profile, and which one (if any) is active."""

    def __init__(self, profiles: Optional[List[Profile]] = None,
                 active: Optional[str] = None):
        self.profiles: List[Profile] = profiles or []
        #: Name of the active profile, or None. A name, not an index — indices
        #: shift under add/remove and a stale one would silently point at the
        #: wrong profile.
        self.active: Optional[str] = active

    def find(self, name: str) -> Optional[Profile]:
        for profile in self.profiles:
            if profile.name == name:
                return profile
        return None

    def find_by_id(self, profile_id: str) -> Optional[Profile]:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        return None

    @property
    def active_profile(self) -> Optional[Profile]:
        return self.find(self.active) if self.active else None

    def unique_name(self, base: str) -> str:
        if not self.find(base):
            return base
        stem, ext = os.path.splitext(base)
        ext = ext or ".dbc"
        n = 2
        while self.find("{} ({}){}".format(stem, n, ext)):
            n += 1
        return "{} ({}){}".format(stem, n, ext)

    def add(self, profile: Profile) -> Profile:
        profile.name = self.unique_name(profile.name)
        self.profiles.append(profile)
        return profile

    def remove(self, name: str) -> None:
        self.profiles = [p for p in self.profiles if p.name != name]
        if self.active == name:
            self.active = None

    # -- serialisation ------------------------------------------------------

    def to_config(self) -> Dict[str, Any]:
        return {
            "profiles": [p.to_dict() for p in self.profiles],
            "active": self.active or "",
        }

    @classmethod
    def from_config(cls, raw: Dict[str, Any]) -> "ProfileStore":
        profiles = [Profile.from_dict(p) for p in raw.get("profiles", ())
                   if isinstance(p, dict)]
        active = str(raw.get("active", "") or "") or None
        store = cls(profiles, active)
        if active and not store.find(active):
            store.active = None
        return store

    # -- migration ------------------------------------------------------

    @classmethod
    def migrate_legacy(cls, dbc_path: str, signal_rules: Sequence[Dict[str, Any]]
                       ) -> Tuple["ProfileStore", List[str]]:
        """Build a store from the old ``dbc.path`` + flat ``signals`` config.

        Every old rule is preserved somewhere — nothing is silently dropped —
        but a rule using a decoder with no bit-precise equivalent (only
        ``ascii``/``u8_pair``/``i8_pair`` qualify, and those were never
        selectable in the old editor's decoder list in the first place, since
        it only offered numeric decoders) cannot be represented and is
        reported rather than guessed at.
        """
        notes: List[str] = []
        store = cls()
        dbc_profile: Optional[Profile] = None

        if dbc_path and os.path.exists(dbc_path):
            try:
                dbc_profile = import_dbc(dbc_path)
            except SignalError as exc:
                notes.append("Could not migrate the previously loaded database: "
                            "{}".format(exc))

        rules_profile = Profile(name="Scaled values")
        dropped = []
        for raw in signal_rules or ():
            if not isinstance(raw, dict):
                continue
            signal = _signal_from_legacy_rule(raw)
            if signal is None:
                dropped.append(str(raw.get("name", "unnamed")))
                continue
            rules_profile.signals.append(signal)

        if dropped:
            notes.append(
                "{} scaled-value rule{} used a decoder with no equivalent "
                "here and could not be migrated: {}".format(
                    len(dropped), "" if len(dropped) == 1 else "s",
                    ", ".join(dropped[:6]) + (", ..." if len(dropped) > 6 else "")))

        if dbc_profile is not None:
            store.add(dbc_profile)
            store.active = dbc_profile.name
        if rules_profile.signals:
            store.add(rules_profile)
            if store.active is None:
                store.active = rules_profile.name
            else:
                notes.append(
                    "Your scaled-value rules are kept in a separate "
                    "\"{}\" profile — only one profile decodes at a time, so "
                    "use it from Profiles when you need those rules instead "
                    "of {}.".format(rules_profile.name, dbc_profile.name))
        return store, notes


def _signal_from_legacy_rule(raw: Dict[str, Any]) -> Optional[Signal]:
    decoder = str(raw.get("decoder", "u16_be"))
    mapping = _LEGACY_DECODER_MAP.get(decoder)
    if mapping is None:
        return None
    encoding, byte_order, is_signed, width_bits = mapping
    length_bytes = max(1, int(raw.get("length", 2) or 2))
    if width_bits == 0:                     # hex_be/hex_le/bcd: rule sets width
        width_bits = length_bytes * 8

    can_id = raw.get("can_id")
    if isinstance(can_id, str):
        can_id = can_id.strip()
        can_id = int(can_id, 16 if can_id.lower().startswith("0x") else 10) \
            if can_id else None

    byte_offset = int(raw.get("offset", 0) or 0)
    if byte_order == BIG_ENDIAN:
        # DBC's Motorola "sawtooth" numbering: a byte-aligned, MSB-first field
        # starts at byte_offset*8 + 7, not byte_offset*8 — see Signal.byte_aligned.
        # Verified against cantools' own decode (tests/test_signals_backend.py),
        # since this is exactly the convention that is easy to get subtly wrong
        # by hand.
        start_bit = byte_offset * 8 + 7
    else:
        start_bit = byte_offset * 8

    return Signal(
        name=str(raw.get("name", "signal")),
        enabled=bool(raw.get("enabled", True)),
        can_id=can_id,
        channel=str(raw.get("channel", "") or ""),
        start=start_bit,
        length=width_bits,
        byte_order=byte_order,
        is_signed=is_signed,
        encoding=encoding,
        scale=_as_float(raw.get("scale", 1.0), 1.0),
        offset=_as_float(raw.get("add", 0.0), 0.0),
        unit=str(raw.get("unit", "") or ""),
        decimals=int(raw.get("precision", 3) or 0),
    )
