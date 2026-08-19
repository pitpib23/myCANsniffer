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
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..analysis import dbc as dbc_status
from ..analysis.series import SeriesCache, block_series, dbc_series
from ..analysis.stats import RangeStateCache
from ..config import Config
from ..interpret import (
    DECODERS, Interpretation, interpret_payload,
    numeric_decoder_keys,
)
from ..model import CanFrame, FrameStats
from .plot_view import SignalPlot
from .columns_popup import ColumnsPopup
from .theme import (
    ROW_HEIGHT, SPACE_LG, SPACE_MD, SPACE_SM, SPACE_XL, SPACE_XS, Theme,
)
from .widgets import (
    CELL_PADDING, MONO_ROLE, TONE_ROLE, ActivityLegend, BitMatrix, Chip,
    CurrentPageStack, EmptyState, InterpretCellDelegate, PayloadStrip,
    SectionLabel, Segmented, scrollable,
)

_BLOCK_SIZES = [1, 2, 4, 8]

#: Plot time windows, as (label, seconds). 0 means the whole retained capture.
#: The default is a minute: a long capture spans hundreds of seconds, and drawn
#: end to end every signal collapses into a band with no readable detail.
_PLOT_WINDOWS = [("10s", 10.0), ("30s", 30.0), ("60s", 60.0),
                 ("5m", 300.0), ("All", 0.0)]
_DEFAULT_PLOT_WINDOW = 2

#: Reading offered first for each block width — the obvious one, not the first
#: one that happens to be registered.
_DEFAULT_PLOT_DECODERS = {1: "u8", 2: "u16_be", 4: "u32_be", 8: "u64_be"}

#: Disclosure caption; {} carries the chevron for the open/closed state.
_BITS_LABEL = "{}  Bit activity"

#: Analysis modes. Stable identifiers -- MainWindow, tests and the
#: `self.workspace` QStackedWidget all address a page by one of these,
#: never by a position in whichever sub-navigation control happens to be
#: showing it. ISO-TP is deliberately not one of these any more: it is
#: capture-wide, not tied to any one selected message the way these four
#: are, and lives as its own top-level workspace owned directly by
#: MainWindow -- see main_window.py's own top-level mode constants.
BLOCKS, SIGNALS, RANGE, PLOT = range(4)

#: The two primary sections a message/frame is examined under -- exactly
#: two of MainWindow's own top-level nav rail destinations (the third,
#: ISO-TP, has no contextual children of its own here), never a second,
#: independent navigation concept. Every mode belongs to exactly one.
MESSAGES, TRACE = "messages", "trace"

#: Children offered under each section, in the order their own
#: sub-navigation control shows them.
_SECTION_MODES = {
    MESSAGES: (RANGE, PLOT),
    TRACE: (BLOCKS, SIGNALS),
}
_MODE_SECTION = {
    mode: section for section, modes in _SECTION_MODES.items() for mode in modes
}
_MODE_LABELS = {
    RANGE: "Range", PLOT: "Plot",
    BLOCKS: "Blocks", SIGNALS: "Signals",
}
_SECTION_TITLES = {MESSAGES: "Messages", TRACE: "Trace"}

#: Child shown on first entering a section this session, and whenever no
#: other child has been chosen there yet -- see InterpretView.set_section.
#: Range over Blocks for Messages: it is also database-free, and "what value
#: did this message's bytes take across the frames observed" is exactly what
#: the aggregate/message-level section is for. Trace keeps Blocks: decoding
#: one exact frame's payload is what an individual-frame section is for.
_DEFAULT_MODE = {MESSAGES: RANGE, TRACE: BLOCKS}


class _ModeSelector:
    """Facade over InterpretView's two per-section Segmented controls.

    A caller that only cares which analysis child is active -- tests,
    chiefly -- still addresses it with one flat mode constant (BLOCKS ..
    PLOT), the same as when a single flat Segmented held all of them. Which
    of the two section-specific controls is actually showing that mode is
    InterpretView's own business.
    """

    def __init__(self, view: "InterpretView") -> None:
        self._view = view

    def set_current(self, mode: int) -> None:
        self._view._on_workspace_changed(mode)

    def current(self) -> int:
        return self._view._mode


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
        #: Optional analysis inputs. Both may be absent; the panel works raw.
        self._store = None
        self._database = None
        self._decoded = None
        self._pending_plot = None
        #: What the plot is drawing: ("dbc", name) or ("raw", offset, length,
        #: decoder_key). None until a message with a payload is selected.
        self._plot_source: Optional[tuple] = None
        self._range_cache = RangeStateCache()
        self._series_cache = SeriesCache()

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
        #: Every remaining mode here (Blocks/Signals/Range/Plot) reads one
        #: selected message's own payload, so this card -- its identity and
        #: raw bytes -- stays visible throughout. ISO-TP, the one mode that
        #: had no single message to show identity for, is no longer one of
        #: these at all; it is MainWindow's own top-level workspace now.
        self.identity_card = summary

        # Card 2: the analysis workspace. Which children are even on offer
        # depends on the section MainWindow's own Messages/Trace nav is
        # showing -- see set_section, called from there. That stays the
        # window's only primary navigation concept; this card only adds a
        # caption plus a sub-navigation control scoped to that section,
        # rather than presenting five equally-weighted tabs of its own.
        self._section = MESSAGES
        #: Child last active in each section this session, so leaving a
        #: section and coming back returns to where the operator left it
        #: instead of always resetting to the section's default.
        self._last_mode = dict(_DEFAULT_MODE)
        self._mode = _DEFAULT_MODE[MESSAGES]

        interpretation = QFrame()
        interpretation.setObjectName("Panel")
        table_layout = QVBoxLayout(interpretation)
        table_layout.setContentsMargins(SPACE_LG, SPACE_MD, SPACE_LG, SPACE_MD)
        table_layout.setSpacing(SPACE_MD)

        tab_row = QHBoxLayout()
        tab_row.setSpacing(SPACE_SM)

        # Same idiom as "Block size" beside its own Segmented below: a small
        # caption naming what the control to its right belongs to.
        self.section_caption = SectionLabel(_SECTION_TITLES[self._section], self.theme)
        self.section_caption.setToolTip(
            "Which section this analysis belongs to -- set by the Messages/"
            "Trace nav on the left, not chosen here."
        )
        tab_row.addWidget(self.section_caption)

        self.view_tabs_messages = Segmented(
            [_MODE_LABELS[m] for m in _SECTION_MODES[MESSAGES]], 0)
        self.view_tabs_messages.setToolTip(
            "Range: what value each byte actually took across every frame "
            "observed for this message.\n"
            "Plot: how a value moves over time."
        )
        self.view_tabs_trace = Segmented(
            [_MODE_LABELS[m] for m in _SECTION_MODES[TRACE]], 0)
        self.view_tabs_trace.setToolTip(
            "Blocks: every decoding of every block of this exact frame's "
            "payload.\n"
            "Signals: named values, when a database is loaded."
        )
        for section, control in ((MESSAGES, self.view_tabs_messages),
                                  (TRACE, self.view_tabs_trace)):
            control.changed.connect(
                lambda local, section=section: self._on_workspace_changed(
                    _SECTION_MODES[section][local]))

        # CurrentPageStack, not a plain QStackedWidget: Messages' control has
        # three buttons and Trace's has two, and only the active section's
        # own children should ever take up room here -- a Signals or Blocks
        # button must never appear while Messages is the active section.
        self.view_tabs_stack = CurrentPageStack()
        self.view_tabs_stack.addWidget(self.view_tabs_messages)
        self.view_tabs_stack.addWidget(self.view_tabs_trace)
        tab_row.addWidget(self.view_tabs_stack)

        # The one flat mode constant (BLOCKS .. PLOT) is the stable thing
        # callers address; which of the two Segmented controls above is
        # actually showing it is this facade's own business -- see
        # _ModeSelector.
        self.view_tabs = _ModeSelector(self)

        tab_row.addStretch(1)
        self.workspace_note = QLabel("")
        self.workspace_note.setObjectName("Muted")
        # Ignored, not the default Preferred: its text is a per-mode status
        # line (frame counts, decode status, ...) with no natural upper
        # bound, and a plain QLabel's minimumSizeHint is its full, unwrapped
        # text -- left at the default, a long note silently made the whole
        # tab row (and everything containing it, up to the top-level window)
        # demand however much width THAT particular mode's text happened to
        # need. Same idiom as isotp_view's own evidence_reason label.
        self.workspace_note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        tab_row.addWidget(self.workspace_note)
        table_layout.addLayout(tab_row)

        self.controls = self._build_controls()
        table_layout.addWidget(self.controls)

        # CurrentPageStack, not a plain QStackedWidget: this switcher's pages
        # (Blocks/Signals/Range/Plot) differ substantially in their own
        # minimum footprint, and a plain QStackedWidget reserves room for
        # the *largest* page even while a smaller one is showing — see
        # widgets.CurrentPageStack for why that matters for the top-level
        # window's own minimum size.
        self.workspace = CurrentPageStack()
        self.workspace.addWidget(self._build_body())        # 0 Blocks
        self.workspace.addWidget(self._build_signals())      # 1 Signals
        self.workspace.addWidget(self._build_range())        # 2 Range
        self.workspace.addWidget(self._build_plot())         # 3 Plot
        table_layout.addWidget(self.workspace, 1)
        root.addWidget(interpretation, 1)

        self._sync_controls()
        # Bring the workspace page, the Blocks-only controls' visibility and
        # the sub-navigation's own checked button into step with the
        # Messages/Range default set above -- construction only adds the
        # widgets, it does not otherwise activate any of them.
        self._on_workspace_changed(self._mode)

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

        # "Hold", not "Freeze": the top bar already has Pause, which stops the
        # whole application updating. This one is narrower — it pins *this
        # panel* to the message on screen while the tables keep filling — and
        # two controls both called some variant of "freeze" left no way to tell
        # which one was in effect.
        self.freeze_check = QCheckBox("Hold")
        self.freeze_check.setToolTip(
            "Keep this panel on the message shown now, while the tables on the "
            "left carry on updating.\n"
            "To freeze every view at once, use Pause in the top bar."
        )
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
        # Ignored: whether this shows text at all -- and how much -- depends
        # on the active analysis mode (Blocks/Plot bracket a byte range here;
        # Range/Signals leave it empty). Left at the default Preferred
        # policy, that made this whole card's minimum width -- and so the
        # top-level window's -- shift by however wide the current bracket
        # label happens to be, purely from switching modes. Same idiom as
        # workspace_note below.
        self.selection_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
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

    def _plain_table(self, headers: List[str]) -> QTableWidget:
        """A read-only table styled like the interpretation table."""
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        header = table.horizontalHeader()
        header.setHighlightSections(False)
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.Interactive)
        return table

    def _build_signals(self) -> QWidget:
        self.signals_stack = QStackedWidget()
        self.signals_empty = EmptyState(
            "No signal database loaded",
            "Load a .dbc from the DBC button to see named signals. "
            "Everything else in this panel works without one.",
            self.theme,
        )
        self.signals_stack.addWidget(self.signals_empty)

        self.signals_table = self._plain_table(
            ["Signal", "Value", "Unit", "Raw", "Notes"])
        self.signals_table.itemSelectionChanged.connect(self._on_signal_selected)
        self.signals_stack.addWidget(self.signals_table)
        return self.signals_stack

    def _build_range(self) -> QWidget:
        self.range_stack = QStackedWidget()
        self.range_empty = EmptyState(
            "No samples yet",
            "Range State reports what value each byte actually took across "
            "the frames observed for this message.",
            self.theme,
        )
        self.range_stack.addWidget(self.range_empty)
        self.range_table = self._plain_table(
            ["Byte", "Min", "Max", "Distinct", "Samples", "Behaviour"])
        self.range_stack.addWidget(self.range_table)
        return self.range_stack

    def _build_plot(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_SM)

        # Row 1: what to plot, read how, over how long.
        chooser = QHBoxLayout()
        chooser.setSpacing(SPACE_MD)

        size_group = QHBoxLayout()
        size_group.setSpacing(SPACE_SM)
        size_group.addWidget(SectionLabel("Block size", self.theme))
        self.plot_size = Segmented([str(n) for n in _BLOCK_SIZES], 1)
        self.plot_size.setToolTip(
            "Bytes per block. Wider blocks unlock the wider decoders."
        )
        self.plot_size.changed.connect(self._on_plot_block_size)
        size_group.addWidget(self.plot_size)
        chooser.addLayout(size_group)

        read_group = QHBoxLayout()
        read_group.setSpacing(SPACE_SM)
        read_group.addWidget(SectionLabel("Read as", self.theme))
        self.plot_decoder = QComboBox()
        self.plot_decoder.setMinimumWidth(150)
        self.plot_decoder.setToolTip(
            "How to turn the selected block's bytes into a number.\n"
            "Only decoders that fit the block width are offered."
        )
        self.plot_decoder.currentIndexChanged.connect(self._on_plot_decoder)
        read_group.addWidget(self.plot_decoder)
        chooser.addLayout(read_group)

        self.plot_signal_label = SectionLabel("Signal", self.theme)
        self.plot_signal = QComboBox()
        self.plot_signal.setMinimumWidth(180)
        self.plot_signal.setToolTip(
            "A named signal from the loaded database. Choosing one plots it "
            "instead of a raw block."
        )
        self.plot_signal.currentIndexChanged.connect(self._on_plot_signal)
        # Also on activated: the combo is a *source* selector, so choosing the
        # entry that is already current still has to switch the plot away from
        # a raw block. currentIndexChanged alone does not fire for that.
        self.plot_signal.activated.connect(self._on_plot_signal)
        chooser.addWidget(self.plot_signal_label)
        chooser.addWidget(self.plot_signal)

        chooser.addStretch(1)

        window_group = QHBoxLayout()
        window_group.setSpacing(SPACE_SM)
        window_group.addWidget(SectionLabel("Window", self.theme))
        self.plot_window = Segmented([label for label, _s in _PLOT_WINDOWS],
                                     _DEFAULT_PLOT_WINDOW)
        self.plot_window.setToolTip(
            "How much of the capture to draw, counted back from the newest "
            "frame of this message."
        )
        self.plot_window.changed.connect(self._on_plot_window)
        window_group.addWidget(self.plot_window)
        chooser.addLayout(window_group)
        layout.addLayout(chooser)

        # No row of block buttons here. The payload strip at the top of the
        # panel is already a row of byte cells that highlights a selected
        # block, and duplicating it below the toolbar meant two controls for
        # one choice — with the real bytes shown only in the one further away.
        # Clicking the strip picks the block; see _on_byte_clicked.
        self.plot_hint = QLabel(
            "Click a byte in the payload above to plot the block starting there.")
        self.plot_hint.setObjectName("Muted")
        layout.addWidget(self.plot_hint)

        self.plot_warning = QLabel("")
        self.plot_warning.setWordWrap(True)
        self.plot_warning.setStyleSheet(
            "color: {};".format(self.theme.hex("warning")))
        layout.addWidget(self.plot_warning)

        self.plot = SignalPlot(self.theme)
        layout.addWidget(self.plot, 1)
        # scrollable(), not a bare container: Plot's chooser row has a real,
        # largely fixed minimum width of its own that must never propagate
        # through self.workspace (a CurrentPageStack -- see widgets.py) up to
        # the top-level window's own minimum size. See scrollable()'s own
        # docstring for the maximized/fullscreen bug this prevents.
        return scrollable(container)

    # ------------------------------------------------------------------
    # analysis wiring
    # ------------------------------------------------------------------

    def set_frame_store(self, store) -> None:
        """Attach the shared frame index the analysis workspaces read from."""
        self._store = store
        self._refresh_workspace()

    def set_database(self, database) -> None:
        """Attach or clear the signal database. Never affects raw display."""
        self._database = database
        self._range_cache.clear()
        self._series_cache.clear()
        self._decoded = None
        self._render_key = None          # decoded columns may now differ
        self.refresh(force=True)

    def _tabs_for(self, section: str) -> Segmented:
        return self.view_tabs_messages if section == MESSAGES else self.view_tabs_trace

    def _sync_section_ui(self) -> None:
        self.section_caption.setText(_SECTION_TITLES[self._section])
        self.view_tabs_stack.setCurrentWidget(self._tabs_for(self._section))

    def set_section(self, section: str) -> None:
        """Switch the primary section, mirroring MainWindow's own Messages/
        Trace nav -- the only thing that decides which analysis children are
        even offered, so there is never a second, disagreeing nav concept.

        Restores whichever child was last active in that section this
        session, defaulting to Range (Messages) or Blocks (Trace) the first
        time -- see _DEFAULT_MODE.
        """
        if section not in _SECTION_MODES:
            return
        self._on_workspace_changed(self._last_mode.get(section, _DEFAULT_MODE[section]))

    def current_section(self) -> str:
        return self._section

    def current_mode(self) -> int:
        return self._mode

    def _on_workspace_changed(self, mode: int) -> None:
        """Activate one analysis child.

        The single place that changes what is on screen: the workspace page,
        the Blocks-only controls' visibility, and -- when ``mode`` belongs to
        the other section -- the active section itself, so this can never
        leave a child showing under the wrong parent's caption. Reached from
        a real click on either sub-navigation control, from set_section's own
        default/remembered choice, and directly by tests.
        """
        section = _MODE_SECTION[mode]
        if section != self._section:
            self._section = section
            self._sync_section_ui()
        self._last_mode[section] = mode
        self._mode = mode

        tabs = self._tabs_for(section)
        blocked = tabs.blockSignals(True)
        tabs.set_current(_SECTION_MODES[section].index(mode))
        tabs.blockSignals(blocked)

        self.workspace.setCurrentIndex(mode)
        # Block size and byte range only mean anything to the Blocks table.
        self.controls.setVisible(mode == BLOCKS)
        self._refresh_workspace()
        # The strip's bracket belongs to whichever workspace is using it as a
        # selector. Leaving the plot's block bracketed after switching back to
        # the table would point at a row that is not selected.
        if mode == BLOCKS:
            self._on_row_selected()
        elif mode != PLOT:
            self.strip.set_highlight(None)
            self.bit_matrix.set_highlight(None)
            self.selection_label.setText("")

    def _window_for_selection(self):
        if self._store is None or self._frame is None:
            return None
        return self._store.window_for_key(self._frame.key)

    def _refresh_workspace(self) -> None:
        index = self._mode
        if index == SIGNALS:
            self._refresh_signals()
        elif index == RANGE:
            self._refresh_range()
        elif index == PLOT:
            self._reload_plot_choices()

    # -- signals ---------------------------------------------------------

    def _refresh_signals(self) -> None:
        frame = self._frame
        if self._database is None:
            self.signals_empty.set_text(
                "No signal database loaded",
                "Load a .dbc from the DBC button to see named signals. "
                "Everything else in this panel works without one.",
            )
            self.signals_stack.setCurrentWidget(self.signals_empty)
            self.workspace_note.setText("")
            return
        if frame is None:
            self.signals_stack.setCurrentWidget(self.signals_empty)
            return

        # Decoded on demand for the selected frame only. Decoding a whole
        # retained capture costs seconds, and nothing here needs it.
        decoded = self._database.decode(frame)
        self._decoded = decoded

        if not decoded.ok:
            reason = {
                dbc_status.NOT_IN_DATABASE:
                    "0x{} is not described by {}. The raw frame above is "
                    "unaffected.".format(frame.id_hex, self._database.name),
                dbc_status.LENGTH_MISMATCH:
                    "This frame does not match the database's definition: {}."
                    .format(decoded.detail),
                dbc_status.DECODE_FAILED:
                    "The database describes 0x{} as {}, but this frame did not "
                    "decode: {}".format(frame.id_hex, decoded.name, decoded.detail),
            }.get(decoded.status, decoded.detail)
            self.signals_empty.set_text(
                decoded.name or "Not decoded", reason)
            self.signals_stack.setCurrentWidget(self.signals_empty)
            self.workspace_note.setText(decoded.status.replace("-", " "))
            return

        table = self.signals_table
        table.setRowCount(len(decoded.signals))
        for row, signal in enumerate(decoded.signals):
            notes = []
            if signal.is_multiplexer:
                notes.append("multiplexor")
            if signal.choice:
                notes.append("enumerated")
            if signal.comment:
                notes.append(signal.comment)
            values = [
                signal.name,
                signal.choice or self._number(signal.value),
                signal.unit,
                "" if signal.raw is None else self._number(signal.raw),
                " · ".join(notes),
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(str(text))
                item.setData(MONO_ROLE, column in (1, 3))
                item.setData(TONE_ROLE, "strong" if column == 1 else "neutral")
                table.setItem(row, column, item)
        table.resizeColumnsToContents()
        self.signals_stack.setCurrentWidget(table)
        self.workspace_note.setText("{} · {} signals".format(
            decoded.name, len(decoded.signals)))

    @staticmethod
    def _number(value) -> str:
        if isinstance(value, float):
            return "{:g}".format(value)
        return str(value)

    def _on_signal_selected(self) -> None:
        """Selecting a signal offers it to the plot without switching away."""
        rows = self.signals_table.selectionModel().selectedRows()
        if not rows or self._decoded is None:
            return
        index = rows[0].row()
        if 0 <= index < len(self._decoded.signals):
            self._pending_plot = ("dbc", self._decoded.signals[index].name)

    # -- range state -----------------------------------------------------

    def _refresh_range(self) -> None:
        window = self._window_for_selection()
        if window is None or not window:
            self.range_empty.set_text(
                "No samples yet",
                "Range State reports what value each byte actually took "
                "across the frames observed for this message.",
            )
            self.range_stack.setCurrentWidget(self.range_empty)
            self.workspace_note.setText("")
            return

        state = self._range_cache.get(window, self._frame.key)
        table = self.range_table
        table.setRowCount(len(state.bytes))
        for row, stat in enumerate(state.bytes):
            behaviour = "constant" if stat.constant else "{} of 256 values".format(
                stat.distinct)
            if stat.samples < state.frames:
                behaviour += "  ·  absent from {} frames".format(
                    state.frames - stat.samples)
            values = [str(stat.index), str(stat.minimum), str(stat.maximum),
                      str(stat.distinct), "{:,}".format(stat.samples), behaviour]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(MONO_ROLE, column < 5)
                item.setData(TONE_ROLE, "muted" if stat.constant else "neutral")
                if column < 5:
                    item.setData(Qt.TextAlignmentRole,
                                 int(Qt.AlignRight | Qt.AlignVCenter))
                table.setItem(row, column, item)
        table.resizeColumnsToContents()
        self.range_stack.setCurrentWidget(table)

        note = "{:,} frames".format(state.frames)
        if state.varying_length:
            note += "  ·  payload length varies: {}".format(
                ", ".join("{}B x{}".format(k, v)
                          for k, v in sorted(state.lengths.items())))
        self.workspace_note.setText(note)

    # -- plot ------------------------------------------------------------

    def _plot_block_size(self) -> int:
        stored = int(self.config.get("interpret.plot_block_size", 2) or 2)
        return stored if stored in _BLOCK_SIZES else 2

    def _plot_window_seconds(self) -> float:
        stored = float(self.config.get("interpret.plot_window_s",
                                       _PLOT_WINDOWS[_DEFAULT_PLOT_WINDOW][1]))
        for _label, seconds in _PLOT_WINDOWS:
            if seconds == stored:
                return stored
        return _PLOT_WINDOWS[_DEFAULT_PLOT_WINDOW][1]

    def _plot_window(self):
        """Frames to plot: the tail of this message's history, not all of it.

        Sliced from the store by timestamp rather than by count, so the X axis
        spans the requested number of seconds whatever the message's rate.
        """
        if self._store is None or self._frame is None:
            return None
        key = self._frame.key
        span = self._plot_window_seconds()
        frames = self._store.frames_for(key)
        if span <= 0 or not frames:
            return self._store.window_for_key(key)
        newest = frames[-1].timestamp
        return self._store.window(start=newest - span, end=None, key=key,
                                  label=key)

    def _reload_plot_choices(self) -> None:
        """Rebuild the block strip, the decoder list and the signal list."""
        frame = self._frame
        size = self._plot_block_size()

        blocked = self.plot_size.blockSignals(True)
        self.plot_size.set_current(_BLOCK_SIZES.index(size))
        self.plot_size.blockSignals(blocked)

        blocked = self.plot_window.blockSignals(True)
        seconds = self._plot_window_seconds()
        for index, (_label, value) in enumerate(_PLOT_WINDOWS):
            if value == seconds:
                self.plot_window.set_current(index)
                break
        self.plot_window.blockSignals(blocked)

        # Keep the block being plotted if it still fits this payload; otherwise
        # fall back to one starting at byte 0.
        total = len(frame.data) if frame is not None else 0
        wanted = None
        if total:
            width = min(size, total)
            if self._plot_source is not None and self._plot_source[0] == "raw":
                offset, length = self._plot_source[1], self._plot_source[2]
                if length == width and offset + length <= total:
                    wanted = (offset, length)
            if wanted is None:
                wanted = (0, width)

        self._reload_plot_decoders(wanted[1] if wanted else size)
        self._reload_plot_signals(frame)

        # A pending request from the Signals tab wins.
        if self._pending_plot is not None:
            pending = self._pending_plot
            self._pending_plot = None
            if pending[0] == "dbc":
                position = self.plot_signal.findData(pending)
                if position >= 0:
                    blocked = self.plot_signal.blockSignals(True)
                    self.plot_signal.setCurrentIndex(position)
                    self.plot_signal.blockSignals(blocked)
                    self._plot_source = pending
        elif self._plot_source is None:
            # Nothing chosen yet. A named signal beats an arbitrary byte block
            # when the database describes this message; the blocks stay one
            # click away for everything it does not describe.
            first_signal = self.plot_signal.itemData(0) \
                if self.plot_signal.count() else None
            if first_signal is not None:
                self._plot_source = first_signal
            elif wanted is not None:
                self._plot_source = ("raw", wanted[0], wanted[1],
                                     self.plot_decoder.currentData())
        elif self._plot_source[0] == "raw" and wanted is not None:
            self._plot_source = ("raw", wanted[0], wanted[1],
                                 self.plot_decoder.currentData())

        self._highlight_plot_block()
        self._refresh_plot()

    def _reload_plot_decoders(self, width: int) -> None:
        """Only decoders that can actually read a block this wide.

        Offering the rest would let the operator pick a reading that silently
        produces nothing — the decoder returns no value for a mismatched width,
        and an empty plot looks the same as a signal that never moved.
        """
        combo = self.plot_decoder
        previous = combo.currentData()
        blocked = combo.blockSignals(True)
        combo.clear()
        for key in numeric_decoder_keys():
            decoder = DECODERS[key]
            if decoder.exact_len not in (None, width):
                continue
            combo.addItem(decoder.label, key)
        if combo.count() == 0:                    # no numeric reading fits
            combo.addItem("—", None)
        position = combo.findData(previous)
        if position < 0:
            # Default to a plain unsigned integer of the block's width. The
            # first *registered* numeric decoder is a hex reading, which is a
            # number but a strange thing to plot; whoever wants it can pick it.
            position = combo.findData(_DEFAULT_PLOT_DECODERS.get(width, ""))
        combo.setCurrentIndex(position if position >= 0 else 0)
        combo.blockSignals(blocked)

    def _reload_plot_signals(self, frame: Optional[CanFrame]) -> None:
        combo = self.plot_signal
        previous = combo.currentData()
        blocked = combo.blockSignals(True)
        combo.clear()
        names = []
        if frame is not None and self._database is not None:
            names = self._database.signals_of(frame)
        for name in names:
            combo.addItem(name, ("dbc", name))
        position = combo.findData(previous)
        if position >= 0:
            combo.setCurrentIndex(position)
        combo.blockSignals(blocked)
        # Hidden rather than shown empty: a database that does not describe
        # this message has no signals to offer, and an empty dropdown reads as
        # a fault rather than as an absence.
        for widget in (self.plot_signal_label, self.plot_signal):
            widget.setVisible(bool(names))

    # -- plot controls ---------------------------------------------------

    def _on_plot_block_size(self, index: int) -> None:
        self.config.set("interpret.plot_block_size", _BLOCK_SIZES[index])
        # Keep where the operator was looking: the block still starts at the
        # same byte, it is just read wider or narrower. Resetting to byte 0
        # would throw away the position they had chosen.
        start = (self._plot_source[1]
                 if self._plot_source is not None
                 and self._plot_source[0] == "raw" else 0)
        self._plot_source = None
        self._reload_plot_choices()
        self._pick_plot_block(start)

    def _on_plot_decoder(self, _index: int) -> None:
        if self._plot_source is None or self._plot_source[0] != "raw":
            return
        _kind, offset, length, _previous = self._plot_source
        self._plot_source = ("raw", offset, length,
                             self.plot_decoder.currentData())
        self._refresh_plot()

    def _on_plot_signal(self, _index: int) -> None:
        data = self.plot_signal.currentData()
        if data is None:
            return
        self._plot_source = data
        self._highlight_plot_block()      # a named signal clears the bracket
        self._refresh_plot()

    def _on_plot_window(self, index: int) -> None:
        self.config.set("interpret.plot_window_s", _PLOT_WINDOWS[index][1])
        self._refresh_plot()

    def _refresh_plot(self) -> None:
        window = self._plot_window()
        choice = self._plot_source
        if window is None or not window or choice is None:
            self.plot.clear()
            self.plot_warning.setText("")
            self.workspace_note.setText("")
            return

        key = self._frame.key
        base = self._time_base or None
        if choice[0] == "dbc" and self._database is not None:
            cache_key = ("dbc", key, choice[1]) + window.cache_key
            series = self._series_cache.get(
                lambda: dbc_series(window, self._database, key, choice[1], base),
                cache_key)
        elif choice[0] == "raw":
            _kind, offset, width, decoder = choice
            if decoder is None:
                self.plot.clear()
                self.plot_warning.setText(
                    "No numeric decoder reads a {}-byte block.".format(width))
                self.workspace_note.setText("")
                return
            cache_key = ("raw", key, offset, width, decoder) + window.cache_key
            series = self._series_cache.get(
                lambda: block_series(window, key, offset, width, decoder, base),
                cache_key)
        else:
            self.plot.clear()
            return

        self.plot_warning.setText(self.plot.set_series([series]))
        self.workspace_note.setText(
            "{:,} of {:,} frames carried a value  ·  {}".format(
                len(series), len(window), self._describe_plot_window(window)))

    def _describe_plot_window(self, window) -> str:
        """Say what the window actually covers, not what was asked for.

        A 60-second window over a capture only 4 seconds long covers 4 seconds,
        and reporting the request instead of the reality would misdescribe the
        axis the operator is reading.
        """
        span = self._plot_window_seconds()
        frames = list(window)
        if not frames:
            return "no frames in window"
        covered = frames[-1].timestamp - frames[0].timestamp
        if span <= 0:
            return "whole capture · {:.1f}s".format(covered)
        if covered < span * 0.995:
            return "last {:.0f}s requested · {:.1f}s available".format(
                span, covered)
        return "last {:.0f}s".format(span)

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

    def reset_content(self) -> None:
        """Drop everything derived from frames that no longer exist.

        Used by Clear. The hold is released as part of this: a held panel
        rejects new frames, so leaving it set would silently keep the panel
        blank after the next message arrived — and the operator would have no
        indication why. Releasing it is visible, because the box unticks.
        """
        self.freeze_check.setChecked(False)
        self._frozen = False
        self._range_cache.clear()
        self._series_cache.clear()
        self._decoded = None
        self._stats = None
        self._plot_source = None
        self._pending_plot = None
        self.show_frame(None)

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
            # A different message: no block selection carries over, and neither
            # does the plot's — its blocks and signals belong to that payload.
            self._selected_block = None
            self._plot_source = None
        self._frame = frame
        self._stats = stats
        self._time_base = float(time_base)
        self.refresh()

    def current_frame(self) -> Optional[CanFrame]:
        """The message on screen, for anything that needs a live sample."""
        return self._frame

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
            self._refresh_workspace()
            return
        self._render_key = render_key

        self._update_identity(frame)
        self.strip.set_payload(
            frame.data, self._stats.changed_mask if self._stats else ()
        )
        self._update_bit_activity(frame)

        result = interpret_payload(
            frame.data,
            decoder_keys_enabled=self.config.enabled_decoders(),
            word_size=int(self.config.get("interpret.word_size", 2)),
            sliding_step=int(self.config.get("interpret.sliding_step", 1)),
            include_remainder=bool(self.config.get("interpret.include_remainder", True)),
            byte_range=self.config.get("interpret.byte_range", [0, 64]),
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

        self._refresh_workspace()

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
        self.columns_badge.setText(str(len(result.decoders)))
        # Every column renders monospace, where the pixel width is exactly the
        # character count times one advance. Tracking the longest string per
        # column therefore sizes the table without measuring a single cell;
        # see _apply_column_widths.
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
        finally:
            selection_model.blockSignals(False)
            self.table.setUpdatesEnabled(True)

        self._apply_column_widths(headers, longest)
        # Re-select the same block, not the same row number: a rebuild can
        # change how many blocks exist and in what order.
        if target_row >= 0:
            self.table.selectRow(target_row)
        else:
            self.table.clearSelection()
            self._selected_block = None
        self._on_row_selected()

    def _apply_column_widths(self, headers: List[str], longest: List[str]) -> None:
        """Size each column to its widest value, once per rebuild.

        This is the work ``QHeaderView.ResizeToContents`` would otherwise redo
        on every resize event, over every row rather than the widest one.
        """
        header_metrics = QFontMetrics(self.table.horizontalHeader().font())
        for column, title in enumerate(headers):
            mono = True                # every remaining column is monospace
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
        """Clicking a byte picks the block starting there.

        Which block that is depends on the workspace: the Plot draws one block
        at a time and the strip *is* its selector, while the Blocks table shows
        every block at once and the click moves its selection.
        """
        if self._mode == PLOT:
            self._pick_plot_block(index)
            return
        if self._result is None:
            return
        for row, entry in enumerate(self._result.words):
            if entry.word.first_index == index:
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, 0))
                return

    def _pick_plot_block(self, index: int) -> None:
        """Plot the block of the configured width starting at this byte.

        The offset is pulled back when a full block would run off the end of
        the payload, rather than the block being shortened: the width is what
        the chosen decoder reads, and quietly narrowing it would change the
        reading the operator asked for. The bracket on the strip shows exactly
        which bytes were taken, so the adjustment is visible rather than
        silent.
        """
        if self._frame is None:
            return
        total = len(self._frame.data)
        if total <= 0:
            return
        size = min(self._plot_block_size(), total)
        offset = max(0, min(int(index), total - size))
        self._reload_plot_decoders(size)
        self._plot_source = ("raw", offset, size, self.plot_decoder.currentData())
        self._highlight_plot_block()
        self._refresh_plot()

    def _highlight_plot_block(self) -> None:
        """Mark the plotted block on the strip, the matrix and the caption."""
        source = self._plot_source
        if source is None or source[0] != "raw":
            # A named signal has no single byte range to bracket.
            self.strip.set_highlight(None)
            self.bit_matrix.set_highlight(None)
            self.selection_label.setText("")
            return
        _kind, offset, length, _decoder = source
        span = (str(offset) if length == 1
                else "{}-{}".format(offset, offset + length - 1))
        self.strip.set_highlight(offset, length, span)
        self.bit_matrix.set_highlight(offset, length)
        self.selection_label.setText(
            "byte {}".format(span) if length == 1 else "bytes {}".format(span))

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
