"""Passive ISO-TP endpoint, UDS event, and conversation reconstruction.

This layer consumes existing ISO-TP transfer results.  It never owns a CAN
source, emits Flow Control, generates UDS payloads, or fills missing evidence.
Normal addressing is the only mode currently supplied by the reassembler.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..model import CanFrame
from .isotp import ADDRESSING, COMPLETE, IsoTpTransfer, frame_facts
from .uds import NEGATIVE, POSITIVE, REQUEST, UdsMessage, interpret


CONVERSATION_ALGORITHM_VERSION = "1.0"
DEFAULT_CONVERSATION_WINDOW = 5.0


class CorrelationStatus(str, Enum):
    PAIRED_POSITIVE = "Paired positive response"
    PAIRED_NEGATIVE = "Paired negative response"
    UNANSWERED_REQUEST = "Request - no observed response"
    ORPHAN_RESPONSE = "Response - no observed request"
    AMBIGUOUS = "Ambiguous correlation"


@dataclass(frozen=True, order=True)
class IsoTpEndpoint:
    channel: str
    can_id: int
    is_extended: bool
    addressing_mode: str = ADDRESSING
    address_extension: Optional[int] = None

    @property
    def identity(self) -> str:
        return "{}:{}:{}:{}:{}".format(
            self.channel, self.can_id, int(self.is_extended),
            self.addressing_mode,
            "" if self.address_extension is None else self.address_extension)

    @property
    def id_label(self) -> str:
        return "0x{:0{width}X}".format(
            self.can_id, width=8 if self.is_extended else 3)


@dataclass(frozen=True)
class IsoTpPeerPair:
    endpoint_a: IsoTpEndpoint
    endpoint_b: IsoTpEndpoint
    tester_like: Optional[IsoTpEndpoint]
    ecu_like: Optional[IsoTpEndpoint]
    direction_basis: str

    @property
    def pair_id(self) -> str:
        values = sorted((self.endpoint_a.identity, self.endpoint_b.identity))
        return _stable_id("peer", *values)


@dataclass(frozen=True)
class DiagnosticTransfer:
    transfer_id: str
    endpoint: IsoTpEndpoint
    peer_hint: Optional[IsoTpEndpoint]
    first_timestamp: float
    last_timestamp: float
    complete: bool
    status: str
    payload: bytes
    declared_length: int
    frame_count: int
    transfer_type: str
    flow_control: Tuple[Tuple[float, str, int, int], ...]
    diagnostics: Tuple[str, ...]
    raw_frames: Tuple[CanFrame, ...]
    caveats: Tuple[str, ...]


@dataclass(frozen=True)
class DiagnosticEvent:
    event_id: str
    transfer: DiagnosticTransfer
    uds: UdsMessage


@dataclass(frozen=True)
class DiagnosticConversation:
    conversation_id: str
    peer_pair: Optional[IsoTpPeerPair]
    request: Optional[DiagnosticEvent]
    response: Optional[DiagnosticEvent]
    correlation_status: CorrelationStatus
    first_timestamp: float
    last_timestamp: float
    latency: Optional[float]
    reasons: Tuple[str, ...]
    caveats: Tuple[str, ...]

    @property
    def service(self) -> Optional[int]:
        event = self.request or self.response
        return event.uds.service if event is not None else None

    @property
    def service_name(self) -> str:
        event = self.request or self.response
        return event.uds.service_name if event is not None else ""


@dataclass(frozen=True)
class DidObservation:
    observation_id: str
    did: int
    peer_pair: Optional[IsoTpPeerPair]
    request_transfer_id: str
    response_transfer_id: str
    raw_value: bytes
    request_timestamp: Optional[float]
    response_timestamp: Optional[float]
    provenance_frames: Tuple[CanFrame, ...]
    caveats: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DtcObservation:
    observation_id: str
    dtc: int
    status: int
    sub_function: int
    peer_pair: Optional[IsoTpPeerPair]
    request_transfer_id: str
    response_transfer_id: str
    timestamp: float
    raw_record: bytes
    provenance_frames: Tuple[CanFrame, ...]
    caveats: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DiagnosticPeerSummary:
    peer_pair: IsoTpPeerPair
    request_count: int
    positive_response_count: int
    negative_response_count: int
    unanswered_count: int
    services: Tuple[Tuple[int, str, int], ...]
    first_timestamp: float
    last_timestamp: float
    caveats: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ConversationSettings:
    response_window: float = DEFAULT_CONVERSATION_WINDOW

    def __post_init__(self) -> None:
        if not 0 < self.response_window <= 3600:
            raise ValueError("response window must be in (0, 3600] seconds")

    @property
    def identity(self) -> str:
        return "window={:.9g}".format(self.response_window)


@dataclass(frozen=True)
class DiagnosticAnalysis:
    transfers: Tuple[DiagnosticTransfer, ...]
    events: Tuple[DiagnosticEvent, ...]
    conversations: Tuple[DiagnosticConversation, ...]
    peers: Tuple[DiagnosticPeerSummary, ...]
    dids: Tuple[DidObservation, ...]
    dtcs: Tuple[DtcObservation, ...]
    caveats: Tuple[str, ...]
    algorithm_version: str
    generated_from_revision: int


def _stable_id(prefix: str, *values: object) -> str:
    text = "|".join(str(value) for value in values)
    return "{}-{}".format(
        prefix, hashlib.sha256(text.encode("utf-8")).hexdigest()[:24])


def endpoint_for_frame(frame: CanFrame) -> IsoTpEndpoint:
    return IsoTpEndpoint(
        frame.channel, frame.arb_id, frame.is_extended, ADDRESSING, None)


def normalize_isotp_transfer(
        transfer: IsoTpTransfer, ordinal: int = 0,
        peer_hint: Optional[IsoTpEndpoint] = None,
        caveats: Sequence[str] = ()) -> DiagnosticTransfer:
    first_name = frame_facts(transfer.frames[0]).name if transfer.frames else ""
    mode = "Single frame" if first_name == "SF" else "Multi frame"
    transfer_id = _stable_id(
        "isotp", transfer.key, "{:.9f}".format(transfer.first_timestamp),
        ordinal, transfer.status, transfer.declared_length)
    diagnostics = tuple(item for item in (transfer.detail,) if item)
    return DiagnosticTransfer(
        transfer_id, IsoTpEndpoint(
            transfer.channel, transfer.arb_id, transfer.is_extended),
        peer_hint, transfer.first_timestamp, transfer.last_timestamp,
        transfer.status == COMPLETE, transfer.status, bytes(transfer.data),
        transfer.declared_length, transfer.frame_count, mode,
        tuple(transfer.flow_control), diagnostics, tuple(transfer.frames),
        tuple(caveats))


def normalize_single_frame(
        frame: CanFrame, payload: bytes, ordinal: int = 0,
        peer_hint: Optional[IsoTpEndpoint] = None,
        caveats: Sequence[str] = ()) -> DiagnosticTransfer:
    transfer_id = _stable_id(
        "isotp", frame.key, "{:.9f}".format(frame.timestamp), ordinal,
        COMPLETE, len(payload))
    return DiagnosticTransfer(
        transfer_id, endpoint_for_frame(frame), peer_hint,
        frame.timestamp, frame.timestamp, True, COMPLETE, bytes(payload),
        len(payload), 1, "Single frame", (), (), (frame,), tuple(caveats))


def _valid_message(message: UdsMessage) -> bool:
    if not message.recognised or message.detail or message.service is None:
        return False
    if message.kind == NEGATIVE:
        return message.nrc is not None
    if message.service == 0x22:
        return bool(message.identifiers)
    if message.service == 0x31:
        return message.sub_function is not None and message.routine_id is not None
    if message.service in (0x10, 0x11, 0x19, 0x27, 0x28, 0x3E, 0x85):
        return message.sub_function is not None
    return True


def _semantic_compatible(request: UdsMessage, response: UdsMessage) -> bool:
    if request.service != response.service:
        return False
    if response.kind == NEGATIVE:
        return True
    if request.service == 0x22:
        return bool(set(request.identifiers).intersection(response.identifiers))
    if request.service == 0x31:
        return (request.routine_id == response.routine_id
                and request.sub_function == response.sub_function)
    if request.service in (0x10, 0x11, 0x19, 0x27, 0x3E):
        return request.sub_function == response.sub_function
    if request.identifier is not None or response.identifier is not None:
        return request.identifier == response.identifier
    return True


def _peer_pair(request: DiagnosticEvent,
               response: DiagnosticEvent) -> IsoTpPeerPair:
    endpoints = sorted((request.transfer.endpoint, response.transfer.endpoint),
                       key=lambda item: item.identity)
    return IsoTpPeerPair(
        endpoints[0], endpoints[1], request.transfer.endpoint,
        response.transfer.endpoint,
        "direction inferred from matched UDS request/response structure")


def _hint_pair(event: DiagnosticEvent,
               tester_is_event: bool) -> Optional[IsoTpPeerPair]:
    hint = event.transfer.peer_hint
    if hint is None or hint == event.transfer.endpoint:
        return None
    endpoints = sorted((event.transfer.endpoint, hint), key=lambda item: item.identity)
    tester = event.transfer.endpoint if tester_is_event else hint
    ecu = hint if tester_is_event else event.transfer.endpoint
    return IsoTpPeerPair(
        endpoints[0], endpoints[1], tester, ecu,
        "peer endpoint inferred from observed ISO-TP Flow Control timing")


def _dedupe(values: Iterable[str]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _conversation(
        status: CorrelationStatus, request: Optional[DiagnosticEvent],
        response: Optional[DiagnosticEvent], pair: Optional[IsoTpPeerPair],
        reasons: Sequence[str], common_caveats: Sequence[str]) -> DiagnosticConversation:
    events = tuple(item for item in (request, response) if item is not None)
    first = min(item.transfer.first_timestamp for item in events)
    last = max(item.transfer.last_timestamp for item in events)
    latency = (response.transfer.first_timestamp - request.transfer.last_timestamp
               if request is not None and response is not None else None)
    caveat_values = tuple(common_caveats) + tuple(
        caveat for event in events for caveat in event.transfer.caveats)
    if common_caveats:
        caveat_values += (
            "Capture integrity/retained-horizon limitations may have hidden "
            "intervening diagnostic transfers",)
    caveats = _dedupe(caveat_values)
    identity = _stable_id(
        "conversation", status.value,
        request.event_id if request is not None else "",
        response.event_id if response is not None else "")
    return DiagnosticConversation(
        identity, pair, request, response, status, first, last, latency,
        tuple(reasons), caveats)


def _event_key(event: DiagnosticEvent) -> Tuple[str, bool, str, int]:
    endpoint = event.transfer.endpoint
    return (endpoint.channel, endpoint.is_extended,
            endpoint.addressing_mode, event.uds.service or -1)


def analyze_diagnostic_transfers(
        transfers: Sequence[DiagnosticTransfer], revision: int = 0,
        common_caveats: Sequence[str] = (),
        settings: ConversationSettings = ConversationSettings(),
        cancelled: Optional[Callable[[], bool]] = None) -> DiagnosticAnalysis:
    """Correlate complete, recognised UDS transfers without raw-frame rescans."""
    ordered = tuple(sorted(
        transfers, key=lambda item: (item.first_timestamp,
                                     item.endpoint.identity, item.transfer_id)))
    events = []
    for index, transfer in enumerate(ordered):
        if cancelled is not None and index % 1024 == 0 and cancelled():
            from .protocols.survey import SurveyCancelled
            raise SurveyCancelled()
        if not transfer.complete:
            continue
        message = interpret(transfer.payload)
        if _valid_message(message):
            events.append(DiagnosticEvent(
                _stable_id("uds", transfer.transfer_id, message.kind,
                           message.service), transfer, message))

    pending: Dict[Tuple[str, bool, str, int], List[DiagnosticEvent]] = defaultdict(list)
    conversations: List[DiagnosticConversation] = []
    known_pairs = set()
    endpoint_peers: Dict[str, set] = defaultdict(set)

    def inferred_pair_for(event: DiagnosticEvent, tester_is_event: bool
                          ) -> Optional[IsoTpPeerPair]:
        hinted = _hint_pair(event, tester_is_event)
        if hinted is not None:
            return hinted
        peers = endpoint_peers.get(event.transfer.endpoint.identity, set())
        if len(peers) != 1:
            return None
        peer_endpoint = next(iter(peers))
        endpoints = sorted((event.transfer.endpoint, peer_endpoint),
                           key=lambda item: item.identity)
        tester = event.transfer.endpoint if tester_is_event else peer_endpoint
        ecu = peer_endpoint if tester_is_event else event.transfer.endpoint
        return IsoTpPeerPair(
            endpoints[0], endpoints[1], tester, ecu,
            "direction reused from earlier unambiguous UDS correlation")

    for index, event in enumerate(events):
        if cancelled is not None and index % 1024 == 0 and cancelled():
            from .protocols.survey import SurveyCancelled
            raise SurveyCancelled()
        key = _event_key(event)
        bucket = pending[key]
        retained = []
        for request in bucket:
            if event.transfer.first_timestamp - request.transfer.last_timestamp \
                    > settings.response_window:
                conversations.append(_conversation(
                    CorrelationStatus.UNANSWERED_REQUEST, request, None,
                    inferred_pair_for(request, True),
                    ("no compatible response was observed within {:.3g}s".format(
                        settings.response_window),), common_caveats))
            else:
                retained.append(request)
        pending[key] = bucket = retained

        if event.uds.kind == REQUEST:
            bucket.append(event)
            continue

        candidates = []
        for request in bucket:
            if request.transfer.endpoint == event.transfer.endpoint:
                continue
            if not _semantic_compatible(request.uds, event.uds):
                continue
            hint = event.transfer.peer_hint
            if hint is not None and request.transfer.endpoint != hint:
                continue
            request_hint = request.transfer.peer_hint
            if request_hint is not None and event.transfer.endpoint != request_hint:
                continue
            candidates.append(request)
        if len(candidates) > 1:
            established = [request for request in candidates
                           if frozenset((request.transfer.endpoint.identity,
                                         event.transfer.endpoint.identity))
                           in known_pairs]
            if established:
                candidates = established
        if len(candidates) == 1:
            request = candidates[0]
            # NRC 0x78 is an observed interim response.  Keep the request
            # outstanding so a later final positive/negative response can
            # still correlate to the same evidence.  Nothing is retried or
            # generated; this only preserves chronology already in capture.
            response_pending = event.uds.kind == NEGATIVE and event.uds.nrc == 0x78
            if not response_pending:
                bucket.remove(request)
            pair = _peer_pair(request, event)
            pair_key = frozenset((pair.endpoint_a.identity, pair.endpoint_b.identity))
            known_pairs.add(pair_key)
            endpoint_peers[pair.endpoint_a.identity].add(pair.endpoint_b)
            endpoint_peers[pair.endpoint_b.identity].add(pair.endpoint_a)
            status = (CorrelationStatus.PAIRED_NEGATIVE
                      if event.uds.kind == NEGATIVE
                      else CorrelationStatus.PAIRED_POSITIVE)
            reasons = [
                "opposite endpoints on the same channel/addressing mode",
                "response service matches request service 0x{:02X}".format(
                    request.uds.service or 0),
                "response arrived within {:.3g}s".format(settings.response_window),
            ]
            if event.uds.kind == POSITIVE:
                reasons.append("positive response SID equals request SID + 0x40")
            else:
                reasons.append("negative response explicitly references the request service")
                if response_pending:
                    reasons.append(
                        "NRC 0x78 is an interim response; request remains outstanding")
            if request.uds.identifiers or request.uds.routine_id is not None \
                    or request.uds.sub_function is not None:
                reasons.append("service-specific DID/subfunction/routine fields agree")
            conversations.append(_conversation(
                status, request, event, pair, reasons, common_caveats))
        elif candidates:
            conversations.append(_conversation(
                CorrelationStatus.AMBIGUOUS, None, event, None,
                ("{} outstanding requests match service-specific and timing rules"
                 .format(len(candidates)),
                 "no request was chosen solely by nearest timestamp"),
                common_caveats))
        else:
            conversations.append(_conversation(
                CorrelationStatus.ORPHAN_RESPONSE, None, event,
                inferred_pair_for(event, False),
                ("no compatible observed request precedes this response",),
                common_caveats))

    capture_end = max((item.last_timestamp for item in ordered), default=0.0)
    for bucket in pending.values():
        for request in bucket:
            if capture_end - request.transfer.last_timestamp > settings.response_window:
                reasons = [
                    "no compatible response was observed within {:.3g}s".format(
                        settings.response_window)]
            else:
                reasons = ["no compatible response was observed before capture end"]
            if request.uds.suppress_positive_response:
                reasons.append(
                    "the observed request set suppressPositiveResponse; absence is not failure")
            conversations.append(_conversation(
                CorrelationStatus.UNANSWERED_REQUEST, request, None,
                inferred_pair_for(request, True), reasons, common_caveats))

    conversations.sort(key=lambda item: (
        item.first_timestamp, item.last_timestamp, item.conversation_id))
    dids = _did_observations(conversations)
    dtcs = _dtc_observations(conversations)
    peers = _peer_summaries(conversations)
    return DiagnosticAnalysis(
        ordered, tuple(events), tuple(conversations), peers, dids, dtcs,
        tuple(common_caveats), CONVERSATION_ALGORITHM_VERSION, revision)


def _did_observations(
        conversations: Sequence[DiagnosticConversation]) -> Tuple[DidObservation, ...]:
    results = []
    for conversation in conversations:
        request = conversation.request
        response = conversation.response
        if request is not None and request.uds.service == 0x22:
            identifiers = request.uds.identifiers
            for did in identifiers:
                raw = b""
                caveats = list(conversation.caveats)
                if (response is not None and response.uds.kind == POSITIVE
                        and len(identifiers) == 1
                        and response.uds.identifier == did):
                    raw = response.uds.payload[2:]
                elif response is not None and len(identifiers) > 1:
                    caveats.append(
                        "multi-DID response value boundaries require local DID lengths; raw response retained")
                frames = tuple(request.transfer.raw_frames) + (
                    tuple(response.transfer.raw_frames) if response is not None else ())
                results.append(DidObservation(
                    _stable_id("did", conversation.conversation_id, did), did,
                    conversation.peer_pair, request.transfer.transfer_id,
                    response.transfer.transfer_id if response is not None else "",
                    raw, request.transfer.first_timestamp,
                    response.transfer.first_timestamp if response is not None else None,
                    frames, _dedupe(caveats)))
        elif (request is None and response is not None
              and response.uds.service == 0x22 and response.uds.identifier is not None):
            results.append(DidObservation(
                _stable_id("did", conversation.conversation_id,
                           response.uds.identifier), response.uds.identifier,
                conversation.peer_pair, "", response.transfer.transfer_id,
                response.uds.payload[2:], None, response.transfer.first_timestamp,
                tuple(response.transfer.raw_frames),
                _dedupe(conversation.caveats + (
                    "response was unpaired; additional DID boundaries are unknown",))))
    return tuple(results)


def _dtc_observations(
        conversations: Sequence[DiagnosticConversation]) -> Tuple[DtcObservation, ...]:
    results = []
    for conversation in conversations:
        response = conversation.response
        if (response is None or response.uds.service != 0x19
                or response.uds.kind != POSITIVE
                or response.uds.sub_function not in (0x02, 0x0A)):
            continue
        request_id = (conversation.request.transfer.transfer_id
                      if conversation.request is not None else "")
        for dtc, status in response.uds.dtc_entries:
            raw = dtc.to_bytes(3, "big") + bytes((status,))
            results.append(DtcObservation(
                _stable_id("dtc", conversation.conversation_id, dtc, status),
                dtc, status, response.uds.sub_function,
                conversation.peer_pair, request_id,
                response.transfer.transfer_id, response.transfer.first_timestamp,
                raw, tuple(response.transfer.raw_frames), conversation.caveats))
    return tuple(results)


def _peer_summaries(
        conversations: Sequence[DiagnosticConversation]
        ) -> Tuple[DiagnosticPeerSummary, ...]:
    groups: Dict[str, List[DiagnosticConversation]] = defaultdict(list)
    pair_by_id = {}
    for conversation in conversations:
        if conversation.peer_pair is None:
            continue
        pair_id = conversation.peer_pair.pair_id
        groups[pair_id].append(conversation)
        pair_by_id[pair_id] = conversation.peer_pair
    values = []
    for pair_id, items in groups.items():
        services = Counter(item.service for item in items if item.service is not None)
        service_rows = tuple((service, next(
            (item.service_name for item in items if item.service == service), ""), count)
            for service, count in sorted(services.items()))
        request_ids = {item.request.event_id for item in items
                       if item.request is not None}
        values.append(DiagnosticPeerSummary(
            pair_by_id[pair_id], len(request_ids),
            sum(item.correlation_status is CorrelationStatus.PAIRED_POSITIVE
                for item in items),
            sum(item.correlation_status is CorrelationStatus.PAIRED_NEGATIVE
                for item in items),
            sum(item.correlation_status is CorrelationStatus.UNANSWERED_REQUEST
                for item in items), service_rows,
            min(item.first_timestamp for item in items),
            max(item.last_timestamp for item in items),
            _dedupe(caveat for item in items for caveat in item.caveats)))
    return tuple(sorted(values, key=lambda item: (
        item.peer_pair.endpoint_a.identity, item.peer_pair.endpoint_b.identity)))


class DiagnosticConversationCache:
    """Revision/settings/integrity cache over already-normalized transfers."""

    def __init__(self):
        self._key = None
        self._result: Optional[DiagnosticAnalysis] = None

    def build(self, transfers: Sequence[DiagnosticTransfer], cache_identity: tuple,
              revision: int = 0, common_caveats: Sequence[str] = (),
              settings: ConversationSettings = ConversationSettings(),
              cancelled: Optional[Callable[[], bool]] = None) -> DiagnosticAnalysis:
        key = (cache_identity, revision, tuple(common_caveats), settings.identity,
               CONVERSATION_ALGORITHM_VERSION)
        if self._key != key or self._result is None:
            result = analyze_diagnostic_transfers(
                transfers, revision, common_caveats, settings, cancelled)
            if cancelled is not None and cancelled():
                from .protocols.survey import SurveyCancelled
                raise SurveyCancelled()
            self._key, self._result = key, result
        return self._result

    def clear(self) -> None:
        self._key = None
        self._result = None


__all__ = [
    "CONVERSATION_ALGORITHM_VERSION", "DEFAULT_CONVERSATION_WINDOW",
    "ConversationSettings", "CorrelationStatus", "DiagnosticAnalysis",
    "DiagnosticConversation", "DiagnosticConversationCache",
    "DiagnosticEvent", "DiagnosticPeerSummary", "DiagnosticTransfer",
    "DidObservation", "DtcObservation", "IsoTpEndpoint", "IsoTpPeerPair",
    "analyze_diagnostic_transfers", "endpoint_for_frame",
    "normalize_isotp_transfer", "normalize_single_frame",
]
