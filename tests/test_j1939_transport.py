"""Passive J1939 TP reassembly, malformed inputs, and provenance."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from cansniff.analysis.protocols.j1939_transport import (
    J1939TransportKind, J1939TransportStatus, analyze_j1939_transport,
)
from cansniff.analysis.profile import TrafficProfileAccumulator
from cansniff.analysis.profile import (
    CaptureIntegritySnapshot, IntegrityStatus, SourceState,
)
from cansniff.analysis.protocols import build_protocol_survey
from cansniff.analysis.protocols.model import EvidenceLevel, ProtocolKind
from cansniff.analysis.store import FrameStore
from cansniff.model import CanFrame


PGN = 0xEF00


def jid(pf, destination, source):
    return (6 << 26) | (pf << 16) | (destination << 8) | source


def frame(pf, destination, source, data, timestamp):
    payload = bytes(data)
    return CanFrame(timestamp, jid(pf, destination, source), payload,
                    len(payload), is_extended=True, channel="can0")


def pgn_bytes(pgn=PGN):
    return [pgn & 0xFF, (pgn >> 8) & 0xFF, (pgn >> 16) & 0xFF]


def cm(control, size, packets, destination, source, timestamp, extra=0xFF):
    return frame(0xEC, destination, source,
                 [control, size & 0xFF, size >> 8, packets, extra]
                 + pgn_bytes(), timestamp)


def dt(sequence, payload, destination, source, timestamp):
    values = list(payload)
    values.extend([0xFF] * (7 - len(values)))
    return frame(0xEB, destination, source,
                 [sequence] + values, timestamp)


def analysis(frames, complete=True):
    store = FrameStore(1000)
    store.add(frames)
    return analyze_j1939_transport(store.all_frames(), horizon_complete=complete)


class J1939TransportTests(unittest.TestCase):
    def test_interleaved_sources_do_not_contaminate_each_other(self):
        frames = [
            cm(0x20, 9, 2, 0xFF, 0x31, 0.0),
            cm(0x20, 9, 2, 0xFF, 0x32, 0.01),
            dt(1, [1] * 7, 0xFF, 0x31, 0.1),
            dt(1, [2] * 7, 0xFF, 0x32, 0.11),
            dt(2, [3, 4], 0xFF, 0x31, 0.2),
            dt(2, [5, 6], 0xFF, 0x32, 0.21),
        ]
        result = analysis(frames)
        self.assertEqual(len(result.payloads), 2)
        self.assertEqual({item.source_address for item in result.payloads},
                         {0x31, 0x32})
        self.assertEqual({item.payload[0] for item in result.payloads}, {1, 2})

    def test_out_of_order_packets_are_not_optimistically_completed(self):
        result = analysis([
            cm(0x20, 9, 2, 0xFF, 0x31, 0.0),
            dt(2, [7, 8], 0xFF, 0x31, 0.1),
            dt(1, range(7), 0xFF, 0x31, 0.2),
        ])
        self.assertEqual(result.sessions[0].status,
                         J1939TransportStatus.MALFORMED)
        self.assertFalse(result.payloads)

    def test_maximum_protocol_payload_is_bounded_and_reassembled(self):
        payload = bytes(index & 0xFF for index in range(1785))
        frames = [cm(0x20, 1785, 255, 0xFF, 0x31, 0.0)]
        frames.extend(dt(index + 1, payload[index * 7:(index + 1) * 7],
                         0xFF, 0x31, 0.001 * (index + 1))
                      for index in range(255))
        result = analysis(frames)
        self.assertEqual(result.payloads[0].payload, payload)
        self.assertEqual(result.sessions[0].announced_size, 1785)

    def test_malformed_control_and_wrong_source_data_stay_separate(self):
        malformed = frame(0xEC, 0xFF, 0x31,
                          [0x99, 9, 0, 2, 0xFF] + pgn_bytes(), 0.0)
        result = analysis([
            malformed,
            cm(0x20, 9, 2, 0xFF, 0x31, 0.1),
            dt(1, range(7), 0xFF, 0x32, 0.2),
        ])
        self.assertEqual(len(result.sessions), 3)
        self.assertFalse(result.payloads)
        self.assertTrue(any(item.status is J1939TransportStatus.MALFORMED
                            for item in result.sessions))

    def test_known_capture_loss_and_unknown_driver_loss_are_caveats(self):
        store = FrameStore(1000)
        frames = [cm(0x20, 9, 2, 0xFF, 0x31, 0.0)]
        store.add(frames)
        integrity = CaptureIntegritySnapshot(
            IntegrityStatus.DEGRADED, SourceState.ERROR,
            2, 2, 1, 1, 1, 0, 1, 0, 0, None, ("loss",))
        result = analyze_j1939_transport(
            store.all_frames(), integrity=integrity, horizon_complete=False)
        caveats = " ".join(result.sessions[0].caveats).lower()
        self.assertIn("loss", caveats)
        self.assertIn("unavailable", caveats)
        self.assertIn("horizon", caveats)

    def test_survey_exposes_sessions_payloads_without_overclaiming_one_bam(self):
        frames = [
            cm(0x20, 9, 2, 0xFF, 0x31, 0.0),
            dt(1, range(7), 0xFF, 0x31, 0.1),
            dt(2, [7, 8], 0xFF, 0x31, 0.2),
        ]
        store = FrameStore(1000)
        store.add(frames)
        accumulator = TrafficProfileAccumulator()
        accumulator.update(frames)
        snapshot = build_protocol_survey(
            store.all_frames(), accumulator.snapshot(store))
        self.assertEqual(len(snapshot.j1939_transport_sessions), 1)
        self.assertEqual(len(snapshot.j1939_payloads), 1)
        self.assertNotEqual(snapshot.result_for(ProtocolKind.J1939).level,
                            EvidenceLevel.STRONG)

    def test_bam_reassembles_and_trims_padding(self):
        payload = bytes(range(10))
        result = analysis([
            cm(0x20, 10, 2, 0xFF, 0x31, 0.0),
            dt(1, payload[:7], 0xFF, 0x31, 0.1),
            dt(2, payload[7:], 0xFF, 0x31, 0.2),
        ])
        self.assertEqual(len(result.sessions), 1)
        session = result.sessions[0]
        self.assertEqual(session.transport_kind, J1939TransportKind.BAM)
        self.assertEqual(session.status, J1939TransportStatus.COMPLETE)
        self.assertEqual(session.payload, payload)
        self.assertEqual(result.payloads[0].payload, payload)
        self.assertEqual(result.payloads[0].pgn, PGN)
        with self.assertRaises(FrozenInstanceError):
            session.status = J1939TransportStatus.ABORTED

    def test_peer_requires_cts_data_and_end_ack(self):
        payload = bytes(range(9))
        frames = [
            cm(0x10, 9, 2, 0x42, 0x31, 0.0, extra=2),
            frame(0xEC, 0x31, 0x42, [0x11, 2, 1, 0xFF, 0xFF]
                  + pgn_bytes(), 0.05),
            dt(1, payload[:7], 0x42, 0x31, 0.1),
            dt(2, payload[7:], 0x42, 0x31, 0.2),
            frame(0xEC, 0x31, 0x42,
                  [0x13, 9, 0, 2, 0xFF] + pgn_bytes(), 0.3),
        ]
        result = analysis(frames)
        self.assertEqual(result.sessions[0].status,
                         J1939TransportStatus.COMPLETE)
        self.assertEqual([item.name for item in result.sessions[0].controls],
                         ["RTS", "CTS", "EndOfMsgACK"])
        self.assertEqual(result.payloads[0].destination_address, 0x42)

    def test_peer_data_without_ack_never_becomes_application_payload(self):
        result = analysis([
            cm(0x10, 9, 2, 0x42, 0x31, 0.0, extra=2),
            frame(0xEC, 0x31, 0x42, [0x11, 2, 1, 0xFF, 0xFF]
                  + pgn_bytes(), 0.05),
            dt(1, range(7), 0x42, 0x31, 0.1),
            dt(2, [7, 8], 0x42, 0x31, 0.2),
        ])
        self.assertEqual(result.sessions[0].status,
                         J1939TransportStatus.INCOMPLETE)
        self.assertFalse(result.payloads)

    def test_orphan_data_is_evidence_not_payload(self):
        result = analysis([dt(1, range(7), 0xFF, 0x31, 0.0)])
        self.assertEqual(result.orphan_frame_count, 1)
        self.assertEqual(result.sessions[0].sequence_status, "ORPHAN")
        self.assertFalse(result.payloads)

    def test_exact_duplicate_is_diagnostic_but_conflict_is_malformed(self):
        packet = list(range(7))
        exact = analysis([
            cm(0x20, 9, 2, 0xFF, 0x31, 0.0),
            dt(1, packet, 0xFF, 0x31, 0.1),
            dt(1, packet, 0xFF, 0x31, 0.11),
            dt(2, [7, 8], 0xFF, 0x31, 0.2),
        ]).sessions[0]
        self.assertEqual(exact.status, J1939TransportStatus.COMPLETE)
        self.assertEqual(exact.packets[0].duplicate_count, 1)
        conflict = analysis([
            cm(0x20, 9, 2, 0xFF, 0x31, 0.0),
            dt(1, packet, 0xFF, 0x31, 0.1),
            dt(1, [9] * 7, 0xFF, 0x31, 0.11),
            dt(2, [7, 8], 0xFF, 0x31, 0.2),
        ]).sessions[0]
        self.assertEqual(conflict.status, J1939TransportStatus.MALFORMED)
        self.assertFalse(conflict.complete)

    def test_invalid_announcement_and_excess_packet_are_malformed(self):
        result = analysis([
            cm(0x20, 9, 1, 0xFF, 0x31, 0.0),
            dt(2, [1, 2], 0xFF, 0x31, 0.1),
        ])
        self.assertEqual(result.sessions[0].status,
                         J1939TransportStatus.MALFORMED)
        self.assertTrue(any("ceil" in item for item in result.sessions[0].diagnostics))

    def test_timeout_and_retained_horizon_are_explicit(self):
        standard = CanFrame(1.0, 0x123, b"\x01", 1)
        result = analysis([
            cm(0x20, 9, 2, 0xFF, 0x31, 0.0),
            dt(1, range(7), 0xFF, 0x31, 0.1), standard,
        ], complete=False)
        session = result.sessions[0]
        self.assertEqual(session.status, J1939TransportStatus.INCOMPLETE)
        self.assertTrue(any("timeout" in item.lower()
                            for item in session.diagnostics))
        self.assertTrue(any("horizon" in item.lower() for item in session.caveats))

    def test_abort_and_replaced_session_are_preserved(self):
        aborted = analysis([
            cm(0x10, 9, 2, 0x42, 0x31, 0.0, extra=2),
            frame(0xEC, 0x31, 0x42,
                  [0xFF, 2, 0xFF, 0xFF, 0xFF] + pgn_bytes(), 0.1),
        ]).sessions[0]
        self.assertEqual(aborted.status, J1939TransportStatus.ABORTED)
        replaced = analysis([
            cm(0x20, 9, 2, 0xFF, 0x31, 0.0),
            cm(0x20, 10, 2, 0xFF, 0x31, 0.1),
        ]).sessions
        self.assertEqual(len(replaced), 2)
        self.assertTrue(any("replaced" in item.lower()
                            for item in replaced[0].diagnostics))

    def test_single_frame_payload_uses_same_normalized_model(self):
        single = frame(0xFF, 0x00, 0x31, range(8), 0.0)
        result = analysis([single])
        self.assertFalse(result.sessions)
        self.assertEqual(result.payloads[0].transport, "Single frame")
        self.assertEqual(result.payloads[0].pgn, 0xFF00)


if __name__ == "__main__":
    unittest.main()
