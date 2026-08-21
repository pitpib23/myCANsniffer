"""Passive PDO/SDO enrichment and non-destructive definition conflicts."""

from __future__ import annotations

import os
import unittest

from cansniff.analysis.canopen_definitions import (
    decode_observed_pdos, definition_conflicts, enrich_sdo_observations,
    parse_definition_bytes, parse_definition_file, signal_overlap_conflicts,
)
from cansniff.analysis.definitions import ConflictKind
from cansniff.analysis.signals import Signal
from cansniff.model import CanFrame


EDS = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic_drive.eds")


def frame(can_id, data, timestamp=1.0, channel="can0"):
    return CanFrame(timestamp, can_id, bytes(data), len(data), channel=channel)


class PdoDecodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definition = parse_definition_file(EDS)

    def test_mapped_tpdo_decodes_unsigned_and_signed_values(self):
        data = (0x1234).to_bytes(2, "little") + (-1450).to_bytes(4, "little", signed=True)
        decoded, conflicts = decode_observed_pdos(
            self.definition, 3, [frame(0x183, data)])
        self.assertEqual(len(decoded), 1)
        self.assertEqual([item.name for item in decoded[0].values],
                         ["Statusword", "Velocity actual value"])
        self.assertEqual([item.value for item in decoded[0].values],
                         [0x1234, -1450])
        self.assertFalse(any(item.kind is ConflictKind.OBSERVED_LENGTH
                             for item in conflicts))

    def test_wrong_node_cob_id_is_not_decoded(self):
        decoded, _ = decode_observed_pdos(
            self.definition, 4, [frame(0x183, b"\x00" * 6)])
        self.assertEqual(decoded, ())

    def test_channel_association_is_respected(self):
        decoded, _ = decode_observed_pdos(
            self.definition, 3,
            [frame(0x183, b"\x00" * 6, channel="can1")], "can0")
        self.assertEqual(decoded, ())

    def test_short_payload_is_conflict_not_truncated(self):
        decoded, conflicts = decode_observed_pdos(
            self.definition, 3, [frame(0x183, b"\x34\x12\x00\x00")])
        self.assertEqual(decoded, ())
        self.assertTrue(any(item.kind is ConflictKind.OBSERVED_LENGTH
                            for item in conflicts))

    def test_exact_eight_byte_payload_is_accepted(self):
        decoded, _ = decode_observed_pdos(
            self.definition, 3, [frame(0x183, b"\x00" * 8)])
        self.assertEqual(len(decoded), 1)

    def test_non_byte_aligned_mapping(self):
        data = (b"[FileInfo]\nFileVersion=1\n"
                b"[1A00]\nObjectType=9\nSubNumber=2\n"
                b"[1A00sub0]\nDataType=5\nDefaultValue=1\n"
                b"[1A00sub1]\nDataType=7\nDefaultValue=0x2000000C\n"
                b"[2000]\nParameterName=Twelve bits\nObjectType=7\n"
                b"DataType=6\nPDOMapping=1\n")
        definition = parse_definition_bytes(data)
        decoded, _ = decode_observed_pdos(
            definition, 2, [frame(0x182, b"\xBC\x0A")])
        self.assertEqual(decoded[0].values[0].value, 0xABC)
        self.assertEqual(decoded[0].values[0].bit_length, 12)

    def test_mapping_missing_object_is_conflict(self):
        data = (b"[FileInfo]\nFileVersion=1\n"
                b"[1A00]\nObjectType=9\nSubNumber=2\n"
                b"[1A00sub0]\nDataType=5\nDefaultValue=1\n"
                b"[1A00sub1]\nDataType=7\nDefaultValue=0x99990008\n")
        definition = parse_definition_bytes(data)
        decoded, conflicts = decode_observed_pdos(
            definition, 1, [frame(0x181, b"\x01")])
        self.assertEqual(decoded, ())
        self.assertTrue(any(item.kind is ConflictKind.OBJECT_MISSING
                            for item in conflicts))

    def test_invalid_definition_is_visible_but_not_used_for_decode(self):
        data = (b"[FileInfo]\nFileVersion=1\n"
                b"[1A00]\nObjectType=9\nSubNumber=2\n"
                b"[1A00sub0]\nDataType=5\nDefaultValue=1\n"
                b"[1A00sub1]\nDataType=7\nDefaultValue=0x20000008\n"
                b"[2000]\nParameterName=X\nObjectType=7\nDataType=bad\n"
                b"PDOMapping=1\n")
        definition = parse_definition_bytes(data)
        decoded, conflicts = decode_observed_pdos(
            definition, 1, [frame(0x181, b"\x01")])
        self.assertEqual(decoded, ())
        self.assertEqual(conflicts[0].kind, ConflictKind.VALIDATION)

    def test_configured_cob_id_disagreement_with_observed_default_warns(self):
        data = (b"[FileInfo]\nFileVersion=1\n"
                b"[1800]\nObjectType=9\nSubNumber=2\n"
                b"[1800sub0]\nDataType=5\nDefaultValue=1\n"
                b"[1800sub1]\nDataType=7\nDefaultValue=0x250\n"
                b"[1A00]\nObjectType=9\nSubNumber=2\n"
                b"[1A00sub0]\nDataType=5\nDefaultValue=1\n"
                b"[1A00sub1]\nDataType=7\nDefaultValue=0x20000008\n"
                b"[2000]\nParameterName=X\nObjectType=7\nDataType=5\n"
                b"PDOMapping=1\n")
        definition = parse_definition_bytes(data)
        decoded, conflicts = decode_observed_pdos(
            definition, 1, [frame(0x181, b"\x01")])
        self.assertEqual(decoded, ())
        self.assertTrue(any(item.kind is ConflictKind.COB_ID
                            for item in conflicts))

    def test_rpdo_default_mapping_decodes_at_node_specific_cob_id(self):
        data = (b"[FileInfo]\nFileVersion=1\n"
                b"[1600]\nObjectType=9\nSubNumber=2\n"
                b"[1600sub0]\nDataType=5\nDefaultValue=1\n"
                b"[1600sub1]\nDataType=7\nDefaultValue=0x20000008\n"
                b"[2000]\nParameterName=Command\nObjectType=7\nDataType=5\n"
                b"AccessType=rw\nPDOMapping=1\n")
        definition = parse_definition_bytes(data)
        decoded, conflicts = decode_observed_pdos(
            definition, 1, [frame(0x201, b"\x7F")])
        self.assertFalse(conflicts)
        self.assertEqual(decoded[0].direction, "RPDO")
        self.assertEqual(decoded[0].values[0].value, 0x7F)

    def test_mapping_type_length_mismatch_is_explicit(self):
        data = (b"[FileInfo]\nFileVersion=1\n"
                b"[1A00]\nObjectType=9\nSubNumber=2\n"
                b"[1A00sub0]\nDataType=5\nDefaultValue=1\n"
                b"[1A00sub1]\nDataType=7\nDefaultValue=0x20000010\n"
                b"[2000]\nParameterName=Wide claim\nObjectType=7\nDataType=7\n"
                b"PDOMapping=1\n")
        definition = parse_definition_bytes(data)
        self.assertTrue(any(item.code == "mapping-type-length"
                            for item in definition.pdo_mappings[0].diagnostics))

    def test_disabled_configured_pdo_is_not_decoded(self):
        data = (b"[FileInfo]\nFileVersion=1\n"
                b"[1800]\nObjectType=9\nSubNumber=2\n"
                b"[1800sub0]\nDataType=5\nDefaultValue=1\n"
                b"[1800sub1]\nDataType=7\nDefaultValue=0x80000181\n"
                b"[1A00]\nObjectType=9\nSubNumber=2\n"
                b"[1A00sub0]\nDataType=5\nDefaultValue=1\n"
                b"[1A00sub1]\nDataType=7\nDefaultValue=0x20000008\n"
                b"[2000]\nParameterName=X\nObjectType=7\nDataType=5\n"
                b"PDOMapping=1\n")
        definition = parse_definition_bytes(data)
        decoded, _conflicts = decode_observed_pdos(
            definition, 1, [frame(0x181, b"\x01")])
        self.assertEqual(decoded, ())


class SdoEnrichmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definition = parse_definition_file(EDS)

    def test_known_sdo_gains_name_type_and_source(self):
        observation = enrich_sdo_observations(
            self.definition, 3,
            [frame(0x583, b"\x4B\x41\x60\x00\x34\x12\x00\x00")])[0]
        self.assertEqual(observation.name, "Statusword")
        self.assertEqual(observation.data_type, "UNSIGNED16")
        self.assertEqual(observation.raw_value, 0x1234)
        self.assertEqual(observation.source.display_name, "synthetic_drive.eds")

    def test_unknown_sdo_remains_raw_with_warning(self):
        observation = enrich_sdo_observations(
            self.definition, 3,
            [frame(0x583, b"\x4F\x99\x99\x00\x01\x00\x00\x00")])[0]
        self.assertEqual(observation.name, "Unknown object")
        self.assertEqual(observation.raw_value, 1)
        self.assertTrue(observation.conflicts)

    def test_access_direction_conflict_is_conservative_warning(self):
        observation = enrich_sdo_observations(
            self.definition, 3,
            [frame(0x603, b"\x2B\x41\x60\x00\x34\x12\x00\x00")])[0]
        self.assertTrue(any(item.kind is ConflictKind.ACCESS
                            for item in observation.conflicts))


class CoexistenceTests(unittest.TestCase):
    def setUp(self):
        self.definition = parse_definition_file(EDS)

    def test_dbc_and_manual_overlaps_retain_both_provenances(self):
        signals = [
            Signal(name="DBC speed", can_id=0x183, start=16, length=32,
                   source_kind="DBC"),
            Signal(name="Manual status", can_id=0x183, start=0, length=16),
        ]
        conflicts = signal_overlap_conflicts(self.definition, 3, signals)
        self.assertEqual({item.kind for item in conflicts},
                         {ConflictKind.DBC_OVERLAP, ConflictKind.MANUAL_OVERLAP})
        self.assertEqual([item.name for item in signals],
                         ["DBC speed", "Manual status"])

    def test_two_definitions_disagree_without_a_winner(self):
        other = parse_definition_bytes(
            b"[FileInfo]\nFileVersion=1\n[6041]\nParameterName=Other\n"
            b"ObjectType=7\nDataType=7\nAccessType=rw\nPDOMapping=1\n",
            "other.eds")
        conflicts = definition_conflicts((self.definition, other))
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].kind, ConflictKind.DEFINITION_OVERLAP)
        self.assertIn("synthetic_drive.eds", conflicts[0].source_a)
        self.assertEqual(self.definition.dictionary.object(0x6041).name,
                         "Statusword")
        self.assertEqual(other.dictionary.object(0x6041).name, "Other")


if __name__ == "__main__":
    unittest.main()
