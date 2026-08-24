"""Conservative, passive Classic CAN bitrate discovery for SocketCAN.

For each candidate bitrate this module:

  1. brings the configured interface (default ``can0``) down;
  2. configures it as Classic CAN at that bitrate with listen-only forced on;
  3. brings it back up;
  4. opens it through the ordinary receive-only ``LiveSource`` and passively
     collects frames for a bounded observation window;
  5. closes that temporary source before moving to the next candidate.

Every one of those steps goes through ``cansniff.socketcan.SocketCanLink``
(structured ``ip link`` calls, never a shell) and ``cansniff.sources.live.
LiveSource`` (the only place a CAN bus is ever opened, and only ever for
``recv()``). Nothing here sends a frame, probes a node, or falls back to a
non-listen-only mode to "get some traffic".

A bitrate is selected only when exactly one candidate reaches ``STABLE``
evidence -- multiple simultaneously-stable candidates are reported as
``AMBIGUOUS`` rather than resolved by guessing.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, replace
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from ..model import CanFrame
from ..socketcan import SocketCanError, SocketCanLink
from ..sources import PassiveSafetyError, SourceError
from ..sources.live import LiveSource
from .model import (
    BitrateCandidateResult, CandidateStatus, DiscoveryProgress, DiscoveryResult,
    DiscoveryStatus,
)

log = logging.getLogger(__name__)

DEFAULT_BITRATES = (
    10_000, 20_000, 33_333, 50_000, 83_333, 100_000,
    125_000, 250_000, 500_000, 800_000, 1_000_000,
)

DEFAULT_INTERFACE = "can0"


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


def _best_evidence_id(
    valid: List[Tuple[float, CanFrame]],
) -> Tuple[Optional[int], int, float]:
    """The single strongest/stablest identifier observed, for diagnostics only.

    Lightweight on purpose: observation count is the primary signal, ties
    broken by the longer first-to-last span (more evidence of continuity),
    then by numeric identifier for determinism. This is never itself used to
    decide whether a candidate is STABLE -- see _classify -- only to report a
    concrete, inspectable "best evidence ID" alongside that verdict.
    """
    if not valid:
        return None, 0, 0.0
    by_id: Dict[int, List[float]] = {}
    for at, frame in valid:
        by_id.setdefault(frame.arb_id, []).append(at)
    best_id = max(
        by_id,
        key=lambda arb_id: (
            len(by_id[arb_id]),
            max(by_id[arb_id]) - min(by_id[arb_id]),
            -arb_id,
        ),
    )
    timestamps = by_id[best_id]
    return best_id, len(timestamps), max(timestamps) - min(timestamps)


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
    best_id, best_id_observations, best_id_span = _best_evidence_id(valid)
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

    if best_id is not None:
        reasons.append(
            "Best evidence ID: 0x{:X} ({} observation(s) across {:.3f}s)".format(
                best_id, best_id_observations, best_id_span))
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
        best_id=best_id,
        best_id_observations=best_id_observations,
        best_id_span=max(0.0, best_id_span),
        reasons=tuple(reasons),
    )


def _failure_candidate(bitrate: int, status: CandidateStatus,
                       reason: str) -> BitrateCandidateResult:
    return BitrateCandidateResult(bitrate, status, reasons=(reason,))


def _kbit(bitrate: int) -> str:
    """Human-facing rate for progress text, e.g. 125000 -> "125", 33333 ->
    "33.333" -- matches the kbit/s figures already used throughout the UI
    and README (10, 20, 33.333, 50, 83.333, 100, 125, 250, 500, 800, 1000)."""
    return "{:g}".format(bitrate / 1000.0)


def _best_effort_down(link: SocketCanLink) -> None:
    """Leave the interface in one deterministic resting state after a scan
    that did not end in a selected bitrate: down, rather than whatever
    candidate happened to be configured last. Never raises -- this runs
    during failure/cancellation cleanup, where a secondary exception must
    not mask the real result.
    """
    try:
        link.down()
    except SocketCanError as exc:
        log.info("Could not bring %s down after discovery: %s", link.interface, exc)


def discover_socketcan_bitrate(
    interface: str = DEFAULT_INTERFACE,
    candidates: Iterable[int] = DEFAULT_BITRATES,
    thresholds: DiscoveryThresholds = DiscoveryThresholds(),
    cancel_event: Optional[threading.Event] = None,
    progress: Optional[Callable[[DiscoveryProgress], None]] = None,
    source_factory: Optional[Callable[[Dict[str, object]], object]] = None,
    link: Optional[SocketCanLink] = None,
    clock: Callable[[], float] = time.monotonic,
) -> DiscoveryResult:
    """Passively determine ``interface``'s Classic CAN bitrate.

    A result is selected only when exactly one candidate is ``STABLE``.
    Multiple stable candidates are deliberately ambiguous rather than ranked
    by a fabricated confidence score.
    """
    rates = tuple(dict.fromkeys(int(rate) for rate in candidates if int(rate) > 0))
    factory = source_factory or LiveSource
    scan_link = link or SocketCanLink(interface)
    stop = cancel_event or threading.Event()
    results: List[BitrateCandidateResult] = []
    total_candidates = len(rates)

    for index, bitrate in enumerate(rates):
        if stop.is_set():
            _best_effort_down(scan_link)
            return DiscoveryResult(
                interface, DiscoveryStatus.CANCELLED, None, tuple(results),
                reasons=("Discovery was cancelled",))

        if progress is not None:
            progress(DiscoveryProgress(
                "candidate-start", "Testing {} kbit/s…".format(_kbit(bitrate)),
                index, total_candidates))

        try:
            scan_link.configure(bitrate, listen_only=True)
        except SocketCanError as exc:
            if exc.systemic:
                _best_effort_down(scan_link)
                return DiscoveryResult(
                    interface, DiscoveryStatus.CONFIGURATION_ERROR, None, tuple(results),
                    reasons=(
                        "Could not configure {}: {} ({})".format(
                            interface, exc, exc.kind.value),
                        "Remaining candidates were not attempted",
                    ))
            candidate = _failure_candidate(
                bitrate, CandidateStatus.CONFIGURATION_ERROR,
                "Could not configure {} at {} bit/s: {}".format(
                    interface, bitrate, exc))
            results.append(candidate)
            if progress is not None:
                progress(DiscoveryProgress(
                    "candidate-complete",
                    "{} kbit/s: configuration rejected".format(_kbit(bitrate)),
                    index + 1, total_candidates, candidate))
            continue

        settings: Dict[str, object] = {
            "interface": "socketcan",
            "channel": interface,
            "bitrate": bitrate,
            "fd": False,
            "require_listen_only": True,
        }
        source = None
        observations: List[Tuple[float, CanFrame]] = []
        candidate: Optional[BitrateCandidateResult] = None
        try:
            source = factory(settings)
            # LiveSource.open is the source of truth for passive enforcement:
            # it independently re-verifies listen-only at the OS level before
            # this loop is allowed to observe anything.
            source.open()
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
        except PassiveSafetyError as exc:
            candidate = _failure_candidate(
                bitrate, CandidateStatus.ERROR,
                "Listen-only could not be confirmed after configuration: {}".format(exc))
        except SourceError as exc:
            candidate = _failure_candidate(bitrate, CandidateStatus.ERROR, str(exc))
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
            "Discovery %s bitrate=%s status=%s frames=%s valid=%s errors=%s",
            interface, bitrate, candidate.status.value,
            candidate.frames, candidate.valid_frames, candidate.error_frames,
        )
        if progress is not None:
            progress(DiscoveryProgress(
                "candidate-complete",
                "{} kbit/s: {}".format(_kbit(bitrate), candidate.status.value),
                index + 1, total_candidates, candidate))

        if candidate.status == CandidateStatus.CANCELLED:
            _best_effort_down(scan_link)
            return DiscoveryResult(
                interface, DiscoveryStatus.CANCELLED, None, tuple(results),
                reasons=("Discovery was cancelled",))

    stable = [item for item in results if item.status == CandidateStatus.STABLE]
    if len(stable) == 1:
        selected = stable[0]
        try:
            # Candidates tried after the winner leave the interface configured
            # at whatever rate was tested last -- explicitly reconfigure it to
            # the winning bitrate before declaring it detected.
            scan_link.configure(selected.bitrate, listen_only=True)
        except SocketCanError as exc:
            _best_effort_down(scan_link)
            return DiscoveryResult(
                interface, DiscoveryStatus.CONFIGURATION_ERROR, None, tuple(results),
                reasons=(
                    "{} bit/s had stable evidence, but reconfiguring {} to it "
                    "failed: {}".format(selected.bitrate, interface, exc),
                ))
        log.info("Selected Classic bitrate %s for %s", selected.bitrate, interface)
        reasons = [
            "Exactly one candidate produced stable, sustained traffic",
            "{} bit/s was selected".format(selected.bitrate),
        ]
        if selected.best_id is not None:
            reasons.append(
                "Best evidence ID: 0x{:X} ({} observation(s), {:.3f}s span)".format(
                    selected.best_id, selected.best_id_observations, selected.best_id_span))
        return DiscoveryResult(
            interface, DiscoveryStatus.DETECTED, selected.bitrate, tuple(results),
            reasons=tuple(reasons),
            warnings=("Software-supported only; this link has not been "
                      "electrically qualified",),
        )
    if len(stable) > 1:
        log.info("Passive Classic bitrate result ambiguous for %s", interface)
        _best_effort_down(scan_link)
        return DiscoveryResult(
            interface, DiscoveryStatus.AMBIGUOUS, None, tuple(results),
            reasons=("Multiple candidates produced stable evidence: {}".format(
                ", ".join(str(item.bitrate) for item in stable)),))
    _best_effort_down(scan_link)
    if results and all(item.status == CandidateStatus.NO_TRAFFIC for item in results):
        return DiscoveryResult(
            interface, DiscoveryStatus.NO_TRAFFIC, None, tuple(results),
            reasons=("No usable traffic was observed at any candidate bitrate",))
    return DiscoveryResult(
        interface, DiscoveryStatus.INCONCLUSIVE, None, tuple(results),
        reasons=("No candidate produced sustained stable traffic",))


__all__ = [
    "DEFAULT_BITRATES", "DEFAULT_INTERFACE", "DiscoveryThresholds",
    "discover_socketcan_bitrate",
]
