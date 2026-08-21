"""Scale-shape guards without fragile wall-clock assertions."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis.profile import TrafficProfileAccumulator  # noqa: E402
from cansniff.analysis.protocols import (  # noqa: E402
    EvidenceLevel, ProtocolKind, ProtocolSurveyCache,
)
from cansniff.analysis.store import FrameStore  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402


class ProtocolSurveyScaleTests(unittest.TestCase):
    def test_large_periodic_proprietary_capture_stays_conservative_and_cacheable(self):
        # The first payload nibble is deliberately not ISO-TP-shaped. The ID
        # happens to occupy a CANopen PDO range, which must not become a
        # protocol conclusion merely through repetition.
        frames = [
            CanFrame(index * 0.0001, 0x555,
                     bytes((0xA0 | (index & 0x0F), index & 0xFF)), 2,
                     channel="can0")
            for index in range(25000)
        ]
        store = FrameStore(25000)
        store.add(frames)
        profile_builder = TrafficProfileAccumulator()
        profile_builder.update(frames)
        profile = profile_builder.snapshot(store)
        cache = ProtocolSurveyCache()

        first = cache.build(store.all_frames(), profile)
        second = cache.build(store.all_frames(), profile)

        self.assertIs(second, first)
        self.assertEqual(first.horizon.frame_count, 25000)
        self.assertEqual(first.result_for(ProtocolKind.CANOPEN).level,
                         EvidenceLevel.WEAK)
        self.assertEqual(first.result_for(ProtocolKind.J1939).level,
                         EvidenceLevel.NONE)
        self.assertEqual(first.result_for(ProtocolKind.ISOTP).level,
                         EvidenceLevel.NONE)
        self.assertEqual(first.result_for(ProtocolKind.UDS).level,
                         EvidenceLevel.NONE)
        self.assertTrue(first.result_for(ProtocolKind.UNKNOWN).present)


if __name__ == "__main__":
    unittest.main()
