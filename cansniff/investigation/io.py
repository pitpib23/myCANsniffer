"""Strict project loading, atomic saving, and external evidence verification."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Dict, Iterable, Optional, Tuple

from .model import (
    FORMAT_VERSION, SCHEMA_VERSION, CaptureReference, InvestigationProject,
    ProjectError, new_id, utc_now,
)


MAX_PROJECT_BYTES = 32 * 1024 * 1024
MAX_HASH_CACHE_ENTRIES = 256
_HASH_CACHE: Dict[Tuple[str, int, int, int], str] = {}


class CaptureReferenceStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    CHANGED = "CHANGED"
    MOVED = "MOVED"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class CaptureVerification:
    status: CaptureReferenceStatus
    path: str
    explanation: str
    actual_hash: str = ""


@dataclass(frozen=True)
class ProjectLoadResult:
    project: InvestigationProject
    path: str
    capture_verifications: Tuple[Tuple[str, CaptureVerification], ...]
    migrations: Tuple[str, ...] = ()
    comparison_issues: Tuple[str, ...] = ()

    def verification_for(self, capture_id: str) -> Optional[CaptureVerification]:
        return next((value for key, value in self.capture_verifications
                     if key == capture_id), None)


def hash_file(path: str) -> str:
    absolute = os.path.abspath(path)
    try:
        stat = os.stat(absolute)
    except OSError as exc:
        raise ProjectError("cannot hash capture {}: {}".format(path, exc)) from exc
    key = (absolute, int(stat.st_size), int(stat.st_mtime_ns),
           int(stat.st_ctime_ns))
    cached = _HASH_CACHE.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    try:
        with open(absolute, "rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise ProjectError("cannot hash capture {}: {}".format(path, exc)) from exc
    result = digest.hexdigest()
    for old_key in tuple(_HASH_CACHE):
        if old_key[0] == absolute and old_key != key:
            _HASH_CACHE.pop(old_key, None)
    while len(_HASH_CACHE) >= MAX_HASH_CACHE_ENTRIES:
        _HASH_CACHE.pop(next(iter(_HASH_CACHE)))
    _HASH_CACHE[key] = result
    return result


def attach_capture(path: str, source_metadata: Optional[Dict[str, str]] = None,
                   now: Optional[str] = None) -> CaptureReference:
    absolute = os.path.abspath(path)
    if not os.path.isfile(absolute):
        raise ProjectError("capture file does not exist: {}".format(path))
    stat = os.stat(absolute)
    return CaptureReference(
        new_id(), absolute, os.path.basename(absolute),
        os.path.splitext(absolute)[1].lower().lstrip("."), hash_file(absolute),
        int(stat.st_size), now or utc_now(),
        tuple(sorted((str(key), str(value))
                     for key, value in (source_metadata or {}).items())),
    )


def verify_capture(reference: CaptureReference,
                   candidate_path: Optional[str] = None) -> CaptureVerification:
    path = os.path.abspath(candidate_path or reference.path)
    if not os.path.isfile(path):
        return CaptureVerification(CaptureReferenceStatus.MISSING, path,
                                   "referenced capture is unavailable")
    try:
        actual = hash_file(path)
    except ProjectError as exc:
        return CaptureVerification(CaptureReferenceStatus.UNVERIFIED, path, str(exc))
    if reference.content_hash and actual != reference.content_hash:
        return CaptureVerification(
            CaptureReferenceStatus.CHANGED, path,
            "capture contents differ from the saved SHA-256; explicit acceptance is required",
            actual)
    moved = os.path.normcase(path) != os.path.normcase(os.path.abspath(reference.path))
    return CaptureVerification(
        CaptureReferenceStatus.MOVED if moved else CaptureReferenceStatus.AVAILABLE,
        path, "same capture content found at a new path" if moved
        else "capture is available and unchanged", actual)


def relocate_capture(reference: CaptureReference, candidate_path: str,
                     accept_changed: bool = False) -> Tuple[CaptureReference,
                                                            CaptureVerification]:
    verification = verify_capture(reference, candidate_path)
    if verification.status is CaptureReferenceStatus.CHANGED and not accept_changed:
        return reference, verification
    if verification.status not in (CaptureReferenceStatus.AVAILABLE,
                                    CaptureReferenceStatus.MOVED,
                                    CaptureReferenceStatus.CHANGED):
        return reference, verification
    path = os.path.abspath(candidate_path)
    stat = os.stat(path)
    metadata = dict(reference.source_metadata)
    if accept_changed and verification.status is CaptureReferenceStatus.CHANGED:
        for key in tuple(metadata):
            if key.startswith(("retained_", "received_", "accepted_", "processed_",
                               "ui_dropped_", "source_errors_", "capture_integrity",
                               "channels_observed")):
                metadata["prior_" + key] = metadata.pop(key)
        metadata["evidence_replaced"] = "true"
        metadata["previous_content_hash"] = reference.content_hash
        metadata["changed_evidence_accepted_at"] = utc_now()
    updated = replace(
        reference, path=path, display_name=os.path.basename(path), size=int(stat.st_size),
        content_hash=(verification.actual_hash if accept_changed
                      and verification.status is CaptureReferenceStatus.CHANGED
                      else reference.content_hash),
        source_metadata=tuple(sorted(metadata.items())))
    final = verify_capture(updated)
    return updated, final


def _metadata_number(reference: CaptureReference, name: str) -> Optional[float]:
    """Read optional capture-horizon metadata without trusting malformed values."""
    raw = dict(reference.source_metadata).get(name, "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def validate_comparisons(
        project: InvestigationProject,
        verifications: Iterable[Tuple[str, CaptureVerification]] = (),
) -> Tuple[str, ...]:
    """Return deterministic caveats for saved inputs; never alter their bounds."""
    captures = {item.capture_id: item for item in project.captures}
    checks = dict(verifications)
    issues = []
    for comparison in project.comparisons:
        label = comparison.note or comparison.comparison_id
        capture = captures.get(comparison.capture_id)
        if capture is None:
            issues.append("Comparison {} references an unavailable capture.".format(label))
            continue
        if dict(capture.source_metadata).get("evidence_replaced") == "true":
            issues.append("Comparison {} must be revalidated because changed evidence "
                          "was explicitly accepted.".format(label))
        check = checks.get(comparison.capture_id)
        if check is not None and check.status in (
                CaptureReferenceStatus.MISSING, CaptureReferenceStatus.CHANGED,
                CaptureReferenceStatus.UNVERIFIED):
            issues.append("Comparison {} cannot be reproduced: capture is {}.".format(
                label, check.status.value.lower()))
        start = _metadata_number(capture, "retained_start")
        end = _metadata_number(capture, "retained_end")
        if start is None or end is None:
            continue
        if (comparison.baseline_start < start or comparison.baseline_end > end or
                comparison.event_start < start or comparison.event_end > end):
            issues.append(
                "Comparison {} has saved bounds outside the attached capture range "
                "[{:.9g}, {:.9g}]; bounds were not adjusted.".format(label, start, end))
    return tuple(issues)


def _migrate(raw: Any) -> Tuple[Dict[str, Any], Tuple[str, ...]]:
    if not isinstance(raw, dict):
        raise ProjectError("project root must be a JSON object")
    version = raw.get("schema_version")
    if isinstance(version, bool):
        raise ProjectError("schema_version must be an integer, not boolean")
    if version == SCHEMA_VERSION:
        return raw, ()
    migrations = []
    if version == 0:
        migrated = dict(raw)
        migrated["schema_version"] = 1
        migrated.setdefault("format_version", "1.0")
        migrated.setdefault("captures", [])
        migrated.setdefault("comparisons", [])
        migrated.setdefault("annotations", [])
        migrated.setdefault("bookmarks", [])
        migrated.setdefault("selections", {})
        migrated.setdefault("display_filter", {})
        migrated.setdefault("analysis_versions", {})
        migrations.append("migrated project schema 0 to schema 1")
        raw, version = migrated, 1
    if version == 1:
        migrated = dict(raw)
        migrated["schema_version"] = 2
        migrated["format_version"] = FORMAT_VERSION
        migrated.setdefault("profile_match_decisions", [])
        versions = dict(migrated.get("analysis_versions", {}) or {})
        versions.setdefault("profile_matching", "1.0")
        migrated["analysis_versions"] = versions
        migrations.append("migrated project schema 1 to schema 2")
        raw, version = migrated, 2
    if version == 2:
        migrated = dict(raw)
        migrated["schema_version"] = 3
        migrated["format_version"] = FORMAT_VERSION
        selections = dict(migrated.get("selections", {}) or {})
        selections.setdefault("diagnostics", {})
        migrated["selections"] = selections
        versions = dict(migrated.get("analysis_versions", {}) or {})
        versions.setdefault("diagnostic_conversation", "1.0")
        versions.setdefault("uds_structured_decode", "1.0")
        migrated["analysis_versions"] = versions
        migrations.append("migrated project schema 2 to schema 3")
        return migrated, tuple(migrations)
    if version is None:
        raise ProjectError("project is missing required schema_version")
    raise ProjectError("unsupported project schema version {} (current {})".format(
        version, SCHEMA_VERSION))


def load_project(path: str, verify_captures: bool = True) -> ProjectLoadResult:
    absolute = os.path.abspath(path)
    try:
        size = os.path.getsize(absolute)
    except OSError as exc:
        raise ProjectError("cannot read project: {}".format(exc)) from exc
    if size > MAX_PROJECT_BYTES:
        raise ProjectError("project exceeds the {} MiB safety limit".format(
            MAX_PROJECT_BYTES // (1024 * 1024)))
    try:
        with open(absolute, "rb") as handle:
            data = handle.read(MAX_PROJECT_BYTES + 1)
        if len(data) > MAX_PROJECT_BYTES:
            raise ProjectError("project exceeds the {} MiB safety limit".format(
                MAX_PROJECT_BYTES // (1024 * 1024)))
        text = data.decode("utf-8")

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ProjectError("project contains duplicate JSON key {!r}".format(key))
                result[key] = value
            return result

        def invalid_constant(value):
            raise ProjectError("project contains invalid numeric constant {}".format(value))

        raw = json.loads(text, object_pairs_hook=unique_object,
                         parse_constant=invalid_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProjectError("cannot parse project: {}".format(exc)) from exc
    migrated, migrations = _migrate(raw)
    project = InvestigationProject.from_dict(migrated)
    checks = tuple((item.capture_id,
                    verify_capture(item) if verify_captures else CaptureVerification(
                        CaptureReferenceStatus.UNVERIFIED, item.path,
                        "capture verification was deferred"))
                   for item in project.captures)
    return ProjectLoadResult(project, absolute, checks, migrations,
                             validate_comparisons(project, checks))


def serialize_project(project: InvestigationProject) -> str:
    # Round-trip validation before any canonical path is touched.
    raw = project.to_dict()
    InvestigationProject.from_dict(raw)
    return json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def save_project(project: InvestigationProject, path: str) -> None:
    absolute = os.path.abspath(path)
    directory = os.path.dirname(absolute) or os.curdir
    os.makedirs(directory, exist_ok=True)
    text = serialize_project(project)
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".cansniff-project-", suffix=".tmp", dir=directory, text=True)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        with open(temporary, "r", encoding="utf-8") as handle:
            InvestigationProject.from_dict(json.load(handle))
        os.replace(temporary, absolute)
        temporary = ""
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectError("could not save project: {}".format(exc)) from exc
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


__all__ = ["CaptureReferenceStatus", "CaptureVerification", "MAX_HASH_CACHE_ENTRIES",
           "MAX_PROJECT_BYTES",
           "ProjectLoadResult", "attach_capture", "hash_file", "load_project",
           "relocate_capture", "save_project", "serialize_project",
           "validate_comparisons", "verify_capture"]
