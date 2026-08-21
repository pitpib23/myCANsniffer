"""The ISO-TP investigation page.

Three levels, top to bottom, matching the question an operator on an
undocumented bus actually has:

    which CAN IDs look like ISO-TP, and on what grounds?   (evidence summary)
        -> what did that ID's traffic reassemble to?       (transfers)
            -> which raw frames produced this transfer?    (frames)

The summary is the part that matters. Reassembly alone will happily turn a
periodic ``01 00 03`` sensor message into a tidy list of "complete" single-frame
transfers, which reads as a confirmed diagnostic channel and is not one. The
summary puts the counts that distinguish the two cases — how many frames on
this ID are not ISO-TP-shaped at all, whether any multi-frame sequence ever
completed — in front of the operator before any transfer is shown.

Below the summary, the transfer list and the transfer-detail workspace sit
side by side rather than stacked: comparing many transfers against each
other needs a compact list, investigating *one* transfer needs real width for
its cards, payload and frame tables, and the two needs compete for the same
vertical space if stacked. The transfer list is collapsible (see
IsoTpView._on_transfers_toggled) so the operator can hand its width to the
detail workspace once a transfer worth investigating is found, without losing
which one that was — a `<prev  n / total  next>` navigator in the detail
header keeps browsing possible with the list out of the way.

Model/view throughout: an ID carrying 60,000 single frames produces 60,000
transfer rows, and building a widget per cell for those is not something the
window survives.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QSizePolicy,
    QPlainTextEdit, QSplitter, QStyle, QStyledItemDelegate, QTableView,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..analysis.diagnostics import CorrelationStatus, DiagnosticAnalysis
from ..analysis.isotp import ADDRESSING, COMPLETE, ERROR_STATUSES, IsoTpTransfer, frame_facts
from ..analysis.isotp_survey import (
    EVIDENCE_ORDER, NONE, POSSIBLE, STRONG, WEAK, IsoTpEvidence,
)
from .theme import RADIUS_SM, ROW_HEIGHT_COMPACT, SPACE_LG, SPACE_MD, SPACE_SM, SPACE_XS, Theme
from .widgets import Chip, Divider, EmptyState, SectionLabel

#: Shown where a value genuinely does not apply, rather than left blank so it
#: reads as "not looked at".
DASH = "—"

#: Colour tone per evidence label, for the plain-text ForegroundRole fallback
#: (the Evidence column itself is painted as a badge -- see BadgeDelegate and
#: _evidence_badge_tone -- but the model still reports a sensible tint of its
#: own for anything that reads DisplayRole/ForegroundRole directly). Strong is
#: not "good" and Weak is not "bad" -- they describe how much the capture
#: supports the reading, so the palette runs from muted to accented rather
#: than red to green.
_EVIDENCE_TONE = {
    STRONG: "accent",
    POSSIBLE: "warning",
    WEAK: "text_muted",
    NONE: "text_muted",
}

#: bg / fg / border theme tokens per Chip-style tone -- mirrors
#: widgets.Chip.apply_tone's own mapping exactly, so a badge painted in a
#: table cell (BadgeDelegate) and a Chip widget in the detail panel read as
#: the same visual language throughout this page.
_BADGE_TOKENS = {
    "neutral": ("overlay", "text_secondary", "border"),
    "accent": ("accent_wash", "accent", "accent"),
    "success": ("success_wash", "success", "success"),
    "warning": ("warning_wash", "warning", "warning"),
    "danger": ("danger_wash", "danger", "danger"),
    "muted": ("surface", "text_muted", "border_subtle"),
}

_HELP = {
    "SF": "Single Frame — a whole payload of up to 7 bytes in one CAN frame.",
    "FF": "First Frame — opens a multi-frame transfer and declares its total "
          "length.",
    "CF": "Consecutive Frame — continues a multi-frame transfer, numbered 1..15 "
          "and wrapping.",
    "FC": "Flow Control — the receiver's reply to a First Frame. This tool only "
          "ever observes these; it never sends one.",
    "Other": "Frames on this ID whose first nibble is not an ISO-TP PCI at all. "
             "A high count here is the clearest sign the ID is not ISO-TP.",
    "Evidence": "How much the captured traffic supports reading this ID as "
                "ISO-TP. Single frames alone can never be more than Weak: "
                "almost any short periodic message parses as one.",
}


def _elide(text: str, limit: int = 64) -> str:
    return text if len(text) <= limit else text[:limit] + " …"


def _evidence_badge_tone(evidence_text: str) -> str:
    return {STRONG: "accent", POSSIBLE: "warning", WEAK: "muted",
            NONE: "muted"}.get(evidence_text, "neutral")


def _status_tone(status: str) -> str:
    """Chip-style tone for a transfer's status -- shared by the Status column
    badge and the detail panel's own status/diagnostic chips, so both always
    agree on what a given outcome looks like.
    """
    if status == COMPLETE:
        return "success"
    if status in ERROR_STATUSES:
        return "danger"
    return "warning"    # Incomplete / Timeout -- started but did not finish.


class BadgeDelegate(QStyledItemDelegate):
    """Paints a cell's text as a small rounded pill instead of plain text.

    Used for the two columns whose value is always one of a short, fixed
    vocabulary an operator scans for at a glance -- Evidence in the CAN-ID
    summary, Status in the transfer list -- so they read as a badge the same
    way every other status pill in the application does (see widgets.Chip),
    rather than as plain coloured text unique to these two tables.
    """

    def __init__(self, theme: Theme, tone_for: Callable[[str], str], parent=None):
        super().__init__(parent)
        self._theme = theme
        self._tone_for = tone_for

    def paint(self, painter: QPainter, option, index) -> None:
        text = index.data(Qt.DisplayRole)
        if not text or text == DASH:
            super().paint(painter, option, index)
            return

        theme = self._theme
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, theme.color("selection"))

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        # A cell whose column has been dragged (or, rarely, a status longer
        # than the column was sized for -- see _TRANSFER_WIDTHS' own
        # comment) must never paint outside its own cell: the default
        # QStyledItemDelegate::paint() this replaces already confines
        # itself that way, and a pill/text pair drawn without this could
        # bleed into the neighbouring column instead of just looking snug.
        painter.setClipRect(option.rect)
        bg, fg, border = _BADGE_TOKENS.get(
            self._tone_for(text), _BADGE_TOKENS["neutral"])

        font = option.font
        metrics = QFontMetrics(font)
        text_width = metrics.horizontalAdvance(text)
        # +12 (6px each side): tight but legible -- see _TRANSFER_WIDTHS'
        # own comment for the column widths this was measured against.
        pill_width = min(option.rect.width() - 6, text_width + 12)
        pill_height = min(option.rect.height() - 6, metrics.height() + 8)
        rect = QRectF(
            option.rect.left() + (option.rect.width() - pill_width) / 2.0,
            option.rect.top() + (option.rect.height() - pill_height) / 2.0,
            pill_width, pill_height,
        )
        painter.setPen(QPen(theme.color(border)))
        painter.setBrush(theme.color(bg))
        painter.drawRoundedRect(rect, RADIUS_SM, RADIUS_SM)
        painter.setFont(font)
        painter.setPen(theme.color(fg))
        painter.drawText(rect, Qt.AlignCenter, text)
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        size = super().sizeHint(option, index)
        return QSize(size.width() + 16, size.height())


class EvidenceModel(QAbstractTableModel):
    """One row per CAN ID: the counts, and what they add up to."""

    COLUMNS = ("CAN ID", "Peer", "Evidence", "Transfers", "Complete", "Errors",
               "Frames", "SF", "FF", "CF", "FC", "Other")
    #: Columns whose numbers read better right-aligned and monospaced.
    _NUMERIC = frozenset(range(3, 12))

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._rows: List[IsoTpEvidence] = []

    def set_rows(self, rows: Sequence[IsoTpEvidence]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, position: int) -> Optional[IsoTpEvidence]:
        if 0 <= position < len(self._rows):
            return self._rows[position]
        return None

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return self.COLUMNS[section]
        if role == Qt.ToolTipRole:
            return _HELP.get(self.COLUMNS[section])
        if role == Qt.TextAlignmentRole and section in self._NUMERIC:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = index.column()

        if role == Qt.DisplayRole:
            return self._text(row, column)
        if role == Qt.TextAlignmentRole and column in self._NUMERIC:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.FontRole and (column in self._NUMERIC or column <= 1):
            return self._theme.mono_font(-0.5)
        if role == Qt.ForegroundRole:
            if column == 2:
                return self._theme.color(_EVIDENCE_TONE.get(row.evidence, "text"))
            if column == 5 and row.errors:
                return self._theme.color("warning")
            if column == 11 and row.other and row.other > row.candidates:
                return self._theme.color("warning")
        if role == Qt.ToolTipRole:
            # The evidence label must always be auditable: hovering any cell of
            # the row says what the label was based on.
            return "{}: {}".format(row.evidence, row.why())
        return None

    def _text(self, row: IsoTpEvidence, column: int) -> str:
        if column == 0:
            return row.id_label
        if column == 1:
            return row.peer_label or DASH
        if column == 2:
            return row.evidence
        if column == 3:
            return "{:,}".format(row.transfers)
        if column == 4:
            return "{:,}".format(row.complete)
        if column == 5:
            return "{:,}".format(row.errors) if row.errors else DASH
        if column == 6:
            return "{:,}".format(row.frames)
        if column == 7:
            return "{:,}".format(row.sf) if row.sf else DASH
        if column == 8:
            return "{:,}".format(row.ff) if row.ff else DASH
        if column == 9:
            return "{:,}".format(row.cf) if row.cf else DASH
        if column == 10:
            return "{:,}".format(row.fc) if row.fc else DASH
        return "{:,}".format(row.other) if row.other else DASH

    def sort(self, column: int, order=Qt.AscendingOrder) -> None:
        reverse = order == Qt.DescendingOrder

        def sort_key(row: IsoTpEvidence):
            return (row.id_label, row.peer_label, row.rank, row.transfers,
                    row.complete, row.errors, row.frames, row.sf, row.ff,
                    row.cf, row.fc, row.other)[column]

        self.beginResetModel()
        self._rows.sort(key=sort_key, reverse=reverse)
        self.endResetModel()


class TransferModel(QAbstractTableModel):
    """Transfers for the selected CAN ID.

    Deliberately lean: Addressing and Diagnostic -- useful for one transfer
    at a time, not for scanning a list of them -- live in the detail panel
    (see IsoTpView._update_detail_panel) instead of being columns here. A
    list an operator is scanning to pick a transfer to investigate should
    show only what helps pick one.
    """

    COLUMNS = ("Start", "Status", "Bytes", "Frames", "Duration")
    _NUMERIC = frozenset((0, 2, 3, 4))

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._rows: List[IsoTpTransfer] = []
        self._base = 0.0

    def set_rows(self, rows: Sequence[IsoTpTransfer], time_base: float = 0.0) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self._base = float(time_base)
        self.endResetModel()

    def row_at(self, position: int) -> Optional[IsoTpTransfer]:
        if 0 <= position < len(self._rows):
            return self._rows[position]
        return None

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        position = index.row()
        transfer = self._rows[position]
        column = index.column()

        if role == Qt.DisplayRole:
            if column == 0:
                return "{:.4f}s".format(transfer.first_timestamp - self._base)
            if column == 1:
                return transfer.status
            if column == 2:
                return transfer.bytes_label
            if column == 3:
                return str(transfer.frame_count)
            return "{:.4f}s".format(transfer.duration)
        if role == Qt.TextAlignmentRole and column in self._NUMERIC:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.FontRole and column in self._NUMERIC:
            return self._theme.mono_font(-0.5)
        if role == Qt.ForegroundRole and column == 1:
            # Fallback tint for anything that reads this role directly; the
            # column itself is painted as a badge -- see BadgeDelegate.
            if transfer.complete:
                return self._theme.color("success")
            if transfer.status in ERROR_STATUSES:
                return self._theme.color("danger")
            return self._theme.color("warning")
        if role == Qt.ToolTipRole:
            return transfer.detail or transfer.describe()
        return None


class FrameModel(QAbstractTableModel):
    """The raw frames behind one transfer, with the ISO-TP reading spelled out.

    ``Data`` is what the ISO-TP reading consumed; ``Extra`` is everything else
    in the frame. Nothing is hidden: a frame reading ``01 00 03`` shows data
    ``00`` and extra ``03`` rather than quietly dropping the byte the protocol
    reading has no use for.
    """

    COLUMNS = ("Time", "CAN ID", "DLC", "Type", "PCI", "Seq / Flow",
               "Declared", "Data", "Extra", "Raw frame")

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._facts: List[Any] = []
        self._used: List[int] = []
        self._base = 0.0

    def set_transfer(self, transfer: Optional[IsoTpTransfer],
                     time_base: float = 0.0) -> None:
        self.beginResetModel()
        if transfer is None:
            self._facts, self._used = [], []
        else:
            self._facts = [frame_facts(f) for f in transfer.frames]
            self._used = list(transfer.contributions)
            while len(self._used) < len(self._facts):
                self._used.append(0)
        self._base = float(time_base)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._facts)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return self.COLUMNS[section]
        if role == Qt.ToolTipRole:
            return {
                "PCI": "The Protocol Control Information byte(s) this reading "
                       "consumed.",
                "Seq / Flow": "Consecutive frames show their sequence number; "
                              "flow control shows status, block size and STmin.",
                "Declared": "The payload length this frame claims.",
                "Data": "Payload bytes this frame contributed to the transfer.",
                "Extra": "Bytes in the frame the ISO-TP reading does not "
                         "account for — padding, or a sign the frame is not "
                         "ISO-TP at all. Never discarded.",
                "Raw frame": "The captured bytes, exactly as received.",
            }.get(self.COLUMNS[section])
        return None

    def _effective_extra(self, position: int) -> bytes:
        """Bytes not used by the transfer, including trimmed CF padding."""
        facts = self._facts[position]
        used = self._used[position]
        unused_payload = facts.payload[used:] if used < len(facts.payload) else b""
        return bytes(unused_payload) + bytes(facts.extra)

    def data(self, index: QModelIndex, role=Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        position = index.row()
        facts = self._facts[position]
        frame = facts.frame
        column = index.column()

        if role == Qt.DisplayRole:
            if column == 0:
                return "{:.4f}s".format(frame.timestamp - self._base)
            if column == 1:
                return "0x" + frame.id_hex
            if column == 2:
                return str(frame.dlc)
            if column == 3:
                return facts.name or DASH
            if column == 4:
                return facts.pci_hex or DASH
            if column == 5:
                return facts.sequence_or_flow or DASH
            if column == 6:
                return (DASH if facts.declared_length is None
                        else str(facts.declared_length))
            if column == 7:
                used = self._used[position]
                shown = facts.payload[:used] if used else facts.payload
                if facts.pci_type is not None and used == 0 and facts.name in ("FC",):
                    return DASH
                return " ".join("{:02X}".format(b) for b in shown) or DASH
            if column == 8:
                extra = self._effective_extra(position)
                return " ".join("{:02X}".format(b) for b in extra) or DASH
            return frame.data_hex
        if role == Qt.FontRole:
            return self._theme.mono_font(-0.5)
        if role == Qt.ForegroundRole:
            if column == 8 and self._effective_extra(position):
                return self._theme.color("warning")
            if column == 3 and facts.problem:
                return self._theme.color("danger")
        if role == Qt.ToolTipRole:
            if facts.problem:
                return facts.problem
            if column == 8 and self._effective_extra(position):
                return ("Not used by the ISO-TP reading. Kept because the raw "
                        "frame is the source of truth.")
        return None


#: Where this page's own internal splitter sizes are persisted, the same way
#: MainWindow persists ui.sidebar_width -- written on every drag (cheap:
#: Config.set() is in-memory only) and only actually reaching disk whenever
#: something else's Config.save() already would (MainWindow.closeEvent, in
#: practice).
#:
#: _SPLITTER_CONFIG_KEY: the outer, vertical split -- [summary, workspace].
#: Two elements, not the pre-redesign three: the transfer list and the
#: detail panel used to be stacked (a third pane below the transfers table)
#: and now sit side by side in their own nested horizontal splitter instead
#: (_WORKSPACE_SPLIT_KEY). An old, three-element saved value simply fails
#: the length check below and falls back to the proportional default --
#: exactly the same graceful degradation an invalid/malformed saved value
#: already had to handle, nothing new to guard against.
_SPLITTER_CONFIG_KEY = "ui.isotp_splitter_sizes"
#: The inner, horizontal split -- [transfers list width, detail panel width].
_WORKSPACE_SPLIT_KEY = "ui.isotp_workspace_split"
#: Whether the transfer list is currently folded away -- see
#: _on_transfers_toggled. Mirrors ui.sidebar_collapsed's own idiom.
_TRANSFERS_COLLAPSED_KEY = "ui.isotp_transfers_collapsed"


class IsoTpView(QWidget):
    """Evidence summary, transfers, and the frames behind a transfer.

    Capture-wide, and given the whole content area to itself: unlike
    Blocks/Signals/Range/Plot, this page's job is comparing many rows across
    several tables at once, not reading one message's payload -- so
    MainWindow gives it its own top-level page rather than squeezing it in
    beside the Messages/Trace packet list. See MainWindow._activate_isotp
    and _build_isotp_workspace.
    """

    #: Emitted with a CanFrame when the operator picks a raw frame, so the rest
    #: of the application can follow along.
    frameActivated = Signal(object)
    bookmarkRequested = Signal(str, str, str)
    diagnosticSelectionChanged = Signal()

    MAX_DIAGNOSTIC_ROWS = 2000

    def __init__(self, config, theme: Theme, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.config = config
        self._theme = theme
        self._rows: List[IsoTpEvidence] = []
        self._selected_key: Optional[str] = None
        self._selected_id_label: str = ""
        self._transfers_caption: str = "Transfers"
        self._transfer_source = None      # callable(key) -> List[IsoTpTransfer]
        self._time_base = 0.0
        self._sized = False
        #: The one authoritative selected-transfer index -- into whatever
        #: transfer_model currently holds. Row clicks, Previous, Next and a
        #: CAN-ID/filter change all funnel through transfer_view.selectRow()
        #: (or clearSelection()), and _on_transfer_selected -- the sole
        #: place this is written -- is the only thing that reacts to it.
        #: Never read the table's current row and the navigator's own count
        #: as two separate ideas of "where we are"; there is only this one.
        self._transfer_index: int = -1
        self._diagnostic_analysis: Optional[DiagnosticAnalysis] = None
        self._conversation_rows = []
        self._did_rows = []
        self._dtc_rows = []
        self._restored_diagnostic_selection: Dict[str, str] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(SPACE_SM)

        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self._on_diagnostic_selection_changed)

        transfers_page = QWidget()
        transfers_layout = QVBoxLayout(transfers_page)
        transfers_layout.setContentsMargins(0, 0, 0, 0)
        transfers_layout.setSpacing(0)

        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_summary())

        self.workspace_splitter = QSplitter(Qt.Horizontal)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.addWidget(self._build_transfers())
        self.workspace_splitter.addWidget(self._build_detail_panel())
        # ~30/70 -- Transfers is a picker, Details is where time is actually
        # spent (see the class docstring); a 40/60 split undersold that.
        self.workspace_splitter.setStretchFactor(0, 3)
        self.workspace_splitter.setStretchFactor(1, 7)
        self.workspace_splitter.splitterMoved.connect(self._remember_workspace_split)
        self.splitter.addWidget(self.workspace_splitter)

        # ~28/72 -- the CAN-ID overview is a quarter-to-a-third of the page,
        # never more: it is a navigation aid, not the investigation itself.
        self.splitter.setStretchFactor(0, 7)
        self.splitter.setStretchFactor(1, 18)
        # In-memory only (see _SPLITTER_CONFIG_KEY) -- an operator dragging a
        # handle must never trigger a disk write per pixel of motion.
        self.splitter.splitterMoved.connect(self._remember_splitter_sizes)
        transfers_layout.addWidget(self.splitter, 1)
        self.tabs.addTab(transfers_page, "Transfers")
        self.tabs.addTab(self._build_diagnostic_overview(), "Overview")
        self.tabs.addTab(self._build_conversations(), "Conversations")
        self.tabs.addTab(self._build_dids(), "DIDs")
        self.tabs.addTab(self._build_dtcs(), "DTCs")
        root.addWidget(self.tabs, 1)

    def _remember_splitter_sizes(self, *_args) -> None:
        self.config.set(_SPLITTER_CONFIG_KEY, list(self.splitter.sizes()))

    def _remember_workspace_split(self, *_args) -> None:
        if self.transfers_toggle.isChecked():
            # Only remember a real, expanded split -- persisting the
            # collapsed [~0, total] arrangement would make every future
            # launch open with the transfer list already folded away,
            # regardless of _TRANSFERS_COLLAPSED_KEY.
            self.config.set(_WORKSPACE_SPLIT_KEY, list(self.workspace_splitter.sizes()))

    # -- construction: shared table setup --------------------------------

    def _table(self) -> QTableView:
        view = QTableView()
        view.setSelectionBehavior(QAbstractItemView.SelectRows)
        view.setSelectionMode(QAbstractItemView.SingleSelection)
        view.setAlternatingRowColors(True)
        view.setShowGrid(False)
        view.setWordWrap(False)
        view.verticalHeader().setVisible(False)
        view.verticalHeader().setDefaultSectionSize(ROW_HEIGHT_COMPACT)
        view.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        header = view.horizontalHeader()
        header.setHighlightSections(False)
        header.setStretchLastSection(True)
        # Interactive so the operator can widen a column; never
        # ResizeToContents, which re-measures every row of a 60,000-row model.
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setMinimumSectionSize(40)
        # Four rows plus the header: below this a table stops being a table.
        view.setMinimumHeight(ROW_HEIGHT_COMPACT * 5 + 8)
        return view

    def _diagnostic_table(self, columns) -> QTableWidget:
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSortingEnabled(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT_COMPACT)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        return table

    def _build_diagnostic_overview(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, SPACE_SM, 0, 0)
        layout.setSpacing(SPACE_SM)
        self.diagnostic_overview_note = QLabel(
            "Conversation analysis is computed from observed complete ISO-TP transfers.")
        self.diagnostic_overview_note.setObjectName("Muted")
        self.diagnostic_overview_note.setWordWrap(True)
        layout.addWidget(self.diagnostic_overview_note)
        layout.addWidget(SectionLabel("Passive diagnostic peers", self._theme))
        self.peer_table = self._diagnostic_table((
            "Tester-like", "ECU-like", "Requests", "Positive", "Negative",
            "Unanswered", "Services", "Basis"))
        layout.addWidget(self.peer_table, 1)
        self.diagnostic_caveats = QPlainTextEdit()
        self.diagnostic_caveats.setReadOnly(True)
        self.diagnostic_caveats.setMaximumHeight(110)
        self.diagnostic_caveats.setPlaceholderText(
            "Capture-integrity and retained-horizon caveats appear here.")
        layout.addWidget(self.diagnostic_caveats)
        return page

    def _build_conversations(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, SPACE_SM, 0, 0)
        layout.setSpacing(SPACE_SM)
        controls = QHBoxLayout()
        self.conversation_filter = QLineEdit()
        self.conversation_filter.setPlaceholderText(
            "Search CAN ID, peer, service, DID, NRC, status, timestamp…")
        self.conversation_filter.setClearButtonEnabled(True)
        self.conversation_filter.textChanged.connect(self._populate_conversations)
        controls.addWidget(self.conversation_filter, 1)
        self.conversation_status_filter = QComboBox()
        self.conversation_status_filter.addItem("All correlations", "")
        for status in CorrelationStatus:
            self.conversation_status_filter.addItem(status.value, status.value)
        self.conversation_status_filter.currentIndexChanged.connect(
            self._populate_conversations)
        controls.addWidget(self.conversation_status_filter)
        self.bookmark_conversation_button = QPushButton("Bookmark conversation")
        self.bookmark_conversation_button.clicked.connect(
            self._bookmark_selected_conversation)
        controls.addWidget(self.bookmark_conversation_button)
        layout.addLayout(controls)
        self.conversation_table = self._diagnostic_table((
            "Time", "Peer pair", "Direction", "Service", "Detail",
            "Correlation", "Latency", "Complete"))
        self.conversation_table.itemSelectionChanged.connect(
            self._on_conversation_selected)
        layout.addWidget(self.conversation_table, 3)
        layout.addWidget(SectionLabel("Observed evidence and correlation", self._theme))
        self.conversation_detail = QPlainTextEdit()
        self.conversation_detail.setReadOnly(True)
        self.conversation_detail.setMinimumHeight(150)
        layout.addWidget(self.conversation_detail, 2)
        return page

    def _build_dids(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, SPACE_SM, 0, 0)
        layout.setSpacing(SPACE_SM)
        controls = QHBoxLayout()
        self.did_filter = QLineEdit()
        self.did_filter.setPlaceholderText("Search DID, peer, raw value, timestamp…")
        self.did_filter.setClearButtonEnabled(True)
        self.did_filter.textChanged.connect(self._populate_dids)
        controls.addWidget(self.did_filter, 1)
        self.bookmark_did_button = QPushButton("Bookmark DID observation")
        self.bookmark_did_button.clicked.connect(self._bookmark_selected_did)
        controls.addWidget(self.bookmark_did_button)
        layout.addLayout(controls)
        self.did_table = self._diagnostic_table((
            "Time", "Peer pair", "DID", "Raw value", "Request transfer",
            "Response transfer", "Caveats"))
        self.did_table.itemSelectionChanged.connect(
            self._on_diagnostic_selection_changed)
        layout.addWidget(self.did_table, 1)
        return page

    def _build_dtcs(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, SPACE_SM, 0, 0)
        layout.setSpacing(SPACE_SM)
        controls = QHBoxLayout()
        self.dtc_filter = QLineEdit()
        self.dtc_filter.setPlaceholderText("Search DTC, status, peer, subfunction…")
        self.dtc_filter.setClearButtonEnabled(True)
        self.dtc_filter.textChanged.connect(self._populate_dtcs)
        controls.addWidget(self.dtc_filter, 1)
        self.bookmark_dtc_button = QPushButton("Bookmark DTC observation")
        self.bookmark_dtc_button.clicked.connect(self._bookmark_selected_dtc)
        controls.addWidget(self.bookmark_dtc_button)
        layout.addLayout(controls)
        self.dtc_table = self._diagnostic_table((
            "Time", "Peer pair", "DTC", "Status", "0x19 subfunction",
            "Source", "Raw record"))
        self.dtc_table.itemSelectionChanged.connect(
            self._on_diagnostic_selection_changed)
        layout.addWidget(self.dtc_table, 1)
        return page

    # -- construction: CAN-ID evidence summary ---------------------------

    def _build_summary(self) -> QWidget:
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACE_SM)

        row = QHBoxLayout()
        row.setSpacing(SPACE_SM)
        row.addWidget(SectionLabel("ISO-TP evidence by CAN ID", self._theme))

        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("Filter by CAN ID…")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.setMinimumWidth(100)
        self.filter_box.setMaximumWidth(220)
        self.filter_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.filter_box.textChanged.connect(self._apply_filter)
        row.addWidget(self.filter_box)

        self.evidence_filter = QComboBox()
        self.evidence_filter.addItem("Any evidence", "")
        for label in reversed(EVIDENCE_ORDER):
            self.evidence_filter.addItem("{} and above".format(label), label)
        self.evidence_filter.setCurrentIndex(0)
        self.evidence_filter.currentIndexChanged.connect(self._apply_filter)
        row.addWidget(self.evidence_filter)

        self.multiframe_only = QCheckBox("Multi-frame only")
        self.multiframe_only.setToolTip(
            "Hide IDs that only ever produced single-frame candidates.")
        self.multiframe_only.toggled.connect(self._apply_filter)
        row.addWidget(self.multiframe_only)

        row.addStretch(1)
        self.summary_note = QLabel("")
        self.summary_note.setObjectName("Muted")
        row.addWidget(self.summary_note)
        column.addLayout(row)

        self.summary_model = EvidenceModel(self._theme, self)
        self.summary_view = self._table()
        self.summary_view.setModel(self.summary_model)
        self.summary_view.setSortingEnabled(True)
        self.summary_view.setItemDelegateForColumn(
            EvidenceModel.COLUMNS.index("Evidence"),
            BadgeDelegate(self._theme, _evidence_badge_tone, self.summary_view),
        )
        self.summary_view.selectionModel().selectionChanged.connect(
            self._on_id_selected)
        # Sorting resets the model, which drops the view's selection even
        # though the row is still there under a new index. Without this, the
        # transfer and frame tables kept showing the right ID's data but
        # nothing in the summary was highlighted to say so — attribution was
        # correct, the picture of it was not.
        self.summary_view.horizontalHeader().sortIndicatorChanged.connect(
            self._reselect_after_sort)
        column.addWidget(self.summary_view, 1)

        self.summary_empty = EmptyState(
            "No frames to analyse",
            "Open or start a capture. This page reads frames that were already "
            "received; it never sends anything, including flow control.",
            self._theme,
        )
        column.addWidget(self.summary_empty, 1)
        self.summary_view.setVisible(False)
        return panel

    # -- construction: transfer list (collapsible) -----------------------

    def _build_transfers(self) -> QWidget:
        # Panel, matching the detail workspace beside it -- master and
        # detail read as one consistent pair of cards, not one bordered
        # investigation area next to an unstyled list.
        panel = QFrame()
        panel.setObjectName("Panel")
        column = QVBoxLayout(panel)
        column.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_MD)
        column.setSpacing(SPACE_SM)

        row = QHBoxLayout()
        row.setSpacing(SPACE_SM)
        # The toggle *is* the header -- its own text carries the "Transfers
        # on 0x..." caption plus the chevron, the same idiom InterpretView's
        # bit-activity disclosure already uses (see interpret_view.py's
        # _BITS_LABEL/bits_toggle) -- so collapsing feels like part of this
        # panel's header, not a separate control bolted onto it.
        self.transfers_toggle = QPushButton()
        self.transfers_toggle.setObjectName("Disclosure")
        self.transfers_toggle.setCheckable(True)
        self.transfers_toggle.setChecked(True)
        self.transfers_toggle.setCursor(Qt.PointingHandCursor)
        self.transfers_toggle.setToolTip(
            "Collapse the transfer list to give the detail panel more room. "
            "The selected transfer, and browsing with Previous/Next there, "
            "both keep working while it is collapsed.")
        self.transfers_toggle.toggled.connect(self._on_transfers_toggled)
        self._refresh_transfers_toggle_text()
        row.addWidget(self.transfers_toggle)

        self.problems_only = QCheckBox("Problems only")
        self.problems_only.setToolTip(
            "Show only transfers that did not complete cleanly.")
        self.problems_only.toggled.connect(self._reload_transfers)
        row.addWidget(self.problems_only)
        row.addStretch(1)
        # A count, not the evidence reason: that text already lives on
        # every cell of the ID's own row in the summary table above (see
        # EvidenceModel's ToolTipRole) -- repeating it here just crowded
        # this header without saying anything the hover didn't already.
        # Ignored, not Preferred: this is the one piece of the header that
        # stays visible while collapsed (see _on_transfers_toggled), and it
        # must never be the reason the collapsed pane's own minimum width
        # grows -- same idiom as workspace_note elsewhere in the app.
        self.transfer_count_label = QLabel("")
        self.transfer_count_label.setObjectName("Muted")
        self.transfer_count_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.transfer_count_label)
        column.addLayout(row)

        self.transfer_model = TransferModel(self._theme, self)
        self.transfer_view = self._table()
        self.transfer_view.setModel(self.transfer_model)
        self.transfer_view.setItemDelegateForColumn(
            TransferModel.COLUMNS.index("Status"),
            BadgeDelegate(self._theme, _status_tone, self.transfer_view),
        )
        self.transfer_view.selectionModel().selectionChanged.connect(
            self._on_transfer_selected)
        column.addWidget(self.transfer_view, 1)
        return panel

    def _refresh_transfers_toggle_text(self) -> None:
        chevron = "▾" if self.transfers_toggle.isChecked() else "▸"
        self.transfers_toggle.setText("{}  {}".format(chevron, self._transfers_caption))

    def _on_transfers_toggled(self, shown: bool) -> None:
        shown = bool(shown)
        self._refresh_transfers_toggle_text()
        self.transfer_view.setVisible(shown)
        # Neither means anything about a table that is not on screen -- and,
        # not incidentally, both were contributors to what "collapsed" used
        # to still need a few hundred pixels for. The toggle itself --
        # "<chevron>  Transfers on 0x...", the header's own irreducible
        # context -- is the only thing left once these are gone too.
        self.problems_only.setVisible(shown)
        self.transfer_count_label.setVisible(shown)
        self.config.set(_TRANSFERS_COLLAPSED_KEY, not shown)

        total = sum(self.workspace_splitter.sizes()) or self.workspace_splitter.width() or 800
        if shown:
            saved = self.config.get(_WORKSPACE_SPLIT_KEY, None)
            if (isinstance(saved, (list, tuple)) and len(saved) == 2
                    and all(isinstance(v, (int, float)) and v >= 0 for v in saved)
                    and sum(saved) > 0):
                width = int(saved[0])
            else:
                width = int(total * self._WORKSPACE_DEFAULT_RATIO)
            width = max(200, min(width, max(200, total - 200)))
            self.workspace_splitter.setSizes([width, max(200, total - width)])
            # Reopening highlights whatever is currently in the detail panel
            # -- the table selection was never touched while collapsed (see
            # the class docstring on _transfer_index) -- and scrolls it back
            # into view, in case Previous/Next moved it off-screen before.
            if self._transfer_index >= 0:
                self.transfer_view.scrollTo(
                    self.transfer_model.index(self._transfer_index, 0))
        else:
            current = self.workspace_splitter.sizes()
            if current and current[0] > 40:
                # Remember the expanded width being given up, exactly like
                # ui.sidebar_width remembers MainWindow's own collapsible
                # panel before it narrows.
                self.config.set(_WORKSPACE_SPLIT_KEY, list(current))
            # transfer_view is hidden, so transfers_panel's own minimum
            # shrinks to just this header row -- a hidden QSplitter child's
            # contribution to layout negotiation drops out entirely, the
            # same mechanism MainWindow.set_sidebar_collapsed already
            # relies on for its own collapsible sidebar.
            self.workspace_splitter.setSizes([0, total])

    # -- construction: transfer detail workspace --------------------------

    def _build_detail_panel(self) -> QWidget:
        """The dominant investigation surface: one bordered card, not a
        card full of smaller cards.

        Identity/timing used to be three separate boxed panels beside a
        fourth boxed payload area -- four nested cards inside this one,
        fighting each other for attention instead of the frame table below
        them (the actual point of the page) getting the room. Everything
        here now shares this single surface: a plain aligned grid for the
        summary fields, a plain diagnostic line, a lightly-tinted (not
        boxed) payload area, separated by thin Dividers rather than borders.
        """
        panel = QFrame()
        panel.setObjectName("Panel")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_MD)
        outer.setSpacing(SPACE_SM)

        header = QHBoxLayout()
        header.setSpacing(SPACE_SM)
        self.detail_title = SectionLabel("Transfer details", self._theme)
        header.addWidget(self.detail_title)
        self.detail_status_chip = Chip("", "muted", self._theme)
        self.detail_status_chip.setVisible(False)
        header.addWidget(self.detail_status_chip)
        header.addStretch(1)
        self.bookmark_transfer_button = QPushButton("Bookmark transfer")
        self.bookmark_transfer_button.setEnabled(False)
        self.bookmark_transfer_button.clicked.connect(self._bookmark_selected_transfer)
        header.addWidget(self.bookmark_transfer_button)
        header.addWidget(self._build_navigator())
        outer.addLayout(header)

        outer.addWidget(Divider())
        outer.addLayout(self._build_summary_grid())
        outer.addLayout(self._build_diagnostic_row())
        outer.addWidget(Divider())

        outer.addWidget(self._build_frames_section(), 1)

        self._update_navigator()
        self._update_detail_panel(None)
        return panel

    def _build_navigator(self) -> QWidget:
        """`<  n / total  >` -- the one place a transfer index is browsed
        from when the transfer list itself is collapsed (or just out of
        reach). See _on_transfer_selected for why this can never disagree
        with the table's own selection.
        """
        nav = QWidget()
        row = QHBoxLayout(nav)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_XS)

        self.prev_button = QPushButton("‹")
        self.prev_button.setObjectName("NavArrow")
        self.prev_button.setCursor(Qt.PointingHandCursor)
        self.prev_button.setToolTip("Previous transfer")
        self.prev_button.setFixedSize(24, 24)
        self.prev_button.clicked.connect(self._on_prev_transfer)
        row.addWidget(self.prev_button)

        self.nav_label = QLabel("0 / 0")
        self.nav_label.setFont(self._theme.mono_font(-0.5, bold=True))
        self.nav_label.setAlignment(Qt.AlignCenter)
        self.nav_label.setMinimumWidth(64)
        row.addWidget(self.nav_label)

        self.next_button = QPushButton("›")
        self.next_button.setObjectName("NavArrow")
        self.next_button.setCursor(Qt.PointingHandCursor)
        self.next_button.setToolTip("Next transfer")
        self.next_button.setFixedSize(24, 24)
        self.next_button.clicked.connect(self._on_next_transfer)
        row.addWidget(self.next_button)
        return nav

    #: Left column: what the transfer *is*. Right column: how big/long it
    #: was. Two label:value columns in one QGridLayout, not two separate
    #: boxed cards -- the grid's own column alignment already reads as
    #: organised without a border around each half saying so again.
    _IDENTITY_FIELDS = ("CAN ID", "Status", "Addressing")
    _TIMING_FIELDS = ("Start", "Duration", "Frames", "Bytes")

    def _build_summary_grid(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(SPACE_LG)
        grid.setVerticalSpacing(SPACE_XS)
        self._identity_fields: Dict[str, QLabel] = {}
        self._timing_fields: Dict[str, QLabel] = {}
        for column, (labels, store) in enumerate((
            (self._IDENTITY_FIELDS, self._identity_fields),
            (self._TIMING_FIELDS, self._timing_fields),
        )):
            for row, label_text in enumerate(labels):
                label = QLabel(label_text)
                label.setObjectName("Muted")
                grid.addWidget(label, row, column * 2)
                value = QLabel(DASH)
                value.setFont(self._theme.mono_font(-0.5))
                value.setTextInteractionFlags(Qt.TextSelectableByMouse)
                grid.addWidget(value, row, column * 2 + 1)
                store[label_text] = value
        # A trailing stretch column keeps both label:value pairs pinned to
        # the left, snug, rather than spread across the panel's full width.
        grid.setColumnStretch(4, 1)
        return grid

    def _build_diagnostic_row(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(SectionLabel("Diagnostic", self._theme))
        # Plain text, not a card: a clean transfer's diagnostic is a single
        # quiet line ("no issues detected") and does not need a border to
        # say so -- see _update_detail_panel, which gives a genuine problem
        # a bolder colour here instead of a bigger box.
        self.diagnostic_line = QLabel(DASH)
        self.diagnostic_line.setWordWrap(True)
        self.diagnostic_line.setTextInteractionFlags(Qt.TextSelectableByMouse)
        column.addWidget(self.diagnostic_line)
        return column

    def _build_frames_section(self) -> QWidget:
        """The raw frames behind the selected transfer -- the primary
        investigation surface, given the space Payload/Reassembled/Raw/
        Protocol used to divide it with. A direct section, not a one-tab
        tab bar: with those three removed, Frames was the only tab left,
        and a tab control that only ever shows one thing is not a control
        worth keeping.
        """
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_XS)

        header = QHBoxLayout()
        header.setSpacing(SPACE_SM)
        header.addWidget(SectionLabel("Frames", self._theme))
        self.frames_note = QLabel("")
        self.frames_note.setObjectName("Muted")
        header.addWidget(self.frames_note)
        header.addStretch(1)
        layout.addLayout(header)

        self.frame_model = FrameModel(self._theme, self)
        self.frame_view = self._table()
        self.frame_view.setModel(self.frame_model)
        self.frame_view.doubleClicked.connect(self._on_frame_activated)
        layout.addWidget(self.frame_view, 1)
        return container

    # -- passive diagnostic conversation workspace ----------------------

    @staticmethod
    def _bytes_text(value: bytes) -> str:
        return " ".join("{:02X}".format(byte) for byte in value) or DASH

    @staticmethod
    def _pair_text(pair) -> str:
        if pair is None:
            return "Unknown peer"
        return "{} ↔ {}".format(pair.endpoint_a.id_label,
                                pair.endpoint_b.id_label)

    def set_diagnostic_analysis(self, analysis: Optional[DiagnosticAnalysis]) -> None:
        """Populate bounded views from the worker-owned immutable snapshot."""
        self._diagnostic_analysis = analysis
        if analysis is None:
            self.clear_diagnostics()
            return
        self._populate_peers()
        self._populate_conversations()
        self._populate_dids()
        self._populate_dtcs()
        complete = sum(item.complete for item in analysis.transfers)
        counts = {status: sum(
            item.correlation_status is status for item in analysis.conversations)
                  for status in CorrelationStatus}
        self.diagnostic_overview_note.setText(
            "{:,} normalized transfer{} ({} complete, {} incomplete) · "
            "{:,} conversation event{} · {} paired positive · {} paired negative · "
            "{} unanswered · {} orphan · {} ambiguous. Normal addressing only; "
            "all rows were observed, never generated.".format(
                len(analysis.transfers),
                "" if len(analysis.transfers) == 1 else "s", complete,
                len(analysis.transfers) - complete, len(analysis.conversations),
                "" if len(analysis.conversations) == 1 else "s",
                counts[CorrelationStatus.PAIRED_POSITIVE],
                counts[CorrelationStatus.PAIRED_NEGATIVE],
                counts[CorrelationStatus.UNANSWERED_REQUEST],
                counts[CorrelationStatus.ORPHAN_RESPONSE],
                counts[CorrelationStatus.AMBIGUOUS]))
        self.diagnostic_caveats.setPlainText(
            "\n".join("• " + item for item in analysis.caveats)
            or "No additional retained-horizon caveats were reported. "
               "Hardware/driver loss visibility is stated by the capture integrity context.")
        self._apply_restored_diagnostic_selection()

    def clear_diagnostics(self) -> None:
        self._diagnostic_analysis = None
        self._conversation_rows = []
        self._did_rows = []
        self._dtc_rows = []
        for table in (self.peer_table, self.conversation_table,
                      self.did_table, self.dtc_table):
            table.blockSignals(True)
            table.setRowCount(0)
            table.blockSignals(False)
        self.conversation_detail.clear()
        self.diagnostic_overview_note.setText(
            "No reconstructed diagnostic conversations in the retained capture.")
        self.diagnostic_caveats.clear()

    def _populate_peers(self) -> None:
        analysis = self._diagnostic_analysis
        rows = analysis.peers if analysis is not None else ()
        self.peer_table.setRowCount(min(len(rows), self.MAX_DIAGNOSTIC_ROWS))
        for position, summary in enumerate(rows[:self.MAX_DIAGNOSTIC_ROWS]):
            pair = summary.peer_pair
            services = ", ".join("0x{:02X} {} ({})".format(*item)
                                 for item in summary.services)
            values = (
                pair.tester_like.id_label if pair.tester_like else "Unknown",
                pair.ecu_like.id_label if pair.ecu_like else "Unknown",
                str(summary.request_count), str(summary.positive_response_count),
                str(summary.negative_response_count), str(summary.unanswered_count),
                services or DASH, pair.direction_basis)
            for column, value in enumerate(values):
                self.peer_table.setItem(position, column, QTableWidgetItem(value))

    def _conversation_search_text(self, conversation) -> str:
        values = [conversation.conversation_id,
                  conversation.correlation_status.value,
                  self._pair_text(conversation.peer_pair),
                  "{:.9g}".format(conversation.first_timestamp),
                  conversation.service_name,
                  "0x{:02X}".format(conversation.service or 0)]
        for event in (conversation.request, conversation.response):
            if event is None:
                continue
            message = event.uds
            values.extend((event.transfer.endpoint.id_label, message.kind,
                           message.describe(), self._bytes_text(message.raw_payload),
                           event.transfer.status))
            values.extend("0x{:04X}".format(item) for item in message.identifiers)
            if message.nrc is not None:
                values.extend(("0x{:02X}".format(message.nrc), message.nrc_text))
        values.extend(conversation.reasons)
        values.extend(conversation.caveats)
        return " ".join(values).lower()

    def _populate_conversations(self, *_args) -> None:
        analysis = self._diagnostic_analysis
        wanted = self._selected_logical_id(self.conversation_table,
                                           self._conversation_rows,
                                           "conversation_id")
        text = self.conversation_filter.text().strip().lower()
        status = self.conversation_status_filter.currentData() or ""
        rows = [] if analysis is None else [
            item for item in analysis.conversations
            if (not status or item.correlation_status.value == status)
            and (not text or text in self._conversation_search_text(item))]
        self._conversation_rows = rows[:self.MAX_DIAGNOSTIC_ROWS]
        table = self.conversation_table
        table.blockSignals(True)
        table.setRowCount(len(self._conversation_rows))
        for position, conversation in enumerate(self._conversation_rows):
            request, response = conversation.request, conversation.response
            if request is not None and response is not None:
                direction = "{} → {}".format(
                    request.transfer.endpoint.id_label,
                    response.transfer.endpoint.id_label)
            else:
                event = request or response
                direction = (event.transfer.endpoint.id_label + " → ?"
                             if request is not None else
                             "? → " + event.transfer.endpoint.id_label)
            detail_event = request or response
            detail = detail_event.uds.describe() if detail_event else DASH
            complete = "/".join(event.transfer.status for event in
                                (request, response) if event is not None)
            values = (
                "{:.4f}s".format(conversation.first_timestamp - self._time_base),
                self._pair_text(conversation.peer_pair), direction,
                "0x{:02X} {}".format(conversation.service or 0,
                                      conversation.service_name),
                detail, conversation.correlation_status.value,
                ("{:.3f} ms".format(conversation.latency * 1000)
                 if conversation.latency is not None else DASH), complete)
            for column, value in enumerate(values):
                table.setItem(position, column, QTableWidgetItem(value))
        self._select_logical_id(table, self._conversation_rows,
                                "conversation_id", wanted)
        table.blockSignals(False)
        self._on_conversation_selected(False)

    def _populate_dids(self, *_args) -> None:
        analysis = self._diagnostic_analysis
        wanted = self._selected_logical_id(
            self.did_table, self._did_rows, "observation_id")
        text = self.did_filter.text().strip().lower()
        rows = [] if analysis is None else [item for item in analysis.dids
            if not text or text in " ".join((
                item.observation_id, "0x{:04X}".format(item.did),
                self._pair_text(item.peer_pair), self._bytes_text(item.raw_value),
                "{:.9g}".format(item.request_timestamp or
                                 item.response_timestamp or 0),
                " ".join(item.caveats))).lower()]
        self._did_rows = rows[:self.MAX_DIAGNOSTIC_ROWS]
        table = self.did_table
        table.blockSignals(True)
        table.setRowCount(len(self._did_rows))
        for position, item in enumerate(self._did_rows):
            stamp = item.request_timestamp if item.request_timestamp is not None \
                else item.response_timestamp
            values = (
                "{:.4f}s".format((stamp or 0) - self._time_base),
                self._pair_text(item.peer_pair), "0x{:04X}".format(item.did),
                self._bytes_text(item.raw_value), item.request_transfer_id or DASH,
                item.response_transfer_id or DASH, "; ".join(item.caveats) or DASH)
            for column, value in enumerate(values):
                table.setItem(position, column, QTableWidgetItem(value))
        self._select_logical_id(table, self._did_rows, "observation_id", wanted)
        table.blockSignals(False)

    def _populate_dtcs(self, *_args) -> None:
        analysis = self._diagnostic_analysis
        wanted = self._selected_logical_id(
            self.dtc_table, self._dtc_rows, "observation_id")
        text = self.dtc_filter.text().strip().lower()
        rows = [] if analysis is None else [item for item in analysis.dtcs
            if not text or text in " ".join((
                item.observation_id, "0x{:06X}".format(item.dtc),
                "0x{:02X}".format(item.status), self._pair_text(item.peer_pair),
                "0x{:02X}".format(item.sub_function),
                "{:.9g}".format(item.timestamp))).lower()]
        self._dtc_rows = rows[:self.MAX_DIAGNOSTIC_ROWS]
        table = self.dtc_table
        table.blockSignals(True)
        table.setRowCount(len(self._dtc_rows))
        for position, item in enumerate(self._dtc_rows):
            values = (
                "{:.4f}s".format(item.timestamp - self._time_base),
                self._pair_text(item.peer_pair), "0x{:06X}".format(item.dtc),
                "0x{:02X}".format(item.status),
                "0x{:02X}".format(item.sub_function),
                "Observed UDS response", self._bytes_text(item.raw_record))
            for column, value in enumerate(values):
                table.setItem(position, column, QTableWidgetItem(value))
        self._select_logical_id(table, self._dtc_rows, "observation_id", wanted)
        table.blockSignals(False)

    @staticmethod
    def _selected_logical_id(table, rows, attribute) -> str:
        position = table.currentRow()
        if 0 <= position < len(rows):
            return str(getattr(rows[position], attribute))
        return ""

    @staticmethod
    def _select_logical_id(table, rows, attribute, wanted: str) -> None:
        if not rows:
            table.clearSelection()
            return
        position = next((index for index, item in enumerate(rows)
                         if str(getattr(item, attribute)) == wanted), 0)
        table.selectRow(position)

    def _format_event(self, title: str, event) -> List[str]:
        if event is None:
            return [title, "  Not observed"]
        transfer, message = event.transfer, event.uds
        lines = [title,
                 "  Endpoint: {} ({})".format(
                     transfer.endpoint.id_label, transfer.endpoint.identity),
                 "  Timestamp: {:.6f} s".format(
                     transfer.first_timestamp - self._time_base),
                 "  UDS: {} · {} · service 0x{:02X} {}".format(
                     message.kind, message.describe(), message.service or 0,
                     message.service_name),
                 "  Transfer: {} · {} · {} frame(s) · {} bytes".format(
                     transfer.transfer_type, transfer.status,
                     transfer.frame_count, len(transfer.payload)),
                 "  Raw payload: {}".format(self._bytes_text(message.raw_payload))]
        for name, value in message.structured_fields:
            lines.append("  {}: {}".format(name, value))
        if message.sub_function_name:
            lines.append("  Subfunction meaning: {}".format(
                message.sub_function_name))
        if message.nrc is not None:
            lines.append("  NRC: 0x{:02X} {}".format(
                message.nrc, message.nrc_text or "unknown standard name"))
        if transfer.diagnostics:
            lines.append("  Transfer diagnostics: {}".format(
                "; ".join(transfer.diagnostics)))
        lines.append("  Raw frames:")
        for raw in transfer.raw_frames:
            lines.append("    {:.6f}s {} {} [{}]".format(
                raw.timestamp - self._time_base, raw.channel,
                "0x" + raw.id_hex, raw.data_hex))
        return lines

    def _on_conversation_selected(self, notify: bool = True) -> None:
        position = self.conversation_table.currentRow()
        if not 0 <= position < len(self._conversation_rows):
            self.conversation_detail.clear()
            return
        item = self._conversation_rows[position]
        lines = self._format_event("REQUEST", item.request)
        lines += [""] + self._format_event("RESPONSE", item.response)
        lines += ["", "CORRELATION", "  Status: " + item.correlation_status.value,
                  "  Peer pair: " + self._pair_text(item.peer_pair)]
        if item.latency is not None:
            lines.append("  Latency: {:.3f} ms".format(item.latency * 1000))
        lines.extend("  Reason: " + reason for reason in item.reasons)
        lines.extend("  Caveat: " + caveat for caveat in item.caveats)
        self.conversation_detail.setPlainText("\n".join(lines))
        if notify:
            self._on_diagnostic_selection_changed()

    def _bookmark_selected_conversation(self) -> None:
        position = self.conversation_table.currentRow()
        if 0 <= position < len(self._conversation_rows):
            item = self._conversation_rows[position]
            self.bookmarkRequested.emit(
                "conversation", item.conversation_id,
                "{} · {}".format(item.service_name,
                                  item.correlation_status.value))

    def _bookmark_selected_did(self) -> None:
        position = self.did_table.currentRow()
        if 0 <= position < len(self._did_rows):
            item = self._did_rows[position]
            self.bookmarkRequested.emit(
                "did", item.observation_id,
                "DID 0x{:04X} · {}".format(item.did,
                                            self._pair_text(item.peer_pair)))

    def _bookmark_selected_dtc(self) -> None:
        position = self.dtc_table.currentRow()
        if 0 <= position < len(self._dtc_rows):
            item = self._dtc_rows[position]
            self.bookmarkRequested.emit(
                "dtc", item.observation_id,
                "DTC 0x{:06X} · status 0x{:02X}".format(item.dtc, item.status))

    def diagnostic_selection(self) -> Dict[str, str]:
        names = ("transfers", "overview", "conversations", "dids", "dtcs")
        state = {
            "tab": names[self.tabs.currentIndex()],
            "conversation_filter": self.conversation_filter.text(),
            "conversation_status": str(
                self.conversation_status_filter.currentData() or ""),
            "did_filter": self.did_filter.text(),
            "dtc_filter": self.dtc_filter.text(),
        }
        conversation = self._selected_logical_id(
            self.conversation_table, self._conversation_rows, "conversation_id")
        did = self._selected_logical_id(
            self.did_table, self._did_rows, "observation_id")
        dtc = self._selected_logical_id(
            self.dtc_table, self._dtc_rows, "observation_id")
        if conversation:
            state["conversation_id"] = conversation
        if did:
            state["did_observation_id"] = did
        if dtc:
            state["dtc_observation_id"] = dtc
        return state

    def apply_diagnostic_selection(self, state: Dict[str, str]) -> None:
        self._restored_diagnostic_selection = {
            str(key): str(value) for key, value in state.items()}
        self._apply_restored_diagnostic_selection()

    def _apply_restored_diagnostic_selection(self) -> None:
        state = self._restored_diagnostic_selection
        if not state:
            return
        controls = ((self.conversation_filter, "conversation_filter"),
                    (self.did_filter, "did_filter"),
                    (self.dtc_filter, "dtc_filter"))
        for control, key in controls:
            control.blockSignals(True)
            control.setText(state.get(key, ""))
            control.blockSignals(False)
        status = state.get("conversation_status", "")
        index = self.conversation_status_filter.findData(status)
        self.conversation_status_filter.blockSignals(True)
        self.conversation_status_filter.setCurrentIndex(max(0, index))
        self.conversation_status_filter.blockSignals(False)
        self._populate_conversations()
        self._populate_dids()
        self._populate_dtcs()
        self._select_logical_id(self.conversation_table, self._conversation_rows,
                                "conversation_id", state.get("conversation_id", ""))
        self._select_logical_id(self.did_table, self._did_rows,
                                "observation_id", state.get("did_observation_id", ""))
        self._select_logical_id(self.dtc_table, self._dtc_rows,
                                "observation_id", state.get("dtc_observation_id", ""))
        names = ("transfers", "overview", "conversations", "dids", "dtcs")
        if state.get("tab") in names:
            self.tabs.setCurrentIndex(names.index(state["tab"]))
        self._restored_diagnostic_selection = {}

    def _on_diagnostic_selection_changed(self, *_args) -> None:
        self.diagnosticSelectionChanged.emit()

    # -- content -----------------------------------------------------------

    def set_survey(self, rows: Sequence[IsoTpEvidence], transfer_source,
                   time_base: float = 0.0,
                   prefer_key: Optional[str] = None) -> None:
        """Show a survey. ``transfer_source`` is called lazily per CAN ID."""
        self._rows = list(rows)
        self._transfer_source = transfer_source
        self._time_base = float(time_base)

        has_rows = bool(self._rows)
        self.summary_view.setVisible(has_rows)
        self.summary_empty.setVisible(not has_rows)

        strong = sum(1 for r in self._rows if r.evidence == STRONG)
        possible = sum(1 for r in self._rows if r.evidence == POSSIBLE)
        self.summary_note.setText(
            "{} ID{} · {} strong · {} possible".format(
                len(self._rows), "" if len(self._rows) == 1 else "s",
                strong, possible) if has_rows else "")

        wanted = prefer_key or self._selected_key
        self._apply_filter(select_key=wanted)

    def _visible_rows(self) -> List[IsoTpEvidence]:
        text = self.filter_box.text().strip().lower().replace("0x", "")
        floor = self.evidence_filter.currentData() or ""
        floor_rank = EVIDENCE_ORDER.index(floor) if floor in EVIDENCE_ORDER else -1
        multi = self.multiframe_only.isChecked()

        rows = []
        for row in self._rows:
            if text and text not in row.id_hex.lower():
                continue
            if floor_rank >= 0 and row.rank < floor_rank:
                continue
            if multi and not row.has_multiframe:
                continue
            rows.append(row)
        return rows

    def _apply_filter(self, *_args, select_key: Optional[str] = None) -> None:
        rows = self._visible_rows()
        keep = select_key or self._selected_key
        self.summary_model.set_rows(rows)

        # Selection is restored by identity, so filtering or sorting never
        # silently swaps which ID's transfers are on screen.
        target = 0
        if keep is not None:
            for position, row in enumerate(rows):
                if row.key == keep:
                    target = position
                    break
            else:
                target = 0
        if rows:
            self.summary_view.selectRow(target)
        else:
            self._selected_key = None
            self._selected_id_label = ""
            self._transfers_caption = "Transfers"
            self._refresh_transfers_toggle_text()
            self.transfer_model.set_rows([])
            self._transfer_index = -1
            self.frame_model.set_transfer(None)
            self._update_navigator()
            self._update_detail_panel(None)
            self._update_transfer_count_label()

    def _reselect_after_sort(self, *_args) -> None:
        # Deferred one event-loop turn: QTableView does further selection
        # bookkeeping after this signal that runs later than a direct slot
        # call, and a selectRow() made synchronously here was found to be
        # overwritten by it — restoring the selection had to happen after
        # Qt's own cleanup, not race it.
        QTimer.singleShot(0, self._do_reselect_after_sort)

    def _do_reselect_after_sort(self) -> None:
        if self._selected_key is None:
            return
        for position in range(self.summary_model.rowCount()):
            row = self.summary_model.row_at(position)
            if row is not None and row.key == self._selected_key:
                self.summary_view.selectRow(position)
                return

    def _on_id_selected(self, *_args) -> None:
        indexes = self.summary_view.selectionModel().selectedRows()
        if not indexes:
            return
        row = self.summary_model.row_at(indexes[0].row())
        if row is None:
            return
        self._selected_key = row.key
        self._selected_id_label = row.id_label
        self._transfers_caption = "Transfers on {}".format(row.id_label)
        self._refresh_transfers_toggle_text()
        self._reload_transfers()

    def _reload_transfers(self, *_args) -> None:
        """Rebuild the transfer list for the selected CAN ID.

        Also the one path a CAN-ID change or a filter toggle (Problems only)
        takes to pick the *initial* transfer -- always row 0 of the fresh
        list, via the same transfer_view.selectRow() every other selection
        change goes through (see _on_transfer_selected), never a second copy
        of what "select this transfer" means.
        """
        if self._selected_key is None or self._transfer_source is None:
            self.transfer_model.set_rows([])
            self._transfer_index = -1
            self.frame_model.set_transfer(None)
            self._update_navigator()
            self._update_detail_panel(None)
            self._update_transfer_count_label()
            self.frames_note.setText("")
            return
        transfers = list(self._transfer_source(self._selected_key))
        if self.problems_only.isChecked():
            transfers = [t for t in transfers if not t.complete]
        self.transfer_model.set_rows(transfers, self._time_base)
        self._update_transfer_count_label()
        if transfers:
            self.transfer_view.selectRow(0)
        else:
            self._transfer_index = -1
            self.frame_model.set_transfer(None)
            self._update_navigator()
            self._update_detail_panel(None)
            self.frames_note.setText("")

    def _update_transfer_count_label(self) -> None:
        total = self.transfer_model.rowCount()
        self.transfer_count_label.setText(
            "{:,} transfer{}".format(total, "" if total == 1 else "s")
            if total else "No transfers")

    # -- transfer selection: the single authoritative path -----------------

    def _on_prev_transfer(self) -> None:
        if self._transfer_index > 0:
            self.transfer_view.selectRow(self._transfer_index - 1)

    def _on_next_transfer(self) -> None:
        if 0 <= self._transfer_index < self.transfer_model.rowCount() - 1:
            self.transfer_view.selectRow(self._transfer_index + 1)

    def _on_transfer_selected(self, *_args) -> None:
        """The one slot that ever writes _transfer_index.

        Reached identically whether the operator clicked a row, pressed
        Previous/Next (both just call transfer_view.selectRow()), or a
        CAN-ID/filter change repopulated the list — Qt's own selectionChanged
        signal is the single event every one of those paths funnels through,
        so there is exactly one place that decides what "the selected
        transfer" means, never a table index and a navigator index drifting
        apart.
        """
        indexes = self.transfer_view.selectionModel().selectedRows()
        if not indexes:
            return
        position = indexes[0].row()
        self._transfer_index = position
        transfer = self.transfer_model.row_at(position)
        self.frame_model.set_transfer(transfer, self._time_base)
        self._update_navigator()
        self._update_detail_panel(transfer)

        if transfer is None:
            self.frames_note.setText("")
        else:
            note = "{} frame{}".format(transfer.frame_count,
                                       "" if transfer.frame_count == 1 else "s")
            if transfer.flow_control:
                note += " · flow control observed"
            if transfer.detail:
                note += " · " + transfer.detail
            self.frames_note.setText(note)

        if self.transfer_view.isVisible():
            self.transfer_view.scrollTo(indexes[0])

    def _update_navigator(self) -> None:
        total = self.transfer_model.rowCount()
        if total == 0 or self._transfer_index < 0:
            self.nav_label.setText("0 / 0")
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(False)
            return
        # 1-based for display -- an operator counts transfers "1, 2, 3", not
        # "0, 1, 2" -- while _transfer_index itself stays the zero-based row
        # number every table/model call here already uses.
        self.nav_label.setText("{} / {}".format(self._transfer_index + 1, total))
        self.prev_button.setEnabled(self._transfer_index > 0)
        self.next_button.setEnabled(self._transfer_index < total - 1)

    def _update_detail_panel(self, transfer: Optional[IsoTpTransfer]) -> None:
        self.detail_title.setText(
            "Transfer details ({})".format(self._selected_id_label)
            if self._selected_id_label else "Transfer details")

        if transfer is None:
            self.bookmark_transfer_button.setEnabled(False)
            self.detail_status_chip.setVisible(False)
            for value in list(self._identity_fields.values()) + list(self._timing_fields.values()):
                value.setText(DASH)
            self.diagnostic_line.setText(DASH)
            self.diagnostic_line.setStyleSheet("")
            return

        self.bookmark_transfer_button.setEnabled(True)
        self.detail_status_chip.setVisible(True)
        self.detail_status_chip.set_text_and_tone(transfer.status, _status_tone(transfer.status))

        self._identity_fields["CAN ID"].setText("0x" + transfer.id_hex)
        self._identity_fields["Status"].setText(transfer.status)
        self._identity_fields["Addressing"].setText(ADDRESSING)
        self._timing_fields["Start"].setText(
            "{:.4f}s".format(transfer.first_timestamp - self._time_base))
        self._timing_fields["Duration"].setText("{:.4f}s".format(transfer.duration))
        self._timing_fields["Frames"].setText(str(transfer.frame_count))
        self._timing_fields["Bytes"].setText(transfer.bytes_label)

        if transfer.complete:
            # Clean is the common case and stays quiet -- a small checkmark
            # line, not a badge -- so a genuine problem (below) is the one
            # that visually stands out, not every transfer equally.
            self.diagnostic_line.setText("✓  No issues detected")
            self.diagnostic_line.setStyleSheet(
                "color: {};".format(self._theme.hex("success")))
        else:
            glyph = "✕" if transfer.failed else "!"
            tone = "danger" if transfer.failed else "warning"
            self.diagnostic_line.setText(
                "{}  {}".format(glyph, transfer.detail or transfer.describe()))
            self.diagnostic_line.setStyleSheet(
                "color: {}; font-weight: 600;".format(self._theme.hex(tone)))

    def _bookmark_selected_transfer(self) -> None:
        transfer = self.transfer_model.row_at(self._transfer_index)
        if transfer is None:
            return
        logical_id = "{}|{:.9f}|{}|{}|{}".format(
            transfer.key, transfer.first_timestamp,
            transfer.declared_length, transfer.status,
            hashlib.sha256(bytes(transfer.data)).hexdigest()[:16])
        self.bookmarkRequested.emit(
            "transfer", logical_id,
            "ISO-TP {} {} at {:.6f}s".format(
                "0x" + transfer.id_hex, transfer.status,
                transfer.first_timestamp - self._time_base))

    def _on_frame_activated(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        facts = self.frame_model._facts[index.row()]
        self.frameActivated.emit(facts.frame)

    # -- for callers ----------------------------------------------------

    def selected_key(self) -> Optional[str]:
        return self._selected_key

    def selected_transfer_index(self) -> int:
        """0-based, or -1 when nothing is selected. Mainly for tests --
        callers that want to *change* the selection use selectRow() on
        transfer_view, or the Previous/Next slots, the same as the UI does.
        """
        return self._transfer_index

    #: Initial column widths. Set explicitly rather than measured: sizing to
    #: contents gave every one of them the width of the widest header --
    #: which pushed Evidence, the column the page exists for, off the
    #: right-hand edge. The count columns (Frames, SF, FF, CF, FC, Other,
    #: Transfers, Complete, Errors) are sized to their *content*, not their
    #: short header, for the same reason in reverse: a capture is routinely
    #: tens of thousands of frames, "{:,}"-formatted (see
    #: EvidenceModel._text), and 108px comfortably fits a six-digit count
    #: ("999,999") with this table's own QTableView::item padding — a
    #: two-letter header like "SF" never needed the room, but a five- or
    #: six-digit value under it did, and a column sized to the header alone
    #: clipped exactly that, forcing a manual drag-to-widen on every one of
    #: them just to read the table this page exists to show.
    _SUMMARY_WIDTHS = (78, 74, 92, 108, 108, 108, 108, 108, 108, 108, 108, 108)
    #: Start / Status / Bytes / Frames / Duration (Duration stretches
    #: regardless -- see _table()'s own setStretchLastSection). Status and
    #: Bytes were measured with QFontMetrics against the actual content
    #: rather than guessed:
    #:   Status is a BadgeDelegate pill, so it needs the full status
    #:   vocabulary's text width (analysis/isotp.py's COMPLETE .. INVALID_
    #:   PCI) plus this table's own 20px QTableView::item padding plus the
    #:   pill's own 12px padding. "Complete" (the common case) alone needs
    #:   ~134px to render unclipped; 140px covers every status except the
    #:   two longest, rarest ones (Sequence Error, Length Mismatch), which
    #:   the delegate clips its *pill* to rather than reserving ~220px of
    #:   this already-dense table for a case that is rare by definition --
    #:   the full text always remains in the cell's own tooltip regardless.
    #:   Bytes is plain text (str(int) or "N of M"), not a badge: 52px
    #:   comfortably fits the overwhelmingly common short values without
    #:   reserving room for "N of M" on an incomplete transfer, which is
    #:   both rare and, again, still in the tooltip.
    _TRANSFER_WIDTHS = (90, 140, 52, 40, 78)
    _FRAME_WIDTHS = (74, 70, 42, 46, 58, 84, 74, 152, 70)

    def size_columns(self) -> None:
        """Give every table its first layout, once."""
        for view, widths in (
            (self.summary_view, self._SUMMARY_WIDTHS),
            (self.transfer_view, self._TRANSFER_WIDTHS),
            (self.frame_view, self._FRAME_WIDTHS),
        ):
            header = view.horizontalHeader()
            for column, width in enumerate(widths):
                if column < header.count():
                    header.resizeSection(column, width)

    #: Default/fallback proportions -- also the sane band a *persisted*
    #: split's own ratio is checked against before being trusted (see
    #: _restore_split). Region 1 (the CAN-ID overview) gets more of the
    #: page by default than a bare "quarter" would give it, so noticeably
    #: more CAN IDs are visible without scrolling; Transfers stays roughly
    #: 30-35% of the workspace, Details getting the rest -- both match this
    #: file's own module docstring on which side of each split is the
    #: investigation.
    _OUTER_DEFAULT_RATIO = 0.38
    _WORKSPACE_DEFAULT_RATIO = 0.32
    #: A persisted ratio outside this band is not a real, deliberately
    #: dragged split -- most likely a stale value saved from a very
    #: differently sized window, or corrupted config -- so it is not
    #: trusted; see _restore_split.
    _SANE_RATIO = (0.12, 0.7)

    def _restore_split(self, splitter: QSplitter, config_key: str,
                       default_ratio: float, minimum_total: int) -> None:
        """Shared restore logic for both of this page's splitters: trust a
        persisted [a, b] pair exactly when its own ratio is plausible (the
        common case -- an operator dragged the handle), otherwise fall back
        to the proportional default rather than risk restoring a split from
        a stale or corrupted value that would leave one side of the page
        effectively unusable.
        """
        saved = self.config.get(config_key, None)
        if (isinstance(saved, (list, tuple)) and len(saved) == 2
                and all(isinstance(v, (int, float)) and v >= 0 for v in saved)
                and sum(saved) > 0):
            ratio = saved[0] / sum(saved)
            if self._SANE_RATIO[0] <= ratio <= self._SANE_RATIO[1]:
                # Trusted exactly, not reclamped against the current
                # widget's own width: Qt's setSizes() already respects each
                # pane's minimum on its own, and reclamping against a width
                # that can differ by a couple of pixels between two
                # otherwise-identical instances (splitter handle/scrollbar
                # rounding) made a faithfully persisted split come back
                # very slightly different from what was saved.
                splitter.setSizes([int(v) for v in saved])
                return
        extent = (splitter.width() if splitter.orientation() == Qt.Horizontal
                 else splitter.height())
        total = max(extent, minimum_total)
        first = int(total * default_ratio)
        splitter.setSizes([first, total - first])

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._sized:
            return
        self._sized = True
        # An operator who has already dragged these handles gets that back,
        # exactly like MainWindow restores ui.sidebar_width -- never a fresh
        # split overriding a size they chose on a previous run.
        self._restore_split(self.splitter, _SPLITTER_CONFIG_KEY,
                            self._OUTER_DEFAULT_RATIO, 420)
        self._restore_split(self.workspace_splitter, _WORKSPACE_SPLIT_KEY,
                            self._WORKSPACE_DEFAULT_RATIO, 800)

        if bool(self.config.get(_TRANSFERS_COLLAPSED_KEY, False)):
            # Triggers _on_transfers_toggled(False), the same path a real
            # click on the disclosure takes -- no separate "start collapsed"
            # code path to keep in sync with it.
            self.transfers_toggle.setChecked(False)
