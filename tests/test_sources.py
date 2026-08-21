"""Source tests.

These exercise the ASC parser and offline playback against the checked-in
capture, and assert that the live source refuses interfaces whose listen-only
mode cannot be confirmed. No CAN interface is opened.
"""

from __future__ import annotations

import os
import sys
import time
import types
import unittest
from unittest import mock

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

    def test_fd_esi_is_not_an_error_frame(self):
        path = self._write(
            "base hex  timestamps absolute\n"
            "  0.100000 CANFD 1 Rx 200 EngineData 1 1 2 2 AA BB\n"
        )
        frame = parse_asc(path).frames[0]
        self.assertTrue(frame.is_error_state_indicator)
        self.assertFalse(frame.is_error_frame)

    def test_fd_explicit_clear_esi_is_preserved(self):
        path = self._write(
            "base hex  timestamps absolute\n"
            "  0.100000 CANFD 1 Rx 200 0 0 2 2 AA BB\n"
        )
        self.assertIs(parse_asc(path).frames[0].is_error_state_indicator, False)

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

    def test_canfd_esi_flag_is_independent_of_error_frame(self):
        path = self._write("(0.0) can0 300##3AABB\n")
        frame = parse_candump(path).frames[0]
        self.assertTrue(frame.is_bitrate_switch)
        self.assertTrue(frame.is_error_state_indicator)
        self.assertFalse(frame.is_error_frame)

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


class PlaybackLoopTimestampTests(unittest.TestCase):
    """Looped playback must keep advancing time, not restart it at 0 on
    every wraparound -- see cansniff/sources/file_source.py's
    _compute_loop_span/receive(). speed=0.0 throughout: it disables the
    wall-clock pacing branch entirely, so receive() can be drained in a tight
    loop without any real waiting, which is what lets these tests assert
    exact timestamp values instead of fuzzy timing bounds.
    """

    def _write_log(self, timestamps, arb_ids=None):
        arb_ids = arb_ids or [0x100] * len(timestamps)
        lines = "".join(
            "({:.6f}) can0 {:X}#0102030405060708\n".format(t, arb)
            for t, arb in zip(timestamps, arb_ids)
        )
        path = os.path.join(
            os.environ.get("TEMP", "."),
            "cansniff_loop_test_{}_{}.log".format(os.getpid(), id(timestamps)),
        )
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(lines)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _drain_loops(self, source, frames_per_loop, loops):
        out = []
        for _ in range(frames_per_loop * loops):
            frame = source.receive(timeout=0.0)
            self.assertIsNotNone(frame, "receive() returned None before the "
                                 "requested number of frames arrived")
            out.append(frame)
        return out

    # -- the reported bug: loop must not reset time to 0 -----------------

    def test_loop_continues_timestamps_without_reset(self):
        stamps = [0.00, 0.10, 0.25, 1.00]
        source = FileSource(self._write_log(stamps), speed=0.0, loop=True)
        source.open()
        self.addCleanup(source.close)

        frames = self._drain_loops(source, len(stamps), loops=3)
        seen = [f.timestamp for f in frames]

        # Never decreases, anywhere -- across all three loop boundaries.
        for earlier, later in zip(seen, seen[1:]):
            self.assertLessEqual(earlier, later,
                                 "timestamp went backwards: {} -> {}".format(earlier, later))
        # The bug this regression covers, specifically: the frame right
        # after wraparound must not equal the capture's own first recorded
        # timestamp (0.00) the way the reported jump-back-to-zero did.
        self.assertNotEqual(seen[len(stamps)], stamps[0])
        self.assertGreater(seen[len(stamps)], seen[len(stamps) - 1])

    def test_intra_loop_deltas_match_the_original_recording(self):
        """The exact values must preserve the timing intervals from the
        original capture -- not just "increasing", but increasing by the
        same amounts."""
        stamps = [0.00, 0.10, 0.25, 1.00]
        original_deltas = [b - a for a, b in zip(stamps, stamps[1:])]

        source = FileSource(self._write_log(stamps), speed=0.0, loop=True)
        source.open()
        self.addCleanup(source.close)

        frames = self._drain_loops(source, len(stamps), loops=2)
        for loop in (0, 1):
            loop_stamps = [f.timestamp for f in frames[loop * 4:(loop + 1) * 4]]
            deltas = [b - a for a, b in zip(loop_stamps, loop_stamps[1:])]
            for expected, actual in zip(original_deltas, deltas):
                self.assertAlmostEqual(expected, actual, places=9)

    def test_first_loop_is_emitted_completely_unchanged(self):
        stamps = [0.00, 0.10, 0.25, 1.00]
        source = FileSource(self._write_log(stamps), speed=0.0, loop=True)
        source.open()
        self.addCleanup(source.close)
        first_loop = self._drain_loops(source, len(stamps), loops=1)
        self.assertEqual([f.timestamp for f in first_loop], stamps)

    def test_frame_order_repeats_each_loop(self):
        stamps = [0.00, 0.10, 0.25, 1.00]
        arb_ids = [0x100, 0x101, 0x102, 0x103]
        source = FileSource(self._write_log(stamps, arb_ids), speed=0.0, loop=True)
        source.open()
        self.addCleanup(source.close)

        frames = self._drain_loops(source, len(stamps), loops=2)
        first_order = [f.arb_id for f in frames[:4]]
        second_order = [f.arb_id for f in frames[4:]]
        self.assertEqual(first_order, arb_ids)
        self.assertEqual(second_order, arb_ids)

    def test_many_loops_accumulate_predictably_without_drift(self):
        stamps = [0.00, 0.10, 0.25, 1.00]
        source = FileSource(self._write_log(stamps), speed=0.0, loop=True)
        source.open()
        self.addCleanup(source.close)

        loops = 20
        frames = self._drain_loops(source, len(stamps), loops=loops)
        first_of_each_loop = [frames[i * 4].timestamp for i in range(loops)]
        step = first_of_each_loop[1] - first_of_each_loop[0]
        self.assertGreater(step, 0.0)
        for n, value in enumerate(first_of_each_loop):
            self.assertAlmostEqual(value, stamps[0] + n * step, places=6,
                                   msg="loop {} drifted from the expected linear offset".format(n))

    # -- raw storage is provenance, never touched -------------------------

    def test_raw_stored_frames_are_never_mutated_by_looping(self):
        stamps = [0.00, 0.10, 0.25, 1.00]
        source = FileSource(self._write_log(stamps), speed=0.0, loop=True)
        source.open()
        self.addCleanup(source.close)
        self._drain_loops(source, len(stamps), loops=3)
        # _frames is the parsed-from-disk record; receive() must only ever
        # adjust what it *hands out*, never what it holds.
        self.assertEqual([f.timestamp for f in source._frames], stamps)

    # -- explicit restart vs. automatic loop are different things ---------

    def test_reopening_starts_a_fresh_continuous_timeline(self):
        stamps = [0.00, 0.10, 0.25, 1.00]
        path = self._write_log(stamps)
        source = FileSource(path, speed=0.0, loop=True)
        source.open()
        self.addCleanup(source.close)
        self._drain_loops(source, len(stamps), loops=2)  # advance the offset

        source.open()  # explicit re-open: a new playback session
        first = source.receive(timeout=0.0)
        self.assertEqual(first.timestamp, stamps[0],
                         "re-opening must not carry over the previous session's "
                         "loop offset")

    # -- edge cases -------------------------------------------------------

    def test_single_frame_capture_loops_without_crashing(self):
        source = FileSource(self._write_log([5.0]), speed=0.0, loop=True)
        source.open()
        self.addCleanup(source.close)
        frames = self._drain_loops(source, frames_per_loop=1, loops=5)
        # No recorded interval exists to derive a step from -- non-decreasing
        # is the documented, acceptable outcome here (never negative/NaN).
        for f in frames:
            self.assertEqual(f.timestamp, 5.0)

    def test_two_frame_capture_loops_without_duplicate_or_division_error(self):
        stamps = [0.0, 0.1]
        source = FileSource(self._write_log(stamps), speed=0.0, loop=True)
        source.open()
        self.addCleanup(source.close)
        frames = self._drain_loops(source, len(stamps), loops=3)
        seen = [f.timestamp for f in frames]
        for earlier, later in zip(seen, seen[1:]):
            self.assertLess(earlier, later)  # strictly increasing throughout
        # No duplicate at the seam: loop 2's first frame must not land on
        # loop 1's last frame's timestamp.
        self.assertNotEqual(seen[2], seen[1])

    def test_capture_starting_at_a_nonzero_timestamp(self):
        stamps = [153.25, 153.40, 154.10]
        source = FileSource(self._write_log(stamps), speed=0.0, loop=True)
        source.open()
        self.addCleanup(source.close)
        frames = self._drain_loops(source, len(stamps), loops=2)
        seen = [f.timestamp for f in frames]
        self.assertEqual(seen[:3], stamps)
        self.assertGreater(seen[3], seen[2])
        self.assertAlmostEqual(seen[4] - seen[3], stamps[1] - stamps[0], places=9)

    def test_identical_timestamps_stay_equal_within_a_loop(self):
        stamps = [0.0, 0.0, 0.5, 1.0]
        source = FileSource(self._write_log(stamps), speed=0.0, loop=True)
        source.open()
        self.addCleanup(source.close)
        frames = self._drain_loops(source, len(stamps), loops=2)
        seen = [f.timestamp for f in frames]
        self.assertEqual(seen[0], seen[1])       # preserved within loop 1
        self.assertEqual(seen[4], seen[5])       # preserved within loop 2
        self.assertGreaterEqual(seen[4], seen[3])  # non-decreasing across the seam

    # -- pause/resume must not touch the offset ----------------------------

    def test_irregular_call_cadence_does_not_disturb_the_offset(self):
        """FileSource has no concept of "paused" -- the worker keeps
        receiving through a pause and MainWindow simply stops delivering
        frames to the views (see capture.py's own comment on this). So the
        only thing to verify here is that receive() itself is stateless
        with respect to *when* it is called -- a gap between calls (which is
        all a pause looks like from this source's perspective) changes
        nothing about which frame or timestamp comes out next."""
        stamps = [0.00, 0.10, 0.25, 1.00]
        source = FileSource(self._write_log(stamps), speed=0.0, loop=True)
        source.open()
        self.addCleanup(source.close)

        uninterrupted = self._drain_loops(source, len(stamps), loops=1)

        source.open()  # fresh session, identical setup
        first_half = [source.receive(timeout=0.0) for _ in range(2)]
        time.sleep(0.05)  # a pause-like gap with no receive() calls at all
        second_half = [source.receive(timeout=0.0) for _ in range(2)]
        interrupted = first_half + second_half

        self.assertEqual([f.timestamp for f in interrupted],
                         [f.timestamp for f in uninterrupted])


class ResumeFromTests(unittest.TestCase):
    """FileSource(resume_from=...) is what lets MainWindow continue a
    Stop -> Start playback-session timeline instead of restarting the
    capture's own recorded t=0 underneath history that Stop never clears --
    see main_window.py's start_capture()/_playback_high_water. Each test
    here constructs a *fresh* FileSource per "run", exactly like
    start_capture() does, rather than reusing one instance across Stop/Start
    the way looping reuses one instance across an automatic wraparound.
    """

    def _write_log(self, timestamps, arb_ids=None):
        arb_ids = arb_ids or [0x100] * len(timestamps)
        lines = "".join(
            "({:.6f}) can0 {:X}#0102030405060708\n".format(t, arb)
            for t, arb in zip(timestamps, arb_ids)
        )
        path = os.path.join(
            os.environ.get("TEMP", "."),
            "cansniff_resume_test_{}_{}.log".format(os.getpid(), id(timestamps)),
        )
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(lines)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _run(self, path, stamps, resume_from=None):
        """One playback "run": a brand new FileSource, drained once through
        the whole file -- the same shape as one Stop-bounded Start."""
        source = FileSource(path, speed=0.0, loop=False, resume_from=resume_from)
        source.open()
        try:
            out = []
            while True:
                frame = source.receive(timeout=0.0)
                if frame is None:
                    break
                out.append(frame)
            return out
        finally:
            source.close()

    def test_no_resume_from_starts_at_the_recorded_timestamps(self):
        stamps = [0.00, 0.10, 0.25, 1.00]
        path = self._write_log(stamps)
        run1 = self._run(path, stamps)
        self.assertEqual([f.timestamp for f in run1], stamps)

    def test_stop_then_start_continues_past_the_previous_runs_endpoint(self):
        """The bug report, reproduced directly: run 1 ends near 68s-scale
        history; run 2 (a brand new FileSource, exactly what a Stop then
        Start builds) must not restart at the capture's own t=0."""
        stamps = [0.00, 0.10, 0.25, 1.00]
        path = self._write_log(stamps)

        run1 = self._run(path, stamps)
        history_high_water = max(f.timestamp for f in run1)  # what MainWindow tracks

        run2 = self._run(path, stamps, resume_from=history_high_water)
        run2_stamps = [f.timestamp for f in run2]

        # The exact reported symptom must not occur: run 2 must not restart
        # at the original recorded first timestamp while run 1 is retained.
        self.assertNotEqual(run2_stamps[0], stamps[0])
        self.assertGreater(run2_stamps[0], history_high_water)
        # And the combined, retained timeline (run1 + run2) is what Plot
        # would actually be handed -- it must never go backward.
        combined = [f.timestamp for f in run1] + run2_stamps
        for earlier, later in zip(combined, combined[1:]):
            self.assertLessEqual(earlier, later)

    def test_resumed_run_preserves_the_original_inter_frame_spacing(self):
        stamps = [0.00, 0.10, 0.25, 1.00]
        original_deltas = [b - a for a, b in zip(stamps, stamps[1:])]
        path = self._write_log(stamps)

        run1 = self._run(path, stamps)
        run2 = self._run(path, stamps, resume_from=max(f.timestamp for f in run1))
        run2_stamps = [f.timestamp for f in run2]
        deltas = [b - a for a, b in zip(run2_stamps, run2_stamps[1:])]
        for expected, actual in zip(original_deltas, deltas):
            self.assertAlmostEqual(expected, actual, places=9)

    def test_boundary_gap_is_not_zero_or_duplicated(self):
        """The seam between run 1's last sample and run 2's first must not
        be a duplicate timestamp (offset = 0) the way naively resuming from
        exactly the last value, with no added gap, would produce."""
        stamps = [0.00, 0.10, 0.25, 1.00]
        path = self._write_log(stamps)
        run1 = self._run(path, stamps)
        last = run1[-1].timestamp
        run2 = self._run(path, stamps, resume_from=last)
        self.assertGreater(run2[0].timestamp, last)

    def test_many_stop_start_cycles_stay_monotonic(self):
        stamps = [0.00, 0.10, 0.25, 1.00]
        path = self._write_log(stamps)

        all_frames = []
        high_water = None
        for _ in range(6):
            run = self._run(path, stamps, resume_from=high_water)
            all_frames.extend(run)
            high_water = max(f.timestamp for f in run)

        seen = [f.timestamp for f in all_frames]
        for earlier, later in zip(seen, seen[1:]):
            self.assertLessEqual(earlier, later)

    def test_pause_between_runs_does_not_add_wall_clock_time(self):
        """Pause/Resume never rebuilds the source at all (see
        capture.py/CaptureWorker: pause only suppresses delivery to the UI,
        it never stops receive()), so it cannot be represented as two
        FileSource "runs" the way Stop -> Start can -- this test instead
        pins down the invariant that matters here directly: a resumed run's
        first timestamp depends only on history_high_water and the
        capture's own recorded spacing, never on how much real time passed
        before this run was constructed."""
        stamps = [0.00, 0.10, 0.25, 1.00]
        path = self._write_log(stamps)
        run1 = self._run(path, stamps)
        high_water = max(f.timestamp for f in run1)

        immediate = self._run(path, stamps, resume_from=high_water)
        time.sleep(0.05)
        after_a_gap = self._run(path, stamps, resume_from=high_water)

        self.assertEqual([f.timestamp for f in immediate],
                         [f.timestamp for f in after_a_gap])

    def test_new_capture_does_not_inherit_a_previous_sessions_offset(self):
        """Opening a different capture is a fresh session -- MainWindow's
        clear_views() resets the high-water mark to None before the next
        Start, which this test represents directly as resume_from=None."""
        stamps_a = [0.00, 0.10, 0.25, 1.00]
        run_a = self._run(self._write_log(stamps_a), stamps_a)
        self.assertGreater(max(f.timestamp for f in run_a), 0.9)  # session A progressed

        stamps_b = [0.00, 0.05, 0.15]
        run_b = self._run(self._write_log(stamps_b), stamps_b, resume_from=None)
        self.assertEqual([f.timestamp for f in run_b], stamps_b)

    def test_single_frame_capture_resumes_without_crashing(self):
        path = self._write_log([5.0])
        run1 = self._run(path, [5.0])
        run2 = self._run(path, [5.0], resume_from=run1[0].timestamp)
        # No recorded interval to derive a gap from -- non-decreasing is the
        # documented, acceptable outcome (never negative, never a crash).
        self.assertGreaterEqual(run2[0].timestamp, run1[0].timestamp)

    def test_resume_from_a_point_the_capture_already_exceeds_does_not_go_negative(self):
        """Defensive only: nothing in this application should ever pass a
        resume_from smaller than the capture's own first timestamp, but the
        offset must still never go negative if it happens anyway."""
        stamps = [10.0, 10.1, 10.2]
        path = self._write_log(stamps)
        run = self._run(path, stamps, resume_from=0.0)
        self.assertGreaterEqual(run[0].timestamp, stamps[0])


class LiveSourceSafetyTests(unittest.TestCase):
    def test_unknown_interface_is_refused_by_default(self):
        source = LiveSource({"interface": "vector", "channel": "0"})
        with self.assertRaises(SourceError) as ctx:
            source.open()
        self.assertIn("listen-only", str(ctx.exception))

    def test_explicit_opt_out_allows_unknown_interface_with_warning(self):
        source = LiveSource({
            "interface": "vector", "channel": "0", "require_listen_only": False,
        })
        note = source._preflight()
        self.assertTrue(note.startswith("NOT VERIFIED"))
        source.passive_note = note
        self.assertFalse(source.passive_verified)

    def test_explicit_opt_out_opens_unknown_receive_only_backend(self):
        calls = []

        class FakeBus:
            def __init__(self, **kwargs):
                calls.append(kwargs)

            def shutdown(self):
                pass

        source = LiveSource({
            "interface": "vector", "channel": "0", "bitrate": 500000,
            "require_listen_only": False,
        })
        with mock.patch.dict(sys.modules, {
                "can": types.SimpleNamespace(Bus=FakeBus)}):
            source.open()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["interface"], "vector")
        self.assertFalse(calls[0]["receive_own_messages"])
        self.assertFalse(source.passive_verified)
        source.close()

    def test_socketcan_opt_out_allows_unverified_link(self):
        source = LiveSource({
            "interface": "socketcan", "channel": "can0",
            "require_listen_only": False,
        })
        with mock.patch("cansniff.sources.live._socketcan_is_listen_only",
                        return_value=False):
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

    def test_protected_extra_kwargs_are_rejected(self):
        protected = ("interface", "bustype", "channel", "bitrate",
                     "data_bitrate", "fd", "receive_own_messages",
                     "driver_mode", "ignore_config", "state", "listen_only",
                     "silent", "passive", "local_loopback")
        for name in protected:
            with self.subTest(name=name):
                source = LiveSource({"interface": "virtual",
                                     "extra_kwargs": {name: True}})
                with self.assertRaises(SourceError) as ctx:
                    source.open()
                self.assertIn(name, str(ctx.exception))

    def test_harmless_extra_kwarg_reaches_the_single_bus_construction(self):
        calls = []

        class FakeBus:
            def __init__(self, **kwargs):
                calls.append(kwargs)

            def shutdown(self):
                pass

        fake_can = types.SimpleNamespace(Bus=FakeBus)
        source = LiveSource({"interface": "virtual", "channel": "safe",
                             "extra_kwargs": {"preserve_timestamps": True}})
        with mock.patch.dict(sys.modules, {"can": fake_can}):
            source.open()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], source._bus_kwargs())
        self.assertFalse(calls[0]["receive_own_messages"])
        source.close()

    def test_pcan_passive_setup_failure_has_no_active_fallback(self):
        calls = []

        class FakeBus:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self.closed = False

            def shutdown(self):
                self.closed = True

        source = LiveSource({"interface": "pcan", "channel": "PCAN_USBBUS1"})

        def reject_listen_only():
            source.close()
            raise SourceError("listen-only rejected")

        with mock.patch.dict(sys.modules, {"can": types.SimpleNamespace(Bus=FakeBus)}), \
                mock.patch.object(source, "_apply_pcan_listen_only",
                                  side_effect=reject_listen_only):
            with self.assertRaises(SourceError):
                source.open()
        self.assertEqual(len(calls), 1)

    def test_pcan_opt_out_keeps_open_bus_when_listen_only_is_rejected(self):
        class FakeBus:
            def shutdown(self):
                pass

        source = LiveSource({
            "interface": "pcan", "channel": "PCAN_USBBUS1",
            "require_listen_only": False,
        })
        source._bus = FakeBus()
        with mock.patch.dict(sys.modules, {
                "can.interfaces.pcan.basic": types.SimpleNamespace()}):
            source._apply_pcan_listen_only()
        self.assertIsNotNone(source._bus)
        self.assertFalse(source.passive_verified)

    def test_live_fd_message_preserves_esi(self):
        message = types.SimpleNamespace(
            timestamp=1.0, arbitration_id=0x123, data=b"\x01", dlc=1,
            is_extended_id=False, is_fd=True, bitrate_switch=True,
            error_state_indicator=True, is_error_frame=False,
            is_remote_frame=False, channel="vcan0",
        )
        source = LiveSource({"interface": "virtual"})
        source._bus = types.SimpleNamespace(recv=lambda timeout: message)
        frame = source.receive()
        self.assertTrue(frame.is_error_state_indicator)
        self.assertFalse(frame.is_error_frame)

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
