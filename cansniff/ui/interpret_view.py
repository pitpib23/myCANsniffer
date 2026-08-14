"""The payload interpretation panel — the primary workspace.

The payload is split into blocks and every enabled decoder is applied to every
block, so all readings sit side by side and the operator can compare them.

The panel presents data only. It does not rank, score, recommend or conclude:
which reading is correct is the operator's call, and the raw bytes plus every
decoding of them are what that call needs.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..config import Config
from ..interpret import Interpretation, SignalRule, interpret_payload
from ..model import CanFrame, FrameStats
from .columns_popup import ColumnsPopup
from .theme import (
    ROW_HEIGHT, SPACE_LG, SPACE_MD, SPACE_SM, SPACE_XL, SPACE_XS, Theme,
)
from .widgets import (
    CELL_PADDING, MONO_ROLE, TONE_ROLE, ActivityLegend, BitMatrix, Chip,
    EmptyState, InterpretCellDelegate, PayloadStrip, SectionLabel,
    Segmented,
)

_BLOCK_SIZES = [1, 2, 4, 8]

#: Disclosure caption; {} carries the chevron for the open/closed state.
_BITS_LABEL = "{}  Bit activity"

#: QSS gives QHeaderView::section 10px of padding on each side; the rest is
#: headroom so a bold header label never lands on the clipping boundary.
_HEADER_PADDING = 2 * 10 + 8

#: Identity of a block: stable across table rebuilds, unlike a row number.
BlockKey = Tuple[int, int]


def _block_key(word) -> BlockKey:
    return (word.offset, word.length)


class InterpretView(QWidget):
    """Live interpretation of the selected ID's payload."""

    def __init__(self, config: Config, theme: Theme, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.config = config
        self.theme = theme

        self._frame: Optional[CanFrame] = None
        self._stats: Optional[FrameStats] = None
        self._time_base = 0.0
        self._frozen = False
        self._result: Optional[Interpretation] = None
        #: Selection is tracked by block identity, never by row index.
        self._selected_block: Optional[BlockKey] = None
        #: Inputs the last render was built from, to skip identical work.
        self._render_key: Optional[tuple] = None
        self._headers: List[str] = []
        self._columns_popup: Optional[ColumnsPopup] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(SPACE_MD)

        # Card 1: what this message is, and its raw bytes.
        summary = QFrame()
        summary.setObjectName("Panel")
        # A grid, so the collapsible detail below lands in the same column as
        # the payload strip above it and the bit matrix lines up with the byte
        # cells it describes.
        summary_layout = QGridLayout(summary)
        summary_layout.setContentsMargins(SPACE_LG, SPACE_MD, SPACE_LG, SPACE_MD)
        summary_layout.setHorizontalSpacing(SPACE_XL)
        summary_layout.setVerticalSpacing(SPACE_SM)
        summary_layout.setColumnStretch(1, 1)
        self._build_identity(summary_layout)
        self.payload_detail = self._build_payload_detail()
        # Column 1 only, matching the strip exactly. Spanning into the actions
        # column would make the matrix wider than the strip, and then the two
        # scroll ranges differ and the bit columns drift off their bytes.
        summary_layout.addWidget(self.payload_detail, 2, 1)
        root.addWidget(summary)

        # Card 2: the interpretation table, given the rest of the height.
        interpretation = QFrame()
        interpretation.setObjectName("Panel")
        table_layout = QVBoxLayout(interpretation)
        table_layout.setContentsMargins(SPACE_LG, SPACE_MD, SPACE_LG, SPACE_MD)
        table_layout.setSpacing(SPACE_MD)
        table_layout.addWidget(self._build_controls())
        table_layout.addWidget(self._build_body(), 1)
        root.addWidget(interpretation, 1)

        self._sync_controls()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build_identity(self, grid: QGridLayout) -> None:
        """Row 0: identity, the raw bytes, and the panel actions.

        The strip lives on the title line rather than in a section of its own.
        The bytes are the one thing that must always be on screen, and putting
        them here means everything below can collapse away without hiding them.
        """
        self.id_label = QLabel("—")
        self.id_label.setFont(self.theme.mono_font(6.0, bold=True))
        grid.addWidget(self.id_label, 0, 0, Qt.AlignLeft | Qt.AlignVCenter)

        self.strip = PayloadStrip(self.theme)
        self.strip.byteClicked.connect(self._on_byte_clicked)
        self.strip_scroll = QScrollArea()
        self.strip_scroll.setObjectName("PayloadScroll")
        self.strip_scroll.setWidget(self.strip)
        self.strip_scroll.setWidgetResizable(True)
        self.strip_scroll.setFrameShape(QFrame.NoFrame)
        self.strip_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.strip_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.strip_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.strip_scroll.setFixedHeight(self.strip.sizeHint().height() + 14)
        grid.addWidget(self.strip_scroll, 0, 1)

        actions = QHBoxLayout()
        actions.setSpacing(SPACE_SM)
        self.last_seen_label = QLabel("")
        self.last_seen_label.setObjectName("Caption")
        actions.addWidget(self.last_seen_label)

        self.freeze_check = QCheckBox("Freeze")
        self.freeze_check.setToolTip("Stop updating this panel while you read a value")
        self.freeze_check.toggled.connect(self._on_freeze)
        actions.addWidget(self.freeze_check)

        self.copy_button = QPushButton("Copy table")
        self.copy_button.setObjectName("Ghost")
        self.copy_button.setToolTip("Copy the table as tab-separated text")
        self.copy_button.clicked.connect(self._copy_table)
        actions.addWidget(self.copy_button)
        grid.addLayout(actions, 0, 2, Qt.AlignRight | Qt.AlignTop)

        # Row 1: the bit-activity disclosure leads, the identity chips trail.
        # The disclosure sits at the left edge under the strip because that is
        # where the thing it opens appears; the chips are reference detail and
        # read fine parked on the right.
        row = QHBoxLayout()
        row.setSpacing(SPACE_SM)

        self.bits_toggle = QPushButton(_BITS_LABEL.format("▾"))
        self.bits_toggle.setObjectName("Disclosure")
        self.bits_toggle.setCheckable(True)
        self.bits_toggle.setCursor(Qt.PointingHandCursor)
        self.bits_toggle.setToolTip("How often each bit flipped over recent frames")
        self.bits_toggle.toggled.connect(self._on_bits_toggled)
        row.addWidget(self.bits_toggle)

        self.bits_caption = QLabel("")
        self.bits_caption.setObjectName("Muted")
        row.addWidget(self.bits_caption)

        self.bits_legend = ActivityLegend(self.theme)
        row.addWidget(self.bits_legend)

        row.addStretch(1)

        # The selected block's byte range, next to the chips it qualifies.
        self.selection_label = QLabel("")
        self.selection_label.setFont(self.theme.mono_font(0.5, bold=True))
        self.selection_label.setStyleSheet("color: {};".format(self.theme.hex("accent")))
        row.addWidget(self.selection_label)

        self.chip_channel = Chip("", "muted", self.theme)
        self.chip_fmt = Chip("", "muted", self.theme)
        self.chip_len = Chip("", "neutral", self.theme)
        self.chip_dlc = Chip("", "warning", self.theme)
        self.chip_rate = Chip("", "neutral", self.theme)
        self.chip_count = Chip("", "muted", self.theme)
        for chip in (self.chip_channel, self.chip_fmt, self.chip_len,
                     self.chip_dlc, self.chip_rate, self.chip_count):
            chip.setVisible(False)
            row.addWidget(chip)

        grid.addLayout(row, 1, 0, 1, 3)

    def _build_payload_detail(self) -> QWidget:
        """Row 2: everything that collapses away, aligned under the strip."""
        container = QWidget()
        container.setObjectName("PayloadDetail")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_SM)

        self.bit_matrix = BitMatrix(self.theme)
        self.bit_matrix.byteClicked.connect(self._on_byte_clicked)
        self.matrix_scroll = QScrollArea()
        self.matrix_scroll.setObjectName("PayloadScroll")
        self.matrix_scroll.setWidget(self.bit_matrix)
        self.matrix_scroll.setWidgetResizable(True)
        self.matrix_scroll.setFrameShape(QFrame.NoFrame)
        self.matrix_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.matrix_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.matrix_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.matrix_scroll.setFixedHeight(self.bit_matrix.sizeHint().height() + 14)
        # The strip scrolls with it, so the columns stay under their bytes.
        self.matrix_scroll.horizontalScrollBar().valueChanged.connect(
            self.strip_scroll.horizontalScrollBar().setValue
        )
        self.strip_scroll.horizontalScrollBar().valueChanged.connect(
            self.matrix_scroll.horizontalScrollBar().setValue
        )

        layout.addWidget(self.matrix_scroll)
        return container

    def _on_bits_toggled(self, shown: bool) -> None:
        shown = bool(shown)
        self.bits_toggle.setText(_BITS_LABEL.format("▾" if shown else "▸"))
        for widget in (self.matrix_scroll, self.bits_caption, self.bits_legend):
            widget.setVisible(shown)
        self.config.set("ui.show_bit_activity", shown)

    def _build_controls(self) -> QWidget:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_MD)

        size_group = QHBoxLayout()
        size_group.setSpacing(SPACE_SM)
        size_group.addWidget(SectionLabel("Block size", self.theme))
        self.size_segmented = Segmented([str(n) for n in _BLOCK_SIZES], 1)
        self.size_segmented.setToolTip(
            "Bytes per block.\n4 enables the 32-bit and float decoders."
        )
        self.size_segmented.changed.connect(self._on_block_size)
        size_group.addWidget(self.size_segmented)
        row.addLayout(size_group)

        range_group = QHBoxLayout()
        range_group.setSpacing(SPACE_SM)
        range_group.addWidget(SectionLabel("Bytes", self.theme))
        self.range_start = QSpinBox()
        self.range_start.setRange(0, 63)
        self.range_start.setFixedWidth(58)
        self.range_start.setToolTip("First payload byte to decode")
        self.range_start.valueChanged.connect(self._on_range)
        range_group.addWidget(self.range_start)
        dash = QLabel("to")
        dash.setObjectName("Muted")
        range_group.addWidget(dash)
        self.range_end = QSpinBox()
        self.range_end.setRange(0, 63)
        self.range_end.setFixedWidth(58)
        self.range_end.setToolTip("Last payload byte to decode")
        self.range_end.valueChanged.connect(self._on_range)
        range_group.addWidget(self.range_end)
        row.addLayout(range_group)

        row.addStretch(1)

        self.decoder_button = QPushButton("Columns")
        self.decoder_button.setCursor(Qt.PointingHandCursor)
        self.decoder_button.setToolTip(
            "Choose which decodings get a column, and drag to reorder them"
        )
        self.decoder_button.clicked.connect(self._open_columns)
        row.addWidget(self.decoder_button)

        self.columns_badge = QLabel("0")
        self.columns_badge.setObjectName("CountBadge")
        self.columns_badge.setAlignment(Qt.AlignCenter)
        self.columns_badge.setToolTip("Columns currently shown")
        row.addWidget(self.columns_badge)
        return container

    def _open_columns(self) -> None:
        if self._columns_popup is None:
            self._columns_popup = ColumnsPopup(self.config, self.theme, self)
            self._columns_popup.changed.connect(self._on_columns_changed)
        self._columns_popup.show_under(self.decoder_button)

    def _on_columns_changed(self) -> None:
        self.refresh(force=True)

    def _build_body(self) -> QWidget:
        self.stack = QStackedWidget()

        self.empty_state = EmptyState(
            "No message selected",
            "Press Start, then choose a CAN ID on the left to decode its payload.",
            self.theme,
        )
        self.stack.addWidget(self.empty_state)

        self.table = QTableWidget(0, 0)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        # Interactive, not ResizeToContents. ResizeToContents makes the header
        # ask the delegate for a sizeHint on every row of every column — all of
        # them, not just the visible ones — each time the widget is resized or
        # a cell changes. On a 64-byte CAN FD payload that is ~700 font
        # measurements per resize event, and the panel visibly lags. _fill_table
        # sets the widths itself, once, from text it already holds.
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        # Not stretch-last: with many decoder columns there is no spare width,
        # and stretching would clip the final column's header and values
        # instead of letting the view scroll to them.
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setHighlightSections(False)
        self.cell_delegate = InterpretCellDelegate(self.theme, self.table)
        self.table.setItemDelegate(self.cell_delegate)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        self.stack.addWidget(self.table)

        self.stack.setCurrentWidget(self.empty_state)
        return self.stack

    # ------------------------------------------------------------------
    # configuration plumbing
    # ------------------------------------------------------------------

    def _sync_controls(self) -> None:
        widgets = [self.size_segmented, self.range_start, self.range_end,
                   self.bits_toggle]
        for widget in widgets:
            widget.blockSignals(True)

        shown = bool(self.config.get("ui.show_bit_activity", True))
        self.bits_toggle.setChecked(shown)
        self.bits_toggle.setText(_BITS_LABEL.format("▾" if shown else "▸"))
        for widget in (self.matrix_scroll, self.bits_caption, self.bits_legend):
            widget.setVisible(shown)

        block_size = int(self.config.get("interpret.word_size", 2))
        self.size_segmented.set_current(
            _BLOCK_SIZES.index(block_size) if block_size in _BLOCK_SIZES else 1
        )

        first, last = self._configured_range()
        self.range_start.setValue(first)
        self.range_end.setValue(last)

        for widget in widgets:
            widget.blockSignals(False)

    def _configured_range(self) -> Tuple[int, int]:
        """Stored range is half-open; the spin boxes show inclusive indices."""
        stored = self.config.get("interpret.byte_range", [0, 64]) or [0, 64]
        first = max(0, int(stored[0]))
        last = max(first, min(63, int(stored[1]) - 1))
        return first, last

    def _on_block_size(self, index: int) -> None:
        self.config.set("interpret.word_size", _BLOCK_SIZES[index])
        self.refresh(force=True)

    def _on_range(self, _value: int) -> None:
        first = self.range_start.value()
        last = max(first, self.range_end.value())
        if self.range_end.value() != last:
            self.range_end.blockSignals(True)
            self.range_end.setValue(last)
            self.range_end.blockSignals(False)
        # Inclusive in the UI, half-open in the splitter.
        self.config.set("interpret.byte_range", [first, last + 1])
        self.refresh(force=True)

    def _on_freeze(self, frozen: bool) -> None:
        self._frozen = bool(frozen)

    def reload_from_config(self) -> None:
        """Re-sync every widget after the configuration changed elsewhere."""
        self._sync_controls()
        if self._columns_popup is not None:
            self._columns_popup.reload()
        self.refresh(force=True)

    def restyle(self) -> None:
        """Re-apply token-derived styling after a font change."""
        self.id_label.setFont(self.theme.mono_font(5.0, bold=True))
        self.selection_label.setFont(self.theme.mono_font(0.5, bold=True))
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.strip.restyle()
        self.bit_matrix.restyle()
        self.bits_legend.restyle()
        self.strip_scroll.setFixedHeight(self.strip.sizeHint().height() + 14)
        self.matrix_scroll.setFixedHeight(self.bit_matrix.sizeHint().height() + 14)
        # Column widths are derived from the delegate's cached font metrics,
        # so those have to go before the next rebuild measures anything.
        self.cell_delegate.invalidate_fonts()
        self.refresh(force=True)

    # ------------------------------------------------------------------
    # content
    # ------------------------------------------------------------------

    def show_frame(
        self,
        frame: Optional[CanFrame],
        stats: Optional[FrameStats] = None,
        time_base: float = 0.0,
    ) -> None:
        if self._frozen:
            return
        if frame is not None and (self._frame is None or frame.key != self._frame.key):
            # A different message: no block selection carries over.
            self._selected_block = None
        self._frame = frame
        self._stats = stats
        self._time_base = float(time_base)
        self.refresh()

    def current_frame(self) -> Optional[CanFrame]:
        """The message on screen, for anything that needs a live sample."""
        return self._frame

    def _signals_fingerprint(self) -> tuple:
        """Identity of the scale/offset rules, for the render memo.

        Deliberately the contents, not the count. Editing a rule's scale,
        offset or decoder leaves the count unchanged, so a count-based key
        matched, the rebuild was skipped, and the Scaled value column went on
        showing figures from the previous rule.

        Values are stringified so a hand-edited config cannot put something
        unhashable in here and break the panel.
        """
        fingerprint = []
        for raw in self.config.signals:
            if isinstance(raw, dict):
                fingerprint.append(
                    tuple(sorted((str(k), str(v)) for k, v in raw.items()))
                )
            else:
                fingerprint.append(repr(raw))
        return tuple(fingerprint)

    def refresh(self, force: bool = False) -> None:
        frame = self._frame
        if frame is None:
            self._clear()
            return

        render_key = (
            frame.key,
            frame.data,
            int(self.config.get("interpret.word_size", 2)),
            int(self.config.get("interpret.sliding_step", 1)),
            bool(self.config.get("interpret.include_remainder", True)),
            tuple(self.config.get("interpret.byte_range", [0, 64]) or [0, 64]),
            tuple(self.config.enabled_decoders()),
            self._signals_fingerprint(),
        )
        # Identical payload and settings produce an identical table. On a busy
        # bus the same bytes arrive many times a second; re-decoding and
        # rebuilding every cell for an unchanged result is the single most
        # wasteful thing this panel could do.
        if not force and render_key == self._render_key:
            self._update_identity(frame)
            # Bit activity moves even when the payload does not: the window
            # slides forward, so a bit that has stopped flipping fades out.
            self._update_bit_activity(frame)
            return
        self._render_key = render_key

        self._update_identity(frame)
        self.strip.set_payload(
            frame.data, self._stats.changed_mask if self._stats else ()
        )
        self._update_bit_activity(frame)

        rules = SignalRule.parse_all(self.config.signals)
        result = interpret_payload(
            frame.data,
            decoder_keys_enabled=self.config.enabled_decoders(),
            word_size=int(self.config.get("interpret.word_size", 2)),
            sliding_step=int(self.config.get("interpret.sliding_step", 1)),
            include_remainder=bool(self.config.get("interpret.include_remainder", True)),
            byte_range=self.config.get("interpret.byte_range", [0, 64]),
            signals=rules,
            arb_id=frame.arb_id,
            channel=frame.channel,
        )
        self._result = result

        self._fill_table(result)

        if result.words:
            self.stack.setCurrentWidget(self.table)
        else:
            # A valid frame can still yield no blocks — usually because the
            # byte range excludes the whole payload. Say so instead of
            # showing a blank table.
            first, last = self._configured_range()
            self.empty_state.set_text(
                "No blocks in this byte range",
                "This message has {} byte{}, and the range is set to {}-{}. "
                "Widen the range to decode it.".format(
                    len(frame.data), "" if len(frame.data) == 1 else "s", first, last
                ),
            )
            self.stack.setCurrentWidget(self.empty_state)

    def _clear(self) -> None:
        self.id_label.setText("—")
        for chip in (self.chip_len, self.chip_dlc, self.chip_fmt, self.chip_rate):
            chip.setVisible(False)
        self.selection_label.setText("")
        self.strip.set_payload(b"")
        self.strip.set_highlight(None)
        self.bit_matrix.set_payload(b"")
        self.bit_matrix.set_highlight(None)
        self.bits_caption.setText("")
        self.table.setRowCount(0)
        self._result = None
        self._render_key = None
        self._selected_block = None
        self.empty_state.set_text(
            "No message selected",
            "Press Start, then choose a CAN ID in the sidebar to decode its payload.",
        )
        self.stack.setCurrentWidget(self.empty_state)

    def _update_identity(self, frame: CanFrame) -> None:
        self.id_label.setText("0x" + frame.id_hex)

        self.chip_channel.set_text_and_tone("Ch {}".format(frame.channel or "—"), "muted")
        self.chip_channel.setVisible(bool(frame.channel))

        marks = ["29-bit" if frame.is_extended else "11-bit"]
        if frame.is_fd:
            marks.append("CAN FD")
        if frame.is_error_frame:
            marks.append("error")
        if frame.is_remote_frame:
            marks.append("remote")
        self.chip_fmt.set_text_and_tone(" · ".join(marks), "muted")
        self.chip_fmt.setVisible(True)

        size = len(frame.data)
        self.chip_len.set_text_and_tone(
            "{} byte{}".format(size, "" if size == 1 else "s"), "neutral")
        self.chip_len.setVisible(True)

        # DLC only earns a chip when it says something the byte count does not,
        # which on CAN FD it does (DLC 9-15 encode 12..64 bytes).
        show_dlc = frame.dlc != size
        if show_dlc:
            self.chip_dlc.set_text_and_tone("DLC {}".format(frame.dlc), "warning")
        self.chip_dlc.setVisible(show_dlc)

        rate = self._stats.rate_hz if self._stats else 0.0
        if rate > 0:
            text = "{:.1f} Hz".format(rate) if rate >= 1.0 else "{:.2f} Hz".format(rate)
            self.chip_rate.set_text_and_tone(text, "neutral")
        self.chip_rate.setVisible(rate > 0)

        # The frame count moved here from the Messages list, which no longer
        # has the width for it.
        count = self._stats.count if self._stats else 0
        if count:
            self.chip_count.set_text_and_tone("{:,} frames".format(count), "muted")
        self.chip_count.setVisible(bool(count))

        if self._stats and self._stats.count:
            # Same reference point as the Messages list, so the two agree.
            self.last_seen_label.setText(
                "Last seen: {:.2f}s".format(self._stats.last_seen - self._time_base)
            )
        else:
            self.last_seen_label.setText("")

    def _update_bit_activity(self, frame: CanFrame) -> None:
        stats = self._stats
        window = stats.window_frames if stats else 0
        self.bit_matrix.set_payload(
            frame.data, stats.bit_flips if stats else (), window
        )
        self.bits_caption.setText(
            "last {:,} frames".format(window) if window else "needs 2+ frames"
        )

    # ------------------------------------------------------------------
    # table rendering
    # ------------------------------------------------------------------

    def _fill_table(self, result: Interpretation) -> None:
        headers = ["Bytes", "Raw"]
        headers += [d.header for d in result.decoders]
        headers.append("Scaled value")
        self.columns_badge.setText(str(len(result.decoders)))
        # Every column but the last renders monospace, where the pixel width is
        # exactly the character count times one advance. Tracking the longest
        # string per column therefore sizes the table without measuring a
        # single cell; see _apply_column_widths.
        physical_column = len(headers) - 1
        longest = [""] * len(headers)

        # Rebuilding headers resets column widths and scroll position, so only
        # do it when the shape actually changed.
        if headers != self._headers:
            self.table.setColumnCount(len(headers))
            self.table.setHorizontalHeaderLabels(headers)
            self._headers = headers
        if self.table.rowCount() != len(result.words):
            self.table.setRowCount(len(result.words))

        selection_model = self.table.selectionModel()
        selection_model.blockSignals(True)
        # Filling emits a change per cell; without this the view repaints and
        # re-lays-out its way through the whole rebuild.
        self.table.setUpdatesEnabled(False)
        try:
            target_row = -1
            for row, entry in enumerate(result.words):
                word = entry.word
                if _block_key(word) == self._selected_block:
                    target_row = row

                column = 0
                span = word.span
                self._set_cell(row, column, span, mono=True, tone="strong",
                               align=Qt.AlignRight | Qt.AlignVCenter)
                if len(span) > len(longest[column]):
                    longest[column] = span
                column += 1

                raw_hex = word.hex
                self._set_cell(row, column, raw_hex, mono=True, tone="strong")
                if len(raw_hex) > len(longest[column]):
                    longest[column] = raw_hex
                column += 1

                for decoder in result.decoders:
                    text = entry.values.get(decoder.key, "")
                    self._set_cell(
                        row, column, text, mono=True,
                        align=Qt.AlignRight | Qt.AlignVCenter,
                        tone="muted" if text == "—" else "neutral",
                    )
                    if len(text) > len(longest[column]):
                        longest[column] = text
                    column += 1

                self._set_cell(row, column, entry.physical,
                               tone="success" if entry.physical else "muted")
                if len(entry.physical) > len(longest[column]):
                    longest[column] = entry.physical
        finally:
            selection_model.blockSignals(False)
            self.table.setUpdatesEnabled(True)

        self._apply_column_widths(headers, longest, physical_column)
        # Re-select the same block, not the same row number: a rebuild can
        # change how many blocks exist and in what order.
        if target_row >= 0:
            self.table.selectRow(target_row)
        else:
            self.table.clearSelection()
            self._selected_block = None
        self._on_row_selected()

    def _apply_column_widths(self, headers: List[str], longest: List[str],
                             physical_column: int) -> None:
        """Size each column to its widest value, once per rebuild.

        This is the work ``QHeaderView.ResizeToContents`` would otherwise redo
        on every resize event, over every row rather than the widest one.
        """
        header_metrics = QFontMetrics(self.table.horizontalHeader().font())
        for column, title in enumerate(headers):
            mono = column != physical_column
            bold = column < 2          # "Bytes" and "Raw" render strong
            content = self.cell_delegate.text_width(longest[column], mono, bold)
            # _HEADER_PADDING covers the QSS section padding either side.
            width = max(content + 2 * CELL_PADDING + 6,
                        header_metrics.horizontalAdvance(title) + _HEADER_PADDING)
            if self.table.columnWidth(column) != width:
                self.table.setColumnWidth(column, width)

    def _set_cell(self, row: int, column: int, text: str, mono: bool = False,
                  align=Qt.AlignLeft | Qt.AlignVCenter, tone: str = "neutral"
                  ) -> QTableWidgetItem:
        """Update the cell in place, creating it only when it does not exist."""
        item = self.table.item(row, column)
        text = str(text)
        if item is None:
            item = QTableWidgetItem(text)
            item.setData(MONO_ROLE, mono)
            item.setData(TONE_ROLE, tone)
            item.setData(Qt.TextAlignmentRole, int(align))
            self.table.setItem(row, column, item)
            return item

        if item.text() != text:
            item.setText(text)
        if item.data(TONE_ROLE) != tone:
            item.setData(TONE_ROLE, tone)
        if item.data(MONO_ROLE) != mono:
            item.setData(MONO_ROLE, mono)
        return item

    # ------------------------------------------------------------------
    # interaction
    # ------------------------------------------------------------------

    def _on_row_selected(self) -> None:
        row = self.table.currentRow()
        rows = self.table.selectionModel().selectedRows() if self.table.model() else []
        if rows:
            row = rows[0].row()

        if self._result is None or not (0 <= row < len(self._result.words)) or not rows:
            self._selected_block = None
            self.strip.set_highlight(None)
            self.bit_matrix.set_highlight(None)
            self.selection_label.setText("")
            return

        word = self._result.words[row].word
        self._selected_block = _block_key(word)
        self.strip.set_highlight(word.first_index, word.length, word.span)
        self.bit_matrix.set_highlight(word.first_index, word.length)
        self.selection_label.setText(
            "byte {}".format(word.span) if word.length == 1
            else "bytes {}".format(word.span)
        )

    def _on_byte_clicked(self, index: int) -> None:
        """Clicking a byte selects the first block that starts at it."""
        if self._result is None:
            return
        for row, entry in enumerate(self._result.words):
            if entry.word.first_index == index:
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, 0))
                return

    def _copy_table(self) -> None:
        if self.table.rowCount() == 0:
            return
        lines = ["\t".join(
            self.table.horizontalHeaderItem(c).text()
            for c in range(self.table.columnCount())
        )]
        for row in range(self.table.rowCount()):
            values = []
            for column in range(self.table.columnCount()):
                item = self.table.item(row, column)
                values.append(item.text() if item else "")
            lines.append("\t".join(values))
        QApplication.clipboard().setText("\n".join(lines))
