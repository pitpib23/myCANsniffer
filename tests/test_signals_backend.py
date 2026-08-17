"""The unified Signal Database backend.

Covers the model, DBC import/export round trips, the scaling math across byte
order/signedness/float, "any ID"/channel matching, live preview, legacy
migration, and the one artifact applying a profile produces: a DbcDatabase
built from its message-bound signals.

Nothing here opens a CAN interface or exercises any encode/transmit surface.
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis import signals as S  # noqa: E402
from cansniff.analysis.dbc import DbcDatabase  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample.dbc")


def _frame(arb=0x100, data=b"\x00" * 8, ext=False, channel="1", t=0.0):
    return CanFrame(timestamp=t, arb_id=arb, data=bytes(data), dlc=len(data),
                    channel=channel, is_extended=ext)


class _TempDir(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="cansniff_signals_")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for name in os.listdir(self._dir):
            try:
                os.remove(os.path.join(self._dir, name))
            except OSError:
                pass
        try:
            os.rmdir(self._dir)
        except OSError:
            pass

    def _path(self, name):
        return os.path.join(self._dir, name)


# ---------------------------------------------------------------------------
# the Signal model itself
# ---------------------------------------------------------------------------


class SignalModelTests(unittest.TestCase):
    def test_defaults_are_sane(self):
        signal = S.Signal()
        self.assertTrue(signal.enabled)
        self.assertIsNone(signal.can_id)
        self.assertEqual(signal.byte_order, S.LITTLE_ENDIAN)

    def test_float32_forces_length_to_32_bits(self):
        signal = S.Signal(encoding=S.FLOAT32, length=8)
        self.assertEqual(signal.length, 32)

    def test_float64_forces_length_to_64_bits(self):
        signal = S.Signal(encoding=S.FLOAT64, length=8)
        self.assertEqual(signal.length, 64)

    def test_an_unknown_byte_order_falls_back_to_little_endian(self):
        signal = S.Signal(byte_order="nonsense")
        self.assertEqual(signal.byte_order, S.LITTLE_ENDIAN)

    def test_byte_aligned_recognises_whole_byte_spans(self):
        self.assertTrue(S.Signal(start=8, length=16).byte_aligned)
        self.assertFalse(S.Signal(start=3, length=16).byte_aligned)
        self.assertFalse(S.Signal(start=8, length=5).byte_aligned)

    def test_id_label_reads_any_id_when_unbound(self):
        self.assertEqual(S.Signal(can_id=None).id_label, "any ID")
        self.assertEqual(S.Signal(can_id=0x100).id_label, "0x100")

    def test_duplicated_copies_every_field_and_renames(self):
        original = S.Signal(name="Rpm", can_id=0x100, scale=0.25, unit="rpm")
        copy = original.duplicated()
        self.assertEqual(copy.name, "Rpm copy")
        self.assertEqual(copy.can_id, 0x100)
        self.assertEqual(copy.scale, 0.25)
        self.assertIsNot(copy, original)

    def test_round_trips_through_dict(self):
        original = S.Signal(name="X", can_id=0x7E0, is_extended=True,
                            channel="2", start=8, length=16,
                            byte_order=S.BIG_ENDIAN, is_signed=True,
                            scale=0.5, offset=-10.0, minimum=0.0,
                            maximum=100.0, unit="C", decimals=2,
                            choices=((1, "on"), (0, "off")))
        again = S.Signal.from_dict(original.to_dict())
        self.assertEqual(again.name, "X")
        self.assertEqual(again.can_id, 0x7E0)
        self.assertTrue(again.is_extended)
        self.assertEqual(again.channel, "2")
        self.assertEqual(again.byte_order, S.BIG_ENDIAN)
        self.assertTrue(again.is_signed)
        self.assertEqual(again.scale, 0.5)
        self.assertEqual(again.offset, -10.0)
        # A faithful round trip, not a re-sort: whatever order was given comes
        # back unchanged.
        self.assertEqual(again.choices, ((1, "on"), (0, "off")))

    def test_can_id_survives_as_hex_text_in_the_dict_form(self):
        d = S.Signal(can_id=0x1FF).to_dict()
        self.assertEqual(d["can_id"], "0x1FF")
        self.assertEqual(S.Signal.from_dict(d).can_id, 0x1FF)

    def test_any_id_stays_none_through_the_dict_form(self):
        d = S.Signal(can_id=None).to_dict()
        self.assertIsNone(d["can_id"])
        self.assertIsNone(S.Signal.from_dict(d).can_id)


# ---------------------------------------------------------------------------
# matching: can_id / any ID / channel
# ---------------------------------------------------------------------------


class MatchingTests(unittest.TestCase):
    def test_a_bound_signal_matches_only_its_id(self):
        signal = S.Signal(can_id=0x100)
        self.assertTrue(signal.matches(_frame(arb=0x100)))
        self.assertFalse(signal.matches(_frame(arb=0x101)))

    def test_an_any_id_signal_matches_every_id(self):
        signal = S.Signal(can_id=None)
        self.assertTrue(signal.matches(_frame(arb=0x100)))
        self.assertTrue(signal.matches(_frame(arb=0x7FF)))

    def test_extended_and_standard_with_the_same_number_do_not_collide(self):
        signal = S.Signal(can_id=0x100, is_extended=False)
        self.assertFalse(signal.matches(_frame(arb=0x100, ext=True)))

    def test_a_channel_restricted_signal_only_matches_its_channel(self):
        signal = S.Signal(can_id=None, channel="2")
        self.assertFalse(signal.matches(_frame(channel="1")))
        self.assertTrue(signal.matches(_frame(channel="2")))

    def test_an_empty_channel_matches_every_channel(self):
        signal = S.Signal(can_id=None, channel="")
        self.assertTrue(signal.matches(_frame(channel="anything")))

    def test_a_disabled_signal_matches_nothing(self):
        signal = S.Signal(can_id=None, enabled=False)
        self.assertFalse(signal.matches(_frame()))


# ---------------------------------------------------------------------------
# scaling math, reused from cantools — byte order, signedness, float
# ---------------------------------------------------------------------------


class PreviewMathTests(unittest.TestCase):
    def test_unsigned_little_endian(self):
        signal = S.Signal(start=0, length=16, byte_order=S.LITTLE_ENDIAN)
        self.assertEqual(S.preview(signal, (1234).to_bytes(2, "little")), 1234.0)

    def test_unsigned_big_endian(self):
        signal = S.Signal(start=7, length=16, byte_order=S.BIG_ENDIAN)
        self.assertEqual(S.preview(signal, (0x1234).to_bytes(2, "big")), 4660.0)

    def test_signed_negative_value(self):
        signal = S.Signal(start=0, length=16, byte_order=S.LITTLE_ENDIAN,
                          is_signed=True)
        data = (-100 & 0xFFFF).to_bytes(2, "little")
        self.assertEqual(S.preview(signal, data), -100.0)

    def test_scale_and_offset_are_applied(self):
        signal = S.Signal(start=0, length=16, byte_order=S.LITTLE_ENDIAN,
                          scale=0.25, offset=-40.0)
        data = (16383).to_bytes(2, "little")
        self.assertAlmostEqual(S.preview(signal, data), 16383 * 0.25 - 40.0)

    def test_float32_little_endian(self):
        signal = S.Signal(start=0, encoding=S.FLOAT32, byte_order=S.LITTLE_ENDIAN)
        data = struct.pack("<f", 3.25) + b"\x00" * 4
        self.assertAlmostEqual(S.preview(signal, data), 3.25, places=4)

    def test_float64_little_endian(self):
        signal = S.Signal(start=0, encoding=S.FLOAT64, byte_order=S.LITTLE_ENDIAN)
        data = struct.pack("<d", -6.5)
        self.assertEqual(S.preview(signal, data), -6.5)

    def test_a_bit_field_not_at_a_byte_boundary(self):
        # bits 4-6 of byte 0 (a 3-bit field) — exercises genuinely sub-byte
        # extraction, which only a real bit-level model can do at all.
        signal = S.Signal(start=4, length=3, byte_order=S.LITTLE_ENDIAN)
        self.assertEqual(S.preview(signal, bytes([0b0011_0000])), 3.0)

    def test_bcd_reads_packed_decimal_digits(self):
        signal = S.Signal(start=0, length=16, encoding=S.BCD)
        self.assertEqual(S.preview(signal, bytes([0x12, 0x34])), 1234.0)

    def test_bcd_scale_and_offset_apply_after_decoding(self):
        signal = S.Signal(start=0, length=8, encoding=S.BCD, scale=0.1)
        self.assertAlmostEqual(S.preview(signal, bytes([0x37])), 3.7)

    def test_invalid_bcd_nibbles_produce_no_value(self):
        signal = S.Signal(start=0, length=8, encoding=S.BCD)
        self.assertIsNone(S.preview(signal, bytes([0xFA])))

    def test_data_shorter_than_the_field_produces_no_value(self):
        signal = S.Signal(start=0, length=32)
        self.assertIsNone(S.preview(signal, bytes([1, 2])))

    def test_decode_for_frame_respects_matching(self):
        signal = S.Signal(can_id=0x100, start=0, length=8)
        self.assertIsNone(S.decode_for_frame(signal, _frame(arb=0x200, data=b"\x05")))
        self.assertEqual(
            S.decode_for_frame(signal, _frame(arb=0x100, data=b"\x05")), 5.0)

    def test_preview_never_raises_on_nonsense(self):
        signal = S.Signal(start=500, length=200)
        self.assertIsNone(S.preview(signal, b"\x00"))


# ---------------------------------------------------------------------------
# DBC import
# ---------------------------------------------------------------------------


class ImportTests(unittest.TestCase):
    def test_every_message_and_signal_is_read(self):
        profile = S.import_dbc(FIXTURE)
        self.assertEqual(len(profile.signals), 10)

    def test_signals_carry_their_message_context(self):
        profile = S.import_dbc(FIXTURE)
        rpm = next(s for s in profile.signals if s.name == "Rpm")
        self.assertEqual(rpm.can_id, 0x100)
        self.assertEqual(rpm.message_name, "Engine")
        self.assertFalse(rpm.is_extended)

    def test_extended_identifiers_are_preserved(self):
        profile = S.import_dbc(FIXTURE)
        big = next(s for s in profile.signals if s.name == "BigCounter")
        self.assertTrue(big.is_extended)
        self.assertEqual(big.can_id, 0x18FEF1FE)

    def test_a_big_endian_signal_keeps_its_byte_order_and_signedness(self):
        profile = S.import_dbc(FIXTURE)
        torque = next(s for s in profile.signals if s.name == "Torque")
        self.assertEqual(torque.byte_order, S.BIG_ENDIAN)
        self.assertTrue(torque.is_signed)

    def test_a_missing_file_is_a_readable_error(self):
        with self.assertRaises(S.SignalError):
            S.import_dbc(os.path.join(os.path.dirname(FIXTURE), "nope.dbc"))

    def test_message_groups_orders_any_id_last(self):
        profile = S.import_dbc(FIXTURE)
        profile.signals.append(S.Signal(name="Free", can_id=None))
        groups = profile.message_groups()
        self.assertIsNone(groups[-1][0])


# ---------------------------------------------------------------------------
# DBC export
# ---------------------------------------------------------------------------


class ExportTests(_TempDir):
    def test_a_round_tripped_database_decodes_identically(self):
        profile = S.import_dbc(FIXTURE)
        path = self._path("out.dbc")
        written, warnings = S.export_dbc(profile, path)
        self.assertEqual(written, 10)
        self.assertEqual(warnings, [])

        reimported = S.import_dbc(path)
        before = {s.name: (s.can_id, s.start, s.length, s.byte_order,
                           s.is_signed, s.scale, s.offset)
                  for s in profile.signals}
        after = {s.name: (s.can_id, s.start, s.length, s.byte_order,
                          s.is_signed, s.scale, s.offset)
                 for s in reimported.signals}
        self.assertEqual(before, after)

    def test_a_float_signal_round_trips(self):
        profile = S.Profile(name="f.dbc")
        profile.signals.append(S.Signal(
            name="Temp", can_id=0x200, start=0, encoding=S.FLOAT32, unit="C"))
        path = self._path("f.dbc")
        S.export_dbc(profile, path)
        reimported = S.import_dbc(path)
        self.assertEqual(reimported.signals[0].encoding, S.FLOAT32)

    def test_an_any_id_signal_is_excluded_and_warned_about(self):
        profile = S.Profile()
        profile.signals.append(S.Signal(name="Bound", can_id=0x100, start=0, length=8))
        profile.signals.append(S.Signal(name="Free", can_id=None, start=0, length=8))
        path = self._path("out.dbc")
        written, warnings = S.export_dbc(profile, path)
        self.assertEqual(written, 1)
        self.assertTrue(any("Free" in w for w in warnings))
        self.assertTrue(any("any ID" in w for w in warnings))

    def test_a_bcd_signal_is_excluded_and_warned_about(self):
        profile = S.Profile()
        profile.signals.append(S.Signal(name="Digits", can_id=0x300,
                                        start=0, length=16, encoding=S.BCD))
        path = self._path("out.dbc")
        written, warnings = S.export_dbc(profile, path)
        self.assertEqual(written, 0)
        self.assertTrue(any("BCD" in w for w in warnings))

    def test_a_channel_restricted_signal_exports_but_is_warned_about(self):
        profile = S.Profile()
        profile.signals.append(S.Signal(name="Ch2Only", can_id=0x100,
                                        start=0, length=8, channel="2"))
        path = self._path("out.dbc")
        written, warnings = S.export_dbc(profile, path)
        self.assertEqual(written, 1)
        self.assertTrue(any("channel" in w.lower() for w in warnings))

    def test_writing_nothing_produces_no_warnings_for_an_empty_profile(self):
        path = self._path("empty.dbc")
        written, warnings = S.export_dbc(S.Profile(), path)
        self.assertEqual((written, warnings), (0, []))

    def test_invalid_signals_are_refused_before_writing(self):
        profile = S.Profile()
        profile.signals.append(S.Signal(name="Zero", can_id=0x100, scale=0.0))
        path = self._path("out.dbc")
        with self.assertRaises(S.SignalError):
            S.export_dbc(profile, path)
        self.assertFalse(os.path.exists(path))

    def test_missing_directories_are_created(self):
        profile = S.Profile()
        profile.signals.append(S.Signal(name="X", can_id=0x100, start=0, length=8))
        path = os.path.join(self._dir, "a", "b", "out.dbc")
        S.export_dbc(profile, path)
        self.assertTrue(os.path.exists(path))


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


class ValidationTests(unittest.TestCase):
    def test_a_sound_profile_has_no_problems(self):
        profile = S.Profile()
        profile.signals.append(S.Signal(name="X", can_id=0x100, start=0, length=8))
        self.assertEqual(profile.validate(), [])

    def test_a_zero_scale_is_flagged(self):
        signal = S.Signal(name="X", scale=0.0)
        self.assertTrue(any("scale of 0" in p for p in S.validate_signal(signal)))

    def test_an_inverted_range_is_flagged(self):
        signal = S.Signal(name="X", minimum=10.0, maximum=1.0)
        self.assertTrue(any("minimum is above maximum" in p
                            for p in S.validate_signal(signal)))

    def test_bcd_without_byte_alignment_is_flagged(self):
        signal = S.Signal(name="X", encoding=S.BCD, start=3, length=8)
        self.assertTrue(any("byte-aligned" in p for p in S.validate_signal(signal)))

    def test_duplicate_names_on_the_same_id_are_flagged(self):
        profile = S.Profile()
        profile.signals.append(S.Signal(name="X", can_id=0x100, start=0, length=8))
        profile.signals.append(S.Signal(name="X", can_id=0x100, start=8, length=8))
        self.assertTrue(any("share the name" in p for p in profile.validate()))

    def test_the_same_name_on_different_ids_is_not_a_conflict(self):
        profile = S.Profile()
        profile.signals.append(S.Signal(name="X", can_id=0x100, start=0, length=8))
        profile.signals.append(S.Signal(name="X", can_id=0x200, start=0, length=8))
        self.assertEqual(profile.validate(), [])


# ---------------------------------------------------------------------------
# apply artifact: the one DbcDatabase a profile builds
# ---------------------------------------------------------------------------


class ApplyTests(unittest.TestCase):
    def test_build_dbc_database_decodes_like_the_original(self):
        profile = S.import_dbc(FIXTURE)
        db = S.build_dbc_database(profile)
        self.assertIsInstance(db, DbcDatabase)
        frame = _frame(arb=0x100, data=bytes([0x00, 0x00, 0x19, 0xFF, 0x38, 0, 0, 0]))
        decoded = db.decode(frame)
        self.assertTrue(decoded.ok)
        names = {s.name for s in decoded.signals}
        self.assertEqual(names, {"Rpm", "CoolantTemp", "Torque", "Gear"})

    def test_an_empty_profile_builds_no_database(self):
        self.assertIsNone(S.build_dbc_database(S.Profile()))

    def test_a_profile_with_only_any_id_signals_builds_no_database(self):
        """"Any ID" signals have no message to build a database from at all."""
        profile = S.Profile()
        profile.signals.append(S.Signal(name="Free", can_id=None, start=0, length=8))
        self.assertIsNone(S.build_dbc_database(profile))

    def test_a_disabled_signal_is_not_in_the_built_database(self):
        profile = S.Profile()
        profile.signals.append(S.Signal(name="On", can_id=0x100, start=0, length=8))
        profile.signals.append(S.Signal(name="Off", can_id=0x100, start=8,
                                        length=8, enabled=False))
        db = S.build_dbc_database(profile)
        decoded = db.decode(_frame(arb=0x100, data=bytes(8)))
        names = {s.name for s in decoded.signals}
        self.assertIn("On", names)
        self.assertNotIn("Off", names)

    def test_a_bcd_signal_is_not_in_the_built_database(self):
        """BCD has no bit-precise DBC representation, decode path included."""
        profile = S.Profile()
        profile.signals.append(S.Signal(name="Real", can_id=0x100, start=0, length=8))
        profile.signals.append(S.Signal(name="Digits", can_id=0x100, start=8,
                                        length=8, encoding=S.BCD))
        db = S.build_dbc_database(profile)
        decoded = db.decode(_frame(arb=0x100, data=bytes(8)))
        names = {s.name for s in decoded.signals}
        self.assertIn("Real", names)
        self.assertNotIn("Digits", names)

    def test_a_non_byte_aligned_signal_still_builds_and_decodes(self):
        """Bit-precise signals never needed byte alignment for the DBC path —
        only the old, now-removed Blocks-table column did."""
        profile = S.Profile()
        profile.signals.append(S.Signal(name="Flag", can_id=0x100, start=4, length=3))
        db = S.build_dbc_database(profile)
        decoded = db.decode(_frame(arb=0x100, data=bytes([0b0011_0000])))
        self.assertTrue(decoded.ok)
        self.assertEqual(decoded.signals[0].value, 3)


# ---------------------------------------------------------------------------
# ProfileStore
# ---------------------------------------------------------------------------


class ProfileStoreTests(unittest.TestCase):
    def test_add_gives_a_unique_name_on_collision(self):
        store = S.ProfileStore()
        store.add(S.Profile(name="a.dbc"))
        second = store.add(S.Profile(name="a.dbc"))
        self.assertEqual(second.name, "a (2).dbc")
        self.assertIsNone(store.find("a.dbc (2)"))

    def test_remove_clears_the_active_pointer_if_it_was_active(self):
        store = S.ProfileStore()
        store.add(S.Profile(name="a.dbc"))
        store.active = "a.dbc"
        store.remove("a.dbc")
        self.assertIsNone(store.active)
        self.assertIsNone(store.active_profile)

    def test_removing_a_profile_never_touches_the_file_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "kept.dbc")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("VERSION \"\"\n")
            store = S.ProfileStore()
            store.add(S.Profile(name="kept.dbc", path=path))
            store.remove("kept.dbc")
            self.assertTrue(os.path.exists(path))

    def test_round_trips_through_config_dict(self):
        store = S.ProfileStore()
        profile = store.add(S.Profile(name="a.dbc"))
        profile.signals.append(S.Signal(name="X", can_id=0x100, start=0, length=8))
        store.active = "a.dbc"

        again = S.ProfileStore.from_config(store.to_config())
        self.assertEqual(again.active, "a.dbc")
        self.assertEqual(len(again.profiles), 1)
        self.assertEqual(again.profiles[0].signals[0].name, "X")

    def test_a_stale_active_name_is_dropped_on_load(self):
        store = S.ProfileStore.from_config(
            {"profiles": [{"name": "a.dbc", "signals": []}], "active": "gone.dbc"})
        self.assertIsNone(store.active)

    def test_find_returns_none_for_an_unknown_name(self):
        self.assertIsNone(S.ProfileStore().find("nope"))


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


class MigrationTests(unittest.TestCase):
    def test_a_previously_loaded_dbc_becomes_the_active_profile(self):
        store, _notes = S.ProfileStore.migrate_legacy(FIXTURE, [])
        self.assertEqual(store.active, "sample.dbc")
        self.assertEqual(len(store.active_profile.signals), 10)

    def test_scaled_value_rules_become_their_own_profile(self):
        rules = [{"name": "R", "can_id": "0x100", "offset": 0, "length": 2,
                 "decoder": "u16_be", "scale": 1.0, "add": 0.0}]
        store, _notes = S.ProfileStore.migrate_legacy("", rules)
        profile = store.find("Scaled values")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.signals[0].name, "R")
        self.assertEqual(profile.signals[0].can_id, 0x100)

    def test_scaled_value_rules_become_active_when_there_is_no_dbc(self):
        rules = [{"name": "R", "can_id": None, "offset": 0, "length": 1,
                 "decoder": "u8", "scale": 1.0, "add": 0.0}]
        store, _notes = S.ProfileStore.migrate_legacy("", rules)
        self.assertEqual(store.active, "Scaled values")

    def test_every_numeric_decoder_the_old_editor_offered_migrates(self):
        from cansniff.interpret import numeric_decoder_keys
        offered = set(numeric_decoder_keys())
        covered = set(S._LEGACY_DECODER_MAP)
        missing = offered - covered
        self.assertEqual(missing, set(),
                         "a decoder the old Scaled Values editor could select "
                         "has no migration mapping: {}".format(missing))

    def test_scale_and_offset_survive_migration_exactly(self):
        rules = [{"name": "R", "can_id": "0x100", "offset": 0, "length": 2,
                 "decoder": "u16_be", "scale": 0.0078125, "add": -273.15}]
        store, _notes = S.ProfileStore.migrate_legacy("", rules)
        signal = store.find("Scaled values").signals[0]
        self.assertEqual(signal.scale, 0.0078125)
        self.assertEqual(signal.offset, -273.15)

    def test_the_enabled_flag_survives_migration(self):
        rules = [{"name": "R", "can_id": None, "offset": 0, "length": 1,
                 "decoder": "u8", "enabled": False}]
        store, _notes = S.ProfileStore.migrate_legacy("", rules)
        self.assertFalse(store.find("Scaled values").signals[0].enabled)

    def test_a_decoder_with_no_equivalent_is_reported_not_silently_dropped(self):
        rules = [{"name": "Text", "can_id": None, "offset": 0, "length": 4,
                 "decoder": "ascii"}]
        store, notes = S.ProfileStore.migrate_legacy("", rules)
        self.assertIsNone(store.find("Scaled values"))
        self.assertTrue(any("Text" in n for n in notes))

    def test_a_missing_dbc_path_produces_no_dbc_profile(self):
        store, notes = S.ProfileStore.migrate_legacy("/no/such/file.dbc", [])
        self.assertEqual(store.profiles, [])
        self.assertIsNone(store.active)

    def test_no_legacy_data_produces_an_empty_store(self):
        store, notes = S.ProfileStore.migrate_legacy("", [])
        self.assertEqual(store.profiles, [])
        self.assertIsNone(store.active)
        self.assertEqual(notes, [])

    def test_both_a_dbc_and_scaled_values_migrate_side_by_side(self):
        rules = [{"name": "R", "can_id": None, "offset": 0, "length": 1,
                 "decoder": "u8"}]
        store, notes = S.ProfileStore.migrate_legacy(FIXTURE, rules)
        self.assertEqual(len(store.profiles), 2)
        self.assertEqual(store.active, "sample.dbc")
        self.assertTrue(any("Scaled values" in n for n in notes))

    def test_migrated_rules_decode_the_same_value_as_before(self):
        """The whole point of migration: old config keeps meaning the same thing.

        The old rule's own formula (raw * scale + add, raw read by the old
        decoder's own math) is recomputed directly here rather than through
        the now-removed SignalRule class, since that class is what is being
        proven redundant.
        """
        from cansniff.interpret import DECODERS
        rule = {"name": "Rpm", "can_id": "0x100", "channel": "", "offset": 0,
               "length": 2, "decoder": "u16_be", "scale": 0.25, "add": 0.0,
               "unit": "rpm", "precision": 1}
        data = (16383).to_bytes(2, "big")
        old_value = DECODERS["u16_be"].value(data) * rule["scale"] + rule["add"]

        store, _notes = S.ProfileStore.migrate_legacy("", [rule])
        signal = store.find("Scaled values").signals[0]
        new_value = S.preview(signal, data)
        self.assertAlmostEqual(new_value, old_value)

    def test_migrated_big_endian_rules_read_the_correct_bytes(self):
        """Regression: DBC's Motorola numbering is not byte_offset*8.

        A naive migration silently read the wrong bits for every big-endian
        (u16_be/i16_be/u32_be/... /hex_be) rule at a nonzero byte offset —
        the values would have been quietly wrong, not merely unmigrated.
        """
        rule = {"name": "T", "can_id": "0x100", "offset": 2, "length": 2,
               "decoder": "u16_be", "scale": 1.0, "add": 0.0}
        store, _notes = S.ProfileStore.migrate_legacy("", [rule])
        signal = store.find("Scaled values").signals[0]

        data = bytearray(8)
        data[2:4] = (0xBEEF).to_bytes(2, "big")
        self.assertEqual(S.preview(signal, bytes(data)), float(0xBEEF))
        self.assertTrue(signal.byte_aligned,
                        "a byte-aligned legacy rule must migrate byte-aligned")

    def test_migrated_big_endian_rules_decode_through_a_built_database(self):
        """The migrated signal must still work end to end: through
        build_dbc_database, exactly as an ordinary imported signal would."""
        rule = {"name": "T", "can_id": "0x100", "offset": 3, "length": 2,
               "decoder": "u16_be", "scale": 1.0, "add": 0.0}
        store, _notes = S.ProfileStore.migrate_legacy("", [rule])
        profile = store.find("Scaled values")
        db = S.build_dbc_database(profile)
        self.assertIsNotNone(db)
        data = bytearray(8)
        data[3:5] = (0xBEEF).to_bytes(2, "big")
        decoded = db.decode(_frame(arb=0x100, data=bytes(data)))
        self.assertTrue(decoded.ok)
        self.assertEqual(decoded.signals[0].value, float(0xBEEF))


# ---------------------------------------------------------------------------
# raw CAN preservation
# ---------------------------------------------------------------------------


class RawFramePreservationTests(unittest.TestCase):
    def test_preview_never_mutates_the_data_it_is_given(self):
        signal = S.Signal(start=0, length=16)
        data = bytearray([1, 2, 3, 4])
        before = bytes(data)
        S.preview(signal, data)
        self.assertEqual(bytes(data), before)

    def test_decode_for_frame_never_mutates_the_frame(self):
        signal = S.Signal(can_id=0x100, start=0, length=8)
        frame = _frame(arb=0x100, data=b"\x2A")
        before = (frame.timestamp, frame.arb_id, bytes(frame.data), frame.dlc,
                  frame.channel, frame.is_extended)
        S.decode_for_frame(signal, frame)
        after = (frame.timestamp, frame.arb_id, bytes(frame.data), frame.dlc,
                 frame.channel, frame.is_extended)
        self.assertEqual(before, after)

    def test_building_a_database_never_touches_the_profile_signals(self):
        profile = S.import_dbc(FIXTURE)
        before = [s.to_dict() for s in profile.signals]
        S.build_dbc_database(profile)
        after = [s.to_dict() for s in profile.signals]
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# containment: no encode/transmit surface
# ---------------------------------------------------------------------------


class ContainmentTests(unittest.TestCase):
    def test_the_module_never_calls_an_encode_or_send_api(self):
        with open(S.__file__, encoding="utf-8") as handle:
            source = handle.read()
        code = source.split('"""', 2)[-1]
        for forbidden in ("encode(", "encode_message", ".send(", "can.Bus("):
            self.assertNotIn(forbidden, code, forbidden)

    def test_the_dbc_database_wrapper_keeps_the_library_object_private(self):
        profile = S.import_dbc(FIXTURE)
        db = S.build_dbc_database(profile)
        public = [n for n in dir(db) if not n.startswith("_")]
        self.assertNotIn("db", public)
        self.assertNotIn("database", public)


if __name__ == "__main__":
    unittest.main()
