"""Conservative Classic CAN bitrate discovery with an explicit safety mode."""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, replace
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from ..model import CanFrame
from ..sources import PassiveSafetyError, SourceError
from ..sources.live import LiveSource
from .model import (
    AdapterDescriptor, BitrateCandidateResult, CandidateStatus,
    DiscoveryProgress, DiscoveryResult, DiscoverySafetyMode, DiscoveryStatus,
    PassiveCapability,
)

log = logging.getLogger(__name__)

DEFAULT_BITRATES = (
    10_000, 20_000, 33_333, 50_000, 83_333, 100_000,
    125_000, 250_000, 500_000, 800_000, 1_000_000,
)


@dataclass(frozen=True)
class DiscoveryThresholds:
    observation_window: float = 1.5
    receive_timeout: float = 0.05
    minimum_valid_frames: int = 6
    minimum_repeated_ids: int = 1
    minimum_traffic_span: float = 0.50
    minimum_window_coverage: float = 0.50
    maximum_error_ratio: float = 0.20

    def __post_init__(self) -> None:
        # Configuration may tune strictness for bus volume, but it may not
        # disable the basic false-positive protections.
        object.__setattr__(self, "observation_window",
                           max(0.01, float(self.observation_window)))
        object.__setattr__(self, "receive_timeout",
                           max(0.001, float(self.receive_timeout)))
        object.__setattr__(self, "minimum_valid_frames",
                           max(2, int(self.minimum_valid_frames)))
        object.__setattr__(self, "minimum_repeated_ids",
                           max(1, int(self.minimum_repeated_ids)))
        object.__setattr__(self, "minimum_traffic_span",
                           max(0.001, float(self.minimum_traffic_span)))
        object.__setattr__(self, "minimum_window_coverage",
                           min(1.0, max(0.05, float(self.minimum_window_coverage))))
        object.__setattr__(self, "maximum_error_ratio",
                           min(0.50, max(0.0, float(self.maximum_error_ratio))))

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "DiscoveryThresholds":
        defaults = cls()
        return cls(
            observation_window=float(values.get(
                "observation_window", defaults.observation_window)),
            receive_timeout=float(values.get("receive_timeout", defaults.receive_timeout)),
            minimum_valid_frames=int(values.get(
                "minimum_valid_frames", defaults.minimum_valid_frames)),
            minimum_repeated_ids=int(values.get(
                "minimum_repeated_ids", defaults.minimum_repeated_ids)),
            minimum_traffic_span=float(values.get(
                "minimum_traffic_span", defaults.minimum_traffic_span)),
            minimum_window_coverage=float(values.get(
                "minimum_window_coverage", defaults.minimum_window_coverage)),
            maximum_error_ratio=float(values.get(
                "maximum_error_ratio", defaults.maximum_error_ratio)),
        )


def _valid_classic_frame(frame: CanFrame) -> bool:
    if frame.is_error_frame or frame.is_remote_frame or frame.is_fd:
        return False
    maximum_id = 0x1FFFFFFF if frame.is_extended else 0x7FF
    return (0 <= frame.arb_id <= maximum_id
            and 0 <= frame.dlc <= 8
            and len(frame.data) <= 8
            and len(frame.data) <= frame.dlc)


def _classify(
    bitrate: int,
    frames: List[Tuple[float, CanFrame]],
    duration: float,
    thresholds: DiscoveryThresholds,
) -> BitrateCandidateResult:
    total = len(frames)
    errors = sum(1 for _at, frame in frames if frame.is_error_frame)
    remotes = sum(1 for _at, frame in frames if frame.is_remote_frame)
    valid_raw = [(at, frame) for at, frame in frames if _valid_classic_frame(frame)]
    # Exact duplicates (including the source timestamp) can be produced by a
    # broken lifecycle/buffer handoff. They are not independent evidence.
    seen = set()
    valid = []
    for at, frame in valid_raw:
        fingerprint = (
            frame.timestamp, frame.channel, frame.arb_id, frame.is_extended,
            frame.dlc, frame.data,
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        valid.append((at, frame))
    duplicates = len(valid_raw) - len(valid)
    counts = Counter((frame.channel, frame.arb_id, frame.is_extended)
                     for _at, frame in valid)
    repeated = sum(1 for count in counts.values() if count >= 2)
    span = valid[-1][0] - valid[0][0] if len(valid) >= 2 else 0.0
    reasons: List[str] = []

    if total == 0:
        status = CandidateStatus.NO_TRAFFIC
        reasons.append("No frames were observed")
    elif not valid:
        status = CandidateStatus.WEAK
        reasons.append("No usable Classic CAN data frames were observed")
    elif len(valid) < thresholds.minimum_valid_frames:
        status = CandidateStatus.WEAK
        reasons.append("Only {} usable frames; at least {} are required".format(
            len(valid), thresholds.minimum_valid_frames))
    elif repeated < thresholds.minimum_repeated_ids:
        status = CandidateStatus.WEAK
        reasons.append("No identifier repeated often enough to establish continuity")
    elif errors / float(max(1, total)) > thresholds.maximum_error_ratio:
        status = CandidateStatus.WEAK
        reasons.append("Error-frame ratio exceeded {:.0%}".format(
            thresholds.maximum_error_ratio))
    else:
        required_span = max(
            thresholds.minimum_traffic_span,
            duration * thresholds.minimum_window_coverage,
        )
        if span < required_span:
            status = CandidateStatus.POSSIBLE
            reasons.append(
                "Usable traffic covered {:.3f}s; {:.3f}s is required for stability"
                .format(span, required_span))
        else:
            status = CandidateStatus.STABLE
            reasons.extend((
                "{} usable frames across {} identifier(s)".format(
                    len(valid), len(counts)),
                "{} repeated identifier(s)".format(repeated),
                "Traffic remained present across {:.3f}s of the window".format(span),
                "{} error frame(s)".format(errors),
            ))

    if total and errors:
        reasons.append("{} of {} observed frames were error frames".format(errors, total))
    if duplicates:
        reasons.append("{} exact duplicate observation(s) were ignored".format(duplicates))
    return BitrateCandidateResult(
        bitrate=bitrate,
        status=status,
        frames=total,
        valid_frames=len(valid),
        error_frames=errors,
        remote_frames=remotes,
        unique_ids=len(counts),
        repeated_ids=repeated,
        observation_duration=max(0.0, duration),
        traffic_span=max(0.0, span),
        reasons=tuple(reasons),
    )


def _failure_candidate(bitrate: int, status: CandidateStatus,
                       reason: str) -> BitrateCandidateResult:
    return BitrateCandidateResult(bitrate, status, reasons=(reason,))


def _default_external_verifier(adapter: AdapterDescriptor) -> Optional[bool]:
    if adapter.interface.lower() != "socketcan":
        return None
    # This is a read-only OS query. It neither opens nor reconfigures the bus.
    from ..sources.live import _socketcan_is_listen_only
    return _socketcan_is_listen_only(adapter.channel)


def passive_scan_available(
    adapter: AdapterDescriptor,
    external_verifier: Optional[Callable[[AdapterDescriptor], Optional[bool]]] = None,
) -> bool:
    """Return whether passive operation can be guaranteed for this scan now."""
    if adapter.passive_capability in (
            PassiveCapability.SUPPORTED_BY_BACKEND_POLICY,
            PassiveCapability.NOT_APPLICABLE):
        return True
    if adapter.passive_capability == PassiveCapability.EXTERNAL_VERIFICATION_REQUIRED:
        verifier = external_verifier or _default_external_verifier
        try:
            return verifier(adapter) is True
        except Exception:
            return False
    return False


def discover_bitrate(
    adapter: AdapterDescriptor,
    candidates: Iterable[int] = DEFAULT_BITRATES,
    base_settings: Optional[Dict[str, object]] = None,
    thresholds: DiscoveryThresholds = DiscoveryThresholds(),
    cancel_event: Optional[threading.Event] = None,
    progress: Optional[Callable[[DiscoveryProgress], None]] = None,
    source_factory: Optional[Callable[[Dict[str, object]], object]] = None,
    clock: Callable[[], float] = time.monotonic,
    safety_mode: DiscoverySafetyMode = DiscoverySafetyMode.PASSIVE_REQUIRED,
    passive_verifier: Optional[
        Callable[[AdapterDescriptor], Optional[bool]]] = None,
) -> DiscoveryResult:
    """Observe each bitrate through a fresh source under one explicit policy.

    A result is selected only when exactly one candidate is ``STABLE``.
    Multiple stable candidates are deliberately ambiguous rather than ranked
    by a fabricated confidence percentage.
    """
    safety_mode = DiscoverySafetyMode(safety_mode)
    if not adapter.implementation_supported or not adapter.auto_bitrate_supported:
        reason = adapter.scan_unavailable_reason or (
            "{} is visible but does not have a usable automatic bitrate path"
            .format(adapter.interface))
        return DiscoveryResult(
            adapter, DiscoveryStatus.UNSUPPORTED, None, (),
            reasons=(reason,), safety_mode=safety_mode,
        )

    if (safety_mode == DiscoverySafetyMode.PASSIVE_REQUIRED
            and not passive_scan_available(adapter, passive_verifier)):
        return DiscoveryResult(
            adapter, DiscoveryStatus.SAFETY_REJECTED, None, (),
            reasons=(
                "Passive/listen-only operation is not guaranteed for {}:{}; "
                "explicit per-scan non-passive authorization is required"
                .format(adapter.interface, adapter.channel),
            ),
            safety_mode=safety_mode,
        )

    rates = tuple(dict.fromkeys(int(rate) for rate in candidates if int(rate) > 0))
    if adapter.supported_bitrates:
        # Backend capability data is more specific than the global defaults.
        # Keep requested rates so unsupported skips remain visible, then add
        # supported backend-specific rates (for example SLCAN 83.3/750 kbit/s).
        rates += tuple(rate for rate in adapter.supported_bitrates
                       if rate not in rates)
    factory = source_factory or LiveSource
    stop = cancel_event or threading.Event()
    results: List[BitrateCandidateResult] = []
    total_candidates = len(rates)

    for index, bitrate in enumerate(rates):
        if stop.is_set():
            return DiscoveryResult(
                adapter, DiscoveryStatus.CANCELLED, None, tuple(results),
                reasons=("Discovery was cancelled",),
                safety_mode=safety_mode,
            )
        unsupported_reason = ""
        if adapter.supported_bitrates and bitrate not in adapter.supported_bitrates:
            unsupported_reason = "Backend/device capability list excludes this bitrate"
        elif adapter.max_bitrate is not None and bitrate > adapter.max_bitrate:
            unsupported_reason = "Bitrate exceeds device maximum of {} bit/s".format(
                adapter.max_bitrate)
        if unsupported_reason:
            candidate = _failure_candidate(
                bitrate, CandidateStatus.UNSUPPORTED, unsupported_reason)
            results.append(candidate)
            if progress is not None:
                progress(DiscoveryProgress(
                    "candidate-complete",
                    "{} bit/s: unsupported (skipped)".format(bitrate),
                    index + 1, total_candidates, candidate))
            continue
        if progress is not None:
            progress(DiscoveryProgress(
                "candidate-start", "Testing {} bit/s".format(bitrate),
                index, total_candidates))

        settings: Dict[str, object] = dict(base_settings or {})
        settings.update({
            "interface": adapter.interface,
            "channel": adapter.channel,
            "bitrate": bitrate,
            "fd": False,
            "require_listen_only": (
                safety_mode == DiscoverySafetyMode.PASSIVE_REQUIRED),
        })
        source = None
        observations: List[Tuple[float, CanFrame]] = []
        candidate: Optional[BitrateCandidateResult] = None
        try:
            source = factory(settings)
            # LiveSource.open is the Phase 1 source of truth: preflight and the
            # exact protected kwargs used for can.Bus construction are one path.
            source.open()
            passive_note = str(getattr(source, "passive_note", ""))
            observation_start = clock()
            deadline = observation_start + max(0.0, thresholds.observation_window)
            while not stop.is_set():
                now = clock()
                if now >= deadline:
                    break
                timeout = min(thresholds.receive_timeout, max(0.0, deadline - now))
                frame = source.receive(timeout=timeout)
                if frame is not None:
                    observations.append((clock() - observation_start, frame))
            duration = min(max(0.0, clock() - observation_start),
                           max(0.0, thresholds.observation_window))
            if stop.is_set():
                candidate = _failure_candidate(
                    bitrate, CandidateStatus.CANCELLED, "Discovery was cancelled")
            else:
                candidate = _classify(bitrate, observations, duration, thresholds)
                if passive_note:
                    candidate = replace(
                        candidate,
                        reasons=(passive_note,) + candidate.reasons,
                    )
        except PassiveSafetyError as exc:
            candidate = _failure_candidate(
                bitrate, CandidateStatus.SAFETY_REJECTED, str(exc))
        except SourceError as exc:
            text = str(exc)
            lowered = text.lower()
            status = (CandidateStatus.UNSUPPORTED
                      if "unsupported" in lowered and "bitrate" in lowered
                      else CandidateStatus.ERROR)
            candidate = _failure_candidate(bitrate, status, text)
        except Exception as exc:
            candidate = _failure_candidate(
                bitrate, CandidateStatus.ERROR,
                "Backend observation failed: {}".format(exc))
        finally:
            if source is not None:
                try:
                    source.close()
                except Exception as exc:
                    candidate = _failure_candidate(
                        bitrate, CandidateStatus.ERROR,
                        "Source close failed: {}".format(exc))

        assert candidate is not None
        results.append(candidate)
        log.info(
            "Discovery %s %s:%s bitrate=%s status=%s frames=%s valid=%s errors=%s",
            safety_mode.value,
            adapter.interface, adapter.channel, bitrate, candidate.status.value,
            candidate.frames, candidate.valid_frames, candidate.error_frames,
        )
        if progress is not None:
            progress(DiscoveryProgress(
                "candidate-complete",
                "{} bit/s: {}".format(bitrate, candidate.status.value),
                index + 1, total_candidates, candidate))

        if candidate.status == CandidateStatus.SAFETY_REJECTED:
            return DiscoveryResult(
                adapter, DiscoveryStatus.SAFETY_REJECTED, None, tuple(results),
                reasons=("Passive safety could not be guaranteed; remaining candidates "
                         "were not opened",),
                safety_mode=safety_mode,
            )
        if candidate.status == CandidateStatus.CANCELLED:
            return DiscoveryResult(
                adapter, DiscoveryStatus.CANCELLED, None, tuple(results),
                reasons=("Discovery was cancelled",),
                safety_mode=safety_mode,
            )

    stable = [item for item in results if item.status == CandidateStatus.STABLE]
    if len(stable) == 1:
        selected = stable[0]
        log.info("Selected Classic bitrate %s for %s:%s (%s)",
                 selected.bitrate, adapter.interface, adapter.channel,
                 safety_mode.value)
        warnings = [
            "Software-supported only; this adapter has not been electrically qualified"
        ]
        if safety_mode == DiscoverySafetyMode.USER_CONFIRMED_NON_PASSIVE:
            warnings.insert(0,
                "NON-PASSIVE observation — explicitly authorized for this scan only")
        return DiscoveryResult(
            adapter, DiscoveryStatus.DETECTED, selected.bitrate, tuple(results),
            reasons=("Exactly one candidate produced stable, sustained traffic",
                     "{} bit/s was selected".format(selected.bitrate)),
            warnings=tuple(warnings), safety_mode=safety_mode,
        )
    if len(stable) > 1:
        log.info("Passive Classic bitrate result ambiguous for %s:%s",
                 adapter.interface, adapter.channel)
        return DiscoveryResult(
            adapter, DiscoveryStatus.AMBIGUOUS, None, tuple(results),
            reasons=("Multiple candidates produced stable evidence: {}".format(
                ", ".join(str(item.bitrate) for item in stable)),),
            warnings=(("NON-PASSIVE observation — explicitly authorized for this scan only",)
                      if safety_mode == DiscoverySafetyMode.USER_CONFIRMED_NON_PASSIVE
                      else ()),
            safety_mode=safety_mode,
        )
    if results and all(item.status == CandidateStatus.NO_TRAFFIC for item in results):
        return DiscoveryResult(
            adapter, DiscoveryStatus.NO_TRAFFIC, None, tuple(results),
            reasons=("No usable traffic was observed at any candidate bitrate",),
            safety_mode=safety_mode,
        )
    return DiscoveryResult(
        adapter, DiscoveryStatus.INCONCLUSIVE, None, tuple(results),
        reasons=("No candidate produced sustained stable traffic",),
        safety_mode=safety_mode,
    )
