"""Receive-time provenance, Trace presentation, CSV and session regressions.

No CAN interface is opened. UI checks use the exact production palette, QSS
and font sizes; set QT_QPA_PLATFORM=xcb/wayland to exercise a real display.
"""

import csv
import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import can
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem

from cansniff.capture import FrameLogger
from cansniff.config import Config
from cansniff.export import CSV_HEADER, FORMATS, export
from cansniff.filters import DisplayFilter
from cansniff.model import CanFrame
from cansniff.sources.asc_reader import parse_asc
from cansniff.sources.candump_reader import parse_candump
from cansniff.sources.file_source import FileSource
from cansniff.sources.live import LiveSource
from cansniff.timestamps import elapsed_us, format_trace_time, timestamp_iso
from cansniff.ui.main_window import MainWindow
from cansniff.ui.tables import TraceTableModel, exemplar_widths
from cansniff.ui.theme import Theme

ZONE = timezone(timedelta(hours=7))
T0 = datetime(2026, 9, 17, 14, 32, 18, 483271, tzinfo=ZONE).timestamp()


def frame(t=T0, arb=0x181, **kwargs):
    return CanFrame(t, arb, bytes(range(8)), 8, channel="can0", **kwargs)


class ReceiveTimestampTests(unittest.TestCase):
    def test_backend_receive_time_survives_independent_of_gui(self):
        message = can.Message(timestamp=T0, arbitration_id=0x181,
                              data=bytes(range(64)), is_fd=True, dlc=64)
        source = LiveSource({"interface": "virtual"})
        source._bus = Mock()
        source._bus.recv.return_value = message
        with patch("cansniff.sources.live.time.time", side_effect=AssertionError("retimestamped")):
            received = source.receive()
        self.assertEqual(received.timestamp, T0)
        self.assertEqual(received.receive_timestamp, message.timestamp)
        self.assertEqual(received.data, bytes(range(64)))
        self.assertIsNone(received.recorded_timestamp)
        with self.assertRaises(FrozenInstanceError):
            received.timestamp = 0

    def test_zero_is_preserved_and_only_missing_timestamp_uses_receive_fallback(self):
        source = LiveSource({"interface": "virtual"})
        source._bus = Mock()
        for supplied in (0.0, None):
            message = can.Message(timestamp=0, arbitration_id=1, data=b"")
            message.timestamp = supplied
            source._bus.recv.return_value = message
            with patch("cansniff.sources.live.time.time", return_value=T0) as clock:
                received = source.receive()
            self.assertEqual(received.receive_timestamp, 0.0 if supplied == 0 else T0)
            self.assertEqual(clock.call_count, int(supplied is None))

    def test_file_loop_and_resume_preserve_original_recorded_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "loop.log")
            with open(path, "w") as handle:
                handle.write("(2.000000) can0 181#01\n(2.000721) can0 220#02\n")
            source = FileSource(path, speed=0, loop=True)
            source.open()
            try:
                frames = [source.receive() for _ in range(4)]
                self.assertEqual([f.receive_timestamp for f in frames], [2, 2.000721] * 2)
                self.assertGreater(frames[2].timestamp, frames[1].timestamp)
                resumed = FileSource(path, speed=0, resume_from=frames[-1].timestamp)
                resumed.open()
                try:
                    next_frame = resumed.receive()
                    self.assertGreater(next_frame.timestamp, frames[-1].timestamp)
                    self.assertEqual(next_frame.receive_timestamp, 2)
                finally:
                    resumed.close()
            finally:
                source.close()

    def test_readers_keep_known_text_precision_without_inventing_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            for extension, line, reader, basis in (
                ("asc", "0.012 1 181 Rx d 1 01\n", parse_asc, "relative"),
                ("log", "(0.012) can0 181#01\n", parse_candump, "unknown"),
            ):
                path = os.path.join(directory, "capture." + extension)
                with open(path, "w") as handle:
                    handle.write(line)
                received = reader(path).frames[0]
                self.assertEqual(received.timestamp_basis, basis)
                self.assertEqual(received.timestamp_precision, 3)
                self.assertEqual(format_trace_time(received), "0.012 s")
                self.assertEqual(timestamp_iso(received), "")


class TimestampFormatTests(unittest.TestCase):
    def test_same_frame_formats_full_and_lite_without_changing_raw_time(self):
        received = frame()
        self.assertEqual(format_trace_time(received, local_zone=ZONE),
                         "2026-09-17 14:32:18.483271")
        self.assertEqual(format_trace_time(received, lite=True, local_zone=ZONE),
                         "14:32:18.483")
        self.assertEqual(received.receive_timestamp, T0)

    def test_iso_uses_a_real_utc_offset(self):
        self.assertEqual(timestamp_iso(frame()), "2026-09-17T07:32:18.483271+00:00")
        self.assertEqual(datetime.fromisoformat(timestamp_iso(frame())).timestamp(), T0)

    def test_known_lower_precision_is_not_padded_with_fake_microseconds(self):
        received = frame(round(T0, 3), timestamp_precision=3)
        self.assertEqual(format_trace_time(received, local_zone=ZONE),
                         "2026-09-17 14:32:18.483")
        self.assertEqual(timestamp_iso(received), "2026-09-17T07:32:18.483+00:00")

    def test_cross_midnight_retains_date_in_iso_and_elapsed(self):
        before = datetime(2026, 9, 17, 23, 59, 59, 999900, tzinfo=ZONE).timestamp()
        first, second = frame(before), frame(before + .0002)
        self.assertEqual(format_trace_time(first, True, ZONE), "23:59:59.999")
        self.assertEqual(format_trace_time(second, True, ZONE), "00:00:00.000")
        self.assertTrue(format_trace_time(second, local_zone=ZONE).startswith("2026-09-18 "))
        self.assertEqual(elapsed_us(second.receive_timestamp, first.receive_timestamp), 200)
        self.assertEqual(datetime.fromisoformat(timestamp_iso(second)).astimezone(ZONE).day, 18)


class TimestampCsvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "capture.csv")

    def read(self):
        with open(self.path, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_iso_elapsed_721_and_capture_order_including_equal_and_backward_times(self):
        frames = [frame(), frame(T0 + .000721, 0x220), frame(T0, 0x100), frame(T0 - 1, 0x101)]
        count, spec = export(frames, self.path)
        rows = self.read()
        self.assertEqual(count, 4)
        self.assertEqual(spec.key, "csv")
        self.assertEqual(tuple(rows[0]), CSV_HEADER)
        self.assertEqual(rows[0]["timestamp_iso"], "2026-09-17T07:32:18.483271+00:00")
        self.assertEqual([row["elapsed_us"] for row in rows], ["0", "721", "0", "-1000000"])
        self.assertEqual([row["can_id"] for row in rows], ["0x181", "0x220", "0x100", "0x101"])
        self.assertEqual([float(row["timestamp"]) for row in rows], [f.timestamp for f in frames])

    def test_fd_64_bytes_metadata_and_remote_dlc_are_preserved(self):
        fd = CanFrame(T0, 0x18FEF100, bytes(range(64)), 64,
                      is_extended=True, is_fd=True, is_bitrate_switch=True,
                      is_error_state_indicator=True, channel="can1")
        remote = CanFrame(T0, 0x123, b"", 8, is_remote_frame=True)
        export([fd, remote, replace(fd, is_error_state_indicator=None)], self.path)
        rows = self.read()
        self.assertEqual(bytes.fromhex(rows[0]["data"]), bytes(range(64)))
        self.assertEqual(rows[0]["dlc"], "64")
        self.assertEqual(rows[0]["can_id"], "0x18FEF100")
        self.assertEqual([rows[0][key] for key in ("extended", "fd", "brs", "esi")], ["1"] * 4)
        self.assertEqual(rows[0]["channel"], "can1")
        self.assertEqual((rows[1]["dlc"], rows[1]["data"], rows[1]["remote"]), ("8", "", "1"))
        self.assertEqual(rows[2]["esi"], "")
        self.assertNotIn("direction", rows[0])

    def test_unknown_epoch_has_raw_time_and_elapsed_but_no_iso(self):
        export([frame(1, timestamp_basis="relative"), frame(1.000721, timestamp_basis="relative")], self.path)
        rows = self.read()
        self.assertEqual([row["timestamp_iso"] for row in rows], ["", ""])
        self.assertEqual(rows[1]["timestamp_basis"], "relative")
        self.assertEqual(rows[1]["elapsed_us"], "721")

    def test_rebased_frame_uses_recorded_time_in_export_and_existing_logger(self):
        received = replace(frame(), timestamp=T0 + 10, recorded_timestamp=T0)
        export([received], self.path, time_base=T0 - 1)
        row = self.read()[0]
        self.assertEqual(float(row["timestamp"]), T0)
        self.assertEqual(row["elapsed_us"], "1000000")
        for fmt in ("csv", "jsonl"):
            logger = FrameLogger(self.tmp.name, fmt)
            logger.write([received])
            logger.close()
            with open(logger.path, newline="", encoding="utf-8") as handle:
                record = next(csv.DictReader(handle)) if fmt == "csv" else json.loads(handle.readline())
            self.assertEqual(float(record["timestamp"]), T0)


class TimestampTraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_delayed_trace_render_uses_original_frame_in_both_modes(self):
        received = frame()
        for lite in (False, True):
            model = TraceTableModel(Theme(), lite=lite)
            model.add_frames([received])
            QTest.qWait(10)
            self.assertIs(model.frame_at(0), received)
            self.assertEqual(model.headerData(0, Qt.Horizontal), "Time")
            self.assertEqual(model.data(model.index(0, 0)), format_trace_time(received, lite))
            self.assertIn("2026-09-17T07:32:18.483271+00:00", model.data(model.index(0, 0), Qt.ToolTipRole))

    def test_eviction_and_filter_keep_base_and_clear_resets_it(self):
        model = TraceTableModel(Theme(), max_rows=100)
        model.add_frames([frame(T0 + i / 1000, i) for i in range(101)])
        self.assertEqual(model.total_rows, 100)
        self.assertEqual(model.time_base, T0)
        model.set_filter(DisplayFilter(id_min=100, id_max=100))
        self.assertEqual(model.time_base, T0)
        model.clear()
        self.assertIsNone(model.time_base)
        model.add_frames([frame(T0 + 9)])
        self.assertEqual(model.time_base, T0 + 9)

    def test_production_theme_lite_widths_and_export_action_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config.defaults(os.path.join(directory, "config.json"))
            config.set("capture.max_frames_retained", 100)
            base_size = float(config.get("ui.font_size", 9) or 9)
            theme = Theme(ui_size=base_size, mono_size=base_size + .5,
                          mono_family=str(config.get("ui.font_family", "") or ""))
            self.app.setPalette(theme.qpalette())
            self.app.setStyleSheet(theme.stylesheet())
            self.app.setFont(theme.ui_font())
            window = MainWindow(config, theme, lite=True)
            try:
                window.show()
                window.setGeometry(0, 0, 800, 480)
                window.nav.group.button(window._NAV_TRACE).click()
                window._on_frames([frame(T0 + i / 1000, i) for i in range(101)])
                QTest.qWait(120)
                table = window.trace_view
                metrics = QFontMetrics(window.trace_model.data(window.trace_model.index(0, 0), Qt.FontRole))
                time_width = table.columnWidth(0)
                option = QStyleOptionViewItem()
                table.initViewItemOption(option)
                index = window.trace_model.index(0, 0)
                table.itemDelegateForIndex(index).initStyleOption(option, index)
                option.rect = table.visualRect(index)
                text_rect = table.style().subElementRect(QStyle.SE_ItemViewItemText, option, table)
                self.assertGreaterEqual(text_rect.width(), metrics.horizontalAdvance(option.text) + 6)
                self.assertGreaterEqual(time_width, metrics.horizontalAdvance("23:59:59.999") + 16)
                self.assertLessEqual(time_width, exemplar_widths(window.trace_model, window.theme)[0] + 1)
                self.assertFalse(table.isColumnHidden(0))
                self.assertEqual(table.horizontalScrollBarPolicy(), Qt.ScrollBarAsNeeded)
                # At least a full Classic payload remains visible beside Time/ID.
                available = table.viewport().width() - time_width - table.columnWidth(2)
                self.assertGreaterEqual(available, metrics.horizontalAdvance(frame().data_hex))
                path = os.path.join(directory, "export")
                with patch("cansniff.ui.main_window.QFileDialog.getSaveFileName", return_value=(path, FORMATS["csv"].filter)):
                    window._export_capture()
                with open(path + ".csv", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), 100)
                self.assertEqual(rows[0]["elapsed_us"], "1000", "eviction must not reset session base")
                window.clear_views()
                self.assertIsNone(window.trace_model.time_base)
                window._on_frames([frame(T0 + 10)])
                with patch("cansniff.ui.main_window.QFileDialog.getSaveFileName", return_value=(path, FORMATS["csv"].filter)):
                    window._export_capture()
                with open(path + ".csv", newline="") as handle:
                    self.assertEqual(next(csv.DictReader(handle))["elapsed_us"], "0")
            finally:
                window._teardown_thread()
                # Drain queued sort callbacks before deleting their models.
                self.app.processEvents()
                window.close()
                window.deleteLater()
                self.app.sendPostedEvents(None, QEvent.DeferredDelete)
                self.app.processEvents()
                self.app.setStyleSheet("")


if __name__ == "__main__":
    unittest.main()
