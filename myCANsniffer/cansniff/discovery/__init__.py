"""Passive SocketCAN Classic CAN bitrate discovery."""

from .bitrate import (
    DEFAULT_BITRATES, DEFAULT_INTERFACE, DiscoveryThresholds,
    discover_socketcan_bitrate,
)
from .model import (
    BitrateCandidateResult, CandidateStatus, DiscoveryProgress, DiscoveryResult,
    DiscoveryStatus,
)

__all__ = [
    "BitrateCandidateResult", "CandidateStatus", "DEFAULT_BITRATES",
    "DEFAULT_INTERFACE", "DiscoveryProgress", "DiscoveryResult",
    "DiscoveryStatus", "DiscoveryThresholds", "discover_socketcan_bitrate",
]
