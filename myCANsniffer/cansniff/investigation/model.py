"""Immutable, JSON-safe investigation project schema version 3."""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 3
FORMAT_VERSION = "3.0"
PROJECT_EXTENSION = ".cansniff-project"
MAX_CAPTURES = 128
MAX_ANNOTATIONS = 10000
MAX_BOOKMARKS = 10000
MAX_TEXT = 10000
ANALYSIS_VERSIONS = tuple(sorted((
    ("traffic_profile", "1"), ("protocol_survey", "1"),
    ("comparison", "1"), ("candidate_analysis", "1"),
    ("canopen_definition_parser", "1.0"),
    ("profile_matching", "1.0"),
    ("j1939_transport", "1.0"),
    ("j1939_definition_parser", "1.0"),
    ("j1939_decode", "1.0"),
    ("diagnostic_conversation", "1.0"),
    ("uds_structured_decode", "1.0"),
)))


class ProjectError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def _uuid(value: Any, where: str) -> str:
    text = str(value or "")
    try:
        uuid.UUID(text)
    except (ValueError, AttributeError) as exc:
        raise ProjectError("{} must be a UUID".format(where)) from exc
    return text


def _text(value: Any, where: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise ProjectError("{} must be text".format(where))
    if len(value) > limit:
        raise ProjectError("{} exceeds {} characters".format(where, limit))
    return value


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectError("{} must be numeric".format(where))
    result = float(value)
    if not math.isfinite(result):
        raise ProjectError("{} must be finite".format(where))
    return result


def _pairs(raw: Any, where: str) -> Tuple[Tuple[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ProjectError("{} must be an object".format(where))
    if len(raw) > 1000:
        raise ProjectError("{} contains too many fields".format(where))
    return tuple(sorted((_text(k, where + " key", 200),
                         _text(v, where + "." + str(k), 2000))
                        for k, v in raw.items()))


def _validate_json_value(value: Any, where: str, depth: int = 0,
                         budget: Optional[list] = None) -> None:
    """Bound an embedded profile snapshot before canonicalizing it."""
    if budget is None:
        budget = [200000]
    budget[0] -= 1
    if budget[0] < 0:
        raise ProjectError("{} contains too many values".format(where))
    if depth > 32:
        raise ProjectError("{} is nested too deeply".format(where))
    if value is None or isinstance(value, (bool, str)):
        if isinstance(value, str):
            _text(value, where)
        return
    if isinstance(value, (int, float)):
        _number(value, where)
        return
    if isinstance(value, list):
        if len(value) > 20000:
            raise ProjectError("{} contains too many list items".format(where))
        for item in value:
            _validate_json_value(item, where, depth + 1, budget)
        return
    if isinstance(value, dict):
        if len(value) > 20000:
            raise ProjectError("{} contains too many object fields".format(where))
        for key, item in value.items():
            _text(key, where + " key", 1000)
            _validate_json_value(item, where + "." + key, depth + 1, budget)
        return
    raise ProjectError("{} contains a non-JSON value".format(where))


@dataclass(frozen=True)
class CaptureReference:
    capture_id: str
    path: str
    display_name: str
    format: str
    content_hash: str
    size: int
    imported_at: str
    source_metadata: Tuple[Tuple[str, str], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.capture_id, "path": self.path,
                "display_name": self.display_name, "format": self.format,
                "content_hash": self.content_hash, "size": self.size,
                "imported_at": self.imported_at,
                "source_metadata": dict(self.source_metadata)}

    @classmethod
    def from_dict(cls, raw: Any) -> "CaptureReference":
        if not isinstance(raw, dict):
            raise ProjectError("capture reference must be an object")
        size = raw.get("size", 0)
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ProjectError("capture size must be a non-negative integer")
        digest = _text(raw.get("content_hash", ""), "capture hash", 128)
        if digest and (len(digest) != 64 or any(c not in "0123456789abcdefABCDEF"
                                                for c in digest)):
            raise ProjectError("capture hash must be SHA-256 hexadecimal")
        return cls(_uuid(raw.get("id"), "capture id"),
                   _text(raw.get("path", ""), "capture path", 32768),
                   _text(raw.get("display_name", ""), "capture display name", 1000),
                   _text(raw.get("format", ""), "capture format", 100),
                   digest.lower(), size,
                   _text(raw.get("imported_at", ""), "capture imported_at", 100),
                   _pairs(raw.get("source_metadata", {}), "source_metadata"))


@dataclass(frozen=True)
class ComparisonDefinition:
    comparison_id: str
    capture_id: str
    baseline_label: str
    baseline_start: float
    baseline_end: float
    event_label: str
    event_start: float
    event_end: float
    created_at: str
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.comparison_id, "capture_id": self.capture_id,
                "baseline": {"label": self.baseline_label,
                             "start": self.baseline_start, "end": self.baseline_end},
                "event": {"label": self.event_label,
                          "start": self.event_start, "end": self.event_end},
                "created_at": self.created_at, "note": self.note}

    @classmethod
    def from_dict(cls, raw: Any) -> "ComparisonDefinition":
        if not isinstance(raw, dict):
            raise ProjectError("comparison must be an object")
        baseline, event = raw.get("baseline"), raw.get("event")
        if not isinstance(baseline, dict) or not isinstance(event, dict):
            raise ProjectError("comparison baseline/event must be objects")
        bs, be = _number(baseline.get("start"), "baseline start"), _number(
            baseline.get("end"), "baseline end")
        es, ee = _number(event.get("start"), "event start"), _number(
            event.get("end"), "event end")
        if be < bs or ee < es:
            raise ProjectError("comparison interval end precedes start")
        return cls(_uuid(raw.get("id"), "comparison id"),
                   _uuid(raw.get("capture_id"), "comparison capture id"),
                   _text(baseline.get("label", "Baseline"), "baseline label", 500),
                   bs, be, _text(event.get("label", "Event"), "event label", 500),
                   es, ee, _text(raw.get("created_at", ""), "comparison created_at", 100),
                   _text(raw.get("note", ""), "comparison note"))


@dataclass(frozen=True)
class Annotation:
    annotation_id: str
    capture_id: str
    start_timestamp: float
    end_timestamp: Optional[float]
    text: str
    created_at: str
    modified_at: str
    tags: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.annotation_id, "capture_id": self.capture_id,
                "start": self.start_timestamp, "end": self.end_timestamp,
                "text": self.text, "created_at": self.created_at,
                "modified_at": self.modified_at, "tags": list(self.tags)}

    @classmethod
    def from_dict(cls, raw: Any) -> "Annotation":
        if not isinstance(raw, dict):
            raise ProjectError("annotation must be an object")
        start = _number(raw.get("start"), "annotation start")
        end_raw = raw.get("end")
        end = None if end_raw is None else _number(end_raw, "annotation end")
        if end is not None and end < start:
            raise ProjectError("annotation end precedes start")
        tags_raw = raw.get("tags", [])
        if not isinstance(tags_raw, list) or len(tags_raw) > 100:
            raise ProjectError("annotation tags must be a bounded list")
        return cls(_uuid(raw.get("id"), "annotation id"),
                   _uuid(raw.get("capture_id"), "annotation capture id"),
                   start, end, _text(raw.get("text"), "annotation text"),
                   _text(raw.get("created_at", ""), "annotation created_at", 100),
                   _text(raw.get("modified_at", ""), "annotation modified_at", 100),
                   tuple(_text(item, "annotation tag", 100) for item in tags_raw))


class BookmarkKind(str, Enum):
    FRAME = "FRAME"
    TIMESTAMP = "TIMESTAMP"
    RANGE = "RANGE"
    MESSAGE = "MESSAGE"
    ISOTP = "ISOTP"
    PROTOCOL = "PROTOCOL"
    COMPARISON = "COMPARISON"
    OBJECT_DICTIONARY = "OBJECT_DICTIONARY"
    CANDIDATE = "CANDIDATE"


@dataclass(frozen=True)
class Bookmark:
    bookmark_id: str
    kind: BookmarkKind
    label: str
    capture_id: str
    target: Tuple[Tuple[str, str], ...]
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.bookmark_id, "kind": self.kind.value,
                "label": self.label, "capture_id": self.capture_id,
                "target": dict(self.target), "created_at": self.created_at}

    @classmethod
    def from_dict(cls, raw: Any) -> "Bookmark":
        if not isinstance(raw, dict):
            raise ProjectError("bookmark must be an object")
        try:
            kind = BookmarkKind(str(raw.get("kind", "")))
        except ValueError as exc:
            raise ProjectError("unsupported bookmark kind") from exc
        target = _pairs(raw.get("target", {}), "bookmark target")
        return cls(_uuid(raw.get("id"), "bookmark id"), kind,
                   _text(raw.get("label", ""), "bookmark label", 1000),
                   _uuid(raw.get("capture_id"), "bookmark capture id"), target,
                   _text(raw.get("created_at", ""), "bookmark created_at", 100))


class ProfileMatchAction(str, Enum):
    USE_PROFILE = "USE_PROFILE"
    ASSOCIATE_DEFINITION = "ASSOCIATE_DEFINITION"


@dataclass(frozen=True)
class ProfileMatchDecision:
    decision_id: str
    action: ProfileMatchAction
    profile_id: str
    profile_name: str
    capture_hash: str
    candidate_set_identity: str
    algorithm_version: str
    accepted_at: str
    definition_hash: str = ""
    node_id: Optional[int] = None
    channel: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.decision_id, "action": self.action.value,
                "profile_id": self.profile_id, "profile_name": self.profile_name,
                "capture_hash": self.capture_hash,
                "candidate_set_identity": self.candidate_set_identity,
                "algorithm_version": self.algorithm_version,
                "accepted_at": self.accepted_at,
                "definition_hash": self.definition_hash,
                "node_id": self.node_id, "channel": self.channel}

    @classmethod
    def from_dict(cls, raw: Any) -> "ProfileMatchDecision":
        if not isinstance(raw, dict):
            raise ProjectError("profile match decision must be an object")
        try:
            action = ProfileMatchAction(str(raw.get("action", "")))
        except ValueError as exc:
            raise ProjectError("unsupported profile match decision action") from exc
        node_raw = raw.get("node_id")
        node = None
        if node_raw is not None:
            if isinstance(node_raw, bool) or not isinstance(node_raw, int) \
                    or not 1 <= node_raw <= 127:
                raise ProjectError("profile match decision node_id must be in 1..127")
            node = node_raw
        digest = _text(raw.get("capture_hash", ""), "decision capture hash", 128)
        definition_hash = _text(raw.get("definition_hash", ""),
                                "decision definition hash", 128)
        candidate_identity = _text(
            raw.get("candidate_set_identity", ""),
            "decision candidate set identity", 128)
        for value, where in ((digest, "decision capture hash"),
                             (candidate_identity,
                              "decision candidate set identity"),
                             (definition_hash, "decision definition hash")):
            if value and (len(value) != 64 or any(
                    character not in "0123456789abcdefABCDEF" for character in value)):
                raise ProjectError("{} must be SHA-256 hexadecimal".format(where))
        if action is ProfileMatchAction.ASSOCIATE_DEFINITION \
                and (not definition_hash or node is None):
            raise ProjectError("definition association decision needs a hash and node_id")
        return cls(
            _uuid(raw.get("id"), "profile match decision id"), action,
            _uuid(raw.get("profile_id"), "decision profile id"),
            _text(raw.get("profile_name", ""), "decision profile name", 1000),
            digest.lower(),
            candidate_identity.lower(),
            _text(raw.get("algorithm_version", ""),
                  "decision algorithm version", 100),
            _text(raw.get("accepted_at", ""), "decision accepted_at", 100),
            definition_hash.lower(), node,
            _text(raw.get("channel", ""), "decision channel", 500))


@dataclass(frozen=True)
class InvestigationProject:
    schema_version: int
    format_version: str
    project_id: str
    title: str
    created_at: str
    modified_at: str
    captures: Tuple[CaptureReference, ...] = ()
    active_capture_id: str = ""
    comparisons: Tuple[ComparisonDefinition, ...] = ()
    annotations: Tuple[Annotation, ...] = ()
    bookmarks: Tuple[Bookmark, ...] = ()
    selected_message_keys: Tuple[str, ...] = ()
    active_workspace: str = "Messages"
    display_filter: Tuple[Tuple[str, str], ...] = ()
    profile_snapshot_json: str = ""
    analysis_versions: Tuple[Tuple[str, str], ...] = ANALYSIS_VERSIONS
    profile_match_decisions: Tuple[ProfileMatchDecision, ...] = ()
    diagnostic_selection: Tuple[Tuple[str, str], ...] = ()

    @property
    def active_capture(self) -> Optional[CaptureReference]:
        return next((item for item in self.captures
                     if item.capture_id == self.active_capture_id), None)

    @property
    def profile_snapshot(self) -> Optional[Dict[str, Any]]:
        if not self.profile_snapshot_json:
            return None
        try:
            value = json.loads(self.profile_snapshot_json)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ProjectError("profile snapshot is not valid JSON") from exc
        return value if isinstance(value, dict) else None

    def changed(self, **fields: Any) -> "InvestigationProject":
        fields.setdefault("modified_at", utc_now())
        return replace(self, **fields)

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": self.schema_version,
                "format_version": self.format_version, "project_id": self.project_id,
                "title": self.title, "created_at": self.created_at,
                "modified_at": self.modified_at,
                "captures": [item.to_dict() for item in self.captures],
                "active_capture_id": self.active_capture_id,
                "comparisons": [item.to_dict() for item in self.comparisons],
                "annotations": [item.to_dict() for item in self.annotations],
                "bookmarks": [item.to_dict() for item in self.bookmarks],
                "selections": {"message_keys": list(self.selected_message_keys),
                               "active_workspace": self.active_workspace,
                               "diagnostics": dict(self.diagnostic_selection)},
                "display_filter": dict(self.display_filter),
                "profile_snapshot": self.profile_snapshot,
                "analysis_versions": dict(self.analysis_versions),
                "profile_match_decisions": [
                    item.to_dict() for item in self.profile_match_decisions]}

    @classmethod
    def from_dict(cls, raw: Any) -> "InvestigationProject":
        if not isinstance(raw, dict):
            raise ProjectError("project root must be an object")
        version = raw.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ProjectError("schema_version is required and must be an integer")
        if version != SCHEMA_VERSION:
            raise ProjectError("unsupported project schema version {} (current {})".format(
                version, SCHEMA_VERSION))
        captures_raw, annotations_raw = raw.get("captures", []), raw.get("annotations", [])
        bookmarks_raw, comparisons_raw = raw.get("bookmarks", []), raw.get("comparisons", [])
        decisions_raw = raw.get("profile_match_decisions", [])
        for value, name, limit in ((captures_raw, "captures", MAX_CAPTURES),
                                   (annotations_raw, "annotations", MAX_ANNOTATIONS),
                                   (bookmarks_raw, "bookmarks", MAX_BOOKMARKS),
                                   (comparisons_raw, "comparisons", 1000),
                                   (decisions_raw, "profile_match_decisions", 1000)):
            if not isinstance(value, list) or len(value) > limit:
                raise ProjectError("{} must be a list with at most {} entries".format(name, limit))
        selections = raw.get("selections", {})
        if not isinstance(selections, dict):
            raise ProjectError("selections must be an object")
        message_keys = selections.get("message_keys", [])
        if not isinstance(message_keys, list) or len(message_keys) > 10000:
            raise ProjectError("selected message keys must be a bounded list")
        snapshot = raw.get("profile_snapshot")
        if snapshot is not None and not isinstance(snapshot, dict):
            raise ProjectError("profile_snapshot must be an object or null")
        if snapshot is not None:
            _validate_json_value(snapshot, "profile_snapshot")
        snapshot_json = (json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
                         if snapshot is not None else "")
        project = cls(
            version, _text(raw.get("format_version", FORMAT_VERSION), "format_version", 100),
            _uuid(raw.get("project_id"), "project id"),
            _text(raw.get("title", "Untitled Investigation"), "project title", 1000),
            _text(raw.get("created_at", ""), "created_at", 100),
            _text(raw.get("modified_at", ""), "modified_at", 100),
            tuple(CaptureReference.from_dict(item) for item in captures_raw),
            _text(raw.get("active_capture_id", ""), "active_capture_id", 100),
            tuple(ComparisonDefinition.from_dict(item) for item in comparisons_raw),
            tuple(Annotation.from_dict(item) for item in annotations_raw),
            tuple(Bookmark.from_dict(item) for item in bookmarks_raw),
            tuple(_text(item, "selected message key", 500) for item in message_keys),
            _text(selections.get("active_workspace", "Messages"), "active workspace", 100),
            _pairs(raw.get("display_filter", {}), "display_filter"), snapshot_json,
            _pairs(raw.get("analysis_versions", dict(ANALYSIS_VERSIONS)),
                   "analysis_versions"),
            tuple(ProfileMatchDecision.from_dict(item) for item in decisions_raw),
            _pairs(selections.get("diagnostics", {}),
                   "diagnostic selections"),
        )
        capture_ids = {item.capture_id for item in project.captures}
        if len(capture_ids) != len(project.captures):
            raise ProjectError("capture IDs must be unique")
        if project.active_capture_id and project.active_capture_id not in capture_ids:
            raise ProjectError("active capture ID is not present")
        entity_ids = [item.annotation_id for item in project.annotations]
        entity_ids += [item.bookmark_id for item in project.bookmarks]
        entity_ids += [item.comparison_id for item in project.comparisons]
        entity_ids += [item.decision_id for item in project.profile_match_decisions]
        if len(entity_ids) != len(set(entity_ids)):
            raise ProjectError(
                "annotation/bookmark/comparison/profile-match decision IDs must be unique")
        return project


def new_project(title: str = "Untitled Investigation", now: Optional[str] = None
                ) -> InvestigationProject:
    timestamp = now or utc_now()
    return InvestigationProject(SCHEMA_VERSION, FORMAT_VERSION, new_id(), title,
                                timestamp, timestamp)


__all__ = ["ANALYSIS_VERSIONS", "FORMAT_VERSION", "PROJECT_EXTENSION",
           "SCHEMA_VERSION", "Annotation", "Bookmark", "BookmarkKind",
           "CaptureReference", "ComparisonDefinition", "InvestigationProject",
           "ProfileMatchAction", "ProfileMatchDecision", "ProjectError",
           "new_id", "new_project", "utc_now"]
