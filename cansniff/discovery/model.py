"""Immutable, Qt-free models for passive SocketCAN Classic-CAN bitrate discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

from .scoring import ScoreComponents


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


# -- numerically-scored manual scan ("Auto Scan" popup) -------------------
#
# A deliberately separate model from BitrateCandidateResult/DiscoveryResult
# above: those back the untouched, auto-selecting discover_socketcan_bitrate
# engine (cansniff/discovery/bitrate.py), which stays exactly as it was and
# stays independently tested (tests/test_discovery.py). Nothing below
# selects a bitrate for the operator -- see cansniff/discovery/scan.py and
# cansniff/ui/auto_scan_dialog.py's "Start Listening".


@dataclass(frozen=True)
class ScoredCandidate:
    """One scanned candidate's full result: the exact score breakdown from
    ``cansniff.discovery.scoring`` when the observation actually ran, or
    ``None`` with a technical reason when it could not (interface
    configuration was rejected, the scan was cancelled mid-candidate, ...).

    ``total_score`` is always populated -- 0.0 in both the "no components"
    and the "observed but zero traffic" cases -- so a result table can
    sort/display every row uniformly without special-casing ``None``.
    """

    bitrate: int
    requested_duration: float
    observed_duration: float
    settle_seconds: float
    #: True only once this candidate's full observation window actually ran
    #: to completion (not cancelled, not rejected at configuration).
    completed: bool
    total_score: float
    components: Optional[ScoreComponents]
    reasons: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ScanProgress:
    stage: str
    message: str
    completed: int = 0
    total: int = 0
    bitrate: Optional[int] = None
    #: Seconds into the current candidate's observation window / the
    #: requested duration for it -- for a live "elapsed / total" readout.
    #: Both 0.0 outside the observation stages.
    candidate_elapsed: float = 0.0
    candidate_duration: float = 0.0
    candidate: Optional[ScoredCandidate] = None


@dataclass(frozen=True)
class ScanResult:
    interface: str
    candidates: Tuple[ScoredCandidate, ...] = field(default_factory=tuple)
    cancelled: bool = False
    reasons: Tuple[str, ...] = field(default_factory=tuple)
