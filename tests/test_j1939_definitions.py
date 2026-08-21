"""Local J1939 definition provenance and unified payload decoding."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError

from cansniff.analysis.definitions import DefinitionSourceKind, ValidationState
from cansniff.analysis.j1939_definitions import (
    J1939DecodeStatus, J1939DefinitionCache, decode_j1939_payload,
    parse_j1939_definition_file,
)
from cansniff.analysis.protocols.j1939_transport import J1939PayloadObservation
from cansniff.analysis.signals import Profile, ProfileStore
from cansniff.analysis.matching import ProfileMatchCache
from cansniff.analysis.profile import TrafficProfileAccumulator
from cansniff.analysis.store import FrameStore
from cansniff.model import CanFrame


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic_j1939.json")


def observation(pgn=61184, payload=None, transport="BAM"):
    data = payload if payload is not None else bytes(
        [1, 0xF4, 0x01, 0x78, 0x56, 0x34, 0x12, 0xFF])
    return J1939PayloadObservation(
        "can0", pgn, 0x31, None, 6, data, transport, True,
        1.0, 1.2, ())


class J1939DefinitionTests(unittest.TestCase):
    def setUp(self):
        self.definition = parse_j1939_definition_file(FIXTURE)

    def test_fixture_is_synthetic_licensed_immutable_and_hashed(self):
        self.assertEqual(self.definition.source.kind, DefinitionSourceKind.J1939)
        self.assertEqual(len(self.definition.source.content_hash), 64)
        self.assertIn("synthetic", self.definition.license_text.lower())
        self.assertEqual(self.definition.validation_state, ValidationState.VALID)
        with self.assertRaises(FrozenInstanceError):
            self.definition.pgns = ()

    def test_decodes_scaling_state_counter_and_special_value(self):
        result = decode_j1939_payload(observation(), [self.definition])
        self.assertEqual(result.status, J1939DecodeStatus.VALID)
        values = {item.spn: item for item in result.values}
        self.assertEqual(values[1000].display_value, "Running")
        self.assertAlmostEqual(values[1001].value, 10.0)
        self.assertEqual(values[1001].unit, "degC")
        self.assertEqual(values[1002].raw, 0x12345678)
        self.assertEqual(values[1003].status, J1939DecodeStatus.NOT_AVAILABLE)
        self.assertEqual(values[1003].display_value, "Not available")
        error_payload = bytearray(observation().payload)
        error_payload[7] = 0xFE
        error = decode_j1939_payload(
            observation(payload=bytes(error_payload)), [self.definition])
        self.assertEqual(error.values[-1].status, J1939DecodeStatus.ERROR)
        self.assertEqual(error.values[-1].raw, 0xFE)

    def test_single_and_transport_payloads_share_decoder(self):
        single = decode_j1939_payload(
            observation(65280, bytes([0xD2, 0x04, 0, 0, 0, 0, 0, 0]),
                        "Single frame"), [self.definition])
        self.assertEqual(single.values[0].value, 12.34)
        self.assertEqual(single.transport, "Single frame")
        transported = decode_j1939_payload(observation(), [self.definition])
        self.assertEqual(transported.transport, "BAM")

    def test_signed_out_of_range_and_bit_aligned_fields_preserve_raw(self):
        payload = bytearray(observation().payload)
        payload[0] = 0xA1  # low four bits remain the defined mode value 1
        payload[1:3] = (-100).to_bytes(2, "little", signed=True)
        result = decode_j1939_payload(
            observation(payload=bytes(payload)), [self.definition])
        values = {item.spn: item for item in result.values}
        self.assertEqual(values[1000].raw, 1)
        self.assertEqual(values[1001].raw, 0xFF9C)
        self.assertAlmostEqual(values[1001].value, -50.0)
        self.assertEqual(values[1001].status,
                         J1939DecodeStatus.OUT_OF_RANGE)

    def test_unsupported_field_is_retained_as_unsupported_decode(self):
        raw = b'{"schema_version":1,"metadata":{"license":"synthetic"},' \
              b'"pgns":[{"pgn":61184,"length":8,"spns":[' \
              b'{"spn":9,"start_bit":0,"bit_length":8,' \
              b'"byte_order":"big_endian"}]}]}'
        from cansniff.analysis.j1939_definitions import parse_j1939_definition_bytes
        definition = parse_j1939_definition_bytes(raw)
        result = decode_j1939_payload(observation(), (definition,))
        self.assertEqual(result.values[0].status, J1939DecodeStatus.UNSUPPORTED)
        self.assertIsNone(result.values[0].raw)

    def test_unknown_pgn_and_truncated_spn_stay_visible(self):
        unknown = decode_j1939_payload(observation(12345, b"\x01"),
                                      [self.definition])
        self.assertEqual(unknown.status, J1939DecodeStatus.UNKNOWN_PGN)
        self.assertFalse(unknown.values)
        proprietary = decode_j1939_payload(observation(0xFF42, b"\x01"), ())
        self.assertIn("Proprietary B", proprietary.pgn_name)
        short = decode_j1939_payload(observation(payload=b"\x01"),
                                    [self.definition])
        self.assertEqual(short.status, J1939DecodeStatus.LENGTH_MISMATCH)
        self.assertTrue(any(item.status is J1939DecodeStatus.TRUNCATED
                            for item in short.values))

    def test_reference_persists_inside_profile_without_auto_activation(self):
        profile = Profile("local")
        profile.add_definition(self.definition.reference)
        store = ProfileStore([profile], None)
        restored = ProfileStore.from_config(store.to_config())
        reference = restored.profiles[0].definitions[0]
        self.assertEqual(reference.source.kind, DefinitionSourceKind.J1939)
        self.assertEqual(reference.source.content_hash,
                         self.definition.source.content_hash)
        self.assertIsNone(restored.active_profile)

    def test_cache_detects_missing_and_changed_source(self):
        cache = J1939DefinitionCache()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "definition.json")
            with open(FIXTURE, "rb") as source, open(path, "wb") as target:
                target.write(source.read())
            reference = cache.parse_file(path).reference
            with open(path, "ab") as target:
                target.write(b" ")
            resolved, state, reason = cache.resolve(reference)
            self.assertIsNone(resolved)
            self.assertEqual(state, ValidationState.CHANGED)
            self.assertIn("explicit", reason)
        resolved, state, _reason = cache.resolve(reference)
        self.assertIsNone(resolved)
        self.assertEqual(state, ValidationState.MISSING)

    def test_matching_exposes_j1939_provenance_without_auto_activation(self):
        profile = Profile("J1939 candidate", definitions=[self.definition.reference])
        profiles = ProfileStore([profile], None)
        frame = CanFrame(0.0, 0x18FF0031, b"\x01" * 8, 8,
                         is_extended=True, channel="can0")
        store = FrameStore(100)
        store.add((frame,))
        accumulator = TrafficProfileAccumulator()
        accumulator.update((frame,))
        match = ProfileMatchCache().build(accumulator.snapshot(store), profiles)
        candidate = match.candidates[0]
        self.assertEqual(candidate.provenance[0].kind, "J1939")
        self.assertTrue(any("transported PGNs" in item
                            for item in candidate.assumptions))
        self.assertIsNone(profiles.active)


if __name__ == "__main__":
    unittest.main()
