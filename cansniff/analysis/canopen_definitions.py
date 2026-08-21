"""Safe, passive CANopen EDS/DCF definition parsing and enrichment.

The parser accepts an intentionally documented INI-style subset and produces
immutable models.  It never evaluates file text, follows external resources,
or communicates with a CAN device.  Raw :class:`CanFrame` values remain the
source observations; every decoded value carries its imported source.
"""

from __future__ import annotations

import configparser
import hashlib
import os
import re
import struct
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..model import CanFrame
from .definitions import (
    ConflictKind, ConflictSeverity, DefinitionConflict, DefinitionDiagnostic,
    DefinitionReference, DefinitionSource, DefinitionSourceKind,
    DiagnosticSeverity, ValidationState,
)
from .signals import extract_little_endian_bits


PARSER_VERSION = "1.0"
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_SECTIONS = 50000
_OBJECT_RE = re.compile(r"^[0-9a-fA-F]{4,8}$")
_SUBOBJECT_RE = re.compile(r"^([0-9a-fA-F]{4,8})sub([0-9]+)$", re.I)
_NODE_TERM = re.compile(
    r"^(?:(?P<left>[-+]?(?:0[xX][0-9a-fA-F]+|\d+))\s*\+\s*)?"
    r"\$NODEID(?:\s*(?P<op>[+-])\s*(?P<right>(?:0[xX][0-9a-fA-F]+|\d+)))?$",
    re.I,
)


class DefinitionParseError(ValueError):
    def __init__(self, message: str,
                 diagnostics: Sequence[DefinitionDiagnostic] = ()):
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)


class CanopenObjectType(str, Enum):
    VAR = "VAR"
    ARRAY = "ARRAY"
    RECORD = "RECORD"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CanopenDataType:
    code: int
    name: str
    bit_length: Optional[int]
    signed: bool = False
    floating: bool = False
    textual: bool = False
    supported: bool = True


_DATA_TYPES: Dict[int, CanopenDataType] = {
    0x0001: CanopenDataType(0x0001, "BOOLEAN", 1),
    0x0002: CanopenDataType(0x0002, "INTEGER8", 8, signed=True),
    0x0003: CanopenDataType(0x0003, "INTEGER16", 16, signed=True),
    0x0004: CanopenDataType(0x0004, "INTEGER32", 32, signed=True),
    0x0005: CanopenDataType(0x0005, "UNSIGNED8", 8),
    0x0006: CanopenDataType(0x0006, "UNSIGNED16", 16),
    0x0007: CanopenDataType(0x0007, "UNSIGNED32", 32),
    0x0008: CanopenDataType(0x0008, "REAL32", 32, floating=True),
    0x0009: CanopenDataType(0x0009, "VISIBLE_STRING", None, textual=True),
    0x000A: CanopenDataType(0x000A, "OCTET_STRING", None),
    0x000F: CanopenDataType(0x000F, "DOMAIN", None, supported=False),
    0x0011: CanopenDataType(0x0011, "REAL64", 64, floating=True),
    0x0015: CanopenDataType(0x0015, "INTEGER64", 64, signed=True),
    0x001B: CanopenDataType(0x001B, "UNSIGNED64", 64),
}


@dataclass(frozen=True)
class NodeIdExpression:
    raw: str
    offset: int = 0

    def resolve(self, node_id: Optional[int]) -> Optional[int]:
        return None if node_id is None else node_id + self.offset


@dataclass(frozen=True)
class CanopenSubObject:
    index: int
    subindex: int
    name: str
    data_type: CanopenDataType
    access_type: str
    pdo_mappable: Optional[bool]
    default_value: Any = None
    configured_value: Any = None
    low_limit: Any = None
    high_limit: Any = None
    metadata: Tuple[Tuple[str, str], ...] = ()

    @property
    def location(self) -> str:
        return "0x{:04X}:{:02X}".format(self.index, self.subindex)


@dataclass(frozen=True)
class CanopenObject:
    index: int
    name: str
    object_type: CanopenObjectType
    data_type: CanopenDataType
    access_type: str
    pdo_mappable: Optional[bool]
    default_value: Any = None
    configured_value: Any = None
    low_limit: Any = None
    high_limit: Any = None
    subobjects: Tuple[CanopenSubObject, ...] = ()
    metadata: Tuple[Tuple[str, str], ...] = ()

    def subobject(self, subindex: int) -> Optional[CanopenSubObject]:
        for item in self.subobjects:
            if item.subindex == subindex:
                return item
        return None


@dataclass(frozen=True)
class CanopenObjectDictionary:
    objects: Tuple[CanopenObject, ...]

    def object(self, index: int) -> Optional[CanopenObject]:
        # Parser output is sorted. Binary search preserves the model's fully
        # immutable shape without making mapped decode O(dictionary size) for
        # every observed frame.
        low, high = 0, len(self.objects)
        while low < high:
            middle = (low + high) // 2
            item = self.objects[middle]
            if item.index < index:
                low = middle + 1
            else:
                high = middle
        if low < len(self.objects) and self.objects[low].index == index:
            return self.objects[low]
        return None

    def entry(self, index: int, subindex: int = 0) -> Optional[Any]:
        obj = self.object(index)
        if obj is None:
            return None
        if subindex == 0 and obj.object_type is CanopenObjectType.VAR:
            return obj
        return obj.subobject(subindex)


@dataclass(frozen=True)
class PdoMappingEntry:
    index: int
    subindex: int
    bit_length: int
    bit_offset: int

    @property
    def location(self) -> str:
        return "0x{:04X}:{:02X}".format(self.index, self.subindex)


@dataclass(frozen=True)
class PdoMapping:
    direction: str
    number: int
    mapping_index: int
    cob_id_value: Any
    entries: Tuple[PdoMappingEntry, ...]
    total_bits: int
    diagnostics: Tuple[DefinitionDiagnostic, ...] = ()

    def cob_id(self, node_id: Optional[int]) -> Optional[int]:
        value = self.cob_id_value
        if isinstance(value, NodeIdExpression):
            value = value.resolve(node_id)
        if not isinstance(value, int):
            return None
        # CiA 301 bit 31 disables the PDO. A disabled configured mapping must
        # not be applied to observed bytes merely because its low bits match.
        if value & 0x80000000:
            return None
        # Bits 30/29 carry RTR/frame-format configuration, not identifier bits.
        return value & 0x1FFFFFFF


@dataclass(frozen=True)
class CanopenDefinition:
    source: DefinitionSource
    validation_state: ValidationState
    diagnostics: Tuple[DefinitionDiagnostic, ...]
    dictionary: CanopenObjectDictionary
    pdo_mappings: Tuple[PdoMapping, ...]
    device_info: Tuple[Tuple[str, str], ...] = ()
    commissioning: Tuple[Tuple[str, str], ...] = ()
    extra_sections: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...] = ()

    @property
    def reference(self) -> DefinitionReference:
        return DefinitionReference(
            self.source, self.validation_state,
            tuple(item.display for item in self.diagnostics
                  if item.severity is not DiagnosticSeverity.INFO),
            len(self.dictionary.objects),
        )

    @property
    def configured_node_id(self) -> Optional[int]:
        raw = _pairs_get(self.commissioning, "NodeID")
        try:
            value = _parse_numeric(raw) if raw is not None else None
        except ValueError:
            return None
        return value if isinstance(value, int) and 1 <= value <= 127 else None


@dataclass(frozen=True)
class PdoDecodedValue:
    index: int
    subindex: int
    name: str
    raw: int
    value: Any
    bit_offset: int
    bit_length: int
    source: DefinitionSource


@dataclass(frozen=True)
class ObservedPdoDecode:
    timestamp: float
    channel: str
    can_id: int
    direction: str
    number: int
    raw_data: bytes
    values: Tuple[PdoDecodedValue, ...]
    source: DefinitionSource


@dataclass(frozen=True)
class SdoEnrichment:
    timestamp: float
    channel: str
    can_id: int
    direction: str
    index: int
    subindex: int
    name: str
    data_type: str
    raw_value: Optional[int]
    source: DefinitionSource
    conflicts: Tuple[DefinitionConflict, ...] = ()


def _pairs_get(pairs: Sequence[Tuple[str, str]], name: str) -> Optional[str]:
    needle = name.casefold()
    for key, value in pairs:
        if key.casefold() == needle:
            return value
    return None


def _parse_int(text: str) -> int:
    value = text.strip()
    sign = 1
    if value[:1] in ("+", "-"):
        if value[0] == "-":
            sign = -1
        value = value[1:].strip()
    base = 16 if value.lower().startswith("0x") else 10
    return sign * int(value, base)


def _parse_numeric(text: str) -> Any:
    value = str(text).strip()
    match = _NODE_TERM.fullmatch(value)
    if match:
        offset = 0
        if match.group("left"):
            offset += _parse_int(match.group("left"))
        if match.group("right"):
            amount = _parse_int(match.group("right"))
            offset += -amount if match.group("op") == "-" else amount
        return NodeIdExpression(value, offset)
    if "$" in value:
        raise ValueError("unsupported symbolic expression {!r}".format(value))
    return _parse_int(value)


def _field(items: Mapping[str, str], name: str, default: str = "") -> str:
    needle = name.casefold()
    for key, value in items.items():
        if key.casefold() == needle:
            return str(value).strip()
    return default


def _bool_field(items: Mapping[str, str], name: str) -> Optional[bool]:
    raw = _field(items, name)
    if not raw:
        return None
    if raw.casefold() in ("1", "true", "yes"):
        return True
    if raw.casefold() in ("0", "false", "no"):
        return False
    raise ValueError("{} must be 0 or 1".format(name))


def _data_type(items: Mapping[str, str], where: str,
               diagnostics: List[DefinitionDiagnostic]) -> CanopenDataType:
    raw = _field(items, "DataType")
    if not raw:
        return CanopenDataType(0, "UNSPECIFIED", None, supported=False)
    try:
        code = _parse_int(raw)
    except ValueError:
        diagnostics.append(DefinitionDiagnostic(
            DiagnosticSeverity.ERROR, "invalid-data-type",
            "invalid DataType value {!r}".format(raw), where))
        return CanopenDataType(0, "INVALID ({})".format(raw), None, supported=False)
    result = _DATA_TYPES.get(code)
    if result is None:
        diagnostics.append(DefinitionDiagnostic(
            DiagnosticSeverity.WARNING, "unsupported-data-type",
            "unsupported DataType 0x{:04X}; metadata is preserved".format(code), where))
        return CanopenDataType(code, "UNSUPPORTED 0x{:04X}".format(code), None,
                               supported=False)
    if not result.supported:
        diagnostics.append(DefinitionDiagnostic(
            DiagnosticSeverity.WARNING, "unsupported-decoding",
            "{} is visible but passive scalar decoding is unsupported".format(result.name),
            where))
    return result


def _typed_value(raw: str, data_type: CanopenDataType) -> Any:
    text = raw.strip()
    if not text:
        return None
    if data_type.textual:
        return text
    if data_type.name == "OCTET_STRING":
        compact = text.replace(" ", "")
        try:
            return bytes.fromhex(compact)
        except ValueError:
            raise ValueError("invalid OCTET_STRING {!r}".format(text))
    if data_type.floating and any(character in text for character in ".eE"):
        return float(text)
    return _parse_numeric(text)


def _object_type(items: Mapping[str, str]) -> CanopenObjectType:
    raw = _field(items, "ObjectType")
    if not raw:
        return CanopenObjectType.VAR
    try:
        return {7: CanopenObjectType.VAR, 8: CanopenObjectType.ARRAY,
                9: CanopenObjectType.RECORD}.get(_parse_int(raw),
                                                  CanopenObjectType.UNKNOWN)
    except ValueError:
        return CanopenObjectType.UNKNOWN


_KNOWN_ENTRY_FIELDS = frozenset(value.casefold() for value in (
    "ParameterName", "ObjectType", "DataType", "AccessType", "DefaultValue",
    "ParameterValue", "LowLimit", "HighLimit", "PDOMapping", "SubNumber",
))


def _entry_values(index: int, subindex: int, items: Mapping[str, str],
                  diagnostics: List[DefinitionDiagnostic]) -> Dict[str, Any]:
    where = "0x{:04X}:{:02X}".format(index, subindex)
    dtype = _data_type(items, where, diagnostics)

    def parsed(name: str) -> Any:
        raw = _field(items, name)
        if not raw:
            return None
        try:
            return _typed_value(raw, dtype)
        except (ValueError, OverflowError) as exc:
            diagnostics.append(DefinitionDiagnostic(
                DiagnosticSeverity.ERROR, "invalid-value",
                "{} is malformed: {}".format(name, exc), where))
            return raw

    try:
        pdo_mappable = _bool_field(items, "PDOMapping")
    except ValueError as exc:
        diagnostics.append(DefinitionDiagnostic(
            DiagnosticSeverity.ERROR, "invalid-pdo-mapping", str(exc), where))
        pdo_mappable = None
    compact = _field(items, "CompactSubObj")
    if compact and compact not in ("0", "0x0"):
        diagnostics.append(DefinitionDiagnostic(
            DiagnosticSeverity.WARNING, "unsupported-compact-object",
            "CompactSubObj is preserved but compact object expansion is unsupported",
            where))
    for resource_name in ("UploadFile", "DownloadFile"):
        resource = _field(items, resource_name)
        if resource:
            diagnostics.append(DefinitionDiagnostic(
                DiagnosticSeverity.WARNING, "unsupported-external-resource",
                "{}={!r} is preserved but is never opened automatically".format(
                    resource_name, resource), where))
    metadata = tuple(sorted((key, str(value)) for key, value in items.items()
                            if key.casefold() not in _KNOWN_ENTRY_FIELDS))
    return {
        "name": _field(items, "ParameterName", "Object 0x{:04X}".format(index)),
        "data_type": dtype,
        "access_type": _field(items, "AccessType", "unknown").casefold(),
        "pdo_mappable": pdo_mappable,
        "default_value": parsed("DefaultValue"),
        "configured_value": parsed("ParameterValue"),
        "low_limit": parsed("LowLimit"),
        "high_limit": parsed("HighLimit"),
        "metadata": metadata,
    }


def _parse_sections(parser: configparser.ConfigParser
                    ) -> Tuple[CanopenObjectDictionary, List[DefinitionDiagnostic],
                               Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...]]:
    diagnostics: List[DefinitionDiagnostic] = []
    parents: Dict[int, Dict[str, Any]] = {}
    children: Dict[int, List[CanopenSubObject]] = {}
    extras: List[Tuple[str, Tuple[Tuple[str, str], ...]]] = []
    standard = {"fileinfo", "deviceinfo", "devicecommissioning",
                "mandatoryobjects", "optionalobjects", "manufacturerobjects",
                "comments", "dummyusage"}
    for section in parser.sections():
        items = dict(parser.items(section, raw=True))
        submatch = _SUBOBJECT_RE.fullmatch(section)
        if submatch:
            index, subindex = int(submatch.group(1), 16), int(submatch.group(2), 10)
            values = _entry_values(index, subindex, items, diagnostics)
            children.setdefault(index, []).append(
                CanopenSubObject(index=index, subindex=subindex, **values))
        elif _OBJECT_RE.fullmatch(section):
            index = int(section, 16)
            values = _entry_values(index, 0, items, diagnostics)
            values["object_type"] = _object_type(items)
            values["declared_subnumber"] = _field(items, "SubNumber")
            parents[index] = values
        elif section.casefold() not in standard:
            extras.append((section, tuple(sorted((key, str(value))
                                                 for key, value in items.items()))))
            diagnostics.append(DefinitionDiagnostic(
                DiagnosticSeverity.WARNING, "unsupported-section",
                "unrecognised section was preserved as metadata", section))

    objects: List[CanopenObject] = []
    for index in sorted(set(parents) | set(children)):
        values = parents.get(index)
        subs = tuple(sorted(children.get(index, ()), key=lambda item: item.subindex))
        if values is None:
            diagnostics.append(DefinitionDiagnostic(
                DiagnosticSeverity.WARNING, "missing-parent",
                "subobjects exist without a parent object section",
                "0x{:04X}".format(index)))
            values = dict(
                name="Object 0x{:04X}".format(index),
                object_type=CanopenObjectType.RECORD,
                data_type=CanopenDataType(0, "UNSPECIFIED", None, supported=False),
                access_type="unknown", pdo_mappable=None, default_value=None,
                configured_value=None, low_limit=None, high_limit=None,
                metadata=(), declared_subnumber="",
            )
        declared = values.pop("declared_subnumber", "")
        if declared:
            try:
                count = _parse_int(declared)
                if count != len(subs):
                    diagnostics.append(DefinitionDiagnostic(
                        DiagnosticSeverity.WARNING, "subnumber-mismatch",
                        "SubNumber {} does not match {} parsed subobjects".format(
                            count, len(subs)), "0x{:04X}".format(index)))
            except ValueError:
                diagnostics.append(DefinitionDiagnostic(
                    DiagnosticSeverity.ERROR, "invalid-subnumber",
                    "SubNumber {!r} is not numeric".format(declared),
                    "0x{:04X}".format(index)))
        if values["object_type"] is CanopenObjectType.UNKNOWN:
            diagnostics.append(DefinitionDiagnostic(
                DiagnosticSeverity.WARNING, "unsupported-object-type",
                "unsupported ObjectType; object metadata remains visible",
                "0x{:04X}".format(index)))
        objects.append(CanopenObject(index=index, subobjects=subs, **values))
    return CanopenObjectDictionary(tuple(objects)), diagnostics, tuple(extras)


def _mapping_value(entry: Any) -> Any:
    return entry.configured_value if entry.configured_value is not None else entry.default_value


def _default_cob_id(direction: str, number: int, node_id: Optional[int] = None) -> Any:
    bases = (0x180, 0x280, 0x380, 0x480) if direction == "TPDO" else (
        0x200, 0x300, 0x400, 0x500)
    if not 1 <= number <= len(bases):
        return None
    base = bases[number - 1]
    return base + node_id if node_id is not None else NodeIdExpression(
        "0x{:X}+$NODEID".format(base), base)


def _build_mappings(dictionary: CanopenObjectDictionary
                    ) -> Tuple[Tuple[PdoMapping, ...], List[DefinitionDiagnostic]]:
    mappings: List[PdoMapping] = []
    all_diagnostics: List[DefinitionDiagnostic] = []
    for obj in dictionary.objects:
        if 0x1600 <= obj.index <= 0x17FF:
            direction, number, communication = "RPDO", obj.index - 0x1600 + 1, obj.index - 0x200
        elif 0x1A00 <= obj.index <= 0x1BFF:
            direction, number, communication = "TPDO", obj.index - 0x1A00 + 1, obj.index - 0x200
        else:
            continue
        local: List[DefinitionDiagnostic] = []
        count_entry = obj.subobject(0)
        count_value = _mapping_value(count_entry) if count_entry is not None else None
        try:
            count = int(count_value) if count_value is not None else len(
                [entry for entry in obj.subobjects if entry.subindex > 0])
        except (TypeError, ValueError):
            count = 0
            local.append(DefinitionDiagnostic(
                DiagnosticSeverity.ERROR, "invalid-mapping-count",
                "mapping count is not numeric", "0x{:04X}:00".format(obj.index)))
        entries: List[PdoMappingEntry] = []
        offset = 0
        for subindex in range(1, max(0, count) + 1):
            sub = obj.subobject(subindex)
            raw = _mapping_value(sub) if sub is not None else None
            if not isinstance(raw, int):
                local.append(DefinitionDiagnostic(
                    DiagnosticSeverity.ERROR, "missing-mapping-entry",
                    "mapping entry {} is missing or non-numeric".format(subindex),
                    "0x{:04X}:{:02X}".format(obj.index, subindex)))
                continue
            target_index = (raw >> 16) & 0xFFFF
            target_subindex = (raw >> 8) & 0xFF
            length = raw & 0xFF
            mapped = dictionary.entry(target_index, target_subindex)
            location = "0x{:04X}:{:02X}".format(obj.index, subindex)
            if length <= 0 or length > 64:
                local.append(DefinitionDiagnostic(
                    DiagnosticSeverity.ERROR, "invalid-mapping-length",
                    "mapped bit length {} is outside 1..64".format(length), location))
            if mapped is None:
                local.append(DefinitionDiagnostic(
                    DiagnosticSeverity.WARNING, "missing-mapped-object",
                    "mapping references missing {}".format(
                        "0x{:04X}:{:02X}".format(target_index, target_subindex)), location))
            else:
                if mapped.pdo_mappable is False:
                    local.append(DefinitionDiagnostic(
                        DiagnosticSeverity.WARNING, "pdo-not-permitted",
                        "referenced object declares PDOMapping=0", location))
                expected = mapped.data_type.bit_length
                if expected is not None and expected != length:
                    local.append(DefinitionDiagnostic(
                        DiagnosticSeverity.WARNING, "mapping-type-length",
                        "mapping uses {} bits but {} declares {} bits".format(
                            length, mapped.data_type.name, expected), location))
            entries.append(PdoMappingEntry(
                target_index, target_subindex, length, offset))
            offset += max(0, length)
        if offset > 64:
            local.append(DefinitionDiagnostic(
                DiagnosticSeverity.ERROR, "mapping-too-large",
                "total mapped size is {} bits; classic CAN PDO maximum is 64".format(offset),
                "0x{:04X}".format(obj.index)))
        comm = dictionary.object(communication)
        cob_entry = comm.subobject(1) if comm is not None else None
        cob_id = _mapping_value(cob_entry) if cob_entry is not None else _default_cob_id(
            direction, number)
        mappings.append(PdoMapping(direction, number, obj.index, cob_id,
                                   tuple(entries), offset, tuple(local)))
        all_diagnostics.extend(local)
    return tuple(mappings), all_diagnostics


def _list_references(parser: configparser.ConfigParser,
                     dictionary: CanopenObjectDictionary
                     ) -> List[DefinitionDiagnostic]:
    diagnostics: List[DefinitionDiagnostic] = []
    for section in ("MandatoryObjects", "OptionalObjects", "ManufacturerObjects"):
        if not parser.has_section(section):
            continue
        items = dict(parser.items(section, raw=True))
        declared_raw = _field(items, "SupportedObjects")
        listed = [(key, raw) for key, raw in items.items()
                  if key.casefold() != "supportedobjects"]
        if declared_raw:
            try:
                declared = _parse_int(declared_raw)
                if declared != len(listed):
                    diagnostics.append(DefinitionDiagnostic(
                        DiagnosticSeverity.WARNING, "object-list-count-mismatch",
                        "SupportedObjects {} does not match {} listed entries".format(
                            declared, len(listed)), section))
            except ValueError:
                diagnostics.append(DefinitionDiagnostic(
                    DiagnosticSeverity.ERROR, "invalid-supported-objects",
                    "SupportedObjects {!r} is not numeric".format(declared_raw), section))
        for key, raw in items.items():
            if key.casefold() == "supportedobjects":
                continue
            try:
                index = _parse_int(raw)
            except ValueError:
                diagnostics.append(DefinitionDiagnostic(
                    DiagnosticSeverity.ERROR, "invalid-object-list-entry",
                    "object list value {!r} is not numeric".format(raw), section))
                continue
            if dictionary.object(index) is None:
                diagnostics.append(DefinitionDiagnostic(
                    DiagnosticSeverity.WARNING, "listed-object-missing",
                    "listed object 0x{:04X} has no section".format(index), section))
    return diagnostics


def _state(diagnostics: Sequence[DefinitionDiagnostic]) -> ValidationState:
    if any(item.severity is DiagnosticSeverity.ERROR for item in diagnostics):
        return ValidationState.INVALID
    if any(item.code.startswith("unsupported") for item in diagnostics):
        return ValidationState.UNSUPPORTED_FEATURES
    if any(item.severity is DiagnosticSeverity.WARNING for item in diagnostics):
        return ValidationState.VALID_WITH_WARNINGS
    return ValidationState.VALID


def parse_definition_bytes(data: bytes, display_name: str = "definition.eds",
                           location: str = "",
                           imported_at: Optional[str] = None) -> CanopenDefinition:
    if len(data) > MAX_FILE_BYTES:
        raise DefinitionParseError(
            "definition is larger than the {} MiB safety limit".format(
                MAX_FILE_BYTES // (1024 * 1024)))
    digest = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    parser = configparser.ConfigParser(interpolation=None, strict=True,
                                       empty_lines_in_values=False)
    parser.optionxform = str
    try:
        parser.read_string(text, source=display_name)
    except (configparser.Error, UnicodeError) as exc:
        diagnostic = DefinitionDiagnostic(
            DiagnosticSeverity.ERROR, "invalid-ini", str(exc), display_name)
        raise DefinitionParseError("Could not parse {}: {}".format(display_name, exc),
                                   (diagnostic,)) from exc
    if len(parser.sections()) > MAX_SECTIONS:
        raise DefinitionParseError(
            "definition contains more than {:,} sections".format(MAX_SECTIONS))
    extension = os.path.splitext(display_name)[1].casefold()
    kind = DefinitionSourceKind.DCF if extension == ".dcf" else DefinitionSourceKind.EDS
    file_info = tuple(parser.items("FileInfo", raw=True)) if parser.has_section("FileInfo") else ()
    version = _pairs_get(file_info, "FileVersion") or _pairs_get(file_info, "EDSVersion") or ""
    source = DefinitionSource(
        kind=kind, display_name=display_name, location=location,
        content_hash=digest,
        imported_at=imported_at or datetime.now(timezone.utc).isoformat(),
        format_version=version, parser_version=PARSER_VERSION,
    )
    dictionary, diagnostics, extras = _parse_sections(parser)
    diagnostics.extend(_list_references(parser, dictionary))
    mappings, mapping_diagnostics = _build_mappings(dictionary)
    diagnostics.extend(mapping_diagnostics)
    if not parser.has_section("FileInfo"):
        diagnostics.append(DefinitionDiagnostic(
            DiagnosticSeverity.WARNING, "missing-file-info",
            "FileInfo section is absent", display_name))
    device = tuple(parser.items("DeviceInfo", raw=True)) if parser.has_section("DeviceInfo") else ()
    commissioning = (tuple(parser.items("DeviceCommissioning", raw=True))
                     if parser.has_section("DeviceCommissioning") else ())
    return CanopenDefinition(source, _state(diagnostics), tuple(diagnostics),
                             dictionary, mappings, device, commissioning, extras)


def parse_definition_file(path: str) -> CanopenDefinition:
    absolute = os.path.abspath(path)
    try:
        size = os.path.getsize(absolute)
    except OSError as exc:
        raise DefinitionParseError("Cannot read {}: {}".format(path, exc)) from exc
    if size > MAX_FILE_BYTES:
        raise DefinitionParseError(
            "definition is larger than the {} MiB safety limit".format(
                MAX_FILE_BYTES // (1024 * 1024)))
    try:
        with open(absolute, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise DefinitionParseError("Cannot read {}: {}".format(path, exc)) from exc
    return parse_definition_bytes(data, os.path.basename(absolute), absolute)


class DefinitionCache:
    """Content/parser-version cache; changed paths never masquerade as old files."""

    def __init__(self):
        self._items: Dict[Tuple[str, str], CanopenDefinition] = {}

    def parse_file(self, path: str) -> CanopenDefinition:
        absolute = os.path.abspath(path)
        try:
            with open(absolute, "rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            raise DefinitionParseError("Cannot read {}: {}".format(path, exc)) from exc
        if len(data) > MAX_FILE_BYTES:
            raise DefinitionParseError("definition exceeds the file-size safety limit")
        digest = hashlib.sha256(data).hexdigest()
        key = (digest, PARSER_VERSION)
        result = self._items.get(key)
        if result is None:
            result = parse_definition_bytes(data, os.path.basename(absolute), absolute)
            self._items[key] = result
            return result
        # Cache normalized immutable contents, not path provenance. Identical
        # bytes imported from a second path retain that path/name/time while
        # sharing the expensive dictionary/mapping parse artifacts.
        source = replace(
            result.source, display_name=os.path.basename(absolute),
            location=absolute, imported_at=datetime.now(timezone.utc).isoformat())
        return replace(result, source=source)

    def resolve(self, reference: DefinitionReference) -> Tuple[Optional[CanopenDefinition],
                                                                ValidationState, str]:
        path = reference.source.location
        if not path or not os.path.isfile(path):
            return None, ValidationState.MISSING, "source file is missing"
        try:
            result = self.parse_file(path)
        except DefinitionParseError as exc:
            return None, ValidationState.INVALID, str(exc)
        if result.source.content_hash != reference.source.content_hash:
            return None, ValidationState.CHANGED, (
                "source path now contains different data; explicit re-import is required")
        # Retain the original import timestamp/provenance identity on reload.
        result = replace(result, source=reference.source)
        return result, result.validation_state, ""

    @property
    def size(self) -> int:
        return len(self._items)


DEFAULT_DEFINITION_CACHE = DefinitionCache()


def _decode_raw(raw: int, bit_length: int, data_type: CanopenDataType) -> Any:
    if data_type.name == "BOOLEAN":
        return bool(raw)
    if data_type.signed and bit_length:
        sign = 1 << (bit_length - 1)
        return raw - (1 << bit_length) if raw & sign else raw
    if data_type.floating and bit_length in (32, 64):
        payload = raw.to_bytes(bit_length // 8, "little")
        return struct.unpack("<f" if bit_length == 32 else "<d", payload)[0]
    return raw


def _mapping_conflicts(mapping: PdoMapping, definition: CanopenDefinition,
                       observed_length: Optional[int] = None
                       ) -> List[DefinitionConflict]:
    conflicts: List[DefinitionConflict] = []
    source = definition.source.display_name
    for diagnostic in mapping.diagnostics:
        kind = (ConflictKind.OBJECT_MISSING if "missing" in diagnostic.code
                else ConflictKind.TYPE_LENGTH if "length" in diagnostic.code
                else ConflictKind.PDO_PERMISSION)
        conflicts.append(DefinitionConflict(
            kind, ConflictSeverity.ERROR if diagnostic.severity is DiagnosticSeverity.ERROR
            else ConflictSeverity.WARNING, source, "Object Dictionary",
            diagnostic.location, diagnostic.message))
    if observed_length is not None and mapping.total_bits > observed_length * 8:
        conflicts.append(DefinitionConflict(
            ConflictKind.OBSERVED_LENGTH, ConflictSeverity.ERROR, source,
            "observed CAN payload", "{}{}".format(mapping.direction, mapping.number),
            "definition maps {} bits but observed payload contains {} bits".format(
                mapping.total_bits, observed_length * 8)))
    return conflicts


def decode_observed_pdos(definition: CanopenDefinition, node_id: int,
                         frames: Iterable[CanFrame], channel: str = ""
                         ) -> Tuple[Tuple[ObservedPdoDecode, ...],
                                    Tuple[DefinitionConflict, ...]]:
    if definition.validation_state is ValidationState.INVALID:
        return (), (DefinitionConflict(
            ConflictKind.VALIDATION, ConflictSeverity.ERROR,
            definition.source.display_name, "definition validation",
            definition.source.display_name,
            "invalid definition is visible but was not used to decode observed traffic"),)
    by_cob: Dict[int, List[PdoMapping]] = {}
    conflicts: List[DefinitionConflict] = []
    usable_frames = tuple(frame for frame in frames
                          if not channel or frame.channel == channel)
    observed_ids = {frame.arb_id for frame in usable_frames
                    if not frame.is_extended and not frame.is_error_frame}
    for mapping in definition.pdo_mappings:
        cob_id = mapping.cob_id(node_id)
        if cob_id is None:
            continue
        by_cob.setdefault(cob_id, []).append(mapping)
        conflicts.extend(_mapping_conflicts(mapping, definition))
        standard = _default_cob_id(mapping.direction, mapping.number, node_id)
        if isinstance(standard, int) and cob_id != standard and standard in observed_ids:
            conflicts.append(DefinitionConflict(
                ConflictKind.COB_ID, ConflictSeverity.WARNING,
                definition.source.display_name, "observed CAN traffic",
                "{}{}".format(mapping.direction, mapping.number),
                "definition configures COB-ID 0x{:03X}, while associated-node "
                "traffic was observed at standard COB-ID 0x{:03X}".format(
                    cob_id, standard)))
    decoded: List[ObservedPdoDecode] = []
    seen_length_conflicts = set()
    for frame in usable_frames:
        if frame.is_extended or frame.is_error_frame or frame.is_remote_frame:
            continue
        if channel and frame.channel != channel:
            continue
        matches = by_cob.get(frame.arb_id, ())
        for mapping in matches:
            if mapping.total_bits > len(frame.data) * 8:
                key = (mapping.mapping_index, len(frame.data))
                if key not in seen_length_conflicts:
                    conflicts.extend(_mapping_conflicts(mapping, definition, len(frame.data)))
                    seen_length_conflicts.add(key)
                continue
            values: List[PdoDecodedValue] = []
            valid = True
            for entry in mapping.entries:
                target = definition.dictionary.entry(entry.index, entry.subindex)
                raw = extract_little_endian_bits(
                    frame.data, entry.bit_offset, entry.bit_length)
                if target is None or raw is None:
                    valid = False
                    break
                values.append(PdoDecodedValue(
                    entry.index, entry.subindex, target.name, raw,
                    _decode_raw(raw, entry.bit_length, target.data_type),
                    entry.bit_offset, entry.bit_length, definition.source))
            if valid:
                decoded.append(ObservedPdoDecode(
                    frame.timestamp, frame.channel, frame.arb_id,
                    mapping.direction, mapping.number, frame.data,
                    tuple(values), definition.source))
    return tuple(decoded), tuple(conflicts)


def enrich_sdo_observations(definition: CanopenDefinition, node_id: int,
                            frames: Iterable[CanFrame], channel: str = ""
                            ) -> Tuple[SdoEnrichment, ...]:
    if definition.validation_state is ValidationState.INVALID:
        return ()
    result: List[SdoEnrichment] = []
    for frame in frames:
        if channel and frame.channel != channel:
            continue
        if frame.is_extended or len(frame.data) != 8:
            continue
        if frame.arb_id == 0x600 + node_id:
            direction = "request"
        elif frame.arb_id == 0x580 + node_id:
            direction = "response"
        else:
            continue
        command = frame.data[0]
        if command not in (0x20, 0x21, 0x22, 0x23, 0x27, 0x2B, 0x2F,
                           0x40, 0x41, 0x42, 0x43, 0x47, 0x4B, 0x4F, 0x60, 0x80):
            continue
        index = frame.data[1] | frame.data[2] << 8
        subindex = frame.data[3]
        entry = definition.dictionary.entry(index, subindex)
        name = entry.name if entry is not None else "Unknown object"
        dtype = entry.data_type.name if entry is not None else "unknown"
        expedited_size = {0x2F: 1, 0x4F: 1, 0x2B: 2, 0x4B: 2,
                          0x27: 3, 0x47: 3, 0x23: 4, 0x43: 4}.get(command)
        raw = (int.from_bytes(frame.data[4:4 + expedited_size], "little")
               if expedited_size else None)
        conflicts: List[DefinitionConflict] = []
        if entry is None:
            conflicts.append(DefinitionConflict(
                ConflictKind.OBJECT_MISSING, ConflictSeverity.WARNING,
                definition.source.display_name, "observed SDO",
                "0x{:04X}:{:02X}".format(index, subindex),
                "observed SDO index/subindex is absent from the definition"))
        elif expedited_size and entry.data_type.bit_length is not None \
                and expedited_size * 8 != entry.data_type.bit_length:
            conflicts.append(DefinitionConflict(
                ConflictKind.TYPE_LENGTH, ConflictSeverity.WARNING,
                definition.source.display_name, "observed SDO",
                "0x{:04X}:{:02X}".format(index, subindex),
                "expedited value is {} bits but definition declares {} bits".format(
                    expedited_size * 8, entry.data_type.bit_length)))
        if entry is not None and direction == "request" and command != 0x40 \
                and entry.access_type in ("ro", "const"):
            conflicts.append(DefinitionConflict(
                ConflictKind.ACCESS, ConflictSeverity.WARNING,
                definition.source.display_name, "observed SDO request",
                "0x{:04X}:{:02X}".format(index, subindex),
                "observed download direction conflicts with {} access".format(
                    entry.access_type)))
        result.append(SdoEnrichment(
            frame.timestamp, frame.channel, frame.arb_id, direction, index,
            subindex, name, dtype, raw, definition.source, tuple(conflicts)))
    return tuple(result)


def definition_conflicts(definitions: Sequence[CanopenDefinition]
                         ) -> Tuple[DefinitionConflict, ...]:
    """Deterministic, non-destructive object conflicts across definitions."""
    result: List[DefinitionConflict] = []
    for left_index, left in enumerate(definitions):
        for right in definitions[left_index + 1:]:
            for obj in left.dictionary.objects:
                other = right.dictionary.object(obj.index)
                if other is None:
                    continue
                if (obj.name, obj.data_type.code) == (other.name, other.data_type.code):
                    continue
                result.append(DefinitionConflict(
                    ConflictKind.DEFINITION_OVERLAP, ConflictSeverity.WARNING,
                    left.source.display_name, right.source.display_name,
                    "0x{:04X}".format(obj.index),
                    "definitions disagree: {!r}/{} versus {!r}/{}".format(
                        obj.name, obj.data_type.name, other.name,
                        other.data_type.name)))
    return tuple(result)


def signal_overlap_conflicts(definition: CanopenDefinition, node_id: int,
                             signals: Sequence[Any]
                             ) -> Tuple[DefinitionConflict, ...]:
    """Report DBC/manual interpretations sharing bytes with mapped PDO data."""
    result: List[DefinitionConflict] = []
    for mapping in definition.pdo_mappings:
        cob_id = mapping.cob_id(node_id)
        if cob_id is None:
            continue
        for signal in signals:
            if getattr(signal, "can_id", None) != cob_id:
                continue
            start = int(getattr(signal, "start", 0))
            end = start + int(getattr(signal, "length", 0))
            for entry in mapping.entries:
                if start < entry.bit_offset + entry.bit_length and entry.bit_offset < end:
                    source_kind = getattr(signal, "source_kind", "MANUAL")
                    kind = (ConflictKind.DBC_OVERLAP if source_kind == "DBC"
                            else ConflictKind.MANUAL_OVERLAP)
                    result.append(DefinitionConflict(
                        kind, ConflictSeverity.WARNING,
                        definition.source.display_name, source_kind,
                        "CAN 0x{:03X} bits {}..{}".format(
                            cob_id, max(start, entry.bit_offset),
                            min(end, entry.bit_offset + entry.bit_length) - 1),
                        "{} and CANopen {} both interpret this observed region".format(
                            getattr(signal, "name", "signal"), entry.location)))
    return tuple(result)


__all__ = [
    "CanopenDataType", "CanopenDefinition", "CanopenObject",
    "CanopenObjectDictionary", "CanopenObjectType", "CanopenSubObject",
    "DEFAULT_DEFINITION_CACHE", "DefinitionCache", "DefinitionParseError",
    "MAX_FILE_BYTES", "MAX_SECTIONS", "NodeIdExpression", "ObservedPdoDecode",
    "PARSER_VERSION", "PdoDecodedValue", "PdoMapping", "PdoMappingEntry",
    "SdoEnrichment", "decode_observed_pdos", "definition_conflicts",
    "enrich_sdo_observations", "parse_definition_bytes", "parse_definition_file",
    "signal_overlap_conflicts",
]
