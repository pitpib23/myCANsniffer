"""Synthetic coverage for passive SocketCAN Classic bitrate discovery."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest

from cansniff.config import Config
from cansniff.discovery.bitrate import (
    DiscoveryThresholds, discover_socketcan_bitrate,
)
from cansniff.discovery.model import CandidateStatus, DiscoveryStatus
from cansniff.model import CanFrame
from cansniff.socketcan import SocketCanError, SocketCanErrorKind
from cansniff.sources import PassiveSafetyError, SourceError


def _frame(index=0, arb_id=0x123, **kwargs):
    return CanFrame(
        timestamp=index * 0.1, arb_id=arb_id, data=b"\x01\x02", dlc=2,
        channel="0", **kwargs)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Factory:
    """Fake LiveSource factory: one scripted (delay, frame_or_exception) list
    per bitrate, exactly like the pre-refactor test double."""

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


class _FakeLink:
    """Records every configure()/down() call; can be scripted to fail."""

    def __init__(self, configure_side_effects=None, down_raises=None):
        self.interface = "can0"
        self.configure_calls = []
        self.down_calls = 0
        self._configure_side_effects = dict(configure_side_effects or {})
        self._down_raises = down_raises

    def configure(self, bitrate, listen_only=True):
        self.configure_calls.append((bitrate, listen_only))
        effect = self._configure_side_effects.get(bitrate)
        if isinstance(effect, BaseException):
            raise effect

    def down(self):
        self.down_calls += 1
        if self._down_raises is not None:
            raise self._down_raises


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
    def _run(self, scenarios, candidates=(250000, 500000), event=None, link=None):
        clock = _Clock()
        factory = _Factory(clock, scenarios, event)
        # Default to a fake link rather than None: unit tests must never
        # shell out to the real `ip` tool, and a real SocketCanLink() here
        # would either fail closed (no `ip`/no can0) or, worse, touch an
        # actual interface on a machine that happens to have one.
        result = discover_socketcan_bitrate(
            "can0", candidates=candidates, thresholds=THRESHOLDS,
            cancel_event=event, source_factory=factory,
            link=link or _FakeLink(), clock=clock)
        return result, factory

    # -- happy path -------------------------------------------------------

    def test_one_stable_bitrate_is_selected_with_evidence(self):
        link = _FakeLink()
        result, factory = self._run({500000: _stable_frames()}, link=link)
        self.assertEqual(result.status, DiscoveryStatus.DETECTED)
        self.assertEqual(result.selected_bitrate, 500000)
        winner = result.candidate_results[1]
        self.assertEqual(winner.status, CandidateStatus.STABLE)
        self.assertEqual(winner.valid_frames, 5)
        self.assertEqual(winner.repeated_ids, 1)
        self.assertEqual(factory.maximum_active, 1)
        self.assertEqual(factory.active, 0)

    def test_stable_id_evidence_is_reported_but_not_the_only_criterion(self):
        result, _factory = self._run({500000: _stable_frames(0x321)})
        winner = result.candidate_results[1]
        self.assertEqual(winner.best_id, 0x321)
        self.assertEqual(winner.best_id_observations, 5)
        self.assertGreater(winner.best_id_span, 0.0)
        self.assertTrue(any("0x321" in reason for reason in winner.reasons))
        # Still requires the full evidence model, not the ID alone -- a
        # candidate with only one observation of an ID cannot win even
        # though it has "a" best_id.
        result_weak, _ = self._run({500000: [(0.2, _frame(0, 0x321))]})
        self.assertEqual(result_weak.candidate_results[1].status, CandidateStatus.WEAK)

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

    # -- conservative rejection --------------------------------------------

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

    def test_single_frame_false_positive_cannot_win(self):
        result, _factory = self._run({500000: [(0.8, _frame())]})
        self.assertEqual(result.status, DiscoveryStatus.INCONCLUSIVE)
        self.assertEqual(result.candidate_results[1].status, CandidateStatus.WEAK)

    def test_startup_burst_cannot_win(self):
        burst = [(0.01, _frame(i)) for i in range(8)]
        result, _factory = self._run({500000: burst})
        self.assertEqual(result.candidate_results[1].status, CandidateStatus.POSSIBLE)
        self.assertIsNone(result.selected_bitrate)

    def test_error_frame_heavy_candidate_is_rejected(self):
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

    # -- link configuration -------------------------------------------------

    def test_each_candidate_forces_listen_only_on(self):
        link = _FakeLink()
        self._run({500000: _stable_frames()}, link=link)
        self.assertTrue(link.configure_calls)
        self.assertTrue(all(listen_only for _bitrate, listen_only in link.configure_calls))

    def test_bitrate_rejected_by_link_is_recorded_and_scan_continues(self):
        link = _FakeLink(configure_side_effects={
            250000: SocketCanError(SocketCanErrorKind.BITRATE_REJECTED, "nope"),
        })
        result, factory = self._run({500000: _stable_frames()}, link=link)
        self.assertEqual(
            result.candidate_results[0].status, CandidateStatus.CONFIGURATION_ERROR)
        self.assertEqual(result.status, DiscoveryStatus.DETECTED)
        # The rejected candidate never got as far as opening a source.
        self.assertEqual(len(factory.settings), 1)

    def test_systemic_configuration_failure_aborts_remaining_candidates(self):
        for kind in (SocketCanErrorKind.IP_UNAVAILABLE,
                     SocketCanErrorKind.INTERFACE_MISSING,
                     SocketCanErrorKind.PERMISSION_DENIED):
            with self.subTest(kind=kind):
                link = _FakeLink(configure_side_effects={
                    250000: SocketCanError(kind, "systemic"),
                })
                result, factory = self._run(
                    {500000: _stable_frames()}, link=link)
                self.assertEqual(result.status, DiscoveryStatus.CONFIGURATION_ERROR)
                self.assertEqual(factory.settings, [],
                                 "no source should ever be opened after a systemic failure")
                self.assertEqual(len(link.configure_calls), 1,
                                 "the second candidate must not be attempted")

    def test_listen_only_unconfirmed_configuration_failure_aborts_the_whole_scan(self):
        # Regression for the "CRITICAL AUTO-SCAN FAILURE CLASSIFICATION" bug:
        # listen-only is one CAN controller-mode bit, unrelated to bitrate,
        # so a genuine failure to confirm it (most often a state-parser
        # mismatch against this system's actual `ip -details` rendering) is
        # systemic -- it must abort the whole scan and say so, rather than
        # silently re-trying every remaining candidate and reporting each
        # one as its own unrelated failure (or worse, "no traffic", if the
        # per-candidate loop happened to keep going).
        link = _FakeLink(configure_side_effects={
            250000: SocketCanError(
                SocketCanErrorKind.LISTEN_ONLY_UNCONFIRMED,
                "listen-only could not be confirmed"),
        })
        result, factory = self._run({500000: _stable_frames()}, link=link)
        self.assertEqual(result.status, DiscoveryStatus.CONFIGURATION_ERROR)
        self.assertEqual(factory.settings, [],
                         "no source should ever be opened after a systemic failure")
        self.assertEqual(len(link.configure_calls), 1,
                         "the second candidate must not be attempted")
        self.assertIn("listen-only", result.reasons[0].lower())

    def test_winning_bitrate_is_explicitly_reconfigured_after_the_scan(self):
        # 250000 is tried and fails after 500000 would have already won --
        # candidates keep going in ascending order, so the link ends up
        # configured at the *last tried* rate unless the winner is
        # explicitly reapplied at the end.
        link = _FakeLink()
        result, _factory = self._run(
            {500000: _stable_frames(), 800000: []},
            candidates=(500000, 800000), link=link)
        self.assertEqual(result.status, DiscoveryStatus.DETECTED)
        self.assertEqual(result.selected_bitrate, 500000)
        self.assertEqual(link.configure_calls[-1], (500000, True))

    def test_failed_final_reconfiguration_is_reported_as_configuration_error(self):
        # A single candidate: one configure() call to observe it, then --
        # since it wins -- one more to explicitly reapply it as the winner.
        # Only that second, final call is scripted to fail.
        link = _FakeLink()
        original_configure = link.configure
        calls = {"n": 0}

        def flaky_configure(bitrate, listen_only=True):
            calls["n"] += 1
            if calls["n"] == 2:
                raise SocketCanError(SocketCanErrorKind.UNKNOWN, "flaky")
            return original_configure(bitrate, listen_only)

        link.configure = flaky_configure
        result, _factory = self._run(
            {500000: _stable_frames()}, candidates=(500000,), link=link)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(result.status, DiscoveryStatus.CONFIGURATION_ERROR)
        self.assertIsNone(result.selected_bitrate)

    def test_cancellation_leaves_the_interface_down(self):
        event = threading.Event()
        link = _FakeLink()
        result, factory = self._run(
            {250000: [(0.1, _frame())]}, event=event, link=link)
        self.assertEqual(result.status, DiscoveryStatus.CANCELLED)
        self.assertEqual(factory.active, 0)
        self.assertEqual(factory.closed, 1)
        self.assertGreaterEqual(link.down_calls, 1)

    def test_no_traffic_result_leaves_the_interface_down(self):
        link = _FakeLink()
        self._run({}, link=link)
        self.assertGreaterEqual(link.down_calls, 1)

    def test_ambiguous_result_leaves_the_interface_down(self):
        link = _FakeLink()
        self._run({250000: _stable_frames(0x100), 500000: _stable_frames(0x200)}, link=link)
        self.assertGreaterEqual(link.down_calls, 1)

    def test_detected_result_does_not_bring_the_interface_down(self):
        link = _FakeLink()
        result, _factory = self._run({500000: _stable_frames()}, link=link)
        self.assertEqual(result.status, DiscoveryStatus.DETECTED)
        self.assertEqual(link.down_calls, 0)

    # -- lifecycle / cleanup -------------------------------------------------

    def test_each_candidate_has_fresh_settings_and_no_overlap(self):
        result, factory = self._run({250000: [(0.2, _frame())]})
        self.assertEqual(len(factory.settings), 2)
        self.assertEqual([item["bitrate"] for item in factory.settings],
                         [250000, 500000])
        self.assertTrue(all(item["fd"] is False for item in factory.settings))
        self.assertTrue(all(item["require_listen_only"] is True
                            for item in factory.settings))
        self.assertTrue(all(item["interface"] == "socketcan"
                            for item in factory.settings))
        self.assertEqual(factory.maximum_active, 1)
        self.assertEqual(result.candidate_results[1].frames, 0,
                         "the second candidate must not inherit the first frame")

    def test_temporary_bus_is_closed_between_every_candidate(self):
        result, factory = self._run(
            {250000: _stable_frames(0x1), 500000: _stable_frames(0x2)},
            candidates=(250000, 500000))
        self.assertEqual(factory.closed, 2)
        self.assertEqual(factory.active, 0)

    def test_cancellation_during_observation_closes_source_and_stops_next_candidate(self):
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

    def test_unsupported_bitrate_from_the_bus_itself_is_structured(self):
        result, _factory = self._run({
            250000: SourceError("unsupported bitrate 250000"),
            500000: _stable_frames(),
        })
        self.assertEqual(result.candidate_results[0].status, CandidateStatus.ERROR)
        self.assertEqual(result.status, DiscoveryStatus.DETECTED)

    def test_listen_only_not_confirmed_after_configuration_fails_closed_for_that_candidate(self):
        result, factory = self._run({
            250000: PassiveSafetyError("listen-only not confirmed"),
            500000: _stable_frames(),
        })
        self.assertEqual(result.candidate_results[0].status, CandidateStatus.ERROR)
        # It did not fall back to a non-listen-only open -- the source
        # factory was still called with require_listen_only True.
        self.assertTrue(factory.settings[0]["require_listen_only"])
        self.assertEqual(result.status, DiscoveryStatus.DETECTED)

    def test_no_can_transmit_method_exists_on_the_discovery_module(self):
        import cansniff.discovery.bitrate as module
        forbidden = {"send", "transmit", "write_frame", "inject", "sendall", "sendto"}
        self.assertFalse(forbidden & set(dir(module)))


class LegacyConfigMigrationTests(unittest.TestCase):
    def test_legacy_config_loads_with_discovery_defaults(self):
        path = os.path.join(tempfile.gettempdir(),
                            "cansniff_discovery_legacy_{}.json".format(id(self)))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"source": {"type": "file"}}, handle)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        config = Config.load(path)
        self.assertEqual(config.get("source.type"), "file")
        self.assertIn(500000, config.get("discovery.classic_bitrates"))

    def test_legacy_live_backend_migrates_to_socketcan(self):
        path = os.path.join(tempfile.gettempdir(),
                            "cansniff_legacy_backend_{}.json".format(id(self)))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"source": {"type": "live", "live": {
                "interface": "kvaser", "channel": "0", "bitrate": 250000,
            }}}, handle)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        config = Config.load(path)
        self.assertEqual(config.get("source.live.interface"), "socketcan")
        # The operator's other settings are preserved, not reset.
        self.assertEqual(config.get("source.live.channel"), "0")
        self.assertEqual(config.get("source.live.bitrate"), 250000)

    def test_virtual_live_backend_is_left_alone(self):
        path = os.path.join(tempfile.gettempdir(),
                            "cansniff_virtual_backend_{}.json".format(id(self)))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"source": {"live": {"interface": "virtual"}}}, handle)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        config = Config.load(path)
        self.assertEqual(config.get("source.live.interface"), "virtual")


if __name__ == "__main__":
    unittest.main()
