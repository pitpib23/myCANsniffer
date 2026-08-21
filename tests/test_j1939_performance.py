"""Broad guards against quadratic TP and SPN processing."""

from __future__ import annotations

import os
import time
import unittest

from cansniff.analysis.j1939_definitions import (
    decode_j1939_payloads, parse_j1939_definition_file,
)
from cansniff.analysis.protocols.j1939_transport import (
    J1939PayloadObservation, analyze_j1939_transport,
)
from cansniff.analysis.store import FrameStore
from cansniff.model import CanFrame


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic_j1939.json")


class J1939PerformanceTests(unittest.TestCase):
    def test_many_bam_sessions_remain_linear_enough(self):
        frames = []
        for index in range(2000):
            source = index % 240
            base = index * 0.01
            cm_id = (6 << 26) | (0xEC << 16) | (0xFF << 8) | source
            dt_id = (6 << 26) | (0xEB << 16) | (0xFF << 8) | source
            frames.extend((
                CanFrame(base, cm_id,
                         bytes([0x20, 9, 0, 2, 0xFF, 0, 0xEF, 0]), 8,
                         is_extended=True, channel="can0"),
                CanFrame(base + 0.001, dt_id, bytes([1]) + bytes(range(7)), 8,
                         is_extended=True, channel="can0"),
                CanFrame(base + 0.002, dt_id,
                         bytes([2, 7, 8, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]), 8,
                         is_extended=True, channel="can0"),
            ))
        store = FrameStore(len(frames))
        store.add(frames)
        started = time.perf_counter()
        result = analyze_j1939_transport(store.all_frames())
        elapsed = time.perf_counter() - started
        self.assertEqual(len(result.sessions), 2000)
        self.assertEqual(len(result.payloads), 2000)
        self.assertLess(elapsed, 4.0)

    def test_many_payload_decodes_reuse_definition_index_shape(self):
        definition = parse_j1939_definition_file(FIXTURE)
        payloads = tuple(J1939PayloadObservation(
            "can0", 61184, index % 240, None, 6,
            bytes([1, 0xF4, 1, 0x78, 0x56, 0x34, 0x12, 0xFF]),
            "BAM", True, index / 1000.0, index / 1000.0, ())
            for index in range(10000))
        started = time.perf_counter()
        decoded = decode_j1939_payloads(payloads, (definition,))
        elapsed = time.perf_counter() - started
        self.assertEqual(len(decoded), 10000)
        self.assertEqual(len(decoded[-1].values), 4)
        self.assertLess(elapsed, 4.0)


if __name__ == "__main__":
    unittest.main()
