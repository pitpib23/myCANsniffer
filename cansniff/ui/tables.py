"""Table models for the grouped-ID view and the raw trace view.

Typography carries meaning here: identifiers and payload bytes are monospaced
so digits line up and a changed byte is visible by position, while labels and
counters use the UI face. Secondary columns are dimmed so the eye lands on the
ID and the data first.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from typing import Deque, Dict, List, Optional

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QSize, QSortFilterProxyModel, Qt, QTimer,
)
from PySide6.QtGui import QFontMetrics, QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ..filters import DisplayFilter
from ..model import DEFAULT_BIT_WINDOW, CanFrame, FrameStats
from .theme import ROW_HEIGHT_COMPACT, SPACE_SM, Theme

BYTES_ROLE = Qt.UserRole + 1
MASK_ROLE = Qt.UserRole + 2
SORT_ROLE = Qt.UserRole + 3
KEY_ROLE = Qt.UserRole + 4

#: QSS gives QHeaderView::section 10px of padding either side.
_HEADER_PADDING = 2 * 10 + 8


def exemplar_widths(model, theme: Theme) -> List[int]:
    """Width each column needs for the widest value its format allows.

    Sizing from a per-column exemplar replaces ``QHeaderView.ResizeToContents``,
    which asks the model and the delegate for a hint on up to a thousand rows
    every time the view is resized. These tables grow to hundreds of IDs and
    hundreds of thousands of frames, so that sweep dominates a window resize —
    and because it follows whatever happens to be on screen, it also makes the
    columns twitch as frames arrive. Every column here holds a bounded format
    (an arbitration ID, a byte count, a timestamp), so one exemplar per column
    is both cheaper and steadier.
    """
    # The views are attached through a proxy; the exemplars live on the source.
    source = getattr(model, "sourceModel", None)
    if callable(source):
        model = source() or model

    metrics_cache: Dict[tuple, QFontMetrics] = {}
    header_metrics = QFontMetrics(theme.ui_font(bold=True))
    widths: List[int] = []
    for column, (text, mono, bold) in enumerate(model.COLUMN_EXEMPLARS):
        key = (mono, bold)
        metrics = metrics_cache.get(key)
        if metrics is None:
            metrics = QFontMetrics(theme.mono_font(bold=bold) if mono
                                   else theme.ui_font(bold=bold))
            metrics_cache[key] = metrics
        widths.append(max(
            metrics.horizontalAdvance(text) + 2 * SPACE_SM + 6,
            header_metrics.horizontalAdvance(model.COLUMNS[column]) + _HEADER_PADDING,
        ))
    return widths


class ByteHighlightDelegate(QStyledItemDelegate):
    """Draws payload bytes, tinting the ones that have changed since first seen."""

    def __init__(self, theme: Theme, parent=None, enabled: bool = True):
        super().__init__(parent)
        self._theme = theme
        self.enabled = enabled
        # Cached because paint() runs per visible row on every repaint.
        self._font = theme.mono_font()
        self._metrics = QFontMetrics(self._font)
        self._advance = self._metrics.horizontalAdvance("FF")
        self._space = self._metrics.horizontalAdvance(" ")

    def invalidate_fonts(self) -> None:
        self._font = self._theme.mono_font()
        self._metrics = QFontMetrics(self._font)
        self._advance = self._metrics.horizontalAdvance("FF")
        self._space = self._metrics.horizontalAdvance(" ")

    def sizeHint(self, option, index) -> QSize:
        """Size from the monospace run this delegate actually draws.

        The default hint measures the DisplayRole string with the view's UI
        font, which is narrower than the monospace face used here — the column
        then comes out too small and the payload is elided.
        """
        data = index.data(BYTES_ROLE)
        if data is None:
            return super().sizeHint(option, index)
        count = len(data)
        width = count * self._advance + max(0, count - 1) * self._space
        return QSize(width + 2 * SPACE_SM, max(ROW_HEIGHT_COMPACT, self._metrics.height() + 8))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        data = index.data(BYTES_ROLE)
        if not self.enabled or data is None:
            super().paint(painter, option, index)
            return
        mask = index.data(MASK_ROLE)

        theme = self._theme
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, theme.color("selection"))

        painter.save()
        painter.setFont(self._font)

        x = option.rect.left() + SPACE_SM
        top = option.rect.top()
        height = option.rect.height()
        width = self._advance
        step = width + self._space
        right_edge = option.rect.right() - 2

        normal = theme.color("text")
        changed_color = theme.color("accent")

        for i, byte in enumerate(data):
            if x + width > right_edge:
                painter.setPen(theme.color("text_muted"))
                painter.drawText(x, top, self._metrics.horizontalAdvance("…"), height,
                                 Qt.AlignVCenter | Qt.AlignLeft, "…")
                break
            changed = mask is not None and i < len(mask) and mask[i]
            painter.setPen(changed_color if changed else normal)
            painter.drawText(x, top, width, height,
                             Qt.AlignVCenter | Qt.AlignLeft, "{:02X}".format(byte))
            x += step
        painter.restore()


class IdTableModel(QAbstractTableModel):
    """One row per (channel, ID) with counters and the latest payload."""

    #: Sidebar-width column set. The frame count is not dropped, only moved:
    #: it is shown in the message summary on the right, where there is room.
    COLUMNS = ["ID", "Ch", "Type", "Bytes", "Rate", "Last", "Payload"]
    #: (widest expected value, monospace, bold) per column, for exemplar_widths.
    #: Payload is stretched to whatever is left, so its entry is unused.
    COLUMN_EXEMPLARS = [
        ("0x1FFFFFFF", True, True),     # widest 29-bit arbitration ID
        ("8888", False, False),         # channel
        ("29-bit FD error", False, False),
        ("64", True, False),            # byte count, CAN FD maximum
        ("8888.8 Hz", True, False),
        ("99999.99s", True, False),
        ("", True, False),              # payload: stretched
    ]
    _MONO_COLUMNS = {0, 3, 4, 5, 6}
    _MUTED_COLUMNS = {1, 2, 5}
    _NUMERIC_COLUMNS = {3, 4, 5}

    def __init__(self, theme: Theme, bit_window: int = DEFAULT_BIT_WINDOW,
                 parent=None):
        super().__init__(parent)
        self._theme = theme
        #: Applied to stats created from here on; IDs already seen keep theirs.
        #: 0 disables per-bit tracking entirely.
        self.bit_window = max(0, int(bit_window))
        self._stats: "OrderedDict[str, FrameStats]" = OrderedDict()
        self._order: List[str] = []
        self._row_of: Dict[str, int] = {}
        self._time_base: Optional[float] = None
        self.relative_timestamps = True

    # -- Qt interface ---------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._order)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return self.COLUMNS[section]
        if role == Qt.TextAlignmentRole and section in (3, 4, 5, 7):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        stats = self._stats.get(self._order[index.row()])
        if stats is None:
            return None
        frame = stats.frame
        column = index.column()

        if role == Qt.FontRole:
            # The ID is what users scan for first, so it carries weight.
            if column == 0:
                return self._theme.mono_font(bold=True)
            return (self._theme.mono_font() if column in self._MONO_COLUMNS
                    else self._theme.ui_font())
        if role == Qt.ForegroundRole:
            if column in self._MUTED_COLUMNS:
                return self._theme.color("text_muted")
            if column in (0, 6):
                return self._theme.color("text")
            return self._theme.color("text_secondary")
        if role == Qt.TextAlignmentRole and column in self._NUMERIC_COLUMNS:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == KEY_ROLE:
            return stats.key
        if role == BYTES_ROLE and column == 6:
            return frame.data
        if role == MASK_ROLE and column == 6:
            return stats.changed_mask
        if role == SORT_ROLE:
            return self._sort_value(stats, column)
        if role != Qt.DisplayRole:
            return None

        if column == 0:
            return "0x" + frame.id_hex
        if column == 1:
            return frame.channel
        if column == 2:
            return self._format_marks(frame)
        if column == 3:
            return str(len(frame.data))
        if column == 4:
            rate = stats.rate_hz
            if rate <= 0:
                return "—"
            return "{:.1f} Hz".format(rate) if rate >= 1.0 else "{:.2f} Hz".format(rate)
        if column == 5:
            return "{:.2f}s".format(self._display_time(stats.last_seen))
        if column == 6:
            return frame.data_hex
        return None

    @staticmethod
    def _format_marks(frame: CanFrame) -> str:
        marks = "29-bit" if frame.is_extended else "11-bit"
        if frame.is_fd:
            marks += " FD"
        if frame.is_error_state_indicator:
            marks += " ESI"
        if frame.is_error_frame:
            marks += " error"
        if frame.is_remote_frame:
            marks += " remote"
        return marks

    def _sort_value(self, stats: FrameStats, column: int):
        frame = stats.frame
        if column == 0:
            return frame.arb_id
        if column == 3:
            return len(frame.data)
        if column == 4:
            return stats.rate_hz
        if column == 5:
            return stats.last_seen
        if column == 1:
            return frame.channel
        if column == 2:
            return self._format_marks(frame)
        return frame.data_hex

    # -- content --------------------------------------------------------

    def _display_time(self, timestamp: float) -> float:
        if self.relative_timestamps and self._time_base is not None:
            return timestamp - self._time_base
        return timestamp

    def add_frames(self, frames: List[CanFrame]) -> None:
        new_keys: List[str] = []
        touched: set = set()

        for frame in frames:
            if self._time_base is None:
                self._time_base = frame.timestamp
            key = frame.key
            stats = self._stats.get(key)
            if stats is None:
                stats = FrameStats(key=key, frame=frame, bit_window=self.bit_window)
                self._stats[key] = stats
                new_keys.append(key)
            else:
                touched.add(key)
            stats.update(frame)

        if new_keys:
            start = len(self._order)
            self.beginInsertRows(QModelIndex(), start, start + len(new_keys) - 1)
            for offset, key in enumerate(new_keys):
                self._row_of[key] = start + offset
            self._order.extend(new_keys)
            self.endInsertRows()

        if not touched:
            return

        # Repaint only the rows that actually received a frame. Emitting one
        # range over the whole table forces every visible cell of every ID to
        # be re-fetched and redrawn, even when a single ID moved.
        last_column = len(self.COLUMNS) - 1
        rows = sorted(self._row_of[key] for key in touched if key in self._row_of)
        run_start = None
        previous = None
        for row in rows:
            if run_start is None:
                run_start = previous = row
                continue
            if row == previous + 1:
                previous = row
                continue
            self.dataChanged.emit(self.index(run_start, 0), self.index(previous, last_column))
            run_start = previous = row
        if run_start is not None:
            self.dataChanged.emit(self.index(run_start, 0), self.index(previous, last_column))

    def clear(self) -> None:
        self.beginResetModel()
        self._stats.clear()
        self._order.clear()
        self._row_of.clear()
        self._time_base = None
        self.endResetModel()

    def stats_at(self, row: int) -> Optional[FrameStats]:
        if 0 <= row < len(self._order):
            return self._stats.get(self._order[row])
        return None

    def stats_for_key(self, key: str) -> Optional[FrameStats]:
        return self._stats.get(key)

    def row_for_key(self, key: str) -> int:
        return self._row_of.get(key, -1)

    @property
    def id_count(self) -> int:
        return len(self._order)

    @property
    def time_base(self) -> float:
        """Timestamp the relative "Last" column counts from."""
        return self._time_base or 0.0


class IdFilterProxy(QSortFilterProxyModel):
    """Applies the display filter and sorting to the per-ID table.

    Dynamic re-sorting stays off on purpose: counters change on every batch, so
    sorting by Count or Rate would make rows reshuffle continuously under the
    pointer. Instead the view is re-sorted only when a *new* ID appears, which
    is rare — without that, a newly seen ID was appended below the sorted rows
    and stayed out of order.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(False)
        self._filter = DisplayFilter()
        self._resort_pending = False

    def setSourceModel(self, model) -> None:
        previous = self.sourceModel()
        if previous is not None:
            try:
                previous.rowsInserted.disconnect(self._schedule_resort)
            except (RuntimeError, TypeError):
                pass
        super().setSourceModel(model)
        if model is not None:
            model.rowsInserted.connect(self._schedule_resort)

    def _schedule_resort(self, *_args) -> None:
        # Deferred: re-sorting while the insertion signal is still being
        # delivered is not safe, and a burst of new IDs then costs one pass.
        if not self._resort_pending:
            self._resort_pending = True
            QTimer.singleShot(0, self._resort)

    def _resort(self) -> None:
        self._resort_pending = False
        self.invalidate()

    def set_filter(self, display_filter: DisplayFilter) -> None:
        if display_filter == self._filter:
            return
        self._filter = display_filter
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self._filter.is_active:
            return True
        stats = self.sourceModel().stats_at(source_row)
        return stats is not None and self._filter.matches(stats.frame)


class TraceTableModel(QAbstractTableModel):
    """Bounded scrollback of individual received frames."""

    COLUMNS = ["Time (s)", "Ch", "CAN ID", "Type", "Bytes", "Payload"]
    #: See IdTableModel.COLUMN_EXEMPLARS.
    COLUMN_EXEMPLARS = [
        ("99999.999999", True, False),  # relative timestamp, 6 decimals
        ("8888", False, False),
        ("0x1FFFFFFF", True, True),
        ("29-bit FD error", False, False),
        ("64", True, False),
        ("", True, False),              # payload: stretched
    ]
    _MONO_COLUMNS = {0, 2, 4, 5}
    _MUTED_COLUMNS = {1, 3}

    def __init__(self, theme: Theme, max_rows: int = 200000, parent=None):
        super().__init__(parent)
        self._theme = theme
        #: Everything retained, so clearing a filter restores the full history.
        self._all: "Deque[CanFrame]" = deque()
        #: The subset currently displayed. Filtering happens here rather than
        #: in a QSortFilterProxyModel: the trace can hold 200k rows, and a
        #: proxy would maintain a full row mapping on every insert.
        self._rows: List[CanFrame] = []
        self._filter = DisplayFilter()
        self._max_rows = max(100, int(max_rows))
        self._time_base: Optional[float] = None
        self.relative_timestamps = True

    def set_filter(self, display_filter: DisplayFilter) -> None:
        if display_filter == self._filter:
            return
        self._filter = display_filter
        self.beginResetModel()
        if display_filter.is_active:
            self._rows = [f for f in self._all if display_filter.matches(f)]
        else:
            self._rows = list(self._all)
        self.endResetModel()

    @property
    def total_rows(self) -> int:
        return len(self._all)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return self.COLUMNS[section]
        if role == Qt.TextAlignmentRole and section in (0, 4):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        frame = self._rows[index.row()]
        column = index.column()

        if role == Qt.FontRole:
            if column == 2:
                return self._theme.mono_font(bold=True)
            return (self._theme.mono_font() if column in self._MONO_COLUMNS
                    else self._theme.ui_font())
        if role == Qt.ForegroundRole:
            if column in self._MUTED_COLUMNS:
                return self._theme.color("text_muted")
            if column in (2, 5):
                return self._theme.color("text")
            return self._theme.color("text_secondary")
        if role == Qt.TextAlignmentRole and column in (0, 4):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role != Qt.DisplayRole:
            return None

        if column == 0:
            timestamp = frame.timestamp
            if self.relative_timestamps and self._time_base is not None:
                timestamp -= self._time_base
            return "{:.6f}".format(timestamp)
        if column == 1:
            return frame.channel
        if column == 2:
            return "0x" + frame.id_hex
        if column == 3:
            return IdTableModel._format_marks(frame)
        if column == 4:
            return str(len(frame.data))
        if column == 5:
            return frame.data_hex
        return None

    def add_frames(self, frames: List[CanFrame]) -> None:
        if not frames:
            return
        if self._time_base is None:
            self._time_base = frames[0].timestamp

        self._all.extend(frames)
        active = self._filter.is_active
        matching = [f for f in frames if self._filter.matches(f)] if active else frames

        if matching:
            start = len(self._rows)
            self.beginInsertRows(QModelIndex(), start, start + len(matching) - 1)
            self._rows.extend(matching)
            self.endInsertRows()

        overflow = len(self._all) - self._max_rows
        if overflow > 0:
            evicted = 0
            for _ in range(overflow):
                oldest = self._all.popleft()
                if not active or self._filter.matches(oldest):
                    evicted += 1
            if evicted:
                self.beginRemoveRows(QModelIndex(), 0, evicted - 1)
                del self._rows[:evicted]
                self.endRemoveRows()

    def frame_at(self, row: int) -> Optional[CanFrame]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def set_max_rows(self, value: int) -> None:
        self._max_rows = max(100, int(value))

    def clear(self) -> None:
        self.beginResetModel()
        self._all.clear()
        self._rows.clear()
        self._time_base = None
        self.endResetModel()
