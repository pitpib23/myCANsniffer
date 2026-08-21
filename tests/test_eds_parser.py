"""Synthetic EDS/DCF subset, malformed input, and untrusted-file behavior."""

from __future__ import annotations

import os
import unittest

from cansniff.analysis.canopen_definitions import (
    CanopenObjectType, DefinitionParseError, NodeIdExpression,
    parse_definition_bytes, parse_definition_file,
)
from cansniff.analysis.definitions import DefinitionSourceKind, ValidationState


HERE = os.path.dirname(__file__)
EDS = os.path.join(HERE, "fixtures", "synthetic_drive.eds")
DCF = os.path.join(HERE, "fixtures", "synthetic_machine.dcf")


class EdsParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definition = parse_definition_file(EDS)

    def test_device_and_file_info(self):
        values = dict(self.definition.device_info)
        self.assertEqual(values["ProductName"], "Passive Drive")
        self.assertEqual(self.definition.source.format_version, "1")

    def test_var_array_record_and_subobjects(self):
        self.assertEqual(self.definition.dictionary.object(0x1000).object_type,
                         CanopenObjectType.VAR)
        self.assertEqual(self.definition.dictionary.object(0x2000).object_type,
                         CanopenObjectType.ARRAY)
        identity = self.definition.dictionary.object(0x1018)
        self.assertEqual(identity.object_type, CanopenObjectType.RECORD)
        self.assertEqual(identity.subobject(1).name, "Vendor ID")

    def test_common_data_types_values_limits_and_access(self):
        status = self.definition.dictionary.object(0x6041)
        velocity = self.definition.dictionary.object(0x606C)
        self.assertEqual(status.data_type.name, "UNSIGNED16")
        self.assertEqual(status.high_limit, 0xFFFF)
        self.assertEqual(status.access_type, "ro")
        self.assertTrue(status.pdo_mappable)
        self.assertEqual(velocity.data_type.name, "INTEGER32")
        self.assertEqual(velocity.default_value, -1)

    def test_parameter_value_and_vendor_metadata_are_preserved(self):
        sample = self.definition.dictionary.object(0x2000).subobject(1)
        self.assertEqual(sample.configured_value, -2)
        vendor = self.definition.dictionary.object(0x1018).subobject(1)
        self.assertIn(("VendorNote", "synthetic-only"), vendor.metadata)

    def test_mapping_and_node_expression(self):
        mapping = self.definition.pdo_mappings[0]
        self.assertEqual(mapping.total_bits, 48)
        self.assertEqual(mapping.cob_id(3), 0x183)
        self.assertIsInstance(mapping.cob_id_value, NodeIdExpression)
        self.assertEqual([(item.index, item.subindex, item.bit_length)
                          for item in mapping.entries],
                         [(0x6041, 0, 16), (0x606C, 0, 32)])

    def test_unknown_extension_is_preserved_and_warned(self):
        self.assertTrue(self.definition.extra_sections)
        self.assertEqual(self.definition.validation_state,
                         ValidationState.UNSUPPORTED_FEATURES)

    def test_dcf_commissioning_and_configured_value(self):
        definition = parse_definition_file(DCF)
        self.assertEqual(definition.source.kind, DefinitionSourceKind.DCF)
        self.assertEqual(definition.configured_node_id, 4)
        self.assertEqual(definition.dictionary.object(0x6041).configured_value,
                         0x1234)

    def test_duplicate_section_is_fatal_and_contextual(self):
        data = b"[FileInfo]\nA=1\n[1000]\nDataType=7\n[1000]\nDataType=6\n"
        with self.assertRaises(DefinitionParseError) as caught:
            parse_definition_bytes(data)
        self.assertTrue(caught.exception.diagnostics)
        self.assertIn("1000", str(caught.exception))

    def test_malformed_values_do_not_execute_or_crash(self):
        data = (b"[FileInfo]\nFileVersion=1\n[1000]\nParameterName=X\n"
                b"ObjectType=7\nDataType=__import__('os')\nDefaultValue=1\n")
        definition = parse_definition_bytes(data)
        self.assertEqual(definition.validation_state, ValidationState.INVALID)
        self.assertIn("INVALID", definition.dictionary.object(0x1000).data_type.name)

    def test_arbitrary_symbolic_expression_is_rejected_without_eval(self):
        data = (b"[FileInfo]\nFileVersion=1\n[1000]\nParameterName=X\n"
                b"ObjectType=7\nDataType=7\nDefaultValue=$HOME+1\n")
        definition = parse_definition_bytes(data)
        self.assertEqual(definition.validation_state, ValidationState.INVALID)
        self.assertEqual(definition.dictionary.object(0x1000).default_value,
                         "$HOME+1")

    def test_missing_listed_object_is_warning(self):
        data = (b"[FileInfo]\nFileVersion=1\n[MandatoryObjects]\n"
                b"SupportedObjects=1\n1=0x9999\n")
        definition = parse_definition_bytes(data)
        self.assertTrue(any(item.code == "listed-object-missing"
                            for item in definition.diagnostics))

    def test_unsupported_type_remains_visible(self):
        data = (b"[FileInfo]\nFileVersion=1\n[1000]\nParameterName=Vendor\n"
                b"ObjectType=7\nDataType=0x1234\nDefaultValue=1\n")
        definition = parse_definition_bytes(data)
        entry = definition.dictionary.object(0x1000)
        self.assertEqual(entry.data_type.code, 0x1234)
        self.assertFalse(entry.data_type.supported)

    def test_subnumber_mismatch_is_diagnostic(self):
        data = (b"[FileInfo]\nFileVersion=1\n[2000]\nObjectType=9\n"
                b"SubNumber=2\n[2000sub0]\nDataType=5\nDefaultValue=0\n")
        definition = parse_definition_bytes(data)
        self.assertTrue(any(item.code == "subnumber-mismatch"
                            for item in definition.diagnostics))

    def test_compact_and_external_resources_are_preserved_not_followed(self):
        data = (b"[FileInfo]\nFileVersion=1\n[2000]\nParameterName=X\n"
                b"ObjectType=8\nCompactSubObj=1\nUploadFile=http://invalid/x\n")
        definition = parse_definition_bytes(data)
        entry = definition.dictionary.object(0x2000)
        self.assertIn(("CompactSubObj", "1"), entry.metadata)
        self.assertIn(("UploadFile", "http://invalid/x"), entry.metadata)
        self.assertTrue(any(item.code == "unsupported-external-resource"
                            for item in definition.diagnostics))


if __name__ == "__main__":
    unittest.main()
