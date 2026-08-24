"""Conservative passive CANopen structure and node correlation.

COB-ID ranges are compatibility observations only.  Classification requires
correlation of multiple communication-object classes for the same channel and
legal node ID; one numerically matching identifier never proves CANopen.
"""

from __future__ import annotations

import math
from collections import Counter, OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Set, Tuple

from ...model import CanFrame
from ..store import StoreWindow
from .model import EvidenceLevel


class CanopenObjectClass(str, Enum):
    NMT = "NMT"
    SYNC = "SYNC"
    EMCY = "EMCY"
    TPDO1 = "TPDO1"
    RPDO1 = "RPDO1"
    TPDO2 = "TPDO2"
    RPDO2 = "RPDO2"
    TPDO3 = "TPDO3"
    RPDO3 = "RPDO3"
    TPDO4 = "TPDO4"
    RPDO4 = "RPDO4"
    SDO_RESPONSE = "SDO response"
    SDO_REQUEST = "SDO request"
    HEARTBEAT = "Heartbeat / boot-up"


@dataclass(frozen=True)
class CanopenCobId:
    object_class: CanopenObjectClass
    node_id: Optional[int]


@dataclass(frozen=True)
class CanopenNodeObservation:
    channel: str
    node_id: int
    object_classes: Tuple[str, ...]
    message_keys: Tuple[str, ...]
    frame_count: int
    first_seen: float
    last_seen: float
    heartbeat_frames: int
    valid_heartbeat_frames: int
    heartbeat_states: Tuple[Tuple[int, int], ...]
    heartbeat_average_period: Optional[float]
    heartbeat_jitter_stddev: Optional[float]
    sdo_request_frames: int
    sdo_response_frames: int
    sdo_pairs: int
    evidence_level: EvidenceLevel
    reasons: Tuple[str, ...]


@dataclass(frozen=True)
class CanopenDetection:
    level: EvidenceLevel
    reasons: Tuple[str, ...]
    related_keys: Tuple[str, ...]
    likely_keys: Tuple[str, ...]
    nodes: Tuple[CanopenNodeObservation, ...]


_NODE_RANGES = (
    (0x080, CanopenObjectClass.EMCY),
    (0x180, CanopenObjectClass.TPDO1),
    (0x200, CanopenObjectClass.RPDO1),
    (0x280, CanopenObjectClass.TPDO2),
    (0x300, CanopenObjectClass.RPDO2),
    (0x380, CanopenObjectClass.TPDO3),
    (0x400, CanopenObjectClass.RPDO3),
    (0x480, CanopenObjectClass.TPDO4),
    (0x500, CanopenObjectClass.RPDO4),
    (0x580, CanopenObjectClass.SDO_RESPONSE),
    (0x600, CanopenObjectClass.SDO_REQUEST),
    (0x700, CanopenObjectClass.HEARTBEAT),
)

_PDO_NAMES = frozenset(item.value for item in (
    CanopenObjectClass.TPDO1, CanopenObjectClass.RPDO1,
    CanopenObjectClass.TPDO2, CanopenObjectClass.RPDO2,
    CanopenObjectClass.TPDO3, CanopenObjectClass.RPDO3,
    CanopenObjectClass.TPDO4, CanopenObjectClass.RPDO4,
))

_HEARTBEAT_STATES = frozenset((0x00, 0x04, 0x05, 0x7F))
_SDO_REQUEST_COMMANDS = frozenset((0x20, 0x21, 0x22, 0x23, 0x27, 0x2B, 0x2F, 0x40))
_SDO_RESPONSE_COMMANDS = frozenset((0x41, 0x42, 0x43, 0x47, 0x4B, 0x4F, 0x60, 0x80))
_SDO_PAIR_WINDOW = 5.0


def classify_cob_id(arb_id: int, is_extended: bool = False
                    ) -> Optional[CanopenCobId]:
    """Return the standard CANopen range interpretation, never a verdict."""
    if is_extended or arb_id < 0 or arb_id > 0x7FF:
        return None
    if arb_id == 0x000:
        return CanopenCobId(CanopenObjectClass.NMT, None)
    if arb_id == 0x080:
        return CanopenCobId(CanopenObjectClass.SYNC, None)
    for base, object_class in _NODE_RANGES:
        node = arb_id - base
        if 1 <= node <= 127:
            return CanopenCobId(object_class, node)
    return None


def sdo_signature(frame: CanFrame, request: bool
                  ) -> Optional[Tuple[int, int]]:
    """Return index/sub-index for a structurally useful expedited SDO shape."""
    data = frame.data
    if len(data) != 8:
        return None
    commands = _SDO_REQUEST_COMMANDS if request else _SDO_RESPONSE_COMMANDS
    if data[0] not in commands:
        return None
    index = data[1] | (data[2] << 8)
    if index == 0:
        return None
    return (index, data[3])


class _Node:
    def __init__(self, channel: str, node_id: int):
        self.channel = channel
        self.node_id = node_id
        self.classes: Set[str] = set()
        self.keys: Set[str] = set()
        self.frames = 0
        self.first = 0.0
        self.last = 0.0
        self.heartbeat_total = 0
        self.heartbeat_valid = 0
        self.heartbeat_states: Counter = Counter()
        self.heartbeat_stamps: List[float] = []
        self.sdo_requests: List[Tuple[float, Tuple[int, int]]] = []
        self.sdo_responses: List[Tuple[float, Tuple[int, int]]] = []

    def add(self, frame: CanFrame, cob: CanopenCobId) -> None:
        self.classes.add(cob.object_class.value)
        self.keys.add(frame.key)
        if self.frames == 0:
            self.first = frame.timestamp
        self.last = frame.timestamp
        self.frames += 1
        if cob.object_class is CanopenObjectClass.HEARTBEAT:
            self.heartbeat_total += 1
            if len(frame.data) == 1 and frame.data[0] in _HEARTBEAT_STATES:
                self.heartbeat_valid += 1
                self.heartbeat_states[frame.data[0]] += 1
                self.heartbeat_stamps.append(frame.timestamp)
        elif cob.object_class is CanopenObjectClass.SDO_REQUEST:
            signature = sdo_signature(frame, True)
            if signature is not None:
                self.sdo_requests.append((frame.timestamp, signature))
        elif cob.object_class is CanopenObjectClass.SDO_RESPONSE:
            signature = sdo_signature(frame, False)
            if signature is not None:
                self.sdo_responses.append((frame.timestamp, signature))


def _periods(stamps: List[float]) -> Tuple[Optional[float], Optional[float]]:
    values = [later - earlier for earlier, later in zip(stamps, stamps[1:])
              if math.isfinite(earlier) and math.isfinite(later) and later >= earlier]
    if not values:
        return (None, None)
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return (mean, math.sqrt(max(0.0, variance)))


def _sdo_pairs(node: _Node) -> int:
    used: Set[int] = set()
    pairs = 0
    for request_time, signature in node.sdo_requests:
        for index, (response_time, response_signature) in enumerate(node.sdo_responses):
            if index in used:
                continue
            if (response_signature == signature
                    and request_time <= response_time <= request_time + _SDO_PAIR_WINDOW):
                used.add(index)
                pairs += 1
                break
    return pairs


def _snapshot_node(node: _Node) -> CanopenNodeObservation:
    classes = tuple(sorted(node.classes))
    pdos = set(classes) & _PDO_NAMES
    mean, jitter = _periods(node.heartbeat_stamps)
    heartbeat_repeated = (
        node.heartbeat_valid >= 2
        and node.heartbeat_valid / float(max(1, node.heartbeat_total)) >= 0.8
    )
    heartbeat_regular = (
        node.heartbeat_valid >= 3 and mean is not None and mean > 0
        and jitter is not None and jitter / mean <= 0.25
    )
    pairs = _sdo_pairs(node)
    reasons: List[str] = []
    if heartbeat_repeated:
        reasons.append("{} repeated valid heartbeat/boot-up state frames".format(
            node.heartbeat_valid))
        if heartbeat_regular:
            reasons.append("heartbeat periods are regular (mean {:.3f}s, jitter {:.3f}s)".format(
                mean, jitter))
    elif node.heartbeat_total:
        reasons.append("{} heartbeat-range frame{} without sufficient valid repetition".format(
            node.heartbeat_total, "" if node.heartbeat_total == 1 else "s"))
    if pdos:
        reasons.append("PDO classes correlate to this node: {}".format(
            ", ".join(sorted(pdos))))
    if pairs:
        reasons.append("{} node-matched SDO request/response transaction{}".format(
            pairs, "" if pairs == 1 else "s"))
    elif node.sdo_requests or node.sdo_responses:
        reasons.append("SDO-range traffic observed without a matched request/response")

    if ((heartbeat_regular and pairs and pdos)
            or (heartbeat_regular and len(pdos) >= 3 and node.frames >= 8)
            or (pairs and len(pdos) >= 2 and node.frames >= 6)):
        level = EvidenceLevel.STRONG
    elif pairs or (heartbeat_repeated and bool(pdos)):
        level = EvidenceLevel.POSSIBLE
    else:
        level = EvidenceLevel.WEAK
        if not reasons:
            reasons.append("only CANopen-compatible COB-ID ranges were observed")

    return CanopenNodeObservation(
        channel=node.channel, node_id=node.node_id,
        object_classes=classes, message_keys=tuple(sorted(node.keys)),
        frame_count=node.frames, first_seen=node.first, last_seen=node.last,
        heartbeat_frames=node.heartbeat_total,
        valid_heartbeat_frames=node.heartbeat_valid,
        heartbeat_states=tuple(sorted(node.heartbeat_states.items())),
        heartbeat_average_period=mean, heartbeat_jitter_stddev=jitter,
        sdo_request_frames=len(node.sdo_requests),
        sdo_response_frames=len(node.sdo_responses), sdo_pairs=pairs,
        evidence_level=level, reasons=tuple(reasons),
    )


def detect_canopen(window: StoreWindow,
                   cancelled: Optional[Callable[[], bool]] = None
                   ) -> CanopenDetection:
    nodes: "OrderedDict[Tuple[str, int], _Node]" = OrderedDict()
    global_objects: Counter = Counter()
    global_keys: Set[str] = set()
    for index, frame in enumerate(window):
        if cancelled is not None and index % 1024 == 0 and cancelled():
            from .survey import SurveyCancelled
            raise SurveyCancelled()
        cob = classify_cob_id(frame.arb_id, frame.is_extended)
        if cob is None or frame.is_error_frame or frame.is_remote_frame:
            continue
        if cob.node_id is None:
            global_objects[cob.object_class.value] += 1
            global_keys.add(frame.key)
            continue
        key = (frame.channel, cob.node_id)
        node = nodes.get(key)
        if node is None:
            node = nodes[key] = _Node(*key)
        node.add(frame, cob)

    observations = tuple(sorted(
        (_snapshot_node(node) for node in nodes.values()),
        key=lambda item: (item.channel, item.node_id),
    ))
    strong = [item for item in observations if item.evidence_level is EvidenceLevel.STRONG]
    possible = [item for item in observations if item.evidence_level is EvidenceLevel.POSSIBLE]
    reasons: List[str] = []
    if strong:
        level = EvidenceLevel.STRONG
        reasons.append("{} coherently correlated CANopen node{} observed".format(
            len(strong), "" if len(strong) == 1 else "s"))
    elif len(possible) >= 2:
        level = EvidenceLevel.STRONG
        reasons.append("multiple independently correlated CANopen node candidates observed")
    elif possible:
        level = EvidenceLevel.POSSIBLE
        reasons.append("one node has correlated heartbeat or SDO evidence")
    elif observations or global_objects:
        level = EvidenceLevel.WEAK
        reasons.append("CANopen-compatible COB-ID ranges exist, without node correlation")
    else:
        level = EvidenceLevel.NONE
        reasons.append("no standard CANopen communication-object ranges observed")
    if global_objects:
        reasons.append("global objects observed: {}".format(
            ", ".join("{} ({})".format(name, count)
                      for name, count in sorted(global_objects.items()))))
    for item in (strong or possible)[:3]:
        reasons.append("node {}{}: {}".format(
            item.node_id, " on " + item.channel if item.channel else "",
            "; ".join(item.reasons)))

    related = set(global_keys)
    for item in observations:
        related.update(item.message_keys)
    likely = {key for item in observations
              if item.evidence_level.rank >= EvidenceLevel.POSSIBLE.rank
              for key in item.message_keys}
    return CanopenDetection(level, tuple(reasons), tuple(sorted(related)),
                            tuple(sorted(likely)), observations)


__all__ = [
    "CanopenCobId", "CanopenDetection", "CanopenNodeObservation",
    "CanopenObjectClass", "classify_cob_id", "detect_canopen", "sdo_signature",
]
