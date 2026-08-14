"""Source tests.

These exercise the ASC parser and offline playback against the checked-in
capture, and assert that the live source refuses interfaces whose listen-only
mode cannot be confirmed. No CAN interface is opened.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.config import Config  # noqa: E402
from cansniff.sources import SourceError, build_source  # noqa: E402
from cansniff.sources.asc_reader import parse_asc  # noqa: E402
from cansniff.sources.file_source import FileSource  # noqa: E402
from cansniff.sources.live import LiveSource, listen_only_support  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(PROJECT_ROOT, "baseline.asc")


@unittest.skipUnless(os.path.exists(BASELINE), "baseline.asc not present")
class AscParserTests(unittest.TestCase):
    def test_parses_expected_ids(self):
        result = parse_asc(BASELINE)
        self.assertGreater(len(result.frames), 0)
        ids = {frame.arb_id for frame in result.frames}
        self.assertEqual(ids, {0x100, 0x101})

    def test_first_frame_fields(self):
        first = parse_asc(BASELINE).frames[0]
        self.assertEqual(first.arb_id, 0x100)
        self.assertEqual(first.dlc, 3)
        self.assertEqual(first.data, bytes.fromhex("010001"))
        self.assertEqual(first.channel, "1")
        self.assertFalse(first.is_extended)
        self.assertAlmostEqual(first.timestamp, 0.0)

    def test_payload_length_matches_dlc(self):
        for frame in parse_asc(BASELINE).frames:
            self.assertEqual(len(frame.data), frame.dlc)

    def test_header_lines_are_not_frames(self):
        result = parse_asc(BASELINE)
        self.assertEqual(result.base, "hex")
        self.assertTrue(all(frame.raw_line for frame in result.frames))


class AscParserSyntheticTests(unittest.TestCase):
    def _write(self, text: str) -> str:
        path = os.path.join(
            os.environ.get("TEMP", "."), "cansniff_test_{}.asc".format(os.getpid())
        )
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_extended_id_suffix(self):
        path = self._write(
            "base hex  timestamps absolute\n"
            "   0.100000 1  18FEF100x       Rx   d 8 01 02 03 04 05 06 07 08\n"
        )
        frames = parse_asc(path).frames
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].arb_id, 0x18FEF100)
        self.assertTrue(frames[0].is_extended)

    def test_remote_frame_has_no_payload(self):
        path = self._write("   0.2 1  200             Rx   r 8\n")
        frames = parse_asc(path).frames
        self.assertTrue(frames[0].is_remote_frame)
        self.assertEqual(frames[0].data, b"")

    def test_error_frame_recognised(self):
        path = self._write("   0.3 1  ErrorFrame\n")
        frames = parse_asc(path).frames
        self.assertTrue(frames[0].is_error_frame)

    def test_fd_frame_reports_dlc_as_a_byte_count(self):
        """CanFrame.dlc is a byte count everywhere, including CAN FD.

        Regression: the FD branch stored the 0-15 DLC code instead, so the
        same 64-byte frame reported 15 from a file and 64 from hardware. That
        lit the "DLC" anomaly chip on every FD frame loaded from a capture and
        made the capture filters' byte-range bounds mean two different things
        depending on where the frame came from.
        """
        payload = " ".join("{:02X}".format(i) for i in range(64))
        path = self._write(
            "base hex  timestamps absolute\n"
            "   0.5 CANFD 1  Rx   500  sig 1 0 F 64 {}\n".format(payload)
        )
        frames = parse_asc(path).frames
        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertTrue(frame.is_fd)
        self.assertEqual(len(frame.data), 64)
        self.assertEqual(frame.dlc, 64, "dlc must be the byte count, not code 15")
        self.assertTrue(frame.is_bitrate_switch)

    def test_fd_quantised_sizes_all_report_their_byte_count(self):
        for code, length in ((0x9, 12), (0xA, 16), (0xB, 20), (0xC, 24),
                             (0xD, 32), (0xE, 48), (0xF, 64)):
            payload = " ".join("00" for _ in range(length))
            path = self._write(
                "base hex  timestamps absolute\n"
                "   0.1 CANFD 1  Rx   300  sig 0 0 {:X} {} {}\n".format(
                    code, length, payload)
            )
            frame = parse_asc(path).frames[0]
            self.assertEqual(frame.dlc, length)
            self.assertEqual(len(frame.data), length)

    def test_classic_frame_dlc_still_matches_its_payload(self):
        path = self._write("   0.6 1  100             Rx   d 3 01 00 01\n")
        frame = parse_asc(path).frames[0]
        self.assertEqual(frame.dlc, 3)
        self.assertEqual(frame.dlc, len(frame.data))

    def test_unparsable_line_is_counted_not_guessed(self):
        path = self._write(
            "   0.4 1  100             Rx   d 3 01 00 01\n"
            "   0.5 1  ZZZZ            Rx   d 1 00\n"
        )
        result = parse_asc(path)
        self.assertEqual(len(result.frames), 1)
        self.assertEqual(result.skipped, 1)


@unittest.skipUnless(os.path.exists(BASELINE), "baseline.asc not present")
class FileSourceTests(unittest.TestCase):
    def test_playback_yields_all_frames_then_reports_exhausted(self):
        source = FileSource(BASELINE, speed=0.0)
        source.open()
        try:
            count = 0
            while True:
                frame = source.receive(timeout=0.0)
                if frame is None:
                    break
                count += 1
            self.assertEqual(count, source.total_frames)
            self.assertTrue(source.exhausted)
        finally:
            source.close()

    def test_missing_file_raises_source_error(self):
        source = FileSource(os.path.join(PROJECT_ROOT, "does-not-exist.asc"))
        with self.assertRaises(SourceError):
            source.open()

    def test_has_no_transmit_api(self):
        source = FileSource(BASELINE)
        for name in ("send", "write", "transmit", "tx", "inject", "replay"):
            self.assertFalse(hasattr(source, name), "unexpected {} attribute".format(name))


class LiveSourceSafetyTests(unittest.TestCase):
    def test_unknown_interface_is_refused_by_default(self):
        source = LiveSource({"interface": "vector", "channel": "0"})
        with self.assertRaises(SourceError) as ctx:
            source.open()
        self.assertIn("listen-only", str(ctx.exception))

    def test_override_flag_allows_but_marks_unverified(self):
        source = LiveSource({
            "interface": "vector", "channel": "0", "require_listen_only": False,
        })
        note = source._preflight()
        self.assertTrue(note.startswith("NOT VERIFIED"))

    def test_socketcan_requires_confirmed_listen_only(self):
        source = LiveSource({"interface": "socketcan", "channel": "can0"})
        with self.assertRaises(SourceError) as ctx:
            source.open()
        self.assertIn("listen-only", str(ctx.exception))

    def test_virtual_interface_is_considered_passive(self):
        source = LiveSource({"interface": "virtual", "channel": "0"})
        self.assertTrue(source._preflight().startswith("passive"))
        self.assertTrue(source.passive_verified)

    def test_support_table(self):
        self.assertEqual(listen_only_support("kvaser"), "enforced-at-init")
        self.assertEqual(listen_only_support("pcan"), "enforced-after-init")
        self.assertEqual(listen_only_support("socketcan"), "external-configuration")
        self.assertEqual(listen_only_support("made-up"), "unsupported")

    def test_has_no_transmit_api(self):
        source = LiveSource({"interface": "virtual"})
        for name in ("send", "write", "transmit", "tx", "inject", "replay"):
            self.assertFalse(hasattr(source, name), "unexpected {} attribute".format(name))


class BuildSourceTests(unittest.TestCase):
    def test_unknown_type_rejected(self):
        config = Config.defaults()
        config.set("source.type", "bus-replay")
        with self.assertRaises(SourceError):
            build_source(config)

    def test_file_type_builds_file_source(self):
        config = Config.defaults()
        config.set("source.type", "file")
        config.set("source.file.path", BASELINE)
        self.assertIsInstance(build_source(config), FileSource)


if __name__ == "__main__":
    unittest.main()
