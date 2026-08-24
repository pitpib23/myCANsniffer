"""Unified passive Protocol Survey orchestration and revision cache."""

from __future__ import annotations

from collections import Counter
from typing import Callable, Dict, List, Optional, Set, Tuple

from ..isotp import COMPLETE, SINGLE, frame_facts
from ..diagnostics import (
    CorrelationStatus, DiagnosticConversationCache, IsoTpEndpoint,
    normalize_isotp_transfer, normalize_single_frame,
)
from ..isotp_survey import (
    NONE as ISOTP_NONE, POSSIBLE as ISOTP_POSSIBLE, STRONG as ISOTP_STRONG,
    WEAK as ISOTP_WEAK, survey as survey_isotp,
)
from ..profile import AnalysisHorizon, HorizonKind, TrafficProfileSnapshot
from ..store import StoreWindow
from ..uds import (
    NEGATIVE, POSITIVE, REQUEST, UdsMessage, interpret,
)
from .canopen import CanopenDetection, detect_canopen
from .j1939 import J1939Detection, detect_j1939
from .j1939_transport import analyze_j1939_transport
from .model import (
    EvidenceLevel, ProtocolEvidence, ProtocolKind, ProtocolSurveySnapshot,
    UdsObservation,
)


class SurveyCancelled(Exception):
    """Cooperative cancellation used by the UI-owned survey worker."""


_ISOTP_LEVELS = {
    ISOTP_NONE: EvidenceLevel.NONE,
    ISOTP_WEAK: EvidenceLevel.WEAK,
    ISOTP_POSSIBLE: EvidenceLevel.POSSIBLE,
    ISOTP_STRONG: EvidenceLevel.STRONG,
}


def _retained_horizon(window: StoreWindow,
                      profile: TrafficProfileSnapshot) -> AnalysisHorizon:
    first = window.frames[0].timestamp if window.frames else None
    last = window.frames[-1].timestamp if window.frames else None
    return AnalysisHorizon(
        HorizonKind.RETAINED, first, last, len(window),
        profile.retained_horizon.complete and len(window) == profile.retained_horizon.frame_count,
    )


def _common_caveats(profile: TrafficProfileSnapshot) -> Tuple[str, ...]:
    integrity = profile.integrity
    caveats: List[str] = []
    if not profile.retained_horizon.complete:
        caveats.append("Retained history is incomplete because earlier frames were evicted")
    if integrity.ui_dropped:
        caveats.append("{} frames were dropped before application analysis".format(
            integrity.ui_dropped))
    if integrity.pause_hidden:
        caveats.append("{} frames were hidden from analysis while display was paused".format(
            integrity.pause_hidden))
    if integrity.source_errors:
        caveats.append("{} source/backend errors were observed".format(
            integrity.source_errors))
    if integrity.parse_errors:
        caveats.append("{} capture-file records could not be parsed".format(
            integrity.parse_errors))
    if integrity.driver_overruns is None:
        caveats.append("Hardware/driver loss visibility is unavailable")
    elif integrity.driver_overruns:
        caveats.append("{} hardware/driver overruns were reported".format(
            integrity.driver_overruns))
    out_of_order = sum(message.out_of_order_timestamps for message in profile.messages)
    if out_of_order:
        caveats.append("{} per-message timestamp deltas were out of order".format(
            out_of_order))
    return tuple(caveats)


def _sequence_loss(profile: TrafficProfileSnapshot) -> bool:
    integrity = profile.integrity
    return bool(integrity.ui_dropped or integrity.pause_hidden
                or integrity.source_errors or integrity.parse_errors
                or (integrity.driver_overruns or 0))


def _evidence(protocol: ProtocolKind, level: EvidenceLevel,
              reasons: Tuple[str, ...], related: Tuple[str, ...],
              horizon: AnalysisHorizon, profile: TrafficProfileSnapshot,
              caveats: Tuple[str, ...]) -> ProtocolEvidence:
    return ProtocolEvidence(protocol, level, reasons, related, horizon,
                            profile.integrity, caveats)


def _adapt_isotp(window: StoreWindow, profile: TrafficProfileSnapshot,
                 horizon: AnalysisHorizon,
                 cancelled: Optional[Callable[[], bool]]):
    if cancelled is not None and cancelled():
        raise SurveyCancelled()
    rows, by_key = survey_isotp(window)
    if cancelled is not None and cancelled():
        raise SurveyCancelled()
    strongest = max((_ISOTP_LEVELS.get(row.evidence, EvidenceLevel.NONE)
                     for row in rows), default=EvidenceLevel.NONE,
                    key=lambda item: item.rank)
    strong_rows = [row for row in rows if row.evidence == ISOTP_STRONG]
    possible_rows = [row for row in rows if row.evidence == ISOTP_POSSIBLE]
    weak_rows = [row for row in rows if row.evidence == ISOTP_WEAK]
    reasons: List[str] = []
    if strong_rows:
        reasons.append("{} CAN ID{} completed coherent multi-frame ISO-TP sequences".format(
            len(strong_rows), "" if len(strong_rows) == 1 else "s"))
    elif possible_rows:
        reasons.append("multi-frame ISO-TP machinery was observed but not fully validated")
    elif weak_rows:
        reasons.append("only ISO-TP-compatible single-frame syntax was observed")
    else:
        reasons.append("no meaningful ISO-TP syntax or sequence evidence was observed")
    for row in (strong_rows or possible_rows or weak_rows)[:3]:
        reasons.append("{}: {}".format(row.id_label, row.why()))

    caveats = list(_common_caveats(profile))
    if _sequence_loss(profile) and strongest is EvidenceLevel.STRONG:
        strongest = EvidenceLevel.POSSIBLE
        caveats.append("ISO-TP sequence strength was reduced because frames may be missing")
    related = tuple(sorted(row.key for row in rows
                           if row.evidence != ISOTP_NONE))
    likely = tuple(sorted(row.key for row in rows
                          if _ISOTP_LEVELS.get(row.evidence, EvidenceLevel.NONE).rank
                          >= EvidenceLevel.POSSIBLE.rank))
    return (_evidence(ProtocolKind.ISOTP, strongest, tuple(reasons), related,
                      horizon, profile, tuple(caveats)), rows, by_key, likely)


def _valid_uds(message: UdsMessage) -> bool:
    if not message.recognised or message.detail:
        return False
    if message.kind == NEGATIVE:
        return message.service is not None and message.nrc is not None
    if message.service in (0x22, 0x24, 0x2A, 0x2C, 0x2E):
        return message.identifier is not None
    if message.service in (0x10, 0x11, 0x19, 0x27, 0x28, 0x31, 0x3E, 0x85):
        return message.sub_function is not None
    return message.service is not None


def _uds_observation(key: str, arb_id: int, channel: str, is_extended: bool,
                     timestamp: float, message: UdsMessage) -> UdsObservation:
    return UdsObservation(
        key, arb_id, channel, is_extended, timestamp, message.kind,
        message.service or 0, message.service_name,
        message.sub_function, message.identifier, message.nrc,
        message.describe(),
    )


def _detect_uds(window: StoreWindow, profile: TrafficProfileSnapshot,
                horizon: AnalysisHorizon, isotp_rows, transfers_by_key,
                cancelled: Optional[Callable[[], bool]],
                diagnostic_cache: Optional[DiagnosticConversationCache] = None):
    common_caveats = _common_caveats(profile)
    row_by_key = {row.key: row for row in isotp_rows}
    diagnostic_transfers = []
    multi_keys = set(transfers_by_key)
    for key, transfers in transfers_by_key.items():
        row = row_by_key.get(key)
        peer_hint = (IsoTpEndpoint(row.channel, row.peer_arb_id, row.is_extended)
                     if row is not None and row.peer_arb_id is not None else None)
        for ordinal, transfer in enumerate(transfers):
            diagnostic_transfers.append(normalize_isotp_transfer(
                transfer, ordinal, peer_hint, common_caveats))

    # Single Frames are already complete ISO-TP reconstructions. Read their
    # declared payload directly to avoid allocating tens of thousands of
    # transfer objects for a periodic non-diagnostic ID.
    candidate_keys = {row.key for row in isotp_rows
                      if row.evidence != ISOTP_NONE and row.key not in multi_keys}
    single_ordinals: Dict[str, int] = {}
    for index, frame in enumerate(window):
        if cancelled is not None and index % 1024 == 0 and cancelled():
            raise SurveyCancelled()
        if frame.key not in candidate_keys:
            continue
        facts = frame_facts(frame)
        if facts.pci_type != SINGLE or facts.problem:
            continue
        message = interpret(facts.payload)
        if _valid_uds(message):
            ordinal = single_ordinals.get(frame.key, 0)
            single_ordinals[frame.key] = ordinal + 1
            row = row_by_key.get(frame.key)
            peer_hint = (IsoTpEndpoint(row.channel, row.peer_arb_id, row.is_extended)
                         if row is not None and row.peer_arb_id is not None else None)
            diagnostic_transfers.append(normalize_single_frame(
                frame, facts.payload, ordinal, peer_hint, common_caveats))

    cache = diagnostic_cache or DiagnosticConversationCache()
    analysis = cache.build(
        tuple(diagnostic_transfers), window.cache_key, window.revision,
        common_caveats, cancelled=cancelled)
    observations = [_uds_observation(
        event.transfer.raw_frames[0].key,
        event.transfer.endpoint.can_id, event.transfer.endpoint.channel,
        event.transfer.endpoint.is_extended, event.transfer.first_timestamp,
        event.uds) for event in analysis.events]

    observations.sort(key=lambda item: (item.timestamp, item.channel,
                                         item.arb_id, item.kind))
    pairs = sum(item.correlation_status in (
        CorrelationStatus.PAIRED_POSITIVE,
        CorrelationStatus.PAIRED_NEGATIVE) for item in analysis.conversations)

    services = Counter(item.service for item in observations)
    multi_complete = sum(
        event.transfer.transfer_type == "Multi frame" for event in analysis.events)
    reasons: List[str] = []
    if pairs >= 2 or (pairs >= 1 and multi_complete >= 1):
        level = EvidenceLevel.STRONG
        reasons.append("{} reconstructed UDS request/response pair{} observed".format(
            pairs, "" if pairs == 1 else "s"))
    elif pairs:
        level = EvidenceLevel.POSSIBLE
        reasons.append("a reconstructed UDS request/response pair was observed")
    elif observations:
        level = EvidenceLevel.WEAK
        reasons.append("reconstructed ISO-TP payloads have recognised UDS shapes, "
                       "but no request/response correlation was observed")
    else:
        level = EvidenceLevel.NONE
        reasons.append("no complete reconstructed ISO-TP payload has coherent UDS syntax")
    if services:
        reasons.append("observed services: {}".format(
            ", ".join("0x{:02X} {} ({})".format(
                service, next((item.service_name for item in observations
                               if item.service == service), ""), count)
                      for service, count in sorted(services.items()))))

    caveats = list(_common_caveats(profile))
    if _sequence_loss(profile) and level is EvidenceLevel.STRONG:
        level = EvidenceLevel.POSSIBLE
        caveats.append("UDS sequence strength was reduced because frames may be missing")
    related = tuple(sorted({item.message_key for item in observations}))
    return (_evidence(ProtocolKind.UDS, level, tuple(reasons), related,
                      horizon, profile, tuple(caveats)), tuple(observations),
            related if level.rank >= EvidenceLevel.POSSIBLE.rank else (),
            analysis)


def _unknown_evidence(window: StoreWindow, profile: TrafficProfileSnapshot,
                      horizon: AnalysisHorizon, known_keys: Set[str]
                      ) -> ProtocolEvidence:
    usable = {frame.key for frame in window if not frame.is_error_frame}
    caveats = _common_caveats(profile)
    if not usable:
        return _evidence(
            ProtocolKind.UNKNOWN, EvidenceLevel.NONE,
            ("No protocol classification possible — no usable traffic observed",),
            (), horizon, profile, caveats)
    unexplained = usable - known_keys
    if unexplained:
        if known_keys:
            reasons = (
                "Usable traffic remains outside the message keys supported by known-protocol evidence",
                "Known and unexplained traffic can coexist in the same session",
            )
        else:
            reasons = (
                "Usable CAN traffic was observed",
                "CANopen, J1939, ISO-TP and UDS evidence is insufficient for a stronger interpretation",
            )
        if _sequence_loss(profile):
            reasons += ("Classification is limited by incomplete capture evidence",)
        return _evidence(ProtocolKind.UNKNOWN, EvidenceLevel.POSSIBLE, reasons,
                         tuple(sorted(unexplained)), horizon, profile, caveats)
    return _evidence(
        ProtocolKind.UNKNOWN, EvidenceLevel.NONE,
        ("Every retained message key is covered by possible-or-strong protocol evidence",),
        (), horizon, profile, caveats)


def build_protocol_survey(window: StoreWindow, profile: TrafficProfileSnapshot,
                          cancelled: Optional[Callable[[], bool]] = None,
                          diagnostic_cache: Optional[DiagnosticConversationCache] = None,
                          ) -> ProtocolSurveySnapshot:
    """Build one immutable multi-protocol conclusion from one retained window."""
    horizon = _retained_horizon(window, profile)
    caveats = _common_caveats(profile)
    canopen: CanopenDetection = detect_canopen(window, cancelled)
    transport = analyze_j1939_transport(
        window, profile.integrity, horizon.complete, cancelled)
    j1939: J1939Detection = detect_j1939(window, cancelled, transport)
    isotp, isotp_rows, transfers_by_key, isotp_likely = _adapt_isotp(
        window, profile, horizon, cancelled)
    uds, uds_observations, uds_likely, diagnostic_analysis = _detect_uds(
        window, profile, horizon, isotp_rows, transfers_by_key, cancelled,
        diagnostic_cache)

    canopen_evidence = _evidence(
        ProtocolKind.CANOPEN, canopen.level, canopen.reasons,
        canopen.related_keys, horizon, profile, caveats)
    j1939_evidence = _evidence(
        ProtocolKind.J1939, j1939.level, j1939.reasons,
        j1939.related_keys, horizon, profile, caveats)
    known = set(canopen.likely_keys) | set(j1939.likely_keys) \
        | set(isotp_likely) | set(uds_likely)
    unknown = _unknown_evidence(window, profile, horizon, known)
    results = (canopen_evidence, j1939_evidence, isotp, uds, unknown)
    return ProtocolSurveySnapshot(
        results=results, generated_from_revision=window.revision,
        horizon=horizon, integrity_context=profile.integrity,
        canopen_nodes=canopen.nodes, j1939_messages=j1939.messages,
        j1939_sources=j1939.sources,
        j1939_transport_sessions=j1939.transport_sessions,
        j1939_payloads=j1939.payloads, uds_observations=uds_observations,
        diagnostic_analysis=diagnostic_analysis,
    )


def _cache_key(window: StoreWindow, profile: TrafficProfileSnapshot) -> tuple:
    return (
        window.cache_key, profile.retained_horizon.complete,
        profile.integrity, profile.invalid_timestamps,
        tuple((item.key, item.out_of_order_timestamps)
              for item in profile.messages if item.out_of_order_timestamps),
    )


class ProtocolSurveyCache:
    """One-result cache keyed by retained revision and integrity context."""

    def __init__(self):
        self._key: Optional[tuple] = None
        self._snapshot: Optional[ProtocolSurveySnapshot] = None
        self._diagnostic_cache = DiagnosticConversationCache()

    def lookup(self, window: StoreWindow,
               profile: TrafficProfileSnapshot) -> Optional[ProtocolSurveySnapshot]:
        return self._snapshot if self._key == _cache_key(window, profile) else None

    def build(self, window: StoreWindow, profile: TrafficProfileSnapshot,
              cancelled: Optional[Callable[[], bool]] = None
              ) -> ProtocolSurveySnapshot:
        key = _cache_key(window, profile)
        if key != self._key or self._snapshot is None:
            snapshot = build_protocol_survey(
                window, profile, cancelled, self._diagnostic_cache)
            if cancelled is not None and cancelled():
                raise SurveyCancelled()
            self._key = key
            self._snapshot = snapshot
        return self._snapshot

    def clear(self) -> None:
        self._key = None
        self._snapshot = None
        self._diagnostic_cache.clear()


__all__ = ["ProtocolSurveyCache", "SurveyCancelled", "build_protocol_survey"]
