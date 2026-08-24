"""Bounded, conservative structural-candidate generation."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ...interpret import DECODERS
from ...model import CanFrame
from ..protocols.model import EvidenceLevel
from .model import ByteComparison, CandidateEvidence, CandidateKind


MIN_CANDIDATE_SAMPLES = 8
MAX_INTERESTING_BYTES = 8
MAX_NUMERIC_RANGES = 8
MAX_NUMERIC_ALTERNATIVES = 3
MAX_CANDIDATES_PER_MESSAGE = 32

_NUMERIC_DECODERS = {
    1: ("u8", "i8"),
    2: ("u16_le", "u16_be", "i16_le", "i16_be"),
    4: ("u32_le", "u32_be", "i32_le", "i32_be", "f32_le", "f32_be"),
}


def shannon_entropy(values: Sequence[int]) -> float:
    if not values:
        return 0.0
    counts = Counter(values)
    total = float(len(values))
    return -sum((count / total) * math.log(count / total, 2)
                for count in counts.values())


def _payload_frames(frames: Sequence[CanFrame]) -> List[CanFrame]:
    return [frame for frame in frames
            if not frame.is_error_frame and not frame.is_remote_frame]


def _byte_sequences(groups: Sequence[Sequence[CanFrame]], index: int
                    ) -> Tuple[List[List[int]], int]:
    sequences: List[List[int]] = []
    missing = 0
    for frames in groups:
        values = []
        for frame in _payload_frames(frames):
            if index >= len(frame.data):
                missing += 1
            else:
                values.append(frame.data[index])
        sequences.append(values)
    return sequences, missing


def _transitions(sequences: Sequence[Sequence[int]]) -> List[Tuple[int, int]]:
    return [(before, after) for values in sequences
            for before, after in zip(values, values[1:])]


def _counter_candidate(key: str, index: int,
                       groups: Sequence[Sequence[CanFrame]]
                       ) -> Optional[CandidateEvidence]:
    sequences, missing = _byte_sequences(groups, index)
    values = [value for sequence in sequences for value in sequence]
    pairs = _transitions(sequences)
    if len(values) < MIN_CANDIDATE_SAMPLES or len(pairs) < 6:
        return None
    deltas = [((after - before) & 0xFF) for before, after in pairs]
    exact = sum(delta == 1 for delta in deltas) / float(len(deltas))
    small = sum(1 <= delta <= 4 for delta in deltas) / float(len(deltas))
    wraps = sum(before >= 0xFC and after <= 0x03
                for before, after in pairs)
    if exact < 0.60 or small < 0.80:
        return None
    reasons = [
        "+1 increments in {:.1%} of consecutive usable observations".format(exact),
        "small positive steps (+1..+4) in {:.1%} of transitions".format(small),
        "{} usable observations; {} missing because the byte was absent".format(
            len(values), missing),
    ]
    if wraps:
        reasons.append("{} modular 255-to-0 wrap{} observed".format(
            wraps, "" if wraps == 1 else "s"))
    else:
        reasons.append("no modular wrap observed; a smooth ramp remains an alternative")
    if wraps and exact >= 0.90 and len(values) >= 16:
        level = EvidenceLevel.STRONG
    elif wraps and (exact >= 0.60 and small >= 0.90):
        level = EvidenceLevel.POSSIBLE
    else:
        # Without a wrap or repeated cycle, a monotonic analogue ramp fits
        # equally well. Preserve it as a Weak lead, never a classification.
        level = EvidenceLevel.WEAK
    score = min(100.0, exact * 65.0 + small * 20.0 + min(15.0, wraps * 5.0))
    return CandidateEvidence(
        key, CandidateKind.COUNTER_LIKE, level, index, 1, tuple(reasons),
        len(values), missing, score,
    )


def _bitfield_candidate(key: str, index: int,
                        groups: Sequence[Sequence[CanFrame]],
                        counter_bytes: Set[int]) -> Optional[CandidateEvidence]:
    if index in counter_bytes:
        return None
    sequences, missing = _byte_sequences(groups, index)
    values = [value for sequence in sequences for value in sequence]
    pairs = _transitions(sequences)
    if len(values) < 12 or len(set(values)) < 3 or not pairs:
        return None
    masks = [before ^ after for before, after in pairs if before != after]
    if len(masks) < 4:
        return None
    bit_counts = [sum(bool(mask & (1 << bit)) for mask in masks) for bit in range(8)]
    active = [bit for bit, count in enumerate(bit_counts) if count >= 2]
    single = sum(mask & (mask - 1) == 0 for mask in masks) / float(len(masks))
    if not (2 <= len(active) <= 5) or single < 0.55:
        return None
    reasons = [
        "bits {} each transition independently across the observations".format(
            ", ".join(str(bit) for bit in active)),
        "{:.1%} of changing transitions toggle exactly one bit".format(single),
        "{} distinct byte states from {} usable observations".format(
            len(set(values)), len(values)),
    ]
    if missing:
        reasons.append("{} observations omitted because this byte was absent".format(missing))
    level = (EvidenceLevel.STRONG if single >= 0.80 and len(set(values)) >= 4
             else EvidenceLevel.POSSIBLE)
    return CandidateEvidence(
        key, CandidateKind.STATUS_BITFIELD_LIKE, level, index, 1,
        tuple(reasons), len(values), missing,
        min(100.0, 45.0 + single * 40.0 + len(active) * 3.0),
    )


def _simple_correlation(xs: Sequence[int], ys: Sequence[int]) -> float:
    if len(xs) < 3 or len(xs) != len(ys):
        return 0.0
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    dx = [value - mx for value in xs]
    dy = [value - my for value in ys]
    xx = sum(value * value for value in dx)
    yy = sum(value * value for value in dy)
    if xx <= 0 or yy <= 0:
        return 0.0
    return sum(a * b for a, b in zip(dx, dy)) / math.sqrt(xx * yy)


def _checksum_candidate(key: str, index: int,
                        groups: Sequence[Sequence[CanFrame]],
                        counter_bytes: Set[int]) -> Optional[CandidateEvidence]:
    if index in counter_bytes:
        return None
    frames = [frame for group in groups for frame in _payload_frames(group)
              if index < len(frame.data)]
    missing = sum(1 for group in groups for frame in _payload_frames(group)
                  if index >= len(frame.data))
    if len(frames) < 24:
        return None
    max_length = max(len(frame.data) for frame in frames)
    if index != max_length - 1:
        return None
    values = [frame.data[index] for frame in frames]
    entropy = shannon_entropy(values)
    if entropy < 3.5:
        return None

    mappings: Dict[bytes, Set[int]] = defaultdict(set)
    occurrences: Counter = Counter()
    for frame in frames:
        core = frame.data[:index] + frame.data[index + 1:]
        mappings[core].add(frame.data[index])
        occurrences[core] += 1
    repeated = [core for core, count in occurrences.items() if count >= 2]
    if len(repeated) < 4:
        return None
    consistent = sum(len(mappings[core]) == 1 for core in repeated) / float(len(repeated))

    core_changes = checksum_changes = 0
    for group in groups:
        usable = [frame for frame in _payload_frames(group) if index < len(frame.data)]
        for before, after in zip(usable, usable[1:]):
            before_core = before.data[:index] + before.data[index + 1:]
            after_core = after.data[:index] + after.data[index + 1:]
            if before_core != after_core:
                core_changes += 1
                if before.data[index] != after.data[index]:
                    checksum_changes += 1
    coupling = checksum_changes / float(core_changes) if core_changes else 0.0
    if coupling < 0.75 or consistent < 0.90:
        return None

    correlations = []
    for other in range(index):
        pairs = [(frame.data[other], frame.data[index]) for frame in frames
                 if other < len(frame.data)]
        if pairs:
            correlations.append(abs(_simple_correlation(
                [pair[0] for pair in pairs], [pair[1] for pair in pairs])))
    max_correlation = max(correlations, default=0.0)
    if max_correlation >= 0.95:
        return None
    reasons = [
        "final payload byte with Shannon entropy {:.2f} bits".format(entropy),
        "changes in {:.1%} of payload-core changes".format(coupling),
        "repeated payload cores reproduce the same byte in {:.1%} of cases".format(
            consistent),
        "not consistent with the detected +1 counter-like pattern",
    ]
    level = (EvidenceLevel.STRONG
             if len(frames) >= 64 and entropy >= 4.5
             and coupling >= 0.90 and consistent >= 0.98
             else EvidenceLevel.POSSIBLE)
    return CandidateEvidence(
        key, CandidateKind.CHECKSUM_LIKE, level, index, 1, tuple(reasons),
        len(frames), missing,
        min(100.0, entropy * 8.0 + coupling * 25.0 + consistent * 25.0),
    )


def _decode_groups(groups: Sequence[Sequence[CanFrame]], offset: int, length: int,
                   decoder_key: str) -> Tuple[List[List[float]], int, int]:
    decoder = DECODERS[decoder_key]
    decoded: List[List[float]] = []
    missing = invalid = 0
    end = offset + length
    for group in groups:
        values = []
        for frame in _payload_frames(group):
            if len(frame.data) < end:
                missing += 1
                continue
            value = decoder.value(frame.data[offset:end])
            if value is None or not math.isfinite(value):
                invalid += 1
                continue
            values.append(float(value))
        decoded.append(values)
    return decoded, missing, invalid


def _numeric_candidate(key: str, offset: int, length: int, decoder_key: str,
                       groups: Sequence[Sequence[CanFrame]]
                       ) -> Optional[CandidateEvidence]:
    sequences, missing, invalid = _decode_groups(groups, offset, length, decoder_key)
    values = [value for sequence in sequences for value in sequence]
    if len(values) < MIN_CANDIDATE_SAMPLES:
        return None
    low, high = min(values), max(values)
    span = high - low
    pairs = [(before, after) for sequence in sequences
             for before, after in zip(sequence, sequence[1:])]
    steps = [abs(after - before) for before, after in pairs]
    maximum_normalized_step = ((max(steps) / span) if steps and span > 0 else 0.0)
    smooth = (sum(step <= max(1e-12, span * 0.10) for step in steps)
              / float(len(steps)) if steps else 0.0)
    repeated = 1.0 - len(set(values)) / float(len(values))

    is_float = decoder_key.startswith("f")
    if is_float:
        total = len(values) + invalid
        finite_fraction = len(values) / float(total) if total else 0.0
        denormal_fraction = sum(0 < abs(value) < 1e-35 for value in values) / float(len(values))
        if finite_fraction < 0.95 or denormal_fraction > 0.20:
            return None
        if max(abs(low), abs(high)) > 1e15:
            return None

    baseline = sequences[0] if sequences else []
    event = sequences[1] if len(sequences) > 1 else []
    baseline_mean = sum(baseline) / len(baseline) if baseline else None
    event_mean = sum(event) / len(event) if event else None
    separation = 0.0
    if baseline_mean is not None and event_mean is not None and span > 0:
        separation = min(1.0, abs(event_mean - baseline_mean) / span)

    continuity = max(0.0, 1.0 - maximum_normalized_step)
    score = smooth * 45.0 + continuity * 30.0 + separation * 15.0 + repeated * 10.0
    if smooth >= 0.90 and continuity >= 0.75 and len(values) >= 20:
        level = EvidenceLevel.STRONG
    elif smooth >= 0.70 and continuity >= 0.35:
        level = EvidenceLevel.POSSIBLE
    else:
        level = EvidenceLevel.WEAK
    reasons = [
        "{} finite usable values; {} missing and {} invalid decodes".format(
            len(values), missing, invalid),
        "observed range {:.6g} to {:.6g}".format(low, high),
        "{:.1%} of consecutive steps are within 10% of the observed range".format(smooth),
        "largest step is {:.1%} of the observed range".format(
            maximum_normalized_step if span > 0 else 0.0),
    ]
    if baseline_mean is not None and event_mean is not None:
        reasons.append("baseline mean {:.6g}; event mean {:.6g}; normalized separation {:.1%}"
                       .format(baseline_mean, event_mean, separation))
    if decoder_key.startswith("i") and low >= 0:
        reasons.append("observed values do not distinguish signed from unsigned interpretation")
    if is_float:
        reasons.append("IEEE-754 values are finite and not dominated by denormals")
    return CandidateEvidence(
        key, CandidateKind.NUMERIC, level, offset, length, tuple(reasons),
        len(values), missing + invalid, score, decoder_key,
        DECODERS[decoder_key].header, low, high, baseline_mean, event_mean,
    )


def analyze_candidates(key: str, baseline_frames: Sequence[CanFrame],
                       event_frames: Sequence[CanFrame],
                       byte_comparisons: Sequence[ByteComparison]
                       ) -> Tuple[CandidateEvidence, ...]:
    """Return bounded candidates for the most active/different byte regions."""
    groups = (baseline_frames, event_frames)
    ranked_bytes = sorted(
        byte_comparisons,
        key=lambda item: (-max(item.distribution_distance,
                              item.event_change_fraction,
                              item.baseline_change_fraction), item.index),
    )
    interesting = [item.index for item in ranked_bytes[:MAX_INTERESTING_BYTES]
                   if (item.baseline_samples or item.event_samples)]
    if not interesting:
        return ()

    candidates: List[CandidateEvidence] = []
    counter_bytes: Set[int] = set()
    for index in interesting:
        candidate = _counter_candidate(key, index, groups)
        if candidate is not None:
            candidates.append(candidate)
            if candidate.evidence_level.rank >= EvidenceLevel.POSSIBLE.rank:
                counter_bytes.add(index)
    for index in interesting:
        candidate = _bitfield_candidate(key, index, groups, counter_bytes)
        if candidate is not None:
            candidates.append(candidate)

    maximum_length = max((len(frame.data) for group in groups
                          for frame in _payload_frames(group)), default=0)
    if maximum_length and maximum_length - 1 in interesting:
        candidate = _checksum_candidate(
            key, maximum_length - 1, groups, counter_bytes)
        if candidate is not None:
            candidates.append(candidate)

    ranges = []
    for width in (1, 2, 4):
        if width > maximum_length:
            continue
        for offset in range(0, maximum_length - width + 1, width):
            if any(index in interesting for index in range(offset, offset + width)):
                priority = max(
                    (item.distribution_distance for item in byte_comparisons
                     if offset <= item.index < offset + width), default=0.0)
                ranges.append((priority, offset, width))
    ranges.sort(key=lambda item: (-item[0], item[1], item[2]))
    for _priority, offset, width in ranges[:MAX_NUMERIC_RANGES]:
        alternatives = []
        for decoder_key in _NUMERIC_DECODERS[width]:
            candidate = _numeric_candidate(key, offset, width, decoder_key, groups)
            if candidate is not None:
                alternatives.append(candidate)
        alternatives.sort(key=lambda item: (-item.score, item.decoder_key))
        # Two-byte fields have exactly four signedness/endian alternatives;
        # keep all four so a tie never hides one merely by decoder spelling.
        limit = 4 if width == 2 else MAX_NUMERIC_ALTERNATIVES
        candidates.extend(alternatives[:limit])

    candidates.sort(key=lambda item: (
        -item.evidence_level.rank, -item.score, item.byte_start,
        item.byte_length, item.kind.value, item.decoder_key))
    return tuple(candidates[:MAX_CANDIDATES_PER_MESSAGE])


__all__ = [
    "MAX_CANDIDATES_PER_MESSAGE", "MAX_INTERESTING_BYTES",
    "analyze_candidates", "shannon_entropy",
]
