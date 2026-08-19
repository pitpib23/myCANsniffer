"""Reusable presentation widgets built from the design tokens.

Nothing here knows about CAN. These are the visual primitives the panels are
assembled from: chips, section headers, empty states, a segmented control, the
payload byte strip, and the delegates that paint the interpretation table.
"""

from __future__ import annotations

from typing import Dict, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QFontMetricsF, QPainter, QPainterPath, QPen,
)
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QVBoxLayout, QWidget,
)  # noqa: F401  (QSizePolicy used by NavRail and PayloadStrip)

from .theme import RADIUS_MD, RADIUS_SM, ROW_HEIGHT, SPACE_SM, SPACE_XS, Theme

# Item-data roles used by the interpretation table delegates.
MONO_ROLE = Qt.UserRole + 13        # bool: render with the monospace font
TONE_ROLE = Qt.UserRole + 14        # str: neutral | strong | secondary | muted | success


# ---------------------------------------------------------------------------
# small building blocks
# ---------------------------------------------------------------------------


class Chip(QLabel):
    """A compact pill for metadata: byte count, DLC, rate, frame format."""

    def __init__(self, text: str = "", tone: str = "neutral",
                 theme: Optional[Theme] = None, parent=None):
        super().__init__(text, parent)
        self._theme = theme or Theme()
        self._tone = tone
        self.setAlignment(Qt.AlignCenter)
        self.apply_tone(tone)

    def apply_tone(self, tone: str) -> None:
        self._tone = tone
        theme = self._theme
        mapping = {
            "neutral": ("overlay", "text_secondary", "border"),
            "accent": ("accent_wash", "accent", "accent"),
            "success": ("success_wash", "success", "success"),
            "warning": ("warning_wash", "warning", "warning"),
            "danger": ("danger_wash", "danger", "danger"),
            "muted": ("surface", "text_muted", "border_subtle"),
        }
        background, foreground, border = mapping.get(tone, mapping["neutral"])
        self.setStyleSheet(
            "background-color: {bg}; color: {fg}; border: 1px solid {bd};"
            "border-radius: {r}px; padding: 2px 8px; font-size: {size:g}pt;"
            "font-weight: 600;".format(
                bg=theme.hex(background), fg=theme.hex(foreground),
                bd=theme.hex(border), r=RADIUS_SM, size=theme.ui_size - 1,
            )
        )

    def set_text_and_tone(self, text: str, tone: str) -> None:
        self.setText(text)
        if tone != self._tone:
            self.apply_tone(tone)

    def restyle(self) -> None:
        self.apply_tone(self._tone)


class SectionLabel(QLabel):
    """Small uppercase label that titles a region without shouting."""

    def __init__(self, text: str, theme: Theme, parent=None):
        super().__init__(text, parent)
        self._theme = theme
        self.setObjectName("SectionLabel")
        self.setFont(theme.label_font())

    def restyle(self) -> None:
        self.setFont(self._theme.label_font())


class Divider(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Divider")
        self.setFrameShape(QFrame.HLine)
        self.setFixedHeight(1)


class CurrentPageStack(QStackedWidget):
    """A QStackedWidget sized to its *current* page only.

    Plain QStackedWidget reserves room for every page it holds, including
    ones nobody can currently see — its own minimumSizeHint() is the max
    over all pages, since Qt has no way to know a hidden page will never
    become current. For a workspace switcher whose pages genuinely differ in
    footprint, that means the smallest page is held hostage to the largest
    one's minimum size forever — inflating the whole top-level window's
    minimum size well past what the currently visible content needs, which
    a maximized top-level window can be silently resized to satisfy the
    moment anything elsewhere invalidates its layout (see MainWindow's
    capture-control buttons).

    Switching pages never shrinks an already-larger window: sizeHint() only
    ever matters as a floor/preference when there is slack to give it, not
    by forcibly shrinking a window that is already bigger — so this does
    not make switching tabs visually jump around.
    """

    def setCurrentIndex(self, index: int) -> None:
        super().setCurrentIndex(index)
        self.updateGeometry()

    def setCurrentWidget(self, widget: QWidget) -> None:
        super().setCurrentWidget(widget)
        self.updateGeometry()

    def sizeHint(self) -> QSize:
        current = self.currentWidget()
        return current.sizeHint() if current is not None else super().sizeHint()

    def minimumSizeHint(self) -> QSize:
        current = self.currentWidget()
        return (current.minimumSizeHint() if current is not None
                else super().minimumSizeHint())


def scrollable(content: QWidget) -> QScrollArea:
    """Wrap a page that can genuinely need more room than the viewport gives
    it, so the overflow scrolls instead of either clipping below the
    window's bottom edge or dragging the top-level window's minimum size up
    to fit it.

    The second half matters as much as the first: a CurrentPageStack (above)
    reports whichever page is current's own minimumSizeHint() as its own —
    by design, so the stack is never held hostage to the *largest* page
    while showing a small one. A page with a real, largely fixed minimum
    footprint of its own (Plot's control row; ISO-TP's splitter, whose
    panes each refuse to show fewer than a handful of rows) is fine in
    itself but, reported directly as "the current page's minimum", would
    still propagate through that stack, through every ancestor layout, up
    to the QMainWindow — which, being top-level, has Qt apply its own
    layout's computed minimum size to the *native window* (WM_GETMINMAXINFO
    and friends). A maximized or fullscreen window whose current geometry
    no longer satisfies that freshly-grown minimum gets resized by the
    window manager to fit it — exactly the "selecting a tool un-maximizes
    or resizes the window" bug this function exists to prevent. A
    QScrollArea's own minimumSizeHint is small and constant regardless of
    its contents, so wrapping a page here is what keeps "the current page's
    own minimum" — the thing CurrentPageStack (deliberately) still reports
    upward — always small too.

    Not every page needs this: one that is already a QAbstractScrollArea
    (a QTableWidget/QTableView behind an empty-state page, for instance)
    already handles its own overflow row-by-row, and wrapping it a second
    time would just nest two scrollbars over the same content for no
    benefit. Use this only for a page whose own natural size can exceed the
    viewport as a *whole page*, not row-by-row.
    """
    scroll = QScrollArea()
    scroll.setObjectName("WorkspaceScroll")
    scroll.setFrameShape(QFrame.NoFrame)
    # Resizable: the content widget is resized to the viewport rather than
    # kept at its own sizeHint, so it expands to fill genuinely available
    # room and only overflows -- triggering a scrollbar -- when the
    # viewport is smaller than the content's own minimum.
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    scroll.setWidget(content)
    return scroll


class MetricChip(QWidget):
    """Label + value pair for the status bar."""

    def __init__(self, label: str, theme: Theme, tone: str = "neutral", parent=None):
        super().__init__(parent)
        self._theme = theme
        self._tone = tone

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_XS + 2)

        self.label = QLabel(label)
        self.value = QLabel("0")
        self.restyle()

        row.addWidget(self.label)
        row.addWidget(self.value)

    def restyle(self) -> None:
        theme = self._theme
        self.label.setStyleSheet(
            "color: {}; font-size: {:g}pt;".format(
                theme.hex("text_muted"), theme.ui_size - 1
            )
        )
        self.value.setFont(theme.mono_font(-0.5, bold=True))
        self._apply_tone(self._tone)

    def _apply_tone(self, tone: str) -> None:
        token = {"neutral": "text", "success": "success",
                 "warning": "warning", "danger": "danger",
                 "muted": "text_muted"}.get(tone, "text")
        self.value.setStyleSheet("color: {};".format(self._theme.hex(token)))

    def set_value(self, text: str, tone: Optional[str] = None) -> None:
        self.value.setText(text)
        if tone is not None and tone != self._tone:
            self._tone = tone
            self._apply_tone(tone)


class EmptyState(QWidget):
    """Shown instead of a table when there is genuinely nothing to display."""

    def __init__(self, title: str, subtitle: str, theme: Theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(SPACE_SM)

        self.title_label = QLabel(title)
        self.title_label.setAlignment(Qt.AlignCenter)

        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setAlignment(Qt.AlignCenter)
        self.subtitle_label.setWordWrap(True)
        self.restyle()

        layout.addWidget(self.title_label)
        layout.addWidget(self.subtitle_label)

    def restyle(self) -> None:
        theme = self._theme
        self.title_label.setFont(theme.ui_font(1.5, bold=True))
        self.title_label.setStyleSheet("color: {};".format(theme.hex("text_secondary")))
        self.subtitle_label.setStyleSheet(
            "color: {}; font-size: {:g}pt;".format(theme.hex("text_muted"), theme.ui_size)
        )

    def set_text(self, title: str, subtitle: str) -> None:
        self.title_label.setText(title)
        self.subtitle_label.setText(subtitle)


class NavRail(QFrame):
    """Compact vertical navigation.

    Deliberately narrow: it replaces a panel that took half the window, and
    every pixel it gives back goes to the table. Each destination is a painted
    glyph plus a short label, so the meaning never rests on the icon alone.
    """

    changed = Signal(int)

    WIDTH = 76

    def __init__(self, destinations: Sequence[Tuple[str, str, str]],
                 theme: Theme, current: int = 0, parent=None,
                 toggleable: Optional[Sequence[int]] = None):
        """``destinations`` is a sequence of ``(glyph, label, tooltip)``.

        ``toggleable``: which destination indexes double as a collapse
        control for a panel beside them -- see update_hints. Defaults to
        every destination (the original, and still the common, case); a
        destination *not* listed there replaces the whole workspace on its
        own rather than opening/closing a panel next to this rail, so its
        own tooltip never changes to "Show X"/"Hide X" the way a toggleable
        one's does.
        """
        super().__init__(parent)
        self.setObjectName("NavRail")
        self.setFixedWidth(self.WIDTH)
        self._theme = theme
        self._destinations = list(destinations)
        self._toggleable = (set(range(len(self._destinations)))
                            if toggleable is None else set(toggleable))

        column = QVBoxLayout(self)
        column.setContentsMargins(SPACE_XS + 2, SPACE_SM, SPACE_XS + 2, SPACE_SM)
        column.setSpacing(SPACE_XS)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for index, (glyph, label, tooltip) in enumerate(destinations):
            button = _NavButton(glyph, label, theme)
            button.setToolTip(tooltip)
            button.setChecked(index == current)
            self.group.addButton(button, index)
            column.addWidget(button)
        column.addStretch(1)
        self.group.idClicked.connect(self.changed.emit)

    def set_indicator(self, index: Optional[int]) -> None:
        """Show the leading-edge bar on exactly ``index`` (or none of them,
        when ``index`` is None).

        This is deliberately a *separate* concept from which button is
        checked (see _NavButton.set_indicator's own docstring): the current
        page's own highlight (the accent wash, driven by isChecked() alone)
        must stay on regardless of this call, so a caller collapsing the
        current page's selector clears the indicator without the page
        itself ever looking unselected. The caller decides what ``index``
        means -- typically "the current page, but only while its own
        selector is on" -- this method only ever paints exactly one
        button's bar, or none.
        """
        for position in range(len(self._destinations)):
            button = self.group.button(position)
            if button is not None:
                button.set_indicator(position == index)

    def set_current(self, index: int) -> None:
        button = self.group.button(index)
        if button is not None:
            button.setChecked(True)

    def current(self) -> int:
        return self.group.checkedId()

    def update_hints(self, collapsed: bool) -> None:
        """Retarget the tooltips at what a click will actually do.

        A toggleable destination (see __init__) doubles as the show/hide
        control for the panel beside it, so "Trace" means *open the trace*
        when the panel is hidden and *hide it* when that section is already
        the one on screen. A non-toggleable one (ISO-TP) replaces the whole
        workspace outright rather than opening or closing anything beside
        this rail, so it always keeps its own plain, unchanging tooltip.
        """
        current = self.current()
        for index, (_glyph, label, tooltip) in enumerate(self._destinations):
            button = self.group.button(index)
            if button is None:
                continue
            if index not in self._toggleable:
                hint = tooltip
            elif collapsed:
                hint = "Show {}".format(label.lower())
            elif index == current:
                hint = "Hide {}".format(label.lower())
            else:
                hint = tooltip
            button.setToolTip(hint)
            button.setAccessibleDescription(hint)

    def restyle(self) -> None:
        for button in self.group.buttons():
            button.restyle()


class _NavButton(QPushButton):
    """One nav destination: a simple painted glyph above a short caption."""

    HEIGHT = 58

    def __init__(self, glyph: str, label: str, theme: Theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._glyph = glyph
        self._label = label
        #: The leading-edge bar -- independent of isChecked() (which page is
        #: current). See set_indicator.
        self._show_indicator = False
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self.setAccessibleName(label)
        self.setObjectName("NavButton")
        # Height comes from sizeHint, not setFixedHeight: applying a stylesheet
        # re-polishes the widget and resets the min/max size constraints that
        # setFixedHeight installs, collapsing the button to the QSS min-height.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(64, self.HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def set_indicator(self, shown: bool) -> None:
        """The leading-edge bar -- a *second*, independent thing from
        ``isChecked()`` (which page is current). See NavRail.set_indicator
        for what this represents: "the current page's own selector is on",
        never "this is the current page" by itself -- that is checked()
        alone, unconditionally, so the accent-wash highlight below never
        needs this to be true.
        """
        shown = bool(shown)
        if shown != self._show_indicator:
            self._show_indicator = shown
            self.update()

    def restyle(self) -> None:
        self.update()

    def paintEvent(self, event) -> None:
        theme = self._theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect())
        # The highlight means "this is the currently open page" and nothing
        # else -- it must stay on regardless of whether that page's own
        # selector happens to be shown or hidden (see MainWindow's own
        # _selector_on / current-page split). Only the leading bar below
        # additionally depends on _show_indicator.
        current = self.isChecked()

        if current:
            path = QPainterPath()
            path.addRoundedRect(rect.adjusted(2, 1, -2, -1), RADIUS_MD, RADIUS_MD)
            painter.fillPath(path, theme.color("accent_wash"))
        elif self.underMouse():
            path = QPainterPath()
            path.addRoundedRect(rect.adjusted(2, 1, -2, -1), RADIUS_MD, RADIUS_MD)
            painter.fillPath(path, theme.color("hover"))

        if current and self._show_indicator:
            # A solid bar on the leading edge makes "this page's selector is
            # open" obvious even at a glance, without relying on the wash
            # alone -- but only when the selector genuinely is open; the
            # wash above already says "current page" on its own.
            marker = QRectF(rect.left(), rect.top() + 10, 3, rect.height() - 20)
            bar = QPainterPath()
            bar.addRoundedRect(marker, 1.5, 1.5)
            painter.fillPath(bar, theme.color("accent"))

        pen = theme.color("accent") if current else theme.color("text_secondary")
        painter.setPen(QPen(pen, 1.6))
        self._paint_glyph(painter, QRectF(rect.center().x() - 9, rect.top() + 11, 18, 18))

        painter.setFont(theme.ui_font(-1.5, bold=current))
        painter.setPen(pen)
        painter.drawText(
            QRectF(rect.left(), rect.top() + 33, rect.width(), 18),
            Qt.AlignCenter, self._label,
        )
        painter.end()

    def _paint_glyph(self, painter: QPainter, box: QRectF) -> None:
        if self._glyph == "list":
            # Three stacked rows: a grouped list of messages.
            for i in range(3):
                y = box.top() + 3 + i * 6
                painter.drawLine(int(box.left()), int(y), int(box.left() + 3), int(y))
                painter.drawLine(int(box.left() + 6), int(y), int(box.right()), int(y))
        elif self._glyph == "stream":
            # A stepped waveform: frames over time.
            points = [
                (box.left(), box.bottom() - 3),
                (box.left() + 4, box.bottom() - 3),
                (box.left() + 4, box.top() + 3),
                (box.left() + 10, box.top() + 3),
                (box.left() + 10, box.bottom() - 3),
                (box.right(), box.bottom() - 3),
            ]
            for (x1, y1), (x2, y2) in zip(points, points[1:]):
                painter.drawLine(int(x1), int(y1), int(x2), int(y2))
        elif self._glyph == "chain":
            # Three separate frames, linked into one reassembled transfer.
            size = 5.0
            nodes = [
                (box.left(), box.bottom() - size),
                (box.left() + (box.width() - size) / 2, box.top() + (box.height() - size) / 2),
                (box.right() - size, box.top()),
            ]
            for x, y in nodes:
                painter.drawRect(QRectF(x, y, size, size))
            for (x1, y1), (x2, y2) in zip(nodes, nodes[1:]):
                painter.drawLine(
                    int(x1 + size), int(y1 + size / 2),
                    int(x2), int(y2 + size / 2),
                )


class FilterChip(QFrame):
    """One active filter, with a control to remove just that filter."""

    removed = Signal(str)

    def __init__(self, field: str, name: str, value: str, theme: Theme, parent=None):
        super().__init__(parent)
        self.setObjectName("FilterChip")
        self._field = field
        self._theme = theme

        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_SM, 2, SPACE_XS, 2)
        row.setSpacing(SPACE_XS)

        self.label = QLabel("{}: {}".format(name, value))
        self.label.setFont(theme.ui_font(-0.5))
        row.addWidget(self.label)

        self.close_button = QPushButton("✕")
        self.close_button.setObjectName("ChipClose")
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.setFixedSize(16, 16)
        self.close_button.setToolTip("Remove this filter")
        self.close_button.setAccessibleName("Remove filter {}".format(name))
        self.close_button.clicked.connect(lambda: self.removed.emit(self._field))
        row.addWidget(self.close_button)

    def restyle(self) -> None:
        self.label.setFont(self._theme.ui_font(-0.5))


class Segmented(QWidget):
    """Mutually exclusive button group styled as one control."""

    changed = Signal(int)

    def __init__(self, options: Sequence[str], current: int = 0, parent=None):
        super().__init__(parent)
        self.setObjectName("Segmented")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for index, text in enumerate(options):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setChecked(index == current)
            self.group.addButton(button, index)
            row.addWidget(button)
        self.group.idClicked.connect(self.changed.emit)
        self._locked = False

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._locked:
            return
        # Pin widths from the text itself. sizeHint alone is unreliable here:
        # it can be sampled before the stylesheet padding is applied, which
        # lets a crowded row squeeze labels until they clip ("2 bytes" -> "2 byte").
        self._locked = True
        for button in self.group.buttons():
            metrics = QFontMetrics(button.font())
            needed = metrics.horizontalAdvance(button.text()) + 2 * _SEGMENT_PADDING
            button.setMinimumWidth(max(needed, button.sizeHint().width()))

    def set_current(self, index: int) -> None:
        button = self.group.button(index)
        if button is not None:
            button.setChecked(True)

    def current(self) -> int:
        return self.group.checkedId()


# ---------------------------------------------------------------------------
# payload byte strip and bit activity
# ---------------------------------------------------------------------------

#: Left gutter shared by the payload strip and the bit matrix so their byte
#: columns line up exactly. The strip leaves it empty; the matrix labels its
#: bit rows in it.
PAYLOAD_GUTTER = 30


def payload_cell_width(theme: Theme) -> int:
    """Width of one byte column. Shared, so the strip and matrix stay aligned."""
    return max(38, QFontMetrics(theme.mono_font(1.0)).horizontalAdvance("FF") + 20)


def activity_color(base: QColor, intensity: float) -> QColor:
    """Wash for a given activity, from barely-there to solid."""
    wash = QColor(base)
    wash.setAlphaF(max(0.0, min(1.0, 0.06 + 0.88 * intensity)))
    return wash


class ActivityLegend(QWidget):
    """Key for the bit matrix wash: pale means rare, solid means every frame."""

    SWATCH = 13
    STEPS = 5

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._metrics = QFontMetrics(theme.ui_font(-2.0))
        self._lead = "rarely"
        self._tail = "every frame"
        self.setFixedHeight(self.SWATCH + 2)
        self._recompute()

    def _recompute(self) -> None:
        self._lead_w = self._metrics.horizontalAdvance(self._lead)
        self._tail_w = self._metrics.horizontalAdvance(self._tail)
        self.setFixedWidth(self._lead_w + self._tail_w
                           + self.STEPS * self.SWATCH + 4 * SPACE_XS)

    def restyle(self) -> None:
        self._metrics = QFontMetrics(self._theme.ui_font(-2.0))
        self._recompute()
        self.update()

    def paintEvent(self, event) -> None:
        theme = self._theme
        painter = QPainter(self)
        painter.setFont(theme.ui_font(-2.0))
        painter.setPen(theme.color("text_muted"))

        height = self.height()
        painter.drawText(QRectF(0, 0, self._lead_w, height),
                         Qt.AlignLeft | Qt.AlignVCenter, self._lead)

        x = self._lead_w + SPACE_XS
        base = theme.color("accent")
        for step in range(self.STEPS):
            intensity = (step + 1) / float(self.STEPS)
            rect = QRectF(x, 1, self.SWATCH - 2, height - 2)
            painter.fillRect(rect, activity_color(base, intensity))
            x += self.SWATCH

        painter.setPen(theme.color("text_muted"))
        painter.drawText(QRectF(x + SPACE_XS, 0, self._tail_w, height),
                         Qt.AlignLeft | Qt.AlignVCenter, self._tail)
        painter.end()


class PayloadStrip(QWidget):
    """The payload as indexed byte cells, with the selected word bracketed.

    This is the anchor between the raw frame and the interpretation table:
    picking a row in the table brackets exactly those bytes here.
    """

    byteClicked = Signal(int)

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._data: bytes = b""
        self._changed: Sequence[int] = ()
        self._highlight: Optional[Tuple[int, int]] = None
        self._highlight_label = ""
        self._hover = -1

        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self._metrics = QFontMetrics(theme.mono_font(1.0))
        self._cell_width = payload_cell_width(theme)
        self.setMinimumHeight(74)

    # -- content --------------------------------------------------------

    def set_payload(self, data: bytes, changed_mask: Sequence[int] = ()) -> None:
        data = bytes(data or b"")
        resized = len(data) != len(self._data)
        self._data = data
        self._changed = changed_mask or ()
        if resized:
            # The width comes from the byte count, so the layout has to be
            # told to re-read sizeHint; otherwise the strip keeps whatever
            # width it had when it was empty and clips its own cells.
            self.updateGeometry()
        self.update()

    def set_highlight(self, offset: Optional[int], length: int = 0, label: str = "") -> None:
        self._highlight = None if offset is None else (offset, length)
        self._highlight_label = label
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(PAYLOAD_GUTTER + max(1, len(self._data)) * self._cell_width, 74)

    def restyle(self) -> None:
        self._metrics = QFontMetrics(self._theme.mono_font(1.0))
        self._cell_width = payload_cell_width(self._theme)
        self.updateGeometry()
        self.update()

    # -- interaction ----------------------------------------------------

    def _index_at(self, x: int) -> int:
        if not self._data or self._cell_width <= 0:
            return -1
        index = int((x - PAYLOAD_GUTTER) // self._cell_width)
        return index if 0 <= index < len(self._data) else -1

    def mouseMoveEvent(self, event) -> None:
        index = self._index_at(event.position().x())
        if index != self._hover:
            self._hover = index
            if index >= 0:
                self.setToolTip(
                    "byte {idx} — 0x{v:02X} / {v} / 0b{v:08b}".format(
                        idx=index, v=self._data[index]
                    )
                )
            else:
                self.setToolTip("")
            self.update()

    def leaveEvent(self, event) -> None:
        self._hover = -1
        self.update()

    def mousePressEvent(self, event) -> None:
        index = self._index_at(event.position().x())
        if index >= 0:
            self.byteClicked.emit(index)

    # -- painting -------------------------------------------------------

    def paintEvent(self, event) -> None:
        if not self._data:
            return
        theme = self._theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        ruler_font = theme.ui_font(-1.5)
        byte_font = theme.mono_font(1.0)
        ruler_height = 16
        cell_top = ruler_height + 2
        cell_height = 32
        width = self._cell_width

        highlight_range = range(0)
        if self._highlight is not None:
            start, length = self._highlight
            highlight_range = range(start, start + length)

        for index, value in enumerate(self._data):
            x = PAYLOAD_GUTTER + index * width
            selected = index in highlight_range
            hovered = index == self._hover
            changed = index < len(self._changed) and self._changed[index]

            # index ruler
            painter.setFont(ruler_font)
            painter.setPen(theme.color("accent" if selected else "text_muted"))
            painter.drawText(
                QRectF(x, 0, width, ruler_height),
                Qt.AlignCenter, str(index),
            )

            # cell
            rect = QRectF(x + 1, cell_top, width - 2, cell_height)
            if selected:
                background = theme.color("accent_wash")
                border = theme.color("accent")
            elif hovered:
                background = theme.color("hover")
                border = theme.color("border_strong")
            else:
                background = theme.color("raised")
                border = theme.color("border_subtle")

            path = QPainterPath()
            path.addRoundedRect(rect, RADIUS_SM, RADIUS_SM)
            painter.fillPath(path, background)
            painter.setPen(QPen(border, 1))
            painter.drawPath(path)

            painter.setFont(byte_font)
            if changed:
                painter.setPen(theme.color("warning"))
            elif selected:
                painter.setPen(theme.color("accent"))
            else:
                painter.setPen(theme.color("text"))
            painter.drawText(rect, Qt.AlignCenter, "{:02X}".format(value))

        # bracket under the highlighted word
        if self._highlight is not None and len(highlight_range):
            start, length = self._highlight
            x0 = PAYLOAD_GUTTER + start * width + 3
            x1 = PAYLOAD_GUTTER + (start + length) * width - 3
            y = cell_top + cell_height + 6
            painter.setPen(QPen(theme.color("accent"), 1.5))
            painter.drawLine(int(x0), y, int(x1), y)
            painter.drawLine(int(x0), y, int(x0), y - 4)
            painter.drawLine(int(x1), y, int(x1), y - 4)
            if self._highlight_label:
                painter.setFont(theme.ui_font(-1.5, bold=True))
                painter.setPen(theme.color("accent"))
                painter.drawText(
                    QRectF(x0, y + 1, max(0.0, x1 - x0), 14),
                    Qt.AlignCenter, self._highlight_label,
                )
        painter.end()


class BitMatrix(QWidget):
    """Per-bit activity for the current payload, aligned under the byte strip.

    One column per payload byte, one row per bit with the most significant at
    the top. The glyph is the bit's value in the latest frame; the wash behind
    it is how often that bit flipped over the last N frames.

    The shapes are what carry the meaning. A rolling counter is a staircase —
    bit 0 flipping every frame, bit 1 half as often, and so on up. A checksum
    is a solid block. Padding is blank. A status flag is one lit cell in a dead
    byte.

    It reports counts and nothing else. It does not label a field, score it or
    suggest what it is; reading the shape is the operator's job.
    """

    byteClicked = Signal(int)

    ROWS = 8
    ROW_HEIGHT = 13
    HEADER = 15

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._data: bytes = b""
        self._flips: Sequence[int] = ()
        self._window = 0
        self._highlight: Optional[Tuple[int, int]] = None
        self._hover: Tuple[int, int] = (-1, -1)

        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self._cell_width = payload_cell_width(theme)
        self._height = self.HEADER + self.ROWS * self.ROW_HEIGHT + 4
        self.setFixedHeight(self._height)
        self._measure_digits()

    def _measure_digits(self) -> None:
        """Offsets that centre a digit, so painting can skip text layout.

        drawText into a rectangle re-runs alignment for every cell, and this
        widget draws one per bit — 512 of them on a 64-byte CAN FD payload.
        Positioning the baseline directly is the same picture for a fraction
        of the work, and monospace means one measurement covers 0 and 1.
        """
        metrics = QFontMetricsF(self._theme.mono_font(-1.0))
        self._digit_dx = (self._cell_width - 2 - metrics.horizontalAdvance("0")) / 2.0
        self._digit_dy = (self.ROW_HEIGHT - 1 + metrics.ascent() - metrics.descent()) / 2.0

    # -- content --------------------------------------------------------

    def set_payload(self, data: bytes, flips: Sequence[int] = (),
                    window: int = 0) -> None:
        data = bytes(data or b"")
        flips = tuple(flips)
        window = max(0, int(window))
        # The panel refreshes ~10 times a second whether or not anything moved,
        # and a full repaint here is hundreds of text draws. Comparing a few
        # dozen integers to skip one is overwhelmingly the better trade.
        if data == self._data and flips == self._flips and window == self._window:
            return
        resized = len(data) != len(self._data)
        self._data = data
        self._flips = flips
        self._window = window
        if resized:
            self.updateGeometry()
        self.update()

    def set_highlight(self, offset: Optional[int], length: int = 0) -> None:
        self._highlight = None if offset is None else (offset, length)
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(PAYLOAD_GUTTER + max(1, len(self._data)) * self._cell_width,
                     self._height)

    def restyle(self) -> None:
        self._cell_width = payload_cell_width(self._theme)
        self._measure_digits()
        self.updateGeometry()
        self.update()

    # -- interaction ----------------------------------------------------

    def _cell_at(self, x: float, y: float) -> Tuple[int, int]:
        if not self._data or self._cell_width <= 0:
            return (-1, -1)
        column = int((x - PAYLOAD_GUTTER) // self._cell_width)
        row = int((y - self.HEADER) // self.ROW_HEIGHT)
        if not (0 <= column < len(self._data)) or not (0 <= row < self.ROWS):
            return (-1, -1)
        return (column, row)

    def mouseMoveEvent(self, event) -> None:
        cell = self._cell_at(event.position().x(), event.position().y())
        if cell == self._hover:
            return
        self._hover = cell
        column, row = cell
        if column < 0:
            self.setToolTip("")
        else:
            bit = self.ROWS - 1 - row          # top row is the MSB
            flips = self._flip_count(column, bit)
            value = (self._data[column] >> bit) & 1
            self.setToolTip(
                "byte {b} bit {n} — currently {v}\n"
                "flipped {f} time{s} in the last {w} frames".format(
                    b=column, n=bit, v=value, f=flips,
                    s="" if flips == 1 else "s", w=self._window,
                )
            )
        self.update()

    def leaveEvent(self, event) -> None:
        self._hover = (-1, -1)
        self.update()

    def mousePressEvent(self, event) -> None:
        column, _row = self._cell_at(event.position().x(), event.position().y())
        if column >= 0:
            self.byteClicked.emit(column)

    def _flip_count(self, byte_index: int, bit: int) -> int:
        position = byte_index * 8 + bit
        if 0 <= position < len(self._flips):
            return self._flips[position]
        return 0

    def _intensity(self, flips: int) -> float:
        """Flip count to ink, on a curve that keeps slow bits visible.

        A counter's high bits flip a few hundred times less often than its low
        bits. On a linear ramp everything above bit 2 would be invisible and
        the staircase — the whole point — would not read.
        """
        if flips <= 0 or self._window <= 0:
            return 0.0
        ratio = min(1.0, flips / float(self._window))
        # Floor: a bit that moved even once must be unmistakably different
        # from one that never moved, which is the first thing you look for.
        return min(1.0, max(0.16, ratio ** 0.4))

    # -- painting -------------------------------------------------------

    def paintEvent(self, event) -> None:
        if not self._data:
            return
        theme = self._theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        width = self._cell_width
        active = theme.color("accent")
        exposed = event.rect()

        painter.setFont(theme.ui_font(-2.0))
        painter.setPen(theme.color("text_muted"))
        painter.drawText(QRectF(0, 0, PAYLOAD_GUTTER - 4, self.HEADER),
                         Qt.AlignRight | Qt.AlignVCenter, "bit")

        highlight_range = range(0)
        if self._highlight is not None:
            start, length = self._highlight
            highlight_range = range(start, start + length)

        label_font = theme.ui_font(-2.0)
        value_font = theme.mono_font(-1.0)
        muted_ink = theme.color("text_muted")
        dark_ink = theme.color("text_inverted")
        light_ink = theme.color("text_secondary")
        hover_pen = QPen(theme.color("border_strong"), 1)
        raised = theme.color("raised")
        # setFont and setPen are not free at this volume, so the loop only
        # touches them when the value actually differs from the last cell.
        current_pen = None

        for row in range(self.ROWS):
            bit = self.ROWS - 1 - row
            y = self.HEADER + row * self.ROW_HEIGHT
            painter.setFont(label_font)
            painter.setPen(muted_ink)
            current_pen = muted_ink
            painter.drawText(QRectF(0, y, PAYLOAD_GUTTER - 6, self.ROW_HEIGHT),
                             Qt.AlignRight | Qt.AlignVCenter, str(bit))
            painter.setFont(value_font)

            for index, value in enumerate(self._data):
                x = PAYLOAD_GUTTER + index * width
                # Skip columns the repaint does not cover; a 64-byte CAN FD
                # payload is 512 cells and most of them are off screen.
                if x + width < exposed.left() or x > exposed.right():
                    continue

                rect = QRectF(x + 1, y, width - 2, self.ROW_HEIGHT - 1)
                intensity = self._intensity(self._flip_count(index, bit))
                if intensity > 0:
                    painter.fillRect(rect, activity_color(active, intensity))
                elif index in highlight_range:
                    painter.fillRect(rect, raised)

                if (index, row) == self._hover:
                    painter.setPen(hover_pen)
                    painter.drawRect(rect)
                    current_pen = None

                # The digit is drawn with the same weight whether it is a 0 or
                # a 1, and varies only enough to stay legible on a dark wash.
                # Inking 1s more heavily than 0s made the *value* read as
                # activity, which buried the staircase this view exists to show.
                ink = dark_ink if intensity >= 0.55 else light_ink
                if ink is not current_pen:
                    painter.setPen(ink)
                    current_pen = ink
                painter.drawText(QPointF(x + 1 + self._digit_dx, y + self._digit_dy),
                                 "1" if (value >> bit) & 1 else "0")

        # Bracket the selected block, matching the strip above.
        if self._highlight is not None and len(highlight_range):
            start, length = self._highlight
            box = QRectF(PAYLOAD_GUTTER + start * width + 1, self.HEADER - 1,
                         length * width - 2, self.ROWS * self.ROW_HEIGHT + 1)
            painter.setPen(QPen(active, 1.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(box)
        painter.end()


# ---------------------------------------------------------------------------
# table delegates
# ---------------------------------------------------------------------------


#: Horizontal padding applied to segmented buttons by the stylesheet, plus a
#: little headroom so a label never lands exactly on the clipping boundary.
_SEGMENT_PADDING = 16

#: Horizontal padding InterpretCellDelegate leaves inside a cell. Exported so
#: whoever sizes the columns pads by exactly what the painter reserves.
CELL_PADDING = SPACE_SM

#: Text colour per tone. "strong" marks the values users scan for first.
_TONE_TOKENS = {
    "neutral": "text",
    "strong": "text",
    "secondary": "text_secondary",
    "muted": "text_muted",
    "success": "success",
    "accent": "accent",
}


class InterpretCellDelegate(QStyledItemDelegate):
    """Paints interpretation cells: values only, no scoring or ranking."""

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        # paint() and sizeHint() run per visible cell per repaint, so building
        # a QFontMetrics each time is measurable waste. Both fonts are fixed.
        self._metrics_cache: Dict[Tuple[bool, bool], QFontMetrics] = {}
        self._advance_cache: Dict[Tuple[bool, bool], float] = {}
        self._width_cache: Dict[Tuple[str, bool], int] = {}

    def _metrics_for(self, mono: bool, bold: bool) -> Tuple[QFont, QFontMetrics]:
        font = (self._theme.mono_font(bold=bold) if mono
                else self._theme.ui_font(bold=bold))
        key = (mono, bold)
        metrics = self._metrics_cache.get(key)
        if metrics is None:
            metrics = QFontMetrics(font)
            self._metrics_cache[key] = metrics
        return font, metrics

    def _font_for(self, index) -> Tuple[QFont, QFontMetrics]:
        mono = bool(index.data(MONO_ROLE))
        bold = (index.data(TONE_ROLE) or "neutral") == "strong"
        return self._metrics_for(mono, bold)

    def text_width(self, text: str, mono: bool = True, bold: bool = False) -> int:
        """Pixel width ``text`` needs, using the font this delegate paints with.

        Callers use this to size columns up front instead of letting the header
        run in ``ResizeToContents``, which re-measures every cell of every row
        on every resize. In a monospace face the width is exactly the character
        count times one advance, so mono columns cost no measurement at all.
        """
        if mono:
            key = (mono, bold)
            advance = self._advance_cache.get(key)
            if advance is None:
                advance = self._metrics_for(mono, bold)[1].horizontalAdvance("0")
                self._advance_cache[key] = advance
            return int(len(text) * advance)

        cached = self._width_cache.get((text, bold))
        if cached is None:
            cached = self._metrics_for(False, bold)[1].horizontalAdvance(text)
            self._width_cache[(text, bold)] = cached
        return cached

    def invalidate_fonts(self) -> None:
        self._metrics_cache.clear()
        self._advance_cache.clear()
        self._width_cache.clear()

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        theme = self._theme
        painter.save()

        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, theme.color("selection"))

        font, metrics = self._font_for(index)
        painter.setFont(font)
        tone = index.data(TONE_ROLE) or "neutral"
        painter.setPen(theme.color(_TONE_TOKENS.get(tone, "text")))

        alignment = index.data(Qt.TextAlignmentRole) or int(Qt.AlignLeft | Qt.AlignVCenter)
        text_rect = option.rect.adjusted(CELL_PADDING, 0, -CELL_PADDING, 0)
        text = str(index.data(Qt.DisplayRole) or "")
        if metrics.horizontalAdvance(text) > text_rect.width():
            text = metrics.elidedText(text, Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, int(alignment), text)
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        # Must measure with the font this delegate actually paints with,
        # otherwise monospace columns are sized for the narrower UI face and
        # every value ends up elided.
        _font, metrics = self._font_for(index)
        text = str(index.data(Qt.DisplayRole) or "")
        return QSize(
            metrics.horizontalAdvance(text) + 2 * CELL_PADDING + 6,
            max(ROW_HEIGHT, metrics.height() + 8),
        )
