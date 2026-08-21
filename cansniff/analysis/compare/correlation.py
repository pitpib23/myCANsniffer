"""Timestamp-aligned Pearson correlation for explicitly selected fields."""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from ..series import Series, block_series
from ..store import StoreWindow
from .model import CandidateEvidence, CorrelationResult


DEFAULT_ALIGNMENT_TOLERANCE = 0.050
MIN_PAIRED_SAMPLES = 5


def numeric_series(window: StoreWindow, candidate: CandidateEvidence) -> Series:
    return block_series(
        window, candidate.message_key, candidate.byte_start,
        candidate.byte_length, candidate.decoder_key,
    )


def align_nearest(left: Series, right: Series, tolerance: float
                  ) -> Tuple[List[float], List[float]]:
    """Greedily pair nearest timestamps without reusing a right sample."""
    tolerance = max(0.0, float(tolerance))
    paired_left: List[float] = []
    paired_right: List[float] = []
    cursor = 0
    last_used = -1
    for left_time, left_value in zip(left.times, left.values):
        if not math.isfinite(left_time) or not math.isfinite(left_value):
            continue
        while cursor < len(right.times) and right.times[cursor] < left_time:
            cursor += 1
        choices = []
        if cursor < len(right.times):
            choices.append(cursor)
        if cursor > 0:
            choices.append(cursor - 1)
        choices = [index for index in choices
                   if index > last_used and index < len(right.values)
                   and math.isfinite(right.times[index])
                   and math.isfinite(right.values[index])]
        if not choices:
            continue
        best = min(choices, key=lambda index: (
            abs(right.times[index] - left_time), index))
        if abs(right.times[best] - left_time) > tolerance:
            continue
        paired_left.append(float(left_value))
        paired_right.append(float(right.values[best]))
        last_used = best
        cursor = best + 1
    return paired_left, paired_right


def pearson_correlation(left: Series, right: Series,
                        tolerance: float = DEFAULT_ALIGNMENT_TOLERANCE,
                        minimum_samples: int = MIN_PAIRED_SAMPLES
                        ) -> CorrelationResult:
    xs, ys = align_nearest(left, right, tolerance)
    count = len(xs)
    reasons = [
        "nearest-timestamp alignment within {:.6f}s; samples are never paired by array index"
        .format(tolerance)
    ]
    coefficient: Optional[float] = None
    if count < max(2, minimum_samples):
        reasons.append("{} paired samples; at least {} are required".format(
            count, max(2, minimum_samples)))
    else:
        mean_x = sum(xs) / count
        mean_y = sum(ys) / count
        dx = [value - mean_x for value in xs]
        dy = [value - mean_y for value in ys]
        denom_x = sum(value * value for value in dx)
        denom_y = sum(value * value for value in dy)
        if denom_x <= 0 or denom_y <= 0:
            reasons.append("Pearson correlation is undefined for a constant series")
        else:
            coefficient = sum(a * b for a, b in zip(dx, dy)) / math.sqrt(
                denom_x * denom_y)
            coefficient = max(-1.0, min(1.0, coefficient))
            magnitude = abs(coefficient)
            strength = ("strong" if magnitude >= 0.8 else
                        "moderate" if magnitude >= 0.5 else "weak")
            direction = "positive" if coefficient >= 0 else "negative"
            reasons.append("{} {} linear correlation over {} paired samples".format(
                strength, direction, count))
            reasons.append("correlation is structural association, not evidence of causation")
    return CorrelationResult(
        left.label, right.label, coefficient, count, tolerance, tuple(reasons),
        left.skipped, right.skipped,
    )


def correlate_candidates(window: StoreWindow, left: CandidateEvidence,
                         right: CandidateEvidence,
                         tolerance: float = DEFAULT_ALIGNMENT_TOLERANCE
                         ) -> CorrelationResult:
    return pearson_correlation(
        numeric_series(window, left), numeric_series(window, right), tolerance)


__all__ = [
    "DEFAULT_ALIGNMENT_TOLERANCE", "MIN_PAIRED_SAMPLES", "align_nearest",
    "correlate_candidates", "numeric_series", "pearson_correlation",
]
