"""Read-only DBC decoding.

A database is an *additional interpretation* of frames that were already
received. It never replaces the raw frame, and a message the database does not
describe stays exactly as visible as it was before the file was loaded.

Deliberately read-only in two senses:

* No editing. There is no writer, no DBC construction, no save path.
* No encoding. ``cantools`` can build frames from signal values, which is the
  first half of transmitting one. That API is not wrapped, not re-exported, and
  the underlying database object stays private to this module — the same
  containment ``sources/live.py`` applies to the python-can bus handle.

Every decode failure is reported as a status on the result rather than raised,
because one malformed message must not take down inspection of the capture
around it.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from ..model import CanFrame

#: Decode outcomes. Anything other than OK means the operator is looking at a
#: frame the database could not fully account for, and the UI must say so
#: rather than present a value as authoritative.
OK = "ok"
NOT_IN_DATABASE = "not-in-database"
LENGTH_MISMATCH = "length-mismatch"
DECODE_FAILED = "decode-failed"


class DbcError(RuntimeError):
    """Raised only when a database file itself cannot be loaded."""


class DecodedSignal(object):
    """One signal's value in one frame."""

    __slots__ = ("name", "value", "raw", "unit", "choice", "comment",
                 "is_multiplexer", "multiplexer_ids")

    def __init__(self, name: str, value: Any, raw: Any = None, unit: str = "",
                 choice: str = "", comment: str = "",
                 is_multiplexer: bool = False,
                 multiplexer_ids: Optional[Tuple[int, ...]] = None):
        self.name = name
        #: Physical value after scale/offset, or the raw value for enumerations.
        self.value = value
        self.raw = raw
        self.unit = unit
        #: Enumeration text when the database defines one for this value.
        self.choice = choice
        self.comment = comment
        self.is_multiplexer = is_multiplexer
        self.multiplexer_ids = multiplexer_ids or ()

    @property
    def display(self) -> str:
        """Human-facing value: enumeration text wins, then value plus unit."""
        if self.choice:
            return self.choice
        if isinstance(self.value, float):
            text = "{:g}".format(self.value)
        else:
            text = str(self.value)
        return "{} {}".format(text, self.unit).strip()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "DecodedSignal({}={})".format(self.name, self.display)


class DecodedMessage(object):
    """The database's account of one observed frame.

    ``status`` is always meaningful. ``signals`` is empty unless it is ``OK``.
    """

    __slots__ = ("name", "signals", "status", "detail", "expected_length",
                 "comment", "senders", "cycle_time")

    def __init__(self, name: str = "", signals: Optional[List[DecodedSignal]] = None,
                 status: str = NOT_IN_DATABASE, detail: str = "",
                 expected_length: Optional[int] = None, comment: str = "",
                 senders: Tuple[str, ...] = (), cycle_time: Optional[int] = None):
        self.name = name
        self.signals = signals or []
        self.status = status
        self.detail = detail
        self.expected_length = expected_length
        self.comment = comment
        self.senders = senders
        self.cycle_time = cycle_time

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def known(self) -> bool:
        """The database describes this ID, even if this frame did not decode."""
        return self.status != NOT_IN_DATABASE

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "DecodedMessage({}, {}, {} signals)".format(
            self.name or "-", self.status, len(self.signals))


#: Returned instead of None so callers never branch on null.
NO_MATCH = DecodedMessage()


class DbcDatabase(object):
    """A loaded database, exposing decoding only.

    Lookup is indexed by ``(frame_id, is_extended)`` on load. A DBC records
    extended identifiers with bit 31 set, and two messages may share the low
    bits while differing in addressing — matching on the pair keeps a standard
    0x100 and an extended 0x100 apart, which is what the wire does.
    """

    def __init__(self, path: str, database: Any):
        self.path = path
        self.name = os.path.basename(path)
        self._db = database                       # private: holds encode APIs
        self._index: Dict[Tuple[int, bool], Any] = {}
        self._build_index()

    # -- construction ---------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "DbcDatabase":
        """Parse a .dbc. Raises DbcError with a readable reason on failure."""
        try:
            import cantools.database
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise DbcError(
                "cantools is not installed; DBC decoding is unavailable. "
                "Install it with: pip install -r requirements.txt"
            ) from exc

        if not os.path.exists(path):
            raise DbcError("No such file: {}".format(path))
        try:
            database = cantools.database.load_file(path, strict=False)
        except Exception as exc:
            # cantools raises a family of parse errors; the operator only needs
            # to know the file was not usable and why.
            raise DbcError("Could not read {}: {}".format(
                os.path.basename(path), exc)) from exc
        return cls(path, database)

    def _build_index(self) -> None:
        for message in getattr(self._db, "messages", ()) or ():
            frame_id = getattr(message, "frame_id", None)
            if frame_id is None:
                continue
            extended = bool(getattr(message, "is_extended_frame", False))
            self._index[(int(frame_id), extended)] = message

    # -- description ----------------------------------------------------

    @property
    def message_count(self) -> int:
        return len(self._index)

    @property
    def signal_count(self) -> int:
        return sum(len(getattr(m, "signals", ()) or ())
                   for m in self._index.values())

    def describe(self) -> str:
        return "{} — {} messages, {} signals".format(
            self.name, self.message_count, self.signal_count)

    def known_keys(self) -> List[Tuple[int, bool]]:
        return list(self._index)

    def message_names(self) -> List[str]:
        return sorted(getattr(m, "name", "") for m in self._index.values())

    # -- decoding -------------------------------------------------------

    def _lookup(self, frame: CanFrame):
        return self._index.get((frame.arb_id, bool(frame.is_extended)))

    def signals_of(self, frame: CanFrame) -> List[str]:
        """Signal names the database defines for this frame's ID, if any."""
        message = self._lookup(frame)
        if message is None:
            return []
        return [getattr(s, "name", "") for s in getattr(message, "signals", ()) or ()]

    def decode(self, frame: CanFrame) -> DecodedMessage:
        """Decode one frame. Never raises; failures come back as a status."""
        message = self._lookup(frame)
        if message is None:
            return NO_MATCH

        name = str(getattr(message, "name", "") or "")
        expected = getattr(message, "length", None)
        comment = str(getattr(message, "comment", "") or "")
        senders = tuple(getattr(message, "senders", ()) or ())
        cycle = getattr(message, "cycle_time", None)

        def shell(status: str, detail: str) -> DecodedMessage:
            return DecodedMessage(name=name, status=status, detail=detail,
                                  expected_length=expected, comment=comment,
                                  senders=senders, cycle_time=cycle)

        # Error and remote frames are observations of the message, not of its
        # data; there is nothing to decode and that is not a failure of the DBC.
        if frame.is_error_frame or frame.is_remote_frame:
            return shell(LENGTH_MISMATCH, "frame carries no payload")

        if expected is not None and len(frame.data) < int(expected):
            return shell(
                LENGTH_MISMATCH,
                "database expects {} bytes, frame carries {}".format(
                    expected, len(frame.data)),
            )

        try:
            values = message.decode(
                frame.data,
                decode_choices=True,
                scaling=True,
                allow_truncated=False,
            )
        except Exception as exc:
            # Invalid multiplexor selector, out-of-range value under strict
            # bounds, unsupported construct — all land here and stay contained.
            return shell(DECODE_FAILED, str(exc))

        signals = self._build_signals(message, frame, values)
        return DecodedMessage(name=name, signals=signals, status=OK,
                              expected_length=expected, comment=comment,
                              senders=senders, cycle_time=cycle)

    def _build_signals(self, message, frame: CanFrame,
                       values: Dict[str, Any]) -> List[DecodedSignal]:
        by_name = {getattr(s, "name", ""): s
                   for s in getattr(message, "signals", ()) or ()}
        # Raw values are decoded separately so a scaled reading and the bits it
        # came from can both be shown; failure here is not fatal.
        raws: Dict[str, Any] = {}
        try:
            raws = message.decode(frame.data, decode_choices=False,
                                  scaling=False, allow_truncated=False)
        except Exception:
            raws = {}

        signals: List[DecodedSignal] = []
        # Preserve database order rather than dict order: a DBC lists signals
        # in a deliberate order and operators read them that way.
        for name in (getattr(s, "name", "") for s in
                     getattr(message, "signals", ()) or ()):
            if name not in values:
                continue                    # not selected by the multiplexor
            value = values[name]
            spec = by_name.get(name)
            choice = ""
            if not isinstance(value, (int, float)):
                # cantools returns a NamedSignalValue for enumerations; its
                # str() is the choice text and int() the underlying number.
                choice = str(value)
                try:
                    value = int(value)
                except Exception:
                    value = raws.get(name, value)
            signals.append(DecodedSignal(
                name=name,
                value=value,
                raw=raws.get(name),
                unit=str(getattr(spec, "unit", "") or "") if spec else "",
                choice=choice,
                comment=str(getattr(spec, "comment", "") or "") if spec else "",
                is_multiplexer=bool(getattr(spec, "is_multiplexer", False))
                if spec else False,
                multiplexer_ids=tuple(getattr(spec, "multiplexer_ids", ()) or ())
                if spec else (),
            ))
        return signals
