"""Protocol-neutral, incremental facts about observed CAN traffic.

The accumulator owns session-wide statistics. It never interprets payloads and
never rescans FrameStore while ingesting. Snapshots also describe the bounded
retained horizon explicitly, so cumulative and retained figures cannot be
mistaken for the same population.
"""

from __future__ import annotations

import math
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple

from ..model import CanFrame
from .store import FrameStore

DEFAULT_UNIQUE_PAYLOAD_LIMIT = 4096


class HorizonKind(str, Enum):
    SESSION = "session"
    RETAINED = "retained"


class IntegrityStatus(str, Enum):
    NOT_STARTED = "not-started"
    NO_APPLICATION_LOSS_OBSERVED = "no-application-loss-observed"
    DEGRADED = "degraded"


class SourceState(str, Enum):
    NOT_STARTED = "not-started"
    ACTIVE = "active"
    STOPPED = "stopped"
    END_OF_SOURCE = "end-of-source"
    ERROR = "error"


@dataclass(frozen=True)
class AnalysisHorizon:
    kind: HorizonKind
    first_timestamp: Optional[float]
    last_timestamp: Optional[float]
    frame_count: int
    complete: bool

    @property
    def duration(self) -> float:
        if self.first_timestamp is None or self.last_timestamp is None:
            return 0.0
        return max(0.0, self.last_timestamp - self.first_timestamp)


@dataclass(frozen=True)
class MessageProfile:
    """Session facts for one repository-standard message key.

    A period is a non-negative timestamp delta between consecutive observations
    of this key. ``jitter_stddev`` is the population standard deviation of all
    valid periods, accumulated with Welford's numerically stable algorithm.
    """
    key: str
    channel: str
    arb_id: int
    is_extended: bool
    horizon_kind: HorizonKind
    count: int
    first_seen: Optional[float]
    last_seen: Optional[float]
    average_rate_hz: float
    payload_lengths: Tuple[Tuple[int, int], ...]
    dlcs: Tuple[Tuple[int, int], ...]
    min_payload_length: int
    max_payload_length: int
    common_payload_length: int
    payload_change_count: int
    length_change_count: int
    changed_byte_mask: Tuple[bool, ...]
    byte_change_counts: Tuple[int, ...]
    unique_payloads: int
    unique_payloads_complete: bool
    classic_frames: int
    fd_frames: int
    brs_frames: int
    esi_true: int
    esi_false: int
    esi_unknown: int
    remote_frames: int
    error_frames: int
    valid_periods: int
    minimum_period: Optional[float]
    average_period: Optional[float]
    maximum_period: Optional[float]
    jitter_stddev: Optional[float]
    equal_timestamp_periods: int
    out_of_order_timestamps: int
    invalid_timestamps: int

    @property
    def duration(self) -> float:
        if self.first_seen is None or self.last_seen is None:
            return 0.0
        return max(0.0, self.last_seen - self.first_seen)


@dataclass(frozen=True)
class CaptureIntegritySnapshot:
    status: IntegrityStatus
    source_state: SourceState
    received: int
    accepted: int
    processed: int
    retained: int
    ui_dropped: int
    pause_hidden: int
    source_errors: int
    parse_errors: Optional[int]
    logger_failures: int
    driver_overruns: Optional[int] = None
    reasons: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def filtered(self) -> int:
        return max(0, self.received - self.accepted)


@dataclass(frozen=True)
class TrafficProfileSnapshot:
    session_horizon: AnalysisHorizon
    retained_horizon: AnalysisHorizon
    integrity: CaptureIntegritySnapshot
    processed_frames: int
    unique_message_keys: int
    retained_message_keys: int
    standard_frames: int
    extended_frames: int
    classic_frames: int
    fd_frames: int
    brs_frames: int
    esi_true: int
    esi_false: int
    esi_unknown: int
    remote_frames: int
    error_frames: int
    channels: Tuple[str, ...]
    invalid_timestamps: int
    average_session_rate_hz: float
    messages: Tuple[MessageProfile, ...]

    def message_for(self, key: str) -> Optional[MessageProfile]:
        for message in self.messages:
            if message.key == key:
                return message
        return None


class _MessageAccumulator:
    def __init__(self, frame: CanFrame, unique_payload_limit: int):
        self.key = frame.key
        self.channel = frame.channel
        self.arb_id = frame.arb_id
        self.is_extended = frame.is_extended
        self.unique_payload_limit = unique_payload_limit
        self.count = 0
        self.first_seen: Optional[float] = None
        self.last_seen: Optional[float] = None
        self.previous_timestamp: Optional[float] = None
        self.invalid_timestamps = 0
        self.out_of_order_timestamps = 0
        self.equal_timestamp_periods = 0
        self.period_count = 0
        self.period_mean = 0.0
        self.period_m2 = 0.0
        self.period_min: Optional[float] = None
        self.period_max: Optional[float] = None
        self.lengths: Counter = Counter()
        self.dlcs: Counter = Counter()
        self.previous_payload: Optional[bytes] = None
        self.payload_change_count = 0
        self.length_change_count = 0
        self.byte_change_counts: List[int] = []
        self.unique_payloads = set()
        self.unique_payloads_complete = True
        self.classic_frames = 0
        self.fd_frames = 0
        self.brs_frames = 0
        self.esi_true = 0
        self.esi_false = 0
        self.esi_unknown = 0
        self.remote_frames = 0
        self.error_frames = 0

    def update(self, frame: CanFrame) -> None:
        self.count += 1
        timestamp = frame.timestamp
        if math.isfinite(timestamp):
            if self.first_seen is None or timestamp < self.first_seen:
                self.first_seen = timestamp
            if self.last_seen is None or timestamp > self.last_seen:
                self.last_seen = timestamp
            if self.previous_timestamp is not None:
                delta = timestamp - self.previous_timestamp
                if delta < 0:
                    self.out_of_order_timestamps += 1
                else:
                    if delta == 0:
                        self.equal_timestamp_periods += 1
                    self.period_count += 1
                    difference = delta - self.period_mean
                    self.period_mean += difference / self.period_count
                    self.period_m2 += difference * (delta - self.period_mean)
                    self.period_min = delta if self.period_min is None else min(
                        self.period_min, delta)
                    self.period_max = delta if self.period_max is None else max(
                        self.period_max, delta)
            # A reset starts a new consecutive baseline. Its negative interval
            # is excluded; it cannot poison the following period.
            self.previous_timestamp = timestamp
        else:
            self.invalid_timestamps += 1

        payload = frame.data
        length = len(payload)
        self.lengths[length] += 1
        self.dlcs[frame.dlc] += 1
        if self.previous_payload is not None and payload != self.previous_payload:
            self.payload_change_count += 1
            if len(payload) != len(self.previous_payload):
                self.length_change_count += 1
            width = max(len(payload), len(self.previous_payload))
            if len(self.byte_change_counts) < width:
                self.byte_change_counts.extend([0] * (width - len(self.byte_change_counts)))
            for index in range(width):
                before = (self.previous_payload[index]
                          if index < len(self.previous_payload) else None)
                after = payload[index] if index < len(payload) else None
                if before != after:
                    self.byte_change_counts[index] += 1
        elif len(self.byte_change_counts) < length:
            self.byte_change_counts.extend([0] * (length - len(self.byte_change_counts)))
        self.previous_payload = payload

        if payload not in self.unique_payloads:
            if len(self.unique_payloads) < self.unique_payload_limit:
                self.unique_payloads.add(payload)
            else:
                self.unique_payloads_complete = False

        if frame.is_fd:
            self.fd_frames += 1
            if frame.is_bitrate_switch:
                self.brs_frames += 1
            if frame.is_error_state_indicator is True:
                self.esi_true += 1
            elif frame.is_error_state_indicator is False:
                self.esi_false += 1
            else:
                self.esi_unknown += 1
        else:
            self.classic_frames += 1
        if frame.is_remote_frame:
            self.remote_frames += 1
        if frame.is_error_frame:
            self.error_frames += 1

    def snapshot(self) -> MessageProfile:
        lengths = tuple(sorted(self.lengths.items()))
        common = min(self.lengths, key=lambda value: (-self.lengths[value], value))
        duration = (max(0.0, self.last_seen - self.first_seen)
                    if self.first_seen is not None and self.last_seen is not None else 0.0)
        valid_timestamps = self.count - self.invalid_timestamps
        rate = ((valid_timestamps - 1) / duration
                if valid_timestamps >= 2 and duration > 0 else 0.0)
        jitter = (math.sqrt(max(0.0, self.period_m2 / self.period_count))
                  if self.period_count else None)
        return MessageProfile(
            key=self.key,
            channel=self.channel,
            arb_id=self.arb_id,
            is_extended=self.is_extended,
            horizon_kind=HorizonKind.SESSION,
            count=self.count,
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            average_rate_hz=rate,
            payload_lengths=lengths,
            dlcs=tuple(sorted(self.dlcs.items())),
            min_payload_length=min(self.lengths),
            max_payload_length=max(self.lengths),
            common_payload_length=common,
            payload_change_count=self.payload_change_count,
            length_change_count=self.length_change_count,
            changed_byte_mask=tuple(value > 0 for value in self.byte_change_counts),
            byte_change_counts=tuple(self.byte_change_counts),
            unique_payloads=len(self.unique_payloads),
            unique_payloads_complete=self.unique_payloads_complete,
            classic_frames=self.classic_frames,
            fd_frames=self.fd_frames,
            brs_frames=self.brs_frames,
            esi_true=self.esi_true,
            esi_false=self.esi_false,
            esi_unknown=self.esi_unknown,
            remote_frames=self.remote_frames,
            error_frames=self.error_frames,
            valid_periods=self.period_count,
            minimum_period=self.period_min,
            average_period=(self.period_mean if self.period_count else None),
            maximum_period=self.period_max,
            jitter_stddev=jitter,
            equal_timestamp_periods=self.equal_timestamp_periods,
            out_of_order_timestamps=self.out_of_order_timestamps,
            invalid_timestamps=self.invalid_timestamps,
        )


class CaptureIntegrityAccumulator:
    """Session totals spanning any number of Stop/Start worker instances."""

    _FIELDS = (
        "received", "accepted", "ui_dropped", "pause_hidden",
        "source_errors", "logger_failures",
    )

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._completed = {name: 0 for name in self._FIELDS}
        self._parse_errors = 0
        self._parse_available = False
        self._driver_overruns = 0
        self._driver_available = False
        self.source_state = SourceState.NOT_STARTED

    def set_source_state(self, state: SourceState) -> None:
        self.source_state = state

    def commit(self, counts: Dict[str, int], parse_errors: Optional[int] = None,
               driver_overruns: Optional[int] = None,
               state: Optional[SourceState] = None) -> None:
        for name in self._FIELDS:
            self._completed[name] += max(0, int(counts.get(name, 0)))
        if parse_errors is not None:
            self._parse_errors += max(0, int(parse_errors))
            self._parse_available = True
        if driver_overruns is not None:
            self._driver_overruns += max(0, int(driver_overruns))
            self._driver_available = True
        if state is not None:
            self.source_state = state

    def snapshot(self, processed: int, retained: int,
                 current: Optional[Dict[str, int]] = None,
                 current_parse_errors: Optional[int] = None,
                 driver_overruns: Optional[int] = None) -> CaptureIntegritySnapshot:
        totals = dict(self._completed)
        for name in self._FIELDS:
            totals[name] += max(0, int((current or {}).get(name, 0)))
        parse_available = self._parse_available or current_parse_errors is not None
        parse_errors = self._parse_errors
        if current_parse_errors is not None:
            parse_errors += max(0, int(current_parse_errors))
        driver_available = self._driver_available or driver_overruns is not None
        driver_total = self._driver_overruns
        if driver_overruns is not None:
            driver_total += max(0, int(driver_overruns))

        reasons: List[str] = []
        degraded = False
        for name, label in (
            ("ui_dropped", "UI-delivery frames dropped"),
            ("pause_hidden", "frames hidden while display was paused"),
            ("source_errors", "source errors"),
            ("logger_failures", "logger failures"),
        ):
            if totals[name]:
                degraded = True
                reasons.append("{:,} {}".format(totals[name], label))
        if parse_available and parse_errors:
            degraded = True
            reasons.append("{:,} file records could not be parsed".format(parse_errors))
        if self.source_state == SourceState.ERROR and not totals["source_errors"]:
            degraded = True
            reasons.append("source ended in an error state")
        if self.source_state == SourceState.NOT_STARTED:
            status = IntegrityStatus.NOT_STARTED
            reasons.append("No source has been opened in this session")
        elif degraded:
            status = IntegrityStatus.DEGRADED
        else:
            status = IntegrityStatus.NO_APPLICATION_LOSS_OBSERVED
            reasons.append("No application-level loss was observed")
        if not driver_available:
            reasons.append("Hardware/driver overrun visibility is unavailable")
        elif driver_total:
            degraded = True
            reasons.append("{:,} hardware/driver overruns reported".format(driver_total))
            status = IntegrityStatus.DEGRADED

        return CaptureIntegritySnapshot(
            status=status,
            source_state=self.source_state,
            received=totals["received"],
            accepted=totals["accepted"],
            processed=max(0, int(processed)),
            retained=max(0, int(retained)),
            ui_dropped=totals["ui_dropped"],
            pause_hidden=totals["pause_hidden"],
            source_errors=totals["source_errors"],
            parse_errors=parse_errors if parse_available else None,
            logger_failures=totals["logger_failures"],
            driver_overruns=driver_total if driver_available else None,
            reasons=tuple(reasons),
        )


class TrafficProfileAccumulator:
    """O(frames * payload-width) incremental session profiler."""

    def __init__(self, unique_payload_limit: int = DEFAULT_UNIQUE_PAYLOAD_LIMIT):
        self.unique_payload_limit = max(1, int(unique_payload_limit))
        self.reset()

    def reset(self) -> None:
        self.processed_frames = 0
        self.standard_frames = 0
        self.extended_frames = 0
        self.classic_frames = 0
        self.fd_frames = 0
        self.brs_frames = 0
        self.esi_true = 0
        self.esi_false = 0
        self.esi_unknown = 0
        self.remote_frames = 0
        self.error_frames = 0
        self.invalid_timestamps = 0
        self.first_timestamp: Optional[float] = None
        self.last_timestamp: Optional[float] = None
        self.channels = set()
        self._messages: "OrderedDict[str, _MessageAccumulator]" = OrderedDict()

    def update(self, frames: Iterable[CanFrame]) -> None:
        for frame in frames:
            self.processed_frames += 1
            if frame.channel:
                self.channels.add(frame.channel)
            if math.isfinite(frame.timestamp):
                if self.first_timestamp is None or frame.timestamp < self.first_timestamp:
                    self.first_timestamp = frame.timestamp
                if self.last_timestamp is None or frame.timestamp > self.last_timestamp:
                    self.last_timestamp = frame.timestamp
            else:
                self.invalid_timestamps += 1

            if frame.is_error_frame:
                self.error_frames += 1
            if frame.is_extended:
                self.extended_frames += 1
            else:
                self.standard_frames += 1
            if frame.is_fd:
                self.fd_frames += 1
                if frame.is_bitrate_switch:
                    self.brs_frames += 1
                if frame.is_error_state_indicator is True:
                    self.esi_true += 1
                elif frame.is_error_state_indicator is False:
                    self.esi_false += 1
                else:
                    self.esi_unknown += 1
            else:
                self.classic_frames += 1
            if frame.is_remote_frame:
                self.remote_frames += 1

            message = self._messages.get(frame.key)
            if message is None:
                message = self._messages[frame.key] = _MessageAccumulator(
                    frame, self.unique_payload_limit)
            message.update(frame)

    def snapshot(self, retained_store: Optional[FrameStore] = None,
                 integrity: Optional[CaptureIntegritySnapshot] = None
                 ) -> TrafficProfileSnapshot:
        retained = len(retained_store) if retained_store is not None else self.processed_frames
        retained_first = retained_last = None
        retained_keys = len(self._messages)
        if retained_store is not None:
            retained_first, retained_last = retained_store.time_span()
            retained_keys = len(retained_store.keys())
        session = AnalysisHorizon(
            HorizonKind.SESSION, self.first_timestamp, self.last_timestamp,
            self.processed_frames, True)
        retained_horizon = AnalysisHorizon(
            HorizonKind.RETAINED, retained_first, retained_last, retained,
            retained == self.processed_frames)
        if integrity is None:
            integrity = CaptureIntegrityAccumulator().snapshot(
                self.processed_frames, retained)
        duration = session.duration
        valid_timestamps = self.processed_frames - self.invalid_timestamps
        rate = ((valid_timestamps - 1) / duration
                if valid_timestamps >= 2 and duration > 0 else 0.0)
        messages = tuple(item.snapshot() for item in self._messages.values())
        return TrafficProfileSnapshot(
            session_horizon=session,
            retained_horizon=retained_horizon,
            integrity=integrity,
            processed_frames=self.processed_frames,
            unique_message_keys=len(messages),
            retained_message_keys=retained_keys,
            standard_frames=self.standard_frames,
            extended_frames=self.extended_frames,
            classic_frames=self.classic_frames,
            fd_frames=self.fd_frames,
            brs_frames=self.brs_frames,
            esi_true=self.esi_true,
            esi_false=self.esi_false,
            esi_unknown=self.esi_unknown,
            remote_frames=self.remote_frames,
            error_frames=self.error_frames,
            channels=tuple(sorted(self.channels)),
            invalid_timestamps=self.invalid_timestamps,
            average_session_rate_hz=rate,
            messages=messages,
        )


__all__ = [
    "AnalysisHorizon", "CaptureIntegrityAccumulator", "CaptureIntegritySnapshot",
    "HorizonKind", "IntegrityStatus", "MessageProfile", "SourceState",
    "TrafficProfileAccumulator", "TrafficProfileSnapshot",
]
