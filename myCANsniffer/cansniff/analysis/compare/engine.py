"""Retained-window comparison, explainable ranking, and revision cache."""

from __future__ import annotations

import math
from collections import Counter, OrderedDict
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ...model import CanFrame
from ..profile import TrafficProfileSnapshot
from ..protocols.model import EvidenceLevel
from ..store import FrameStore, StoreWindow
from .candidates import analyze_candidates
from .model import (
    BitComparison, ByteComparison, ComparisonInput, ComparisonSnapshot,
    ComparisonWindow, MessageComparison, PresenceChange, WindowSummary,
)


MAX_CANDIDATE_MESSAGES = 32
MIN_REPEATABLE_PRESENCE = 3


class ComparisonCancelled(Exception):
    """Raised only at cooperative cancellation checkpoints."""


class ComparisonValidationError(ValueError):
    def __init__(self, reasons: Sequence[str]):
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


class _Facts:
    def __init__(self, frame: CanFrame):
        self.key = frame.key
        self.channel = frame.channel
        self.arb_id = frame.arb_id
        self.is_extended = frame.is_extended
        self.is_fd = frame.is_fd
        self.count = 0
        self.usable = 0
        self.first: Optional[float] = None
        self.last: Optional[float] = None
        self.previous_timestamp: Optional[float] = None
        self.period_count = 0
        self.period_sum = 0.0
        self.out_of_order = 0
        self.lengths: Counter = Counter()
        self.byte_values: List[Counter] = []
        self.byte_transitions: List[int] = []
        self.byte_opportunities: List[int] = []
        self.bit_transitions: List[int] = []
        self.previous_data: Optional[bytes] = None
        self.payload_transitions = 0
        self.payload_changes = 0

    def _ensure(self, size: int) -> None:
        while len(self.byte_values) < size:
            self.byte_values.append(Counter())
            self.byte_transitions.append(0)
            self.byte_opportunities.append(0)
        while len(self.bit_transitions) < size * 8:
            self.bit_transitions.append(0)

    def add(self, frame: CanFrame) -> None:
        self.count += 1
        self.is_fd = self.is_fd or frame.is_fd
        timestamp = frame.timestamp
        if math.isfinite(timestamp):
            self.first = timestamp if self.first is None else min(self.first, timestamp)
            self.last = timestamp if self.last is None else max(self.last, timestamp)
            if self.previous_timestamp is not None:
                delta = timestamp - self.previous_timestamp
                if delta >= 0:
                    self.period_count += 1
                    self.period_sum += delta
                else:
                    self.out_of_order += 1
            self.previous_timestamp = timestamp
        if frame.is_error_frame or frame.is_remote_frame:
            self.previous_data = None
            return
        data = frame.data
        self.usable += 1
        self.lengths[len(data)] += 1
        self._ensure(len(data))
        for index, value in enumerate(data):
            self.byte_values[index][value] += 1
        previous = self.previous_data
        if previous is not None:
            self.payload_transitions += 1
            if previous != data:
                self.payload_changes += 1
            for index in range(min(len(previous), len(data))):
                self.byte_opportunities[index] += 1
                diff = previous[index] ^ data[index]
                if diff:
                    self.byte_transitions[index] += 1
                    base = index * 8
                    for bit in range(8):
                        if diff & (1 << bit):
                            self.bit_transitions[base + bit] += 1
        self.previous_data = data

    @property
    def rate(self) -> float:
        if self.count < 2 or self.first is None or self.last is None or self.last <= self.first:
            return 0.0
        return (self.count - 1) / (self.last - self.first)

    @property
    def average_period(self) -> Optional[float]:
        return (self.period_sum / self.period_count
                if self.period_count else None)

    @property
    def payload_change_fraction(self) -> float:
        return (self.payload_changes / float(self.payload_transitions)
                if self.payload_transitions else 0.0)


def _scan(window: StoreWindow, cancelled: Optional[Callable[[], bool]]) -> Dict[str, _Facts]:
    results: "OrderedDict[str, _Facts]" = OrderedDict()
    for index, frame in enumerate(window):
        if cancelled is not None and index % 1024 == 0 and cancelled():
            raise ComparisonCancelled()
        facts = results.get(frame.key)
        if facts is None:
            facts = results[frame.key] = _Facts(frame)
        facts.add(frame)
    return results


def _mode(counter: Counter) -> Optional[int]:
    if not counter:
        return None
    highest = max(counter.values())
    return min(value for value, count in counter.items() if count == highest)


def _distance(left: Counter, right: Counter) -> float:
    if not left or not right:
        return 0.0
    left_total, right_total = float(sum(left.values())), float(sum(right.values()))
    return 0.5 * sum(abs(left.get(value, 0) / left_total
                         - right.get(value, 0) / right_total)
                     for value in set(left) | set(right))


def _entropy(counter: Counter) -> float:
    if not counter:
        return 0.0
    total = float(sum(counter.values()))
    return -sum((count / total) * math.log(count / total, 2)
                for count in counter.values())


def _byte_change(index: int, baseline: Optional[_Facts], event: Optional[_Facts]
                 ) -> ByteComparison:
    bc = (baseline.byte_values[index]
          if baseline is not None and index < len(baseline.byte_values) else Counter())
    ec = (event.byte_values[index]
          if event is not None and index < len(event.byte_values) else Counter())
    bs, es = sum(bc.values()), sum(ec.values())
    bu = baseline.usable if baseline is not None else 0
    eu = event.usable if event is not None else 0
    bo = (baseline.byte_opportunities[index]
          if baseline is not None and index < len(baseline.byte_opportunities) else 0)
    eo = (event.byte_opportunities[index]
          if event is not None and index < len(event.byte_opportunities) else 0)
    bt = (baseline.byte_transitions[index]
          if baseline is not None and index < len(baseline.byte_transitions) else 0)
    et = (event.byte_transitions[index]
          if event is not None and index < len(event.byte_transitions) else 0)
    return ByteComparison(
        index, bs, es, bu - bs, eu - es,
        min(bc) if bc else None, max(bc) if bc else None,
        min(ec) if ec else None, max(ec) if ec else None,
        len(bc), len(ec), _entropy(bc), _entropy(ec), _mode(bc), _mode(ec),
        _distance(bc, ec), bt / float(bo) if bo else 0.0,
        et / float(eo) if eo else 0.0,
    )


def _bit_changes(byte_changes: Sequence[ByteComparison], baseline: Optional[_Facts],
                 event: Optional[_Facts]) -> Tuple[BitComparison, ...]:
    results = []
    for byte in byte_changes:
        bc = (baseline.byte_values[byte.index]
              if baseline is not None and byte.index < len(baseline.byte_values)
              else Counter())
        ec = (event.byte_values[byte.index]
              if event is not None and byte.index < len(event.byte_values)
              else Counter())
        bs, es = sum(bc.values()), sum(ec.values())
        for bit in range(8):
            base_index = byte.index * 8 + bit
            bt = (baseline.bit_transitions[base_index]
                  if baseline is not None and base_index < len(baseline.bit_transitions)
                  else 0)
            et = (event.bit_transitions[base_index]
                  if event is not None and base_index < len(event.bit_transitions)
                  else 0)
            bp = sum(count for value, count in bc.items() if value & (1 << bit)) / float(bs) if bs else 0.0
            ep = sum(count for value, count in ec.items() if value & (1 << bit)) / float(es) if es else 0.0
            if bt or et or abs(bp - ep) > 0:
                results.append(BitComparison(
                    byte.index, bit, bs, es, bp, ep, bt, et))
    return tuple(results)


def _compare_message(key: str, baseline: Optional[_Facts], event: Optional[_Facts]
                     ) -> MessageComparison:
    template = baseline or event
    assert template is not None
    baseline_count = baseline.count if baseline else 0
    event_count = event.count if event else 0
    if baseline_count and event_count:
        presence = PresenceChange.UNCHANGED
    elif event_count:
        presence = PresenceChange.NEW
    else:
        presence = PresenceChange.REMOVED
    br = baseline.rate if baseline else 0.0
    er = event.rate if event else 0.0
    normalized_rate = abs(er - br) / max(br, er) if max(br, er) > 0 else 0.0
    bp = baseline.average_period if baseline else None
    ep = event.average_period if event else None
    period_delta = ep - bp if ep is not None and bp is not None else None
    max_bytes = max(
        len(baseline.byte_values) if baseline else 0,
        len(event.byte_values) if event else 0,
    )
    bytes_result = tuple(_byte_change(index, baseline, event)
                         for index in range(max_bytes))
    bits_result = _bit_changes(bytes_result, baseline, event)

    score = 0.0
    reasons: List[str] = []
    if presence is not PresenceChange.UNCHANGED:
        count = event_count if presence is PresenceChange.NEW else baseline_count
        repeatable = count >= MIN_REPEATABLE_PRESENCE
        score += 35.0 if repeatable else 12.0
        reasons.append("{} ({} observation{}{})".format(
            presence.value, count, "" if count == 1 else "s",
            "; repeatable" if repeatable else "; sparse"))
    distances = [item.distribution_distance for item in bytes_result]
    if distances:
        maximum = max(distances)
        mean = sum(distances) / len(distances)
        score += maximum * 35.0 + mean * 15.0
        if maximum >= 0.10:
            byte = min(item.index for item in bytes_result
                       if item.distribution_distance == maximum)
            reasons.append("byte {} baseline/event distributions differ by {:.1%}".format(
                byte, maximum))
    bit_difference = max((item.state_difference for item in bits_result), default=0.0)
    score += bit_difference * 20.0
    if bit_difference >= 0.10:
        bit = min((item for item in bits_result
                   if item.state_difference == bit_difference),
                  key=lambda item: (item.byte_index, item.bit_index))
        reasons.append("byte {} bit {} state fraction differs by {:.1%}".format(
            bit.byte_index, bit.bit_index, bit_difference))
    if normalized_rate >= 0.10:
        score += normalized_rate * 15.0
        reasons.append("rate changed from {:.3f} Hz to {:.3f} Hz ({:.1%} normalized)"
                       .format(br, er, normalized_rate))
    if bp is not None and ep is not None and max(bp, ep) > 0:
        normalized_period = min(1.0, abs(ep - bp) / max(bp, ep))
        score += normalized_period * 8.0
        if normalized_period >= 0.10:
            reasons.append("average period changed from {:.6f}s to {:.6f}s".format(bp, ep))
    baseline_dynamic = baseline.payload_change_fraction if baseline else 0.0
    event_dynamic = event.payload_change_fraction if event else 0.0
    dynamic_delta = abs(event_dynamic - baseline_dynamic)
    score += dynamic_delta * 7.0
    if dynamic_delta >= 0.20:
        reasons.append("payload change frequency shifted from {:.1%} to {:.1%}".format(
            baseline_dynamic, event_dynamic))
    baseline_lengths = tuple(sorted(baseline.lengths.items())) if baseline else ()
    event_lengths = tuple(sorted(event.lengths.items())) if event else ()
    if baseline_lengths and event_lengths and {x[0] for x in baseline_lengths} != {x[0] for x in event_lengths}:
        score += 8.0
        reasons.append("observed payload-length sets differ between conditions")
    score = min(100.0, score)
    level = (EvidenceLevel.STRONG if score >= 60 else
             EvidenceLevel.POSSIBLE if score >= 25 else
             EvidenceLevel.WEAK if score > 0 else EvidenceLevel.NONE)
    if not reasons:
        reasons.append("no material baseline/event difference was measured")
    return MessageComparison(
        key, template.channel, template.arb_id, template.is_extended, template.is_fd,
        presence, baseline_count, event_count, br, er, er - br, normalized_rate,
        bp, ep, period_delta, baseline_lengths, event_lengths,
        baseline_dynamic, event_dynamic,
        baseline.first if baseline else None, baseline.last if baseline else None,
        event.first if event else None, event.last if event else None,
        bytes_result, bits_result, level, score, tuple(reasons),
    )


def _integrity_caveats(profile: TrafficProfileSnapshot) -> Tuple[str, ...]:
    integrity = profile.integrity
    caveats = []
    if integrity.ui_dropped:
        caveats.append("{} frames were dropped before application analysis; selected intervals may be incomplete"
                       .format(integrity.ui_dropped))
    if integrity.pause_hidden:
        caveats.append("{} frames were hidden while display was paused".format(
            integrity.pause_hidden))
    if integrity.source_errors:
        caveats.append("{} source/backend errors were observed".format(
            integrity.source_errors))
    if integrity.parse_errors:
        caveats.append("{} capture-file records could not be parsed".format(
            integrity.parse_errors))
    if integrity.driver_overruns is None:
        application_loss = bool(
            integrity.ui_dropped or integrity.pause_hidden
            or integrity.source_errors or integrity.parse_errors)
        caveats.append(
            "Hardware/driver loss visibility unavailable" if application_loss else
            "No application-level loss observed; hardware/driver loss visibility unavailable")
    elif integrity.driver_overruns:
        caveats.append("{} hardware/driver overruns were reported".format(
            integrity.driver_overruns))
    out_of_order = sum(message.out_of_order_timestamps for message in profile.messages)
    if out_of_order:
        caveats.append("{} per-message timestamp deltas were out of order".format(
            out_of_order))
    if profile.invalid_timestamps:
        caveats.append("{} frames had non-finite timestamps".format(
            profile.invalid_timestamps))
    return tuple(caveats)


def _validate(store: FrameStore, ref: ComparisonWindow,
              profile: TrafficProfileSnapshot) -> StoreWindow:
    errors = []
    if not math.isfinite(ref.start_timestamp) or not math.isfinite(ref.end_timestamp):
        errors.append("{} interval uses a non-finite timestamp".format(ref.label))
    elif ref.end_timestamp < ref.start_timestamp:
        errors.append("{} interval end precedes its start".format(ref.label))
    retained_first, retained_last = store.time_span()
    if retained_first is None or retained_last is None:
        errors.append("{} cannot be selected because no retained traffic exists".format(ref.label))
    elif not errors:
        if ref.start_timestamp < retained_first:
            wording = "was evicted" if not profile.retained_horizon.complete else "precedes the observed session"
            errors.append("{} interval is incomplete because its start {}".format(
                ref.label, wording))
        if ref.end_timestamp > retained_last:
            errors.append("{} interval is incomplete because its end has not been retained/observed".format(
                ref.label))
    if errors:
        raise ComparisonValidationError(errors)
    window = store.window(ref.start_timestamp, ref.end_timestamp, label=ref.label)
    if any(not math.isfinite(frame.timestamp) for frame in window):
        raise ComparisonValidationError((
            "{} interval contains frames with non-finite timestamps".format(ref.label),))
    usable = [frame for frame in window
              if not frame.is_error_frame and not frame.is_remote_frame]
    if not usable:
        raise ComparisonValidationError((
            "{} interval contains no usable data frames".format(ref.label),))
    return window


def build_comparison(store: FrameStore, comparison_input: ComparisonInput,
                     profile: TrafficProfileSnapshot,
                     cancelled: Optional[Callable[[], bool]] = None
                     ) -> ComparisonSnapshot:
    source_revision = store.revision
    baseline_window = _validate(store, comparison_input.baseline, profile)
    event_window = _validate(store, comparison_input.event, profile)
    if store.revision != source_revision:
        raise ComparisonValidationError((
            "retained capture changed while comparison windows were extracted; compare again",))
    baseline_facts = _scan(baseline_window, cancelled)
    event_facts = _scan(event_window, cancelled)
    keys = sorted(set(baseline_facts) | set(event_facts))
    comparisons = [_compare_message(
        key, baseline_facts.get(key), event_facts.get(key)) for key in keys]
    comparisons.sort(key=lambda item: (-item.ranking_score, item.key))

    caveats = _integrity_caveats(profile)
    candidate_keys = {item.key for item in comparisons[:MAX_CANDIDATE_MESSAGES]
                      if item.ranking_score > 0}
    baseline_by_key: Dict[str, List[CanFrame]] = {key: [] for key in candidate_keys}
    event_by_key: Dict[str, List[CanFrame]] = {key: [] for key in candidate_keys}
    for index, frame in enumerate(baseline_window):
        if cancelled is not None and index % 1024 == 0 and cancelled():
            raise ComparisonCancelled()
        if frame.key in baseline_by_key:
            baseline_by_key[frame.key].append(frame)
    for index, frame in enumerate(event_window):
        if cancelled is not None and index % 1024 == 0 and cancelled():
            raise ComparisonCancelled()
        if frame.key in event_by_key:
            event_by_key[frame.key].append(frame)
    candidates = []
    known_sequence_loss = bool(
        profile.integrity.ui_dropped or profile.integrity.pause_hidden
        or profile.integrity.source_errors or profile.integrity.parse_errors
        or (profile.integrity.driver_overruns or 0))
    for comparison in comparisons[:MAX_CANDIDATE_MESSAGES]:
        if comparison.key not in candidate_keys:
            continue
        found = analyze_candidates(
            comparison.key, baseline_by_key[comparison.key],
            event_by_key[comparison.key], comparison.bytes)
        found = tuple(replace(
            item, baseline_window=comparison_input.baseline,
            event_window=comparison_input.event, caveats=caveats)
            for item in found)
        if known_sequence_loss:
            found = tuple(
                replace(item, evidence_level=EvidenceLevel.POSSIBLE,
                        reasons=item.reasons + (
                            "strength reduced because known capture loss can disrupt sequence evidence",))
                if item.evidence_level is EvidenceLevel.STRONG
                and item.kind.value in ("counter-like", "checksum/CRC-like") else item
                for item in found)
        candidates.extend(found)

    baseline_summary = WindowSummary(
        comparison_input.baseline, len(baseline_window),
        sum(item.usable for item in baseline_facts.values()),
        tuple(sorted(baseline_facts)), True, caveats,
    )
    event_summary = WindowSummary(
        comparison_input.event, len(event_window),
        sum(item.usable for item in event_facts.values()),
        tuple(sorted(event_facts)), True, caveats,
    )
    return ComparisonSnapshot(
        comparison_input, source_revision, baseline_summary, event_summary,
        tuple(comparisons), tuple(candidates), profile.integrity, caveats,
    )


def _cache_key(store: FrameStore, comparison_input: ComparisonInput,
               profile: TrafficProfileSnapshot) -> tuple:
    return (
        store.revision, comparison_input, profile.integrity,
        profile.retained_horizon.complete, profile.invalid_timestamps,
        MAX_CANDIDATE_MESSAGES,
    )


class ComparisonCache:
    def __init__(self):
        self._key: Optional[tuple] = None
        self._snapshot: Optional[ComparisonSnapshot] = None

    def lookup(self, store: FrameStore, comparison_input: ComparisonInput,
               profile: TrafficProfileSnapshot) -> Optional[ComparisonSnapshot]:
        return self._snapshot if self._key == _cache_key(
            store, comparison_input, profile) else None

    def build(self, store: FrameStore, comparison_input: ComparisonInput,
              profile: TrafficProfileSnapshot,
              cancelled: Optional[Callable[[], bool]] = None
              ) -> ComparisonSnapshot:
        key = _cache_key(store, comparison_input, profile)
        if key != self._key or self._snapshot is None:
            snapshot = build_comparison(store, comparison_input, profile, cancelled)
            if cancelled is not None and cancelled():
                raise ComparisonCancelled()
            self._key, self._snapshot = key, snapshot
        return self._snapshot

    def clear(self) -> None:
        self._key = None
        self._snapshot = None


__all__ = [
    "ComparisonCache", "ComparisonCancelled", "ComparisonValidationError",
    "MAX_CANDIDATE_MESSAGES", "build_comparison",
]
