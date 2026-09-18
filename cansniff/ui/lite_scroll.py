"""Lite nested scrolling: one Qt recognizer, one axis and one destination.

The workspace QScroller runs over virtual content. Its Scroll events (including
inertia) drive the selected real scrollbar. Child areas never grab gestures, so
recognizers cannot compete and no mid-gesture ungrab is necessary. Acquisition
uses Qt's gesture pipeline, not application-level raw MouseMove delivery.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSlider, QAbstractSpinBox,
    QComboBox, QHeaderView, QLineEdit, QPlainTextEdit, QScroller,
    QScrollerProperties, QTextEdit,
)
from shiboken6 import isValid


def determine_axis(dx, dy):
    if dx == 0 and dy == 0:
        return None
    return 'h' if abs(dx) > abs(dy) else 'v'


def choose_owner(candidates, axis):
    """Return the first capable index from (role, horizontal, vertical) ranges.

    Candidates are ordered inside out. Ranges are extents, not current values:
    being at a boundary must never transfer a gesture to another owner.
    """
    for index, (role, horizontal, vertical) in enumerate(candidates):
        if role in ('both', axis) and (horizontal if axis == 'h' else vertical) > 0:
            return index
    return None


class LiteScrollRouter(QObject):
    _CENTER = QPointF(1000000, 1000000)

    def __init__(self, page, roles):
        super().__init__(page)
        self.page = page
        self.viewport = page.viewport()
        self.roles = dict(roles)
        self.roles[page] = 'both'
        self.owner = None
        self.axis = None
        self._candidates = []
        self._new_gesture = False
        self._last = QPointF(self._CENTER)
        self._remainder = 0.0
        self.scroller = QScroller.scroller(page.viewport())
        props = self.scroller.scrollerProperties()
        # A slow coasting press must begin a fresh drag too. Qt's default
        # click-through branch stops the scroller and passes subsequent moves
        # to the child instead, which loses that new gesture offscreen.
        props.setScrollMetric(QScrollerProperties.MaximumClickThroughVelocity, 0.0)
        for metric in (QScrollerProperties.HorizontalOvershootPolicy,
                       QScrollerProperties.VerticalOvershootPolicy):
            props.setScrollMetric(metric, QScrollerProperties.OvershootAlwaysOff)
        self.scroller.setScrollerProperties(props)
        for area in self.roles:
            if isinstance(area, QAbstractItemView):
                area.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
                area.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        page.viewport().installEventFilter(self)
        page.installEventFilter(self)
        QScroller.grabGesture(page.viewport(), QScroller.LeftMouseButtonGesture)

    def _areas_at(self, position):
        viewport = self.page.viewport()
        target = viewport.childAt(position.toPoint()) or viewport
        areas = []
        widget = target
        while widget is not None:
            if isinstance(widget, (QAbstractSlider, QHeaderView, QLineEdit,
                                   QPlainTextEdit, QTextEdit, QAbstractSpinBox, QComboBox)):
                return []
            # Charts retain their rubber-band / payload plot interactions.
            if widget.inherits('QChartView'):
                return []
            if widget in self.roles:
                areas.append(widget)
            if widget is self.page:
                return areas
            widget = widget.parentWidget()
        return []

    @staticmethod
    def _range(area, axis):
        bar = area.horizontalScrollBar() if axis == 'h' else area.verticalScrollBar()
        policy = area.horizontalScrollBarPolicy() if axis == 'h' else area.verticalScrollBarPolicy()
        return 0 if policy == Qt.ScrollBarAlwaysOff else bar.maximum() - bar.minimum()

    def eventFilter(self, watched, event):
        kind = event.type()
        if watched is self.page and kind in (QEvent.Hide, QEvent.EnabledChange):
            if kind == QEvent.Hide or not self.page.isEnabled():
                self.scroller.stop()
                self.owner = None
        if watched is not self.viewport:
            return False
        if kind == QEvent.Gesture:
            gesture = event.gesture(QScroller.grabbedGesture(self.viewport))
            if gesture is not None and gesture.state() == Qt.GestureStarted:
                # A new press can interrupt inertia without an automatic
                # ScrollPrepare. Refresh once Qt recognizes that new drag;
                # do not change grabs or stop a recognizer mid-flight.
                self._new_gesture = True
                try:
                    self.scroller.resendPrepareEvent()
                finally:
                    self._new_gesture = False
        if kind == QEvent.ScrollPrepare:
            # Geometry/model updates can also request preparation. They must
            # not transfer a live gesture when an owner's range disappears.
            if self._new_gesture or self.scroller.state() in (QScroller.Inactive, QScroller.Pressed):
                self._candidates = self._areas_at(event.startPos())
                self.owner = None
                self.axis = None
                self._last = QPointF(self._CENTER)
                self._remainder = 0.0
            if not self._candidates:
                event.ignore()
                return True
            event.setViewportSize(self.page.viewport().size())
            event.setContentPosRange(QRectF(0, 0, 2000000, 2000000))
            event.setContentPos(self._last)
            event.accept()
            return True
        if kind == QEvent.Scroll:
            position = event.contentPos()
            delta = position - self._last
            self._last = QPointF(position)
            if self.axis is None:
                self.axis = determine_axis(delta.x(), delta.y())
                if self.axis is not None:
                    candidates = [area for area in self._candidates
                                  if isValid(area) and area.isVisible() and area.isEnabled()]
                    index = choose_owner([
                        (self.roles[area], self._range(area, 'h'), self._range(area, 'v'))
                        for area in candidates], self.axis)
                    self.owner = candidates[index] if index is not None else None
            if self.owner is not None and isValid(self.owner):
                bar = (self.owner.horizontalScrollBar() if self.axis == 'h'
                       else self.owner.verticalScrollBar())
                amount = (delta.x() if self.axis == 'h' else delta.y()) + self._remainder
                pixels = round(amount)
                self._remainder = amount - pixels
                bar.setValue(bar.value() + pixels)
            event.accept()
            return True
        return False
