"""Synthetic coverage for passive adapter and Classic bitrate discovery."""

from __future__ import annotations

import threading
import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError

from cansniff.discovery.adapters import enumerate_adapters, installed_backends
from cansniff.discovery.bitrate import DiscoveryThresholds, discover_bitrate
from cansniff.discovery.model import (
    AdapterDescriptor, CandidateStatus, DiscoverySafetyMode, DiscoveryStatus,
    EnumerationStatus, PassiveCapability, Provenance,
)
from cansniff.model import CanFrame
from cansniff.config import Config
from cansniff.sources import PassiveSafetyError, SourceError
from cansniff.sources.live import LiveSource


def _adapter(interface="kvaser", auto=True,
             passive=PassiveCapability.SUPPORTED_BY_BACKEND_POLICY, **kwargs):
    return AdapterDescriptor(
        display_name="Test adapter", interface=interface, channel="0",
        passive_capability=passive,
        enumeration_source=Provenance.DETECTED,
        auto_bitrate_supported=auto,
        implementation_supported=True,
        **kwargs,
    )


def _frame(index=0, arb_id=0x123, **kwargs):
    return CanFrame(
        timestamp=index * 0.1, arb_id=arb_id, data=b"\x01\x02", dlc=2,
        channel="0", **kwargs)


class AdapterEnumerationTests(unittest.TestCase):
    def test_zero_adapters_is_distinct_from_failure(self):
        result = enumerate_adapters(
            interfaces=("kvaser",), detector=lambda _name, _timeout: [],
            include_virtual=False)
        self.assertEqual(result.adapters, ())
        self.assertEqual(result.backends[0].status, EnumerationStatus.EMPTY)

    def test_one_adapter_preserves_detected_and_unknown_capabilities(self):
        result = enumerate_adapters(
            interfaces=("kvaser",),
            detector=lambda _name, _timeout: [{"channel": 2, "serial": "abc"}],
            include_virtual=False)
        adapter = result.adapters[0]
        self.assertEqual(adapter.channel, "2")
        self.assertIs(adapter.supports_fd, None)
        self.assertEqual(adapter.enumeration_source, Provenance.DETECTED)
        self.assertFalse(adapter.hardware_qualified)
        self.assertFalse(adapter.electrically_verified)
        self.assertIn(("serial", "abc"), adapter.metadata)
        with self.assertRaises(FrozenInstanceError):
            adapter.channel = "changed"

    def test_multiple_adapters_are_deduplicated_and_stably_ordered(self):
        configs = [{"channel": 3}, {"channel": 1}, {"channel": 3}]
        result = enumerate_adapters(
            interfaces=("kvaser",), detector=lambda _n, _t: configs,
            include_virtual=False)
        self.assertEqual([item.channel for item in result.adapters], ["1", "3"])

    def test_backend_outcomes_are_isolated(self):
        def detector(name, _timeout):
            if name == "kvaser":
                raise OSError("driver missing")
            raise RuntimeError("backend exploded")

        with self.assertLogs("cansniff.discovery.adapters", level="INFO"):
            result = enumerate_adapters(
                interfaces=("kvaser", "pcan"), detector=detector,
                include_virtual=False)
        self.assertEqual(
            [item.status for item in result.backends],
            [EnumerationStatus.BACKEND_UNAVAILABLE, EnumerationStatus.ERROR])

    def test_unknown_backend_is_not_filtered_when_detector_finds_device(self):
        result = enumerate_adapters(
            interfaces=("vendor_future",),
            detector=lambda _n, _t: [{"channel": "device-7"}],
            include_virtual=False)
        self.assertEqual(result.backends[0].status, EnumerationStatus.FOUND)
        self.assertEqual(result.adapters[0].interface, "vendor_future")
        self.assertTrue(result.adapters[0].auto_bitrate_supported)
        self.assertEqual(result.adapters[0].passive_capability,
                         PassiveCapability.UNKNOWN)

    def test_virtual_is_clearly_non_physical(self):
        result = enumerate_adapters(interfaces=(), include_virtual=True)
        adapter = result.adapters[0]
        self.assertEqual(adapter.interface, "virtual")
        self.assertFalse(adapter.is_physical)
        self.assertEqual(adapter.passive_capability, PassiveCapability.NOT_APPLICABLE)
        self.assertFalse(adapter.auto_bitrate_supported)

    def test_pcan_and_socketcan_remain_visible_and_scannable_with_confirmation(self):
        result = enumerate_adapters(
            interfaces=("pcan", "socketcan"),
            detector=lambda name, _timeout: [{"channel": name + "0"}],
            include_virtual=False)
        self.assertTrue(result.adapters)
        self.assertTrue(all(item.auto_bitrate_supported for item in result.adapters))
        capabilities = {item.interface: item.passive_capability
                        for item in result.adapters}
        self.assertEqual(capabilities["pcan"], PassiveCapability.UNSUPPORTED)
        self.assertEqual(capabilities["socketcan"],
                         PassiveCapability.EXTERNAL_VERIFICATION_REQUIRED)

    def test_slcan_is_visible_only_when_backend_detector_returns_channel(self):
        found = enumerate_adapters(
            interfaces=("slcan",),
            detector=lambda _n, _t: [{"interface": "slcan", "channel": "COM7"}],
            include_virtual=False)
        self.assertEqual(found.adapters[0].channel, "COM7")
        self.assertTrue(found.adapters[0].auto_bitrate_supported)
        self.assertEqual(found.adapters[0].passive_capability,
                         PassiveCapability.UNSUPPORTED)
        empty = enumerate_adapters(
            interfaces=("slcan",), detector=lambda _n, _t: [],
            include_virtual=False)
        self.assertEqual(empty.adapters, ())
        self.assertIn("manual channel", empty.backends[0].message)

    def test_enumerated_serial_port_becomes_only_unverified_slcan_candidate(self):
        result = enumerate_adapters(
            interfaces=("serial", "slcan"),
            detector=lambda name, _timeout: (
                [{"interface": "serial", "channel": "COM7"}]
                if name == "serial" else []),
            include_virtual=False)
        by_key = {item.key: item for item in result.adapters}
        self.assertNotIn(("serial", "COM7"), by_key)
        self.assertIn(("slcan", "COM7"), by_key)
        slcan = by_key[("slcan", "COM7")]
        self.assertEqual(slcan.display_name, "SLCAN candidate COM7")
        self.assertEqual(slcan.enumeration_source, Provenance.BACKEND_KNOWN)
        self.assertTrue(slcan.auto_bitrate_supported)
        self.assertIn(750000, slcan.supported_bitrates)
        backend = next(item for item in result.backends
                       if item.interface == "slcan")
        self.assertEqual(backend.status, EnumerationStatus.FOUND)
        self.assertIn("unverified", backend.message)
        serial_backend = next(item for item in result.backends
                              if item.interface == "serial")
        self.assertEqual(serial_backend.adapters, ())
        self.assertIn("not offered", serial_backend.message)

    def test_default_backend_set_comes_from_installed_python_can_registry(self):
        names = installed_backends()
        self.assertIn("slcan", names)
        self.assertIn("kvaser", names)

    def test_mixed_backends_are_deduplicated_and_stably_ordered(self):
        def detector(name, _timeout):
            return [{"channel": "2"}, {"channel": "1"}, {"channel": "2"}]
        result = enumerate_adapters(
            interfaces=("slcan", "kvaser", "pcan"), detector=detector,
            include_virtual=False)
        self.assertEqual(
            [(item.interface, item.channel) for item in result.adapters],
            [("kvaser", "1"), ("kvaser", "2"), ("pcan", "1"),
             ("pcan", "2"), ("slcan", "1"), ("slcan", "2")])

    def test_cancellation_prevents_the_next_backend(self):
        event = threading.Event()
        calls = []

        def detector(name, _timeout):
            calls.append(name)
            event.set()
            return []

        result = enumerate_adapters(
            interfaces=("kvaser", "pcan"), detector=detector,
            cancel_event=event, include_virtual=False)
        self.assertEqual(calls, ["kvaser"])
        self.assertTrue(result.cancelled)

    def test_legacy_config_loads_with_discovery_defaults(self):
        path = os.path.join(tempfile.gettempdir(),
                            "cansniff_discovery_legacy_{}.json".format(id(self)))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"source": {"type": "file"}}, handle)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        config = Config.load(path)
        self.assertEqual(config.get("source.type"), "file")
        self.assertIn(500000, config.get("discovery.classic_bitrates"))


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Factory:
    def __init__(self, clock, scenarios, cancel_event=None):
        self.clock = clock
        self.scenarios = scenarios
        self.cancel_event = cancel_event
        self.settings = []
        self.active = 0
        self.maximum_active = 0
        self.closed = 0

    def __call__(self, settings):
        self.settings.append(dict(settings))
        scenario = self.scenarios.get(settings["bitrate"], [])
        factory = self

        class Source:
            def __init__(self):
                self.items = list(scenario) if isinstance(scenario, list) else []
                self.opened = False

            def open(self):
                if isinstance(scenario, BaseException):
                    raise scenario
                self.opened = True
                factory.active += 1
                factory.maximum_active = max(factory.maximum_active, factory.active)

            def receive(self, timeout=0.1):
                if self.items:
                    delay, item = self.items.pop(0)
                    factory.clock.advance(delay)
                    if factory.cancel_event is not None:
                        factory.cancel_event.set()
                    if isinstance(item, BaseException):
                        raise item
                    return item
                factory.clock.advance(timeout)
                return None

            def close(self):
                if self.opened:
                    factory.active -= 1
                    factory.closed += 1
                    self.opened = False

        return Source()


THRESHOLDS = DiscoveryThresholds(
    observation_window=1.0,
    receive_timeout=0.1,
    minimum_valid_frames=4,
    minimum_repeated_ids=1,
    minimum_traffic_span=0.5,
    minimum_window_coverage=0.5,
    maximum_error_ratio=0.2,
)


def _stable_frames(arb_id=0x123):
    return [(0.2, _frame(i, arb_id)) for i in range(5)]


class BitrateDiscoveryTests(unittest.TestCase):
    def _run(self, scenarios, candidates=(250000, 500000), event=None):
        clock = _Clock()
        factory = _Factory(clock, scenarios, event)
        result = discover_bitrate(
            _adapter(), candidates=candidates, thresholds=THRESHOLDS,
            cancel_event=event, source_factory=factory, clock=clock)
        return result, factory

    def test_one_stable_bitrate_is_selected_with_evidence(self):
        result, factory = self._run({500000: _stable_frames()})
        self.assertEqual(result.status, DiscoveryStatus.DETECTED)
        self.assertEqual(result.selected_bitrate, 500000)
        winner = result.candidate_results[1]
        self.assertEqual(winner.status, CandidateStatus.STABLE)
        self.assertEqual(winner.valid_frames, 5)
        self.assertEqual(winner.repeated_ids, 1)
        self.assertEqual(factory.maximum_active, 1)
        self.assertEqual(factory.active, 0)

    def test_configurable_thresholds_keep_hard_false_positive_floors(self):
        thresholds = DiscoveryThresholds(
            minimum_valid_frames=1, minimum_repeated_ids=0,
            minimum_traffic_span=0, minimum_window_coverage=0,
            maximum_error_ratio=1.0)
        self.assertEqual(thresholds.minimum_valid_frames, 2)
        self.assertEqual(thresholds.minimum_repeated_ids, 1)
        self.assertGreater(thresholds.minimum_traffic_span, 0)
        self.assertGreater(thresholds.minimum_window_coverage, 0)
        self.assertLessEqual(thresholds.maximum_error_ratio, 0.5)

    def test_silent_bus_has_no_result(self):
        result, _factory = self._run({})
        self.assertEqual(result.status, DiscoveryStatus.NO_TRAFFIC)
        self.assertIsNone(result.selected_bitrate)

    def test_multiple_stable_candidates_are_ambiguous(self):
        result, _factory = self._run({
            250000: _stable_frames(0x100),
            500000: _stable_frames(0x200),
        })
        self.assertEqual(result.status, DiscoveryStatus.AMBIGUOUS)
        self.assertIsNone(result.selected_bitrate)

    def test_one_frame_cannot_win(self):
        result, _factory = self._run({500000: [(0.8, _frame())]})
        self.assertEqual(result.status, DiscoveryStatus.INCONCLUSIVE)
        self.assertEqual(result.candidate_results[1].status, CandidateStatus.WEAK)

    def test_startup_burst_cannot_win(self):
        burst = [(0.01, _frame(i)) for i in range(8)]
        result, _factory = self._run({500000: burst})
        self.assertEqual(result.candidate_results[1].status, CandidateStatus.POSSIBLE)
        self.assertIsNone(result.selected_bitrate)

    def test_only_error_frames_cannot_win(self):
        errors = [(0.2, _frame(i, is_error_frame=True)) for i in range(5)]
        result, _factory = self._run({500000: errors})
        candidate = result.candidate_results[1]
        self.assertEqual(candidate.status, CandidateStatus.WEAK)
        self.assertEqual(candidate.valid_frames, 0)
        self.assertEqual(candidate.error_frames, 5)

    def test_only_remote_frames_cannot_win(self):
        remotes = [(0.2, _frame(i, is_remote_frame=True)) for i in range(5)]
        result, _factory = self._run({500000: remotes})
        candidate = result.candidate_results[1]
        self.assertEqual(candidate.status, CandidateStatus.WEAK)
        self.assertEqual(candidate.valid_frames, 0)
        self.assertEqual(candidate.remote_frames, 5)

    def test_exact_duplicate_delivery_is_not_independent_evidence(self):
        duplicate = _frame(1)
        observations = [(0.2, duplicate) for _ in range(5)]
        result, _factory = self._run({500000: observations})
        candidate = result.candidate_results[1]
        self.assertEqual(candidate.status, CandidateStatus.WEAK)
        self.assertEqual(candidate.valid_frames, 1)
        self.assertTrue(any("duplicate" in reason for reason in candidate.reasons))

    def test_repeated_malformed_frames_cannot_win(self):
        malformed = [(0.2, CanFrame(i * 0.1, 0x123, b"\x01" * 9, 9))
                     for i in range(5)]
        result, _factory = self._run({500000: malformed})
        self.assertEqual(result.candidate_results[1].valid_frames, 0)

    def test_unsupported_candidate_and_open_failure_are_structured(self):
        result, _factory = self._run({
            250000: SourceError("unsupported bitrate 250000"),
            500000: SourceError("adapter disappeared"),
        })
        self.assertEqual(
            [item.status for item in result.candidate_results],
            [CandidateStatus.UNSUPPORTED, CandidateStatus.ERROR])

    def test_passive_rejection_aborts_remaining_candidates(self):
        result, factory = self._run({
            250000: PassiveSafetyError("silent mode unavailable"),
            500000: _stable_frames(),
        })
        self.assertEqual(result.status, DiscoveryStatus.SAFETY_REJECTED)
        self.assertEqual(len(factory.settings), 1)

    def test_each_candidate_has_fresh_settings_and_no_overlap(self):
        result, factory = self._run({250000: [(0.2, _frame())]})
        self.assertEqual(len(factory.settings), 2)
        self.assertEqual([item["bitrate"] for item in factory.settings],
                         [250000, 500000])
        self.assertTrue(all(item["fd"] is False for item in factory.settings))
        self.assertTrue(all(item["require_listen_only"] is True
                            for item in factory.settings))
        self.assertEqual(factory.maximum_active, 1)
        self.assertEqual(result.candidate_results[1].frames, 0,
                         "the second candidate must not inherit the first frame")

    def test_cancellation_closes_source_and_stops_next_candidate(self):
        event = threading.Event()
        result, factory = self._run(
            {250000: [(0.1, _frame())]}, event=event)
        self.assertEqual(result.status, DiscoveryStatus.CANCELLED)
        self.assertEqual(len(factory.settings), 1)
        self.assertEqual(factory.active, 0)
        self.assertEqual(factory.closed, 1)

    def test_receive_exception_is_structured_and_next_candidate_runs(self):
        result, factory = self._run({
            250000: [(0.1, RuntimeError("device removed"))],
            500000: _stable_frames(),
        })
        self.assertEqual(result.candidate_results[0].status, CandidateStatus.ERROR)
        self.assertEqual(result.status, DiscoveryStatus.DETECTED)
        self.assertEqual(factory.closed, 2)

    def test_unsupported_backend_never_constructs_source(self):
        clock = _Clock()
        factory = _Factory(clock, {})
        result = discover_bitrate(
            _adapter(interface="pcan", auto=False),
            source_factory=factory, clock=clock)
        self.assertEqual(result.status, DiscoveryStatus.UNSUPPORTED)
        self.assertEqual(factory.settings, [])

    def test_unsupported_passive_adapter_default_is_refused_before_open(self):
        clock = _Clock()
        factory = _Factory(clock, {})
        result = discover_bitrate(
            _adapter(interface="slcan", passive=PassiveCapability.UNSUPPORTED),
            candidates=(500000,), source_factory=factory, clock=clock)
        self.assertEqual(result.status, DiscoveryStatus.SAFETY_REJECTED)
        self.assertEqual(factory.settings, [])

    def test_explicit_non_passive_mode_allows_one_scan_without_mutating_settings(self):
        clock = _Clock()
        factory = _Factory(clock, {500000: _stable_frames()})
        base = {"require_listen_only": True, "vendor_option": 4}
        result = discover_bitrate(
            _adapter(interface="slcan", passive=PassiveCapability.UNSUPPORTED),
            candidates=(500000,), base_settings=base, thresholds=THRESHOLDS,
            source_factory=factory, clock=clock,
            safety_mode=DiscoverySafetyMode.USER_CONFIRMED_NON_PASSIVE)
        self.assertEqual(result.status, DiscoveryStatus.DETECTED)
        self.assertEqual(result.safety_mode,
                         DiscoverySafetyMode.USER_CONFIRMED_NON_PASSIVE)
        self.assertFalse(factory.settings[0]["require_listen_only"])
        self.assertEqual(base["require_listen_only"], True)
        self.assertTrue(any("NON-PASSIVE" in warning for warning in result.warnings))

        # The one-scan authorization did not alter the production default.
        ordinary = LiveSource({"interface": "slcan", "channel": "COM7",
                               "bitrate": 500000,
                               "require_listen_only": True})
        with self.assertRaises(PassiveSafetyError):
            ordinary.qualification_plan()

    def test_external_passive_requires_current_verification(self):
        adapter = _adapter(
            interface="socketcan",
            passive=PassiveCapability.EXTERNAL_VERIFICATION_REQUIRED)
        clock = _Clock()
        refused_factory = _Factory(clock, {})
        refused = discover_bitrate(
            adapter, candidates=(500000,), source_factory=refused_factory,
            clock=clock, passive_verifier=lambda _adapter: False)
        self.assertEqual(refused.status, DiscoveryStatus.SAFETY_REJECTED)
        self.assertEqual(refused_factory.settings, [])

        allowed_factory = _Factory(clock, {})
        allowed = discover_bitrate(
            adapter, candidates=(500000,), source_factory=allowed_factory,
            clock=clock, passive_verifier=lambda _adapter: True)
        self.assertEqual(allowed.status, DiscoveryStatus.NO_TRAFFIC)
        self.assertTrue(allowed_factory.settings[0]["require_listen_only"])

    def test_capability_rates_are_skipped_without_constructing_source(self):
        clock = _Clock()
        factory = _Factory(clock, {250000: _stable_frames()})
        adapter = _adapter(supported_bitrates=(250000,))
        result = discover_bitrate(
            adapter, candidates=(125000, 250000, 500000), thresholds=THRESHOLDS,
            source_factory=factory, clock=clock)
        self.assertEqual([item["bitrate"] for item in factory.settings], [250000])
        self.assertEqual(
            [item.status for item in result.candidate_results],
            [CandidateStatus.UNSUPPORTED, CandidateStatus.STABLE,
             CandidateStatus.UNSUPPORTED])


if __name__ == "__main__":
    unittest.main()
