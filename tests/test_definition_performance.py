"""Broad sanity bounds against accidental quadratic definition work."""

from __future__ import annotations

import os
import time
import unittest

from cansniff.analysis.canopen_definitions import (
    DefinitionCache, decode_observed_pdos, parse_definition_bytes,
    parse_definition_file,
)
from cansniff.analysis.signals import Profile, ProfileStore
from cansniff.model import CanFrame


EDS = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic_drive.eds")


class DefinitionPerformanceTests(unittest.TestCase):
    def test_large_dictionary_parse_is_linear_enough(self):
        sections = ["[FileInfo]\nFileVersion=1\n"]
        for index in range(0x2000, 0x2000 + 3000):
            sections.append(
                "[{0:04X}]\nParameterName=Object {0:04X}\nObjectType=7\n"
                "DataType=7\nAccessType=rw\nDefaultValue={0}\nPDOMapping=1\n"
                .format(index))
        data = "\n".join(sections).encode("ascii")
        started = time.perf_counter()
        definition = parse_definition_bytes(data, "large.eds")
        elapsed = time.perf_counter() - started
        self.assertEqual(len(definition.dictionary.objects), 3000)
        self.assertLess(elapsed, 8.0)

    def test_content_cache_reuses_parsed_immutable_dictionary(self):
        cache = DefinitionCache()
        first = cache.parse_file(EDS)
        second = cache.parse_file(EDS)
        self.assertIs(first.dictionary, second.dictionary)
        self.assertIs(first.pdo_mappings, second.pdo_mappings)
        self.assertEqual(cache.size, 1)

    def test_large_retained_window_decode_is_not_quadratic(self):
        definition = parse_definition_file(EDS)
        payload = b"\x34\x12" + (10).to_bytes(4, "little", signed=True)
        frames = tuple(CanFrame(index / 1000.0, 0x183, payload, 6)
                       for index in range(50000))
        started = time.perf_counter()
        decoded, conflicts = decode_observed_pdos(definition, 3, frames)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(decoded), 50000)
        self.assertFalse(conflicts)
        self.assertLess(elapsed, 8.0)

    def test_profile_load_with_many_definition_references_is_fast(self):
        definition = parse_definition_file(EDS)
        raw = ProfileStore([
            Profile("profile {}".format(index), definitions=[definition.reference])
            for index in range(100)
        ], "profile 0").to_config()
        started = time.perf_counter()
        restored = ProfileStore.from_config(raw)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(restored.profiles), 100)
        self.assertLess(elapsed, 2.0)

    def test_mapped_decode_with_large_dictionary_uses_indexed_lookup(self):
        last_index = 0x2000 + 2999
        sections = [
            "[FileInfo]\nFileVersion=1\n",
            "[1A00]\nObjectType=9\nSubNumber=2\n",
            "[1A00sub0]\nDataType=5\nDefaultValue=1\n",
            "[1A00sub1]\nDataType=7\nDefaultValue=0x{:08X}\n".format(
                (last_index << 16) | 8),
        ]
        for index in range(0x2000, last_index + 1):
            sections.append(
                "[{0:04X}]\nParameterName=Object {0:04X}\nObjectType=7\n"
                "DataType=5\nPDOMapping=1\n".format(index))
        definition = parse_definition_bytes(
            "\n".join(sections).encode("ascii"), "mapped-large.eds")
        frames = tuple(CanFrame(index / 1000.0, 0x181, b"\x7F", 1)
                       for index in range(10000))
        started = time.perf_counter()
        decoded, conflicts = decode_observed_pdos(definition, 1, frames)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(decoded), 10000)
        self.assertFalse(conflicts)
        self.assertLess(elapsed, 4.0)


if __name__ == "__main__":
    unittest.main()
