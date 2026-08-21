"""Passive ISO-TP/UDS endpoint and conversation reconstruction."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from cansniff.analysis.diagnostics import (
    ConversationSettings, CorrelationStatus, DiagnosticConversationCache,
    IsoTpEndpoint, analyze_diagnostic_transfers, normalize_single_frame,
)
from cansniff.analysis.uds import NEGATIVE, POSITIVE, REQUEST, interpret
from cansniff.model import CanFrame


def frame(timestamp, arb_id, payload, channel="can0", extended=False):
    data = bytes((len(payload),)) + bytes(payload)
    return CanFrame(timestamp, arb_id, data, len(data),
                    is_extended=extended, channel=channel)


def transfer(timestamp, arb_id, payload, ordinal=0, peer=None,
             channel="can0", caveats=()):
    item = frame(timestamp, arb_id, payload, channel)
    hint = IsoTpEndpoint(channel, peer, False) if peer is not None else None
    return normalize_single_frame(item, bytes(payload), ordinal, hint, caveats)


def statuses(analysis):
    return [item.correlation_status for item in analysis.conversations]


class EndpointModelTests(unittest.TestCase):
    def test_endpoint_identity_includes_channel_format_and_addressing(self):
        a = IsoTpEndpoint("can0", 0x7E0, False)
        b = IsoTpEndpoint("can1", 0x7E0, False)
        x = IsoTpEndpoint("can0", 0x7E0, True)
        self.assertNotEqual(a.identity, b.identity)
        self.assertNotEqual(a.identity, x.identity)
        self.assertEqual(a.addressing_mode, "normal")

    def test_endpoint_and_normalized_transfer_are_immutable(self):
        item = transfer(0, 0x7E0, [0x22, 0xF1, 0x90])
        with self.assertRaises(FrozenInstanceError):
            item.endpoint.can_id = 1
        with self.assertRaises(FrozenInstanceError):
            item.payload = b""


class CorrelationTests(unittest.TestCase):
    def test_positive_request_response_and_did_value(self):
        analysis = analyze_diagnostic_transfers((
            transfer(1.0, 0x7E0, [0x22, 0xF1, 0x90]),
            transfer(1.02, 0x7E8, [0x62, 0xF1, 0x90, 0x12, 0x34]),
        ))
        conversation = analysis.conversations[0]
        self.assertEqual(conversation.correlation_status,
                         CorrelationStatus.PAIRED_POSITIVE)
        self.assertAlmostEqual(conversation.latency, 0.02)
        self.assertEqual(conversation.peer_pair.tester_like.can_id, 0x7E0)
        self.assertEqual(analysis.dids[0].did, 0xF190)
        self.assertEqual(analysis.dids[0].raw_value, b"\x12\x34")
        self.assertEqual(len(analysis.dids[0].provenance_frames), 2)

    def test_higher_id_can_be_tester_like_when_observed_direction_says_so(self):
        analysis = analyze_diagnostic_transfers((
            transfer(0, 0x7E8, [0x3E, 0]),
            transfer(.01, 0x7E0, [0x7E, 0]),
        ))
        pair = analysis.conversations[0].peer_pair
        self.assertEqual(pair.tester_like.can_id, 0x7E8)
        self.assertEqual(pair.ecu_like.can_id, 0x7E0)

    def test_negative_response_references_original_service(self):
        analysis = analyze_diagnostic_transfers((
            transfer(0, 0x700, [0x27, 0x01]),
            transfer(.1, 0x708, [0x7F, 0x27, 0x33]),
        ))
        conversation = analysis.conversations[0]
        self.assertEqual(conversation.correlation_status,
                         CorrelationStatus.PAIRED_NEGATIVE)
        self.assertEqual(conversation.response.uds.nrc, 0x33)
        self.assertEqual(conversation.response.uds.nrc_text,
                         "Security access denied")

    def test_same_id_never_pairs_request_and_response(self):
        analysis = analyze_diagnostic_transfers((
            transfer(0, 0x700, [0x10, 0x03]),
            transfer(.1, 0x700, [0x50, 0x03]),
        ))
        self.assertEqual(set(statuses(analysis)), {
            CorrelationStatus.ORPHAN_RESPONSE,
            CorrelationStatus.UNANSWERED_REQUEST,
        })

    def test_stale_response_is_orphan_and_request_times_out(self):
        analysis = analyze_diagnostic_transfers((
            transfer(0, 0x700, [0x10, 0x03]),
            transfer(2, 0x708, [0x50, 0x03]),
        ), settings=ConversationSettings(.5))
        self.assertEqual(set(statuses(analysis)), {
            CorrelationStatus.ORPHAN_RESPONSE,
            CorrelationStatus.UNANSWERED_REQUEST,
        })

    def test_capture_chronology_closes_old_request_at_passive_window(self):
        analysis = analyze_diagnostic_transfers((
            transfer(0, 0x700, [0x10, 0x03]),
            transfer(10, 0x701, [0x3E, 0]),
        ), settings=ConversationSettings(.5))
        session = next(item for item in analysis.conversations
                       if item.service == 0x10)
        self.assertEqual(session.correlation_status,
                         CorrelationStatus.UNANSWERED_REQUEST)
        self.assertIn("within 0.5s", session.reasons[0])

    def test_overlapping_dids_pair_by_identifier_not_nearest_time(self):
        analysis = analyze_diagnostic_transfers((
            transfer(0, 0x7E0, [0x22, 0xF1, 0x90]),
            transfer(.01, 0x7E0, [0x22, 0xF1, 0x91], ordinal=1),
            transfer(.02, 0x7E8, [0x62, 0xF1, 0x90, 1]),
            transfer(.03, 0x7E8, [0x62, 0xF1, 0x91, 2], ordinal=1),
        ))
        paired = [item for item in analysis.conversations if item.request]
        self.assertEqual([item.request.uds.identifier for item in paired],
                         [0xF190, 0xF191])
        self.assertTrue(all(item.correlation_status is
                            CorrelationStatus.PAIRED_POSITIVE for item in paired))

    def test_same_did_outstanding_requests_remain_ambiguous(self):
        analysis = analyze_diagnostic_transfers((
            transfer(0, 0x7E0, [0x22, 0xF1, 0x90]),
            transfer(.01, 0x7E0, [0x22, 0xF1, 0x90], ordinal=1),
            transfer(.02, 0x7E8, [0x62, 0xF1, 0x90, 1]),
        ))
        self.assertIn(CorrelationStatus.AMBIGUOUS, statuses(analysis))
        self.assertEqual(statuses(analysis).count(
            CorrelationStatus.UNANSWERED_REQUEST), 2)
        ambiguous = next(item for item in analysis.conversations
                         if item.correlation_status is CorrelationStatus.AMBIGUOUS)
        self.assertTrue(any("nearest timestamp" in reason
                            for reason in ambiguous.reasons))

    def test_different_channels_and_extended_formats_do_not_pair(self):
        request = transfer(0, 0x700, [0x3E, 0], channel="can0")
        response = transfer(.1, 0x708, [0x7E, 0], channel="can1")
        analysis = analyze_diagnostic_transfers((request, response))
        self.assertNotIn(CorrelationStatus.PAIRED_POSITIVE, statuses(analysis))

    def test_flow_control_peer_hint_prevents_other_ecu_pairing(self):
        analysis = analyze_diagnostic_transfers((
            transfer(0, 0x7E0, [0x10, 3], peer=0x7E8),
            transfer(.01, 0x7E9, [0x50, 3]),
            transfer(.02, 0x7E8, [0x50, 3]),
        ))
        paired = [item for item in analysis.conversations
                  if item.correlation_status is CorrelationStatus.PAIRED_POSITIVE]
        self.assertEqual(len(paired), 1)
        self.assertEqual(paired[0].response.transfer.endpoint.can_id, 0x7E8)

    def test_identical_dids_on_established_peer_pairs_do_not_cross_pair(self):
        analysis = analyze_diagnostic_transfers((
            # Establish two unambiguous directions first.
            transfer(0, 0x700, [0x10, 3]),
            transfer(.01, 0x708, [0x50, 3]),
            transfer(.02, 0x701, [0x10, 3], ordinal=1),
            transfer(.03, 0x709, [0x50, 3], ordinal=1),
            # Identical DID traffic then overlaps on the same channel.
            transfer(.10, 0x700, [0x22, 0xF1, 0x90], ordinal=2),
            transfer(.11, 0x701, [0x22, 0xF1, 0x90], ordinal=3),
            transfer(.12, 0x708, [0x62, 0xF1, 0x90, 8], ordinal=2),
            transfer(.13, 0x709, [0x62, 0xF1, 0x90, 9], ordinal=3),
        ))
        did_pairs = [(item.request.transfer.endpoint.can_id,
                      item.response.transfer.endpoint.can_id)
                     for item in analysis.conversations
                     if item.service == 0x22 and item.response is not None]
        self.assertEqual(did_pairs, [(0x700, 0x708), (0x701, 0x709)])

    def test_response_pending_keeps_request_for_final_response(self):
        analysis = analyze_diagnostic_transfers((
            transfer(0, 0x700, [0x31, 1, 0x12, 0x34]),
            transfer(.1, 0x708, [0x7F, 0x31, 0x78]),
            transfer(.2, 0x708, [0x71, 1, 0x12, 0x34]),
        ))
        self.assertEqual(statuses(analysis), [
            CorrelationStatus.PAIRED_NEGATIVE,
            CorrelationStatus.PAIRED_POSITIVE,
        ])

    def test_capture_integrity_caveats_propagate(self):
        analysis = analyze_diagnostic_transfers((
            transfer(0, 0x700, [0x3E, 0]),
        ), common_caveats=("retained horizon begins after capture start",))
        self.assertIn("retained horizon", analysis.conversations[0].caveats[0])


class StructuredUdsTests(unittest.TestCase):
    def test_sessions_reset_security_routine_and_tester_present_details(self):
        session = interpret(bytes([0x10, 0x83]))
        reset = interpret(bytes([0x11, 0x03]))
        security = interpret(bytes([0x27, 0x01, 1, 2]))
        routine = interpret(bytes([0x31, 0x01, 0x12, 0x34, 0xAA]))
        present = interpret(bytes([0x3E, 0x80]))
        self.assertEqual(session.sub_function_name,
                         "Extended diagnostic session")
        self.assertTrue(session.suppress_positive_response)
        self.assertEqual(reset.sub_function_name, "Soft reset")
        self.assertEqual(security.sub_function_name, "Request seed level 1")
        self.assertEqual(routine.routine_id, 0x1234)
        self.assertEqual(routine.routine_type, 1)
        self.assertTrue(present.suppress_positive_response)

    def test_multiple_dids_preserved_without_guessing_value_boundaries(self):
        request = interpret(bytes([0x22, 0xF1, 0x90, 0xF1, 0x91]))
        self.assertEqual(request.identifiers, (0xF190, 0xF191))
        analysis = analyze_diagnostic_transfers((
            transfer(0, 0x700, request.raw_payload),
            transfer(.1, 0x708, [0x62, 0xF1, 0x90, 1, 2, 3]),
        ))
        self.assertEqual(len(analysis.dids), 2)
        self.assertTrue(all(item.raw_value == b"" for item in analysis.dids))
        self.assertTrue(any("boundaries" in caveat
                            for item in analysis.dids for caveat in item.caveats))

    def test_supported_dtc_layout_and_truncation(self):
        response = interpret(bytes([
            0x59, 0x02, 0xFF, 0x12, 0x34, 0x56, 0x2F,
            0xAB, 0xCD, 0xEF, 0x01,
        ]))
        self.assertEqual(response.dtc_entries,
                         ((0x123456, 0x2F), (0xABCDEF, 0x01)))
        malformed = interpret(bytes([0x59, 0x02, 0xFF, 0x12, 0x34]))
        self.assertIn("truncated", malformed.detail)
        self.assertEqual(malformed.dtc_entries, ())

    def test_dtc_observations_retain_raw_provenance(self):
        analysis = analyze_diagnostic_transfers((
            transfer(0, 0x700, [0x19, 0x02, 0xFF]),
            transfer(.1, 0x708,
                     [0x59, 0x02, 0xFF, 0x12, 0x34, 0x56, 0x2F]),
        ))
        self.assertEqual(len(analysis.dtcs), 1)
        self.assertEqual(analysis.dtcs[0].dtc, 0x123456)
        self.assertEqual(analysis.dtcs[0].status, 0x2F)
        self.assertEqual(analysis.dtcs[0].raw_record, b"\x12\x34\x56\x2f")
        self.assertEqual(len(analysis.dtcs[0].provenance_frames), 1)

    def test_truncated_messages_never_become_events(self):
        analysis = analyze_diagnostic_transfers((
            transfer(0, 0x700, [0x7F]),
            transfer(.1, 0x700, [0x31, 1]),
            transfer(.2, 0x700, [0x22, 0xF1]),
        ))
        self.assertEqual(analysis.events, ())


class CacheTests(unittest.TestCase):
    def test_cache_keys_revision_settings_and_integrity(self):
        values = (transfer(0, 0x700, [0x3E, 0]),)
        cache = DiagnosticConversationCache()
        first = cache.build(values, ("window", 1), 1)
        self.assertIs(cache.build(values, ("window", 1), 1), first)
        self.assertIsNot(cache.build(values, ("window", 1), 2), first)
        second = cache.build(values, ("window", 1), 2)
        changed = cache.build(values, ("window", 1), 2,
                              settings=ConversationSettings(1.0))
        self.assertIsNot(changed, second)


if __name__ == "__main__":
    unittest.main()
