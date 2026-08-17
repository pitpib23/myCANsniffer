"""Capture export, and semantic round-trip through the application's importer.

Byte-for-byte file identity is not required — ASC and candump legitimately
normalise headers and timestamp bases. Frame equivalence is.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.export import (  # noqa: E402
    FORMAT_ORDER, FORMATS, ExportError, describe_losses, export,
    format_for_path,
)
from cansniff.model import CanFrame  # noqa: E402
from cansniff.sources.asc_reader import parse_asc  # noqa: E402


def _frame(t=0.0, arb=0x123, data=b"\x01\x02\x03", ext=False, ch="1",
           fd=False, brs=False, error=False, remote=False, dlc=None):
    return CanFrame(timestamp=t, arb_id=arb, data=bytes(data),
                    dlc=len(data) if dlc is None else dlc, channel=ch,
                    is_extended=ext, is_fd=fd, is_bitrate_switch=brs,
                    is_error_frame=error, is_remote_frame=remote)


def _semantic(frame):
    """What must survive a round trip, ignoring format-normalised detail."""
    return (round(frame.timestamp, 4), frame.arb_id, bool(frame.is_extended),
            bytes(frame.data))


class _TempDir(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="cansniff_export_")
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


class FormatSelectionTests(_TempDir):
    def test_extension_picks_the_format(self):
        self.assertEqual(format_for_path("x.asc").key, "asc")
        self.assertEqual(format_for_path("x.log").key, "candump")
        self.assertEqual(format_for_path("x.csv").key, "csv")
        self.assertEqual(format_for_path("x.jsonl").key, "jsonl")
        self.assertIsNone(format_for_path("x.bin"))

    def test_unknown_extension_is_a_readable_error(self):
        with self.assertRaises(ExportError) as ctx:
            export([_frame()], self._path("out.bin"))
        self.assertIn("Cannot tell which format", str(ctx.exception))

    def test_every_ordered_format_exists(self):
        for key in FORMAT_ORDER:
            self.assertIn(key, FORMATS)

    def test_round_trippable_formats_are_marked(self):
        self.assertTrue(FORMATS["asc"].importable)
        self.assertTrue(FORMATS["candump"].importable)


class AscRoundTripTests(_TempDir):
    def _round_trip(self, frames):
        path = self._path("capture.asc")
        count, spec = export(frames, path)
        self.assertEqual(count, len(frames))
        self.assertEqual(spec.key, "asc")
        self.assertTrue(os.path.getsize(path) > 0)
        return parse_asc(path)

    def test_standard_frames_survive(self):
        frames = [_frame(t=i * 0.01, arb=0x100 + i, data=bytes([i, i + 1]))
                  for i in range(10)]
        result = self._round_trip(frames)
        self.assertEqual(result.skipped, 0)
        self.assertEqual([_semantic(f) for f in result.frames],
                         [_semantic(f) for f in frames])

    def test_extended_identity_survives(self):
        frames = [_frame(arb=0x18FEF100, ext=True, data=b"\xAA\xBB"),
                  _frame(t=0.01, arb=0x100, ext=False, data=b"\xCC")]
        result = self._round_trip(frames)
        self.assertEqual([f.is_extended for f in result.frames], [True, False])
        self.assertEqual(result.frames[0].arb_id, 0x18FEF100)

    def test_eight_byte_payload_survives(self):
        frames = [_frame(data=bytes(range(8)))]
        result = self._round_trip(frames)
        self.assertEqual(result.frames[0].data, bytes(range(8)))

    def test_zero_length_payload_survives(self):
        frames = [_frame(data=b"")]
        result = self._round_trip(frames)
        self.assertEqual(len(result.frames), 1)
        self.assertEqual(result.frames[0].data, b"")

    def test_can_fd_payload_survives(self):
        frames = [_frame(data=bytes(range(64)), fd=True, brs=True, dlc=64)]
        result = self._round_trip(frames)
        self.assertEqual(len(result.frames), 1)
        got = result.frames[0]
        self.assertTrue(got.is_fd)
        self.assertEqual(len(got.data), 64)
        self.assertEqual(got.dlc, 64, "dlc is a byte count on both sides")

    def test_timestamps_keep_their_ordering_and_spacing(self):
        frames = [_frame(t=i * 0.25) for i in range(6)]
        result = self._round_trip(frames)
        stamps = [f.timestamp for f in result.frames]
        self.assertEqual(stamps, sorted(stamps))
        self.assertAlmostEqual(stamps[-1] - stamps[0], 1.25, places=3)

    def test_multiple_channels_are_written(self):
        frames = [_frame(ch="1", data=b"\x01"), _frame(t=0.01, ch="2", data=b"\x02")]
        result = self._round_trip(frames)
        self.assertEqual({f.channel for f in result.frames}, {"1", "2"})

    def test_large_capture_round_trips(self):
        frames = [_frame(t=i * 0.001, arb=0x100 + (i % 20),
                         data=bytes([i & 0xFF, (i >> 8) & 0xFF]))
                  for i in range(5000)]
        result = self._round_trip(frames)
        self.assertEqual(len(result.frames), 5000)
        self.assertEqual(result.skipped, 0)


class CandumpRoundTripTests(_TempDir):
    def _export(self, frames, name="capture.log"):
        path = self._path(name)
        count, spec = export(frames, path)
        self.assertEqual(spec.key, "candump")
        return path, count

    def _read_back(self, path):
        import can
        with can.CanutilsLogReader(path) as reader:
            return list(reader)

    def test_standard_frames_survive(self):
        frames = [_frame(t=i * 0.01, arb=0x100 + i, data=bytes([i]))
                  for i in range(8)]
        path, count = self._export(frames)
        self.assertEqual(count, 8)
        back = self._read_back(path)
        self.assertEqual([m.arbitration_id for m in back],
                         [f.arb_id for f in frames])
        self.assertEqual([bytes(m.data) for m in back],
                         [f.data for f in frames])

    def test_extended_identity_survives(self):
        frames = [_frame(arb=0x18FEF100, ext=True, data=b"\x01")]
        back = self._read_back(self._export(frames)[0])
        self.assertTrue(back[0].is_extended_id)
        self.assertEqual(back[0].arbitration_id, 0x18FEF100)

    def test_zero_length_payload_survives(self):
        back = self._read_back(self._export([_frame(data=b"")])[0])
        self.assertEqual(len(back), 1)
        self.assertEqual(bytes(back[0].data), b"")

    def test_eight_byte_payload_survives(self):
        back = self._read_back(self._export([_frame(data=bytes(range(8)))])[0])
        self.assertEqual(bytes(back[0].data), bytes(range(8)))

    def test_named_interface_channel_survives_unchanged(self):
        frames = [_frame(ch="vcan0", data=b"\x01")]
        back = self._read_back(self._export(frames)[0])
        self.assertEqual(str(back[0].channel), "vcan0")

    def test_numeric_channel_becomes_an_interface_name(self):
        """candump records interface names, not bare channel numbers."""
        back = self._read_back(self._export([_frame(ch="1", data=b"\x01")])[0])
        self.assertEqual(str(back[0].channel), "can1")

    def test_that_rename_is_reported_to_the_user(self):
        note = describe_losses(FORMATS["candump"], [_frame(ch="1")])
        self.assertIn("1 becomes can1", note)

    def test_no_rename_note_for_named_interfaces(self):
        self.assertEqual(describe_losses(FORMATS["candump"],
                                         [_frame(ch="vcan0")]), "")


class InHouseFormatTests(_TempDir):
    def test_csv_has_a_header_and_a_row_per_frame(self):
        frames = [_frame(t=i * 0.1) for i in range(4)]
        path = self._path("out.csv")
        count, _spec = export(frames, path)
        self.assertEqual(count, 4)
        with open(path, encoding="utf-8") as fh:
            lines = [line for line in fh.read().splitlines() if line]
        self.assertEqual(len(lines), 5)
        self.assertIn("timestamp", lines[0])

    def test_jsonl_is_one_object_per_frame(self):
        import json
        frames = [_frame(t=i * 0.1, arb=0x200 + i) for i in range(3)]
        path = self._path("out.jsonl")
        export(frames, path)
        with open(path, encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["id"], "200")
        self.assertFalse(records[0]["extended"])

    def test_empty_capture_writes_a_valid_file(self):
        for name in ("empty.csv", "empty.jsonl", "empty.asc", "empty.log"):
            path = self._path(name)
            count, _spec = export([], path)
            self.assertEqual(count, 0)
            self.assertTrue(os.path.exists(path), name)


class ErrorHandlingTests(_TempDir):
    def test_unwritable_destination_is_reported_not_raised_raw(self):
        target = self._path("nested")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        with self.assertRaises(ExportError):
            export([_frame()], os.path.join(target, "deeper", "out.csv"))

    def test_missing_directory_is_created(self):
        path = os.path.join(self._dir, "made", "up", "out.csv")
        count, _spec = export([_frame()], path)
        self.assertEqual(count, 1)
        self.assertTrue(os.path.exists(path))


class LossReportingTests(unittest.TestCase):
    def test_candump_warns_about_error_frames_only_when_present(self):
        spec = FORMATS["candump"]
        # A named interface avoids the separate channel-rename note.
        self.assertEqual(describe_losses(spec, [_frame(ch="vcan0")]), "")
        note = describe_losses(spec, [_frame(ch="vcan0", error=True)])
        self.assertIn("error frames", note)

    def test_candump_warns_about_remote_frames(self):
        note = describe_losses(FORMATS["candump"], [_frame(ch="vcan0", remote=True)])
        self.assertIn("remote frames", note)

    def test_asc_reports_nothing_for_an_ordinary_capture(self):
        self.assertEqual(describe_losses(FORMATS["asc"], [_frame()]), "")


if __name__ == "__main__":
    unittest.main()
