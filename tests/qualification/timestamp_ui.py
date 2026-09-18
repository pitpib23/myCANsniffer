"""Exercise production main(--lite) / main() without opening a CAN interface.

Run from the repository root with its venv, on offscreen/xcb/wayland. The
entry point builds its actual theme/window; this harness only injects received
frames, chooses Trace, checks widths and exports, then captures the window.
Artifacts go to /tmp/cansniff-timestamp-ui unless --output-dir is supplied.
"""

import argparse
import csv
import json
import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem

import main
from cansniff.export import FORMATS
from cansniff.model import CanFrame
from cansniff.ui.main_window import MainWindow


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lite", action="store_true")
    parser.add_argument("--output-dir", default="/tmp/cansniff-timestamp-ui")
    args = parser.parse_args()
    os.makedirs(args.output_dir, exist_ok=True)
    mode = "lite" if args.lite else "full"
    failures = []
    original_exec = QApplication.exec
    t0 = datetime(2026, 9, 17, 14, 32, 18, 483271,
                  tzinfo=timezone(timedelta(hours=7))).timestamp()
    frames = [CanFrame(t0 + i * .000721, 0x181 + i, bytes(range(8)), 8,
                       channel="can0") for i in range(12)]
    frames.append(CanFrame(t0 + 1, 0x18FEF100, bytes(range(64)), 64,
                           is_extended=True, is_fd=True, is_bitrate_switch=True,
                           channel="can0"))

    def check(window):
        try:
            table = window.trace_view
            model = window.trace_model
            assert model.frame_at(0) is frames[0]
            assert model.COLUMNS[0] == "Time"
            metrics = QFontMetrics(model.data(model.index(0, 0), Qt.FontRole))
            displayed = model.data(model.index(0, 0))
            assert table.columnWidth(0) >= metrics.horizontalAdvance(displayed) + 16
            option = QStyleOptionViewItem()
            table.initViewItemOption(option)
            index = model.index(0, 0)
            table.itemDelegateForIndex(index).initStyleOption(option, index)
            option.rect = table.visualRect(index)
            text_rect = table.style().subElementRect(QStyle.SE_ItemViewItemText, option, table)
            assert text_rect.width() >= metrics.horizontalAdvance(displayed) + 6, (text_rect, displayed)
            available = table.viewport().width() - table.columnWidth(0) - table.columnWidth(2)
            if args.lite:
                assert available >= metrics.horizontalAdvance(frames[0].data_hex)
                assert table.horizontalScrollBarPolicy() == Qt.ScrollBarAsNeeded
            csv_path = os.path.join(args.output_dir, mode + ".csv")
            with patch("cansniff.ui.main_window.QFileDialog.getSaveFileName",
                       return_value=(csv_path, FORMATS["csv"].filter)):
                window._export_capture()
            with open(csv_path, newline="") as handle:
                rows = list(csv.DictReader(handle))
            assert len(rows) == len(frames)
            assert rows[1]["elapsed_us"] == "721"
            assert bytes.fromhex(rows[-1]["data"]) == bytes(range(64))
            image_path = os.path.join(args.output_dir, mode + ".png")
            assert window.grab().save(image_path)
            result = dict(mode=mode, platform=QApplication.platformName(),
                          window=[window.width(), window.height()],
                          time=displayed, time_width=table.columnWidth(0),
                          id_width=table.columnWidth(2),
                          payload_width=table.columnWidth(5),
                          viewport_width=table.viewport().width(),
                          horizontal_max=table.horizontalScrollBar().maximum(),
                          image=image_path, csv=csv_path)
            print(json.dumps(result), flush=True)
            with open(os.path.join(args.output_dir, mode + ".json"), "w") as handle:
                json.dump(result, handle, indent=2)
        except Exception:
            failures.append(traceback.format_exc())
        finally:
            window.close()
            QApplication.quit()

    def inject():
        try:
            window = next(w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow))
            window.nav.group.button(window._NAV_TRACE).click()
            window._on_frames(frames)
            QTimer.singleShot(500, lambda: check(window))
        except Exception:
            failures.append(traceback.format_exc())
            QApplication.quit()

    def event_loop(app):
        QTimer.singleShot(300, inject)
        return original_exec()

    with tempfile.TemporaryDirectory(prefix="cansniff-timestamp-config-") as directory:
        argv = ["--config", os.path.join(directory, "config.json")]
        if args.lite:
            argv.append("--lite")
        with patch.object(QApplication, "exec", event_loop):
            result = main.main(argv)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(run())
