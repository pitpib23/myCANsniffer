"""Definition provenance, persistence, association, and conflict models."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError

from cansniff.analysis.canopen_definitions import (
    DefinitionCache, parse_definition_file,
)
from cansniff.analysis.definitions import (
    CanopenNodeAssociation, DefinitionSourceKind, ValidationState,
)
from cansniff.analysis.signals import Profile, ProfileStore, Signal


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic_drive.eds")


class DefinitionModelTests(unittest.TestCase):
    def setUp(self):
        self.definition = parse_definition_file(FIXTURE)

    def test_source_is_immutable_and_hashed(self):
        self.assertEqual(self.definition.source.kind, DefinitionSourceKind.EDS)
        self.assertEqual(len(self.definition.source.content_hash), 64)
        with self.assertRaises(FrozenInstanceError):
            self.definition.source.display_name = "changed"

    def test_reference_serialization_is_deterministic(self):
        reference = self.definition.reference
        self.assertEqual(reference.to_dict(), reference.to_dict())
        self.assertEqual(type(reference).from_dict(reference.to_dict()), reference)

    def test_profile_persists_definition_and_association(self):
        profile = Profile("test")
        self.assertTrue(profile.add_definition(self.definition.reference))
        profile.associate_canopen(self.definition.source.content_hash, 3, "can0")
        restored = Profile.from_dict(profile.to_dict())
        self.assertEqual(restored.definitions, profile.definitions)
        self.assertEqual(restored.canopen_associations, profile.canopen_associations)

    def test_repeated_content_does_not_overwrite(self):
        profile = Profile("test")
        self.assertTrue(profile.add_definition(self.definition.reference))
        self.assertFalse(profile.add_definition(self.definition.reference))
        self.assertEqual(len(profile.definitions), 1)

    def test_explicit_replace_moves_associations(self):
        profile = Profile("test")
        profile.add_definition(self.definition.reference)
        old_hash = self.definition.source.content_hash
        profile.associate_canopen(old_hash, 3)
        dcf = parse_definition_file(os.path.join(
            os.path.dirname(__file__), "fixtures", "synthetic_machine.dcf"))
        self.assertTrue(profile.replace_definition(old_hash, dcf.reference))
        self.assertEqual(profile.canopen_associations[0].definition_hash,
                         dcf.source.content_hash)

    def test_remove_definition_removes_only_its_associations(self):
        profile = Profile("test")
        profile.add_definition(self.definition.reference)
        profile.associate_canopen(self.definition.source.content_hash, 3)
        profile.remove_definition(self.definition.source.content_hash)
        self.assertEqual(profile.definitions, [])
        self.assertEqual(profile.canopen_associations, [])

    def test_node_range_is_validated(self):
        profile = Profile("test", definitions=[self.definition.reference])
        with self.assertRaises(ValueError):
            profile.associate_canopen(self.definition.source.content_hash, 0)
        with self.assertRaises(ValueError):
            profile.associate_canopen(self.definition.source.content_hash, 128)

    def test_same_definition_may_be_manually_associated_to_multiple_nodes(self):
        profile = Profile("test", definitions=[self.definition.reference])
        profile.associate_canopen(self.definition.source.content_hash, 3)
        profile.associate_canopen(self.definition.source.content_hash, 4)
        self.assertEqual([item.node_id for item in profile.canopen_associations], [3, 4])

    def test_store_round_trip_preserves_old_profiles_and_new_metadata(self):
        profile = Profile("mixed")
        profile.add_definition(self.definition.reference)
        store = ProfileStore([profile], "mixed")
        restored = ProfileStore.from_config(store.to_config())
        self.assertEqual(restored.to_config(), store.to_config())

    def test_malformed_persistent_counts_and_node_do_not_crash_load(self):
        raw_reference = self.definition.reference.to_dict()
        raw_reference["object_count"] = "not-a-number"
        raw = {"profiles": [{
            "name": "bad", "definitions": [raw_reference],
            "canopen_associations": [{
                "definition_hash": self.definition.source.content_hash,
                "node_id": "invalid"}],
        }]}
        store = ProfileStore.from_config(raw)
        self.assertEqual(store.profiles[0].definitions[0].object_count, 0)
        self.assertEqual(store.profiles[0].canopen_associations[0].node_id, 0)

    def test_pre_phase_six_dbc_signal_gets_dbc_provenance_on_load(self):
        raw = Profile("old", "old.dbc", [Signal("Speed")]).to_dict()
        del raw["signals"][0]["source_kind"]
        restored = Profile.from_dict(raw)
        self.assertEqual(restored.signals[0].source_kind, "DBC")

    def test_missing_file_retains_reference(self):
        cache = DefinitionCache()
        reference = self.definition.reference
        missing_source = type(reference.source)(
            reference.source.kind, reference.source.display_name,
            os.path.join(tempfile.gettempdir(), "definitely_missing.eds"),
            reference.source.content_hash, reference.source.imported_at,
            reference.source.format_version, reference.source.parser_version)
        missing_reference = type(reference)(
            missing_source, reference.validation_state, reference.warnings,
            reference.object_count)
        result, state, reason = cache.resolve(missing_reference)
        self.assertIsNone(result)
        self.assertEqual(state, ValidationState.MISSING)
        self.assertIn("missing", reason)

    def test_same_path_changed_content_is_not_accepted(self):
        cache = DefinitionCache()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "device.eds")
            with open(FIXTURE, "rb") as source, open(path, "wb") as target:
                target.write(source.read())
            original = cache.parse_file(path).reference
            with open(path, "ab") as target:
                target.write(b"\n[Changed]\nValue=1\n")
            result, state, reason = cache.resolve(original)
        self.assertIsNone(result)
        self.assertEqual(state, ValidationState.CHANGED)
        self.assertIn("explicit", reason)

    def test_same_content_at_two_paths_keeps_each_path_provenance(self):
        cache = DefinitionCache()
        with tempfile.TemporaryDirectory() as directory:
            paths = [os.path.join(directory, name) for name in ("a.eds", "b.eds")]
            with open(FIXTURE, "rb") as source:
                data = source.read()
            for path in paths:
                with open(path, "wb") as target:
                    target.write(data)
            first = cache.parse_file(paths[0])
            second = cache.parse_file(paths[1])
        self.assertEqual(first.source.content_hash, second.source.content_hash)
        self.assertNotEqual(first.source.location, second.source.location)
        self.assertEqual(first.source.display_name, "a.eds")
        self.assertEqual(second.source.display_name, "b.eds")
        self.assertIs(first.dictionary, second.dictionary)


if __name__ == "__main__":
    unittest.main()
