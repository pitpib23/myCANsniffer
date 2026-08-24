"""Pure J1939 29-bit parsing and conservative passive traffic evidence."""

from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Set, Tuple

from ..store import StoreWindow
from .model import EvidenceLevel

if TYPE_CHECKING:
    from .j1939_transport import J1939TransportAnalysis


PGN_REQUEST = 0x00EA00       # 59904
PGN_TP_DT = 0x00EB00         # 60160
PGN_TP_CM = 0x00EC00         # 60416
PGN_ADDRESS_CLAIM = 0x00EE00 # 60928


@dataclass(frozen=True)
class J1939Identifier:
    can_id: int
    priority: int
    reserved: int
    data_page: int
    pdu_format: int
    pdu_specific: int
    source_address: int
    destination_address: Optional[int]
    pgn: int
    is_pdu1: bool


@dataclass(frozen=True)
class J1939MessageObservation:
    message_key: str
    can_id: int
    channel: str
    pgn: int
    source_address: int
    destination_address: Optional[int]
    priority: int
    count: int
    first_seen: float
    last_seen: float
    payload_lengths: Tuple[Tuple[int, int], ...]
    management_shape: str = ""


@dataclass(frozen=True)
class J1939SourceObservation:
    channel: str
    source_address: int
    pgns: Tuple[int, ...]
    stable_pgns: Tuple[int, ...]
    destinations: Tuple[int, ...]
    message_keys: Tuple[str, ...]
    frame_count: int
    first_seen: float
    last_seen: float


@dataclass(frozen=True)
class J1939Detection:
    level: EvidenceLevel
    reasons: Tuple[str, ...]
    related_keys: Tuple[str, ...]
    likely_keys: Tuple[str, ...]
    messages: Tuple[J1939MessageObservation, ...]
    sources: Tuple[J1939SourceObservation, ...]
    transport_sessions: Tuple[object, ...] = ()
    payloads: Tuple[object, ...] = ()


def parse_j1939_id(can_id: int) -> J1939Identifier:
    """Parse one valid 29-bit identifier without claiming it is J1939.

    For PDU1 (PF < 240), PS is a destination and is zeroed in the PGN. For
    PDU2, PS is the group extension and therefore contributes to the PGN.
    """
    if can_id < 0 or can_id > 0x1FFFFFFF:
        raise ValueError("J1939 identifier must fit in 29 bits")
    priority = (can_id >> 26) & 0x7
    reserved = (can_id >> 25) & 0x1
    data_page = (can_id >> 24) & 0x1
    pf = (can_id >> 16) & 0xFF
    ps = (can_id >> 8) & 0xFF
    source = can_id & 0xFF
    pgn_base = (reserved << 17) | (data_page << 16) | (pf << 8)
    is_pdu1 = pf < 240
    destination = ps if is_pdu1 else None
    pgn = pgn_base if is_pdu1 else pgn_base | ps
    return J1939Identifier(
        can_id, priority, reserved, data_page, pf, ps, source,
        destination, pgn, is_pdu1,
    )


def _management_shape(identifier: J1939Identifier, data: bytes) -> str:
    if identifier.pgn == PGN_ADDRESS_CLAIM and len(data) == 8:
        return "Address Claim"
    if identifier.pgn == PGN_REQUEST and len(data) == 3:
        requested = data[0] | (data[1] << 8) | (data[2] << 16)
        if requested <= 0x3FFFF:
            return "Request"
    if identifier.pgn == PGN_TP_CM and len(data) == 8 \
            and data[0] in (0x10, 0x11, 0x13, 0x20, 0xFF):
        return "TP.CM"
    if identifier.pgn == PGN_TP_DT and len(data) == 8 and data[0] != 0:
        return "TP.DT"
    return ""


class _Message:
    def __init__(self, frame, identifier: J1939Identifier):
        self.frame = frame
        self.identifier = identifier
        self.count = 0
        self.first = frame.timestamp
        self.last = frame.timestamp
        self.lengths: Counter = Counter()
        self.management: Counter = Counter()

    def add(self, frame) -> None:
        self.count += 1
        self.last = frame.timestamp
        self.lengths[len(frame.data)] += 1
        shape = _management_shape(self.identifier, frame.data)
        if shape:
            self.management[shape] += 1

    def snapshot(self) -> J1939MessageObservation:
        shape = self.management.most_common(1)[0][0] if self.management else ""
        i = self.identifier
        return J1939MessageObservation(
            self.frame.key, self.frame.arb_id, self.frame.channel, i.pgn,
            i.source_address, i.destination_address, i.priority, self.count,
            self.first, self.last, tuple(sorted(self.lengths.items())), shape,
        )


def detect_j1939(window: StoreWindow,
                 cancelled: Optional[Callable[[], bool]] = None,
                 transport: Optional["J1939TransportAnalysis"] = None,
                 ) -> J1939Detection:
    aggregates: "OrderedDict[str, _Message]" = OrderedDict()
    for index, frame in enumerate(window):
        if cancelled is not None and index % 1024 == 0 and cancelled():
            from .survey import SurveyCancelled
            raise SurveyCancelled()
        if not frame.is_extended or frame.is_error_frame or frame.is_remote_frame:
            continue
        identifier = parse_j1939_id(frame.arb_id)
        aggregate = aggregates.get(frame.key)
        if aggregate is None:
            aggregate = aggregates[frame.key] = _Message(frame, identifier)
        aggregate.add(frame)

    messages = tuple(item.snapshot() for item in aggregates.values())
    source_data: "OrderedDict[Tuple[str, int], Dict[str, object]]" = OrderedDict()
    for message in messages:
        key = (message.channel, message.source_address)
        data = source_data.get(key)
        if data is None:
            data = source_data[key] = {
                "pgn_counts": Counter(), "destinations": set(), "keys": set(),
                "frames": 0, "first": message.first_seen, "last": message.last_seen,
            }
        data["pgn_counts"][message.pgn] += message.count
        if message.destination_address is not None:
            data["destinations"].add(message.destination_address)
        data["keys"].add(message.message_key)
        data["frames"] += message.count
        data["first"] = min(data["first"], message.first_seen)
        data["last"] = max(data["last"], message.last_seen)

    sources: List[J1939SourceObservation] = []
    for (channel, source), data in source_data.items():
        counts = data["pgn_counts"]
        sources.append(J1939SourceObservation(
            channel, source, tuple(sorted(counts)),
            tuple(sorted(pgn for pgn, count in counts.items() if count >= 2)),
            tuple(sorted(data["destinations"])), tuple(sorted(data["keys"])),
            data["frames"], data["first"], data["last"],
        ))
    sources_tuple = tuple(sorted(sources, key=lambda item: (item.channel,
                                                             item.source_address)))

    management = [item for item in messages if item.management_shape]
    coherent = [item for item in sources_tuple
                if len(item.stable_pgns) >= 3 and item.frame_count >= 6]
    strongly_coherent = []
    for source in sources_tuple:
        source_messages = [item for item in messages
                           if item.channel == source.channel
                           and item.source_address == source.source_address]
        has_pdu1 = any(item.destination_address is not None for item in source_messages)
        has_pdu2 = any(item.destination_address is None for item in source_messages)
        if (len(source.stable_pgns) >= 4 and source.frame_count >= 12
                and has_pdu1 and has_pdu2):
            strongly_coherent.append(source)

    address_claim_sources = {
        (item.channel, item.source_address) for item in management
        if item.management_shape == "Address Claim"
    }
    claimed_coherent = [item for item in sources_tuple
                        if (item.channel, item.source_address) in address_claim_sources
                        and len(item.stable_pgns) >= 2]

    reasons: List[str] = []
    if strongly_coherent or len(coherent) >= 2 or claimed_coherent:
        level = EvidenceLevel.STRONG
        if strongly_coherent:
            reasons.append("{} source address{} repeatedly emitted at least four stable "
                           "PGNs with coherent PDU1 and PDU2 use".format(
                               len(strongly_coherent),
                               "" if len(strongly_coherent) == 1 else "es"))
        if len(coherent) >= 2:
            reasons.append("multiple source addresses each have repeated multi-PGN traffic")
        if claimed_coherent:
            reasons.append("valid Address Claim structure correlates with repeated PGNs")
    elif coherent or management:
        level = EvidenceLevel.POSSIBLE
        if coherent:
            reasons.append("repeated stable PGNs correlate to one source address")
        if management:
            reasons.append("J1939-specific management shapes observed: {}".format(
                ", ".join(sorted({item.management_shape for item in management}))))
    elif messages:
        level = EvidenceLevel.WEAK
        reasons.append("29-bit identifiers parse into J1939 fields, but source/PGN "
                       "correlation is insufficient")
    else:
        level = EvidenceLevel.NONE
        reasons.append("no usable 29-bit data traffic observed")

    if transport is not None:
        complete = sum(item.complete for item in transport.sessions)
        incomplete = len(transport.sessions) - complete
        if complete:
            reasons.append(
                "{} complete passive J1939 transport session{} reassembled".format(
                    complete, "" if complete == 1 else "s"))
        if incomplete:
            reasons.append(
                "{} incomplete/malformed/orphan transport session{} retained as evidence"
                .format(incomplete, "" if incomplete == 1 else "s"))
        # Reassembly is useful corroboration, but one syntactically valid BAM
        # can occur in lookalike 29-bit traffic and never proves a network.
        if level is EvidenceLevel.WEAK and complete >= 2:
            level = EvidenceLevel.POSSIBLE

    for source in (strongly_coherent or coherent)[:3]:
        reasons.append("source 0x{:02X}{}: {} PGNs, {} frames".format(
            source.source_address,
            " on " + source.channel if source.channel else "",
            len(source.pgns), source.frame_count))

    related = tuple(sorted(item.message_key for item in messages))
    likely: Set[str] = set()
    coherent_keys = {(item.channel, item.source_address)
                     for item in strongly_coherent + coherent + claimed_coherent}
    for item in messages:
        if ((item.channel, item.source_address) in coherent_keys
                or bool(item.management_shape)):
            likely.add(item.message_key)
    return J1939Detection(
        level, tuple(reasons), related, tuple(sorted(likely)), messages,
        sources_tuple, (() if transport is None else transport.sessions),
        (() if transport is None else transport.payloads))


__all__ = [
    "J1939Detection", "J1939Identifier", "J1939MessageObservation",
    "J1939SourceObservation", "PGN_ADDRESS_CLAIM", "PGN_REQUEST", "PGN_TP_CM",
    "PGN_TP_DT", "detect_j1939", "parse_j1939_id",
]
