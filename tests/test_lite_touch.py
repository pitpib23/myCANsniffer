"""Movement assertions through Qt's actual gesture pipeline, with production QSS."""
import os
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScroller

from cansniff.config import Config
from cansniff.model import CanFrame
from cansniff.ui.lite_scroll import choose_owner, determine_axis
from cansniff.ui.main_window import MainWindow
from cansniff.ui.theme import Theme


class OwnershipTests(unittest.TestCase):
    def test_axis_and_role_range(self):
        self.assertIsNone(determine_axis(0, 0))
        self.assertEqual(determine_axis(20, 5), 'h')
        self.assertEqual(determine_axis(5, -20), 'v')
        for role in ('h', 'v', 'both'):
            for axis in ('h', 'v'):
                for horizontal in (0, 100):
                    for vertical in (0, 100):
                        with self.subTest(role=role, axis=axis, h=horizontal, v=vertical):
                            capable = role in (axis, 'both') and (horizontal if axis == 'h' else vertical) > 0
                            self.assertEqual(choose_owner([(role, horizontal, vertical), ('both', 100, 100)], axis), 0 if capable else 1)
        self.assertIsNone(choose_owner([('v', 0, 0), ('both', 0, 0)], 'v'))


class LiteTouchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config.defaults(os.path.join(self.tmp.name, 'config.json'))
        self.theme = Theme(ui_size=9.0, mono_size=9.5)
        self.app.setPalette(self.theme.qpalette())
        self.app.setStyleSheet(self.theme.stylesheet())
        self.app.setFont(self.theme.ui_font())
        self.window = MainWindow(self.config, self.theme, lite=True)
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.window.setGeometry(0, 0, 800, 480)
        QTest.qWait(60)
        self.assertTrue(QTest.qWaitForWindowExposed(self.window, 2000))
        # Wayland cannot activate a window using a synthetic pointer's seat
        # serial. Model compositor activation for these synthetic focus tests.
        if self.app.platformName().startswith('wayland'):
            self.app.setActiveWindow(self.window)
        else:
            self.assertTrue(QTest.qWaitForWindowActive(self.window, 2000))
        self.page = self.window.lite_workspace_scroll
        self.router = self.window._lite_scroll_router

    def tearDown(self):
        self.router.scroller.stop()
        self.window._teardown_thread()
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.app.setStyleSheet('')
        self.tmp.cleanup()

    def populate(self):
        frames = [CanFrame(timestamp=i / 100, arb_id=0x100+i,
                           data=bytes(range(64)), dlc=64, channel='0') for i in range(80)]
        self.window.id_model.add_frames(frames)
        self.window.trace_model.add_frames(frames)
        self.window.id_view.selectRow(0)
        QTest.qWait(40)

    def reveal(self, widget):
        self.page.ensureWidgetVisible(widget, 5, 5)
        bar = self.page.verticalScrollBar()
        bar.setValue(bar.value() + widget.mapTo(self.page.viewport(), widget.rect().center()).y()
                     - self.page.viewport().height() // 2)
        QTest.qWait(20)

    def hit_point(self, widget):
        bounds = QRect(widget.mapToGlobal(QPoint()), widget.size()).intersected(
            QRect(self.page.viewport().mapToGlobal(QPoint()), self.page.viewport().size())).intersected(self.window.screen().geometry())
        self.assertFalse(bounds.isEmpty(), 'gesture must begin on visible content')
        start = QPoint(bounds.left() + min(bounds.width() // 2, 160), bounds.center().y())
        target = QApplication.widgetAt(start)
        self.assertTrue(target is widget or widget.isAncestorOf(target),
                        f'actual hit={target}, start={start}, bounds={bounds}, viewport={widget}, screen={self.window.screen().geometry()}')
        return start

    def drag(self, widget, dx=0, dy=-100, release=True):
        # Coordinates stay global even when the outer page moves underneath.
        start = self.hit_point(widget)
        QTest.mousePress(widget, Qt.LeftButton, pos=widget.mapFromGlobal(start))
        for step in range(1, 9):
            end = start + QPoint(round(dx * step / 8), round(dy * step / 8))
            QTest.mouseMove(widget, widget.mapFromGlobal(end), delay=10)
            QTest.qWait(10)
        if release:
            QTest.mouseRelease(widget, Qt.LeftButton, pos=widget.mapFromGlobal(end))
            QTest.qWait(20)
        return end

    def test_empty_tables_vertical_before_start(self):
        for trace in (False, True):
            if trace:
                self.window.nav.group.button(self.window._NAV_TRACE).click()
            table = self.window.trace_view if trace else self.window.id_view
            self.page.verticalScrollBar().setValue(0)
            QTest.qWait(20)
            self.assertEqual(table.verticalScrollBar().maximum(), 0)
            self.drag(table.viewport())
            self.assertIs(self.router.owner, self.page)
            self.assertGreater(self.page.verticalScrollBar().value(), 0)
            self.router.scroller.stop()

    def test_populated_tables_vertical_and_horizontal(self):
        self.populate()
        # Force real page overflow independently of font/screen geometry.
        self.page.widget().setMinimumWidth(1300)
        QTest.qWait(20)
        for trace in (False, True):
            if trace:
                self.window.nav.group.button(self.window._NAV_TRACE).click()
            table = self.window.trace_view if trace else self.window.id_view
            for axis in ('v', 'h'):
                self.router.scroller.stop()
                self.page.verticalScrollBar().setValue(0)
                self.page.horizontalScrollBar().setValue(0)
                table.verticalScrollBar().setValue(0)
                QTest.qWait(20)
                parent_y = self.page.verticalScrollBar().value()
                self.drag(table.viewport(), dx=-100 if axis == 'h' else 0, dy=-100 if axis == 'v' else 0)
                self.assertIs(self.router.owner, table if axis == 'v' else self.page)
                if axis == 'v':
                    self.assertGreater(table.verticalScrollBar().value(), 0)
                    self.assertEqual(self.page.verticalScrollBar().value(), parent_y)
                else:
                    self.assertGreater(self.page.horizontalScrollBar().value(), 0)
                    self.assertEqual(table.horizontalScrollBar().value(), 0)

    def test_payload_axes_and_synchronization(self):
        self.populate()
        for name in ('strip_scroll', 'matrix_scroll'):
            area = getattr(self.window.interpret_view, name)
            area.show()
            self.reveal(area)
            self.assertGreater(area.horizontalScrollBar().maximum(), 0)
            old_y = self.page.verticalScrollBar().value()
            self.drag(area.viewport(), dx=-90, dy=0)
            self.assertIs(self.router.owner, area)
            self.assertGreater(area.horizontalScrollBar().value(), 0)
            self.assertEqual(self.page.verticalScrollBar().value(), old_y)
            self.assertEqual(self.window.interpret_view.strip_scroll.horizontalScrollBar().value(), self.window.interpret_view.matrix_scroll.horizontalScrollBar().value())
            self.router.scroller.stop()
            self.reveal(area)
            old_y = self.page.verticalScrollBar().value()
            self.drag(area.viewport(), dy=70 if old_y > 100 else -70)
            self.assertIs(self.router.owner, self.page)
            self.assertNotEqual(self.page.verticalScrollBar().value(), old_y)
            self.router.scroller.stop()

    def test_boundary_and_repeated_owners(self):
        self.populate()
        table = self.window.id_view
        for _ in range(2):
            self.page.verticalScrollBar().setValue(0)
            table.verticalScrollBar().setValue(table.verticalScrollBar().maximum())
            self.drag(table.viewport())
            self.assertIs(self.router.owner, table)
            self.assertEqual(self.page.verticalScrollBar().value(), 0)
            self.router.scroller.stop()
            self.page.widget().setMinimumWidth(1300)
            QTest.qWait(20)
            self.drag(table.viewport(), dx=-80, dy=0)
            self.assertIs(self.router.owner, self.page)
            self.assertGreater(self.page.horizontalScrollBar().value(), 0)
            self.router.scroller.stop()
            self.page.horizontalScrollBar().setValue(0)

    def test_tap_selects_and_inertia_stays_with_owner(self):
        self.populate()
        table = self.window.id_view
        index = self.window.id_proxy.index(1, 0)
        QTest.mouseClick(table.viewport(), Qt.LeftButton, pos=table.visualRect(index).center())
        QTest.qWait(50)
        self.assertEqual(table.selectionModel().selectedRows()[0].row(), 1)
        old_y = self.page.verticalScrollBar().value()
        self.drag(table.viewport(), dy=-150)
        after_release = table.verticalScrollBar().value()
        QTest.qWait(150)
        self.assertGreater(table.verticalScrollBar().value(), after_release)
        self.assertEqual(self.page.verticalScrollBar().value(), old_y)
        self.assertIs(self.router.owner, table)

    def test_new_press_interrupts_inertia_without_stale_owner(self):
        self.populate()
        table = self.window.id_view
        self.page.widget().setMinimumWidth(1300)
        QTest.qWait(20)
        for axis in ('v', 'h', 'v', 'h'):
            self.page.verticalScrollBar().setValue(0)
            self.page.horizontalScrollBar().setValue(0)
            self.drag(table.viewport(), dx=-80 if axis == 'h' else 0,
                      dy=-80 if axis == 'v' else 0)
            self.assertIs(self.router.owner, table if axis == 'v' else self.page)

    def test_payload_taps_and_scrollbar_drag(self):
        self.populate()
        strip = self.window.interpret_view.strip
        self.reveal(strip)
        clicks = []
        strip.byteClicked.connect(clicks.append)
        point = QPoint(30, strip.height() // 2)
        QTest.mouseClick(strip, Qt.LeftButton, pos=point)
        QTest.qWait(50)
        self.assertTrue(clicks)
        self.page.verticalScrollBar().setValue(0)
        table = self.window.id_view
        bar = table.verticalScrollBar()
        bar.setValue(0)
        QTest.mousePress(bar, Qt.LeftButton, pos=QPoint(bar.width() // 2, 25))
        QTest.mouseMove(bar, QPoint(bar.width() // 2, bar.height() // 2), delay=30)
        QTest.mouseRelease(bar, Qt.LeftButton, pos=QPoint(bar.width() // 2, bar.height() // 2))
        QTest.qWait(30)
        self.assertGreater(bar.value(), 0)
        self.assertEqual(self.page.verticalScrollBar().value(), 0)

    def test_range_change_during_drag_does_not_reassign_owner(self):
        self.populate()
        table = self.window.id_view
        end = self.drag(table.viewport(), release=False)
        self.assertIs(self.router.owner, table)
        old_y = self.page.verticalScrollBar().value()
        table.verticalScrollBar().setRange(0, 0)
        self.router.scroller.resendPrepareEvent()
        self.assertIs(self.router.owner, table)
        QTest.mouseMove(table.viewport(), table.viewport().mapFromGlobal(end + QPoint(0, -40)), delay=20)
        QTest.mouseRelease(table.viewport(), Qt.LeftButton,
                           pos=table.viewport().mapFromGlobal(end + QPoint(0, -40)))
        QTest.qWait(40)
        self.assertEqual(self.page.verticalScrollBar().value(), old_y)
        self.assertIs(self.router.owner, table)

    def test_zero_range_payload_falls_back_and_wheel_is_native(self):
        self.populate()
        self.window.interpret_view.strip.set_payload(bytes(range(8)))
        self.window.interpret_view.bit_matrix.set_payload(bytes(range(8)))
        self.page.widget().setMinimumWidth(1300)
        QTest.qWait(30)
        for name in ('strip_scroll', 'matrix_scroll'):
            area = getattr(self.window.interpret_view, name)
            area.show()
            self.reveal(area)
            self.assertEqual(area.horizontalScrollBar().maximum(), 0)
            self.page.horizontalScrollBar().setValue(0)
            self.drag(area.viewport(), dx=-80, dy=0)
            self.assertIs(self.router.owner, self.page)
            self.assertGreater(self.page.horizontalScrollBar().value(), 0)
            self.router.scroller.stop()
        self.page.horizontalScrollBar().setValue(0)
        self.page.verticalScrollBar().setValue(0)
        table = self.window.id_view
        table.verticalScrollBar().setValue(0)
        point = QPointF(80, 80)
        event = QWheelEvent(point, table.viewport().mapToGlobal(point), QPoint(),
                            QPoint(0, -120), Qt.NoButton, Qt.NoModifier,
                            Qt.NoScrollPhase, False)
        QApplication.sendEvent(table.viewport(), event)
        self.assertGreater(table.verticalScrollBar().value(), 0)
        self.assertEqual(self.page.verticalScrollBar().value(), 0)

    def test_native_touch_drag_and_tap(self):
        self.populate()
        table = self.window.id_view
        viewport = table.viewport()
        device = QTest.createTouchDevice()
        start = viewport.mapFromGlobal(self.hit_point(viewport))
        sequence = QTest.touchEvent(viewport, device, autoCommit=False)
        sequence.press(0, start, viewport).commit()
        for step in range(1, 9):
            sequence.move(0, start + QPoint(0, -step * 10), viewport).commit()
            QTest.qWait(30)
        sequence.release(0, start + QPoint(0, -80), viewport).commit()
        QTest.qWait(80)
        self.assertIs(self.router.owner, table)
        self.assertGreater(table.verticalScrollBar().value(), 0)
        self.router.scroller.stop()
        table.verticalScrollBar().setValue(0)
        index = self.window.id_proxy.index(1, 0)
        point = table.visualRect(index).center()
        sequence.press(0, point, viewport).commit()
        sequence.release(0, point, viewport).commit()
        QTest.qWait(80)
        self.assertEqual(table.selectionModel().selectedRows()[0].row(), 1)

    def test_start_file_capture_preserves_router(self):
        self.test_empty_tables_vertical_before_start()
        self.router.scroller.stop()
        path = os.path.join(self.tmp.name, 'capture.log')
        with open(path, 'w') as handle:
            handle.write(''.join('(%0.6f) can0 %03X#01020304\n' % (i / 100, 0x100+i) for i in range(80)))
        self.config.set('source.type', 'file')
        self.config.set('source.file.path', path)
        self.config.set('source.file.speed', 0.0)
        self.config.set('logging.enabled', False)
        self.window.start_capture()
        for _ in range(100):
            QTest.qWait(20)
            if self.window.id_proxy.rowCount() >= 80:
                break
        self.assertEqual(self.window.id_proxy.rowCount(), 80)
        self.assertIs(self.window._lite_scroll_router, self.router)
        self.window.nav.group.button(self.window._NAV_MESSAGES).click()
        self.page.verticalScrollBar().setValue(0)
        old_y = self.page.verticalScrollBar().value()
        self.drag(self.window.id_view.viewport())
        self.assertIs(self.router.owner, self.window.id_view)
        self.assertEqual(self.page.verticalScrollBar().value(), old_y)


if __name__ == '__main__':
    unittest.main()
