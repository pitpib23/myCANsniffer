"""Protocol-neutral TrafficProfile and capture-integrity facts."""

from __future__ import annotations

import math
import os
import sys
import unittest
from dataclasses import FrozenInstanceError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis.profile import (  # noqa: E402
    CaptureIntegrityAccumulator, HorizonKind, IntegrityStatus, SourceState,
    TrafficProfileAccumulator,
)
from cansniff.analysis.store import FrameStore  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402


def frame(timestamp=0.0, arb_id=0x123, data=b"\x00", channel="can0", **kwargs):
    return CanFrame(timestamp=timestamp, arb_id=arb_id, data=data,
                    dlc=kwargs.pop("dlc", len(data)), channel=channel, **kwargs)


class TrafficProfileTests(unittest.TestCase):
    def snapshot(self, frames, store=None, limit=4096):
        profile = TrafficProfileAccumulator(limit)
        profile.update(frames)
        if store is not None:
            store.add(frames)
        return profile, profile.snapshot(store)

    def test_empty_profile_has_explicit_session_and_retained_horizons(self):
        store = FrameStore(100)
        _profile, result = self.snapshot([], store)
        self.assertEqual(result.processed_frames, 0)
        self.assertEqual(result.unique_message_keys, 0)
        self.assertEqual(result.session_horizon.kind, HorizonKind.SESSION)
        self.assertEqual(result.retained_horizon.kind, HorizonKind.RETAINED)
        self.assertTrue(result.retained_horizon.complete)
        self.assertIsNone(result.session_horizon.first_timestamp)
        self.assertEqual(result.average_session_rate_hz, 0.0)

    def test_one_frame_is_a_sparse_message_without_period_or_rate(self):
        _profile, result = self.snapshot([frame(4.5, data=b"\x01\x02")])
        message = result.messages[0]
        self.assertEqual((message.count, message.duration), (1, 0.0))
        self.assertEqual(message.valid_periods, 0)
        self.assertIsNone(message.average_period)
        self.assertIsNone(message.jitter_stddev)
        self.assertEqual(message.average_rate_hz, 0.0)

    def test_identity_preserves_channel_id_and_frame_format(self):
        frames = [
            frame(0, channel="a"), frame(1, channel="b"),
            frame(2, channel="a", is_extended=True),
        ]
        _profile, result = self.snapshot(frames)
        self.assertEqual(result.unique_message_keys, 3)
        self.assertEqual(result.channels, ("a", "b"))
        self.assertEqual((result.standard_frames, result.extended_frames), (2, 1))
        self.assertEqual({item.key for item in result.messages}, {item.key for item in frames})

    def test_classic_fd_brs_esi_remote_and_error_facts_are_deliberate(self):
        frames = [
            frame(0),
            frame(1, arb_id=0x124, data=b"", is_remote_frame=True),
            frame(2, arb_id=0x125, is_fd=True, is_bitrate_switch=True,
                  is_error_state_indicator=True),
            frame(3, arb_id=0x126, is_fd=True,
                  is_error_state_indicator=False),
            frame(4, arb_id=0x127, is_fd=True,
                  is_error_state_indicator=None),
            frame(5, arb_id=0x128, is_error_frame=True),
        ]
        _profile, result = self.snapshot(frames)
        self.assertEqual((result.classic_frames, result.fd_frames), (3, 3))
        self.assertEqual(result.brs_frames, 1)
        self.assertEqual((result.esi_true, result.esi_false, result.esi_unknown),
                         (1, 1, 1))
        self.assertEqual((result.remote_frames, result.error_frames), (1, 1))
        self.assertEqual(result.message_for(frames[-1].key).error_frames, 1)
        self.assertEqual(result.message_for(frames[1].key).remote_frames, 1)

    def test_period_and_population_jitter_definitions(self):
        frames = [frame(value) for value in (0.0, 1.0, 3.0, 6.0)]
        _profile, result = self.snapshot(frames)
        message = result.messages[0]
        self.assertEqual(message.valid_periods, 3)
        self.assertEqual((message.minimum_period, message.average_period,
                          message.maximum_period), (1.0, 2.0, 3.0))
        self.assertAlmostEqual(message.jitter_stddev, math.sqrt(2.0 / 3.0))
        self.assertAlmostEqual(message.average_rate_hz, 0.5)
        self.assertAlmostEqual(result.average_session_rate_hz, 0.5)

    def test_equal_timestamps_are_valid_zero_periods(self):
        _profile, result = self.snapshot([frame(2), frame(2), frame(2)])
        message = result.messages[0]
        self.assertEqual(message.valid_periods, 2)
        self.assertEqual(message.equal_timestamp_periods, 2)
        self.assertEqual(message.average_period, 0.0)
        self.assertEqual(message.jitter_stddev, 0.0)
        self.assertEqual(message.average_rate_hz, 0.0)

    def test_negative_delta_is_excluded_and_next_period_uses_new_baseline(self):
        _profile, result = self.snapshot([frame(10), frame(9), frame(11)])
        message = result.messages[0]
        self.assertEqual(message.out_of_order_timestamps, 1)
        self.assertEqual(message.valid_periods, 1)
        self.assertEqual(message.average_period, 2.0)
        self.assertEqual((message.first_seen, message.last_seen), (9, 11))

    def test_non_finite_timestamp_is_counted_but_not_used_for_rates(self):
        _profile, result = self.snapshot([frame(0), frame(float("nan")), frame(2)])
        message = result.messages[0]
        self.assertEqual((result.invalid_timestamps, message.invalid_timestamps), (1, 1))
        self.assertEqual(message.valid_periods, 1)
        self.assertEqual(message.average_period, 2.0)
        self.assertEqual(message.average_rate_hz, 0.5)
        self.assertEqual(result.average_session_rate_hz, 0.5)

    def test_payload_lengths_changes_bytes_and_unique_payloads(self):
        frames = [
            frame(0, data=b"\x01\x02", dlc=2),
            frame(1, data=b"\x01\x02", dlc=2),
            frame(2, data=b"\x01\x03\x04", dlc=3),
            frame(3, data=b"\x01", dlc=1),
        ]
        _profile, result = self.snapshot(frames)
        message = result.messages[0]
        self.assertEqual(message.payload_lengths, ((1, 1), (2, 2), (3, 1)))
        self.assertEqual(message.dlcs, ((1, 1), (2, 2), (3, 1)))
        self.assertEqual((message.min_payload_length, message.max_payload_length,
                          message.common_payload_length), (1, 3, 2))
        self.assertEqual((message.payload_change_count, message.length_change_count), (2, 2))
        self.assertEqual(message.changed_byte_mask, (False, True, True))
        self.assertEqual(message.byte_change_counts, (0, 2, 2))
        self.assertEqual(message.unique_payloads, 3)
        self.assertTrue(message.unique_payloads_complete)

    def test_unique_payload_memory_is_bounded_and_marked_incomplete(self):
        frames = [frame(i, data=i.to_bytes(2, "big")) for i in range(20)]
        profile, result = self.snapshot(frames, limit=5)
        message = result.messages[0]
        self.assertEqual(message.unique_payloads, 5)
        self.assertFalse(message.unique_payloads_complete)
        self.assertEqual(len(profile._messages[frames[0].key].unique_payloads), 5)

    def test_retention_is_explicit_without_a_second_store(self):
        frames = [frame(i * 0.01, arb_id=0x100 + (i % 3)) for i in range(150)]
        store = FrameStore(100)
        _profile, result = self.snapshot(frames, store)
        self.assertEqual(result.processed_frames, 150)
        self.assertEqual(result.retained_horizon.frame_count, 100)
        self.assertFalse(result.retained_horizon.complete)
        self.assertEqual(result.unique_message_keys, 3)
        self.assertEqual(result.retained_message_keys, 3)
        self.assertAlmostEqual(result.retained_horizon.first_timestamp, 0.50)
        self.assertAlmostEqual(result.retained_horizon.last_timestamp, 1.49)

    def test_reset_discards_timing_payload_and_message_state(self):
        profile = TrafficProfileAccumulator()
        profile.update([frame(0, data=b"\x00"), frame(1, data=b"\x01")])
        profile.reset()
        result = profile.snapshot()
        self.assertEqual((result.processed_frames, result.unique_message_keys), (0, 0))
        self.assertEqual(result.messages, ())

    def test_snapshots_and_nested_profiles_are_immutable(self):
        _profile, result = self.snapshot([frame()])
        with self.assertRaises(FrozenInstanceError):
            result.processed_frames = 5
        with self.assertRaises(FrozenInstanceError):
            result.messages[0].count = 9

    def test_equivalent_normalized_sequences_produce_equal_traffic_profiles(self):
        frames = [frame(i / 10, arb_id=0x100 + i % 2, data=bytes([i]))
                  for i in range(12)]
        live = TrafficProfileAccumulator()
        file_input = TrafficProfileAccumulator()
        live.update(frames[:5])
        live.update(frames[5:])
        file_input.update(frames)
        self.assertEqual(live.snapshot().messages, file_input.snapshot().messages)
        self.assertEqual(live.snapshot().processed_frames,
                         file_input.snapshot().processed_frames)


class CaptureIntegrityTests(unittest.TestCase):
    def test_not_started_keeps_optional_metrics_unavailable(self):
        result = CaptureIntegrityAccumulator().snapshot(0, 0)
        self.assertEqual(result.status, IntegrityStatus.NOT_STARTED)
        self.assertEqual(result.source_state, SourceState.NOT_STARTED)
        self.assertIsNone(result.parse_errors)
        self.assertIsNone(result.driver_overruns)

    def test_silence_and_filtered_all_are_factually_distinguishable(self):
        integrity = CaptureIntegrityAccumulator()
        integrity.set_source_state(SourceState.ACTIVE)
        silent = integrity.snapshot(0, 0, current={"received": 0, "accepted": 0})
        filtered = integrity.snapshot(0, 0, current={"received": 20, "accepted": 0})
        self.assertEqual((silent.received, silent.filtered), (0, 0))
        self.assertEqual((filtered.received, filtered.filtered), (20, 20))
        self.assertEqual(filtered.status, IntegrityStatus.NO_APPLICATION_LOSS_OBSERVED)

    def test_application_loss_and_source_failures_are_separate_reasons(self):
        integrity = CaptureIntegrityAccumulator()
        integrity.set_source_state(SourceState.ACTIVE)
        result = integrity.snapshot(80, 50, current={
            "received": 100, "accepted": 90, "ui_dropped": 5,
            "pause_hidden": 5, "source_errors": 1, "logger_failures": 1,
        }, current_parse_errors=2)
        self.assertEqual(result.status, IntegrityStatus.DEGRADED)
        self.assertEqual(result.filtered, 10)
        self.assertEqual((result.processed, result.retained), (80, 50))
        self.assertEqual((result.ui_dropped, result.pause_hidden), (5, 5))
        self.assertEqual(result.parse_errors, 2)
        self.assertIsNone(result.driver_overruns)
        self.assertGreaterEqual(len(result.reasons), 5)

    def test_stop_start_commits_counters_and_optional_availability(self):
        integrity = CaptureIntegrityAccumulator()
        integrity.commit({"received": 10, "accepted": 8, "ui_dropped": 2},
                         parse_errors=0, driver_overruns=0,
                         state=SourceState.STOPPED)
        result = integrity.snapshot(11, 11, current={"received": 5, "accepted": 3})
        self.assertEqual((result.received, result.accepted, result.ui_dropped), (15, 11, 2))
        self.assertEqual(result.parse_errors, 0)
        self.assertEqual(result.driver_overruns, 0)

    def test_error_state_is_degraded_even_without_numeric_source_counter(self):
        integrity = CaptureIntegrityAccumulator()
        integrity.set_source_state(SourceState.ERROR)
        result = integrity.snapshot(0, 0)
        self.assertEqual(result.status, IntegrityStatus.DEGRADED)

    def test_reported_driver_overruns_are_available_and_degraded(self):
        integrity = CaptureIntegrityAccumulator()
        integrity.set_source_state(SourceState.ACTIVE)
        result = integrity.snapshot(2, 2, current={"received": 2, "accepted": 2},
                                    driver_overruns=3)
        self.assertEqual(result.driver_overruns, 3)
        self.assertEqual(result.status, IntegrityStatus.DEGRADED)
        self.assertTrue(any("driver overruns" in reason for reason in result.reasons))

    def test_reset_starts_a_fresh_integrity_session(self):
        integrity = CaptureIntegrityAccumulator()
        integrity.commit({"received": 9, "accepted": 8, "ui_dropped": 1},
                         parse_errors=2, state=SourceState.STOPPED)
        integrity.reset()
        result = integrity.snapshot(0, 0)
        self.assertEqual((result.received, result.accepted, result.ui_dropped), (0, 0, 0))
        self.assertIsNone(result.parse_errors)
        self.assertEqual(result.status, IntegrityStatus.NOT_STARTED)


if __name__ == "__main__":
    unittest.main()
