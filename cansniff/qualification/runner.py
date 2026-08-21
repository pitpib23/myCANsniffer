"""Deterministic, offline-only execution of permissioned qualification corpora."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

from ..analysis.canopen_definitions import parse_definition_file
from ..analysis.j1939_definitions import parse_j1939_definition_file
from ..analysis.profile import TrafficProfileAccumulator
from ..analysis.protocols.model import EvidenceLevel
from ..analysis.protocols.survey import build_protocol_survey
from ..analysis.signals import import_dbc
from ..analysis.store import FrameStore
from ..sources.file_source import FileSource
from .manifest import (
    ArtifactKind, CaptureCorpusEntry, ExpectationMode, QualificationExpectation,
    QualificationManifest, load_qualification_manifest,
)
from .model import (
    EvidenceReference, QualificationLevel, QualificationRecord,
    QualificationStatus, collect_environment,
)


RUN_REPORT_SCHEMA_VERSION = 1
_EVIDENCE_RANK = {item.value: item.rank for item in EvidenceLevel}


@dataclass(frozen=True)
class ExpectationOutcome:
    fact: str
    mode: str
    expected: Any
    actual: Any
    passed: bool
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class EntryResult:
    entry_id: str
    status: str
    artifact_kind: str
    synthetic: bool
    sha256: str
    duration_seconds: float
    facts: Dict[str, Any]
    outcomes: Tuple[ExpectationOutcome, ...]
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id, "status": self.status,
            "artifact_kind": self.artifact_kind, "synthetic": self.synthetic,
            "sha256": self.sha256, "duration_seconds": self.duration_seconds,
            "facts": self.facts,
            "outcomes": [item.to_dict() for item in self.outcomes],
            "error": self.error,
        }


@dataclass(frozen=True)
class QualificationRunReport:
    manifest_id: str
    started_at: str
    finished_at: str
    entries: Tuple[EntryResult, ...]
    records: Tuple[QualificationRecord, ...]
    metrics: Dict[str, Any]

    @property
    def passed(self) -> bool:
        return bool(self.entries) and all(item.passed for item in self.entries)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": RUN_REPORT_SCHEMA_VERSION,
            "manifest_id": self.manifest_id, "started_at": self.started_at,
            "finished_at": self.finished_at, "passed": self.passed,
            "entries": [item.to_dict() for item in self.entries],
            "metrics": self.metrics,
            "records": [item.to_dict() for item in self.records],
        }

    def to_markdown(self) -> str:
        lines = ["# Qualification run: {}".format(self.manifest_id), "",
                 "Overall: **{}**".format("PASS" if self.passed else "FAIL"), "",
                 "| Entry | Kind | Provenance | Result | Checks |",
                 "|---|---|---|---|---:|"]
        for item in self.entries:
            checks = "{}/{}".format(sum(value.passed for value in item.outcomes),
                                     len(item.outcomes))
            provenance = "synthetic" if item.synthetic else "real"
            lines.append("| {} | {} | {} | {} | {} |".format(
                item.entry_id, item.artifact_kind, provenance, item.status, checks))
        lines += ["", "## Detection metrics", "", "```json",
                  json.dumps(self.metrics, indent=2, sort_keys=True), "```", "",
                  "Synthetic PASS results are SOFTWARE_TESTED only. A real, lawful "
                  "capture is required for CAPTURE_VALIDATED."]
        return "\n".join(lines) + "\n"


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _capture_facts(path: str) -> Tuple[Dict[str, Any], int]:
    source = FileSource(path, speed=0.0, loop=False)
    frames = []
    source.open()
    try:
        while not source.exhausted:
            frame = source.receive(timeout=0.0)
            if frame is not None:
                frames.append(frame)
        skipped = source.skipped_lines
    finally:
        source.close()
    store = FrameStore(max_frames=max(100, len(frames)))
    store.add(frames)
    accumulator = TrafficProfileAccumulator()
    accumulator.update(frames)
    profile = accumulator.snapshot(store)
    survey = build_protocol_survey(store.all_frames("qualification"), profile)
    facts: Dict[str, Any] = {
        "frames.total": len(frames), "frames.fd": profile.fd_frames,
        "frames.classic": profile.classic_frames, "frames.parse_errors": skipped,
    }
    for result in survey.results:
        facts["protocol.{}.level".format(result.protocol.value)] = result.level.value
    facts["canopen.node_ids"] = sorted({item.node_id for item in survey.canopen_nodes})
    facts["j1939.pgns"] = sorted({item.pgn for item in survey.j1939_messages})
    facts["j1939.source_addresses"] = sorted(
        {item.source_address for item in survey.j1939_messages})
    complete = sum(getattr(item.status, "value", item.status) == "Complete"
                   for item in survey.j1939_transport_sessions)
    facts["j1939.transport.complete"] = complete
    facts["j1939.transport.incomplete"] = len(survey.j1939_transport_sessions) - complete
    observations = survey.uds_observations
    facts["diagnostics.services"] = sorted({item.service for item in observations})
    facts["diagnostics.dids"] = sorted(
        {item.identifier for item in observations if item.identifier is not None})
    facts["diagnostics.dtcs"] = []
    analysis = survey.diagnostic_analysis
    facts["diagnostics.pairs"] = len(analysis.peers) if analysis else 0
    facts["diagnostics.conversations.total"] = (
        len(analysis.conversations) if analysis else 0)
    if analysis:
        facts["diagnostics.dids"] = sorted({item.did for item in analysis.dids})
        facts["diagnostics.dtcs"] = sorted({item.dtc for item in analysis.dtcs})
        statuses: Dict[str, int] = {}
        for item in analysis.conversations:
            key = item.correlation_status.value.lower().replace(" ", "_").replace("-", "_")
            statuses[key] = statuses.get(key, 0) + 1
        for key, value in statuses.items():
            facts["diagnostics.conversations.{}".format(key)] = value
    return facts, len(frames)


def _definition_facts(entry: CaptureCorpusEntry, path: str) -> Dict[str, Any]:
    if entry.artifact_kind in (ArtifactKind.EDS, ArtifactKind.DCF):
        value = parse_definition_file(path)
        return {
            "definition.kind": value.source.kind.value,
            "definition.objects": len(value.dictionary.objects),
            "definition.pdo_mappings": len(value.pdo_mappings),
            "definition.warnings": len(value.diagnostics),
            "definition.validation_state": value.validation_state.value,
        }
    if entry.artifact_kind is ArtifactKind.J1939_DEFINITION:
        value = parse_j1939_definition_file(path)
        return {
            "definition.kind": "J1939", "definition.pgns": len(value.pgns),
            "definition.spns": sum(len(item.spns) for item in value.pgns),
            "definition.warnings": len(value.diagnostics),
            "definition.validation_state": value.validation_state.value,
        }
    if entry.artifact_kind is ArtifactKind.DBC:
        profile = import_dbc(path)
        return {
            "definition.kind": "DBC",
            "definition.messages": len({(item.can_id, item.is_extended)
                                         for item in profile.signals}),
            "definition.signals": len(profile.signals),
            "definition.validation_state": "VALID",
            "definition.warnings": 0,
        }
    raise ValueError("unsupported definition kind")


def evaluate_expectation(expectation: QualificationExpectation,
                         actual: Any) -> ExpectationOutcome:
    expected, mode = expectation.expected, expectation.mode
    try:
        if mode is ExpectationMode.EXACT:
            passed = actual == expected
        elif mode is ExpectationMode.CONTAINS:
            passed = isinstance(actual, (list, tuple, set)) and set(expected) <= set(actual)
        elif mode is ExpectationMode.RANGE:
            passed = expected[0] <= actual <= expected[1]
        elif mode is ExpectationMode.MINIMUM:
            if expectation.fact.startswith("protocol."):
                passed = _EVIDENCE_RANK[str(actual)] >= _EVIDENCE_RANK[str(expected)]
            else:
                passed = actual >= expected
        else:  # ABSENT
            if expectation.fact.startswith("protocol."):
                threshold = str(expected or EvidenceLevel.WEAK.value)
                passed = _EVIDENCE_RANK[str(actual)] < _EVIDENCE_RANK[threshold]
            elif expected is None:
                passed = actual in (None, [], (), {}, "", 0)
            elif isinstance(actual, (list, tuple, set)):
                passed = expected not in actual
            else:
                passed = actual != expected
    except (KeyError, TypeError, ValueError):
        passed = False
    detail = "" if passed else "expected {} {}, observed {!r}".format(
        mode.value, expected, actual)
    return ExpectationOutcome(expectation.fact, mode.value, expected, actual,
                              passed, detail)


def current_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL,
            text=True, timeout=3).strip()
    except Exception:
        return "unknown"


def _metrics(entries: Iterable[EntryResult]) -> Dict[str, Any]:
    values = tuple(entries)
    negative_total = false_positive = positive_total = missed = partial = detected = 0
    observed_levels = {item.value: 0 for item in EvidenceLevel}
    unexpected_levels = {item.value: 0 for item in EvidenceLevel}
    correlation = {"tp": 0, "fp": 0, "fn": 0, "ambiguous": 0,
                   "labeled_entries": 0}
    for entry in values:
        facts = entry.facts
        if "diagnostics.pairs" in facts:
            pair_truth = next((item for item in entry.outcomes
                               if item.fact == "diagnostics.pairs"), None)
            if pair_truth is not None and isinstance(pair_truth.expected, int):
                actual = facts["diagnostics.pairs"]
                correlation["labeled_entries"] += 1
                correlation["tp"] += min(actual, pair_truth.expected)
                correlation["fp"] += max(0, actual - pair_truth.expected)
                correlation["fn"] += max(0, pair_truth.expected - actual)
            correlation["ambiguous"] += sum(
                value for key, value in facts.items()
                if key.startswith("diagnostics.conversations.ambiguous"))
        for outcome in entry.outcomes:
            if not outcome.fact.startswith("protocol."):
                continue
            if outcome.mode == ExpectationMode.ABSENT.value:
                negative_total += 1
                if str(outcome.actual) in observed_levels:
                    observed_levels[str(outcome.actual)] += 1
                false_positive += not outcome.passed
                if not outcome.passed and str(outcome.actual) in unexpected_levels:
                    unexpected_levels[str(outcome.actual)] += 1
            elif outcome.mode == ExpectationMode.MINIMUM.value:
                positive_total += 1
                if not outcome.passed:
                    actual_rank = _EVIDENCE_RANK.get(str(outcome.actual), 0)
                    partial += actual_rank > 0
                    missed += actual_rank == 0
                else:
                    detected += 1
    return {
        "protocol_false_positives": {"count": false_positive,
                                      "opportunities": negative_total,
                                      "observed_levels": observed_levels,
                                      "unexpected_levels": unexpected_levels},
        "protocol_false_negatives": {"missed": missed, "partial": partial,
                                      "detected": detected,
                                      "opportunities": positive_total},
        "diagnostic_correlation": correlation,
        "ground_truth_correlation": {
            "passed_expectations": sum(o.passed for e in values for o in e.outcomes),
            "expectations": sum(len(e.outcomes) for e in values),
        },
    }


def run_manifest(manifest_or_path: Any) -> QualificationRunReport:
    manifest = (load_qualification_manifest(manifest_or_path)
                if isinstance(manifest_or_path, (str, os.PathLike))
                else manifest_or_path)
    if not isinstance(manifest, QualificationManifest):
        raise TypeError("run_manifest requires a QualificationManifest or path")
    started = datetime.now(timezone.utc).isoformat()
    environment = collect_environment(current_commit())
    results: List[EntryResult] = []
    records: List[QualificationRecord] = []
    for entry in manifest.entries:
        begin = time.perf_counter()
        path = manifest.resolve(entry)
        digest = ""
        facts: Dict[str, Any] = {}
        outcomes: Tuple[ExpectationOutcome, ...] = ()
        error = ""
        count = 0
        try:
            digest = _sha256(path)
            if digest != entry.sha256:
                raise ValueError("SHA-256 mismatch; artifact refused before parsing")
            if entry.artifact_kind is ArtifactKind.CAPTURE:
                facts, count = _capture_facts(path)
            else:
                facts = _definition_facts(entry, path)
            outcomes = tuple(evaluate_expectation(item, facts.get(item.fact))
                             for item in entry.expectations)
            status = "PASS" if all(item.passed for item in outcomes) else "FAIL"
        except Exception as exc:
            status, error = "FAIL", "{}: {}".format(type(exc).__name__, exc)
        duration = time.perf_counter() - begin
        result = EntryResult(entry.entry_id, status, entry.artifact_kind.value,
                             entry.synthetic, digest, duration, facts, outcomes, error)
        results.append(result)
        level = (QualificationLevel.SOFTWARE_TESTED if entry.synthetic
                 else QualificationLevel.CAPTURE_VALIDATED)
        records.append(QualificationRecord(
            "{}-{}".format(manifest.manifest_id, entry.entry_id),
            "offline.{}".format(entry.artifact_kind.value.lower()), level,
            QualificationStatus.PASS if result.passed else QualificationStatus.FAIL,
            entry.entry_id, datetime.now(timezone.utc).isoformat(), environment,
            duration_seconds=duration, frame_count=count or None,
            evidence=(EvidenceReference("artifact", path, digest, entry.source),),
            limitations=(("Synthetic evidence does not qualify real captures or hardware.",)
                         if entry.synthetic else ()),
            provenance=tuple(sorted({"license_or_permission": entry.license_or_permission,
                                     "anonymization": entry.anonymization.value,
                                     "manifest": manifest.manifest_id}.items())),
        ))
    finished = datetime.now(timezone.utc).isoformat()
    return QualificationRunReport(manifest.manifest_id, started, finished,
                                  tuple(results), tuple(records), _metrics(results))


def write_report(report: QualificationRunReport, json_path: str = "",
                 markdown_path: str = "") -> None:
    if json_path:
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(report.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
    if markdown_path:
        with open(markdown_path, "w", encoding="utf-8") as handle:
            handle.write(report.to_markdown())


__all__ = [
    "EntryResult", "ExpectationOutcome", "QualificationRunReport",
    "current_commit", "evaluate_expectation", "run_manifest", "write_report",
]
