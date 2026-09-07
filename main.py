"""CAN Sniffer — passive, receive-only entry point.

Usage:
    python main.py                       # start the UI with sniffer_config.json
    python main.py --capture other.asc   # start the UI on a specific capture file
    python main.py --config my.json      # use a different configuration document

This program only ever receives. It has no command that transmits, injects,
replays onto a bus, probes or scans.
"""

from __future__ import annotations

import argparse
import os
import sys

from cansniff.config import Config, default_config_path


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="can-sniffer",
        description="Passive CAN bus sniffer (receive-only).",
    )
    parser.add_argument(
        "--config", default=None,
        help="path to the JSON configuration (created on first run); defaults "
             "to ./sniffer_config.json if present, else the per-user "
             "configuration directory for this platform",
    )
    parser.add_argument(
        "--capture", default=None,
        help="capture file to open for offline playback; sets the source to 'file'",
    )
    parser.add_argument(
        "--monitor", action="store_true",
        help="use the configured live listen-only interface instead of a file",
    )
    parser.add_argument(
        "--reset-config", action="store_true",
        help="overwrite the configuration file with defaults before starting",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    config_path = args.config or default_config_path()

    if args.reset_config:
        Config.defaults(config_path).save()

    try:
        config = Config.load(config_path)
    except Exception as exc:
        sys.stderr.write("Could not read configuration {}: {}\n".format(config_path, exc))
        return 2

    if args.capture:
        if not os.path.exists(args.capture):
            sys.stderr.write("Capture file not found: {}\n".format(args.capture))
            return 2
        config.set("source.type", "file")
        config.set("source.file.path", os.path.abspath(args.capture))
    if args.monitor:
        config.set("source.type", "live")

    try:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
    except ImportError:
        sys.stderr.write(
            "PySide6 is not installed. Install the dependencies with:\n"
            "    pip install -r requirements.txt\n"
        )
        return 2

    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.theme import Theme

    app = QApplication(sys.argv[:1])
    app.setApplicationName("CAN Sniffer")
    app.setDesktopFileName("CANbus-sniffer")
    app.setWindowIcon(QIcon("/home/pi/.local/share/icons/hicolor/256x256/apps/CANbus-sniffer.png"))

    # The font database is only queryable once a QApplication exists, so the
    # theme is resolved here rather than at import time.
    base_size = float(config.get("ui.font_size", 9) or 9)
    theme = Theme(
        ui_size=base_size,
        mono_size=base_size + 0.5,
        mono_family=str(config.get("ui.font_family", "") or ""),
    )
    app.setPalette(theme.qpalette())
    app.setStyleSheet(theme.stylesheet())
    app.setFont(theme.ui_font())

    window = MainWindow(config, theme)
    window.showMaximized()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
