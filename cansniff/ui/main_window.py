"""Main application window.

Structure: a top bar that separates capture control from configuration, a
frame browser on the left, and the interpretation workspace on the right —
which is where the actual work happens, so it gets the larger share.

Capture still runs on a worker thread; this window only consumes frames.
"""

from __future__ import annotations

import os
import time
from typing import List, Optional

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QStackedWidget, QTableView, QVBoxLayout, QWidget,
)

from ..capture import CaptureWorker, FrameLogger
from ..config import Config
from ..filters import FilterSet
from ..model import CanFrame
from ..sources import SourceError, build_source
from .config_dialog import ConfigDialog
from .filter_bar import FilterBar
from .filter_dialog import FilterDialog
from .interpret_view import InterpretView
from .signal_dialog import SignalDialog
from .tables import (
    ByteHighlightDelegate, IdFilterProxy, IdTableModel, KEY_ROLE, TraceTableModel,
    exemplar_widths,
)
from .theme import ROW_HEIGHT_COMPACT, SPACE_LG, SPACE_MD, SPACE_SM, Theme
from .widgets import Chip, MetricChip, NavRail, SectionLabel


class MainWindow(QMainWindow):
    batchConsumed = Signal()

    def __init__(self, config: Config, theme: Theme):
        super().__init__()
        self.config = config
        self.theme = theme
        self.setWindowTitle("CAN Sniffer — passive receive-only")

        self._thread: Optional[QThread] = None
        self._worker: Optional[CaptureWorker] = None
        self._selected_key: Optional[str] = None
        self._follow_trace = True
        #: Tracked rather than read back from widget visibility: isVisible() is
        #: False for every child until the window itself is shown, so asking Qt
        #: reports "collapsed" for a sidebar that is merely not on screen yet.
        self._sidebar_collapsed = False
        self._seen_channels: set = set()
        self._last_received = 0
        self._last_rate_at = time.monotonic()

        self._build_ui()
        self._build_shortcuts()
        self._apply_config_to_widgets()

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start(500)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_top_bar())

        self.banner = QLabel()
        self.banner.setObjectName("Banner")
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        banner_wrap = QWidget()
        banner_layout = QVBoxLayout(banner_wrap)
        banner_layout.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, 0)
        banner_layout.addWidget(self.banner)
        self.banner_wrap = banner_wrap
        banner_wrap.setVisible(False)
        outer.addWidget(banner_wrap)

        # Sidebar: the icon rail is always present; the Messages panel beside
        # it is what collapses away.
        self.sidebar = QWidget()
        sidebar_layout = QHBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        self.nav = NavRail(
            [
                ("list", "Messages", "One row per CAN ID, with rate and the latest payload"),
                ("stream", "Trace", "Every received frame in arrival order"),
            ],
            self.theme,
        )
        self.nav.changed.connect(self._on_nav_clicked)
        sidebar_layout.addWidget(self.nav)

        self.browser_panel = self._build_browser()
        self.browser_panel.setMinimumWidth(330)
        sidebar_layout.addWidget(self.browser_panel, 1)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.sidebar)

        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_MD)
        workspace_layout.setSpacing(0)
        self.interpret_view = InterpretView(self.config, self.theme)
        self.interpret_view.setMinimumWidth(520)
        workspace_layout.addWidget(self.interpret_view, 1)
        self.splitter.addWidget(workspace)

        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.splitterMoved.connect(self._on_splitter_moved)
        outer.addWidget(self.splitter, 1)

        self.setCentralWidget(central)
        self._build_status_bar()

        size = self.config.get("ui.window", {}) or {}
        self.resize(int(size.get("width", 1640)), int(size.get("height", 940)))

        self._min_sidebar = self.browser_panel.minimumWidth() + self.nav.WIDTH
        self._sidebar_width = max(
            self._min_sidebar, int(self.config.get("ui.sidebar_width", 470))
        )
        self.set_sidebar_collapsed(
            bool(self.config.get("ui.sidebar_collapsed", False)), remember=False
        )

    # ------------------------------------------------------------------
    # sidebar
    # ------------------------------------------------------------------

    def set_sidebar_collapsed(self, collapsed: bool, remember: bool = True) -> None:
        """Collapse to the icon rail, or restore the remembered width."""
        collapsed = bool(collapsed)
        if not collapsed and not self._sidebar_collapsed:
            # Keep the width the user dragged to before collapsing.
            self._remember_width()

        self._sidebar_collapsed = collapsed
        self.browser_panel.setVisible(not collapsed)
        self.nav.set_collapsed(collapsed)
        self.nav.update_hints(collapsed)

        if collapsed:
            self.sidebar.setFixedWidth(self.nav.WIDTH)
            # Hand the reclaimed width to the workspace explicitly; the
            # splitter keeps its stored sizes otherwise.
            total = max(self.splitter.width(), self.nav.WIDTH + 520)
            self.splitter.setSizes([self.nav.WIDTH, total - self.nav.WIDTH])
        else:
            self.sidebar.setMinimumWidth(self._min_sidebar)
            self.sidebar.setMaximumWidth(16777215)
            total = max(self.splitter.width(), self._sidebar_width + 520)
            self.splitter.setSizes(
                [self._sidebar_width, max(520, total - self._sidebar_width)]
            )
        if remember:
            self.config.set("ui.sidebar_collapsed", collapsed)

    def _remember_width(self) -> None:
        sizes = self.splitter.sizes()
        if sizes and sizes[0] >= self._min_sidebar:
            self._sidebar_width = sizes[0]

    def _on_splitter_moved(self, *_args) -> None:
        if not self._sidebar_collapsed:
            self._remember_width()

    def _build_top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(SPACE_LG, SPACE_MD, SPACE_LG, SPACE_MD)
        row.setSpacing(SPACE_SM)

        title_block = QVBoxLayout()
        title_block.setSpacing(0)
        title = QLabel("CAN Sniffer")
        title.setObjectName("AppTitle")
        title_block.addWidget(title)
        row.addLayout(title_block)

        row.addSpacing(SPACE_LG)

        self.start_button = self._bar_button("Start", primary=True, slot=self.start_capture,
                                             tip="Open the configured source and begin "
                                                 "receiving  (F5)")
        self.stop_button = self._bar_button("Stop", slot=self.stop_capture,
                                            tip="Close the source  (F6)")
        self.stop_button.setEnabled(False)
        self.pause_button = self._bar_button(
            "Pause", slot=self._on_pause, checkable=True,
            tip="Stop adding frames to the views; reception continues  (F7)",
        )
        for button in (self.start_button, self.stop_button, self.pause_button):
            row.addWidget(button)

        row.addStretch(1)

        self.source_chip = Chip("no source", "muted", self.theme)
        row.addWidget(self.source_chip)
        row.addSpacing(SPACE_SM)

        for text, slot, tip in (
            ("Open capture", self._open_capture, "Load a capture file for offline playback"),
            ("Capture filters", self._edit_filters,
             "Choose which frames are received. To hide rows you have already "
             "captured, use the filter bar above the table."),
            ("Scaled values", self._edit_signals,
             "Turn raw bytes into physical values: raw × scale + offset"),
            ("Settings", self._edit_settings, "Source, capture, display and raw configuration"),
        ):
            row.addWidget(self._bar_button(text, slot=slot, ghost=True, tip=tip))
        return bar

    def _bar_button(self, text: str, slot=None, primary: bool = False, ghost: bool = False,
                    checkable: bool = False, tip: str = "") -> QPushButton:
        button = QPushButton(text)
        if primary:
            button.setObjectName("Primary")
        elif ghost:
            button.setObjectName("Ghost")
        button.setCheckable(checkable)
        button.setCursor(Qt.PointingHandCursor)
        if tip:
            button.setToolTip(tip)
        if slot is not None:
            if checkable:
                button.toggled.connect(slot)
            else:
                button.clicked.connect(slot)
        return button

    def _build_browser(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_MD)
        layout.setSpacing(SPACE_SM)

        header = QHBoxLayout()
        header.setSpacing(SPACE_SM)
        self.section_label = SectionLabel("Messages", self.theme)
        header.addWidget(self.section_label)
        header.addStretch(1)
        layout.addLayout(header)

        self.filter_bar = FilterBar(self.theme)
        self.filter_bar.changed.connect(self._on_filter_changed)
        layout.addWidget(self.filter_bar)

        self.id_model = IdTableModel(
            self.theme, int(self.config.get("interpret.bit_window", 512)), self
        )
        self.id_proxy = IdFilterProxy(self)
        self.id_proxy.setSourceModel(self.id_model)

        self.id_view = QTableView()
        self.id_view.setModel(self.id_proxy)
        self._configure_table(self.id_view, payload_column=6)
        self.id_view.setSortingEnabled(True)
        # Without this the header starts on descending, which reads as random.
        self.id_view.sortByColumn(0, Qt.AscendingOrder)
        self.id_view.setItemDelegateForColumn(
            6, ByteHighlightDelegate(self.theme, self.id_view)
        )
        self.id_view.selectionModel().selectionChanged.connect(self._on_id_selection)

        self.trace_model = TraceTableModel(
            self.theme, int(self.config.get("capture.max_frames_retained", 200000)), self
        )
        self.trace_view = QTableView()
        self.trace_view.setModel(self.trace_model)
        self._configure_table(self.trace_view, payload_column=5)
        self.trace_view.selectionModel().selectionChanged.connect(self._on_trace_selection)
        self.trace_view.verticalScrollBar().valueChanged.connect(self._on_trace_scrolled)

        self.browser_stack = QStackedWidget()
        self.browser_stack.addWidget(self.id_view)
        self.browser_stack.addWidget(self.trace_view)
        layout.addWidget(self.browser_stack, 1)

        footer = QHBoxLayout()
        self.browser_count = QLabel("")
        self.browser_count.setObjectName("Muted")
        footer.addWidget(self.browser_count)
        footer.addStretch(1)
        layout.addLayout(footer)
        return panel

    def _configure_table(self, view: QTableView, payload_column: int) -> None:
        view.setSelectionBehavior(QAbstractItemView.SelectRows)
        view.setSelectionMode(QAbstractItemView.SingleSelection)
        view.setAlternatingRowColors(True)
        view.setShowGrid(False)
        view.setMouseTracking(True)
        view.setWordWrap(False)
        view.verticalHeader().setVisible(False)
        view.verticalHeader().setDefaultSectionSize(ROW_HEIGHT_COMPACT)
        view.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        header = view.horizontalHeader()
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        # Floor for the stretched payload column: without it the fixed columns
        # eat the whole sidebar and clip even the header text.
        header.setMinimumSectionSize(72)
        # Identity and counters get a width sized to their format, not to the
        # rows currently loaded. ResizeToContents here re-measured up to a
        # thousand rows on every window resize — the dominant cost of resizing
        # once a capture had been running. The payload is a *preview* in this
        # narrow sidebar: it takes whatever width is left and elides, which
        # keeps every other column readable instead of pushing them behind a
        # horizontal scrollbar. The full payload is always shown in the detail
        # panel on the right.
        for column in range(view.model().columnCount()):
            header.setSectionResizeMode(
                column,
                QHeaderView.Stretch if column == payload_column
                else QHeaderView.Interactive,
            )
        self._size_table_columns(view, payload_column)

    def _size_table_columns(self, view: QTableView, payload_column: int) -> None:
        for column, width in enumerate(exemplar_widths(view.model(), self.theme)):
            if column != payload_column:
                view.setColumnWidth(column, width)

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)

        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(SPACE_SM, 2, SPACE_SM, 2)
        row.setSpacing(SPACE_LG)

        self.metric_received = MetricChip("Received", self.theme)
        self.metric_shown = MetricChip("Shown", self.theme)
        self.metric_dropped = MetricChip("Dropped", self.theme, "muted")
        self.metric_ids = MetricChip("IDs", self.theme)
        self.metric_rate = MetricChip("Rate", self.theme, "muted")
        for metric in (self.metric_received, self.metric_shown, self.metric_dropped,
                       self.metric_ids, self.metric_rate):
            row.addWidget(metric)

        row.addStretch(1)

        self.status_message = QLabel("Idle — press Start")
        self.status_message.setObjectName("Caption")
        row.addWidget(self.status_message)

        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        divider.setStyleSheet("color: {};".format(self.theme.hex("border_subtle")))
        row.addWidget(divider)

        self.passive_chip = Chip("receive-only", "success", self.theme)
        row.addWidget(self.passive_chip)

        bar.addPermanentWidget(container, 1)

    def _build_shortcuts(self) -> None:
        for text, sequence, slot in (
            ("Start", "F5", self.start_capture),
            ("Stop", "F6", self.stop_capture),
            ("Clear", "Ctrl+L", self.clear_views),
            ("Focus search", "Ctrl+F", lambda: self.filter_bar.search_box.setFocus()),
        ):
            action = QAction(text, self)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(slot)
            self.addAction(action)

        pause = QAction("Pause", self)
        pause.setShortcut(QKeySequence("F7"))
        pause.triggered.connect(lambda: self.pause_button.toggle())
        self.addAction(pause)

    def _apply_config_to_widgets(self) -> None:
        relative = bool(self.config.get("ui.relative_timestamps", True))
        self.id_model.relative_timestamps = relative
        self.trace_model.relative_timestamps = relative
        self.trace_model.set_max_rows(int(self.config.get("capture.max_frames_retained", 200000)))
        self.id_model.bit_window = max(0, int(self.config.get("interpret.bit_window", 512)))

        delegate = self.id_view.itemDelegateForColumn(6)
        if isinstance(delegate, ByteHighlightDelegate):
            delegate.enabled = bool(self.config.get("ui.highlight_changed_bytes", True))

        self._update_source_chip()

    def _update_source_chip(self) -> None:
        kind = str(self.config.get("source.type", "file"))
        if kind == "file":
            name = os.path.basename(str(self.config.get("source.file.path", "")) or "—")
            self.source_chip.set_text_and_tone("file · {}".format(name), "muted")
        else:
            self.source_chip.set_text_and_tone(
                "live · {} {}".format(
                    self.config.get("source.live.interface", "?"),
                    self.config.get("source.live.channel", ""),
                ).strip(), "accent",
            )

    # ------------------------------------------------------------------
    # capture control
    # ------------------------------------------------------------------

    def start_capture(self) -> None:
        if self._thread is not None:
            return
        try:
            source = build_source(self.config)
        except SourceError as exc:
            QMessageBox.critical(self, "Cannot start capture", str(exc))
            return

        logger = None
        if bool(self.config.get("logging.enabled", False)):
            try:
                logger = FrameLogger(
                    str(self.config.get("logging.directory", "captures")),
                    str(self.config.get("logging.format", "csv")),
                )
            except Exception as exc:
                QMessageBox.warning(self, "Logging disabled", str(exc))

        worker = CaptureWorker(
            source=source,
            filter_set=FilterSet.from_config(self.config.filters),
            refresh_ms=int(self.config.get("capture.ui_refresh_ms", 100)),
            batch_limit=max(1, int(self.config.get("capture.queue_size", 20000)) // 10),
            logger=logger,
        )
        thread = QThread(self)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.framesReady.connect(self._on_frames)
        worker.errorOccurred.connect(self._on_error)
        worker.statusChanged.connect(self.status_message.setText)
        worker.started.connect(self._on_source_started)
        worker.sourceFinished.connect(self._on_source_finished)
        self.batchConsumed.connect(worker.batch_consumed)

        self._worker = worker
        self._thread = thread
        self._last_received = 0
        self._last_rate_at = time.monotonic()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        thread.start()

    def stop_capture(self) -> None:
        if self._worker is not None:
            self._worker.request_stop()
        self.stop_button.setEnabled(False)

    def _teardown_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None
        self._worker = None
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def _on_source_started(self, description: str) -> None:
        self.status_message.setText(description)
        source = getattr(self._worker, "_source", None)
        verified = getattr(source, "passive_verified", True)
        note = getattr(source, "passive_note", "")

        if not verified:
            self.banner.setText(
                "PASSIVE OPERATION NOT VERIFIED — {}. The adapter may acknowledge frames "
                "electrically. This sniffer still never transmits application "
                "traffic.".format(note)
            )
            self.banner.setVisible(True)
            self.banner_wrap.setVisible(True)
            self.passive_chip.set_text_and_tone("not verified", "danger")
        else:
            self.banner.setVisible(False)
            self.banner_wrap.setVisible(False)
            self.passive_chip.set_text_and_tone(
                "listen-only" if note.startswith("passive") else "receive-only", "success"
            )
        if note:
            self.passive_chip.setToolTip(note)
        self._update_source_chip()

    def _on_source_finished(self) -> None:
        # Take the final counters before the worker goes away: a file replayed
        # at full speed can finish before the status timer's first tick.
        self._update_status(final=True)
        self._teardown_thread()
        if not self.status_message.text().startswith("End of capture"):
            self.status_message.setText("Stopped")

    def _on_error(self, message: str) -> None:
        QMessageBox.critical(self, "Capture error", message)
        self.status_message.setText("Error: {}".format(message.splitlines()[0]))

    def _on_pause(self, paused: bool) -> None:
        if self._worker is not None:
            self._worker.set_paused(paused)
        self.pause_button.setText("Resume" if paused else "Pause")

    # ------------------------------------------------------------------
    # frame intake
    # ------------------------------------------------------------------

    def _on_frames(self, frames: List[CanFrame]) -> None:
        try:
            self.id_model.add_frames(frames)
            self.trace_model.add_frames(frames)

            channels = {f.channel for f in frames if f.channel}
            if channels - self._seen_channels:
                self._seen_channels |= channels
                self.filter_bar.known_channels(sorted(self._seen_channels))

            if self._follow_trace and self.browser_stack.currentIndex() == 1:
                self.trace_view.scrollToBottom()

            if self._selected_key is not None:
                for frame in reversed(frames):
                    if frame.key == self._selected_key:
                        self._push_selection(self._selected_key)
                        break
            elif self.id_model.id_count and not self.id_view.currentIndex().isValid():
                self.id_view.selectRow(0)
        finally:
            self.batchConsumed.emit()

    def _push_selection(self, key: str) -> None:
        stats = self.id_model.stats_for_key(key)
        if stats is None:
            return
        self.interpret_view.show_frame(stats.frame, stats, self.id_model.time_base)

    # ------------------------------------------------------------------
    # selection & browsing
    # ------------------------------------------------------------------

    def _on_nav_clicked(self, index: int) -> None:
        """The nav icons also open and close the packet list beside them.

        Without this they look dead whenever the sidebar is collapsed: they
        switch a stack that nobody can see. Clicking a section that is already
        on screen closes the sidebar; clicking any section while it is closed
        opens it on that section.
        """
        if self._sidebar_collapsed:
            self._on_view_changed(index)
            self.set_sidebar_collapsed(False)
        elif index == self.browser_stack.currentIndex():
            self.set_sidebar_collapsed(True)
        else:
            self._on_view_changed(index)

    def _on_view_changed(self, index: int) -> None:
        self.browser_stack.setCurrentIndex(index)
        self.section_label.setText("Messages" if index == 0 else "Trace")
        self.nav.update_hints(self._sidebar_collapsed)
        self._update_match_count()

    def _on_filter_changed(self, display_filter) -> None:
        # The same filter drives both views, so switching sections never
        # silently changes what is being hidden.
        self.id_proxy.set_filter(display_filter)
        self.trace_model.set_filter(display_filter)
        self._update_match_count()

    def _update_match_count(self) -> None:
        if self.browser_stack.currentIndex() == 0:
            self.filter_bar.set_match_count(self.id_proxy.rowCount(),
                                            self.id_model.id_count)
            self.browser_count.setText(
                "{:,} message IDs".format(self.id_model.id_count)
                if self.id_model.id_count else ""
            )
        else:
            self.filter_bar.set_match_count(self.trace_model.rowCount(),
                                            self.trace_model.total_rows)
            self.browser_count.setText(
                "{:,} frames".format(self.trace_model.total_rows)
                if self.trace_model.total_rows else ""
            )

    def _on_id_selection(self, *_args) -> None:
        rows = self.id_view.selectionModel().selectedRows()
        index = rows[0] if rows else self.id_view.currentIndex()
        if not index.isValid():
            return
        key = self.id_proxy.data(self.id_proxy.index(index.row(), 0), KEY_ROLE)
        if not key:
            return
        self._selected_key = key
        self._push_selection(key)

    def _on_trace_selection(self, *_args) -> None:
        row = self.trace_view.currentIndex().row()
        frame = self.trace_model.frame_at(row)
        if frame is None:
            return
        # Picking a trace row pins that exact frame instead of following an ID.
        self._selected_key = None
        self.interpret_view.show_frame(
            frame, self.id_model.stats_for_key(frame.key), self.id_model.time_base
        )

    def _on_trace_scrolled(self, value: int) -> None:
        bar = self.trace_view.verticalScrollBar()
        self._follow_trace = value >= bar.maximum() - 2

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def clear_views(self) -> None:
        self.id_model.clear()
        self.trace_model.clear()
        self._seen_channels.clear()
        self._selected_key = None
        self.interpret_view.show_frame(None)
        self._update_match_count()

    def _open_capture(self) -> None:
        start_dir = os.path.dirname(str(self.config.get("source.file.path", "")) or ".")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open capture file", start_dir,
            "CAN captures (*.asc *.blf *.log *.csv *.trc);;All files (*)",
        )
        if not path:
            return
        if self._thread is not None:
            QMessageBox.information(self, "Capture running", "Stop the capture first.")
            return
        self.config.set("source.type", "file")
        self.config.set("source.file.path", path)
        self.config.save()
        self.clear_views()
        self._update_source_chip()
        self.start_capture()

    def _edit_filters(self) -> None:
        dialog = FilterDialog(self.config.filters, self, self.theme,
                              sample=self.interpret_view.current_frame())
        if dialog.exec() != FilterDialog.Accepted:
            return
        self.config.data["filters"] = dialog.rules_config()
        self.config.save()
        if self._worker is not None:
            self._worker.set_filters(FilterSet.from_config(self.config.filters))
        self.status_message.setText(
            "{} filter rule(s) active — applied to frames received from now on".format(
                len(self.config.filters)
            )
        )

    def _edit_signals(self) -> None:
        # The message on screen is what the preview computes against, so a
        # rule can be checked before it is saved.
        dialog = SignalDialog(self.config.signals, self, self.theme,
                              sample=self.interpret_view.current_frame())
        if dialog.exec() != SignalDialog.Accepted:
            return
        self.config.data["signals"] = dialog.signals_config()
        self.config.save()
        self.interpret_view.refresh()

    def _edit_settings(self) -> None:
        dialog = ConfigDialog(self.config, self)
        if dialog.exec() != ConfigDialog.Accepted:
            return
        previous_fonts = (self.config.get("ui.font_size"), self.config.get("ui.font_family"))
        try:
            self.config.replace(dialog.updated_config())
            self.config.save()
        except Exception as exc:
            QMessageBox.warning(self, "Configuration not saved", str(exc))
            return

        if (self.config.get("ui.font_size"), self.config.get("ui.font_family")) != previous_fonts:
            self.apply_fonts()
        self._apply_config_to_widgets()
        self.interpret_view.reload_from_config()
        if self._worker is not None:
            self._worker.set_filters(FilterSet.from_config(self.config.filters))
        self.status_message.setText(
            "Settings saved — source changes take effect on the next Start"
        )

    def apply_fonts(self) -> None:
        """Re-apply typography in place after a settings change."""
        base = float(self.config.get("ui.font_size", 9) or 9)
        self.theme.set_fonts(
            ui_size=base,
            mono_size=base + 0.5,
            mono_family=str(self.config.get("ui.font_family", "") or ""),
        )
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(self.theme.stylesheet())
            app.setFont(self.theme.ui_font())
        for widget in self.findChildren(QWidget):
            restyle = getattr(widget, "restyle", None)
            if callable(restyle):
                restyle()
        for view, payload_column in ((self.id_view, 6), (self.trace_view, 5)):
            for column in range(view.model().columnCount()):
                delegate = view.itemDelegateForColumn(column)
                invalidate = getattr(delegate, "invalidate_fonts", None)
                if callable(invalidate):
                    invalidate()
            # Column widths come from the font metrics, so they have to be
            # recomputed rather than left at the previous face's sizes.
            self._size_table_columns(view, payload_column)
        self.id_model.layoutChanged.emit()
        self.trace_model.layoutChanged.emit()

    # ------------------------------------------------------------------
    # status & shutdown
    # ------------------------------------------------------------------

    def _update_status(self, final: bool = False) -> None:
        worker = self._worker
        self.metric_ids.set_value("{:,}".format(self.id_model.id_count))
        self._update_match_count()
        if worker is None:
            self.metric_rate.set_value("—", "muted")
            return

        self.metric_received.set_value("{:,}".format(worker.received))
        self.metric_shown.set_value("{:,}".format(worker.accepted))
        self.metric_dropped.set_value(
            "{:,}".format(worker.dropped), "warning" if worker.dropped else "muted"
        )

        if final:
            self.metric_rate.set_value("—", "muted")
            return

        now = time.monotonic()
        elapsed = now - self._last_rate_at
        if elapsed >= 0.4:
            rate = (worker.received - self._last_received) / elapsed
            self.metric_rate.set_value("{:,.0f}/s".format(rate),
                                       "neutral" if rate > 0 else "muted")
            self._last_received = worker.received
            self._last_rate_at = now

        source = getattr(worker, "_source", None)
        total = getattr(source, "total_frames", 0)
        if total:
            self.status_message.setText(
                "Playing back {} — {:.0f}%".format(
                    os.path.basename(getattr(source, "path", "")),
                    getattr(source, "progress", 0.0) * 100,
                )
            )

    def closeEvent(self, event) -> None:
        self.config.set("ui.window", {"width": self.width(), "height": self.height()})
        if self.browser_panel.isVisible():
            self._remember_width()
        self.config.set("ui.sidebar_width", int(self._sidebar_width))
        self.config.set("ui.sidebar_collapsed", not self.browser_panel.isVisible())
        try:
            self.config.save()
        except Exception:
            pass
        if self._worker is not None:
            self._worker.request_stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
        super().closeEvent(event)


__all__ = ["MainWindow"]
