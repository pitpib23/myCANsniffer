"""Unified evidence, ISO-TP/UDS layering, Unknown, horizons, and cache."""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import FrozenInstanceError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis.profile import (  # noqa: E402
    CaptureIntegrityAccumulator, SourceState, TrafficProfileAccumulator,
)
from cansniff.analysis.protocols import (  # noqa: E402
    EvidenceLevel, ProtocolKind, ProtocolSurveyCache, SurveyCancelled,
    build_protocol_survey,
)
from cansniff.analysis.store import FrameStore  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402


def f(arb, data, t=0.0, extended=False, channel="can0", error=False):
    payload = bytes(data)
    return CanFrame(t, arb, payload, len(payload), is_extended=extended,
                    is_error_frame=error, channel=channel)


def canopen_frames(node=1, start=0.0):
    frames = [f(0x700 + node, [5], start + i) for i in range(3)]
    frames += [f(0x180 + node, [1, i], start + 3 + i * 0.1) for i in range(2)]
    frames += [f(0x200 + node, [2, i], start + 3.3 + i * 0.1) for i in range(2)]
    frames += [
        f(0x600 + node, [0x40, 0, 0x20, 0, 0, 0, 0, 0], start + 4),
        f(0x580 + node, [0x43, 0, 0x20, 0, 1, 2, 3, 4], start + 4.1),
    ]
    return frames


def isotp_multiframe(start=0.0):
    return [
        f(0x7E8, [0x10, 0x0A, 0x62, 0xF1, 0x90, 1, 2, 3], start),
        f(0x7E8, [0x21, 4, 5, 6, 7, 8, 9, 10], start + 0.01),
    ]


def uds_pairs():
    return [
        f(0x7E0, [0x03, 0x22, 0xF1, 0x90], 0.0),
        f(0x7E8, [0x04, 0x62, 0xF1, 0x90, 1], 0.1),
        f(0x7E0, [0x02, 0x10, 0x03], 1.0),
        f(0x7E8, [0x02, 0x50, 0x03], 1.1),
    ]


def build(frames, max_frames=10000, ui_dropped=0):
    store = FrameStore(max_frames)
    store.add(frames)
    traffic = TrafficProfileAccumulator()
    traffic.update(frames)
    integrity = CaptureIntegrityAccumulator()
    integrity.set_source_state(SourceState.ACTIVE)
    facts = integrity.snapshot(
        len(frames), len(store),
        current={"received": len(frames) + ui_dropped,
                 "accepted": len(frames) + ui_dropped,
                 "ui_dropped": ui_dropped},
    )
    profile = traffic.snapshot(store, facts)
    return build_protocol_survey(store.all_frames(), profile), store, traffic, facts


class GenericModelTests(unittest.TestCase):
    def test_result_order_is_deterministic_and_complete(self):
        snapshot, _store, _traffic, _facts = build([])
        self.assertEqual([item.protocol for item in snapshot.results], [
            ProtocolKind.CANOPEN, ProtocolKind.J1939, ProtocolKind.ISOTP,
            ProtocolKind.UDS, ProtocolKind.UNKNOWN,
        ])

    def test_snapshots_results_and_integrity_context_are_immutable(self):
        snapshot, _store, _traffic, _facts = build(canopen_frames())
        with self.assertRaises(FrozenInstanceError):
            snapshot.generated_from_revision = 9
        with self.assertRaises(FrozenInstanceError):
            snapshot.results[0].level = EvidenceLevel.NONE

    def test_every_positive_result_has_structured_reasons_and_horizon(self):
        snapshot, _store, _traffic, _facts = build(canopen_frames() + uds_pairs())
        for result in snapshot.results:
            if result.present:
                self.assertTrue(result.reasons, result.protocol)
                self.assertIs(result.horizon, snapshot.horizon)
                self.assertIs(result.integrity_context, snapshot.integrity_context)

    def test_confirmed_is_never_created_by_passive_heuristics(self):
        snapshot, _store, _traffic, _facts = build(canopen_frames() + uds_pairs())
        self.assertNotIn(EvidenceLevel.CONFIRMED,
                         {item.level for item in snapshot.results})

    def test_incomplete_retention_is_preserved_as_horizon_and_caveat(self):
        frames = [f(0x555, [i & 0xFF], i * 0.01) for i in range(150)]
        snapshot, _store, _traffic, _facts = build(frames, max_frames=100)
        self.assertFalse(snapshot.horizon.complete)
        self.assertEqual(snapshot.horizon.frame_count, 100)
        self.assertTrue(any("evicted" in caveat for caveat in
                            snapshot.result_for(ProtocolKind.UNKNOWN).caveats))

    def test_cache_reuses_same_revision_and_invalidates_on_new_frames(self):
        frames = canopen_frames()
        store = FrameStore(1000)
        store.add(frames)
        traffic = TrafficProfileAccumulator()
        traffic.update(frames)
        profile = traffic.snapshot(store)
        cache = ProtocolSurveyCache()
        first = cache.build(store.all_frames(), profile)
        self.assertIs(cache.build(store.all_frames(), profile), first)
        extra = f(0x555, [1], 20)
        store.add([extra])
        traffic.update([extra])
        second = cache.build(store.all_frames(), traffic.snapshot(store))
        self.assertIsNot(second, first)

    def test_cooperative_cancellation_aborts_without_partial_snapshot(self):
        frames = [f(0x555, [1], i) for i in range(10)]
        store = FrameStore(100)
        store.add(frames)
        traffic = TrafficProfileAccumulator()
        traffic.update(frames)
        with self.assertRaises(SurveyCancelled):
            build_protocol_survey(store.all_frames(), traffic.snapshot(store),
                                  cancelled=lambda: True)


class IsoTpAndUdsIntegrationTests(unittest.TestCase):
    def test_existing_strong_isotp_maps_to_generic_strong(self):
        snapshot, _store, _traffic, _facts = build(isotp_multiframe())
        result = snapshot.result_for(ProtocolKind.ISOTP)
        self.assertEqual(result.level, EvidenceLevel.STRONG)
        self.assertIn("can0:7E8:S", result.related_message_keys)

    def test_application_loss_downgrades_sequence_sensitive_isotp(self):
        snapshot, _store, _traffic, _facts = build(isotp_multiframe(), ui_dropped=1)
        result = snapshot.result_for(ProtocolKind.ISOTP)
        self.assertEqual(result.level, EvidenceLevel.POSSIBLE)
        self.assertTrue(any("reduced" in item for item in result.caveats))

    def test_uds_uses_only_complete_reconstructed_isotp_payloads(self):
        snapshot, _store, _traffic, _facts = build(uds_pairs())
        uds = snapshot.result_for(ProtocolKind.UDS)
        self.assertEqual(uds.level, EvidenceLevel.STRONG)
        self.assertEqual(len(snapshot.uds_observations), 4)
        self.assertEqual({item.service for item in snapshot.uds_observations},
                         {0x10, 0x22})
        self.assertIsNotNone(snapshot.diagnostic_analysis)
        self.assertEqual(len(snapshot.diagnostic_analysis.conversations), 2)
        self.assertEqual(len(snapshot.diagnostic_analysis.dids), 1)

    def test_incomplete_isotp_is_retained_but_not_decoded_as_uds(self):
        frames = [
            f(0x7E8, [0x10, 0x14, 0x62, 0xF1, 0x90, 1, 2, 3], 0.0),
            f(0x7E8, [0x21, 4, 5, 6, 7, 8, 9, 10], 0.1),
        ]
        snapshot, _store, _traffic, _facts = build(frames)
        analysis = snapshot.diagnostic_analysis
        self.assertEqual(len(analysis.transfers), 1)
        self.assertFalse(analysis.transfers[0].complete)
        self.assertEqual(analysis.events, ())
        self.assertEqual(analysis.conversations, ())

    def test_raw_frame_beginning_with_service_byte_is_not_uds(self):
        snapshot, _store, _traffic, _facts = build([
            f(0x123, [0x22, 0xF1, 0x90], 0),
        ])
        self.assertEqual(snapshot.result_for(ProtocolKind.UDS).level,
                         EvidenceLevel.NONE)
        self.assertEqual(snapshot.uds_observations, ())

    def test_malformed_negative_response_is_not_uds_evidence(self):
        # Valid ISO-TP SF carrying only 7F 22; UDS negative response needs NRC.
        snapshot, _store, _traffic, _facts = build([f(0x7E8, [0x02, 0x7F, 0x22])])
        self.assertEqual(snapshot.result_for(ProtocolKind.UDS).level,
                         EvidenceLevel.NONE)

    def test_valid_negative_response_is_surfaced_from_isotp(self):
        snapshot, _store, _traffic, _facts = build([
            f(0x7E0, [0x03, 0x22, 0xF1, 0x90], 0.0),
            f(0x7E8, [0x03, 0x7F, 0x22, 0x31], 0.1),
        ])
        uds = snapshot.result_for(ProtocolKind.UDS)
        self.assertEqual(uds.level, EvidenceLevel.POSSIBLE)
        negative = snapshot.uds_observations[1]
        self.assertEqual((negative.kind, negative.service, negative.nrc),
                         ("negative-response", 0x22, 0x31))

    def test_service_looking_unknown_isotp_payload_is_not_named_uds(self):
        snapshot, _store, _traffic, _facts = build([f(0x100, [0x02, 0x99, 0])])
        self.assertEqual(snapshot.result_for(ProtocolKind.UDS).level,
                         EvidenceLevel.NONE)

    def test_repeated_requests_without_responses_remain_weak(self):
        frames = [f(0x7E0, [0x03, 0x22, 0xF1, 0x90], float(index))
                  for index in range(12)]
        snapshot, _store, _traffic, _facts = build(frames)
        self.assertEqual(snapshot.result_for(ProtocolKind.UDS).level,
                         EvidenceLevel.WEAK)

    def test_request_and_response_shapes_on_same_key_do_not_pair(self):
        frames = [
            f(0x650, [0x03, 0x22, 0xF1, 0x90], 0.0),
            f(0x650, [0x04, 0x62, 0xF1, 0x90, 1], 0.1),
        ]
        snapshot, _store, _traffic, _facts = build(frames)
        self.assertEqual(snapshot.result_for(ProtocolKind.UDS).level,
                         EvidenceLevel.WEAK)


class UnknownAndMixedTests(unittest.TestCase):
    def test_proprietary_standard_traffic_is_first_class_unknown(self):
        frames = [f(0x555, [0xA0 + i % 4, 1], i) for i in range(10)]
        snapshot, _store, _traffic, _facts = build(frames)
        unknown = snapshot.result_for(ProtocolKind.UNKNOWN)
        self.assertEqual(unknown.level, EvidenceLevel.POSSIBLE)
        self.assertTrue(unknown.related_message_keys)

    def test_proprietary_extended_traffic_is_unknown_despite_numeric_parse(self):
        frames = [f(0x18ABCD00 + i, [i], i, extended=True) for i in range(5)]
        snapshot, _store, _traffic, _facts = build(frames)
        self.assertEqual(snapshot.result_for(ProtocolKind.J1939).level,
                         EvidenceLevel.WEAK)
        self.assertTrue(snapshot.result_for(ProtocolKind.UNKNOWN).present)

    def test_mixed_known_and_proprietary_traffic_reports_both(self):
        proprietary = f(0x555, [0xAA, 0xBB], 30)
        snapshot, _store, _traffic, _facts = build(canopen_frames() + [proprietary])
        self.assertEqual(snapshot.result_for(ProtocolKind.CANOPEN).level,
                         EvidenceLevel.STRONG)
        unknown = snapshot.result_for(ProtocolKind.UNKNOWN)
        self.assertTrue(unknown.present)
        self.assertIn(proprietary.key, unknown.related_message_keys)

    def test_silent_capture_is_not_unknown_proprietary(self):
        snapshot, _store, _traffic, _facts = build([])
        unknown = snapshot.result_for(ProtocolKind.UNKNOWN)
        self.assertEqual(unknown.level, EvidenceLevel.NONE)
        self.assertIn("no usable traffic", unknown.reasons[0].lower())

    def test_only_error_frames_are_not_unknown_proprietary(self):
        snapshot, _store, _traffic, _facts = build([
            f(0, [], 0, error=True), f(0, [], 1, error=True),
        ])
        self.assertEqual(snapshot.result_for(ProtocolKind.UNKNOWN).level,
                         EvidenceLevel.NONE)

    def test_degraded_capture_annotates_unknown_limit(self):
        frames = [f(0x555, [1], i) for i in range(4)]
        snapshot, _store, _traffic, _facts = build(frames, ui_dropped=2)
        unknown = snapshot.result_for(ProtocolKind.UNKNOWN)
        self.assertTrue(any("limited" in reason for reason in unknown.reasons))


if __name__ == "__main__":
    unittest.main()
