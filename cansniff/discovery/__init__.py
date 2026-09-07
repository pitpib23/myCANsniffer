"""Passive SocketCAN Classic CAN bitrate discovery."""

from .bitrate import (
    DEFAULT_BITRATES, DEFAULT_INTERFACE, DiscoveryThresholds,
    discover_socketcan_bitrate,
)
from .model import (
    BitrateCandidateResult, CandidateStatus, DiscoveryProgress, DiscoveryResult,
    DiscoveryStatus, ScanProgress, ScanResult, ScoredCandidate,
)
from .scan import (
    DEFAULT_SCAN_DURATION, DEFAULT_SETTLE_SECONDS, MIN_SCAN_DURATION,
    scan_bitrate_candidates,
)
from .scoring import ScoreComponents, ScoringConfig, score_candidate

__all__ = [
    "BitrateCandidateResult", "CandidateStatus", "DEFAULT_BITRATES",
    "DEFAULT_INTERFACE", "DEFAULT_SCAN_DURATION", "DEFAULT_SETTLE_SECONDS",
    "DiscoveryProgress", "DiscoveryResult", "DiscoveryStatus", "DiscoveryThresholds",
    "MIN_SCAN_DURATION", "ScanProgress", "ScanResult", "ScoreComponents",
    "ScoredCandidate", "ScoringConfig", "discover_socketcan_bitrate",
    "scan_bitrate_candidates", "score_candidate",
]
