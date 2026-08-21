"""Explainable structural matching of observed traffic to local profiles.

Matching only suggests.  Nothing in this module mutates a ProfileStore,
associates a definition, opens a source, or decodes a semantic value.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .canopen_definitions import DEFAULT_DEFINITION_CACHE, DefinitionCache
from .definitions import DefinitionSourceKind, ValidationState
from .j1939_definitions import DEFAULT_J1939_DEFINITION_CACHE
from .profile import AnalysisHorizon, CaptureIntegritySnapshot, TrafficProfileSnapshot
from .protocols.model import EvidenceLevel, ProtocolKind, ProtocolSurveySnapshot
from .signals import Profile, ProfileStore


MATCHING_ALGORITHM_VERSION = "1.0"
WEIGHT_CAP_MEDIAN_MULTIPLIER = 4.0


class ProfileMatchLevel(str, Enum):
    NONE = "None"
    WEAK = "Weak"
    POSSIBLE = "Possible"
    STRONG = "Strong"

    @property
    def rank(self) -> int:
        return (ProfileMatchLevel.NONE, ProfileMatchLevel.WEAK,
                ProfileMatchLevel.POSSIBLE, ProfileMatchLevel.STRONG).index(self)


class MatchConflictKind(str, Enum):
    ID_FORMAT = "ID_FORMAT"
    PAYLOAD_LENGTH = "PAYLOAD_LENGTH"
    FRAME_FORMAT = "FRAME_FORMAT"
    TIMING = "TIMING"
    CANOPEN_NODE = "CANOPEN_NODE"
    CONFIGURED_COB_ID = "CONFIGURED_COB_ID"
    PROTOCOL = "PROTOCOL"
    SOURCE_MISSING = "SOURCE_MISSING"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    DEFINITION_INVALID = "DEFINITION_INVALID"
    DEFINITION_WARNING = "DEFINITION_WARNING"


CONFLICT_PENALTIES = {
    MatchConflictKind.ID_FORMAT: 24.0,
    MatchConflictKind.PAYLOAD_LENGTH: 8.0,
    MatchConflictKind.FRAME_FORMAT: 20.0,
    MatchConflictKind.TIMING: 4.0,
    MatchConflictKind.CANOPEN_NODE: 16.0,
    MatchConflictKind.CONFIGURED_COB_ID: 10.0,
    MatchConflictKind.PROTOCOL: 12.0,
    MatchConflictKind.SOURCE_MISSING: 24.0,
    MatchConflictKind.SOURCE_CHANGED: 20.0,
    MatchConflictKind.DEFINITION_INVALID: 20.0,
    MatchConflictKind.DEFINITION_WARNING: 3.0,
}


@dataclass(frozen=True)
class MatchingSettings:
    weight_cap_median_multiplier: float = WEIGHT_CAP_MEDIAN_MULTIPLIER

    @property
    def identity(self) -> str:
        return "weight-cap-median-x{:.6g}".format(
            self.weight_cap_median_multiplier)


@dataclass(frozen=True)
class ObservedMessageFact:
    key: str
    channel: str
    arb_id: int
    is_extended: bool
    count: int
    payload_lengths: Tuple[int, ...]
    common_payload_length: int
    classic_frames: int
    fd_frames: int
    average_period: Optional[float]


@dataclass(frozen=True)
class ObservedCanopenNodeFact:
    channel: str
    node_id: int
    message_keys: Tuple[str, ...]
    evidence_level: EvidenceLevel


@dataclass(frozen=True)
class ObservedMatchFacts:
    identity: str
    capture_identity: str
    session_horizon: AnalysisHorizon
    retained_horizon: AnalysisHorizon
    integrity: CaptureIntegritySnapshot
    messages: Tuple[ObservedMessageFact, ...]
    canopen_nodes: Tuple[ObservedCanopenNodeFact, ...]
    protocol_levels: Tuple[Tuple[str, str], ...]
    caveats: Tuple[str, ...] = ()

    @property
    def total_frames(self) -> int:
        return sum(item.count for item in self.messages)


@dataclass(frozen=True)
class DefinedMessageFact:
    logical_id: str
    arb_id: int
    is_extended: bool
    channel: str
    expected_length: Optional[int]
    is_fd: Optional[bool]
    cycle_time_ms: Optional[int]
    source_kind: str
    source_name: str
    required: bool = False

    @property
    def display_key(self) -> str:
        channel = self.channel or "any channel"
        width = 8 if self.is_extended else 3
        return "{}:0x{:0{w}X}:{}".format(
            channel, self.arb_id, "extended" if self.is_extended else "standard",
            w=width)


@dataclass(frozen=True)
class MatchSourceProvenance:
    kind: str
    display_name: str
    content_hash: str
    state: str
    details: str = ""


@dataclass(frozen=True)
class MatchConflict:
    kind: MatchConflictKind
    message_key: str
    explanation: str
    penalty: float


@dataclass(frozen=True)
class DefinitionAssociationSuggestion:
    definition_hash: str
    definition_name: str
    node_id: int
    channel: str
    matched_pdos: int
    defined_pdos: int
    reasons: Tuple[str, ...]
    conflicts: Tuple[str, ...]


@dataclass(frozen=True)
class MatchCoverage:
    matched_observed_keys: int
    observed_keys: int
    matched_defined_keys: int
    defined_keys: int
    matched_frames: int
    observed_frames: int
    balanced_matched_weight: float
    balanced_observed_weight: float
    structurally_compatible_matches: int

    @property
    def observed_key_coverage(self) -> float:
        return (self.matched_observed_keys / self.observed_keys
                if self.observed_keys else 0.0)

    @property
    def definition_key_coverage(self) -> float:
        return (self.matched_defined_keys / self.defined_keys
                if self.defined_keys else 0.0)

    @property
    def weighted_frame_coverage(self) -> float:
        return (self.matched_frames / self.observed_frames
                if self.observed_frames else 0.0)

    @property
    def balanced_frame_coverage(self) -> float:
        return (self.balanced_matched_weight / self.balanced_observed_weight
                if self.balanced_observed_weight else 0.0)

    @property
    def structural_compatibility(self) -> float:
        return (self.structurally_compatible_matches / self.matched_observed_keys
                if self.matched_observed_keys else 0.0)


@dataclass(frozen=True)
class ProfileMatchCandidate:
    profile_id: str
    display_name: str
    level: ProfileMatchLevel
    rank_value: float
    coverage: MatchCoverage
    provenance: Tuple[MatchSourceProvenance, ...]
    reasons: Tuple[str, ...]
    conflicts: Tuple[MatchConflict, ...]
    matched_messages: Tuple[str, ...]
    unmatched_observed: Tuple[str, ...]
    unmatched_defined: Tuple[str, ...]
    assumptions: Tuple[str, ...]
    association_suggestions: Tuple[DefinitionAssociationSuggestion, ...] = ()


@dataclass(frozen=True)
class ProfileMatchSnapshot:
    capture_identity: str
    observed_facts_identity: str
    candidate_set_identity: str
    algorithm_version: str
    settings_identity: str
    generated_from_revision: int
    session_horizon: AnalysisHorizon
    retained_horizon: AnalysisHorizon
    integrity_context: CaptureIntegritySnapshot
    candidates: Tuple[ProfileMatchCandidate, ...]
    caveats: Tuple[str, ...]

    def candidate_for(self, profile_id: str) -> ProfileMatchCandidate:
        for candidate in self.candidates:
            if candidate.profile_id == profile_id:
                return candidate
        raise KeyError(profile_id)


class ProfileMatchingCancelled(Exception):
    pass


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _external_fingerprint(path: str) -> Tuple[Any, ...]:
    if not path:
        return ("none",)
    absolute = os.path.abspath(path)
    try:
        stat = os.stat(absolute)
    except OSError:
        return ("missing", os.path.basename(absolute))
    return ("available", os.path.basename(absolute), int(stat.st_size),
            int(stat.st_mtime_ns), int(stat.st_ctime_ns))


def candidate_set_identity(store: ProfileStore) -> str:
    """Cheap invalidation identity; content hashes are checked during build."""
    values = []
    for profile in sorted(store.profiles, key=lambda item: item.profile_id):
        values.append({
            "profile": profile.to_dict(),
            "dbc_stat": _external_fingerprint(profile.path),
            "definition_stats": [
                _external_fingerprint(reference.source.location)
                for reference in profile.definitions],
        })
    return _digest(values)


def build_observed_facts(
        traffic: TrafficProfileSnapshot,
        protocols: Optional[ProtocolSurveySnapshot] = None,
        capture_identity: str = "",
) -> ObservedMatchFacts:
    messages = tuple(sorted((ObservedMessageFact(
        item.key, item.channel, item.arb_id, item.is_extended, item.count,
        tuple(length for length, _count in item.payload_lengths),
        item.common_payload_length, item.classic_frames, item.fd_frames,
        item.average_period,
    ) for item in traffic.messages), key=lambda item: item.key))
    nodes = ()
    levels: Tuple[Tuple[str, str], ...] = ()
    if protocols is not None:
        nodes = tuple(sorted((ObservedCanopenNodeFact(
            item.channel, item.node_id, item.message_keys, item.evidence_level)
            for item in protocols.canopen_nodes),
            key=lambda item: (item.channel, item.node_id)))
        levels = tuple(sorted((item.protocol.value, item.level.value)
                              for item in protocols.results))
    identity = _digest({
        "capture_identity": capture_identity,
        "messages": [(item.key, item.count, item.payload_lengths,
                      item.common_payload_length, item.classic_frames,
                      item.fd_frames, item.average_period) for item in messages],
        "session": (traffic.session_horizon.first_timestamp,
                    traffic.session_horizon.last_timestamp,
                    traffic.session_horizon.frame_count,
                    traffic.session_horizon.complete),
        "retained": (traffic.retained_horizon.first_timestamp,
                     traffic.retained_horizon.last_timestamp,
                     traffic.retained_horizon.frame_count,
                     traffic.retained_horizon.complete),
        "integrity": (
            traffic.integrity.status.value, traffic.integrity.source_state.value,
            traffic.integrity.received, traffic.integrity.accepted,
            traffic.integrity.processed, traffic.integrity.retained,
            traffic.integrity.ui_dropped, traffic.integrity.pause_hidden,
            traffic.integrity.source_errors, traffic.integrity.parse_errors,
            traffic.integrity.logger_failures, traffic.integrity.driver_overruns,
            traffic.integrity.reasons),
        "protocol_revision": (protocols.generated_from_revision
                              if protocols is not None else None),
        "protocol_levels": levels,
        "nodes": [(item.channel, item.node_id, item.message_keys,
                   item.evidence_level.value) for item in nodes],
    })
    caveats = list(traffic.integrity.reasons)
    if not traffic.retained_horizon.complete:
        caveats.append(
            "Session-wide ID coverage remains available, but retained payload "
            "validation is incomplete because early frames were evicted.")
    if traffic.integrity.driver_overruns is None:
        caveats.append("Driver-overrun metrics are unavailable; hardware loss is unknown.")
    return ObservedMatchFacts(
        identity, capture_identity, traffic.session_horizon,
        traffic.retained_horizon, traffic.integrity, messages, nodes, levels,
        tuple(caveats))


@dataclass
class _AdaptedProfile:
    messages: List[DefinedMessageFact] = field(default_factory=list)
    provenance: List[MatchSourceProvenance] = field(default_factory=list)
    conflicts: List[MatchConflict] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    suggestions: List[DefinitionAssociationSuggestion] = field(default_factory=list)
    has_canopen: bool = False
    manual_only: bool = False


def _profile_message_facts(profile: Profile, adapted: _AdaptedProfile) -> None:
    source_kinds = set()
    any_id = 0
    for can_id, is_extended, group in profile.message_groups():
        enabled = [item for item in group if item.enabled]
        if can_id is None:
            any_id += len(enabled)
            continue
        if not enabled:
            continue
        by_channel: Dict[str, List[Any]] = {}
        for signal in enabled:
            by_channel.setdefault(signal.channel or "", []).append(signal)
        for channel, channel_signals in sorted(by_channel.items()):
            first = channel_signals[0]
            kinds = sorted({item.source_kind or "MANUAL"
                            for item in channel_signals})
            source_kinds.update(kinds)
            expected = first.message_length
            if expected is None:
                expected = max((item.start + item.length + 7) // 8
                               for item in channel_signals)
            adapted.messages.append(DefinedMessageFact(
                "profile:{}:{}:{}:{}".format(
                    profile.profile_id, can_id, int(is_extended), channel),
                int(can_id), bool(is_extended), channel, int(expected),
                bool(first.message_is_fd), first.message_cycle_time,
                "+".join(kinds), first.message_name or profile.name))
    if any_id:
        adapted.assumptions.append(
            "{} enabled manual signal(s) use any-ID matching and were excluded "
            "from structural profile scoring.".format(any_id))
    adapted.manual_only = bool(source_kinds) and source_kinds <= {"MANUAL"}


def _dbc_provenance(profile: Profile, adapted: _AdaptedProfile) -> None:
    has_dbc = bool(profile.path or any(
        item.source_kind == "DBC" for item in profile.signals))
    has_manual = any((item.source_kind or "MANUAL") != "DBC"
                     for item in profile.signals)
    if has_manual:
        adapted.provenance.append(MatchSourceProvenance(
            "MANUAL", "User-authored profile", "", "AVAILABLE",
            "manual definitions are treated as potentially partial"))
    if not has_dbc:
        if profile.signals and not has_manual:
            adapted.provenance.append(MatchSourceProvenance(
                "MANUAL", "User-authored profile", "", "AVAILABLE",
                "manual definitions are treated as potentially partial"))
        return
    name = os.path.basename(profile.path) if profile.path else profile.name
    if not profile.path:
        adapted.provenance.append(MatchSourceProvenance(
            "DBC", name, profile.source_hash, "SNAPSHOT",
            "no external DBC path is associated"))
        return
    if not os.path.isfile(profile.path):
        adapted.provenance.append(MatchSourceProvenance(
            "DBC", name, profile.source_hash, "MISSING", "source file is missing"))
        adapted.conflicts.append(MatchConflict(
            MatchConflictKind.SOURCE_MISSING, "", "DBC source {} is missing".format(name),
            CONFLICT_PENALTIES[MatchConflictKind.SOURCE_MISSING]))
        return
    try:
        current = _hash_file(profile.path)
    except OSError as exc:
        adapted.provenance.append(MatchSourceProvenance(
            "DBC", name, profile.source_hash, "MISSING", str(exc)))
        adapted.conflicts.append(MatchConflict(
            MatchConflictKind.SOURCE_MISSING, "", "DBC source cannot be verified",
            CONFLICT_PENALTIES[MatchConflictKind.SOURCE_MISSING]))
        return
    state = "AVAILABLE"
    details = "source hash agrees with the imported profile"
    if profile.source_hash and current != profile.source_hash:
        state, details = "CHANGED", "source path now contains different bytes"
        adapted.conflicts.append(MatchConflict(
            MatchConflictKind.SOURCE_CHANGED, "",
            "DBC source {} changed since import".format(name),
            CONFLICT_PENALTIES[MatchConflictKind.SOURCE_CHANGED]))
    elif not profile.source_hash:
        state, details = "CURRENT_UNPINNED", "legacy profile had no saved DBC hash"
        adapted.assumptions.append("DBC provenance is unpinned because this is a legacy profile.")
    if profile.dirty:
        state = "LOCALLY_MODIFIED"
        details = "in-application definitions contain unsaved edits"
    adapted.provenance.append(MatchSourceProvenance(
        "DBC", name, profile.source_hash or current, state, details))


def _mapping_facts(definition, node_id: int, channel: str,
                   reference_hash: str) -> List[DefinedMessageFact]:
    values = []
    for mapping in definition.pdo_mappings:
        cob_id = mapping.cob_id(node_id)
        if cob_id is None:
            continue
        values.append(DefinedMessageFact(
            "definition:{}:{}:{}".format(reference_hash, node_id,
                                         mapping.mapping_index),
            cob_id, False, channel,
            int(math.ceil(mapping.total_bits / 8.0)), False, None,
            definition.source.kind.value, definition.source.display_name))
    return values


def _simple_mapping_score(messages: Sequence[DefinedMessageFact],
                          observed: ObservedMatchFacts) -> Tuple[int, int]:
    observed_by_identity: Dict[Tuple[int, bool], List[ObservedMessageFact]] = {}
    for item in observed.messages:
        observed_by_identity.setdefault((item.arb_id, item.is_extended), []).append(item)
    matched, contradictions = 0, 0
    for defined in messages:
        exact = [item for item in observed_by_identity.get(
                    (defined.arb_id, defined.is_extended), ())
                 if not defined.channel or item.channel == defined.channel]
        if exact:
            matched += 1
            if defined.expected_length is not None and any(
                    defined.expected_length not in item.payload_lengths for item in exact):
                contradictions += 1
    return matched, contradictions


def _standard_pdo_cob_id(direction: str, number: int, node_id: int) -> Optional[int]:
    if not 1 <= number <= 4:
        return None
    base = 0x180 if direction.upper() == "TPDO" else (
        0x200 if direction.upper() == "RPDO" else None)
    return None if base is None else base + (number - 1) * 0x100 + node_id


def _configured_cob_conflicts(definition, node_id: int, channel: str,
                              observed: ObservedMatchFacts) -> List[str]:
    observed_ids = {(item.arb_id, item.channel) for item in observed.messages
                    if not item.is_extended}
    conflicts = []
    for mapping in definition.pdo_mappings:
        configured = mapping.cob_id(node_id)
        standard = _standard_pdo_cob_id(
            mapping.direction, mapping.number, node_id)
        if (configured is not None and standard is not None
                and configured != standard
                and ((standard, channel) in observed_ids
                     or (not channel and any(arb_id == standard
                                             for arb_id, _ in observed_ids)))):
            conflicts.append(
                "{}{} configures COB-ID 0x{:03X}, while observed node traffic "
                "uses standard COB-ID 0x{:03X}".format(
                    mapping.direction, mapping.number, configured, standard))
    return conflicts


def _definition_facts(profile: Profile, observed: ObservedMatchFacts,
                      adapted: _AdaptedProfile,
                      definition_cache: DefinitionCache) -> None:
    associations_by_hash: Dict[str, List[Any]] = {}
    for association in profile.canopen_associations:
        associations_by_hash.setdefault(association.definition_hash, []).append(association)
    for reference in profile.definitions:
        if reference.source.kind is DefinitionSourceKind.J1939:
            _j1939, state, reason = DEFAULT_J1939_DEFINITION_CACHE.resolve(reference)
            adapted.provenance.append(MatchSourceProvenance(
                reference.source.kind.value, reference.source.display_name,
                reference.source.content_hash, state.value,
                reason or "; ".join(reference.warnings)))
            adapted.assumptions.append(
                "J1939 PGN definitions contribute provenance and decoding, but are "
                "excluded from CAN-ID structural coverage because transported PGNs "
                "do not map one-to-one to observed CAN message keys.")
            if state is ValidationState.MISSING:
                adapted.conflicts.append(MatchConflict(
                    MatchConflictKind.SOURCE_MISSING, "",
                    "{} is missing".format(reference.source.display_name),
                    CONFLICT_PENALTIES[MatchConflictKind.SOURCE_MISSING]))
            elif state is ValidationState.CHANGED:
                adapted.conflicts.append(MatchConflict(
                    MatchConflictKind.SOURCE_CHANGED, "",
                    "{} changed since import".format(reference.source.display_name),
                    CONFLICT_PENALTIES[MatchConflictKind.SOURCE_CHANGED]))
            elif state is ValidationState.INVALID:
                adapted.conflicts.append(MatchConflict(
                    MatchConflictKind.DEFINITION_INVALID, "",
                    "{} is not a usable definition".format(
                        reference.source.display_name),
                    CONFLICT_PENALTIES[MatchConflictKind.DEFINITION_INVALID]))
            continue
        definition, state, reason = definition_cache.resolve(reference)
        adapted.provenance.append(MatchSourceProvenance(
            reference.source.kind.value, reference.source.display_name,
            reference.source.content_hash, state.value,
            reason or "; ".join(reference.warnings)))
        adapted.has_canopen = True
        if state is ValidationState.MISSING:
            adapted.conflicts.append(MatchConflict(
                MatchConflictKind.SOURCE_MISSING, "",
                "{} is missing".format(reference.source.display_name),
                CONFLICT_PENALTIES[MatchConflictKind.SOURCE_MISSING]))
            continue
        if state is ValidationState.CHANGED:
            adapted.conflicts.append(MatchConflict(
                MatchConflictKind.SOURCE_CHANGED, "",
                "{} changed since import".format(reference.source.display_name),
                CONFLICT_PENALTIES[MatchConflictKind.SOURCE_CHANGED]))
            continue
        if definition is None or state is ValidationState.INVALID:
            adapted.conflicts.append(MatchConflict(
                MatchConflictKind.DEFINITION_INVALID, "",
                "{} is not a usable definition".format(reference.source.display_name),
                CONFLICT_PENALTIES[MatchConflictKind.DEFINITION_INVALID]))
            continue
        if reference.warnings:
            adapted.conflicts.append(MatchConflict(
                MatchConflictKind.DEFINITION_WARNING, "",
                "{} has {} validation warning(s)".format(
                    reference.source.display_name, len(reference.warnings)),
                CONFLICT_PENALTIES[MatchConflictKind.DEFINITION_WARNING]))
        associations = associations_by_hash.get(reference.identity, [])
        if associations:
            for association in associations:
                adapted.messages.extend(_mapping_facts(
                    definition, association.node_id, association.channel,
                    reference.identity))
            continue
        options = []
        configured = definition.configured_node_id
        nodes = list(observed.canopen_nodes)
        if not nodes and configured is not None:
            nodes = [ObservedCanopenNodeFact("", configured, (), EvidenceLevel.NONE)]
        for node in nodes:
            resolution_node = configured if configured is not None else node.node_id
            facts = _mapping_facts(definition, resolution_node, node.channel,
                                   reference.identity)
            matched, contradictions = _simple_mapping_score(facts, observed)
            node_conflicts = []
            if configured is not None and configured != node.node_id:
                node_conflicts.append(
                    "DCF NodeID {} conflicts with observed Node {}".format(
                        configured, node.node_id))
            cob_conflicts = _configured_cob_conflicts(
                definition, resolution_node, node.channel, observed)
            options.append((matched - contradictions * 2 - len(node_conflicts) * 2,
                            matched, -contradictions, -node.node_id, node.channel,
                            node, facts, tuple(node_conflicts),
                            tuple(cob_conflicts)))
        if not options:
            adapted.assumptions.append(
                "{} was not node-resolved because no observed CANopen node was available."
                .format(reference.source.display_name))
            continue
        (_rank, matched, neg_conflicts, _node_order, _channel_order,
         node, facts, node_conflicts, cob_conflicts) = max(options)
        adapted.messages.extend(facts)
        if node_conflicts:
            adapted.conflicts.append(MatchConflict(
                MatchConflictKind.CANOPEN_NODE, "", node_conflicts[0],
                CONFLICT_PENALTIES[MatchConflictKind.CANOPEN_NODE]))
        for explanation in cob_conflicts:
            adapted.conflicts.append(MatchConflict(
                MatchConflictKind.CONFIGURED_COB_ID, "", explanation,
                CONFLICT_PENALTIES[MatchConflictKind.CONFIGURED_COB_ID]))
        if matched:
            reasons = ("{}/{} configured PDO COB-IDs were observed".format(
                matched, len(facts)),)
            adapted.suggestions.append(DefinitionAssociationSuggestion(
                reference.identity, reference.source.display_name,
                node.node_id, node.channel, matched, len(facts), reasons,
                node_conflicts + cob_conflicts))
            adapted.reasons.append(
                "{} is structurally compatible with observed CANopen Node {}."
                .format(reference.source.display_name, node.node_id))


def _adapt_profile(profile: Profile, observed: ObservedMatchFacts,
                   definition_cache: DefinitionCache) -> _AdaptedProfile:
    adapted = _AdaptedProfile()
    _profile_message_facts(profile, adapted)
    _dbc_provenance(profile, adapted)
    _definition_facts(profile, observed, adapted, definition_cache)
    if not adapted.provenance:
        adapted.provenance.append(MatchSourceProvenance(
            "MANUAL", profile.name, "", "EMPTY", "profile has no definitions"))
    return adapted


def _channels_compatible(observed: ObservedMessageFact,
                         defined: DefinedMessageFact) -> bool:
    return not defined.channel or observed.channel == defined.channel


def _match_profile(profile: Profile, observed: ObservedMatchFacts,
                   adapted: _AdaptedProfile,
                   settings: MatchingSettings) -> ProfileMatchCandidate:
    conflicts = list(adapted.conflicts)
    exact_by_observed: Dict[str, List[DefinedMessageFact]] = {}
    seen_defined = set()
    structurally_compatible = set()
    matched_lines = []
    length_agreements = 0
    defined_by_identity: Dict[Tuple[int, bool], List[DefinedMessageFact]] = {}
    for defined in adapted.messages:
        defined_by_identity.setdefault(
            (defined.arb_id, defined.is_extended), []).append(defined)

    for observed_message in observed.messages:
        exact = [defined for defined in defined_by_identity.get(
                    (observed_message.arb_id, observed_message.is_extended), ())
                 if _channels_compatible(observed_message, defined)]
        if exact:
            exact_by_observed[observed_message.key] = exact
        for defined in exact:
            seen_defined.add(defined.logical_id)
        if exact:
            compatible_one = False
            for defined in exact:
                local_conflict = False
                if (defined.expected_length is not None and
                        defined.expected_length not in observed_message.payload_lengths):
                    local_conflict = True
                    conflicts.append(MatchConflict(
                        MatchConflictKind.PAYLOAD_LENGTH, observed_message.key,
                        "{} observed length(s) {}; {} expects {} bytes".format(
                            observed_message.key,
                            ", ".join(str(value)
                                      for value in observed_message.payload_lengths),
                            defined.source_name, defined.expected_length),
                        CONFLICT_PENALTIES[MatchConflictKind.PAYLOAD_LENGTH]))
                else:
                    length_agreements += 1
                if defined.is_fd is False and observed_message.fd_frames:
                    local_conflict = True
                    conflicts.append(MatchConflict(
                        MatchConflictKind.FRAME_FORMAT, observed_message.key,
                        "{} includes CAN FD frames; {} is Classic-only".format(
                            observed_message.key, defined.source_name),
                        CONFLICT_PENALTIES[MatchConflictKind.FRAME_FORMAT]))
                elif defined.is_fd is True and observed_message.classic_frames:
                    local_conflict = True
                    conflicts.append(MatchConflict(
                        MatchConflictKind.FRAME_FORMAT, observed_message.key,
                        "{} includes Classic frames; {} expects CAN FD".format(
                            observed_message.key, defined.source_name),
                        CONFLICT_PENALTIES[MatchConflictKind.FRAME_FORMAT]))
                if (defined.cycle_time_ms and observed_message.average_period is not None
                        and defined.cycle_time_ms > 0):
                    expected_period = defined.cycle_time_ms / 1000.0
                    ratio = abs(observed_message.average_period - expected_period) / expected_period
                    if ratio > 0.5:
                        conflicts.append(MatchConflict(
                            MatchConflictKind.TIMING, observed_message.key,
                            "{} mean period {:.6g}s differs from defined {:.6g}s".format(
                                observed_message.key, observed_message.average_period,
                                expected_period),
                            CONFLICT_PENALTIES[MatchConflictKind.TIMING]))
                compatible_one = compatible_one or not local_conflict
                matched_lines.append("{} matched {} ({})".format(
                    observed_message.key, defined.source_name, defined.source_kind))
            if compatible_one:
                structurally_compatible.add(observed_message.key)

    # Same numeric identifier/channel with the wrong standard/extended flag is
    # a contradiction, not an absent match.
    for observed_message in observed.messages:
        wrong_format = [defined for defined in defined_by_identity.get(
                            (observed_message.arb_id,
                             not observed_message.is_extended), ())
                        if _channels_compatible(observed_message, defined)]
        for defined in wrong_format:
            conflicts.append(MatchConflict(
                MatchConflictKind.ID_FORMAT, observed_message.key,
                "{} is {}; {} defines the same numeric ID as {}".format(
                    observed_message.key,
                    "extended" if observed_message.is_extended else "standard",
                    defined.source_name,
                    "extended" if defined.is_extended else "standard"),
                CONFLICT_PENALTIES[MatchConflictKind.ID_FORMAT]))

    protocol_levels = dict(observed.protocol_levels)
    if adapted.has_canopen:
        canopen = protocol_levels.get(ProtocolKind.CANOPEN.value, EvidenceLevel.NONE.value)
        if canopen == EvidenceLevel.NONE.value:
            conflicts.append(MatchConflict(
                MatchConflictKind.PROTOCOL, "",
                "profile contains CANopen definitions, but CANopen survey evidence is None",
                CONFLICT_PENALTIES[MatchConflictKind.PROTOCOL]))

    # Duplicate explanations from several signals/mappings do not multiply a
    # penalty. Different explicit sources remain separately inspectable.
    unique_conflicts = {}
    for conflict in conflicts:
        key = (conflict.kind.value, conflict.message_key, conflict.explanation)
        unique_conflicts[key] = conflict
    conflicts = list(unique_conflicts.values())

    matched_keys = set(exact_by_observed)
    observed_frames = sum(item.count for item in observed.messages)
    matched_frames = sum(item.count for item in observed.messages
                         if item.key in matched_keys)
    counts = [item.count for item in observed.messages if item.count > 0]
    median = statistics.median(counts) if counts else 1.0
    cap = max(1.0, median * settings.weight_cap_median_multiplier)
    balanced_total = sum(min(item.count, cap) for item in observed.messages)
    balanced_matched = sum(min(item.count, cap) for item in observed.messages
                           if item.key in matched_keys)
    coverage = MatchCoverage(
        len(matched_keys), len(observed.messages), len(seen_defined),
        len(adapted.messages), matched_frames, observed_frames,
        float(balanced_matched), float(balanced_total),
        len(structurally_compatible))

    definition_weight = 5.0 if adapted.manual_only else 15.0
    rank_value = (
        coverage.observed_key_coverage * 50.0
        + coverage.definition_key_coverage * definition_weight
        + coverage.structural_compatibility * 20.0
        + coverage.balanced_frame_coverage * 10.0)
    if adapted.has_canopen:
        canopen = protocol_levels.get(ProtocolKind.CANOPEN.value, EvidenceLevel.NONE.value)
        if canopen == EvidenceLevel.STRONG.value:
            rank_value += 5.0
        elif canopen == EvidenceLevel.POSSIBLE.value:
            rank_value += 3.0
    rank_value -= sum(conflict.penalty for conflict in conflicts)
    rank_value = max(0.0, min(100.0, rank_value))

    severe = sum(conflict.kind in (
        MatchConflictKind.ID_FORMAT, MatchConflictKind.FRAME_FORMAT,
        MatchConflictKind.CANOPEN_NODE, MatchConflictKind.SOURCE_MISSING,
        MatchConflictKind.SOURCE_CHANGED, MatchConflictKind.DEFINITION_INVALID)
                 for conflict in conflicts)
    if not observed.messages or not adapted.messages or not matched_keys:
        level = ProfileMatchLevel.NONE
    elif (rank_value >= 75.0 and coverage.observed_key_coverage >= 0.65
          and coverage.structural_compatibility >= 0.8 and severe == 0):
        level = ProfileMatchLevel.STRONG
    elif rank_value >= 40.0 and coverage.observed_key_coverage >= 0.25:
        level = ProfileMatchLevel.POSSIBLE
    else:
        level = ProfileMatchLevel.WEAK

    reasons = [
        "{}/{} observed message keys matched ({:.1%} observed ID coverage).".format(
            coverage.matched_observed_keys, coverage.observed_keys,
            coverage.observed_key_coverage),
        "{}/{} defined message keys were observed ({:.1%} definition coverage).".format(
            coverage.matched_defined_keys, coverage.defined_keys,
            coverage.definition_key_coverage),
        "{}/{} observed frames belonged to matched IDs ({:.1%} weighted frame coverage)."
        .format(coverage.matched_frames, coverage.observed_frames,
                coverage.weighted_frame_coverage),
        "Median-capped frame coverage is {:.1%}; each ID contributes at most {:.6g} frames."
        .format(coverage.balanced_frame_coverage, cap),
        "{}/{} matched observed keys had compatible length/frame structure."
        .format(coverage.structurally_compatible_matches,
                coverage.matched_observed_keys),
    ]
    reasons.extend(adapted.reasons)
    if adapted.manual_only:
        adapted.assumptions.append(
            "Manual profiles are treated as potentially partial; unseen definitions "
            "carry less ranking weight.")
    unmatched_observed = tuple(item.key for item in observed.messages
                               if item.key not in matched_keys)
    unmatched_defined = tuple(item.display_key for item in adapted.messages
                              if item.logical_id not in seen_defined)
    return ProfileMatchCandidate(
        profile.profile_id, profile.name, level, rank_value, coverage,
        tuple(adapted.provenance), tuple(reasons),
        tuple(sorted(conflicts,
                     key=lambda item: (item.kind.value, item.message_key,
                                       item.explanation))),
        tuple(sorted(set(matched_lines))), unmatched_observed,
        unmatched_defined, tuple(adapted.assumptions),
        tuple(adapted.suggestions))


class ProfileMatchCache:
    """One-result revision cache; no raw frame scan occurs per candidate."""

    def __init__(self, definition_cache: DefinitionCache = DEFAULT_DEFINITION_CACHE):
        self.definition_cache = definition_cache
        self._key = None
        self._result: Optional[ProfileMatchSnapshot] = None

    def clear(self) -> None:
        self._key = None
        self._result = None

    def lookup(self, observed_identity: str, candidate_identity: str,
               settings: MatchingSettings = MatchingSettings()
               ) -> Optional[ProfileMatchSnapshot]:
        key = (observed_identity, candidate_identity,
               MATCHING_ALGORITHM_VERSION, settings.identity)
        return self._result if key == self._key else None

    def build(self, traffic: TrafficProfileSnapshot, store: ProfileStore,
              protocols: Optional[ProtocolSurveySnapshot] = None,
              capture_identity: str = "",
              settings: MatchingSettings = MatchingSettings(),
              cancelled: Callable[[], bool] = lambda: False,
              ) -> ProfileMatchSnapshot:
        observed = build_observed_facts(traffic, protocols, capture_identity)
        candidate_identity = candidate_set_identity(store)
        cached = self.lookup(observed.identity, candidate_identity, settings)
        if cached is not None:
            return cached
        if cancelled():
            raise ProfileMatchingCancelled()
        candidates = []
        if observed.messages:
            for profile in sorted(store.profiles,
                                  key=lambda item: (item.profile_id, item.name.casefold())):
                if cancelled():
                    raise ProfileMatchingCancelled()
                adapted = _adapt_profile(profile, observed, self.definition_cache)
                candidates.append(_match_profile(profile, observed, adapted, settings))
        candidates.sort(key=lambda item: (
            -item.level.rank, -item.rank_value, item.profile_id,
            item.display_name.casefold()))
        caveats = list(observed.caveats)
        if not observed.messages:
            caveats.append("No matching possible — no usable traffic observed.")
        elif not store.profiles:
            caveats.append("No local profiles are available for matching.")
        result = ProfileMatchSnapshot(
            capture_identity, observed.identity, candidate_identity,
            MATCHING_ALGORITHM_VERSION, settings.identity,
            protocols.generated_from_revision if protocols is not None
            else traffic.retained_horizon.frame_count,
            observed.session_horizon, observed.retained_horizon,
            observed.integrity, tuple(candidates), tuple(caveats))
        self._key = (observed.identity, candidate_identity,
                     MATCHING_ALGORITHM_VERSION, settings.identity)
        self._result = result
        return result


__all__ = [
    "CONFLICT_PENALTIES", "MATCHING_ALGORITHM_VERSION", "DefinedMessageFact",
    "DefinitionAssociationSuggestion", "MatchConflict", "MatchConflictKind",
    "MatchCoverage", "MatchSourceProvenance", "MatchingSettings",
    "ObservedCanopenNodeFact", "ObservedMatchFacts", "ObservedMessageFact",
    "ProfileMatchCache", "ProfileMatchCandidate", "ProfileMatchLevel",
    "ProfileMatchSnapshot", "ProfileMatchingCancelled", "build_observed_facts",
    "candidate_set_identity",
]
