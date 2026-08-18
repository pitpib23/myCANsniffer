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
from cansniff.sources.candump_reader import parse_candump  # noqa: E402
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

    def test_fd_line_without_a_message_name_is_parsed(self):
        """The ASC FD name field is optional; python-can's writer omits it.

        Regression: assuming it was always present shifted every later field by
        one, and a 64-byte frame read back as carrying zero bytes — silently,
        because the shifted length field still parsed as a number.
        """
        payload = " ".join("{:02X}".format(i) for i in range(64))
        path = self._write(
            "base hex  timestamps absolute\n"
            "  0.000000 CANFD   2 Rx        123    1 0 f 64 {}"
            "        0    0     3000        0        0        0\n".format(payload)
        )
        frames = parse_asc(path).frames
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].is_fd)
        self.assertEqual(len(frames[0].data), 64)
        self.assertEqual(frames[0].dlc, 64)
        self.assertTrue(frames[0].is_bitrate_switch)

    def test_fd_line_with_a_message_name_is_still_parsed(self):
        payload = " ".join("{:02X}".format(i) for i in range(8))
        path = self._write(
            "base hex  timestamps absolute\n"
            "  0.100000 CANFD   1 Rx   200  EngineData 1 0 8 8 {}\n".format(payload)
        )
        frames = parse_asc(path).frames
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].arb_id, 0x200)
        self.assertEqual(frames[0].data, bytes(range(8)))

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


class CandumpParserTests(unittest.TestCase):
    """The SocketCAN candump (.log) reader — in-house, not python-can's
    CanutilsLogReader, specifically so it tolerates a trailing field that
    reader does not. See candump_reader.py.
    """

    def _write(self, text: str) -> str:
        path = os.path.join(
            os.environ.get("TEMP", "."), "cansniff_test_{}.log".format(os.getpid())
        )
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_classic_frame(self):
        path = self._write("(1698232894.363802) can0 153#208010FF00FFB05E\n")
        frames = parse_candump(path).frames
        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertAlmostEqual(frame.timestamp, 1698232894.363802)
        self.assertEqual(frame.arb_id, 0x153)
        self.assertEqual(frame.data, bytes.fromhex("208010FF00FFB05E"))
        self.assertEqual(frame.dlc, 8)
        self.assertEqual(frame.channel, "can0")
        self.assertFalse(frame.is_extended)

    def test_extended_id_recognised_by_width(self):
        path = self._write("(0.0) can0 18FEF100#0102030405060708\n")
        frame = parse_candump(path).frames[0]
        self.assertEqual(frame.arb_id, 0x18FEF100)
        self.assertTrue(frame.is_extended)

    def test_remote_frame_has_no_payload(self):
        path = self._write("(0.0) can0 200#R8\n")
        frame = parse_candump(path).frames[0]
        self.assertTrue(frame.is_remote_frame)
        self.assertEqual(frame.data, b"")
        self.assertEqual(frame.dlc, 8)

    def test_error_frame_recognised(self):
        # CAN_ERR_FLAG (0x20000000) | CAN_ERR_BUSERROR (0x80), per SocketCAN.
        path = self._write("(0.0) can0 20000080#0000000000000000\n")
        frame = parse_candump(path).frames[0]
        self.assertTrue(frame.is_error_frame)

    def test_canfd_frame_reports_dlc_as_byte_count_and_brs(self):
        payload = "".join("{:02X}".format(i) for i in range(20))
        path = self._write("(0.0) can0 300##1{}\n".format(payload))
        frame = parse_candump(path).frames[0]
        self.assertTrue(frame.is_fd)
        self.assertTrue(frame.is_bitrate_switch)
        self.assertEqual(len(frame.data), 20)
        self.assertEqual(frame.dlc, 20)

    def test_a_trailing_rx_tx_flag_is_tolerated(self):
        """The one trailing case python-can's own reader already handles —
        must keep working, not just the new one below."""
        path = self._write("(0.0) can0 100#0102 R\n")
        frames = parse_candump(path).frames
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].arb_id, 0x100)

    def test_an_extra_trailing_label_does_not_break_parsing(self):
        """Regression: this is the exact shape of the bug report — a real
        published attack-capture dataset appends a plain 0/1 label after
        id#data on every line, which python-can's CanutilsLogReader cannot
        tolerate (ValueError: too many values to unpack, expected 3) since
        it only special-cases a trailing " r"/" t" rx/tx flag and otherwise
        demands the line split into exactly 3 tokens.
        """
        path = self._write(
            "(1698232894.363802) can0 153#208010FF00FFB05E 0\n"
            "(1698232894.363804) can0 160#00E7FF0A00000012 0\n"
            "(1698232894.363806) can0 164#00080A02 1\n"
        )
        result = parse_candump(path)
        self.assertEqual(len(result.frames), 3)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.frames[0].arb_id, 0x153)
        self.assertEqual(result.frames[2].data, bytes.fromhex("00080A02"))

    def test_unparsable_line_is_counted_not_guessed(self):
        path = self._write(
            "(0.0) can0 100#0102\n"
            "this is not a candump line at all\n"
        )
        result = parse_candump(path)
        self.assertEqual(len(result.frames), 1)
        self.assertEqual(result.skipped, 1)

    def test_blank_lines_are_ignored_without_counting_as_skipped(self):
        path = self._write("(0.0) can0 100#0102\n\n\n")
        result = parse_candump(path)
        self.assertEqual(len(result.frames), 1)
        self.assertEqual(result.skipped, 0)

    def test_file_source_opens_a_dot_log_file_via_this_parser(self):
        """End-to-end: FileSource must route .log through parse_candump, not
        python-can's LogReader — confirmed by using the exact line shape
        that reader cannot open (see test_an_extra_trailing_label_...)."""
        path = self._write(
            "(1698232894.363802) can0 153#208010FF00FFB05E 0\n"
            "(1698232894.363804) can0 160#00E7FF0A00000012 1\n"
        )
        source = FileSource(path, speed=0.0)
        source.open()
        try:
            self.assertEqual(source.total_frames, 2)
            self.assertEqual(source.skipped_lines, 0)
            first = source.receive(timeout=0.0)
            self.assertIsNotNone(first)
            self.assertEqual(first.arb_id, 0x153)
        finally:
            source.close()


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
