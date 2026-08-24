"""Immutable, Qt-free models for passive SocketCAN Classic-CAN bitrate discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple


class CandidateStatus(str, Enum):
    NO_TRAFFIC = "no-traffic"
    WEAK = "weak"
    POSSIBLE = "possible"
    STABLE = "stable"
    ERROR = "error"
    #: The `ip link` down/type/bitrate/listen-only/up sequence itself failed
    #: for this one candidate (for example the driver rejected the bitrate).
    #: Distinct from ERROR, which covers python-can bus open/receive failures
    #: after link configuration succeeded.
    CONFIGURATION_ERROR = "configuration-error"
    CANCELLED = "cancelled"


class DiscoveryStatus(str, Enum):
    DETECTED = "detected"
    NO_TRAFFIC = "no-traffic"
    AMBIGUOUS = "ambiguous"
    INCONCLUSIVE = "inconclusive"
    #: A systemic SocketCAN configuration failure (missing `ip`, missing
    #: interface, insufficient privilege) that would recur identically for
    #: every remaining candidate, so the scan stopped rather than retrying.
    CONFIGURATION_ERROR = "configuration-error"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class BitrateCandidateResult:
    bitrate: int
    status: CandidateStatus
    frames: int = 0
    valid_frames: int = 0
    error_frames: int = 0
    remote_frames: int = 0
    unique_ids: int = 0
    repeated_ids: int = 0
    observation_duration: float = 0.0
    traffic_span: float = 0.0
    #: Diagnostic-only "stablest identifier" evidence -- never itself a
    #: bitrate-selection criterion, see cansniff/discovery/bitrate.py.
    best_id: Optional[int] = None
    best_id_observations: int = 0
    best_id_span: float = 0.0
    reasons: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DiscoveryResult:
    interface: str
    status: DiscoveryStatus
    selected_bitrate: Optional[int]
    candidate_results: Tuple[BitrateCandidateResult, ...]
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DiscoveryProgress:
    stage: str
    message: str
    completed: int = 0
    total: int = 0
    candidate: Optional[BitrateCandidateResult] = None
