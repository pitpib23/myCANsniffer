"""CANopen passive structure, correlation, and false-positive guards."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis.protocols.canopen import (  # noqa: E402
    CanopenObjectClass, classify_cob_id, detect_canopen, sdo_signature,
)
from cansniff.analysis.protocols.model import EvidenceLevel  # noqa: E402
from cansniff.analysis.store import FrameStore  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402


def f(arb, data, t=0.0, channel="can0", extended=False):
    payload = bytes(data)
    return CanFrame(t, arb, payload, len(payload), is_extended=extended,
                    channel=channel)


def detect(frames):
    store = FrameStore(10000)
    store.add(frames)
    return detect_canopen(store.all_frames())


def coherent_node(node=1, start=0.0, channel="can0"):
    frames = [f(0x700 + node, [0x05], start + i, channel) for i in range(3)]
    for offset, base in enumerate((0x180, 0x200, 0x280)):
        frames += [f(base + node, [offset, i], start + 3 + offset + i * 0.1, channel)
                   for i in range(2)]
    frames += [
        f(0x600 + node, [0x40, 0x00, 0x20, 0x00, 0, 0, 0, 0], start + 5, channel),
        f(0x580 + node, [0x43, 0x00, 0x20, 0x00, 1, 2, 3, 4], start + 5.01, channel),
    ]
    return frames


class CobIdTests(unittest.TestCase):
    def test_global_objects_and_legal_node_boundaries(self):
        self.assertEqual(classify_cob_id(0).object_class, CanopenObjectClass.NMT)
        self.assertEqual(classify_cob_id(0x80).object_class, CanopenObjectClass.SYNC)
        self.assertIsNone(classify_cob_id(0x180))
        self.assertEqual(classify_cob_id(0x181).node_id, 1)
        self.assertEqual(classify_cob_id(0x1FF).node_id, 127)
        self.assertIsNone(classify_cob_id(0x680))
        self.assertEqual(classify_cob_id(0x77F).node_id, 127)

    def test_extended_ids_are_never_canopen_cob_ids(self):
        self.assertIsNone(classify_cob_id(0x181, is_extended=True))

    def test_every_supported_object_family_maps_to_same_node(self):
        expected = {
            0x081: "EMCY", 0x181: "TPDO1", 0x201: "RPDO1",
            0x281: "TPDO2", 0x301: "RPDO2", 0x381: "TPDO3",
            0x401: "RPDO3", 0x481: "TPDO4", 0x501: "RPDO4",
            0x581: "SDO response", 0x601: "SDO request",
            0x701: "Heartbeat / boot-up",
        }
        for arb, name in expected.items():
            with self.subTest(arb=arb):
                result = classify_cob_id(arb)
                self.assertEqual((result.node_id, result.object_class.value), (1, name))

    def test_sdo_shapes_require_eight_bytes_known_command_and_nonzero_index(self):
        request = f(0x601, [0x40, 0x00, 0x20, 0, 0, 0, 0, 0])
        response = f(0x581, [0x43, 0x00, 0x20, 0, 1, 2, 3, 4])
        self.assertEqual(sdo_signature(request, True), (0x2000, 0))
        self.assertEqual(sdo_signature(response, False), (0x2000, 0))
        self.assertIsNone(sdo_signature(f(0x601, [0x40, 0, 0, 0]), True))
        self.assertIsNone(sdo_signature(f(0x601, [0x99, 0, 0x20, 0, 0, 0, 0, 0]), True))


class CanopenDetectionTests(unittest.TestCase):
    def test_coherent_heartbeat_pdo_and_sdo_node_is_strong(self):
        result = detect(coherent_node())
        self.assertEqual(result.level, EvidenceLevel.STRONG)
        self.assertEqual(len(result.nodes), 1)
        node = result.nodes[0]
        self.assertEqual(node.node_id, 1)
        self.assertEqual(node.evidence_level, EvidenceLevel.STRONG)
        self.assertEqual(node.sdo_pairs, 1)
        self.assertEqual(node.valid_heartbeat_frames, 3)
        self.assertIn("TPDO1", node.object_classes)
        self.assertTrue(node.reasons)

    def test_multiple_nodes_stay_separate_by_node_and_channel(self):
        result = detect(coherent_node(1, 0, "a") + coherent_node(3, 10, "b"))
        self.assertEqual(result.level, EvidenceLevel.STRONG)
        self.assertEqual([(n.channel, n.node_id) for n in result.nodes],
                         [("a", 1), ("b", 3)])

    def test_boot_up_state_is_valid_heartbeat_shape(self):
        result = detect([f(0x701, [0x00], 0), f(0x701, [0x00], 1),
                         f(0x181, [1], 1.5)])
        node = result.nodes[0]
        self.assertEqual(node.heartbeat_states, ((0, 2),))
        self.assertEqual(node.evidence_level, EvidenceLevel.POSSIBLE)

    def test_isolated_heartbeat_can_never_be_strong(self):
        result = detect([f(0x701, [0x05])])
        self.assertEqual(result.level, EvidenceLevel.WEAK)
        self.assertEqual(result.nodes[0].evidence_level, EvidenceLevel.WEAK)

    def test_random_periodic_byte_in_heartbeat_range_is_weak(self):
        result = detect([f(0x705, [0x33], i) for i in range(20)])
        self.assertEqual(result.level, EvidenceLevel.WEAK)
        self.assertEqual(result.nodes[0].valid_heartbeat_frames, 0)

    def test_irregular_heartbeat_and_pdos_do_not_become_strong_by_volume(self):
        frames = [f(0x701, [0x05], stamp)
                  for stamp in (0.0, 0.01, 10.0, 10.02)]
        for index in range(10):
            frames.append(f(0x181, [index], 11 + index * 0.01))
            frames.append(f(0x201, [index], 12 + index * 0.01))
            frames.append(f(0x281, [index], 13 + index * 0.01))
        result = detect(frames)
        self.assertNotEqual(result.level, EvidenceLevel.STRONG)
        self.assertNotEqual(result.nodes[0].evidence_level, EvidenceLevel.STRONG)

    def test_pdo_ranges_accidentally_sharing_node_offset_are_weak(self):
        frames = [f(base + 7, [i] * 8, i) for i, base in enumerate(
            (0x180, 0x200, 0x280, 0x300, 0x380))]
        result = detect(frames)
        self.assertEqual(result.level, EvidenceLevel.WEAK)
        self.assertEqual(result.likely_keys, ())

    def test_one_sdo_range_id_with_random_payload_is_weak(self):
        result = detect([f(0x601, [0x99] * 8)])
        self.assertEqual(result.level, EvidenceLevel.WEAK)
        self.assertEqual(result.nodes[0].sdo_pairs, 0)

    def test_matching_sdo_fields_far_apart_are_not_paired(self):
        result = detect([
            f(0x601, [0x40, 0x00, 0x20, 0, 0, 0, 0, 0], 0.0),
            f(0x581, [0x43, 0x00, 0x20, 0, 1, 2, 3, 4], 60.0),
        ])
        self.assertEqual(result.nodes[0].sdo_pairs, 0)
        self.assertEqual(result.level, EvidenceLevel.WEAK)

    def test_global_nmt_or_sync_alone_is_only_weak(self):
        result = detect([f(0, [1, 2]), f(0x80, [])])
        self.assertEqual(result.level, EvidenceLevel.WEAK)
        self.assertEqual(result.nodes, ())


if __name__ == "__main__":
    unittest.main()
