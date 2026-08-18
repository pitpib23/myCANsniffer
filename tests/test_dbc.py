"""Tests for read-only DBC decoding.

Frames are constructed in-process and decoded against a fixture database.
Nothing here opens a CAN interface, and no encode path is exercised because
none is exposed.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis import dbc as dbc_module  # noqa: E402
from cansniff.analysis.dbc import (  # noqa: E402
    DECODE_FAILED, LENGTH_MISMATCH, NOT_IN_DATABASE, OK, DbcDatabase, DbcError,
)
from cansniff.model import CanFrame  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SAMPLE = os.path.join(FIXTURES, "sample.dbc")

try:
    import cantools  # noqa: F401
    HAVE_CANTOOLS = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_CANTOOLS = False


def _frame(arb, data, ext=False, **kw):
    return CanFrame(timestamp=kw.pop("t", 0.0), arb_id=arb, data=bytes(data),
                    dlc=len(data), channel="1", is_extended=ext, **kw)


def _engine():
    # Rpm 8000 raw (LE) -> 2000 rpm; CoolantTemp raw 105 -> 65 degC;
    # Torque big-endian signed; Gear 15 -> "Reverse".
    return _frame(0x100, [0x40, 0x1F, 0x69, 0x01, 0x90, 0x0F, 0x00, 0x00])


@unittest.skipUnless(HAVE_CANTOOLS, "cantools not installed")
class DbcLoadTests(unittest.TestCase):
    def test_loads_and_describes(self):
        db = DbcDatabase.load(SAMPLE)
        self.assertEqual(db.message_count, 4)
        self.assertEqual(db.signal_count, 10)
        self.assertIn("sample.dbc", db.describe())

    def test_missing_file_reports_cleanly(self):
        with self.assertRaises(DbcError) as ctx:
            DbcDatabase.load(os.path.join(FIXTURES, "nope.dbc"))
        self.assertIn("No such file", str(ctx.exception))

    def test_invalid_file_reports_cleanly_without_crashing(self):
        path = os.path.join(os.environ.get("TEMP", "."), "cansniff_bad.dbc")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("this is not a dbc\nBO_ nonsense (((\n")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with self.assertRaises(DbcError):
            DbcDatabase.load(path)

    def test_no_encode_surface_is_exposed(self):
        """cantools can build frames; that half must not reach callers."""
        db = DbcDatabase.load(SAMPLE)
        for name in ("encode", "encode_message", "send", "transmit", "write"):
            self.assertFalse(hasattr(db, name),
                             "{} must not be exposed on the database".format(name))
        public = [n for n in dir(db) if not n.startswith("_")]
        self.assertNotIn("database", public)


@unittest.skipUnless(HAVE_CANTOOLS, "cantools not installed")
class DbcDecodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = DbcDatabase.load(SAMPLE)

    def _by_name(self, decoded):
        return {s.name: s for s in decoded.signals}

    def test_little_endian_unsigned_with_scale(self):
        signals = self._by_name(self.db.decode(_engine()))
        self.assertAlmostEqual(signals["Rpm"].value, 2000.0)
        self.assertEqual(signals["Rpm"].unit, "rpm")
        self.assertEqual(signals["Rpm"].raw, 8000)

    def test_offset_is_applied(self):
        signals = self._by_name(self.db.decode(_engine()))
        self.assertAlmostEqual(signals["CoolantTemp"].value, 65.0)
        self.assertEqual(signals["CoolantTemp"].unit, "degC")

    def test_big_endian_signed_signal(self):
        signals = self._by_name(self.db.decode(_engine()))
        self.assertLess(signals["Torque"].value, 0, "signal is signed big-endian")
        self.assertEqual(signals["Torque"].unit, "Nm")

    def test_enumerated_choice_text(self):
        signals = self._by_name(self.db.decode(_engine()))
        self.assertEqual(signals["Gear"].choice, "Reverse")
        self.assertEqual(signals["Gear"].display, "Reverse")
        self.assertEqual(signals["Gear"].value, 15)

    def test_choice_on_a_second_message(self):
        signals = self._by_name(self.db.decode(_frame(0x101, [0x06, 0, 0, 0])))
        self.assertEqual(signals["Mode"].choice, "Running")
        self.assertEqual(signals["Ready"].value, 1)

    def test_extended_identifier_message(self):
        decoded = self.db.decode(
            _frame(0x18FEF1FE, [1, 2, 3, 4, 0, 0, 0, 0], ext=True))
        self.assertEqual(decoded.status, OK)
        self.assertEqual(decoded.name, "Extended")
        self.assertEqual(self._by_name(decoded)["BigCounter"].value, 0x04030201)

    def test_standard_and_extended_ids_do_not_collide(self):
        """0x100 standard is in the database; 0x100 extended is not."""
        self.assertEqual(self.db.decode(_frame(0x100, [0] * 8)).status, OK)
        self.assertEqual(
            self.db.decode(_frame(0x100, [0] * 8, ext=True)).status,
            NOT_IN_DATABASE)

    def test_multiplexed_selector_zero(self):
        decoded = self.db.decode(_frame(0x200, [0x00, 0xE8, 0x03, 0, 0, 0, 0, 0]))
        names = set(self._by_name(decoded))
        self.assertEqual(decoded.status, OK)
        self.assertIn("TempA", names)
        self.assertNotIn("VoltB", names, "other multiplex branch must be absent")

    def test_multiplexed_selector_one(self):
        decoded = self.db.decode(_frame(0x200, [0x01, 0xE8, 0x03, 0, 0, 0, 0, 0]))
        names = set(self._by_name(decoded))
        self.assertIn("VoltB", names)
        self.assertNotIn("TempA", names)

    def test_invalid_multiplexor_degrades_not_crashes(self):
        decoded = self.db.decode(_frame(0x200, [0x7F, 0, 0, 0, 0, 0, 0, 0]))
        self.assertIn(decoded.status, (DECODE_FAILED, OK))
        self.assertTrue(decoded.known, "the ID is still known to the database")

    def test_unknown_id_is_reported_not_guessed(self):
        decoded = self.db.decode(_frame(0x7FF, [0]))
        self.assertEqual(decoded.status, NOT_IN_DATABASE)
        self.assertEqual(decoded.signals, [])
        self.assertFalse(decoded.known)

    def test_short_frame_is_flagged_not_decoded(self):
        decoded = self.db.decode(_frame(0x100, [0x00, 0x01]))
        self.assertEqual(decoded.status, LENGTH_MISMATCH)
        self.assertEqual(decoded.signals, [])
        self.assertIn("expects 8", decoded.detail)
        self.assertTrue(decoded.known)

    def test_payloadless_frames_are_not_decode_failures(self):
        error = CanFrame(timestamp=0.0, arb_id=0x100, data=b"", dlc=0,
                         channel="1", is_error_frame=True)
        decoded = self.db.decode(error)
        self.assertEqual(decoded.status, LENGTH_MISMATCH)
        self.assertIn("no payload", decoded.detail)

    def test_message_metadata_is_carried_through(self):
        decoded = self.db.decode(_engine())
        self.assertEqual(decoded.name, "Engine")
        self.assertEqual(decoded.expected_length, 8)
        self.assertIn("Engine operating", decoded.comment)

    def test_signal_order_follows_the_database(self):
        decoded = self.db.decode(_engine())
        self.assertEqual([s.name for s in decoded.signals],
                         ["Rpm", "CoolantTemp", "Torque", "Gear"])

    def test_raw_frame_is_untouched_by_decoding(self):
        """Decoding is a presentation layer; the frame must survive intact."""
        frame = _engine()
        before = (frame.timestamp, frame.arb_id, frame.data, frame.dlc,
                  frame.channel, frame.is_extended)
        self.db.decode(frame)
        after = (frame.timestamp, frame.arb_id, frame.data, frame.dlc,
                 frame.channel, frame.is_extended)
        self.assertEqual(before, after)

    def test_signals_of_lists_names_without_decoding(self):
        self.assertEqual(self.db.signals_of(_frame(0x101, [0] * 4)),
                         ["Mode", "Ready"])
        self.assertEqual(self.db.signals_of(_frame(0x7FF, [0])), [])

    def test_decode_never_raises_on_arbitrary_payloads(self):
        for value in range(0, 256, 17):
            for arb in (0x100, 0x101, 0x200, 0x7FF):
                decoded = self.db.decode(_frame(arb, [value] * 8))
                self.assertIn(decoded.status,
                              (OK, NOT_IN_DATABASE, LENGTH_MISMATCH, DECODE_FAILED))


class DbcWithoutCantoolsTests(unittest.TestCase):
    def test_missing_dependency_is_reported_as_a_readable_error(self):
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
            else __builtins__.__import__

        def blocked(name, *args, **kwargs):
            if name.startswith("cantools"):
                raise ImportError("blocked for test")
            return real_import(name, *args, **kwargs)

        import builtins
        builtins.__import__ = blocked
        try:
            with self.assertRaises(DbcError) as ctx:
                dbc_module.DbcDatabase.load(SAMPLE)
            self.assertIn("cantools is not installed", str(ctx.exception))
        finally:
            builtins.__import__ = real_import


if __name__ == "__main__":
    unittest.main()
