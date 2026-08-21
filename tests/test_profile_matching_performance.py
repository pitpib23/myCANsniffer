"""Representative matching scale checks; raw frames are never scanned per candidate."""

from __future__ import annotations

import os
import time
import unittest

from cansniff.analysis.canopen_definitions import DefinitionCache, parse_definition_file
from cansniff.analysis.matching import ProfileMatchCache, build_observed_facts
from cansniff.analysis.profile import TrafficProfileAccumulator
from cansniff.analysis.signals import Profile, ProfileStore, Signal
from cansniff.analysis.store import FrameStore
from cansniff.model import CanFrame
from tests.test_profile_matching_canopen import canopen_node, observed


EDS = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic_drive.eds")


def _signal(arb_id):
    return Signal("V", can_id=arb_id, start=0, length=8,
                  message_name="M{:X}".format(arb_id), message_length=8)


def _traffic(message_count=100, repeats=2):
    frames = [CanFrame(repeat + index / 1000, 0x100 + index, b"\0" * 8, 8,
                       channel="can0")
              for repeat in range(repeats) for index in range(message_count)]
    accumulator, store = TrafficProfileAccumulator(), FrameStore(len(frames) + 1)
    accumulator.update(frames)
    store.add(frames)
    return accumulator.snapshot(store)


class ProfileMatchingPerformanceTests(unittest.TestCase):
    def test_100_observed_keys_against_100_profiles(self):
        traffic = _traffic()
        profiles = [Profile(
            "P{}".format(index), signals=[_signal(0x100 + offset)
                                         for offset in range(index % 50, index % 50 + 20)])
                    for index in range(100)]
        started = time.perf_counter()
        result = ProfileMatchCache().build(traffic, ProfileStore(profiles))
        self.assertEqual(len(result.candidates), 100)
        self.assertLess(time.perf_counter() - started, 8.0)

    def test_1000_small_profiles_and_cache_hit(self):
        traffic = _traffic(20)
        profiles = [Profile("P{}".format(index), signals=[
            _signal(0x100 + index % 20), _signal(0x100 + (index + 1) % 20)])
                    for index in range(1000)]
        cache, store = ProfileMatchCache(), ProfileStore(profiles)
        started = time.perf_counter()
        first = cache.build(traffic, store)
        build_elapsed = time.perf_counter() - started
        started = time.perf_counter()
        second = cache.build(traffic, store)
        hit_elapsed = time.perf_counter() - started
        self.assertIs(first, second)
        self.assertEqual(len(first.candidates), 1000)
        self.assertLess(build_elapsed, 12.0)
        self.assertLess(hit_elapsed, build_elapsed)

    def test_large_candidate_uses_message_index(self):
        traffic = _traffic()
        signals = [_signal(0x100 + index) for index in range(100)]
        signals.extend(Signal(
            "V", can_id=0x10000 + index, is_extended=True, start=0, length=8,
            message_name="Extended{}".format(index), message_length=8)
                       for index in range(1900))
        candidate = Profile("Large", signals=signals)
        started = time.perf_counter()
        result = ProfileMatchCache().build(traffic, ProfileStore([candidate]))
        self.assertEqual(result.candidates[0].coverage.matched_observed_keys, 100)
        self.assertLess(time.perf_counter() - started, 4.0)

    def test_duplicate_eds_candidates_share_definition_cache_across_nodes(self):
        definition = parse_definition_file(EDS)
        frames = canopen_node(2) + canopen_node(3) + canopen_node(4)
        traffic, protocols = observed(frames)
        profiles = [Profile("Drive {}".format(index),
                            definitions=[definition.reference])
                    for index in range(30)]
        cache = ProfileMatchCache(DefinitionCache())
        result = cache.build(traffic, ProfileStore(profiles), protocols)
        self.assertEqual(len(result.candidates), 30)
        self.assertTrue(all(item.association_suggestions for item in result.candidates))
        self.assertEqual(cache.definition_cache.size, 1)

    def test_200k_session_frames_are_precomputed_once_by_message_key(self):
        accumulator, retained = TrafficProfileAccumulator(), FrameStore(2000)
        for start in range(0, 200000, 5000):
            batch = [CanFrame((start + index) / 10000, 0x100 + index % 100,
                              b"\0" * 8, 8, channel="can0")
                     for index in range(5000)]
            accumulator.update(batch)
            retained.add(batch)
        traffic = accumulator.snapshot(retained)
        started = time.perf_counter()
        facts = build_observed_facts(traffic)
        elapsed = time.perf_counter() - started
        self.assertEqual(facts.total_frames, 200000)
        self.assertEqual(len(facts.messages), 100)
        self.assertEqual(facts.retained_horizon.frame_count, 2000)
        self.assertLess(elapsed, 4.0)


if __name__ == "__main__":
    unittest.main()
