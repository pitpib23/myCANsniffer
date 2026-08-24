"""Passive analysis layers over observed CAN frames.

Everything in this package reads frames that have already been received. No
module here opens a bus, transmits, or holds a driver handle — the only way
frames enter is by being handed in from the capture path.

    raw capture / live observation
              |
        CanFrame (frozen)
              |
          FrameStore            time-ordered index, one pointer per frame
              |                                      |
      +-------+--------+-------- ... ------+    TrafficProfile
      |                |                   |    (session facts)
  byte stats       DBC decode          (later layers)

The raw frame stays the source of truth. Every layer here produces a *separate*
presentation object and never mutates, replaces or hides the frame it came
from.
"""

from __future__ import annotations

from .profile import (
    AnalysisHorizon, CaptureIntegrityAccumulator, CaptureIntegritySnapshot,
    HorizonKind, IntegrityStatus, MessageProfile, SourceState,
    TrafficProfileAccumulator, TrafficProfileSnapshot,
)
from .compare import (
    CandidateEvidence, CandidateKind, ComparisonCache, ComparisonInput,
    ComparisonSnapshot, ComparisonWindow, CorrelationResult, MessageComparison,
    build_comparison,
)
from .protocols import (
    EvidenceLevel, ProtocolEvidence, ProtocolKind, ProtocolSurveyCache,
    ProtocolSurveySnapshot, build_protocol_survey,
)
from .store import FrameStore, StoreWindow
from .stats import ByteStat, RangeState, range_state
from .definitions import (
    CanopenNodeAssociation, DefinitionConflict, DefinitionReference,
    DefinitionSource, DefinitionSourceKind, ValidationState,
)
from .canopen_definitions import (
    CanopenDefinition, CanopenObjectDictionary, DefinitionCache,
    DefinitionParseError, decode_observed_pdos, enrich_sdo_observations,
    parse_definition_file,
)
from .j1939_definitions import (
    DEFAULT_J1939_DEFINITION_CACHE, DecodedJ1939Message, DecodedJ1939Spn,
    J1939DecodeStatus, J1939DefinitionCache, J1939DefinitionError,
    J1939DefinitionSet, decode_j1939_payload, decode_j1939_payloads,
    parse_j1939_definition_file,
)
from .matching import (
    CONFLICT_PENALTIES, MATCHING_ALGORITHM_VERSION, DefinitionAssociationSuggestion,
    MatchConflict, MatchConflictKind, MatchCoverage, MatchingSettings,
    ObservedMatchFacts, ProfileMatchCache, ProfileMatchCandidate,
    ProfileMatchLevel, ProfileMatchSnapshot, build_observed_facts,
    candidate_set_identity,
)
from .diagnostics import (
    CONVERSATION_ALGORITHM_VERSION, ConversationSettings, CorrelationStatus,
    DiagnosticAnalysis, DiagnosticConversation, DiagnosticConversationCache,
    DiagnosticEvent, DiagnosticPeerSummary, DiagnosticTransfer,
    DidObservation, DtcObservation, IsoTpEndpoint, IsoTpPeerPair,
    analyze_diagnostic_transfers,
)

__all__ = [
    "FrameStore",
    "StoreWindow",
    "ByteStat",
    "RangeState",
    "range_state",
    "AnalysisHorizon",
    "CaptureIntegrityAccumulator",
    "CaptureIntegritySnapshot",
    "HorizonKind",
    "IntegrityStatus",
    "MessageProfile",
    "SourceState",
    "TrafficProfileAccumulator",
    "TrafficProfileSnapshot",
    "EvidenceLevel",
    "ProtocolEvidence",
    "ProtocolKind",
    "ProtocolSurveyCache",
    "ProtocolSurveySnapshot",
    "build_protocol_survey",
    "CandidateEvidence",
    "CandidateKind",
    "ComparisonCache",
    "ComparisonInput",
    "ComparisonSnapshot",
    "ComparisonWindow",
    "CorrelationResult",
    "MessageComparison",
    "build_comparison",
    "CanopenDefinition",
    "CanopenNodeAssociation",
    "CanopenObjectDictionary",
    "DefinitionCache",
    "DefinitionConflict",
    "DefinitionParseError",
    "DefinitionReference",
    "DefinitionSource",
    "DefinitionSourceKind",
    "ValidationState",
    "decode_observed_pdos",
    "enrich_sdo_observations",
    "parse_definition_file",
    "DEFAULT_J1939_DEFINITION_CACHE",
    "DecodedJ1939Message",
    "DecodedJ1939Spn",
    "J1939DecodeStatus",
    "J1939DefinitionCache",
    "J1939DefinitionError",
    "J1939DefinitionSet",
    "decode_j1939_payload",
    "decode_j1939_payloads",
    "parse_j1939_definition_file",
    "CONFLICT_PENALTIES",
    "MATCHING_ALGORITHM_VERSION",
    "DefinitionAssociationSuggestion",
    "MatchConflict",
    "MatchConflictKind",
    "MatchCoverage",
    "MatchingSettings",
    "ObservedMatchFacts",
    "ProfileMatchCache",
    "ProfileMatchCandidate",
    "ProfileMatchLevel",
    "ProfileMatchSnapshot",
    "build_observed_facts",
    "candidate_set_identity",
    "CONVERSATION_ALGORITHM_VERSION",
    "ConversationSettings",
    "CorrelationStatus",
    "DiagnosticAnalysis",
    "DiagnosticConversation",
    "DiagnosticConversationCache",
    "DiagnosticEvent",
    "DiagnosticPeerSummary",
    "DiagnosticTransfer",
    "DidObservation",
    "DtcObservation",
    "IsoTpEndpoint",
    "IsoTpPeerPair",
    "analyze_diagnostic_transfers",
]
