"""EDS/DCF node resolution and protocol-aware profile matching."""

from __future__ import annotations

import os
import tempfile
import unittest

from cansniff.analysis.canopen_definitions import parse_definition_file
from cansniff.analysis.matching import MatchConflictKind, ProfileMatchCache
from cansniff.analysis.profile import TrafficProfileAccumulator
from cansniff.analysis.protocols import build_protocol_survey
from cansniff.analysis.signals import Profile, ProfileStore
from cansniff.analysis.store import FrameStore
from cansniff.model import CanFrame


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
EDS = os.path.join(FIXTURES, "synthetic_drive.eds")
DCF = os.path.join(FIXTURES, "synthetic_machine.dcf")


def f(arb_id, data, timestamp, channel="can0", extended=False):
    payload = bytes(data)
    return CanFrame(timestamp, arb_id, payload, len(payload),
                    channel=channel, is_extended=extended)


def canopen_node(node):
    frames = [f(0x700 + node, [5], index) for index in range(3)]
    frames += [f(0x180 + node, [1, 2, 3, 4, 5, 6], 3 + index / 10)
               for index in range(3)]
    frames += [
        f(0x600 + node, [0x40, 0x41, 0x60, 0, 0, 0, 0, 0], 4),
        f(0x580 + node, [0x43, 0x41, 0x60, 0, 1, 2, 3, 4], 4.1),
    ]
    return frames


def observed(frames):
    store = FrameStore(max(100, len(frames)))
    store.add(frames)
    accumulator = TrafficProfileAccumulator()
    accumulator.update(frames)
    traffic = accumulator.snapshot(store)
    protocols = build_protocol_survey(store.all_frames(), traffic)
    return traffic, protocols


class CanopenProfileMatchingTests(unittest.TestCase):
    def test_generic_eds_resolves_only_against_observed_node(self):
        definition = parse_definition_file(EDS)
        candidate = Profile("Generic drive", definitions=[definition.reference])
        traffic, protocols = observed(canopen_node(3))
        match = ProfileMatchCache().build(
            traffic, ProfileStore([candidate]), protocols).candidates[0]
        self.assertTrue(match.association_suggestions)
        suggestion = match.association_suggestions[0]
        self.assertEqual(suggestion.node_id, 3)
        self.assertEqual(suggestion.definition_hash, definition.source.content_hash)
        self.assertGreaterEqual(suggestion.matched_pdos, 1)
        self.assertTrue(any("Node 3" in reason for reason in match.reasons))

    def test_dcf_commissioned_node_conflict_is_not_reinterpreted(self):
        definition = parse_definition_file(DCF)
        self.assertEqual(definition.configured_node_id, 4)
        candidate = Profile("Commissioned machine", definitions=[definition.reference])
        traffic, protocols = observed(canopen_node(3))
        match = ProfileMatchCache().build(
            traffic, ProfileStore([candidate]), protocols).candidates[0]
        self.assertIn(MatchConflictKind.CANOPEN_NODE,
                      {item.kind for item in match.conflicts})
        self.assertTrue(any("NodeID 4" in item.explanation
                            for item in match.conflicts))

    def test_configured_pdo_cob_id_mismatch_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "custom.eds")
            with open(EDS, "r", encoding="utf-8") as source:
                data = source.read().replace(
                    "DefaultValue=0x180+$NODEID", "DefaultValue=0x333")
            with open(path, "w", encoding="utf-8") as target:
                target.write(data)
            definition = parse_definition_file(path)
            candidate = Profile("Custom COB-ID", definitions=[definition.reference])
            traffic, protocols = observed(canopen_node(3))
            match = ProfileMatchCache().build(
                traffic, ProfileStore([candidate]), protocols).candidates[0]
            self.assertIn(MatchConflictKind.CONFIGURED_COB_ID,
                          {item.kind for item in match.conflicts})
            self.assertTrue(any("0x333" in item.explanation
                                for item in match.conflicts))

    def test_missing_and_changed_definition_reduce_trust(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "drive.eds")
            with open(EDS, "rb") as source, open(path, "wb") as target:
                target.write(source.read())
            definition = parse_definition_file(path)
            candidate = Profile("Drive", definitions=[definition.reference])
            traffic, protocols = observed(canopen_node(3))
            with open(path, "ab") as target:
                target.write(b"\n[Changed]\nValue=1\n")
            changed = ProfileMatchCache().build(
                traffic, ProfileStore([candidate]), protocols).candidates[0]
            self.assertIn(MatchConflictKind.SOURCE_CHANGED,
                          {item.kind for item in changed.conflicts})
            os.remove(path)
            missing = ProfileMatchCache().build(
                traffic, ProfileStore([candidate]), protocols).candidates[0]
            self.assertIn(MatchConflictKind.SOURCE_MISSING,
                          {item.kind for item in missing.conflicts})

    def test_extended_j1939_shape_does_not_accidentally_match_canopen_eds(self):
        definition = parse_definition_file(EDS)
        candidate = Profile("CANopen", definitions=[definition.reference])
        frames = [f(0x18F00403, [0] * 8, index, extended=True)
                  for index in range(10)]
        traffic, protocols = observed(frames)
        match = ProfileMatchCache().build(
            traffic, ProfileStore([candidate]), protocols).candidates[0]
        self.assertEqual(match.coverage.matched_observed_keys, 0)
        self.assertFalse(match.association_suggestions)

    def test_existing_manual_association_is_used_but_not_changed(self):
        definition = parse_definition_file(EDS)
        candidate = Profile("Associated", definitions=[definition.reference])
        candidate.associate_canopen(definition.source.content_hash, 3, "can0")
        before = tuple(candidate.canopen_associations)
        traffic, protocols = observed(canopen_node(3))
        match = ProfileMatchCache().build(
            traffic, ProfileStore([candidate]), protocols).candidates[0]
        self.assertGreater(match.coverage.matched_defined_keys, 0)
        self.assertEqual(tuple(candidate.canopen_associations), before)
        self.assertFalse(match.association_suggestions)


if __name__ == "__main__":
    unittest.main()
