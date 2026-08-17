"""ISO-TP evidence: which CAN IDs actually look like ISO-TP, and why.

The reassembler will parse almost anything short into a tidy "complete" single
frame. These tests pin the difference between *parsing* as ISO-TP and *being*
ISO-TP, because presenting the first as the second is the failure mode that
matters on an undocumented industrial bus.

Nothing here opens a CAN interface or sends a frame, including flow control.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cansniff.analysis.isotp import (  # noqa: E402
    COMPLETE, LENGTH_MISMATCH, ORPHAN_CF, SEQUENCE_ERROR, frame_facts,
)
from cansniff.analysis.isotp_survey import (  # noqa: E402
    NONE, POSSIBLE, STRONG, WEAK, IsoTpSurveyCache, survey, transfers_for,
)
from cansniff.analysis.store import FrameStore  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402


def _f(arb, spec, t=0.0, channel="1", extended=False):
    data = bytes(int(x, 16) for x in spec.split()) if isinstance(spec, str) \
        else bytes(spec)
    return CanFrame(timestamp=t, arb_id=arb, data=data, dlc=len(data),
                    channel=channel, is_extended=extended)


def _survey(frames):
    store = FrameStore(500000)
    store.add(frames)
    rows, by_key = survey(store.all_frames())
    return {row.id_label: row for row in rows}, by_key, store


class FrameSyntaxTests(unittest.TestCase):
    """One frame, read as an ISO-TP candidate. No sequence, no conclusions."""

    def test_single_frame_fields(self):
        facts = frame_facts(_f(0x100, "02 50 03"))
        self.assertEqual(facts.name, "SF")
        self.assertEqual(facts.declared_length, 2)
        self.assertEqual(facts.payload, b"\x50\x03")
        self.assertEqual(facts.extra, b"")

    def test_trailing_bytes_are_reported_not_dropped(self):
        """The regression the brief names: 01 00 03 must expose the 03."""
        facts = frame_facts(_f(0x100, "01 00 03"))
        self.assertEqual(facts.declared_length, 1)
        self.assertEqual(facts.payload, b"\x00")
        self.assertEqual(facts.extra, b"\x03")
        self.assertEqual(facts.extra_hex, "03")

    def test_first_frame_declares_a_twelve_bit_length(self):
        facts = frame_facts(_f(0x7E8, "10 14 62 F1 90 57 56 57"))
        self.assertEqual(facts.name, "FF")
        self.assertEqual(facts.declared_length, 0x014)
        self.assertEqual(len(facts.payload), 6)

    def test_consecutive_frame_exposes_its_sequence_number(self):
        facts = frame_facts(_f(0x7E8, "25 01 02 03"))
        self.assertEqual(facts.name, "CF")
        self.assertEqual(facts.sequence, 5)
        self.assertIn("SN 5", facts.sequence_or_flow)

    def test_flow_control_exposes_status_block_size_and_stmin(self):
        facts = frame_facts(_f(0x7E0, "30 08 14"))
        self.assertEqual(facts.name, "FC")
        self.assertEqual(facts.flow_status, "continue")
        self.assertEqual(facts.block_size, 8)
        self.assertEqual(facts.st_min, 0x14)
        self.assertIn("BS=8", facts.flow_detail)

    def test_a_reserved_flow_status_is_flagged(self):
        facts = frame_facts(_f(0x7E0, "35 00 00"))
        self.assertIn("reserved", facts.flow_status)
        self.assertIn("reserved", facts.problem)

    def test_a_truncated_flow_control_is_flagged(self):
        self.assertIn("shorter", frame_facts(_f(0x7E0, "30")).problem)

    def test_a_non_isotp_nibble_is_not_a_candidate(self):
        facts = frame_facts(_f(0x2A0, "A5 01 02 03"))
        self.assertFalse(facts.is_candidate)
        self.assertEqual(facts.name, "")

    def test_an_empty_or_error_frame_is_not_a_candidate(self):
        self.assertFalse(frame_facts(_f(0x100, "")).is_candidate)
        error = CanFrame(timestamp=0.0, arb_id=0x100, data=b"\x01\x00",
                         dlc=2, channel="1", is_error_frame=True)
        self.assertFalse(frame_facts(error).is_candidate)

    def test_an_oversized_single_frame_length_is_flagged(self):
        """0F declares 15 bytes; a classic frame can carry at most 7."""
        facts = frame_facts(_f(0x100, "0F 01 02 03 04 05 06 07"))
        self.assertIn("declares 15 bytes but carries 7", facts.problem)

    def test_reading_a_frame_never_mutates_it(self):
        frame = _f(0x100, "01 00 03")
        before = bytes(frame.data)
        frame_facts(frame)
        self.assertEqual(frame.data, before)


class WeakEvidenceTests(unittest.TestCase):
    """The regression the brief specifies verbatim."""

    def setUp(self):
        self.rows, self.by_key, self.store = _survey([
            _f(0x100, "01 00 01", 0.0),
            _f(0x100, "01 00 02", 0.3),
            _f(0x100, "01 00 03", 0.6),
        ])
        self.row = self.rows["0x100"]

    def test_single_frames_alone_are_never_more_than_weak(self):
        self.assertEqual(self.row.evidence, WEAK)

    def test_the_frames_are_still_counted_and_parsed(self):
        """Weak evidence is not a refusal to analyse: the data is all there."""
        self.assertEqual(self.row.sf, 3)
        self.assertEqual(self.row.frames, 3)
        self.assertEqual(self.row.complete, 3)

    def test_the_reason_says_why_it_is_weak(self):
        why = self.row.why()
        self.assertIn("only single-frame candidates", why)
        self.assertIn("no first, consecutive or flow-control", why)

    def test_the_unexplained_trailing_byte_is_part_of_the_reason(self):
        self.assertIn("after its declared payload", self.row.why())
        self.assertEqual(self.row.sf_with_extra, 3)

    def test_a_thousand_of_them_is_still_weak(self):
        """Volume is not evidence. Repetition of a guess is still a guess."""
        rows, _by_key, _store = _survey(
            [_f(0x100, "01 00 {:02X}".format(i % 3 + 1), i * 0.3)
             for i in range(1332)])
        self.assertEqual(rows["0x100"].evidence, WEAK)
        self.assertEqual(rows["0x100"].sf, 1332)

    def test_raw_frames_survive_into_the_transfer(self):
        transfers = transfers_for(self.store.all_frames(), self.row.key)
        self.assertEqual(transfers[0].frames[0].data, b"\x01\x00\x01")


class StrongEvidenceTests(unittest.TestCase):
    """The coherent multi-frame case from the brief."""

    def setUp(self):
        self.rows, self.by_key, self.store = _survey([
            _f(0x7E8, "10 0A 62 F1 90 01 02 03", 1.000),
            _f(0x7E0, "30 00 00", 1.001),
            _f(0x7E8, "21 04 05 06 07 08 09 0A", 1.002),
        ])

    def test_a_completed_multiframe_sequence_is_strong(self):
        self.assertEqual(self.rows["0x7E8"].evidence, STRONG)

    def test_it_is_stronger_than_the_single_frame_case(self):
        weak, _b, _s = _survey([_f(0x100, "01 00 01", 0.0)])
        self.assertGreater(self.rows["0x7E8"].rank, weak["0x100"].rank)

    def test_the_reason_names_the_observations(self):
        why = self.rows["0x7E8"].why()
        self.assertIn("multi-frame transfer", why)
        self.assertIn("declared length", why)

    def test_the_flow_control_peer_is_inferred_from_timing(self):
        self.assertEqual(self.rows["0x7E8"].peer_label, "0x7E0")
        self.assertEqual(self.rows["0x7E0"].peer_label, "0x7E8")

    def test_flow_control_appears_in_the_reason_for_the_responder(self):
        self.assertIn("flow control observed from 0x7E0",
                      self.rows["0x7E8"].why())

    def test_the_payload_reassembles_exactly(self):
        transfer = self.by_key[self.rows["0x7E8"].key][0]
        self.assertEqual(transfer.status, COMPLETE)
        self.assertEqual(
            transfer.data,
            bytes([0x62, 0xF1, 0x90, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07]))

    def test_an_id_that_only_sends_flow_control_is_not_strong(self):
        """Answering is not the same as demonstrably running a transfer."""
        self.assertEqual(self.rows["0x7E0"].evidence, POSSIBLE)

    def test_a_numerically_diagnostic_id_earns_nothing_on_its_number(self):
        """0x7E0/0x7E8 must prove themselves like any other ID."""
        rows, _b, _s = _survey([_f(0x7E0, "01 00 03", i * 0.1) for i in range(20)]
                              + [_f(0x7E8, "01 00 04", i * 0.1) for i in range(20)])
        self.assertEqual(rows["0x7E0"].evidence, WEAK)
        self.assertEqual(rows["0x7E8"].evidence, WEAK)


class BrokenTrafficTests(unittest.TestCase):
    def test_first_frame_with_no_continuation(self):
        rows, _b, _s = _survey([_f(0x300, "10 20 01 02 03 04 05 06", 0.0)])
        row = rows["0x300"]
        self.assertEqual(row.evidence, POSSIBLE)
        self.assertEqual(row.ff_without_cf, 1)
        self.assertIn("never followed by a consecutive frame", row.why())

    def test_orphan_consecutive_frame(self):
        rows, by_key, _s = _survey([_f(0x301, "21 01 02 03", 0.0)])
        row = rows["0x301"]
        self.assertEqual(row.orphan_cf, 1)
        self.assertEqual(row.errors, 1)
        self.assertEqual(by_key[row.key][0].status, ORPHAN_CF)
        self.assertIn("no first frame", row.why())

    def test_wrong_sequence_number(self):
        rows, by_key, _s = _survey([
            _f(0x302, "10 14 01 02 03 04 05 06", 0.0),
            _f(0x302, "25 07 08 09 0A 0B 0C 0D", 0.01),
        ])
        row = rows["0x302"]
        self.assertEqual(row.sequence_errors, 1)
        self.assertEqual(by_key[row.key][0].status, SEQUENCE_ERROR)
        self.assertIn("sequence error", row.why())

    def test_a_sequence_error_prevents_strong_evidence(self):
        frames = [_f(0x310, "10 0A 62 F1 90 01 02 03", 1.0),
                  _f(0x310, "21 04 05 06 07 08 09 0A", 1.002),
                  _f(0x310, "10 0A 62 F1 90 01 02 03", 2.0),
                  _f(0x310, "25 04 05 06 07 08 09 0A", 2.002)]
        rows, _b, _s = _survey(frames)
        self.assertEqual(rows["0x310"].evidence, POSSIBLE)

    def test_missing_consecutive_frames_leave_the_transfer_unfinished(self):
        rows, by_key, _s = _survey([
            _f(0x311, "10 28 01 02 03 04 05 06", 0.0),
            _f(0x311, "21 07 08 09 0A 0B 0C 0D", 0.01),
        ])
        transfer = by_key[rows["0x311"].key][0]
        self.assertFalse(transfer.complete)
        self.assertEqual(transfer.declared_length, 0x28)
        self.assertLess(transfer.received_length, 0x28)
        self.assertEqual(rows["0x311"].unfinished, 1)

    def test_duplicate_consecutive_frame_does_not_double_the_payload(self):
        # 20-byte payload: the first CF (7 bytes) does not finish the transfer,
        # so a repeat of it lands inside the session as a genuine duplicate
        # rather than completing the transfer and orphaning the repeat.
        payload = bytes(range(20))
        frames = [
            _f(0x312, [0x10, 20] + list(payload[:6]), 0.0),
            _f(0x312, [0x21] + list(payload[6:13]), 0.01),
        ]
        frames.append(frames[-1])            # exact repeat of CF sequence 1
        frames.append(_f(0x312, [0x22] + list(payload[13:20]), 0.03))
        rows, by_key, _s = _survey(frames)
        transfer = by_key[rows["0x312"].key][0]
        self.assertEqual(transfer.received_length, 20)
        self.assertEqual(transfer.data, payload)
        self.assertEqual(transfer.frame_count, 4, "the duplicate is still shown")

    def test_single_frame_with_impossible_length(self):
        rows, by_key, _s = _survey([_f(0x313, "07 01 02", 0.0)])
        self.assertEqual(by_key.get(rows["0x313"].key, [None])[0], None)
        transfers = transfers_for(_survey([_f(0x313, "07 01 02", 0.0)])[2]
                                  .all_frames(), rows["0x313"].key)
        self.assertEqual(transfers[0].status, LENGTH_MISMATCH)
        self.assertEqual(rows["0x313"].errors, 1)

    def test_traffic_with_no_isotp_shape_gets_no_evidence(self):
        rows, _b, _s = _survey(
            [_f(0x2A0, "A5 01 7F 03 00 11 22 33", i * 0.1) for i in range(50)])
        row = rows["0x2A0"]
        self.assertEqual(row.evidence, NONE)
        self.assertEqual(row.other, 50)
        self.assertEqual(row.candidates, 0)
        self.assertIn("no frame on this ID has an ISO-TP PCI", row.why())

    def test_mostly_non_isotp_traffic_is_held_down_to_weak(self):
        frames = [_f(0x2A1, "A5 01 02 03", i * 0.1) for i in range(20)]
        frames.append(_f(0x2A1, "01 00 03", 5.0))
        rows, _b, _s = _survey(frames)
        self.assertEqual(rows["0x2A1"].evidence, WEAK)
        self.assertIn("not ISO-TP-shaped", rows["0x2A1"].why())


class SeparationTests(unittest.TestCase):
    def test_interleaved_ids_are_surveyed_independently(self):
        frames = []
        for i in range(5):
            frames.append(_f(0x100, "01 00 01", i * 0.1))
            frames.append(_f(0x7E8, "10 0A 62 F1 90 01 02 03", i * 0.1 + 0.01))
            frames.append(_f(0x7E8, "21 04 05 06 07 08 09 0A", i * 0.1 + 0.02))
        rows, _b, _s = _survey(frames)
        self.assertEqual(rows["0x100"].evidence, WEAK)
        self.assertEqual(rows["0x7E8"].evidence, STRONG)

    def test_extended_identifiers_are_surveyed(self):
        rows, _b, _s = _survey([
            _f(0x18DAF110, "10 0A 62 F1 90 01 02 03", 1.0, extended=True),
            _f(0x18DA10F1, "30 00 00", 1.001, extended=True),
            _f(0x18DAF110, "21 04 05 06 07 08 09 0A", 1.002, extended=True),
        ])
        row = rows["0x18DAF110"]
        self.assertEqual(row.evidence, STRONG)
        self.assertEqual(row.peer_label, "0x18DA10F1")

    def test_the_same_id_on_two_channels_stays_separate(self):
        rows, _b, _s = _survey([
            _f(0x100, "01 00 01", 0.0, channel="1"),
            _f(0x100, "10 0A 62 F1 90 01 02 03", 0.1, channel="2"),
            _f(0x100, "21 04 05 06 07 08 09 0A", 0.2, channel="2"),
        ])
        # Both rows carry the same label; the keys are what keep them apart.
        self.assertEqual(len(rows), 1, "identical labels collapse in this dict")
        _all, _by, store = _survey([])
        store = FrameStore()
        store.add([_f(0x100, "01 00 01", 0.0, channel="1"),
                   _f(0x100, "10 0A 62 F1 90 01 02 03", 0.1, channel="2"),
                   _f(0x100, "21 04 05 06 07 08 09 0A", 0.2, channel="2")])
        listed, _by_key = survey(store.all_frames())
        self.assertEqual(len(listed), 2)
        self.assertEqual({r.channel for r in listed}, {"1", "2"})

    def test_a_peer_on_another_channel_is_not_paired(self):
        rows_list, _by = survey(_store([
            _f(0x7E8, "10 0A 62 F1 90 01 02 03", 1.0, channel="1"),
            _f(0x7E0, "30 00 00", 1.001, channel="2"),
            _f(0x7E8, "21 04 05 06 07 08 09 0A", 1.002, channel="1"),
        ]).all_frames())
        by_label = {r.id_label: r for r in rows_list}
        self.assertEqual(by_label["0x7E8"].peer_label, "")

    def test_an_empty_capture_produces_no_rows(self):
        rows, by_key, _s = _survey([])
        self.assertEqual(rows, {})
        self.assertEqual(by_key, {})


def _store(frames):
    store = FrameStore(500000)
    store.add(frames)
    return store


class CacheTests(unittest.TestCase):
    def test_the_survey_is_computed_once_per_window(self):
        store = _store([_f(0x100, "01 00 01", i * 0.1) for i in range(200)])
        cache = IsoTpSurveyCache()
        window = store.all_frames()
        first = cache.rows(window)
        self.assertIs(cache.rows(window), first, "recomputed for the same window")

    def test_new_frames_invalidate_the_survey(self):
        store = _store([_f(0x100, "01 00 01", 0.0)])
        cache = IsoTpSurveyCache()
        before = cache.rows(store.all_frames())[0].frames
        store.add([_f(0x100, "01 00 02", 1.0)])
        after = cache.rows(store.all_frames())[0].frames
        self.assertEqual((before, after), (1, 2))

    def test_transfers_are_available_for_single_frame_only_ids(self):
        """The survey skips reassembling them; drilling in must still work."""
        store = _store([_f(0x100, "01 00 0{}".format(i % 3), i * 0.1)
                        for i in range(50)])
        cache = IsoTpSurveyCache()
        window = store.all_frames()
        rows = cache.rows(window)
        transfers = cache.transfers(window, rows[0].key)
        self.assertEqual(len(transfers), 50)
        self.assertIs(cache.transfers(window, rows[0].key), transfers)


class PerformanceTests(unittest.TestCase):
    """A large capture must not make the page unusable."""

    def test_a_large_single_frame_capture_surveys_quickly(self):
        frames = [_f(0x100, "01 00 {:02X}".format(i & 0xFF), i * 0.001)
                  for i in range(60000)]
        frames += [_f(0x7E8, "10 0A 62 F1 90 01 02 03", 100.0),
                   _f(0x7E0, "30 00 00", 100.001),
                   _f(0x7E8, "21 04 05 06 07 08 09 0A", 100.002)]
        window = _store(frames).all_frames()
        started = time.perf_counter()
        rows, _by_key = survey(window)
        elapsed = time.perf_counter() - started
        # Generous: the point is that reassembling 60,000 single frames is
        # skipped entirely, not that a particular machine hits a particular
        # number.
        self.assertLess(elapsed, 2.0,
                        "survey took {:.2f}s".format(elapsed))
        by_label = {r.id_label: r for r in rows}
        self.assertEqual(by_label["0x100"].sf, 60000)
        self.assertEqual(by_label["0x7E8"].evidence, STRONG)


if __name__ == "__main__":
    unittest.main()
