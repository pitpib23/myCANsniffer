"""Manual Phase 10 representative-scale benchmark (not unittest discovery)."""

from __future__ import annotations

import gc
import json
import os
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
        "benchmark-{}".format(index), endpoint, peer, timestamp, timestamp,
        True, COMPLETE, bytes(payload), len(payload), 1, "Single frame",
        (), (), (raw,), ())


def run():
    tracemalloc.start()
    transfers, payloads = [], []
    for pair_index in range(50000):
        peer_number = pair_index % 2000
        tester = IsoTpEndpoint(
            "can{}".format(peer_number % 4),
            0x18DA0000 + peer_number * 2, True)
        ecu = IsoTpEndpoint(tester.channel, tester.can_id + 1, True)
        timestamp = pair_index * 0.001
        if pair_index % 10 == 0:
            request, response = b"\x27\x01", b"\x7F\x27\x33"
        else:
            did = 0xF100 + pair_index % 256
            request = bytes((0x22, did >> 8, did & 0xFF))
            response = bytes((0x62, did >> 8, did & 0xFF,
                              pair_index & 0xFF))
        payloads.extend((request, response))
        transfers.extend((
            reconstructed(pair_index * 2, timestamp, tester, ecu, request),
            reconstructed(pair_index * 2 + 1, timestamp + 0.0002,
                          ecu, tester, response),
        ))

    started = time.perf_counter()
    decoded = tuple(interpret(payload) for payload in payloads)
    uds_seconds = time.perf_counter() - started
    assert len(decoded) == 100000
    del decoded
    gc.collect()

    cache = DiagnosticConversationCache()
    started = time.perf_counter()
    result = cache.build(transfers, ("phase10-benchmark", 100000), revision=1)
    conversation_seconds = time.perf_counter() - started
    started = time.perf_counter()
    cached = cache.build(transfers, ("phase10-benchmark", 100000), revision=1)
    cache_seconds = time.perf_counter() - started
    assert cached is result
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "transfers": len(result.transfers),
        "peer_pairs": len(result.peers),
        "conversations": len(result.conversations),
        "did_observations": len(result.dids),
        "negative_responses": sum(
            event.uds.is_negative for event in result.events),
        "uds_decode_seconds": round(uds_seconds, 6),
        "conversation_grouping_seconds": round(conversation_seconds, 6),
        "cache_hit_seconds": round(cache_seconds, 9),
        "tracemalloc_peak_mib": round(peak / (1024 * 1024), 3),
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
