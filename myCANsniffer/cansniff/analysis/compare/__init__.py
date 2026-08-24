"""Passive baseline/event comparison and structural investigation."""

from .correlation import (
    DEFAULT_ALIGNMENT_TOLERANCE, correlate_candidates, pearson_correlation,
)
from .engine import (
    ComparisonCache, ComparisonCancelled, ComparisonValidationError,
    build_comparison,
)
from .model import (
    BitComparison, ByteComparison, CandidateEvidence, CandidateKind,
    ComparisonInput, ComparisonSnapshot, ComparisonWindow, CorrelationResult,
    MessageComparison, PresenceChange, WindowSummary,
)

__all__ = [
    "BitComparison", "ByteComparison", "CandidateEvidence", "CandidateKind",
    "ComparisonCache", "ComparisonCancelled", "ComparisonInput",
    "ComparisonSnapshot", "ComparisonValidationError", "ComparisonWindow",
    "CorrelationResult", "DEFAULT_ALIGNMENT_TOLERANCE", "MessageComparison",
    "PresenceChange", "WindowSummary", "build_comparison",
    "correlate_candidates", "pearson_correlation",
]
