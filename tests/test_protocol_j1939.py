"""J1939 identifier math, source aggregation, and false-positive guards."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis.protocols.j1939 import (  # noqa: E402
    PGN_ADDRESS_CLAIM, PGN_REQUEST, detect_j1939, parse_j1939_id,
)
from cansniff.analysis.protocols.model import EvidenceLevel  # noqa: E402
from cansniff.analysis.store import FrameStore  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402


def jid(priority=6, reserved=0, page=0, pf=0xF0, ps=0x04, source=3):
    return ((priority & 7) << 26) | ((reserved & 1) << 25) \
        | ((page & 1) << 24) | ((pf & 0xFF) << 16) \
        | ((ps & 0xFF) << 8) | (source & 0xFF)


def f(can_id, data, t=0.0, channel="can0"):
    payload = bytes(data)
    return CanFrame(t, can_id, payload, len(payload), is_extended=True,
                    channel=channel)


def detect(frames):
    store = FrameStore(10000)
    store.add(frames)
    return detect_j1939(store.all_frames())


def coherent_source(source=3, channel="can0", start=0.0):
    ids_payloads = [
        (jid(pf=0xF0, ps=0x04, source=source), [1] * 8),
        (jid(pf=0xF1, ps=0x00, source=source), [2] * 8),
        (jid(pf=0xFE, ps=0xEE, source=source), [3] * 8),
        # PDU1 Request: PS is destination, not part of PGN.
        (jid(pf=0xEA, ps=0xFF, source=source), [0x00, 0xEE, 0x00]),
    ]
    frames = []
    for group, (can_id, payload) in enumerate(ids_payloads):
        for repeat in range(3):
            frames.append(f(can_id, payload, start + group + repeat * 0.1, channel))
    return frames


class IdentifierParserTests(unittest.TestCase):
    def test_priority_reserved_page_pf_ps_and_source(self):
        value = jid(priority=3, reserved=1, page=1, pf=0xEF, ps=0x22, source=0x45)
        parsed = parse_j1939_id(value)
        self.assertEqual(
            (parsed.priority, parsed.reserved, parsed.data_page,
             parsed.pdu_format, parsed.pdu_specific, parsed.source_address),
            (3, 1, 1, 0xEF, 0x22, 0x45))

    def test_pdu1_zeros_destination_out_of_pgn(self):
        a = parse_j1939_id(jid(pf=0xEA, ps=0x10, source=1))
        b = parse_j1939_id(jid(pf=0xEA, ps=0x99, source=2))
        self.assertTrue(a.is_pdu1)
        self.assertEqual((a.pgn, b.pgn), (PGN_REQUEST, PGN_REQUEST))
        self.assertEqual((a.destination_address, b.destination_address), (0x10, 0x99))

    def test_pdu2_group_extension_contributes_to_pgn(self):
        parsed = parse_j1939_id(jid(pf=0xF0, ps=0x04, source=3))
        self.assertFalse(parsed.is_pdu1)
        self.assertEqual(parsed.pgn, 0xF004)
        self.assertIsNone(parsed.destination_address)

    def test_page_and_reserved_bits_contribute_to_pgn(self):
        parsed = parse_j1939_id(jid(reserved=1, page=1, pf=0xF0, ps=0x04))
        self.assertEqual(parsed.pgn, 0x3F004)

    def test_out_of_range_identifier_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_j1939_id(0x20000000)


class J1939DetectionTests(unittest.TestCase):
    def test_one_source_with_four_repeated_pgns_and_pdu_types_is_strong(self):
        result = detect(coherent_source())
        self.assertEqual(result.level, EvidenceLevel.STRONG)
        self.assertEqual(len(result.sources), 1)
        source = result.sources[0]
        self.assertEqual(source.source_address, 3)
        self.assertEqual(len(source.stable_pgns), 4)
        self.assertEqual(source.frame_count, 12)
        self.assertIn(PGN_REQUEST, source.pgns)

    def test_multiple_source_addresses_are_aggregated_separately(self):
        result = detect(coherent_source(0) + coherent_source(0x21, start=10))
        self.assertEqual(result.level, EvidenceLevel.STRONG)
        self.assertEqual([item.source_address for item in result.sources], [0, 0x21])

    def test_destination_specific_and_broadcast_observations(self):
        result = detect(coherent_source())
        by_pgn = {item.pgn: item for item in result.messages}
        self.assertEqual(by_pgn[PGN_REQUEST].destination_address, 0xFF)
        self.assertIsNone(by_pgn[0xF004].destination_address)

    def test_address_claim_shape_contributes_specific_management_evidence(self):
        claim = jid(pf=0xEE, ps=0xFF, source=7)
        frames = [f(claim, [1] * 8, 0)] + [
            f(jid(pf=0xF0 + p, ps=p, source=7), [p] * 8, p + r * 0.1)
            for p in range(2) for r in range(2)
        ]
        result = detect(frames)
        self.assertTrue(any(item.management_shape == "Address Claim"
                            and item.pgn == PGN_ADDRESS_CLAIM
                            for item in result.messages))
        self.assertGreaterEqual(result.level.rank, EvidenceLevel.POSSIBLE.rank)

    def test_one_arbitrary_extended_id_is_only_weak(self):
        result = detect([f(jid(pf=0xAB, ps=0xCD, source=0xEF), [1] * 8)])
        self.assertEqual(result.level, EvidenceLevel.WEAK)
        self.assertEqual(result.likely_keys, ())

    def test_random_extended_ids_with_unstable_sources_remain_weak(self):
        frames = [f(jid(priority=i % 8, pf=(0x80 + i) & 0xFF,
                        ps=(i * 17) & 0xFF, source=i), [i & 0xFF] * 8, i)
                  for i in range(40)]
        result = detect(frames)
        self.assertEqual(result.level, EvidenceLevel.WEAK)
        self.assertEqual(result.likely_keys, ())

    def test_standard_frames_do_not_enter_j1939_detector(self):
        standard = CanFrame(0, 0x123, b"\x01", 1, is_extended=False)
        result = detect([standard])
        self.assertEqual(result.level, EvidenceLevel.NONE)
        self.assertEqual(result.messages, ())

    def test_mixed_coherent_and_proprietary_extended_traffic_preserves_both(self):
        proprietary = f(jid(pf=0x90, ps=0x22, source=0x99), [9] * 8, 50)
        result = detect(coherent_source() + [proprietary])
        self.assertEqual(result.level, EvidenceLevel.STRONG)
        self.assertIn(proprietary.key, result.related_keys)
        self.assertNotIn(proprietary.key, result.likely_keys)


if __name__ == "__main__":
    unittest.main()
