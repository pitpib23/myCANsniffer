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

Model/view throughout: an ID carrying 60,000 single frames produces 60,000
transfer rows, and building a widget per cell for those is not something the
window survives.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QSizePolicy, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from ..analysis.isotp import ERROR_STATUSES, IsoTpTransfer, frame_facts
from ..analysis.isotp_survey import (
    EVIDENCE_ORDER, NONE, POSSIBLE, STRONG, WEAK, IsoTpEvidence,
)
from ..analysis.uds import interpret as uds_interpret
from .theme import ROW_HEIGHT_COMPACT, SPACE_SM, Theme
from .widgets import EmptyState, SectionLabel

#: Shown where a value genuinely does not apply, rather than left blank so it
#: reads as "not looked at".
DASH = "—"

#: Colour tone per evidence label. Strong is not "good" and Weak is not "bad" —
#: they describe how much the capture supports the reading, so the palette runs
#: from muted to accented rather than red to green.
_EVIDENCE_TONE = {
    STRONG: "accent",
    POSSIBLE: "warning",
    WEAK: "text_muted",
    NONE: "text_muted",
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


class EvidenceModel(QAbstractTableModel):
    """One row per CAN ID: the counts, and what they add up to."""

    COLUMNS = ("CAN ID", "Peer", "Frames", "SF", "FF", "CF", "FC", "Other",
               "Transfers", "Complete", "Errors", "Evidence")
    #: Columns whose numbers read better right-aligned and monospaced.
    _NUMERIC = frozenset(range(2, 11))

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
            if column == 11:
                return self._theme.color(_EVIDENCE_TONE.get(row.evidence, "text"))
            if column == 10 and row.errors:
                return self._theme.color("warning")
            if column == 7 and row.other and row.other > row.candidates:
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
            return "{:,}".format(row.frames)
        if column == 3:
            return "{:,}".format(row.sf) if row.sf else DASH
        if column == 4:
            return "{:,}".format(row.ff) if row.ff else DASH
        if column == 5:
            return "{:,}".format(row.cf) if row.cf else DASH
        if column == 6:
            return "{:,}".format(row.fc) if row.fc else DASH
        if column == 7:
            return "{:,}".format(row.other) if row.other else DASH
        if column == 8:
            return "{:,}".format(row.transfers)
        if column == 9:
            return "{:,}".format(row.complete)
        if column == 10:
            return "{:,}".format(row.errors) if row.errors else DASH
        return row.evidence

    def sort(self, column: int, order=Qt.AscendingOrder) -> None:
        reverse = order == Qt.DescendingOrder

        def sort_key(row: IsoTpEvidence):
            return (row.id_label, row.peer_label, row.frames, row.sf, row.ff,
                    row.cf, row.fc, row.other, row.transfers, row.complete,
                    row.errors, row.rank)[column]

        self.beginResetModel()
        self._rows.sort(key=sort_key, reverse=reverse)
        self.endResetModel()


class TransferModel(QAbstractTableModel):
    """Transfers for the selected CAN ID."""

    COLUMNS = ("Start", "Duration", "Bytes", "Frames", "Status", "Addressing",
               "Diagnostic", "Payload")
    _NUMERIC = frozenset((0, 1, 2, 3))

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._rows: List[IsoTpTransfer] = []
        self._base = 0.0
        #: Diagnostic text is derived lazily and memoised: UDS interpretation
        #: is a separate layer and must not run over a whole capture just to
        #: fill a column the operator may never look at.
        self._diagnostic: dict = {}

    def set_rows(self, rows: Sequence[IsoTpTransfer], time_base: float = 0.0) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self._base = float(time_base)
        self._diagnostic = {}
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
        if role == Qt.ToolTipRole and self.COLUMNS[section] == "Diagnostic":
            return ("A higher-level reading of the reassembled payload, when "
                    "one applies. Blank means no decoder claimed it — not that "
                    "the transfer failed.")
        return None

    def _diagnostic_for(self, position: int) -> str:
        cached = self._diagnostic.get(position)
        if cached is None:
            transfer = self._rows[position]
            # Only a payload that actually reassembled is worth interpreting;
            # feeding fragments to a diagnostic decoder invents meaning.
            cached = DASH
            if transfer.complete and transfer.data:
                message = uds_interpret(transfer.data)
                if message is not None and message.recognised:
                    cached = message.describe()
            self._diagnostic[position] = cached
        return cached

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
                return "{:.4f}s".format(transfer.duration)
            if column == 2:
                return transfer.bytes_label
            if column == 3:
                return str(transfer.frame_count)
            if column == 4:
                return transfer.status
            if column == 5:
                return "normal"
            if column == 6:
                return self._diagnostic_for(position)
            return _elide(transfer.data_hex) or DASH
        if role == Qt.TextAlignmentRole and column in self._NUMERIC:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.FontRole and column in (0, 1, 2, 3, 7):
            return self._theme.mono_font(-0.5)
        if role == Qt.ForegroundRole and column == 4:
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


class IsoTpView(QWidget):
    """Evidence summary, transfers, and the frames behind a transfer."""

    #: Emitted with a CanFrame when the operator picks a raw frame, so the rest
    #: of the application can follow along.
    frameActivated = Signal(object)

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._theme = theme
        self._rows: List[IsoTpEvidence] = []
        self._selected_key: Optional[str] = None
        self._transfer_source = None      # callable(key) -> List[IsoTpTransfer]
        self._time_base = 0.0
        self._sized = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(SPACE_SM)

        self.stack_note = QLabel("")
        self.stack_note.setObjectName("Muted")
        self.stack_note.setWordWrap(True)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_summary())
        splitter.addWidget(self._build_transfers())
        splitter.addWidget(self._build_frames())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)
        root.addWidget(splitter, 1)
        self.splitter = splitter

    # -- construction ---------------------------------------------------

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
        self.filter_box.setFixedWidth(150)
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

    def _build_transfers(self) -> QWidget:
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACE_SM)

        row = QHBoxLayout()
        row.setSpacing(SPACE_SM)
        self.transfer_label = SectionLabel("Transfers", self._theme)
        row.addWidget(self.transfer_label)
        self.problems_only = QCheckBox("Problems only")
        self.problems_only.setToolTip(
            "Show only transfers that did not complete cleanly.")
        self.problems_only.toggled.connect(self._reload_transfers)
        row.addWidget(self.problems_only)
        row.addStretch(1)
        # One line, elided, full text on hover. Wrapped, this grew to four
        # lines and took the room the transfer table needed.
        self.evidence_reason = QLabel("")
        self.evidence_reason.setObjectName("Muted")
        self.evidence_reason.setWordWrap(False)
        self.evidence_reason.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.evidence_reason.setSizePolicy(QSizePolicy.Ignored,
                                           QSizePolicy.Preferred)
        row.addWidget(self.evidence_reason, 1)
        column.addLayout(row)

        self.transfer_model = TransferModel(self._theme, self)
        self.transfer_view = self._table()
        self.transfer_view.setModel(self.transfer_model)
        self.transfer_view.selectionModel().selectionChanged.connect(
            self._on_transfer_selected)
        column.addWidget(self.transfer_view, 1)
        return panel

    def _build_frames(self) -> QWidget:
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACE_SM)

        row = QHBoxLayout()
        row.setSpacing(SPACE_SM)
        row.addWidget(SectionLabel("Frames in this transfer", self._theme))
        self.frames_note = QLabel("")
        self.frames_note.setObjectName("Muted")
        row.addWidget(self.frames_note)
        row.addStretch(1)
        column.addLayout(row)

        self.frame_model = FrameModel(self._theme, self)
        self.frame_view = self._table()
        self.frame_view.setModel(self.frame_model)
        self.frame_view.doubleClicked.connect(self._on_frame_activated)
        column.addWidget(self.frame_view, 1)
        return panel

    # -- content --------------------------------------------------------

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
            self.transfer_model.set_rows([])
            self.frame_model.set_transfer(None)
            self.transfer_label.setText("Transfers")
            self.evidence_reason.setText("")

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
        self.transfer_label.setText("Transfers on {}".format(row.id_label))
        reason = "{} — {}".format(row.evidence, row.why())
        self.evidence_reason.setText(_elide(reason, 96))
        self.evidence_reason.setToolTip(row.why())
        self._reload_transfers()

    def _reload_transfers(self, *_args) -> None:
        if self._selected_key is None or self._transfer_source is None:
            self.transfer_model.set_rows([])
            self.frame_model.set_transfer(None)
            return
        transfers = list(self._transfer_source(self._selected_key))
        if self.problems_only.isChecked():
            transfers = [t for t in transfers if not t.complete]
        self.transfer_model.set_rows(transfers, self._time_base)
        if transfers:
            self.transfer_view.selectRow(0)
        else:
            self.frame_model.set_transfer(None)
            self.frames_note.setText("")

    def _on_transfer_selected(self, *_args) -> None:
        indexes = self.transfer_view.selectionModel().selectedRows()
        if not indexes:
            return
        transfer = self.transfer_model.row_at(indexes[0].row())
        self.frame_model.set_transfer(transfer, self._time_base)
        if transfer is None:
            self.frames_note.setText("")
            return
        note = "{} frame{}".format(transfer.frame_count,
                                   "" if transfer.frame_count == 1 else "s")
        if transfer.flow_control:
            note += " · flow control observed"
        if transfer.detail:
            note += " · " + transfer.detail
        self.frames_note.setText(note)

    def _on_frame_activated(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        facts = self.frame_model._facts[index.row()]
        self.frameActivated.emit(facts.frame)

    # -- for callers ----------------------------------------------------

    def selected_key(self) -> Optional[str]:
        return self._selected_key

    #: Initial column widths. Set explicitly rather than measured: the counts
    #: are narrow numbers under wide headers, and sizing to contents gave every
    #: one of them the width of the word "Transfers" — which pushed Evidence,
    #: the column the page exists for, off the right-hand edge.
    _SUMMARY_WIDTHS = (78, 74, 64, 52, 46, 46, 46, 56, 76, 76, 58, 74)
    _TRANSFER_WIDTHS = (86, 78, 66, 62, 104, 82, 180)
    _FRAME_WIDTHS = (74, 70, 42, 46, 58, 84, 74, 152, 70)

    def size_columns(self) -> None:
        """Give every table its first layout, once."""
        for view, widths in ((self.summary_view, self._SUMMARY_WIDTHS),
                             (self.transfer_view, self._TRANSFER_WIDTHS),
                             (self.frame_view, self._FRAME_WIDTHS)):
            header = view.horizontalHeader()
            for column, width in enumerate(widths):
                if column < header.count():
                    header.resizeSection(column, width)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._sized:
            return
        self._sized = True
        # Split the height deliberately. Stretch factors alone left every
        # section showing a single row in the space this panel actually has,
        # which is worse than useless for a page whose whole job is comparing
        # rows against each other.
        height = max(self.height(), 420)
        self.splitter.setSizes([int(height * 0.42), int(height * 0.33),
                                int(height * 0.25)])
