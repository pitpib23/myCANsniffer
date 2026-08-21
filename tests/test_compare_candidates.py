"""Structural candidates and aggressive false-positive fixtures."""

from __future__ import annotations

import os
import random
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis.compare import CandidateKind  # noqa: E402
from cansniff.analysis.protocols.model import EvidenceLevel  # noqa: E402
from tests.test_compare_engine import compare, frame  # noqa: E402


def run(payloads_a, payloads_b, arb=0x321, fd=False):
    frames = [frame(float(i), arb, payload, fd=fd)
              for i, payload in enumerate(payloads_a)]
    start = 1000.0
    frames += [frame(start + i, arb, payload, fd=fd)
               for i, payload in enumerate(payloads_b)]
    snapshot, _store, _profile, _request = compare(
        frames, baseline=(0, len(payloads_a) - 1),
        event=(start, start + len(payloads_b) - 1))
    return snapshot, snapshot.candidates_for("can0:{:03X}:S".format(arb))


def kinds(candidates, kind):
    return [item for item in candidates if item.kind is kind]


class CounterCandidateTests(unittest.TestCase):
    def test_plus_one_with_modular_wrap_is_strong_counter_like(self):
        baseline = [[value & 0xFF] for value in range(240, 288)]
        event = [[value & 0xFF] for value in range(100, 148)]
        _snapshot, candidates = run(baseline, event)
        candidate = kinds(candidates, CandidateKind.COUNTER_LIKE)[0]
        self.assertEqual(candidate.evidence_level, EvidenceLevel.STRONG)
        self.assertTrue(any("wrap" in reason for reason in candidate.reasons))
        self.assertIsNotNone(candidate.baseline_window)
        self.assertIsNotNone(candidate.event_window)
        self.assertTrue(candidate.caveats)
        self.assertNotIn(EvidenceLevel.CONFIRMED,
                         {item.evidence_level for item in candidates})

    def test_dropped_observations_remain_possible_when_wrap_exists(self):
        values = []
        value = 240
        for index in range(80):
            values.append([value & 0xFF])
            value += 2 if index % 9 == 0 else 1
        _snapshot, candidates = run(values[:40], values[40:])
        candidate = kinds(candidates, CandidateKind.COUNTER_LIKE)[0]
        self.assertGreaterEqual(candidate.evidence_level.rank,
                                EvidenceLevel.POSSIBLE.rank)

    def test_smooth_analogue_ramp_without_wrap_is_only_weak(self):
        _snapshot, candidates = run(
            [[value] for value in range(40)],
            [[value] for value in range(40, 80)])
        candidate = kinds(candidates, CandidateKind.COUNTER_LIKE)[0]
        self.assertEqual(candidate.evidence_level, EvidenceLevel.WEAK)
        self.assertTrue(any("alternative" in reason for reason in candidate.reasons))

    def test_random_and_alternating_values_are_not_counter_candidates(self):
        rng = random.Random(42)
        _snapshot, random_candidates = run(
            [[rng.randrange(256)] for _ in range(50)],
            [[rng.randrange(256)] for _ in range(50)])
        self.assertEqual(kinds(random_candidates, CandidateKind.COUNTER_LIKE), [])
        _snapshot, toggle_candidates = run(
            [[index % 2] for index in range(40)],
            [[(index + 1) % 2] for index in range(40)])
        self.assertEqual(kinds(toggle_candidates, CandidateKind.COUNTER_LIKE), [])


class BitfieldCandidateTests(unittest.TestCase):
    def test_independently_toggled_bits_are_status_bitfield_like(self):
        gray = [0, 1, 5, 4, 36, 37, 33, 32]
        baseline = [[0] for _ in range(32)]
        event = [[gray[index % len(gray)]] for index in range(64)]
        _snapshot, candidates = run(baseline, event)
        candidate = kinds(candidates, CandidateKind.STATUS_BITFIELD_LIKE)[0]
        self.assertEqual(candidate.evidence_level, EvidenceLevel.STRONG)
        self.assertTrue(any("0, 2, 5" in reason for reason in candidate.reasons))

    def test_static_byte_and_smooth_multibit_numeric_are_not_bitfields(self):
        _snapshot, static = run([[7]] * 20, [[7]] * 20)
        self.assertEqual(kinds(static, CandidateKind.STATUS_BITFIELD_LIKE), [])
        _snapshot, ramp = run(
            [[value & 0xFF] for value in range(40)],
            [[value & 0xFF] for value in range(40, 80)])
        self.assertEqual(kinds(ramp, CandidateKind.STATUS_BITFIELD_LIKE), [])


class ChecksumCandidateTests(unittest.TestCase):
    @staticmethod
    def payload(value):
        a, b = value & 0x0F, (value >> 4) & 0x03
        checksum = ((a * 17 + b * 31) ^ 0xA5) & 0xFF
        return [a, b, checksum]

    def test_repeatable_high_entropy_final_dependency_is_checksum_like(self):
        baseline = [self.payload(index % 32) for index in range(64)]
        event = [self.payload(32 + index % 32) for index in range(64)]
        _snapshot, candidates = run(baseline, event)
        candidate = kinds(candidates, CandidateKind.CHECKSUM_LIKE)[0]
        self.assertGreaterEqual(candidate.evidence_level.rank,
                                EvidenceLevel.POSSIBLE.rank)
        self.assertTrue(any("Shannon entropy" in reason for reason in candidate.reasons))

    def test_final_counter_static_flag_and_unique_random_field_are_not_checksum(self):
        counter_a = [[7, value & 0xFF] for value in range(240, 280)]
        counter_b = [[8, value & 0xFF] for value in range(100, 140)]
        _snapshot, counter = run(counter_a, counter_b)
        self.assertEqual(kinds(counter, CandidateKind.CHECKSUM_LIKE), [])

        _snapshot, static = run([[value, 0] for value in range(40)],
                                [[value + 40, 0] for value in range(40)])
        self.assertEqual(kinds(static, CandidateKind.CHECKSUM_LIKE), [])

        _snapshot, event_flag = run([[value, 0] for value in range(40)],
                                    [[value + 40, 1] for value in range(40)])
        self.assertEqual(kinds(event_flag, CandidateKind.CHECKSUM_LIKE), [])

        rng = random.Random(19)
        unique_a = [[index, rng.randrange(256)] for index in range(40)]
        unique_b = [[index + 40, rng.randrange(256)] for index in range(40)]
        _snapshot, random_field = run(unique_a, unique_b)
        self.assertEqual(kinds(random_field, CandidateKind.CHECKSUM_LIKE), [])


class NumericCandidateTests(unittest.TestCase):
    def test_little_endian_u16_is_smooth_and_wrong_endian_is_weaker(self):
        def le(value):
            return [value & 0xFF, (value >> 8) & 0xFF]
        _snapshot, candidates = run(
            [le(value) for value in range(240, 260)],
            [le(value) for value in range(260, 280)])
        numeric = {item.decoder_key: item for item in
                   kinds(candidates, CandidateKind.NUMERIC)}
        self.assertEqual(numeric["u16_le"].evidence_level, EvidenceLevel.STRONG)
        self.assertGreater(numeric["u16_le"].score, numeric["u16_be"].score)

    def test_signed_negative_values_are_exposed_neutrally(self):
        def signed(value):
            raw = value.to_bytes(2, "little", signed=True)
            return list(raw)
        _snapshot, candidates = run(
            [signed(value) for value in range(-40, -20)],
            [signed(value) for value in range(-20, 0)])
        numeric = {item.decoder_key: item for item in
                   kinds(candidates, CandidateKind.NUMERIC)}
        self.assertLess(numeric["i16_le"].minimum, 0)
        self.assertEqual(numeric["i16_le"].evidence_level, EvidenceLevel.STRONG)

    def test_unsigned_ambiguity_is_stated_when_signed_values_match(self):
        def le(value):
            return [value & 0xFF, value >> 8]
        _snapshot, candidates = run(
            [le(value) for value in range(100, 120)],
            [le(value) for value in range(120, 140)])
        signed = next(item for item in candidates if item.decoder_key == "i16_le")
        self.assertTrue(any("do not distinguish" in reason for reason in signed.reasons))

    def test_plausible_float32_and_nan_negative(self):
        def f32(value):
            return list(struct.pack("<f", value))
        _snapshot, candidates = run(
            [f32(1.0 + index * 0.02) for index in range(20)],
            [f32(2.0 + index * 0.02) for index in range(20)])
        floats = [item for item in candidates if item.decoder_key == "f32_le"]
        self.assertTrue(floats)
        self.assertGreaterEqual(floats[0].evidence_level.rank,
                                EvidenceLevel.POSSIBLE.rank)

        nan = list(struct.pack("<f", float("nan")))
        _snapshot, invalid = run([[0, 0, 0, 0]] * 20, [nan] * 20)
        self.assertFalse(any(item.decoder_key == "f32_le" for item in invalid))

    def test_variable_length_reports_missing_samples(self):
        baseline = [[value & 0xFF] if value % 2 else [value & 0xFF, 0]
                    for value in range(20)]
        event = [[value & 0xFF, 0] for value in range(20, 40)]
        _snapshot, candidates = run(baseline, event)
        u16 = next(item for item in candidates if item.decoder_key == "u16_le")
        self.assertEqual(u16.missing_samples, 10)

    def test_can_fd_candidate_generation_is_bounded(self):
        baseline = [[0] * 64 for _ in range(20)]
        event = []
        for sample in range(20):
            payload = [0] * 64
            for index in range(64):
                payload[index] = (sample + index) & 0xFF
            event.append(payload)
        _snapshot, candidates = run(baseline, event, fd=True)
        self.assertLessEqual(len(candidates), 32)
        self.assertTrue(all(item.byte_start + item.byte_length <= 64
                            for item in candidates))


if __name__ == "__main__":
    unittest.main()
