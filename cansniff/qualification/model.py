"""Evidence-derived qualification vocabulary and machine-readable records."""

from __future__ import annotations

import platform
import math
import sys
from dataclasses import dataclass, field
from enum import Enum
from importlib import metadata
from typing import Any, Dict, Iterable, Optional, Tuple

from .. import __version__


QUALIFICATION_SCHEMA_VERSION = 1


class QualificationLevel(str, Enum):
    UNTESTED = "UNTESTED"
    SOFTWARE_TESTED = "SOFTWARE_TESTED"
    CAPTURE_VALIDATED = "CAPTURE_VALIDATED"
    HARDWARE_TESTED = "HARDWARE_TESTED"
    ELECTRICALLY_PASSIVE_VERIFIED = "ELECTRICALLY_PASSIVE_VERIFIED"

    @property
    def rank(self) -> int:
        return tuple(QualificationLevel).index(self)


class QualificationStatus(str, Enum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    REFUSED = "REFUSED"


@dataclass(frozen=True)
class EnvironmentReport:
    os: str
    python: str
    application_version: str
    application_commit: str
    pyside6: str
    python_can: str
    cantools: str
    cpu_count: Optional[int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "os": self.os, "python": self.python,
            "application_version": self.application_version,
            "application_commit": self.application_commit,
            "pyside6": self.pyside6, "python_can": self.python_can,
            "cantools": self.cantools, "cpu_count": self.cpu_count,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "EnvironmentReport":
        if not isinstance(raw, dict):
            raise ValueError("environment must be an object")
        return cls(*(_bounded_text(raw.get(name, ""), "environment." + name, 1000)
                     for name in ("os", "python", "application_version",
                                  "application_commit", "pyside6", "python_can",
                                  "cantools")),
                   _optional_nonnegative_int(raw.get("cpu_count"), "cpu_count"))


@dataclass(frozen=True)
class EvidenceReference:
    kind: str
    reference: str
    sha256: str = ""
    notes: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "reference": self.reference,
                "sha256": self.sha256, "notes": self.notes}

    @classmethod
    def from_dict(cls, raw: Any) -> "EvidenceReference":
        if not isinstance(raw, dict):
            raise ValueError("evidence reference must be an object")
        digest = _digest(raw.get("sha256", ""), "evidence sha256", allow_empty=True)
        return cls(_bounded_text(raw.get("kind", ""), "evidence kind", 100),
                   _bounded_text(raw.get("reference", ""), "evidence reference", 32768),
                   digest,
                   _bounded_text(raw.get("notes", ""), "evidence notes", 10000))


@dataclass(frozen=True)
class QualificationRecord:
    record_id: str
    capability: str
    level: QualificationLevel
    status: QualificationStatus
    test_id: str
    timestamp: str
    environment: EnvironmentReport
    backend: str = ""
    adapter: str = ""
    hardware_revision: str = ""
    firmware_version: str = ""
    driver_version: str = ""
    channel: str = ""
    bitrate: Optional[int] = None
    fd: Optional[bool] = None
    duration_seconds: Optional[float] = None
    frame_count: Optional[int] = None
    evidence: Tuple[EvidenceReference, ...] = field(default_factory=tuple)
    limitations: Tuple[str, ...] = field(default_factory=tuple)
    provenance: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": QUALIFICATION_SCHEMA_VERSION,
            "record_id": self.record_id, "capability": self.capability,
            "level": self.level.value, "status": self.status.value,
            "test_id": self.test_id, "timestamp": self.timestamp,
            "environment": self.environment.to_dict(), "backend": self.backend,
            "adapter": self.adapter, "hardware_revision": self.hardware_revision,
            "firmware_version": self.firmware_version,
            "driver_version": self.driver_version, "channel": self.channel,
            "bitrate": self.bitrate, "fd": self.fd,
            "duration_seconds": self.duration_seconds,
            "frame_count": self.frame_count,
            "evidence": [item.to_dict() for item in self.evidence],
            "limitations": list(self.limitations),
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "QualificationRecord":
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError("qualification record schema_version must be 1")
        evidence = _bounded_list(raw.get("evidence", []), "evidence", 1000)
        limitations = _bounded_list(raw.get("limitations", []), "limitations", 1000)
        provenance = raw.get("provenance", {})
        if not isinstance(provenance, dict) or len(provenance) > 1000:
            raise ValueError("provenance must be a bounded object")
        try:
            level = QualificationLevel(str(raw.get("level", "")))
            status = QualificationStatus(str(raw.get("status", "")))
        except ValueError as exc:
            raise ValueError("qualification level/status is unsupported") from exc
        duration = raw.get("duration_seconds")
        if duration is not None:
            if isinstance(duration, bool):
                raise ValueError("duration_seconds must be a finite non-negative number")
            duration = float(duration)
            if duration < 0 or not math.isfinite(duration):
                raise ValueError("duration_seconds must be a finite non-negative number")
        fd = raw.get("fd")
        if fd is not None and not isinstance(fd, bool):
            raise ValueError("fd must be boolean or null")
        return cls(
            _bounded_text(raw.get("record_id", ""), "record_id", 200),
            _bounded_text(raw.get("capability", ""), "capability", 500),
            level, status, _bounded_text(raw.get("test_id", ""), "test_id", 500),
            _bounded_text(raw.get("timestamp", ""), "timestamp", 100),
            EnvironmentReport.from_dict(raw.get("environment")),
            *(_bounded_text(raw.get(name, ""), name, 1000) for name in (
                "backend", "adapter", "hardware_revision", "firmware_version",
                "driver_version", "channel")),
            _optional_nonnegative_int(raw.get("bitrate"), "bitrate"), fd, duration,
            _optional_nonnegative_int(raw.get("frame_count"), "frame_count"),
            tuple(EvidenceReference.from_dict(item) for item in evidence),
            tuple(_bounded_text(item, "limitation", 10000) for item in limitations),
            tuple(sorted((_bounded_text(key, "provenance key", 200),
                          _bounded_text(value, "provenance value", 5000))
                         for key, value in provenance.items())),
        )


@dataclass(frozen=True)
class QualificationMatrixEntry:
    capability: str
    level: QualificationLevel
    passing_records: int
    partial_records: int
    failed_records: int
    evidence_record_ids: Tuple[str, ...]
    limitations: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {"capability": self.capability, "level": self.level.value,
                "passing_records": self.passing_records,
                "partial_records": self.partial_records,
                "failed_records": self.failed_records,
                "evidence_record_ids": list(self.evidence_record_ids),
                "limitations": list(self.limitations)}


def build_qualification_matrix(
        capabilities: Iterable[str], records: Iterable[QualificationRecord]
        ) -> Tuple[QualificationMatrixEntry, ...]:
    values = tuple(records)
    entries = []
    for capability in sorted(set(str(item) for item in capabilities)):
        relevant = [item for item in values if item.capability == capability]
        passing = [item for item in relevant
                   if item.status is QualificationStatus.PASS]
        level = max((item.level for item in passing),
                    default=QualificationLevel.UNTESTED,
                    key=lambda item: item.rank)
        entries.append(QualificationMatrixEntry(
            capability, level, len(passing),
            sum(item.status is QualificationStatus.PARTIAL for item in relevant),
            sum(item.status is QualificationStatus.FAIL for item in relevant),
            tuple(item.record_id for item in relevant),
            tuple(dict.fromkeys(value for item in relevant
                                for value in item.limitations))))
    return tuple(entries)


def collect_environment(application_commit: str = "unknown") -> EnvironmentReport:
    def version(distribution: str) -> str:
        try:
            return metadata.version(distribution)
        except metadata.PackageNotFoundError:
            return "unavailable"
    import os
    return EnvironmentReport(
        platform.platform(), platform.python_version(), __version__,
        application_commit or "unknown", version("PySide6"),
        version("python-can"), version("cantools"), os.cpu_count())


def _bounded_text(value: Any, where: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("{} must be text no longer than {}".format(where, limit))
    return value


def _bounded_list(value: Any, where: str, limit: int) -> list:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("{} must be a list with at most {} entries".format(where, limit))
    return value


def _optional_nonnegative_int(value: Any, where: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("{} must be a non-negative integer or null".format(where))
    return value


def _digest(value: Any, where: str, allow_empty: bool = False) -> str:
    text = _bounded_text(value, where, 64)
    if allow_empty and not text:
        return ""
    if len(text) != 64 or any(character not in "0123456789abcdefABCDEF"
                              for character in text):
        raise ValueError("{} must be SHA-256 hexadecimal".format(where))
    return text.lower()


__all__ = [
    "QUALIFICATION_SCHEMA_VERSION", "EnvironmentReport", "EvidenceReference",
    "QualificationLevel", "QualificationMatrixEntry", "QualificationRecord",
    "QualificationStatus", "build_qualification_matrix", "collect_environment",
]
