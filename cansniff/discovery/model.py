"""Immutable, Qt-free models for passive adapter and bitrate discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple


class Provenance(str, Enum):
    DETECTED = "detected"
    BACKEND_KNOWN = "backend-known"
    CONFIGURED = "configured"
    UNKNOWN = "unknown"


class PassiveCapability(str, Enum):
    NOT_APPLICABLE = "not-applicable"
    SUPPORTED_BY_BACKEND_POLICY = "supported-by-backend-policy"
    EXTERNAL_VERIFICATION_REQUIRED = "external-verification-required"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class EnumerationStatus(str, Enum):
    FOUND = "found"
    EMPTY = "no-adapters-found"
    UNSUPPORTED = "enumeration-unsupported"
    BACKEND_UNAVAILABLE = "backend-unavailable"
    ERROR = "backend-error"
    CANCELLED = "cancelled"


class CandidateStatus(str, Enum):
    NO_TRAFFIC = "no-traffic"
    WEAK = "weak"
    POSSIBLE = "possible"
    STABLE = "stable"
    ERROR = "error"
    UNSUPPORTED = "unsupported"
    SAFETY_REJECTED = "passive-safety-rejected"
    CANCELLED = "cancelled"


class DiscoveryStatus(str, Enum):
    DETECTED = "detected"
    NO_TRAFFIC = "no-traffic"
    AMBIGUOUS = "ambiguous"
    INCONCLUSIVE = "inconclusive"
    UNSUPPORTED = "unsupported"
    SAFETY_REJECTED = "passive-safety-rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AdapterDescriptor:
    display_name: str
    interface: str
    channel: str
    supports_classic: Optional[bool] = True
    supports_fd: Optional[bool] = None
    passive_capability: PassiveCapability = PassiveCapability.UNKNOWN
    enumeration_source: Provenance = Provenance.UNKNOWN
    auto_bitrate_supported: bool = False
    is_physical: bool = True
    hardware_timestamp: Optional[bool] = None
    max_bitrate: Optional[int] = None
    max_data_bitrate: Optional[int] = None
    implementation_supported: bool = False
    hardware_qualified: bool = False
    electrically_verified: bool = False
    metadata: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def key(self) -> Tuple[str, str]:
        return (self.interface, self.channel)


@dataclass(frozen=True)
class BackendEnumerationResult:
    interface: str
    status: EnumerationStatus
    adapters: Tuple[AdapterDescriptor, ...] = field(default_factory=tuple)
    message: str = ""


@dataclass(frozen=True)
class AdapterScanResult:
    adapters: Tuple[AdapterDescriptor, ...]
    backends: Tuple[BackendEnumerationResult, ...]
    cancelled: bool = False


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
    reasons: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DiscoveryResult:
    adapter: AdapterDescriptor
    status: DiscoveryStatus
    selected_bitrate: Optional[int]
    candidate_results: Tuple[BitrateCandidateResult, ...]
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    mode: str = "classic"


@dataclass(frozen=True)
class DiscoveryProgress:
    stage: str
    message: str
    completed: int = 0
    total: int = 0
    candidate: Optional[BitrateCandidateResult] = None
