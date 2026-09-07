"""Passive, numerically-scored Classic CAN bitrate scanning for SocketCAN.

This is the engine behind the Auto Scan popup's checkbox-driven workflow:
the operator picks candidate bitrates (existing checkboxes -- see
``cansniff/ui/auto_scan_dialog.py``) and an observation duration, and this
module scans exactly those candidates for exactly that long, attaching a
transparent 0-100 score (``cansniff.discovery.scoring``) to every one. It
never selects a winner and never reconfigures the interface to "the best"
bitrate -- the operator picks a row in the results table and clicks "Start
Listening" themselves (``cansniff/ui/main_window.py``).

Deliberately a second, additive engine next to ``cansniff.discovery.bitrate.
discover_socketcan_bitrate``, which stays completely untouched: that
function's own STABLE/WEAK/... classification and auto-selection behavior
is still real, still used by nothing in the production UI, but still
independently tested (``tests/test_discovery.py``) and left alone rather
than risk regressing it. The two engines intentionally share almost nothing
beyond ``SocketCanLink``/``LiveSource``/``PassiveSafetyError`` -- see each
module's own docstring for why.

Per-candidate lifecycle (identical safety posture to the legacy engine):

  1. the previous candidate's receiver, if any, is already closed (this
     loop's own ``finally``);
  2. ``SocketCanLink.configure()`` -- down -> type can bitrate <rate>
     listen-only on -> up -> verify -- as one privileged, transactional
     helper call;
  3. a fresh ``LiveSource`` is opened (receive-only; see its own module
     docstring -- this never constructs a transmit-capable bus);
  4. a short settling period (``settle_seconds``, clamped to 0.1-0.25s)
     during which frames are still genuinely received but never counted --
     letting the controller/transceiver settle after a bitrate change
     before evidence starts accumulating;
  5. the candidate is observed for exactly the requested ``duration``, every
     frame (data, remote, error, FD) kept and timestamped, deliberately
     *not* deduplicated -- repeated messages are evidence, see
     ``cansniff.discovery.scoring``;
  6. the receiver is closed before the next candidate begins.

Nothing here ever transmits a CAN frame, an ACK, an error flag, or a probe
of any kind -- the only bus call anywhere in this module's dependency chain
is ``LiveSource.receive()`` -> ``Bus.recv()``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ..model import CanFrame
from ..socketcan import SocketCanError, SocketCanLink
from ..sources import PassiveSafetyError, SourceError
from ..sources.live import LiveSource
from .model import ScanProgress, ScanResult, ScoredCandidate
from .scoring import ScoringConfig, score_candidate

log = logging.getLogger(__name__)

MIN_SCAN_DURATION = 10.0
DEFAULT_SCAN_DURATION = 30.0
MIN_SETTLE_SECONDS = 0.1
MAX_SETTLE_SECONDS = 0.25
DEFAULT_SETTLE_SECONDS = 0.2

#: Live progress during one candidate's observation is throttled to this
#: cadence rather than emitted on every receive-loop iteration -- a signal
#: emitted every ~1ms across a 30s scan would flood the Qt event queue for
#: no visible benefit (the same reasoning cansniff/ui/responsive.py applies
#: to resize events).
PROGRESS_INTERVAL = 0.1


def _kbit(bitrate: int) -> str:
    return "{:g}".format(bitrate / 1000.0)


def _clamp_settle(seconds: float) -> float:
    return max(MIN_SETTLE_SECONDS, min(MAX_SETTLE_SECONDS, float(seconds)))


def _best_effort_down(link: SocketCanLink) -> None:
    """Never raises -- mirrors cansniff.discovery.bitrate's helper of the
    same name. This engine never auto-selects a winner to leave configured,
    so every exit path -- finished, cancelled, or a systemic configure
    failure -- leaves the interface down."""
    try:
        link.down()
    except SocketCanError as exc:
        log.info("Could not bring %s down after scan: %s", link.interface, exc)


def _empty_candidate(
    bitrate: int, duration: float, settle: float, completed: bool,
    observed: float, reasons: Tuple[str, ...],
) -> ScoredCandidate:
    return ScoredCandidate(
        bitrate=bitrate, requested_duration=duration, observed_duration=observed,
        settle_seconds=settle, completed=completed, total_score=0.0,
        components=None, reasons=reasons,
    )


def scan_bitrate_candidates(
    interface: str,
    candidates: Iterable[int],
    duration: float = DEFAULT_SCAN_DURATION,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    scoring_config: ScoringConfig = ScoringConfig(),
    cancel_event: Optional[threading.Event] = None,
    progress: Optional[Callable[[ScanProgress], None]] = None,
    source_factory: Optional[Callable[[Dict[str, object]], object]] = None,
    link: Optional[SocketCanLink] = None,
    clock: Callable[[], float] = time.monotonic,
) -> ScanResult:
    """Scan exactly ``candidates`` for exactly ``duration`` seconds each.

    Never selects a bitrate; always returns every candidate that was
    attempted, sorted by ``total_score`` descending (a display convenience
    only -- selection stays entirely up to whoever consumes this).
    ``duration`` is clamped up to ``MIN_SCAN_DURATION``: this function is
    the last line of defense for that floor, even though the popup's own
    duration field already enforces it.
    """
    rates = tuple(dict.fromkeys(int(rate) for rate in candidates if int(rate) > 0))
    duration = max(MIN_SCAN_DURATION, float(duration))
    settle = _clamp_settle(settle_seconds)
    factory = source_factory or LiveSource
    scan_link = link or SocketCanLink(interface)
    stop = cancel_event or threading.Event()
    results: List[ScoredCandidate] = []
    total = len(rates)

    if not rates:
        return ScanResult(interface, (), cancelled=False,
                          reasons=("No bitrate candidates were selected",))

    if progress is not None:
        progress(ScanProgress(
            "checking-interface", "Checking {}…".format(interface), 0, total))

    for index, bitrate in enumerate(rates):
        if stop.is_set():
            _best_effort_down(scan_link)
            return ScanResult(interface, tuple(results), cancelled=True,
                              reasons=("Scan was cancelled",))

        if progress is not None:
            progress(ScanProgress(
                "candidate-start", "Testing {} kbit/s…".format(_kbit(bitrate)),
                index, total, bitrate=bitrate, candidate_duration=duration))

        try:
            scan_link.configure(bitrate, listen_only=True)
        except SocketCanError as exc:
            if exc.systemic:
                _best_effort_down(scan_link)
                return ScanResult(interface, tuple(results), cancelled=False, reasons=(
                    "Could not configure {}: {} ({})".format(
                        interface, exc, exc.kind.value),
                    "Remaining candidates were not attempted",
                ))
            candidate = _empty_candidate(
                bitrate, duration, settle, False, 0.0,
                ("Could not configure {} at {} bit/s: {}".format(
                    interface, bitrate, exc),))
            results.append(candidate)
            if progress is not None:
                progress(ScanProgress(
                    "candidate-complete",
                    "{} kbit/s: configuration rejected".format(_kbit(bitrate)),
                    index + 1, total, bitrate=bitrate, candidate=candidate))
            continue

        settings: Dict[str, object] = {
            "interface": "socketcan", "channel": interface, "bitrate": bitrate,
            "fd": False, "require_listen_only": True,
        }
        source = None
        records: List[Tuple[float, CanFrame]] = []
        candidate: Optional[ScoredCandidate] = None
        observed = 0.0
        try:
            source = factory(settings)
            source.open()

            # -- settling period: real receive events, deliberately never
            # counted as evidence for this candidate (see module docstring).
            settle_deadline = clock() + settle
            while not stop.is_set() and clock() < settle_deadline:
                source.receive(timeout=min(0.05, max(0.0, settle_deadline - clock())))

            if progress is not None:
                progress(ScanProgress(
                    "candidate-listening", "Listening at {} kbit/s…".format(_kbit(bitrate)),
                    index, total, bitrate=bitrate, candidate_duration=duration))

            observation_start = clock()
            deadline = observation_start + duration
            last_progress = observation_start
            while not stop.is_set():
                now = clock()
                if now >= deadline:
                    break
                timeout = min(0.05, max(0.0, deadline - now))
                frame = source.receive(timeout=timeout)
                now = clock()
                if frame is not None:
                    records.append((now - observation_start, frame))
                if progress is not None and now - last_progress >= PROGRESS_INTERVAL:
                    last_progress = now
                    progress(ScanProgress(
                        "candidate-progress",
                        "Listening at {} kbit/s… ({} frames so far)".format(
                            _kbit(bitrate), len(records)),
                        index, total, bitrate=bitrate,
                        candidate_elapsed=now - observation_start,
                        candidate_duration=duration))

            observed = min(max(0.0, clock() - observation_start), duration)
            if stop.is_set():
                candidate = _empty_candidate(
                    bitrate, duration, settle, False, observed, ("Scan was cancelled",))
            else:
                components = score_candidate(records, duration, scoring_config)
                reasons: Tuple[str, ...] = ()
                if components.total_records == 0:
                    reasons = ("No frames were observed during the observation window",)
                candidate = ScoredCandidate(
                    bitrate=bitrate, requested_duration=duration,
                    observed_duration=observed, settle_seconds=settle, completed=True,
                    total_score=components.total_score, components=components,
                    reasons=reasons,
                )
        except PassiveSafetyError as exc:
            candidate = _empty_candidate(
                bitrate, duration, settle, False, observed,
                ("Listen-only could not be confirmed after configuration: {}".format(exc),))
        except SourceError as exc:
            candidate = _empty_candidate(
                bitrate, duration, settle, False, observed, (str(exc),))
        except Exception as exc:
            candidate = _empty_candidate(
                bitrate, duration, settle, False, observed,
                ("Backend observation failed: {}".format(exc),))
        finally:
            if source is not None:
                try:
                    source.close()
                except Exception as exc:
                    candidate = _empty_candidate(
                        bitrate, duration, settle, False, observed,
                        ("Source close failed: {}".format(exc),))

        assert candidate is not None
        results.append(candidate)
        log.info(
            "Scan %s bitrate=%s score=%.1f records=%s",
            interface, bitrate, candidate.total_score,
            candidate.components.total_records if candidate.components else 0,
        )
        if progress is not None:
            progress(ScanProgress(
                "candidate-complete",
                "{} kbit/s: score {:.1f}/100".format(_kbit(bitrate), candidate.total_score),
                index + 1, total, bitrate=bitrate, candidate=candidate))

        if stop.is_set():
            _best_effort_down(scan_link)
            return ScanResult(interface, tuple(results), cancelled=True,
                              reasons=("Scan was cancelled",))

    _best_effort_down(scan_link)
    if progress is not None:
        progress(ScanProgress("finished", "Scan complete", total, total))
    ordered = tuple(sorted(results, key=lambda item: item.total_score, reverse=True))
    return ScanResult(interface, ordered, cancelled=False)


__all__ = [
    "DEFAULT_SCAN_DURATION", "DEFAULT_SETTLE_SECONDS", "MAX_SETTLE_SECONDS",
    "MIN_SCAN_DURATION", "MIN_SETTLE_SECONDS", "scan_bitrate_candidates",
]
