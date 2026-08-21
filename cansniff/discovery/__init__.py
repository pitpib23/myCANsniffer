"""Passive adapter enumeration and Classic CAN bitrate discovery."""

from .adapters import enumerate_adapters
from .bitrate import DEFAULT_BITRATES, DiscoveryThresholds, discover_bitrate
from .model import (
    AdapterDescriptor, AdapterScanResult, BackendEnumerationResult,
    BitrateCandidateResult, CandidateStatus, DiscoveryProgress, DiscoveryResult,
    DiscoveryStatus, EnumerationStatus, PassiveCapability, Provenance,
)

__all__ = [
    "AdapterDescriptor", "AdapterScanResult", "BackendEnumerationResult",
    "BitrateCandidateResult", "CandidateStatus", "DEFAULT_BITRATES",
    "DiscoveryProgress", "DiscoveryResult", "DiscoveryStatus",
    "DiscoveryThresholds", "EnumerationStatus", "PassiveCapability",
    "Provenance", "discover_bitrate", "enumerate_adapters",
]
