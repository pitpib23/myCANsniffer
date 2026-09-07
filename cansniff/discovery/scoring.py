"""Numerical scoring for a single Classic CAN bitrate candidate's passive
observation.

Qt-free and independently testable, exactly like ``cansniff.discovery.
bitrate``. This module never opens a bus, never touches ``SocketCanLink``,
and never decides which bitrate to use -- it only turns a list of received
frames (already collected by ``cansniff.discovery.scan.scan_bitrate_
candidates``) into a transparent 0-100 score with every contributing metric
exposed, so an operator can compare candidates and choose one themselves.

This is deliberately a *different* algorithm from ``cansniff.discovery.
bitrate``'s ``STABLE``/``WEAK``/... classification, which stays untouched
and keeps auto-selecting a single winner when its own conservative criteria
are met. Nothing here selects anything; see ``ScoreComponents``/
``score_candidate`` below.

Scoring model (weights sum to 100, ``total_score`` always clamped to
[0, 100]):

  =========================================  ======
  component                                   points
  =========================================  ======
  persistent ID ratio                             30
  time-bucket ID stability (Jaccard)              15
  singleton ratio (5) + new-ID churn (5)          10
  error-frame rate                                15
  remote-frame rate                               10
  frame-format rate (extended vs. standard)       10
  structural validity (classic data frames)        5
  payload-to-ID consistency                        5
  =========================================  ======

A candidate that received literally nothing (``total_records == 0``) always
scores exactly 0 across every component -- the formulas below would
otherwise reward silence (0% errors, 0% remote, ...), which is not evidence
of a correct bitrate.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from ..model import CanFrame

#: (relative_timestamp_seconds, frame) -- exactly the shape
#: cansniff.discovery.scan collects during one candidate's observation.
Record = Tuple[float, CanFrame]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _clamp_score(value: float, weight: float) -> float:
    return _clamp01(value) * weight


@dataclass(frozen=True)
class ScoringConfig:
    """Every threshold this module uses, configurable in one place.

    Constructed with sensible, documented defaults; ``from_mapping`` reads
    the same keys ``cansniff.config``'s ``discovery`` section stores, the
    same pattern ``DiscoveryThresholds.from_mapping`` already uses for the
    unrelated legacy engine.
    """

    bucket_count: int = 10
    error_rate_threshold: float = 0.05
    remote_rate_threshold: float = 0.20
    format_rate_threshold: float = 0.20
    #: "standard" or "extended" -- which frame format this bus is expected
    #: to use. The format score penalizes the *other* one, never assumes
    #: every CAN network is standard-only.
    expected_format: str = "standard"
    #: Optional, protocol-specific heuristic (payload byte 0 == arbitration
    #: ID low byte). Computed only when explicitly enabled, and never folded
    #: into total_score -- see the module docstring and ScoreComponents.
    enable_protocol_id_low_byte_heuristic: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "bucket_count", max(2, int(self.bucket_count)))
        object.__setattr__(self, "error_rate_threshold",
                           max(1e-6, float(self.error_rate_threshold)))
        object.__setattr__(self, "remote_rate_threshold",
                           max(1e-6, float(self.remote_rate_threshold)))
        object.__setattr__(self, "format_rate_threshold",
                           max(1e-6, float(self.format_rate_threshold)))
        fmt = str(self.expected_format).strip().lower()
        object.__setattr__(self, "expected_format",
                           fmt if fmt in ("standard", "extended") else "standard")
        object.__setattr__(self, "enable_protocol_id_low_byte_heuristic",
                           bool(self.enable_protocol_id_low_byte_heuristic))

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "ScoringConfig":
        defaults = cls()
        return cls(
            bucket_count=int(values.get("scan_bucket_count", defaults.bucket_count)),
            error_rate_threshold=float(values.get(
                "scan_error_rate_threshold", defaults.error_rate_threshold)),
            remote_rate_threshold=float(values.get(
                "scan_remote_rate_threshold", defaults.remote_rate_threshold)),
            format_rate_threshold=float(values.get(
                "scan_format_rate_threshold", defaults.format_rate_threshold)),
            expected_format=str(values.get(
                "scan_expected_format", defaults.expected_format)),
            enable_protocol_id_low_byte_heuristic=bool(values.get(
                "scan_protocol_id_low_byte_heuristic",
                defaults.enable_protocol_id_low_byte_heuristic)),
        )


@dataclass(frozen=True)
class ScoreComponents:
    """Every weighted score, every raw ratio it came from, and every raw
    count -- nothing is hidden behind a single unexplained number. See the
    module docstring for the weights."""

    # -- weighted points (sum to total_score, each clamped to its weight) --
    persistent_id_score: float
    bucket_stability_score: float
    singleton_score: float
    churn_score: float
    error_score: float
    remote_score: float
    format_score: float
    structural_score: float
    payload_migration_score: float
    total_score: float

    # -- raw ratios (0.0-1.0) the scores above were computed from --
    persistent_id_ratio: float
    singleton_id_ratio: float
    bucket_id_stability: float
    new_id_ratio: float
    error_rate: float
    remote_rate: float
    unexpected_format_rate: float
    structural_validity: float
    multi_id_payload_ratio: float
    #: None unless ScoringConfig.enable_protocol_id_low_byte_heuristic was
    #: set -- an informational-only ratio, never part of total_score.
    protocol_id_low_byte_ratio: Optional[float]

    # -- raw counts, shown alongside the ratios above --
    total_records: int
    error_frames: int
    remote_frames: int
    extended_frames: int
    fd_frames: int
    data_frames: int
    structurally_valid_data_frames: int
    unique_ids: int
    persistent_ids: int
    singleton_ids: int


def _zero_components() -> ScoreComponents:
    return ScoreComponents(
        persistent_id_score=0.0, bucket_stability_score=0.0, singleton_score=0.0,
        churn_score=0.0, error_score=0.0, remote_score=0.0, format_score=0.0,
        structural_score=0.0, payload_migration_score=0.0, total_score=0.0,
        persistent_id_ratio=0.0, singleton_id_ratio=0.0, bucket_id_stability=0.0,
        new_id_ratio=0.0, error_rate=0.0, remote_rate=0.0, unexpected_format_rate=0.0,
        structural_validity=0.0, multi_id_payload_ratio=0.0,
        protocol_id_low_byte_ratio=None,
        total_records=0, error_frames=0, remote_frames=0, extended_frames=0,
        fd_frames=0, data_frames=0, structurally_valid_data_frames=0, unique_ids=0,
        persistent_ids=0, singleton_ids=0,
    )


def _jaccard(a: FrozenSet, b: FrozenSet) -> Optional[float]:
    """None when both sets are empty -- see score_candidate's use: an
    adjacent pair where nothing was heard on either side is excluded from
    the stability average rather than counted as "perfectly stable"."""
    union = a | b
    if not union:
        return None
    return len(a & b) / len(union)


def _bucket_index(timestamp: float, duration: float, bucket_count: int) -> int:
    if duration <= 0:
        return 0
    index = int((timestamp / duration) * bucket_count)
    return max(0, min(bucket_count - 1, index))


def score_candidate(
    records: Sequence[Record],
    duration: float,
    config: ScoringConfig = ScoringConfig(),
) -> ScoreComponents:
    """Score one candidate's full (post-settle) observation window.

    ``records`` is every frame received during the observation window, in
    the exact order received, each paired with its time since the window
    started -- never deduplicated (repeated messages are evidence, see the
    module docstring of ``cansniff.discovery.scan``); any duplicate
    suppression belongs to a display layer, not here.
    """
    total_records = len(records)
    if total_records == 0:
        return _zero_components()

    bucket_count = config.bucket_count
    duration = max(1e-6, float(duration))

    error_frames = sum(1 for _t, frame in records if frame.is_error_frame)
    non_error = [(t, frame) for t, frame in records if not frame.is_error_frame]
    all_received_frames = len(non_error)

    fd_frames = sum(1 for _t, frame in non_error if frame.is_fd)
    remote_frames = sum(1 for _t, frame in non_error if frame.is_remote_frame)
    extended_frames = sum(1 for _t, frame in non_error if frame.is_extended)

    data_records = [
        (t, frame) for t, frame in non_error
        if not frame.is_remote_frame and not frame.is_fd
    ]
    data_frames = len(data_records)
    structurally_valid = sum(
        1 for _t, frame in data_records
        if 0 <= frame.dlc <= 8 and len(frame.data) == frame.dlc
    )

    # -- per-ID / per-time-bucket tracking. Error frames carry no real
    # identifier (see cansniff/sources/asc_reader.py and the live source);
    # CAN FD frames are excluded too -- this scan is explicitly Classic CAN
    # only, and folding FD identifiers into the same population would let
    # decoder-format traffic influence a Classic-only decision.
    id_records = [(t, frame) for t, frame in non_error if not frame.is_fd]
    buckets: List[Set[Tuple[int, bool]]] = [set() for _ in range(bucket_count)]
    id_counts: Counter = Counter()
    id_first_bucket: Dict[Tuple[int, bool], int] = {}
    for t, frame in id_records:
        key = (frame.arb_id, frame.is_extended)
        idx = _bucket_index(t, duration, bucket_count)
        buckets[idx].add(key)
        id_counts[key] += 1
        if key not in id_first_bucket:
            id_first_bucket[key] = idx

    unique_ids = len(id_counts)
    if unique_ids:
        required_buckets = bucket_count / 2.0
        persistent_ids = sum(
            1 for key in id_counts
            if sum(1 for bucket in buckets if key in bucket) >= required_buckets
        )
        persistent_id_ratio = persistent_ids / unique_ids
        singleton_ids = sum(1 for count in id_counts.values() if count == 1)
        singleton_id_ratio = singleton_ids / unique_ids
        # "New" = first seen only in the second half of the observation
        # window -- a bus whose identifier population keeps growing well
        # after the scan started looks unstable/random, exactly the
        # wrong-bitrate symptom the brief describes.
        half = bucket_count // 2
        new_ids = sum(1 for bucket_idx in id_first_bucket.values() if bucket_idx >= half)
        new_id_ratio = new_ids / unique_ids
    else:
        persistent_ids = 0
        persistent_id_ratio = 0.0
        singleton_ids = 0
        singleton_id_ratio = 0.0
        new_id_ratio = 0.0

    # bucket_id_stability: mean Jaccard similarity of adjacent buckets'
    # ID sets, skipping pairs where both sides are empty (no evidence
    # either way -- see _jaccard).
    pair_scores = []
    for i in range(bucket_count - 1):
        similarity = _jaccard(buckets[i], buckets[i + 1])
        if similarity is not None:
            pair_scores.append(similarity)
    bucket_id_stability = (sum(pair_scores) / len(pair_scores)) if pair_scores else 0.0

    # -- payload-to-ID consistency: what fraction of distinct payload
    # values were seen under more than one arbitration ID.
    payload_ids: Dict[bytes, Set[Tuple[int, bool]]] = {}
    for _t, frame in data_records:
        payload_ids.setdefault(bytes(frame.data), set()).add(
            (frame.arb_id, frame.is_extended))
    distinct_payloads = len(payload_ids)
    migrated_payloads = sum(1 for ids in payload_ids.values() if len(ids) >= 2)
    multi_id_payload_ratio = (
        migrated_payloads / distinct_payloads) if distinct_payloads else 0.0

    # -- rates --
    error_rate = error_frames / max(1, total_records)
    remote_rate = remote_frames / max(1, all_received_frames)
    if config.expected_format == "extended":
        unexpected_format_frames = all_received_frames - extended_frames
    else:
        unexpected_format_frames = extended_frames
    unexpected_format_rate = unexpected_format_frames / max(1, all_received_frames)
    structural_validity = structurally_valid / max(1, data_frames)

    protocol_ratio: Optional[float] = None
    if config.enable_protocol_id_low_byte_heuristic and data_frames:
        matches = sum(
            1 for _t, frame in data_records
            if frame.data and frame.data[0] == (frame.arb_id & 0xFF)
        )
        protocol_ratio = matches / data_frames

    persistent_id_score = _clamp_score(persistent_id_ratio, 30.0)
    bucket_stability_score = _clamp_score(bucket_id_stability, 15.0)
    singleton_score = _clamp_score(1.0 - _clamp01(singleton_id_ratio), 5.0)
    stable_new_id_score = 1.0 - _clamp01(new_id_ratio)
    churn_score = stable_new_id_score * 5.0
    error_score = _clamp_score(
        1.0 - (error_rate / config.error_rate_threshold), 15.0)
    remote_score = _clamp_score(
        1.0 - (remote_rate / config.remote_rate_threshold), 10.0)
    format_score = _clamp_score(
        1.0 - (unexpected_format_rate / config.format_rate_threshold), 10.0)
    structural_score = _clamp_score(structural_validity, 5.0)
    payload_migration_score = _clamp_score(1.0 - _clamp01(multi_id_payload_ratio), 5.0)

    total = (
        persistent_id_score + bucket_stability_score + singleton_score + churn_score
        + error_score + remote_score + format_score + structural_score
        + payload_migration_score
    )
    total_score = max(0.0, min(100.0, total))

    return ScoreComponents(
        persistent_id_score=persistent_id_score,
        bucket_stability_score=bucket_stability_score,
        singleton_score=singleton_score,
        churn_score=churn_score,
        error_score=error_score,
        remote_score=remote_score,
        format_score=format_score,
        structural_score=structural_score,
        payload_migration_score=payload_migration_score,
        total_score=total_score,
        persistent_id_ratio=persistent_id_ratio,
        singleton_id_ratio=singleton_id_ratio,
        bucket_id_stability=bucket_id_stability,
        new_id_ratio=new_id_ratio,
        error_rate=error_rate,
        remote_rate=remote_rate,
        unexpected_format_rate=unexpected_format_rate,
        structural_validity=structural_validity,
        multi_id_payload_ratio=multi_id_payload_ratio,
        protocol_id_low_byte_ratio=protocol_ratio,
        total_records=total_records,
        error_frames=error_frames,
        remote_frames=remote_frames,
        extended_frames=extended_frames,
        fd_frames=fd_frames,
        data_frames=data_frames,
        structurally_valid_data_frames=structurally_valid,
        unique_ids=unique_ids,
        persistent_ids=persistent_ids,
        singleton_ids=singleton_ids,
    )


__all__ = ["Record", "ScoreComponents", "ScoringConfig", "score_candidate"]
