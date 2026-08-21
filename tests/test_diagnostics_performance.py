"""Representative-scale guards for passive conversation reconstruction."""

from __future__ import annotations

import time
import unittest

from cansniff.analysis.diagnostics import (
    DiagnosticConversationCache, DiagnosticTransfer, IsoTpEndpoint,
)
from cansniff.analysis.isotp import COMPLETE
from cansniff.analysis.uds import interpret
from cansniff.model import CanFrame


def reconstructed(index, timestamp, endpoint, peer, payload):
    raw = CanFrame(timestamp, endpoint.can_id, bytes(payload), len(payload),
                   is_extended=True, channel=endpoint.channel)
    return DiagnosticTransfer(
        "scale-{}".format(index), endpoint, peer, timestamp, timestamp,
        True, COMPLETE, bytes(payload), len(payload), 1, "Single frame",
        (), (), (raw,), ())


class DiagnosticPerformanceTests(unittest.TestCase):
    def test_one_hundred_thousand_transfers_many_peers_and_cache_hit(self):
        transfers = []
        payloads = []
        for pair_index in range(50000):
            peer_number = pair_index % 2000
            tester = IsoTpEndpoint(
                "can{}".format(peer_number % 4),
                0x18DA0000 + peer_number * 2, True)
            ecu = IsoTpEndpoint(
                tester.channel, tester.can_id + 1, True)
            stamp = pair_index * 0.001
            if pair_index % 10 == 0:
                request, response = bytes((0x27, 0x01)), bytes((0x7F, 0x27, 0x33))
            else:
                did = 0xF100 + (pair_index % 256)
                request = bytes((0x22, did >> 8, did & 0xFF))
                response = bytes((0x62, did >> 8, did & 0xFF,
                                  pair_index & 0xFF))
            payloads.extend((request, response))
            transfers.append(reconstructed(
                pair_index * 2, stamp, tester, ecu, request))
            transfers.append(reconstructed(
                pair_index * 2 + 1, stamp + 0.0002, ecu, tester, response))

        decode_started = time.perf_counter()
        decoded = tuple(interpret(payload) for payload in payloads)
        decode_elapsed = time.perf_counter() - decode_started
        self.assertEqual(len(decoded), 100000)
        self.assertTrue(all(item.recognised for item in decoded))

        cache = DiagnosticConversationCache()
        grouping_started = time.perf_counter()
        result = cache.build(transfers, ("scale", 100000), revision=1)
        grouping_elapsed = time.perf_counter() - grouping_started
        cache_started = time.perf_counter()
        cached = cache.build(transfers, ("scale", 100000), revision=1)
        cache_elapsed = time.perf_counter() - cache_started

        self.assertEqual(len(result.transfers), 100000)
        self.assertEqual(len(result.conversations), 50000)
        self.assertEqual(len(result.peers), 2000)
        self.assertEqual(len(result.dids), 45000)
        self.assertIs(cached, result)
        # Broad regression ceilings, matching the repository's existing
        # performance-test convention rather than benchmarking exact hardware.
        self.assertLess(decode_elapsed, 10.0)
        self.assertLess(grouping_elapsed, 20.0)
        self.assertLess(cache_elapsed, 0.25)


if __name__ == "__main__":
    unittest.main()
