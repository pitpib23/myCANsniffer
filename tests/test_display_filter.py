"""Tests for the display filter that hides already-received rows."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.filters import DisplayFilter  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402


def frame(arb_id=0x100, data=b"\x01\x00\x01", channel="1", extended=False,
          fd=False, error=False, remote=False):
    return CanFrame(
        timestamp=0.0, arb_id=arb_id, data=data, dlc=len(data),
        is_extended=extended, is_fd=fd, is_error_frame=error,
        is_remote_frame=remote, channel=channel,
    )


class InactiveFilterTests(unittest.TestCase):
    def test_default_filter_is_inactive_and_matches_everything(self):
        empty = DisplayFilter()
        self.assertFalse(empty.is_active)
        self.assertEqual(empty.active_chips(), [])
        self.assertTrue(empty.matches(frame()))
        self.assertTrue(empty.matches(frame(0x7FF, b"\xFF" * 8, extended=True)))


class TextFilterTests(unittest.TestCase):
    def test_matches_can_id(self):
        self.assertTrue(DisplayFilter(text="101").matches(frame(0x101)))
        self.assertFalse(DisplayFilter(text="101").matches(frame(0x200)))

    def test_matches_payload_bytes(self):
        self.assertTrue(DisplayFilter(text="4c cc").matches(
            frame(0x100, bytes.fromhex("404CCCCD"))))

    def test_is_case_insensitive(self):
        self.assertTrue(DisplayFilter(text="4c cc").matches(
            frame(0x100, bytes.fromhex("4CCC"))))
        self.assertTrue(DisplayFilter(text="1ff").matches(frame(0x1FF)))

    def test_contiguous_hex_matches_spaced_payload(self):
        # Payload is displayed as "40 4C CC CD"; typing or pasting it without
        # spaces has to work too.
        payload = frame(0x100, bytes.fromhex("404CCCCD"))
        self.assertTrue(DisplayFilter(text="4CCCCD").matches(payload))
        self.assertTrue(DisplayFilter(text="404ccc").matches(payload))

    def test_hex_prefix_is_tolerated(self):
        self.assertTrue(DisplayFilter(text="0x101").matches(frame(0x101)))

    def test_unrelated_text_still_excluded(self):
        self.assertFalse(DisplayFilter(text="9999").matches(
            frame(0x100, bytes.fromhex("404CCCCD"))))


class IdRangeTests(unittest.TestCase):
    def test_inclusive_bounds(self):
        rule = DisplayFilter(id_min=0x100, id_max=0x1FF)
        self.assertTrue(rule.matches(frame(0x100)))
        self.assertTrue(rule.matches(frame(0x1FF)))
        self.assertFalse(rule.matches(frame(0x0FF)))
        self.assertFalse(rule.matches(frame(0x200)))

    def test_open_ended_bounds(self):
        self.assertTrue(DisplayFilter(id_min=0x100).matches(frame(0x7FF)))
        self.assertTrue(DisplayFilter(id_max=0x100).matches(frame(0x000)))

    def test_single_value_chip_reads_as_one_id(self):
        chips = DisplayFilter(id_min=0x101, id_max=0x101).active_chips()
        self.assertEqual(chips, [("id_range", "CAN ID", "0x101")])


class CategoryTests(unittest.TestCase):
    def test_channel(self):
        self.assertTrue(DisplayFilter(channel="2").matches(frame(channel="2")))
        self.assertFalse(DisplayFilter(channel="2").matches(frame(channel="1")))

    def test_frame_types(self):
        self.assertTrue(DisplayFilter(frame_type="std").matches(frame()))
        self.assertFalse(DisplayFilter(frame_type="std").matches(frame(extended=True)))
        self.assertTrue(DisplayFilter(frame_type="ext").matches(frame(extended=True)))
        self.assertTrue(DisplayFilter(frame_type="fd").matches(frame(fd=True)))
        self.assertFalse(DisplayFilter(frame_type="fd").matches(frame()))
        self.assertTrue(DisplayFilter(frame_type="error").matches(frame(error=True)))
        self.assertTrue(DisplayFilter(frame_type="remote").matches(frame(remote=True)))

    def test_any_type_accepts_all(self):
        rule = DisplayFilter(frame_type="any")
        for candidate in (frame(), frame(extended=True), frame(fd=True), frame(error=True)):
            self.assertTrue(rule.matches(candidate))


class PayloadSizeTests(unittest.TestCase):
    def test_size_bounds_are_inclusive(self):
        rule = DisplayFilter(len_min=3, len_max=8)
        self.assertTrue(rule.matches(frame(data=b"\x00" * 3)))
        self.assertTrue(rule.matches(frame(data=b"\x00" * 8)))
        self.assertFalse(rule.matches(frame(data=b"\x00" * 2)))

    def test_exact_size(self):
        rule = DisplayFilter(len_min=8, len_max=8)
        self.assertTrue(rule.matches(frame(data=b"\x00" * 8)))
        self.assertFalse(rule.matches(frame(data=b"\x00" * 3)))


class CombinationTests(unittest.TestCase):
    def test_filters_combine_with_and(self):
        rule = DisplayFilter(id_min=0x100, id_max=0x1FF, len_min=8, channel="1")
        self.assertTrue(rule.matches(frame(0x101, b"\x00" * 8, channel="1")))
        self.assertFalse(rule.matches(frame(0x101, b"\x00" * 3, channel="1")))
        self.assertFalse(rule.matches(frame(0x101, b"\x00" * 8, channel="2")))
        self.assertFalse(rule.matches(frame(0x300, b"\x00" * 8, channel="1")))


class ChipTests(unittest.TestCase):
    def test_every_active_field_produces_exactly_one_chip(self):
        rule = DisplayFilter(text="4C", id_min=0x100, id_max=0x1FF, channel="1",
                             frame_type="fd", len_min=2, len_max=8)
        fields = [field for field, _name, _value in rule.active_chips()]
        self.assertEqual(fields, ["text", "id_range", "channel", "frame_type", "len_range"])

    def test_chips_are_human_readable(self):
        rule = DisplayFilter(frame_type="ext", len_min=8, len_max=8)
        rendered = {name: value for _f, name, value in rule.active_chips()}
        self.assertEqual(rendered["Frame type"], "Extended (29-bit ID)")
        self.assertEqual(rendered["Payload size"], "8 bytes")

    def test_removing_one_chip_leaves_the_others(self):
        rule = DisplayFilter(text="4C", id_min=0x100, channel="1")
        without_id = rule.cleared_field("id_range")
        self.assertIsNone(without_id.id_min)
        self.assertEqual(without_id.text, "4C")
        self.assertEqual(without_id.channel, "1")

    def test_removing_every_chip_deactivates_the_filter(self):
        rule = DisplayFilter(text="4C", id_min=0x100, id_max=0x1FF, channel="1",
                             frame_type="fd", len_min=2, len_max=8)
        for field, _name, _value in list(rule.active_chips()):
            rule = rule.cleared_field(field)
        self.assertFalse(rule.is_active)
        self.assertEqual(rule, DisplayFilter())

    def test_cleared_field_does_not_mutate_the_original(self):
        rule = DisplayFilter(text="abc", channel="1")
        rule.cleared_field("text")
        self.assertEqual(rule.text, "abc")


if __name__ == "__main__":
    unittest.main()
