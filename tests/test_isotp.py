"""Passive ISO-TP reassembly and diagnostic interpretation.

Every fixture is a constructed capture. The critical assertion running through
this file is that analysis is passive: no socket, no session, no Flow Control
produced. Flow Control that appears is something the capture contained.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis import isotp as isotp_module  # noqa: E402
from cansniff.analysis.isotp import (  # noqa: E402
    ADDRESSING, COMPLETE, INCOMPLETE, INVALID_PCI, LENGTH_MISMATCH,
    ORPHAN_CF, SEQUENCE_ERROR, TIMEOUT, pci_name, reassemble,
)
from cansniff.analysis.store import FrameStore  # noqa: E402
from cansniff.analysis.uds import (  # noqa: E402
    NEGATIVE, POSITIVE, REQUEST, UNKNOWN, interpret,
)
from cansniff.model import CanFrame  # noqa: E402


def _f(t, data, arb=0x7E8, ext=False, ch="1"):
    return CanFrame(timestamp=t, arb_id=arb, data=bytes(data), dlc=len(data),
                    channel=ch, is_extended=ext)


def _window(frames):
    store = FrameStore()
    store.add(frames)
    return store.all_frames()


def _multi(payload, arb=0x7E8, start=0.0, step=0.01, pad=0x00):
    """Build FF + CF frames carrying `payload`."""
    frames = [_f(start, [0x10 | ((len(payload) >> 8) & 0x0F), len(payload) & 0xFF]
                 + list(payload[:6]), arb=arb)]
    rest = payload[6:]
    sn = 1
    t = start
    while rest:
        t += step
        chunk = rest[:7]
        rest = rest[7:]
        body = list(chunk) + [pad] * (7 - len(chunk))
        frames.append(_f(t, [0x20 | (sn & 0x0F)] + body, arb=arb))
        sn = (sn + 1) % 16
    return frames


class SingleFrameTests(unittest.TestCase):
    def test_single_frame_is_complete(self):
        transfers = reassemble(_window([_f(0.0, [0x03, 0x7F, 0x22, 0x31])]))
        self.assertEqual(len(transfers), 1)
        t = transfers[0]
        self.assertEqual(t.status, COMPLETE)
        self.assertEqual(t.data, bytes([0x7F, 0x22, 0x31]))
        self.assertEqual(t.frame_count, 1)

    def test_padding_after_a_single_frame_is_not_payload(self):
        transfers = reassemble(_window([_f(0.0, [0x02, 0x50, 0x03, 0, 0, 0, 0, 0])]))
        self.assertEqual(transfers[0].data, bytes([0x50, 0x03]))

    def test_zero_length_single_frame_is_invalid_pci(self):
        transfers = reassemble(_window([_f(0.0, [0x00, 0, 0, 0])]))
        self.assertEqual(transfers[0].status, INVALID_PCI)

    def test_truncated_single_frame_is_a_length_mismatch_not_padded(self):
        transfers = reassemble(_window([_f(0.0, [0x07, 0x01, 0x02])]))
        self.assertEqual(transfers[0].status, LENGTH_MISMATCH)
        self.assertEqual(transfers[0].data, bytes([0x01, 0x02]),
                         "observed bytes are kept; missing ones are not invented")


class MultiFrameTests(unittest.TestCase):
    def test_first_plus_consecutive_reassembles_exactly(self):
        payload = bytes(range(20))
        transfers = reassemble(_window(_multi(payload)))
        self.assertEqual(len(transfers), 1)
        t = transfers[0]
        self.assertEqual(t.status, COMPLETE)
        self.assertEqual(t.data, payload)
        self.assertEqual(t.declared_length, 20)
        self.assertEqual(t.frame_count, 1 + 2)

    def test_long_transfer_across_many_consecutive_frames(self):
        payload = bytes((i * 7) & 0xFF for i in range(300))
        transfers = reassemble(_window(_multi(payload)))
        self.assertEqual(transfers[0].status, COMPLETE)
        self.assertEqual(transfers[0].data, payload)

    def test_sequence_number_wraps_past_fifteen(self):
        payload = bytes(range(256))          # needs > 16 consecutive frames
        transfers = reassemble(_window(_multi(payload)))
        self.assertEqual(transfers[0].status, COMPLETE)
        self.assertEqual(transfers[0].data, payload)

    def test_trailing_padding_is_trimmed_to_the_declared_length(self):
        payload = bytes(range(10))
        transfers = reassemble(_window(_multi(payload, pad=0xAA)))
        self.assertEqual(transfers[0].data, payload)

    def test_wrong_sequence_number_is_a_sequence_error_and_stops_there(self):
        frames = _multi(bytes(range(20)))
        frames[2] = _f(frames[2].timestamp, [0x25] + [0xFF] * 7)   # expected 2
        transfers = reassemble(_window(frames))
        self.assertEqual(transfers[0].status, SEQUENCE_ERROR)
        self.assertIn("expected sequence", transfers[0].detail)

    def test_duplicate_consecutive_frame_is_not_appended_twice(self):
        frames = _multi(bytes(range(20)))
        frames.insert(2, frames[1])          # repeat the first CF
        transfers = reassemble(_window(frames))
        self.assertEqual(transfers[0].status, COMPLETE)
        self.assertEqual(transfers[0].data, bytes(range(20)))

    def test_incomplete_transfer_keeps_what_was_observed(self):
        frames = _multi(bytes(range(40)))[:3]     # cut the capture short
        transfers = reassemble(_window(frames))
        t = transfers[0]
        self.assertEqual(t.status, INCOMPLETE)
        self.assertEqual(t.declared_length, 40)
        self.assertLess(t.received_length, 40)
        self.assertEqual(t.data, bytes(range(t.received_length)),
                         "missing bytes must not be fabricated")
        self.assertEqual(t.detail, "capture ended mid-transfer")

    def test_consecutive_frame_with_no_first_frame_is_an_orphan(self):
        transfers = reassemble(_window([_f(0.0, [0x21] + [1] * 7)]))
        self.assertEqual(transfers[0].status, ORPHAN_CF)
        self.assertIn("no first frame", transfers[0].detail)

    def test_first_frame_declaring_too_little_is_a_length_mismatch(self):
        transfers = reassemble(_window([_f(0.0, [0x10, 0x03, 1, 2, 3, 0, 0, 0])]))
        self.assertEqual(transfers[0].status, LENGTH_MISMATCH)

    def test_a_new_first_frame_closes_the_previous_transfer(self):
        frames = _multi(bytes(range(40)))[:2] + _multi(bytes(range(10)), start=5.0)
        transfers = reassemble(_window(frames), timeout=0)
        self.assertEqual(transfers[0].status, INCOMPLETE)
        self.assertIn("interrupted", transfers[0].detail)
        self.assertEqual(transfers[1].status, COMPLETE)


class SeparationTests(unittest.TestCase):
    def test_interleaved_conversations_do_not_merge(self):
        a = _multi(bytes(range(20)), arb=0x7E8, start=0.0)
        b = _multi(bytes(range(100, 120)), arb=0x7E9, start=0.005)
        merged = sorted(a + b, key=lambda f: f.timestamp)
        transfers = reassemble(_window(merged))
        self.assertEqual(len(transfers), 2)
        by_id = {t.arb_id: t for t in transfers}
        self.assertEqual(by_id[0x7E8].data, bytes(range(20)))
        self.assertEqual(by_id[0x7E9].data, bytes(range(100, 120)))

    def test_same_id_on_different_channels_is_separate(self):
        a = _multi(bytes(range(20)), arb=0x7E8)
        b = [CanFrame(timestamp=f.timestamp, arb_id=f.arb_id, data=f.data,
                      dlc=f.dlc, channel="2") for f in _multi(bytes(range(20)))]
        transfers = reassemble(_window(sorted(a + b, key=lambda f: f.timestamp)))
        self.assertEqual(len(transfers), 2)
        self.assertEqual({t.channel for t in transfers}, {"1", "2"})

    def test_extended_identifiers_are_supported_and_kept_apart(self):
        std = _multi(bytes(range(20)), arb=0x100)
        ext = [CanFrame(timestamp=f.timestamp, arb_id=0x100, data=f.data,
                        dlc=f.dlc, channel="1", is_extended=True)
               for f in _multi(bytes(range(50, 70)))]
        transfers = reassemble(_window(sorted(std + ext, key=lambda f: f.timestamp)))
        self.assertEqual(len(transfers), 2)
        self.assertEqual({t.is_extended for t in transfers}, {False, True})

    def test_key_restriction(self):
        frames = _multi(bytes(range(20)), arb=0x7E8) + \
            _multi(bytes(range(20)), arb=0x7E9)
        transfers = reassemble(_window(frames), key="1:7E9:S")
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0].arb_id, 0x7E9)

    def test_timeout_closes_a_stalled_transfer(self):
        frames = _multi(bytes(range(40)))[:2]
        frames.append(_f(30.0, [0x03, 0x50, 0x01, 0x02]))   # much later
        transfers = reassemble(_window(frames), timeout=1.0)
        self.assertEqual(transfers[0].status, TIMEOUT)
        self.assertIn("no further frames", transfers[0].detail)
        self.assertEqual(transfers[1].status, COMPLETE)


class FlowControlTests(unittest.TestCase):
    def test_flow_control_is_recorded_as_context_only(self):
        frames = _multi(bytes(range(40)))
        frames.insert(1, _f(0.002, [0x30, 0x00, 0x14, 0, 0, 0, 0, 0]))
        transfers = reassemble(_window(frames))
        t = transfers[0]
        self.assertEqual(len(t.flow_control), 1)
        _stamp, status, block_size, st_min = t.flow_control[0]
        self.assertEqual(status, "continue")
        self.assertEqual(block_size, 0)
        self.assertEqual(st_min, 0x14)

    def test_flow_control_does_not_corrupt_the_payload(self):
        payload = bytes(range(40))
        frames = _multi(payload)
        frames.insert(1, _f(0.002, [0x30, 0x00, 0x00, 0, 0, 0, 0, 0]))
        transfers = reassemble(_window(frames))
        self.assertEqual(transfers[0].status, COMPLETE)
        self.assertEqual(transfers[0].data, payload)

    def test_module_declares_it_never_produces_flow_control(self):
        """The reassembler must expose no way to emit anything."""
        for name in ("send", "write", "transmit", "emit_flow_control",
                     "send_flow_control", "open", "connect"):
            self.assertFalse(hasattr(isotp_module, name),
                             "{} must not exist in a passive reassembler".format(name))

    def test_addressing_mode_is_stated_not_implied(self):
        self.assertEqual(ADDRESSING, "normal")


class ProvenanceTests(unittest.TestCase):
    def test_every_contributing_frame_is_retained(self):
        frames = _multi(bytes(range(40)))
        transfers = reassemble(_window(frames))
        self.assertEqual(transfers[0].frame_count, len(frames))
        for frame in transfers[0].frames:
            self.assertIsInstance(frame, CanFrame)

    def test_timestamps_span_the_transfer(self):
        frames = _multi(bytes(range(40)), start=2.0, step=0.02)
        t = reassemble(_window(frames))[0]
        self.assertAlmostEqual(t.first_timestamp, 2.0)
        self.assertGreater(t.last_timestamp, t.first_timestamp)
        self.assertGreater(t.duration, 0)

    def test_frames_are_not_mutated(self):
        frames = _multi(bytes(range(20)))
        before = [(f.timestamp, f.arb_id, f.data) for f in frames]
        reassemble(_window(frames))
        after = [(f.timestamp, f.arb_id, f.data) for f in frames]
        self.assertEqual(before, after)

    def test_pci_name_labels_each_role(self):
        self.assertEqual(pci_name(_f(0.0, [0x03, 1, 2, 3])), "SF")
        self.assertEqual(pci_name(_f(0.0, [0x10, 0x20])), "FF")
        self.assertEqual(pci_name(_f(0.0, [0x21, 1])), "CF")
        self.assertEqual(pci_name(_f(0.0, [0x30, 0, 0])), "FC")

    def test_error_and_remote_frames_are_ignored(self):
        frames = [
            CanFrame(timestamp=0.0, arb_id=0x7E8, data=b"", dlc=0, channel="1",
                     is_error_frame=True),
            _f(0.1, [0x03, 0x50, 0x01, 0x02]),
        ]
        transfers = reassemble(_window(frames))
        self.assertEqual(len(transfers), 1)


class UdsTests(unittest.TestCase):
    def test_positive_response_is_named(self):
        message = interpret(bytes([0x62, 0xF1, 0x90, 0x01, 0x02]))
        self.assertEqual(message.kind, POSITIVE)
        self.assertEqual(message.service, 0x22)
        self.assertEqual(message.service_name, "ReadDataByIdentifier")
        self.assertEqual(message.identifier, 0xF190)

    def test_request_is_named(self):
        message = interpret(bytes([0x22, 0xF1, 0x90]))
        self.assertEqual(message.kind, REQUEST)
        self.assertEqual(message.identifier, 0xF190)

    def test_negative_response_carries_the_code(self):
        message = interpret(bytes([0x7F, 0x22, 0x31]))
        self.assertEqual(message.kind, NEGATIVE)
        self.assertTrue(message.is_negative)
        self.assertEqual(message.nrc, 0x31)
        self.assertEqual(message.nrc_text, "Request out of range")
        self.assertIn("rejected", message.describe())

    def test_response_pending_is_recognised(self):
        message = interpret(bytes([0x7F, 0x22, 0x78]))
        self.assertIn("response pending", message.nrc_text)

    def test_sub_function_strips_the_suppress_bit(self):
        message = interpret(bytes([0x10, 0x83]))
        self.assertEqual(message.sub_function, 0x03)

    def test_unknown_service_is_reported_not_guessed(self):
        message = interpret(bytes([0xA5, 0x01]))
        self.assertEqual(message.kind, UNKNOWN)
        self.assertFalse(message.recognised)
        self.assertIn("not one this tool names", message.detail)

    def test_malformed_negative_response_is_flagged(self):
        message = interpret(bytes([0x7F]))
        self.assertEqual(message.kind, NEGATIVE)
        self.assertIn("truncated", message.detail)

    def test_empty_payload(self):
        self.assertEqual(interpret(b"").kind, UNKNOWN)

    def test_interpretation_never_raises(self):
        for value in range(256):
            interpret(bytes([value]))
            interpret(bytes([value, 0x01, 0x02, 0x03]))

    def test_reassembled_transfer_feeds_interpretation(self):
        """The whole passive path: frames -> payload -> named response."""
        payload = bytes([0x62, 0xF1, 0x90]) + b"WVWZZZ1JZ3W386752"[:17]
        transfers = reassemble(_window(_multi(payload)))
        self.assertEqual(transfers[0].status, COMPLETE)
        message = interpret(transfers[0].data)
        self.assertEqual(message.kind, POSITIVE)
        self.assertEqual(message.identifier, 0xF190)
        self.assertEqual(transfers[0].data, payload,
                         "the raw reconstructed payload stays available")


if __name__ == "__main__":
    unittest.main()
