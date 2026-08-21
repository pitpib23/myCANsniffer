"""Immutable generic protocol-evidence models.

Evidence levels are deliberately categorical rather than percentages.  They
describe the strength of observations in one explicit horizon, not the
probability that a whole network implements a protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Tuple

from ..profile import AnalysisHorizon, CaptureIntegritySnapshot


class ProtocolKind(str, Enum):
    CANOPEN = "CANopen"
    J1939 = "J1939"
    ISOTP = "ISO-TP"
    UDS = "UDS"
    UNKNOWN = "Unknown / proprietary"


class EvidenceLevel(str, Enum):
    NONE = "None"
    WEAK = "Weak"
    POSSIBLE = "Possible"
    STRONG = "Strong"
    CONFIRMED = "Confirmed"

    @property
    def rank(self) -> int:
        return EVIDENCE_ORDER.index(self)


EVIDENCE_ORDER = (
    EvidenceLevel.NONE, EvidenceLevel.WEAK, EvidenceLevel.POSSIBLE,
    EvidenceLevel.STRONG, EvidenceLevel.CONFIRMED,
)


@dataclass(frozen=True)
class ProtocolEvidence:
    protocol: ProtocolKind
    level: EvidenceLevel
    reasons: Tuple[str, ...]
    related_message_keys: Tuple[str, ...]
    horizon: AnalysisHorizon
    integrity_context: CaptureIntegritySnapshot
    caveats: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def present(self) -> bool:
        return self.level is not EvidenceLevel.NONE


@dataclass(frozen=True)
class UdsObservation:
    message_key: str
    arb_id: int
    channel: str
    is_extended: bool
    timestamp: float
    kind: str
    service: int
    service_name: str
    sub_function: Optional[int]
    identifier: Optional[int]
    nrc: Optional[int]
    description: str


@dataclass(frozen=True)
class ProtocolSurveySnapshot:
    results: Tuple[ProtocolEvidence, ...]
    generated_from_revision: int
    horizon: AnalysisHorizon
    integrity_context: CaptureIntegritySnapshot
    canopen_nodes: Tuple[Any, ...] = field(default_factory=tuple)
    j1939_messages: Tuple[Any, ...] = field(default_factory=tuple)
    j1939_sources: Tuple[Any, ...] = field(default_factory=tuple)
    j1939_transport_sessions: Tuple[Any, ...] = field(default_factory=tuple)
    j1939_payloads: Tuple[Any, ...] = field(default_factory=tuple)
    uds_observations: Tuple[UdsObservation, ...] = field(default_factory=tuple)
    diagnostic_analysis: Any = None

    def result_for(self, protocol: ProtocolKind) -> ProtocolEvidence:
        for result in self.results:
            if result.protocol is protocol:
                return result
        raise KeyError(protocol)


__all__ = [
    "EVIDENCE_ORDER", "EvidenceLevel", "ProtocolEvidence", "ProtocolKind",
    "ProtocolSurveySnapshot", "UdsObservation",
]
