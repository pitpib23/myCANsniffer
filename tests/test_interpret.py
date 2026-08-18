"""Tests for payload splitting and decoding.

All fixtures are constructed in-process. Nothing here touches a CAN interface.
"""

from __future__ import annotations

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.interpret import (  # noqa: E402
    DECODERS, interpret_payload, numeric_decoder_keys, split_fixed,
    split_payload, split_sliding,
)


class SplitTests(unittest.TestCase):
    def test_fixed_split_pairs(self):
        words = split_fixed(bytes.fromhex("81000141CC000000"), 2)
        self.assertEqual([w.offset for w in words], [0, 2, 4, 6])
        self.assertEqual(words[0].raw, bytes.fromhex("8100"))
        self.assertEqual(words[1].raw, bytes.fromhex("0141"))

    def test_odd_aligned_value_needs_the_sliding_split(self):
        # 41 CC starts at offset 3, so the fixed pair grid never produces it.
        payload = bytes.fromhex("81000141CC000000")
        self.assertNotIn(bytes.fromhex("41CC"), [w.raw for w in split_fixed(payload, 2)])
        self.assertIn(bytes.fromhex("41CC"), [w.raw for w in split_sliding(payload, 2)])

    def test_fixed_split_keeps_odd_remainder(self):
        words = split_fixed(bytes.fromhex("010002"), 2, include_remainder=True)
        self.assertEqual([w.length for w in words], [2, 1])
        self.assertEqual(words[1].raw, b"\x02")

    def test_fixed_split_can_drop_remainder(self):
        words = split_fixed(bytes.fromhex("010002"), 2, include_remainder=False)
        self.assertEqual([w.length for w in words], [2])

    def test_sliding_split_offsets(self):
        words = split_sliding(bytes.fromhex("01000102"), 2)
        self.assertEqual([w.offset for w in words], [0, 1, 2])

    def test_combined_split_has_no_duplicate_words(self):
        words = split_payload(bytes.fromhex("01000102"), 2)
        seen = [(w.offset, w.length) for w in words]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(sorted(seen), [(0, 2), (1, 2), (2, 2)])

    def test_both_grids_are_always_produced(self):
        # Regression: the aligned/sliding selector is gone, so the splitter
        # must never return one grid without the other. 41 CC only exists on
        # the sliding grid; 81 00 only on the aligned one.
        payload = bytes.fromhex("81000141CC000000")
        raws = [w.raw for w in split_payload(payload, 2)]
        self.assertIn(bytes.fromhex("8100"), raws)
        self.assertIn(bytes.fromhex("41CC"), raws)

    def test_byte_range_offsets_stay_absolute(self):
        words = split_payload(bytes.fromhex("81000141CC000000"), 2, byte_range=[3, 7])
        self.assertEqual([w.offset for w in words], [3, 4, 5])
        self.assertEqual(words[0].raw, bytes.fromhex("41CC"))

    def test_empty_payload_produces_no_words(self):
        self.assertEqual(split_payload(b"", 2), [])


class BlockIndexTests(unittest.TestCase):
    """The displayed index must name the bytes the block actually contains.

    Regression: the label used Python half-open slice notation, so a 4-byte
    block at offset 3 was labelled ``[3:7]`` — naming byte 7, which it does not
    contain — and the last block of an 8-byte payload was labelled ``[4:8]``,
    naming a byte index that does not exist.
    """

    def test_label_is_inclusive_and_matches_contents(self):
        payload = bytes.fromhex("81000340" "4CCCCD00")
        for word in split_payload(payload, 4):
            first, last = word.first_index, word.last_index
            self.assertEqual(word.span, "{}-{}".format(first, last))
            self.assertEqual(word.indices, list(range(first, last + 1)))
            self.assertEqual(len(word.indices), word.length)
            self.assertEqual(bytes(payload[i] for i in word.indices), word.raw)

    def test_label_never_names_a_byte_outside_the_payload(self):
        payload = bytes.fromhex("81000340" "4CCCCD00")
        for size in (1, 2, 3, 4, 8):
            for word in split_payload(payload, size):
                self.assertLess(word.last_index, len(payload))
                self.assertGreaterEqual(word.first_index, 0)

    def test_single_byte_block_shows_one_index(self):
        word = split_payload(b"\xAA\xBB", 1)[1]
        self.assertEqual(word.span, "1")
        self.assertEqual(word.indices, [1])

    def test_final_aligned_block_of_eight_byte_payload(self):
        # Previously "[4:8]" — byte 8 does not exist on an 8-byte payload.
        words = split_payload(bytes(8), 4)
        self.assertEqual(words[-1].span, "4-7")
        self.assertEqual(words[-1].last_index, 7)

    def test_remainder_block_reports_its_real_length(self):
        words = split_fixed(bytes(8), 3)
        self.assertEqual([w.span for w in words], ["0-2", "3-5", "6-7"])

    def test_restricted_range_keeps_absolute_indices(self):
        payload = bytes.fromhex("81000340" "4CCCCD00")
        words = split_payload(payload, 2, byte_range=[3, 7])
        self.assertEqual([w.span for w in words], ["3-4", "4-5", "5-6"])
        self.assertEqual(words[0].raw, bytes.fromhex("404C"))


class DecoderTests(unittest.TestCase):
    def test_unsigned_and_signed_orders(self):
        raw = bytes.fromhex("41CC")
        self.assertEqual(DECODERS["u16_be"].render(raw), "16844")
        self.assertEqual(DECODERS["u16_le"].render(raw), "52289")
        self.assertEqual(DECODERS["i16_be"].render(raw), "16844")
        self.assertEqual(DECODERS["i16_le"].render(raw), "-13247")

    def test_hex_orders(self):
        raw = bytes.fromhex("41CC")
        self.assertEqual(DECODERS["hex_be"].render(raw), "0x41CC")
        self.assertEqual(DECODERS["hex_le"].render(raw), "0xCC41")

    def test_byte_pairs(self):
        raw = bytes([0x00, 0xFF])
        self.assertEqual(DECODERS["u8_pair"].render(raw), "0 / 255")
        self.assertEqual(DECODERS["i8_pair"].render(raw), "0 / -1")

    def test_length_mismatch_is_not_an_error(self):
        self.assertEqual(DECODERS["u16_be"].render(b"\x01"), "—")
        self.assertEqual(DECODERS["f32_be"].render(b"\x01\x02"), "—")

    def test_float32_matches_baseline_payload(self):
        # 0x101 payload from baseline.asc: 81 00 01 41 CC 00 00 00
        self.assertEqual(DECODERS["f32_be"].render(bytes.fromhex("41CC0000")), "25.5")
        self.assertEqual(DECODERS["f32_be"].render(bytes.fromhex("42600000")), "56")

    def test_bcd_rejects_non_decimal_nibbles(self):
        self.assertEqual(DECODERS["bcd"].render(bytes.fromhex("1234")), "1234")
        self.assertEqual(DECODERS["bcd"].render(bytes.fromhex("1A34")), "—")

    def test_ascii_replaces_unprintable(self):
        self.assertEqual(DECODERS["ascii"].render(b"A\x00"), "A.")


class DecoderCoverageTests(unittest.TestCase):
    """Every block size must yield a value, and every rule decoder a number.

    Regression: block sizes 1 and 8 had no integer or float decoder at all, so
    the table showed nothing but em-dashes; and five decoders had no numeric
    function, so a signal rule using one saved happily and then produced
    nothing with no error anywhere.
    """

    def test_every_block_size_has_a_numeric_decoder(self):
        for size in (1, 2, 4, 8):
            raw = bytes(range(1, size + 1))
            numeric = [k for k, d in DECODERS.items()
                       if d.applies(raw) and d.value(raw) is not None]
            self.assertTrue(numeric, "block size {} has no numeric decoder".format(size))

    def test_eight_byte_block_reads_as_an_integer(self):
        raw = bytes.fromhex("0000000000000100")
        self.assertEqual(DECODERS["u64_be"].render(raw), "256")
        self.assertEqual(DECODERS["i64_be"].render(bytes(b"\xff" * 8)), "-1")

    def test_single_byte_block_reads_as_an_integer(self):
        self.assertEqual(DECODERS["u8"].render(b"\x81"), "129")
        self.assertEqual(DECODERS["i8"].render(b"\x81"), "-127")

    def test_float64_round_trips(self):
        self.assertEqual(DECODERS["f64_be"].render(struct.pack(">d", 3.2)), "3.2")
        self.assertEqual(DECODERS["f64_le"].render(struct.pack("<d", -0.5)), "-0.5")

    def test_hex_decoders_carry_a_value_at_any_width(self):
        # The only way to read a 3-, 5-, 6- or 7-byte field: no fixed-width
        # decoder covers those.
        for width in (1, 2, 3, 5, 6, 7, 8):
            raw = bytes(range(1, width + 1))
            self.assertEqual(DECODERS["hex_be"].value(raw),
                             float(int.from_bytes(raw, "big")))
            self.assertEqual(DECODERS["hex_le"].value(raw),
                             float(int.from_bytes(raw, "little")))

    def test_every_numeric_decoder_actually_produces_a_number(self):
        for key in numeric_decoder_keys():
            decoder = DECODERS[key]
            raw = bytes(range(1, (decoder.exact_len or 2) + 1))
            self.assertIsNotNone(
                decoder.value(raw),
                "{} is marked numeric but produces nothing".format(key))

    def test_text_only_decoders_are_not_marked_numeric(self):
        for key in ("u8_pair", "i8_pair", "ascii"):
            self.assertFalse(DECODERS[key].numeric)
            self.assertNotIn(key, numeric_decoder_keys())

    def test_numeric_flag_matches_having_a_number(self):
        for key, decoder in DECODERS.items():
            self.assertEqual(decoder.numeric, decoder.number is not None, key)


class ByteOrderPreviewRemovalTests(unittest.TestCase):
    """The whole-payload byte-order previews are gone.

    They restated, as hex to be re-read by eye, what the table already gives
    numerically: word-swapping then reading big-endian is the ``u16 LE``
    column, and a full reverse is the same at a mirrored offset, which the
    sliding split already covers.
    """

    def test_interpretation_carries_no_orders(self):
        result = interpret_payload(b"\x01\x02\x03\x04", decoder_keys_enabled=["u16_be"])
        self.assertFalse(hasattr(result, "orders"))

    def test_word_swap_is_just_the_little_endian_column(self):
        payload = bytes.fromhex("0102")
        self.assertEqual(DECODERS["u16_le"].render(payload),
                         DECODERS["u16_be"].render(payload[::-1]))


class InterpretationTests(unittest.TestCase):
    def test_every_word_gets_every_decoder(self):
        result = interpret_payload(
            bytes.fromhex("81000141CC000000"),
            decoder_keys_enabled=["u16_be", "u16_le", "hex_be"],
            word_size=2,
        )
        # 4 aligned blocks plus the 3 sliding ones that are not duplicates.
        self.assertEqual(len(result.words), 7)
        for row in result.words:
            self.assertEqual(set(row.values), {"u16_be", "u16_le", "hex_be"})

    def test_unknown_decoder_key_is_ignored(self):
        result = interpret_payload(b"\x01\x02", decoder_keys_enabled=["nope", "u16_be"])
        self.assertEqual([d.key for d in result.decoders], ["u16_be"])


if __name__ == "__main__":
    unittest.main()
