"""Safe, provenance-aware local J1939 PGN/SPN definitions and decoding.

The JSON format implemented here is intentionally repository-specific and
small.  It is not an SAE Digital Annex reader and ships no proprietary data.
Bit positions are zero-based from the least-significant bit of payload byte 0.
Only sequential little-endian fields are decoded; other declared encodings are
retained and reported as unsupported.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Sequence, Tuple

from .definitions import (
    DefinitionDiagnostic, DefinitionReference, DefinitionSource,
    DefinitionSourceKind, DiagnosticSeverity, J1939PgnDefinition,
    J1939SpnDefinition, ValidationState,
)
from .protocols.j1939_transport import J1939PayloadObservation
from .signals import extract_little_endian_bits_from_integer


PARSER_VERSION = "1.0"
SCHEMA_VERSION = 1
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PGNS = 10000
MAX_SPNS = 100000
MAX_SPNS_PER_PGN = 2048
MAX_TEXT = 10000
MAX_FIELD_BITS = 512


class J1939DefinitionError(ValueError):
    pass


class J1939DecodeStatus(str, Enum):
    VALID = "VALID"
    SPECIAL = "SPECIAL"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    ERROR = "ERROR"
    OUT_OF_RANGE = "OUT_OF_DEFINED_RANGE"
    TRUNCATED = "TRUNCATED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN_PGN = "UNKNOWN_PGN"
    LENGTH_MISMATCH = "LENGTH_MISMATCH"


@dataclass(frozen=True)
class J1939DefinitionSet:
    source: DefinitionSource
    schema_version: int
    pgns: Tuple[J1939PgnDefinition, ...]
    validation_state: ValidationState
    diagnostics: Tuple[DefinitionDiagnostic, ...] = ()
    license_text: str = ""
    _pgn_numbers: Tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self._pgn_numbers:
            object.__setattr__(self, "_pgn_numbers",
                               tuple(item.pgn for item in self.pgns))

    @property
    def reference(self) -> DefinitionReference:
        warnings = tuple(item.display for item in self.diagnostics
                         if item.severity is not DiagnosticSeverity.INFO)
        return DefinitionReference(
            self.source, self.validation_state, warnings, len(self.pgns))

    def pgn(self, number: int) -> Optional[J1939PgnDefinition]:
        index = bisect_left(self._pgn_numbers, number)
        return (self.pgns[index] if index < len(self.pgns)
                and self._pgn_numbers[index] == number else None)


@dataclass(frozen=True)
class DecodedJ1939Spn:
    spn: int
    name: str
    raw: Optional[int]
    value: Optional[float]
    display_value: str
    unit: str
    status: J1939DecodeStatus
    bit_offset: int
    bit_length: int
    source: DefinitionSource
    diagnostic: str = ""


@dataclass(frozen=True)
class DecodedJ1939Message:
    pgn: int
    pgn_name: str
    channel: str
    source_address: int
    destination_address: Optional[int]
    transport: str
    payload: bytes
    first_timestamp: float
    last_timestamp: float
    status: J1939DecodeStatus
    values: Tuple[DecodedJ1939Spn, ...]
    source: Optional[DefinitionSource]
    diagnostics: Tuple[str, ...] = ()


def _duplicate_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise J1939DefinitionError("duplicate JSON key {!r}".format(key))
        result[key] = value
    return result


def _reject_constant(value):
    raise J1939DefinitionError("non-finite JSON number {} is not allowed".format(value))


def _dict(value: Any, where: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise J1939DefinitionError("{} must be an object".format(where))
    return value


def _list(value: Any, where: str) -> list:
    if not isinstance(value, list):
        raise J1939DefinitionError("{} must be an array".format(where))
    return value


def _text(value: Any, where: str, default: str = "") -> str:
    if value is None:
        value = default
    if not isinstance(value, str):
        raise J1939DefinitionError("{} must be text".format(where))
    if len(value) > MAX_TEXT:
        raise J1939DefinitionError("{} exceeds {} characters".format(where, MAX_TEXT))
    return value


def _int(value: Any, where: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise J1939DefinitionError("{} must be an integer".format(where))
    if not minimum <= value <= maximum:
        raise J1939DefinitionError(
            "{} must be in {}..{}".format(where, minimum, maximum))
    return value


def _float(value: Any, where: str, default: Optional[float] = None
           ) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise J1939DefinitionError("{} must be numeric".format(where))
    result = float(value)
    if not math.isfinite(result):
        raise J1939DefinitionError("{} must be finite".format(where))
    return result


def _mapping(value: Any, where: str) -> Tuple[Tuple[int, str], ...]:
    if value is None:
        return ()
    raw = _dict(value, where)
    if len(raw) > 4096:
        raise J1939DefinitionError("{} has too many entries".format(where))
    result = []
    for key, label in raw.items():
        try:
            number = int(key, 0)
        except (TypeError, ValueError) as exc:
            try:
                number = int(key, 10)
            except (TypeError, ValueError):
                raise J1939DefinitionError(
                    "{} key {!r} is not an integer".format(where, key)) from exc
        if number < 0 or number.bit_length() > MAX_FIELD_BITS:
            raise J1939DefinitionError("{} key is outside the supported range".format(where))
        result.append((number, _text(label, where + "." + str(key))))
    return tuple(sorted(result))


def _special_mapping(value: Any, where: str) -> Tuple[Tuple[int, str, str], ...]:
    if value is None:
        return ()
    raw = _dict(value, where)
    if len(raw) > 4096:
        raise J1939DefinitionError("{} has too many entries".format(where))
    result = []
    allowed = {"SPECIAL", "NOT_AVAILABLE", "ERROR"}
    for key, entry in raw.items():
        try:
            number = int(key, 0)
        except (TypeError, ValueError):
            try:
                number = int(key, 10)
            except (TypeError, ValueError) as exc:
                raise J1939DefinitionError(
                    "{} key {!r} is not an integer".format(where, key)) from exc
        if isinstance(entry, str):
            label = _text(entry, where + "." + str(key))
            folded = label.casefold()
            validity = ("NOT_AVAILABLE" if "not available" in folded
                        else "ERROR" if "error" in folded else "SPECIAL")
        else:
            item = _dict(entry, where + "." + str(key))
            label = _text(item.get("label", ""), where + "." + str(key) + ".label")
            validity = _text(item.get("status", "SPECIAL"),
                             where + "." + str(key) + ".status").upper()
        if validity not in allowed:
            raise J1939DefinitionError(
                "{} status must be SPECIAL, NOT_AVAILABLE, or ERROR".format(where))
        if number < 0 or number.bit_length() > MAX_FIELD_BITS:
            raise J1939DefinitionError("{} key is outside the supported range".format(where))
        result.append((number, label, validity))
    return tuple(sorted(result))


def _state(diagnostics: Sequence[DefinitionDiagnostic]) -> ValidationState:
    if any(item.severity is DiagnosticSeverity.ERROR for item in diagnostics):
        return ValidationState.INVALID
    if any(item.code == "unsupported-encoding" for item in diagnostics):
        return ValidationState.UNSUPPORTED_FEATURES
    if any(item.severity is DiagnosticSeverity.WARNING for item in diagnostics):
        return ValidationState.VALID_WITH_WARNINGS
    return ValidationState.VALID


def parse_j1939_definition_bytes(
        data: bytes, display_name: str = "definition.json", location: str = "",
        imported_at: str = "") -> J1939DefinitionSet:
    if len(data) > MAX_FILE_BYTES:
        raise J1939DefinitionError("definition exceeds the 8 MiB safety limit")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise J1939DefinitionError("definition must be UTF-8 JSON") from exc
    try:
        raw = json.loads(text, object_pairs_hook=_duplicate_object,
                         parse_constant=_reject_constant)
    except J1939DefinitionError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise J1939DefinitionError("invalid J1939 definition JSON: {}".format(exc)) from exc
    root = _dict(raw, "root")
    version = _int(root.get("schema_version"), "schema_version", 1, 1)
    metadata = _dict(root.get("metadata", {}), "metadata")
    license_text = _text(metadata.get("license", ""), "metadata.license")
    format_version = _text(metadata.get("version", str(version)), "metadata.version")
    digest = hashlib.sha256(data).hexdigest()
    source = DefinitionSource(
        DefinitionSourceKind.J1939, display_name, location, digest,
        imported_at or datetime.now(timezone.utc).isoformat(),
        format_version, PARSER_VERSION)
    raw_pgns = _list(root.get("pgns"), "pgns")
    if len(raw_pgns) > MAX_PGNS:
        raise J1939DefinitionError("definition has more than {:,} PGNs".format(MAX_PGNS))
    diagnostics = []
    pgns = []
    seen_pgns = set()
    total_spns = 0
    for pgn_index, pgn_value in enumerate(raw_pgns):
        where = "pgns[{}]".format(pgn_index)
        item = _dict(pgn_value, where)
        pgn = _int(item.get("pgn"), where + ".pgn", 0, 0x3FFFF)
        if pgn in seen_pgns:
            raise J1939DefinitionError("duplicate PGN {}".format(pgn))
        seen_pgns.add(pgn)
        name = _text(item.get("name", "PGN {}".format(pgn)), where + ".name")
        description = _text(item.get("description", ""), where + ".description")
        length = item.get("length", {"min": 0, "max": 1785})
        if isinstance(length, int) and not isinstance(length, bool):
            minimum_length = maximum_length = _int(length, where + ".length", 0, 1785)
        else:
            lengths = _dict(length, where + ".length")
            minimum_length = _int(lengths.get("min", 0), where + ".length.min", 0, 1785)
            maximum_length = _int(lengths.get("max", 1785), where + ".length.max", 0, 1785)
            if minimum_length > maximum_length:
                raise J1939DefinitionError(where + ".length min exceeds max")
        transport = _text(item.get("transport", "either"), where + ".transport")
        if transport not in ("single", "transport", "either"):
            raise J1939DefinitionError(where + ".transport must be single, transport, or either")
        raw_spns = _list(item.get("spns", []), where + ".spns")
        if len(raw_spns) > MAX_SPNS_PER_PGN:
            raise J1939DefinitionError(where + " has too many SPNs")
        total_spns += len(raw_spns)
        if total_spns > MAX_SPNS:
            raise J1939DefinitionError("definition has more than {:,} SPNs".format(MAX_SPNS))
        spns = []
        seen_spns = set()
        occupied = []
        for spn_index, spn_value in enumerate(raw_spns):
            spn_where = where + ".spns[{}]".format(spn_index)
            value = _dict(spn_value, spn_where)
            spn = _int(value.get("spn"), spn_where + ".spn", 0, 0x7FFFF)
            if spn in seen_spns:
                raise J1939DefinitionError("{} duplicates SPN {}".format(where, spn))
            seen_spns.add(spn)
            start = _int(value.get("start_bit"), spn_where + ".start_bit", 0, 14279)
            bits = _int(value.get("bit_length"), spn_where + ".bit_length", 1,
                        MAX_FIELD_BITS)
            byte_order = _text(value.get("byte_order", "little_endian"),
                               spn_where + ".byte_order")
            unsupported = ""
            if byte_order != "little_endian":
                unsupported = "byte order {!r} is retained but not decoded".format(byte_order)
                diagnostics.append(DefinitionDiagnostic(
                    DiagnosticSeverity.WARNING, "unsupported-encoding", unsupported,
                    spn_where))
            for other_start, other_end, other_spn in occupied:
                if start < other_end and other_start < start + bits:
                    diagnostics.append(DefinitionDiagnostic(
                        DiagnosticSeverity.WARNING, "overlapping-spn",
                        "SPN {} overlaps SPN {}".format(spn, other_spn), spn_where))
            occupied.append((start, start + bits, spn))
            states = _mapping(value.get("states"), spn_where + ".states")
            special = _special_mapping(value.get("special_values"),
                                       spn_where + ".special_values")
            maximum_raw = (1 << bits) - 1
            if (any(number > maximum_raw for number, _label in states)
                    or any(number > maximum_raw
                           for number, _label, _status in special)):
                raise J1939DefinitionError(
                    spn_where + " state/special value does not fit bit_length")
            signed = value.get("signed", False)
            if not isinstance(signed, bool):
                raise J1939DefinitionError(spn_where + ".signed must be boolean")
            minimum = _float(value.get("minimum"), spn_where + ".minimum")
            maximum = _float(value.get("maximum"), spn_where + ".maximum")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise J1939DefinitionError(spn_where + ".minimum exceeds maximum")
            spns.append(J1939SpnDefinition(
                spn, _text(value.get("name", "SPN {}".format(spn)),
                           spn_where + ".name"), start, bits, source,
                _text(value.get("description", ""), spn_where + ".description"),
                byte_order, signed,
                _float(value.get("scale"), spn_where + ".scale", 1.0),
                _float(value.get("offset"), spn_where + ".offset", 0.0),
                _text(value.get("unit", ""), spn_where + ".unit"),
                minimum, maximum,
                states, special, unsupported))
        pgns.append(J1939PgnDefinition(
            pgn, name, tuple(spns), source, description, minimum_length,
            maximum_length, transport))
    if not license_text:
        diagnostics.append(DefinitionDiagnostic(
            DiagnosticSeverity.WARNING, "missing-license",
            "Definition metadata does not declare a license", display_name))
    pgns.sort(key=lambda item: item.pgn)
    return J1939DefinitionSet(source, version, tuple(pgns),
                              _state(diagnostics), tuple(diagnostics), license_text)


def parse_j1939_definition_file(path: str) -> J1939DefinitionSet:
    absolute = os.path.abspath(path)
    try:
        size = os.path.getsize(absolute)
        if size > MAX_FILE_BYTES:
            raise J1939DefinitionError("definition exceeds the 8 MiB safety limit")
        with open(absolute, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise J1939DefinitionError("cannot read {}: {}".format(path, exc)) from exc
    return parse_j1939_definition_bytes(data, os.path.basename(absolute), absolute)


def _rebind(definition: J1939DefinitionSet,
            source: DefinitionSource) -> J1939DefinitionSet:
    pgns = tuple(replace(
        pgn, source=source,
        spns=tuple(replace(spn, source=source) for spn in pgn.spns))
        for pgn in definition.pgns)
    return replace(definition, source=source, pgns=pgns)


class J1939DefinitionCache:
    def __init__(self):
        self._items: Dict[Tuple[str, str], J1939DefinitionSet] = {}

    def parse_file(self, path: str) -> J1939DefinitionSet:
        absolute = os.path.abspath(path)
        try:
            with open(absolute, "rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            raise J1939DefinitionError("cannot read {}: {}".format(path, exc)) from exc
        if len(data) > MAX_FILE_BYTES:
            raise J1939DefinitionError("definition exceeds the 8 MiB safety limit")
        digest = hashlib.sha256(data).hexdigest()
        key = (digest, PARSER_VERSION)
        result = self._items.get(key)
        if result is None:
            result = parse_j1939_definition_bytes(
                data, os.path.basename(absolute), absolute)
            self._items[key] = result
            return result
        source = replace(
            result.source, display_name=os.path.basename(absolute),
            location=absolute, imported_at=datetime.now(timezone.utc).isoformat())
        return _rebind(result, source)

    def resolve(self, reference: DefinitionReference
                ) -> Tuple[Optional[J1939DefinitionSet], ValidationState, str]:
        if reference.source.kind is not DefinitionSourceKind.J1939:
            return None, ValidationState.INVALID, "reference is not a J1939 definition"
        path = reference.source.location
        if not path or not os.path.isfile(path):
            return None, ValidationState.MISSING, "source file is missing"
        try:
            result = self.parse_file(path)
        except J1939DefinitionError as exc:
            return None, ValidationState.INVALID, str(exc)
        if result.source.content_hash != reference.source.content_hash:
            return None, ValidationState.CHANGED, (
                "source path now contains different data; explicit re-import is required")
        result = _rebind(result, reference.source)
        return result, result.validation_state, ""

    @property
    def size(self) -> int:
        return len(self._items)


def decode_j1939_payload(
        payload: J1939PayloadObservation,
        definitions: Sequence[J1939DefinitionSet]) -> DecodedJ1939Message:
    pgn_definition = None
    for definition in definitions:
        pgn_definition = definition.pgn(payload.pgn)
        if pgn_definition is not None:
            break
    if pgn_definition is None:
        pdu_format = (payload.pgn >> 8) & 0xFF
        structural_name = (
            "Proprietary A (semantics undefined)" if pdu_format == 0xEF
            else "Proprietary B (semantics undefined)" if pdu_format == 0xFF
            else "")
        return DecodedJ1939Message(
            payload.pgn, structural_name, payload.channel, payload.source_address,
            payload.destination_address, payload.transport, payload.payload,
            payload.first_timestamp, payload.last_timestamp,
            J1939DecodeStatus.UNKNOWN_PGN, (), None,
            ("No active local definition for PGN {}".format(payload.pgn),))
    diagnostics = []
    length = len(payload.payload)
    status = J1939DecodeStatus.VALID
    if not pgn_definition.minimum_length <= length <= pgn_definition.maximum_length:
        status = J1939DecodeStatus.LENGTH_MISMATCH
        diagnostics.append(
            "Payload length {} is outside defined range {}..{}".format(
                length, pgn_definition.minimum_length,
                pgn_definition.maximum_length))
    expects_transport = pgn_definition.transport
    is_transport = payload.transport != "Single frame"
    if ((expects_transport == "single" and is_transport)
            or (expects_transport == "transport" and not is_transport)):
        diagnostics.append(
            "Observed {} conflicts with definition transport {!r}".format(
                payload.transport, expects_transport))
        status = J1939DecodeStatus.LENGTH_MISMATCH
    integer = int.from_bytes(payload.payload, "little", signed=False)
    values = []
    for spn in pgn_definition.spns:
        if spn.unsupported_reason:
            values.append(DecodedJ1939Spn(
                spn.spn, spn.name, None, None, "unsupported", spn.unit,
                J1939DecodeStatus.UNSUPPORTED, spn.start_bit, spn.bit_length,
                spn.source, spn.unsupported_reason))
            continue
        if spn.start_bit + spn.bit_length > length * 8:
            values.append(DecodedJ1939Spn(
                spn.spn, spn.name, None, None, "truncated", spn.unit,
                J1939DecodeStatus.TRUNCATED, spn.start_bit, spn.bit_length,
                spn.source, "field extends beyond observed payload"))
            continue
        raw = extract_little_endian_bits_from_integer(
            integer, length * 8, spn.start_bit, spn.bit_length)
        assert raw is not None
        special_entry = next((item for item in spn.special_values
                              if item[0] == raw), None)
        state_name = dict(spn.states).get(raw)
        signed_raw = raw
        if spn.signed and raw & (1 << (spn.bit_length - 1)):
            signed_raw = raw - (1 << spn.bit_length)
        physical = signed_raw * spn.scale + spn.offset
        value_status = J1939DecodeStatus.VALID
        display = state_name or "{:g}".format(physical)
        diagnostic = ""
        if special_entry is not None:
            value_status = J1939DecodeStatus(special_entry[2])
            display = special_entry[1]
            diagnostic = "raw value is defined as a special state"
        elif ((spn.minimum is not None and physical < spn.minimum)
              or (spn.maximum is not None and physical > spn.maximum)):
            value_status = J1939DecodeStatus.OUT_OF_RANGE
            diagnostic = "scaled value is outside the defined range"
        values.append(DecodedJ1939Spn(
            spn.spn, spn.name, raw, physical, display, spn.unit,
            value_status, spn.start_bit, spn.bit_length, spn.source, diagnostic))
    return DecodedJ1939Message(
        payload.pgn, pgn_definition.name, payload.channel,
        payload.source_address, payload.destination_address,
        payload.transport, payload.payload, payload.first_timestamp,
        payload.last_timestamp, status, tuple(values), pgn_definition.source,
        tuple(diagnostics))


def decode_j1939_payloads(
        payloads: Sequence[J1939PayloadObservation],
        definitions: Sequence[J1939DefinitionSet]) -> Tuple[DecodedJ1939Message, ...]:
    return tuple(decode_j1939_payload(item, definitions) for item in payloads)


DEFAULT_J1939_DEFINITION_CACHE = J1939DefinitionCache()


__all__ = [
    "DEFAULT_J1939_DEFINITION_CACHE", "DecodedJ1939Message",
    "DecodedJ1939Spn", "J1939DecodeStatus", "J1939DefinitionCache",
    "J1939DefinitionError", "J1939DefinitionSet", "MAX_FILE_BYTES",
    "PARSER_VERSION", "SCHEMA_VERSION", "decode_j1939_payload",
    "decode_j1939_payloads", "parse_j1939_definition_bytes",
    "parse_j1939_definition_file",
]
