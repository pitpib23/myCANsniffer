"""Bounded, passive SAE J1939-21 TP.CM / TP.DT observation.

This module only consumes immutable frames.  It has no source, socket, CAN
driver, response, acknowledgement, or transmit surface.  Control constants,
the 1785-byte TP limit, and conservative receive timeouts mirror the Linux
J1939 implementation documented in ``net/can/j1939/transport.c``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

from ..profile import CaptureIntegritySnapshot
from ..store import StoreWindow
from .j1939 import PGN_TP_CM, PGN_TP_DT, parse_j1939_id


TP_CM_RTS = 0x10
TP_CM_CTS = 0x11
TP_CM_END_OF_MESSAGE_ACK = 0x13
TP_CM_BAM = 0x20
TP_CM_ABORT = 0xFF
TP_MAX_PACKETS = 255
TP_MAX_BYTES = TP_MAX_PACKETS * 7
TP_MIN_BYTES = 9
TP_BAM_TIMEOUT_SECONDS = 0.750
TP_PEER_TIMEOUT_SECONDS = 1.250


class J1939TransportKind(str, Enum):
    BAM = "BAM"
    RTS_CTS = "RTS/CTS"
    ORPHAN = "Orphan TP.DT"


class J1939TransportStatus(str, Enum):
    COMPLETE = "Complete"
    INCOMPLETE = "Incomplete"
    MALFORMED = "Malformed"
    ABORTED = "Aborted"


@dataclass(frozen=True)
class J1939RawFrameReference:
    message_key: str
    can_id: int
    timestamp: float
    role: str
    sequence: Optional[int] = None


@dataclass(frozen=True)
class J1939TransportPacket:
    sequence: int
    data: bytes
    timestamp: float
    message_key: str
    duplicate_count: int = 0
    conflicting_duplicate: bool = False


@dataclass(frozen=True)
class J1939TransportControl:
    control: int
    name: str
    source_address: int
    destination_address: int
    transported_pgn: int
    timestamp: float
    message_key: str


@dataclass(frozen=True)
class J1939PayloadObservation:
    channel: str
    pgn: int
    source_address: int
    destination_address: Optional[int]
    priority: int
    payload: bytes
    transport: str
    complete: bool
    first_timestamp: float
    last_timestamp: float
    raw_frames: Tuple[J1939RawFrameReference, ...]
    caveats: Tuple[str, ...] = ()


@dataclass(frozen=True)
class J1939TransportSession:
    session_id: str
    channel: str
    transport_kind: J1939TransportKind
    source_address: int
    destination_address: int
    transported_pgn: int
    announced_size: int
    announced_packets: int
    received_packets: int
    sequence_status: str
    status: J1939TransportStatus
    payload: bytes
    first_timestamp: float
    last_timestamp: float
    packets: Tuple[J1939TransportPacket, ...]
    controls: Tuple[J1939TransportControl, ...]
    raw_frames: Tuple[J1939RawFrameReference, ...]
    diagnostics: Tuple[str, ...]
    caveats: Tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.status is J1939TransportStatus.COMPLETE


@dataclass(frozen=True)
class J1939TransportAnalysis:
    sessions: Tuple[J1939TransportSession, ...]
    payloads: Tuple[J1939PayloadObservation, ...]
    orphan_frame_count: int
    generated_from_revision: int


@dataclass
class _MutableSession:
    serial: int
    channel: str
    kind: J1939TransportKind
    source: int
    destination: int
    pgn: int
    size: int
    packet_count: int
    priority: int
    first: float
    last: float
    controls: List[J1939TransportControl] = field(default_factory=list)
    frames: List[J1939RawFrameReference] = field(default_factory=list)
    packets: Dict[int, bytes] = field(default_factory=dict)
    packet_times: Dict[int, float] = field(default_factory=dict)
    packet_keys: Dict[int, str] = field(default_factory=dict)
    duplicate_counts: Dict[int, int] = field(default_factory=dict)
    conflicting: set = field(default_factory=set)
    diagnostics: List[str] = field(default_factory=list)
    malformed: bool = False
    aborted: bool = False
    cts_seen: bool = False
    ack_seen: bool = False
    replaced: bool = False
    cts_windows: List[Tuple[int, int]] = field(default_factory=list)

    @property
    def timeout(self) -> float:
        return (TP_BAM_TIMEOUT_SECONDS if self.kind is J1939TransportKind.BAM
                else TP_PEER_TIMEOUT_SECONDS)


def _cancelled(cancelled: Optional[Callable[[], bool]], index: int) -> None:
    if cancelled is not None and index % 1024 == 0 and cancelled():
        from .survey import SurveyCancelled
        raise SurveyCancelled()


def _pgn(data: bytes) -> int:
    value = data[5] | (data[6] << 8) | (data[7] << 16)
    return value & 0x3FFFF


def _integrity_caveats(integrity: Optional[CaptureIntegritySnapshot],
                       horizon_complete: bool) -> Tuple[str, ...]:
    values = []
    if not horizon_complete:
        values.append(
            "Retained history is incomplete; the transport may have started "
            "before the retained horizon")
    if integrity is not None:
        if (integrity.ui_dropped or integrity.pause_hidden
                or integrity.source_errors or integrity.parse_errors
                or (integrity.driver_overruns or 0)):
            values.append(
                "Known capture loss/errors may have removed sequence-sensitive "
                "transport evidence")
        if integrity.driver_overruns is None:
            values.append("Hardware/driver loss visibility is unavailable")
    return tuple(values)


def _control(frame, identifier, name: str, transported_pgn: int
             ) -> J1939TransportControl:
    return J1939TransportControl(
        frame.data[0], name, identifier.source_address,
        identifier.destination_address if identifier.destination_address is not None else 0xFF,
        transported_pgn, frame.timestamp, frame.key)


def _frame_ref(frame, role: str, sequence: Optional[int] = None
               ) -> J1939RawFrameReference:
    return J1939RawFrameReference(
        frame.key, frame.arb_id, frame.timestamp, role, sequence)


def _sequence_status(session: _MutableSession) -> str:
    if session.kind is J1939TransportKind.ORPHAN:
        return "ORPHAN"
    if session.conflicting:
        return "CONFLICTING_DUPLICATE"
    expected = set(range(1, session.packet_count + 1))
    seen = set(session.packets).intersection(expected)
    if seen == expected and expected:
        return "COMPLETE"
    if any(sequence > session.packet_count for sequence in session.packets):
        return "EXCESS"
    if seen:
        return "MISSING_OR_OUT_OF_ORDER"
    return "NO_DATA"


def _partial_payload(session: _MutableSession) -> bytes:
    values = []
    for sequence in range(1, session.packet_count + 1):
        data = session.packets.get(sequence)
        if data is None:
            break
        values.append(data)
    return b"".join(values)[:session.size]


def _snapshot(session: _MutableSession, common_caveats: Tuple[str, ...],
              final_reason: str = "") -> J1939TransportSession:
    diagnostics = list(session.diagnostics)
    if final_reason and final_reason not in diagnostics:
        diagnostics.append(final_reason)
    sequence = _sequence_status(session)
    payload = _partial_payload(session)
    data_complete = (sequence == "COMPLETE" and len(payload) == session.size
                     and session.packet_count == int(math.ceil(session.size / 7.0)))
    protocol_complete = data_complete and (
        session.kind is J1939TransportKind.BAM
        or (session.cts_seen and session.ack_seen))
    if session.aborted:
        status = J1939TransportStatus.ABORTED
    elif session.malformed or session.conflicting:
        status = J1939TransportStatus.MALFORMED
    elif protocol_complete:
        status = J1939TransportStatus.COMPLETE
    else:
        status = J1939TransportStatus.INCOMPLETE
    packets = tuple(J1939TransportPacket(
        number, session.packets[number], session.packet_times[number],
        session.packet_keys[number], session.duplicate_counts.get(number, 0),
        number in session.conflicting) for number in sorted(session.packets))
    return J1939TransportSession(
        "{}:{}:{:02X}:{:02X}:{:05X}:{}".format(
            session.channel, session.serial, session.source,
            session.destination, session.pgn, session.kind.value),
        session.channel, session.kind, session.source, session.destination,
        session.pgn, session.size, session.packet_count, len(session.packets),
        sequence, status, payload, session.first, session.last, packets,
        tuple(session.controls), tuple(session.frames), tuple(diagnostics),
        common_caveats)


def analyze_j1939_transport(
        window: StoreWindow,
        integrity: Optional[CaptureIntegritySnapshot] = None,
        horizon_complete: bool = True,
        cancelled: Optional[Callable[[], bool]] = None,
        ) -> J1939TransportAnalysis:
    """Single chronological pass over retained frames.

    TP.DT without a matching announcement is retained as orphan evidence and
    never becomes an application payload.  Peer-to-peer data becomes complete
    only when an observed CTS and EndOfMsgACK bracket the received packets.
    """
    active: Dict[Tuple[str, int, int], _MutableSession] = {}
    orphan_active: Dict[Tuple[str, int, int], _MutableSession] = {}
    sessions: List[J1939TransportSession] = []
    payloads: List[J1939PayloadObservation] = []
    serial = 0
    common_caveats = _integrity_caveats(integrity, horizon_complete)

    def finish(collection, key, reason=""):
        value = collection.pop(key, None)
        if value is None:
            return
        result = _snapshot(value, common_caveats, reason)
        sessions.append(result)
        if result.complete:
            payloads.append(J1939PayloadObservation(
                result.channel, result.transported_pgn, result.source_address,
                None if result.destination_address == 0xFF
                else result.destination_address,
                value.priority, result.payload, result.transport_kind.value,
                True, result.first_timestamp, result.last_timestamp,
                result.raw_frames, result.caveats))

    def expire(now: float):
        if not math.isfinite(now):
            return
        for collection in (active, orphan_active):
            for key, value in list(collection.items()):
                if now >= value.last and now - value.last > value.timeout:
                    finish(collection, key, "Incomplete - timeout")

    for index, frame in enumerate(window):
        _cancelled(cancelled, index)
        # All capture traffic advances passive time, not just extended J1939
        # candidates.  This prevents an otherwise quiet TP link from staying
        # active indefinitely while standard frames continue to arrive.
        expire(frame.timestamp)
        if not frame.is_extended or frame.is_error_frame or frame.is_remote_frame:
            continue
        identifier = parse_j1939_id(frame.arb_id)
        if identifier.pgn not in (PGN_TP_CM, PGN_TP_DT):
            payloads.append(J1939PayloadObservation(
                frame.channel, identifier.pgn, identifier.source_address,
                identifier.destination_address, identifier.priority, frame.data,
                "Single frame", True, frame.timestamp, frame.timestamp,
                (_frame_ref(frame, "single-frame payload"),), common_caveats))
            continue
        if len(frame.data) != 8:
            # Retain malformed management/data frames as orphan evidence.
            serial += 1
            kind = (J1939TransportKind.ORPHAN if identifier.pgn == PGN_TP_DT
                    else J1939TransportKind.RTS_CTS)
            key = (frame.channel, identifier.source_address,
                   identifier.destination_address or 0xFF)
            value = _MutableSession(
                serial, frame.channel, kind, identifier.source_address,
                identifier.destination_address or 0xFF, 0, 0, 0,
                identifier.priority, frame.timestamp, frame.timestamp,
                malformed=True)
            value.frames.append(_frame_ref(frame, "malformed TP frame"))
            value.diagnostics.append("TP.CM/TP.DT frame length is not 8 bytes")
            orphan_active[key] = value
            finish(orphan_active, key)
            continue

        destination = (identifier.destination_address
                       if identifier.destination_address is not None else 0xFF)
        link = (frame.channel, identifier.source_address, destination)
        if identifier.pgn == PGN_TP_CM:
            control = frame.data[0]
            transported_pgn = _pgn(frame.data)
            if control in (TP_CM_BAM, TP_CM_RTS):
                if link in active:
                    active[link].replaced = True
                    finish(active, link, "New announcement replaced an active session")
                serial += 1
                kind = (J1939TransportKind.BAM if control == TP_CM_BAM
                        else J1939TransportKind.RTS_CTS)
                size = frame.data[1] | (frame.data[2] << 8)
                packets = frame.data[3]
                value = _MutableSession(
                    serial, frame.channel, kind, identifier.source_address,
                    destination, transported_pgn, size, packets,
                    identifier.priority, frame.timestamp, frame.timestamp)
                value.controls.append(_control(
                    frame, identifier, "BAM" if kind is J1939TransportKind.BAM
                    else "RTS", transported_pgn))
                value.frames.append(_frame_ref(frame, "TP.CM announcement"))
                expected = int(math.ceil(size / 7.0)) if size else 0
                if kind is J1939TransportKind.BAM and destination != 0xFF:
                    value.malformed = True
                    value.diagnostics.append("BAM destination is not global 0xFF")
                if kind is J1939TransportKind.BAM and frame.data[4] != 0xFF:
                    value.malformed = True
                    value.diagnostics.append("BAM reserved byte is not 0xFF")
                if kind is J1939TransportKind.RTS_CTS and destination == 0xFF:
                    value.malformed = True
                    value.diagnostics.append("RTS uses the global destination")
                if (kind is J1939TransportKind.RTS_CTS
                        and not 1 <= frame.data[4] <= TP_MAX_PACKETS):
                    value.malformed = True
                    value.diagnostics.append("RTS maximum-packets-per-CTS is invalid")
                if not TP_MIN_BYTES <= size <= TP_MAX_BYTES:
                    value.malformed = True
                    value.diagnostics.append(
                        "Announced size {} is outside bounded J1939-21 TP range "
                        "{}..{}".format(size, TP_MIN_BYTES, TP_MAX_BYTES))
                if not 1 <= packets <= TP_MAX_PACKETS:
                    value.malformed = True
                    value.diagnostics.append("Announced packet count is outside 1..255")
                if packets != expected:
                    value.malformed = True
                    value.diagnostics.append(
                        "Packet count {} does not equal ceil({}/7) = {}".format(
                            packets, size, expected))
                if transported_pgn in (PGN_TP_CM, PGN_TP_DT):
                    value.malformed = True
                    value.diagnostics.append(
                        "Transported PGN is itself a TP management/data PGN")
                active[link] = value
                continue

            if control in (TP_CM_CTS, TP_CM_END_OF_MESSAGE_ACK, TP_CM_ABORT):
                reverse = (frame.channel, destination,
                           identifier.source_address)
                value = active.get(reverse)
                if (value is None or value.pgn != transported_pgn
                        or value.kind is not J1939TransportKind.RTS_CTS):
                    serial += 1
                    orphan = _MutableSession(
                        serial, frame.channel, J1939TransportKind.ORPHAN,
                        destination, identifier.source_address, transported_pgn,
                        0, 0, identifier.priority, frame.timestamp, frame.timestamp,
                        malformed=True)
                    orphan.controls.append(_control(
                        frame, identifier, "orphan control", transported_pgn))
                    orphan.frames.append(_frame_ref(frame, "orphan TP.CM control"))
                    orphan.diagnostics.append(
                        "{} has no matching active RTS session".format(
                            {TP_CM_CTS: "CTS", TP_CM_END_OF_MESSAGE_ACK: "EndOfMsgACK",
                             TP_CM_ABORT: "Abort"}[control]))
                    orphan_active[link] = orphan
                    finish(orphan_active, link)
                    continue
                if frame.timestamp < value.last:
                    value.diagnostics.append(
                        "Out-of-order capture timestamp observed on TP.CM response")
                value.last = max(value.last, frame.timestamp)
                value.frames.append(_frame_ref(frame, "TP.CM response"))
                if control == TP_CM_CTS:
                    value.cts_seen = True
                    value.controls.append(_control(frame, identifier, "CTS", transported_pgn))
                    requested = frame.data[1]
                    first_packet = frame.data[2]
                    if not requested or not 1 <= first_packet <= value.packet_count:
                        value.malformed = True
                        value.diagnostics.append("CTS packet window is invalid")
                    elif first_packet + requested - 1 > value.packet_count:
                        value.malformed = True
                        value.diagnostics.append(
                            "CTS packet window exceeds the announced packet count")
                    else:
                        value.cts_windows.append(
                            (first_packet, first_packet + requested - 1))
                    if frame.data[3:5] != b"\xFF\xFF":
                        value.malformed = True
                        value.diagnostics.append("CTS reserved bytes are not 0xFF")
                elif control == TP_CM_END_OF_MESSAGE_ACK:
                    value.ack_seen = True
                    value.controls.append(_control(
                        frame, identifier, "EndOfMsgACK", transported_pgn))
                    ack_size = frame.data[1] | (frame.data[2] << 8)
                    if ack_size != value.size or frame.data[3] != value.packet_count:
                        value.malformed = True
                        value.diagnostics.append(
                            "EndOfMsgACK size/packet count conflicts with RTS")
                    if frame.data[4] != 0xFF:
                        value.malformed = True
                        value.diagnostics.append(
                            "EndOfMsgACK reserved byte is not 0xFF")
                    finish(active, reverse)
                else:
                    value.aborted = True
                    value.controls.append(_control(frame, identifier, "Abort", transported_pgn))
                    value.diagnostics.append(
                        "Observed Abort reason 0x{:02X}".format(frame.data[1]))
                    finish(active, reverse)
                continue

            serial += 1
            malformed = _MutableSession(
                serial, frame.channel, J1939TransportKind.ORPHAN,
                identifier.source_address, destination, transported_pgn, 0, 0,
                identifier.priority, frame.timestamp, frame.timestamp,
                malformed=True)
            malformed.controls.append(_control(
                frame, identifier, "unknown control", transported_pgn))
            malformed.frames.append(_frame_ref(frame, "malformed TP.CM control"))
            malformed.diagnostics.append(
                "Unsupported/malformed TP.CM control 0x{:02X}".format(control))
            orphan_active[link] = malformed
            finish(orphan_active, link)
            continue

        # TP.DT: the sender/destination link is the same direction as RTS/BAM.
        sequence = frame.data[0]
        value = active.get(link)
        if value is None:
            value = orphan_active.get(link)
            if value is None or sequence == 1:
                if value is not None:
                    finish(orphan_active, link, "A new orphan sequence started")
                serial += 1
                value = _MutableSession(
                    serial, frame.channel, J1939TransportKind.ORPHAN,
                    identifier.source_address, destination, 0, 0, 0,
                    identifier.priority, frame.timestamp, frame.timestamp)
                value.diagnostics.append("TP.DT observed without an active TP.CM announcement")
                orphan_active[link] = value
        if frame.timestamp < value.last:
            value.diagnostics.append(
                "Out-of-order capture timestamp observed on TP.DT")
        value.last = max(value.last, frame.timestamp)
        value.frames.append(_frame_ref(frame, "TP.DT", sequence))
        if not 1 <= sequence <= TP_MAX_PACKETS:
            value.malformed = True
            value.diagnostics.append("TP.DT sequence number 0 is invalid")
            continue
        data = bytes(frame.data[1:8])
        if sequence in value.packets:
            value.duplicate_counts[sequence] = value.duplicate_counts.get(sequence, 0) + 1
            if value.packets[sequence] == data:
                value.diagnostics.append(
                    "Exact duplicate TP.DT packet {} observed".format(sequence))
            else:
                value.conflicting.add(sequence)
                value.malformed = True
                value.diagnostics.append(
                    "TP.DT packet {} was repeated with conflicting bytes".format(sequence))
            continue
        if value.kind is not J1939TransportKind.ORPHAN:
            if sequence > value.packet_count:
                value.malformed = True
                value.diagnostics.append(
                    "Excess TP.DT packet {} exceeds announced count {}".format(
                        sequence, value.packet_count))
            expected_next = max(value.packets, default=0) + 1
            if sequence != expected_next:
                value.malformed = True
                value.diagnostics.append(
                    "Out-of-order/skipped TP.DT packet: expected {}, observed {}".format(
                        expected_next, sequence))
            if value.kind is J1939TransportKind.RTS_CTS:
                if not value.cts_seen:
                    value.malformed = True
                    value.diagnostics.append(
                        "Peer TP.DT arrived before an observed CTS")
                elif not any(first <= sequence <= last
                             for first, last in value.cts_windows):
                    value.malformed = True
                    value.diagnostics.append(
                        "Peer TP.DT packet {} is outside observed CTS windows"
                        .format(sequence))
        value.packets[sequence] = data
        value.packet_times[sequence] = frame.timestamp
        value.packet_keys[sequence] = frame.key
        if (value.kind is J1939TransportKind.BAM
                and len(value.packets) >= value.packet_count):
            finish(active, link)

    if window.frames:
        expire(window.frames[-1].timestamp)
    for key in list(active):
        finish(active, key, "Incomplete - retained capture ended before session completion")
    for key in list(orphan_active):
        finish(orphan_active, key, "Incomplete orphan transport evidence")
    sessions.sort(key=lambda item: (item.first_timestamp, item.channel,
                                    item.source_address, item.session_id))
    payloads.sort(key=lambda item: (item.first_timestamp, item.channel,
                                    item.source_address, item.pgn))
    orphan_count = sum(item.transport_kind is J1939TransportKind.ORPHAN
                       for item in sessions)
    return J1939TransportAnalysis(
        tuple(sessions), tuple(payloads), orphan_count, window.revision)


__all__ = [
    "J1939PayloadObservation", "J1939RawFrameReference",
    "J1939TransportAnalysis", "J1939TransportControl", "J1939TransportKind",
    "J1939TransportPacket", "J1939TransportSession", "J1939TransportStatus",
    "TP_BAM_TIMEOUT_SECONDS", "TP_CM_ABORT", "TP_CM_BAM", "TP_CM_CTS",
    "TP_CM_END_OF_MESSAGE_ACK", "TP_CM_RTS", "TP_MAX_BYTES", "TP_MAX_PACKETS",
    "TP_PEER_TIMEOUT_SECONDS", "analyze_j1939_transport",
]
