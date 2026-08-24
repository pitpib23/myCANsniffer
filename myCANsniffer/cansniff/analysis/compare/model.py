"""Immutable factual models for baseline/event investigation.

These objects describe observed structure. Candidate names deliberately end in
``-like`` or name only a numeric representation; none assigns signal meaning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

from ..profile import CaptureIntegritySnapshot
from ..protocols.model import EvidenceLevel


class PresenceChange(str, Enum):
    UNCHANGED = "present in both"
    NEW = "new during event"
    REMOVED = "absent during event"


class CandidateKind(str, Enum):
    COUNTER_LIKE = "counter-like"
    STATUS_BITFIELD_LIKE = "status-bitfield-like"
    CHECKSUM_LIKE = "checksum/CRC-like"
    NUMERIC = "numeric"


@dataclass(frozen=True)
class ComparisonWindow:
    label: str
    start_timestamp: float
    end_timestamp: float
    created_revision: int

    @property
    def duration(self) -> float:
        return max(0.0, self.end_timestamp - self.start_timestamp)


@dataclass(frozen=True)
class ComparisonInput:
    baseline: ComparisonWindow
    event: ComparisonWindow


@dataclass(frozen=True)
class WindowSummary:
    window: ComparisonWindow
    frame_count: int
    usable_frames: int
    message_keys: Tuple[str, ...]
    complete: bool
    caveats: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ByteComparison:
    index: int
    baseline_samples: int
    event_samples: int
    baseline_missing: int
    event_missing: int
    baseline_minimum: Optional[int]
    baseline_maximum: Optional[int]
    event_minimum: Optional[int]
    event_maximum: Optional[int]
    baseline_distinct: int
    event_distinct: int
    baseline_entropy: float
    event_entropy: float
    baseline_mode: Optional[int]
    event_mode: Optional[int]
    distribution_distance: float
    baseline_change_fraction: float
    event_change_fraction: float


@dataclass(frozen=True)
class BitComparison:
    byte_index: int
    bit_index: int
    baseline_samples: int
    event_samples: int
    baseline_one_fraction: float
    event_one_fraction: float
    baseline_transitions: int
    event_transitions: int

    @property
    def state_difference(self) -> float:
        return abs(self.event_one_fraction - self.baseline_one_fraction)


@dataclass(frozen=True)
class MessageComparison:
    key: str
    channel: str
    arb_id: int
    is_extended: bool
    is_fd: bool
    presence_change: PresenceChange
    baseline_count: int
    event_count: int
    baseline_rate_hz: float
    event_rate_hz: float
    rate_delta_hz: float
    normalized_rate_delta: float
    baseline_average_period: Optional[float]
    event_average_period: Optional[float]
    period_delta: Optional[float]
    baseline_payload_lengths: Tuple[Tuple[int, int], ...]
    event_payload_lengths: Tuple[Tuple[int, int], ...]
    baseline_payload_change_fraction: float
    event_payload_change_fraction: float
    baseline_first_seen: Optional[float]
    baseline_last_seen: Optional[float]
    event_first_seen: Optional[float]
    event_last_seen: Optional[float]
    bytes: Tuple[ByteComparison, ...]
    bits: Tuple[BitComparison, ...]
    evidence_level: EvidenceLevel
    ranking_score: float
    reasons: Tuple[str, ...]


@dataclass(frozen=True)
class CandidateEvidence:
    message_key: str
    kind: CandidateKind
    evidence_level: EvidenceLevel
    byte_start: int
    byte_length: int
    reasons: Tuple[str, ...]
    sample_count: int
    missing_samples: int
    score: float
    decoder_key: str = ""
    interpretation: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    baseline_mean: Optional[float] = None
    event_mean: Optional[float] = None
    baseline_window: Optional[ComparisonWindow] = None
    event_window: Optional[ComparisonWindow] = None
    caveats: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def field_label(self) -> str:
        end = self.byte_start + self.byte_length - 1
        span = str(self.byte_start) if end == self.byte_start else "{}-{}".format(
            self.byte_start, end)
        suffix = " / " + self.interpretation if self.interpretation else ""
        return "{} bytes {}{}".format(self.message_key, span, suffix)


@dataclass(frozen=True)
class ComparisonSnapshot:
    comparison_input: ComparisonInput
    generated_from_revision: int
    baseline: WindowSummary
    event: WindowSummary
    messages: Tuple[MessageComparison, ...]
    candidates: Tuple[CandidateEvidence, ...]
    integrity_context: CaptureIntegritySnapshot
    caveats: Tuple[str, ...]

    def message_for(self, key: str) -> MessageComparison:
        for message in self.messages:
            if message.key == key:
                return message
        raise KeyError(key)

    def candidates_for(self, key: str) -> Tuple[CandidateEvidence, ...]:
        return tuple(item for item in self.candidates if item.message_key == key)


@dataclass(frozen=True)
class CorrelationResult:
    left_label: str
    right_label: str
    coefficient: Optional[float]
    paired_samples: int
    tolerance_seconds: float
    reasons: Tuple[str, ...]
    left_missing: int = 0
    right_missing: int = 0


__all__ = [
    "BitComparison", "ByteComparison", "CandidateEvidence", "CandidateKind",
    "ComparisonInput", "ComparisonSnapshot", "ComparisonWindow",
    "CorrelationResult", "MessageComparison", "PresenceChange", "WindowSummary",
]
