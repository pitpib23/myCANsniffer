"""Passive adapter enumeration and Classic CAN bitrate discovery."""

from .adapters import enumerate_adapters, installed_backends
from .bitrate import (
    DEFAULT_BITRATES, DiscoveryThresholds, discover_bitrate,
    passive_scan_available,
)
from .model import (
    AdapterDescriptor, AdapterScanResult, BackendEnumerationResult,
    BitrateCandidateResult, CandidateStatus, DiscoveryProgress, DiscoveryResult,
    DiscoverySafetyMode, DiscoveryStatus, EnumerationStatus,
    PassiveCapability, Provenance,
)

__all__ = [
    "AdapterDescriptor", "AdapterScanResult", "BackendEnumerationResult",
    "BitrateCandidateResult", "CandidateStatus", "DEFAULT_BITRATES",
    "DiscoveryProgress", "DiscoveryResult", "DiscoverySafetyMode", "DiscoveryStatus",
    "DiscoveryThresholds", "EnumerationStatus", "PassiveCapability",
    "Provenance", "discover_bitrate", "enumerate_adapters",
    "installed_backends", "passive_scan_available",
]
