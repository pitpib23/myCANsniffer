"""Reproducible offline and opt-in hardware qualification framework."""

from .model import (
    QUALIFICATION_SCHEMA_VERSION, EnvironmentReport, EvidenceReference,
    QualificationLevel, QualificationMatrixEntry, QualificationRecord,
    QualificationStatus, build_qualification_matrix, collect_environment,
)
from .manifest import (
    MANIFEST_SCHEMA_VERSION, AnonymizationStatus, ArtifactKind,
    CaptureCorpusEntry, CorpusCategory, ExpectationMode,
    QualificationExpectation, QualificationManifest,
    load_qualification_manifest,
)
from .runner import (
    EntryResult, ExpectationOutcome, QualificationRunReport,
    current_commit, evaluate_expectation, run_manifest, write_report,
)

__all__ = [
    "QUALIFICATION_SCHEMA_VERSION", "EnvironmentReport", "EvidenceReference",
    "QualificationLevel", "QualificationMatrixEntry", "QualificationRecord",
    "QualificationStatus", "build_qualification_matrix", "collect_environment",
    "MANIFEST_SCHEMA_VERSION", "AnonymizationStatus", "ArtifactKind",
    "CaptureCorpusEntry", "CorpusCategory", "ExpectationMode",
    "QualificationExpectation", "QualificationManifest",
    "load_qualification_manifest",
    "EntryResult", "ExpectationOutcome", "QualificationRunReport",
    "current_commit", "evaluate_expectation", "run_manifest", "write_report",
]
