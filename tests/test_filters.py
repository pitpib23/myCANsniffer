"""Filter tests. Frames are constructed in-process; no interface is opened."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.filters import BytePattern, FilterRule, FilterSet, parse_int  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402


def frame(arb_id=0x100, data=b"\x01\x00\x01", channel="1", extended=False, fd=False):
    return CanFrame(
        timestamp=0.0, arb_id=arb_id, data=data, dlc=len(data),
        is_extended=extended, is_fd=fd, channel=channel,
    )


class ParseIntTests(unittest.TestCase):
    def test_accepted_forms(self):
        self.assertEqual(parse_int("0x1FF"), 511)
        self.assertEqual(parse_int("1FFh"), 511)
        self.assertEqual(parse_int("511"), 511)
        self.assertEqual(parse_int(511), 511)

    def test_empty_is_none(self):
        self.assertIsNone(parse_int(""))
        self.assertIsNone(parse_int(None))


class BytePatternTests(unittest.TestCase):
    def test_exact_and_wildcards(self):
        pattern = BytePattern.parse("81 ?? 01")
        self.assertTrue(pattern.matches(bytes.fromhex("810001")))
        self.assertTrue(pattern.matches(bytes.fromhex("81FF01")))
        self.assertFalse(pattern.matches(bytes.fromhex("810002")))

    def test_nibble_wildcard(self):
        pattern = BytePattern.parse("4?")
        self.assertTrue(pattern.matches(bytes.fromhex("41")))
        self.assertFalse(pattern.matches(bytes.fromhex("51")))

    def test_offset_anchors_the_pattern(self):
        pattern = BytePattern.parse("41 CC", offset=3)
        self.assertTrue(pattern.matches(bytes.fromhex("81000141CC000000")))
        self.assertFalse(pattern.matches(bytes.fromhex("41CC000000000000")))

    def test_pattern_longer_than_payload_does_not_match(self):
        pattern = BytePattern.parse("01 02 03 04")
        self.assertFalse(pattern.matches(b"\x01\x02"))

    def test_invalid_token_rejected(self):
        with self.assertRaises(ValueError):
            BytePattern.parse("zz")


class FilterSetTests(unittest.TestCase):
    def test_no_rules_keeps_everything(self):
        rules = FilterSet.from_config([])
        self.assertFalse(rules.active)
        self.assertTrue(rules.accepts(frame()))

    def test_allow_rule_excludes_non_matching(self):
        rules = FilterSet.from_config([
            {"name": "only 101", "mode": "allow", "id_min": "0x101", "id_max": "0x101"},
        ])
        self.assertTrue(rules.accepts(frame(0x101)))
        self.assertFalse(rules.accepts(frame(0x100)))

    def test_block_rule_wins_over_allow(self):
        rules = FilterSet.from_config([
            {"name": "range", "mode": "allow", "id_min": "0x100", "id_max": "0x1FF"},
            {"name": "noise", "mode": "block", "id_min": "0x101", "id_max": "0x101"},
        ])
        self.assertTrue(rules.accepts(frame(0x100)))
        self.assertFalse(rules.accepts(frame(0x101)))

    def test_disabled_rule_is_inert(self):
        rules = FilterSet.from_config([
            {"name": "off", "mode": "allow", "id_min": "0x999", "enabled": False},
        ])
        self.assertTrue(rules.accepts(frame(0x100)))

    def test_mask_match(self):
        rule = FilterRule.from_dict(
            {"mode": "allow", "id_mask": "0x700", "id_value": "0x100"}
        )
        self.assertTrue(rule.matches(frame(0x123)))
        self.assertFalse(rule.matches(frame(0x223)))

    def test_dlc_and_format_criteria(self):
        rule = FilterRule.from_dict({"dlc_min": 8, "extended": False})
        self.assertTrue(rule.matches(frame(0x100, b"\x00" * 8)))
        self.assertFalse(rule.matches(frame(0x100, b"\x00" * 3)))
        self.assertFalse(rule.matches(frame(0x100, b"\x00" * 8, extended=True)))

    def test_channel_criterion(self):
        rule = FilterRule.from_dict({"channel": "2"})
        self.assertFalse(rule.matches(frame(channel="1")))
        self.assertTrue(rule.matches(frame(channel="2")))

    def test_data_pattern_criterion(self):
        rules = FilterSet.from_config([
            {"mode": "allow", "data_pattern": "81 ?? 01"},
        ])
        self.assertTrue(rules.accepts(frame(0x101, bytes.fromhex("810001"))))
        self.assertFalse(rules.accepts(frame(0x101, bytes.fromhex("810002"))))

    def test_malformed_rule_is_skipped_not_fatal(self):
        rules = FilterSet.from_config([
            {"mode": "allow", "data_pattern": "zz"},
            {"mode": "allow", "id_min": "0x100", "id_max": "0x100"},
        ])
        self.assertEqual(len(rules.rules), 1)
        self.assertTrue(rules.accepts(frame(0x100)))

    def test_round_trip_through_config_dict(self):
        original = FilterRule.from_dict(
            {"name": "r", "mode": "block", "id_min": "0x100", "data_pattern": "81 ??"}
        )
        restored = FilterRule.from_dict(original.to_dict())
        self.assertEqual(restored.id_min, 0x100)
        self.assertEqual(restored.mode, "block")
        self.assertEqual(restored.data_pattern, "81 ??")


if __name__ == "__main__":
    unittest.main()
