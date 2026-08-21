"""Adversarial parser bounds for untrusted local J1939 JSON."""

from __future__ import annotations

import unittest

from cansniff.analysis.j1939_definitions import (
    J1939DefinitionError, MAX_FILE_BYTES, parse_j1939_definition_bytes,
)


BASE = b'{"schema_version":1,"metadata":{"license":"synthetic"},"pgns":[]}'


class J1939DefinitionSecurityTests(unittest.TestCase):
    def test_duplicate_keys_rejected(self):
        with self.assertRaisesRegex(J1939DefinitionError, "duplicate"):
            parse_j1939_definition_bytes(
                b'{"schema_version":1,"schema_version":1,"pgns":[]}')

    def test_nonfinite_numbers_rejected(self):
        with self.assertRaisesRegex(J1939DefinitionError, "non-finite"):
            parse_j1939_definition_bytes(
                b'{"schema_version":1,"pgns":[{"pgn":1,"name":"x",'
                b'"spns":[{"spn":1,"start_bit":0,"bit_length":8,'
                b'"scale":NaN}]}]}')

    def test_oversized_input_rejected_before_json_parse(self):
        with self.assertRaisesRegex(J1939DefinitionError, "MiB"):
            parse_j1939_definition_bytes(b" " * (MAX_FILE_BYTES + 1))

    def test_out_of_range_pgn_and_bit_field_rejected(self):
        with self.assertRaisesRegex(J1939DefinitionError, "pgn"):
            parse_j1939_definition_bytes(
                b'{"schema_version":1,"pgns":[{"pgn":262144,"spns":[]}]}')
        with self.assertRaisesRegex(J1939DefinitionError, "bit_length"):
            parse_j1939_definition_bytes(
                b'{"schema_version":1,"pgns":[{"pgn":1,"spns":['
                b'{"spn":1,"start_bit":0,"bit_length":513}]}]}')

    def test_unsupported_byte_order_is_retained_not_executed(self):
        value = parse_j1939_definition_bytes(
            b'{"schema_version":1,"metadata":{"license":"synthetic"},'
            b'"pgns":[{"pgn":1,"spns":[{"spn":1,"start_bit":0,'
            b'"bit_length":8,"byte_order":"vendor-script()"}]}]}')
        self.assertEqual(value.validation_state.value, "UNSUPPORTED_FEATURES")
        self.assertIn("not decoded", value.pgns[0].spns[0].unsupported_reason)

    def test_wrong_types_and_duplicate_pgn_rejected(self):
        with self.assertRaisesRegex(J1939DefinitionError, "integer"):
            parse_j1939_definition_bytes(
                b'{"schema_version":true,"pgns":[]}')
        with self.assertRaisesRegex(J1939DefinitionError, "duplicate PGN"):
            parse_j1939_definition_bytes(
                b'{"schema_version":1,"pgns":['
                b'{"pgn":1,"spns":[]},{"pgn":1,"spns":[]}]}')


if __name__ == "__main__":
    unittest.main()
