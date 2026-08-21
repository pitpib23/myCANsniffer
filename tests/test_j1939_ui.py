"""J1939 transport, definition, search, and detail presentation."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.analysis.j1939_definitions import parse_j1939_definition_file
    from cansniff.analysis.profile import TrafficProfileAccumulator
    from cansniff.analysis.protocols import build_protocol_survey
    from cansniff.analysis.signals import Profile, ProfileStore
    from cansniff.analysis.store import FrameStore
    from cansniff.model import CanFrame
    from cansniff.ui.protocols_view import ProtocolsView
    from cansniff.ui.theme import Theme


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic_j1939.json")


def can_id(pf, destination, source=0x31):
    return (6 << 26) | (pf << 16) | (destination << 8) | source


def capture():
    pgn = 0xEF00
    payload = bytes([1, 0xF4, 1, 0x78, 0x56, 0x34, 0x12, 0xFF, 9])
    pgn_bytes = [pgn & 0xFF, pgn >> 8, 0]
    values = [
        CanFrame(0.0, can_id(0xEC, 0xFF),
                 bytes([0x20, 9, 0, 2, 0xFF] + pgn_bytes), 8,
                 is_extended=True, channel="can0"),
        CanFrame(0.1, can_id(0xEB, 0xFF),
                 bytes([1]) + payload[:7], 8, is_extended=True, channel="can0"),
        CanFrame(0.2, can_id(0xEB, 0xFF),
                 bytes([2]) + payload[7:] + bytes([0xFF] * 5), 8,
                 is_extended=True, channel="can0"),
    ]
    return values


def survey(frames):
    store = FrameStore(1000)
    store.add(frames)
    traffic = TrafficProfileAccumulator()
    traffic.update(frames)
    return build_protocol_survey(store.all_frames(), traffic.snapshot(store))


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class J1939UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        definition = parse_j1939_definition_file(FIXTURE)
        profile = Profile("active-j1939", definitions=[definition.reference])
        self.store = ProfileStore([profile], profile.name)
        self.view = ProtocolsView(Theme())
        self.view.set_definition_context(self.store)
        self.view.set_snapshot(survey(capture()))
        self.addCleanup(self.view.deleteLater)

    def test_transport_and_decoded_spns_are_visible_with_provenance(self):
        self.assertEqual(self.view.j1939_transport_table.rowCount(), 1)
        self.assertEqual(self.view.j1939_transport_table.item(0, 2).text(),
                         "Complete")
        self.assertGreaterEqual(self.view.j1939_decoded_table.rowCount(), 4)
        text = self.view.j1939_transport_detail.toPlainText()
        self.assertIn("Payload:", text)
        self.assertIn("Controls:", text)
        self.assertIn("Packets:", text)
        sources = {self.view.j1939_decoded_table.item(row, 10).text()
                   for row in range(self.view.j1939_decoded_table.rowCount())}
        self.assertIn("synthetic_j1939.json", sources)

    def test_search_filters_on_spn_and_status(self):
        with patch("cansniff.ui.protocols_view.decode_j1939_payloads",
                   side_effect=AssertionError("decode cache miss")):
            self.view.j1939_search.setText("1001")
            self.app.processEvents()
        self.assertEqual(self.view.j1939_decoded_table.rowCount(), 1)
        self.assertEqual(self.view.j1939_decoded_table.item(0, 3).text(), "1001")
        self.view.j1939_search.setText("Complete")
        self.app.processEvents()
        self.assertEqual(self.view.j1939_transport_table.rowCount(), 1)


if __name__ == "__main__":
    unittest.main()
