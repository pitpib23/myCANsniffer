"""Qt lifecycle and configuration handoff for Auto Discover."""

from __future__ import annotations

import copy
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QDialog
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.config import Config
    from cansniff.discovery.model import (
        AdapterDescriptor, AdapterScanResult, BackendEnumerationResult,
        DiscoveryResult, DiscoveryStatus, EnumerationStatus, PassiveCapability,
        Provenance,
    )
    from cansniff.model import CanFrame
    from cansniff.sources import CanFrameSource
    from cansniff.ui.discovery_dialog import DiscoveryDialog
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.theme import Theme


def _descriptor():
    return AdapterDescriptor(
        "Kvaser test", "kvaser", "0",
        passive_capability=PassiveCapability.SUPPORTED_BY_BACKEND_POLICY,
        enumeration_source=Provenance.DETECTED,
        auto_bitrate_supported=True,
        implementation_supported=True,
    )


def _scan():
    adapter = _descriptor()
    return AdapterScanResult(
        (adapter,),
        (BackendEnumerationResult(
            "kvaser", EnumerationStatus.FOUND, (adapter,), ""),),
    )


def _detected(adapter=None):
    return DiscoveryResult(
        adapter or _descriptor(), DiscoveryStatus.DETECTED, 500000, (),
        reasons=("stable synthetic evidence",),
    )


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class DiscoveryDialogTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Config.defaults(os.path.join(
            tempfile.gettempdir(), "cansniff_discovery_ui_{}.json".format(id(self))))
        self.config.set("source.live.extra_kwargs", {"vendor_option": 7})
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        self.app.processEvents()

    def _wait(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.002)
        return predicate()

    def _dialog(self, discoverer=lambda adapter, **_kwargs: _detected(adapter)):
        dialog = DiscoveryDialog(
            self.config,
            enumerator=lambda **_kwargs: _scan(),
            discoverer=discoverer,
        )
        self.dialogs.append(dialog)
        dialog.show()
        self.assertTrue(self._wait(lambda: dialog._thread is None
                                   and dialog.adapter_combo.count() == 1))
        return dialog

    def test_enumeration_runs_off_ui_thread_and_exposes_capability(self):
        dialog = self._dialog()
        self.assertEqual(dialog.current_adapter().interface, "kvaser")
        self.assertTrue(dialog.test_button.isEnabled())
        self.assertIn("not hardware qualified", dialog.capability_label.text())

    def test_success_commits_only_when_user_accepts(self):
        before = copy.deepcopy(self.config.data)
        dialog = self._dialog()
        dialog.start_bitrate_discovery()
        self.assertTrue(self._wait(lambda: dialog._thread is None
                                   and dialog._result is not None))
        self.assertEqual(self.config.data, before,
                         "temporary discovery must not mutate Config")
        dialog._accept_result()
        selected = dialog.selected_settings()
        self.assertEqual(selected["interface"], "kvaser")
        self.assertEqual(selected["channel"], "0")
        self.assertEqual(selected["bitrate"], 500000)
        self.assertFalse(selected["fd"])
        self.assertTrue(selected["require_listen_only"])
        self.assertEqual(selected["extra_kwargs"], {"vendor_option": 7})

    def test_cancel_closes_worker_thread_and_produces_no_config(self):
        entered = threading.Event()

        def cancellable(adapter, cancel_event=None, **_kwargs):
            entered.set()
            cancel_event.wait(2.0)
            return DiscoveryResult(
                adapter, DiscoveryStatus.CANCELLED, None, (),
                reasons=("cancelled",))

        dialog = self._dialog(cancellable)
        dialog.start_bitrate_discovery()
        self.assertTrue(entered.wait(1.0))
        dialog.cancel_operation()
        self.assertTrue(self._wait(lambda: dialog._thread is None))
        self.assertEqual(dialog._result.status, DiscoveryStatus.CANCELLED)
        self.assertIsNone(dialog.selected_settings())

    def test_reject_during_discovery_leaves_no_thread(self):
        entered = threading.Event()

        def cancellable(adapter, cancel_event=None, **_kwargs):
            entered.set()
            cancel_event.wait(2.0)
            return DiscoveryResult(adapter, DiscoveryStatus.CANCELLED, None, ())

        dialog = self._dialog(cancellable)
        dialog.start_bitrate_discovery()
        self.assertTrue(entered.wait(1.0))
        dialog.reject()
        self.assertIsNone(dialog._thread)
        self.assertIsNone(dialog._worker)

    def test_discovery_can_be_repeated_without_thread_leaks(self):
        dialog = self._dialog()
        for _ in range(4):
            dialog.start_bitrate_discovery()
            self.assertTrue(self._wait(lambda: dialog._thread is None
                                       and dialog._result is not None))
        self.assertIsNone(dialog._thread)
        self.assertIsNone(dialog._worker)


if HAVE_QT:
    class _FiniteSource(CanFrameSource):
        name = "post-discovery"

        def __init__(self):
            self.index = 0
            self.closed = False

        def open(self):
            pass

        def receive(self, timeout=0.1):
            if self.index >= 2:
                return None
            self.index += 1
            return CanFrame(self.index * 0.1, 0x123, b"\x01", 1)

        @property
        def exhausted(self):
            return self.index >= 2

        def close(self):
            self.closed = True


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class MainWindowDiscoveryHandoffTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.path = os.path.join(
            tempfile.gettempdir(), "cansniff_discovery_main_{}.json".format(id(self)))
        self.config = Config.defaults(self.path)
        self.window = MainWindow(self.config, Theme())

    def tearDown(self):
        self.window._teardown_thread()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _wait(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.002)
        return predicate()

    def test_successful_handoff_is_saved_and_capture_still_runs(self):
        selected = copy.deepcopy(self.config.get("source.live"))
        selected.update({"interface": "kvaser", "channel": "2",
                         "bitrate": 500000, "fd": False,
                         "require_listen_only": True})

        class AcceptedDialog:
            Accepted = QDialog.Accepted

            def __init__(self, *_args):
                pass

            def exec(self):
                return self.Accepted

            def selected_settings(self):
                return selected

        with mock.patch("cansniff.ui.main_window.DiscoveryDialog", AcceptedDialog):
            self.window._auto_discover()
        self.assertEqual(self.config.get("source.type"), "live")
        self.assertEqual(self.config.get("source.live.channel"), "2")
        self.assertEqual(self.config.get("source.live.bitrate"), 500000)

        with mock.patch("cansniff.ui.main_window.build_source",
                        return_value=_FiniteSource()):
            self.window.start_capture()
            self.assertTrue(self._wait(lambda: self.window._thread is None))
        self.assertEqual(len(self.window.frame_store.all_frames()), 2)

    def test_cancelled_dialog_preserves_existing_config(self):
        before = copy.deepcopy(self.config.data)

        class RejectedDialog:
            Accepted = QDialog.Accepted

            def __init__(self, *_args):
                pass

            def exec(self):
                return QDialog.Rejected

            def selected_settings(self):
                raise AssertionError("must not request settings after rejection")

        with mock.patch("cansniff.ui.main_window.DiscoveryDialog", RejectedDialog):
            self.window._auto_discover()
        self.assertEqual(self.config.data, before)

    def test_main_window_close_cancels_active_discovery(self):
        entered = threading.Event()

        def cancellable(adapter, cancel_event=None, **_kwargs):
            entered.set()
            cancel_event.wait(2.0)
            return DiscoveryResult(adapter, DiscoveryStatus.CANCELLED, None, ())

        dialog = DiscoveryDialog(
            self.config, self.window,
            enumerator=lambda **_kwargs: _scan(), discoverer=cancellable)
        dialog.show()
        self.assertTrue(self._wait(lambda: dialog._thread is None
                                   and dialog.adapter_combo.count() == 1))
        dialog.start_bitrate_discovery()
        self.assertTrue(entered.wait(1.0))
        self.window._discovery_dialog = dialog
        self.window.close()
        self.assertIsNone(dialog._thread)
        self.assertIsNone(dialog._worker)


if __name__ == "__main__":
    unittest.main()
