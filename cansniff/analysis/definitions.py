"""Shared provenance and conflict vocabulary for imported definitions.

Definitions are interpretations of captured bytes.  These immutable records
make their origin and validation state explicit without coupling a parser to
Qt, a capture source, or a live CAN handle.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class DefinitionSourceKind(str, Enum):
    DBC = "DBC"
    MANUAL = "MANUAL"
    EDS = "EDS"
    DCF = "DCF"
    J1939 = "J1939"
    BUILTIN = "BUILTIN"


class ValidationState(str, Enum):
    VALID = "VALID"
    VALID_WITH_WARNINGS = "VALID_WITH_WARNINGS"
    INVALID = "INVALID"
    UNSUPPORTED_FEATURES = "UNSUPPORTED_FEATURES"
    MISSING = "MISSING"
    CHANGED = "CHANGED"

    @property
    def display(self) -> str:
        return self.value.replace("_", " ").title()


class DiagnosticSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class ConflictSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class ConflictKind(str, Enum):
    VALIDATION = "VALIDATION"
    OBSERVED_LENGTH = "OBSERVED_LENGTH"
    OBJECT_MISSING = "OBJECT_MISSING"
    SUBOBJECT_MISSING = "SUBOBJECT_MISSING"
    TYPE_LENGTH = "TYPE_LENGTH"
    PDO_PERMISSION = "PDO_PERMISSION"
    COB_ID = "COB_ID"
    NODE_ID = "NODE_ID"
    ACCESS = "ACCESS"
    DBC_OVERLAP = "DBC_OVERLAP"
    MANUAL_OVERLAP = "MANUAL_OVERLAP"
    DEFINITION_OVERLAP = "DEFINITION_OVERLAP"


@dataclass(frozen=True)
class DefinitionDiagnostic:
    severity: DiagnosticSeverity
    code: str
    message: str
    location: str = ""

    @property
    def display(self) -> str:
        return "{}: {}".format(self.location, self.message) if self.location else self.message


@dataclass(frozen=True)
class DefinitionSource:
    kind: DefinitionSourceKind
    display_name: str
    location: str
    content_hash: str
    imported_at: str
    format_version: str = ""
    parser_version: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "display_name": self.display_name,
            "location": self.location,
            "content_hash": self.content_hash,
            "imported_at": self.imported_at,
            "format_version": self.format_version,
            "parser_version": self.parser_version,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "DefinitionSource":
        try:
            kind = DefinitionSourceKind(str(raw.get("kind", "EDS")))
        except ValueError:
            kind = DefinitionSourceKind.EDS
        return cls(
            kind=kind,
            display_name=str(raw.get("display_name", "") or ""),
            location=str(raw.get("location", "") or ""),
            content_hash=str(raw.get("content_hash", "") or ""),
            imported_at=str(raw.get("imported_at", "") or ""),
            format_version=str(raw.get("format_version", "") or ""),
            parser_version=str(raw.get("parser_version", "") or ""),
        )


@dataclass(frozen=True)
class DefinitionReference:
    """Small persistent record; parsed file contents remain outside config."""

    source: DefinitionSource
    validation_state: ValidationState
    warnings: Tuple[str, ...] = ()
    object_count: int = 0

    @property
    def identity(self) -> str:
        return self.source.content_hash

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "validation_state": self.validation_state.value,
            "warnings": list(self.warnings),
            "object_count": self.object_count,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "DefinitionReference":
        try:
            state = ValidationState(str(raw.get("validation_state", "INVALID")))
        except ValueError:
            state = ValidationState.INVALID
        try:
            object_count = max(0, int(raw.get("object_count", 0) or 0))
        except (TypeError, ValueError):
            object_count = 0
        return cls(
            source=DefinitionSource.from_dict(raw.get("source", {})
                                              if isinstance(raw.get("source"), dict)
                                              else {}),
            validation_state=state,
            warnings=tuple(str(value) for value in raw.get("warnings", ()) or ()),
            object_count=object_count,
        )


@dataclass(frozen=True)
class CanopenNodeAssociation:
    definition_hash: str
    node_id: int
    channel: str = ""
    method: str = "MANUAL"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "definition_hash": self.definition_hash,
            "node_id": self.node_id,
            "channel": self.channel,
            "method": self.method,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "CanopenNodeAssociation":
        try:
            node_id = int(raw.get("node_id", 0) or 0)
        except (TypeError, ValueError):
            node_id = 0
        return cls(
            definition_hash=str(raw.get("definition_hash", "") or ""),
            node_id=node_id,
            channel=str(raw.get("channel", "") or ""),
            method=str(raw.get("method", "MANUAL") or "MANUAL"),
        )


@dataclass(frozen=True)
class DefinitionConflict:
    kind: ConflictKind
    severity: ConflictSeverity
    source_a: str
    source_b_or_observation: str
    location: str
    explanation: str


@dataclass(frozen=True)
class J1939SpnDefinition:
    """One provenance-bound SPN using zero-based sequential LSB numbering."""

    spn: int
    name: str
    start_bit: int
    bit_length: int
    source: DefinitionSource
    description: str = ""
    byte_order: str = "little_endian"
    signed: bool = False
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    states: Tuple[Tuple[int, str], ...] = ()
    # raw, label, validity (for example NOT_AVAILABLE or ERROR)
    special_values: Tuple[Tuple[int, str, str], ...] = ()
    unsupported_reason: str = ""


@dataclass(frozen=True)
class J1939PgnDefinition:
    pgn: int
    name: str
    spns: Tuple[J1939SpnDefinition, ...]
    source: DefinitionSource
    description: str = ""
    minimum_length: int = 0
    maximum_length: int = 1785
    transport: str = "either"


__all__ = [
    "CanopenNodeAssociation", "ConflictKind", "ConflictSeverity",
    "DefinitionConflict", "DefinitionDiagnostic", "DefinitionReference",
    "DefinitionSource", "DefinitionSourceKind", "DiagnosticSeverity",
    "J1939PgnDefinition", "J1939SpnDefinition", "ValidationState",
]
