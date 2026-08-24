"""Bounded corpus manifests for local, permissioned qualification artifacts."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Tuple


MANIFEST_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 1000


class ArtifactKind(str, Enum):
    CAPTURE = "CAPTURE"
    DBC = "DBC"
    EDS = "EDS"
    DCF = "DCF"
    J1939_DEFINITION = "J1939_DEFINITION"


class CorpusCategory(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    MIXED = "MIXED"
    MALFORMED = "MALFORMED"
    LARGE = "LARGE"


class AnonymizationStatus(str, Enum):
    NOT_REVIEWED = "NOT_REVIEWED"
    NOT_REQUIRED = "NOT_REQUIRED"
    ANONYMIZED = "ANONYMIZED"
    SENSITIVE_EXTERNAL_ONLY = "SENSITIVE_EXTERNAL_ONLY"


class ExpectationMode(str, Enum):
    EXACT = "EXACT"
    MINIMUM = "MINIMUM"
    CONTAINS = "CONTAINS"
    ABSENT = "ABSENT"
    RANGE = "RANGE"


_FACT_PATH = re.compile(
    r"^(frames\.(total|fd|classic|parse_errors)|"
    r"protocol\.(CANopen|J1939|ISO-TP|UDS|Unknown / proprietary)\.level|"
    r"canopen\.node_ids|j1939\.(pgns|source_addresses|transport\.(complete|incomplete))|"
    r"diagnostics\.(services|dids|dtcs|pairs|conversations\.[a-z_]+)|"
    r"definition\.(kind|objects|pdo_mappings|warnings|validation_state|messages|signals|pgns|spns))$")


@dataclass(frozen=True)
class QualificationExpectation:
    fact: str
    mode: ExpectationMode
    expected: Any
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"fact": self.fact, "mode": self.mode.value,
                "expected": self.expected, "note": self.note}

    @classmethod
    def from_dict(cls, raw: Any) -> "QualificationExpectation":
        if not isinstance(raw, dict):
            raise ValueError("expectation must be an object")
        fact = _text(raw.get("fact", ""), "expectation fact", 500)
        if not _FACT_PATH.match(fact):
            raise ValueError("unsupported qualification fact {!r}".format(fact))
        try:
            mode = ExpectationMode(str(raw.get("mode", "")))
        except ValueError as exc:
            raise ValueError("unsupported expectation mode") from exc
        expected = raw.get("expected")
        _validate_json(expected, "expectation expected")
        if mode is ExpectationMode.RANGE:
            if (not isinstance(expected, list) or len(expected) != 2
                    or any(isinstance(item, bool) or not isinstance(item, (int, float))
                           or not math.isfinite(float(item)) for item in expected)
                    or expected[1] < expected[0]):
                raise ValueError("RANGE expectation needs finite [minimum, maximum]")
        if mode is ExpectationMode.CONTAINS and not isinstance(expected, list):
            raise ValueError("CONTAINS expectation needs a list")
        return cls(fact, mode, expected,
                   _text(raw.get("note", ""), "expectation note", 10000))


@dataclass(frozen=True)
class CaptureCorpusEntry:
    entry_id: str
    artifact_kind: ArtifactKind
    category: CorpusCategory
    path: str
    sha256: str
    source: str
    license_or_permission: str
    anonymization: AnonymizationStatus
    synthetic: bool
    redistributed: bool
    protocols: Tuple[str, ...]
    expected_features: Tuple[str, ...]
    expected_non_features: Tuple[str, ...]
    expectations: Tuple[QualificationExpectation, ...]
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.entry_id, "artifact_kind": self.artifact_kind.value,
            "category": self.category.value, "path": self.path,
            "sha256": self.sha256, "source": self.source,
            "license_or_permission": self.license_or_permission,
            "anonymization": self.anonymization.value,
            "synthetic": self.synthetic, "redistributed": self.redistributed,
            "protocols": list(self.protocols),
            "expected_features": list(self.expected_features),
            "expected_non_features": list(self.expected_non_features),
            "expectations": [item.to_dict() for item in self.expectations],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "CaptureCorpusEntry":
        if not isinstance(raw, dict):
            raise ValueError("corpus entry must be an object")
        try:
            artifact = ArtifactKind(str(raw.get("artifact_kind", "CAPTURE")))
            category = CorpusCategory(str(raw.get("category", "")))
            anonymization = AnonymizationStatus(str(raw.get("anonymization", "")))
        except ValueError as exc:
            raise ValueError("unsupported artifact/category/anonymization value") from exc
        path = _text(raw.get("path", ""), "artifact path", 32768)
        if (not path or "://" in path or path.lower().startswith("file:")
                or "\0" in path or path.startswith("\\\\") or path.startswith("//")):
            raise ValueError("artifact path must be a non-URL local path")
        source = _text(raw.get("source", ""), "artifact source", 5000)
        permission = _text(raw.get("license_or_permission", ""),
                           "license_or_permission", 10000)
        if not source.strip() or not permission.strip():
            raise ValueError("source and license_or_permission are required")
        synthetic = raw.get("synthetic")
        redistributed = raw.get("redistributed")
        if not isinstance(synthetic, bool) or not isinstance(redistributed, bool):
            raise ValueError("synthetic and redistributed must be boolean")
        if redistributed and anonymization is AnonymizationStatus.NOT_REVIEWED:
            raise ValueError("redistributed artifacts require anonymization review")
        protocols = _text_list(raw.get("protocols", []), "protocols", 100)
        features = _text_list(raw.get("expected_features", []),
                              "expected_features", 1000)
        non_features = _text_list(raw.get("expected_non_features", []),
                                  "expected_non_features", 1000)
        expectation_raw = raw.get("expectations", [])
        if not isinstance(expectation_raw, list) or len(expectation_raw) > 1000:
            raise ValueError("expectations must be a bounded list")
        return cls(
            _token(raw.get("id", ""), "entry id"), artifact, category, path,
            _sha256(raw.get("sha256", "")), source, permission, anonymization,
            synthetic, redistributed, protocols, features, non_features,
            tuple(QualificationExpectation.from_dict(item)
                  for item in expectation_raw),
            _text(raw.get("notes", ""), "entry notes", 20000))


@dataclass(frozen=True)
class QualificationManifest:
    manifest_id: str
    title: str
    entries: Tuple[CaptureCorpusEntry, ...]
    created_at: str = ""
    notes: str = ""
    source_path: str = field(default="", compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "manifest_id": self.manifest_id, "title": self.title,
            "created_at": self.created_at, "notes": self.notes,
            "entries": [item.to_dict() for item in self.entries],
        }

    @classmethod
    def from_dict(cls, raw: Any, source_path: str = "") -> "QualificationManifest":
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError("qualification manifest schema_version must be 1")
        entries = raw.get("entries", [])
        if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
            raise ValueError("entries must be a list with at most {} items".format(
                MAX_ENTRIES))
        parsed = tuple(CaptureCorpusEntry.from_dict(item) for item in entries)
        identities = [item.entry_id for item in parsed]
        if len(identities) != len(set(identities)):
            raise ValueError("corpus entry IDs must be unique")
        return cls(
            _token(raw.get("manifest_id", ""), "manifest id"),
            _text(raw.get("title", ""), "manifest title", 1000), parsed,
            _text(raw.get("created_at", ""), "manifest created_at", 100),
            _text(raw.get("notes", ""), "manifest notes", 20000), source_path)

    def resolve(self, entry: CaptureCorpusEntry) -> str:
        if os.path.isabs(entry.path):
            return os.path.abspath(entry.path)
        base = os.path.dirname(self.source_path) if self.source_path else os.curdir
        return os.path.abspath(os.path.join(base, entry.path))


def load_qualification_manifest(path: str) -> QualificationManifest:
    absolute = os.path.abspath(path)
    size = os.path.getsize(absolute)
    if size > MAX_MANIFEST_BYTES:
        raise ValueError("qualification manifest exceeds the 4 MiB limit")
    with open(absolute, "rb") as handle:
        data = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("qualification manifest exceeds the 4 MiB limit")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key {!r}".format(key))
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("invalid numeric constant {}".format(value))

    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=unique,
                         parse_constant=invalid)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("cannot parse qualification manifest: {}".format(exc)) from exc
    return QualificationManifest.from_dict(raw, absolute)


def _text(value: Any, where: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("{} must be bounded text".format(where))
    return value


def _text_list(value: Any, where: str, limit: int) -> Tuple[str, ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("{} must be a bounded list".format(where))
    return tuple(_text(item, where + " item", 1000) for item in value)


def _token(value: Any, where: str) -> str:
    text = _text(value, where, 200)
    if not text or not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", text):
        raise ValueError("{} must be a stable token".format(where))
    return text


def _sha256(value: Any) -> str:
    text = _text(value, "sha256", 64)
    if len(text) != 64 or any(item not in "0123456789abcdefABCDEF" for item in text):
        raise ValueError("sha256 must be 64 hexadecimal characters")
    return text.lower()


def _validate_json(value: Any, where: str, depth: int = 0) -> None:
    if depth > 16:
        raise ValueError("{} is nested too deeply".format(where))
    if value is None or isinstance(value, (bool, str)):
        if isinstance(value, str) and len(value) > 10000:
            raise ValueError("{} text is too long".format(where))
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ValueError("{} must be finite".format(where))
        return
    if isinstance(value, list) and len(value) <= 10000:
        for item in value:
            _validate_json(item, where, depth + 1)
        return
    raise ValueError("{} contains an unsupported or excessive value".format(where))


__all__ = [
    "MANIFEST_SCHEMA_VERSION", "MAX_MANIFEST_BYTES", "AnonymizationStatus",
    "ArtifactKind", "CaptureCorpusEntry", "CorpusCategory", "ExpectationMode",
    "QualificationExpectation", "QualificationManifest",
    "load_qualification_manifest",
]
