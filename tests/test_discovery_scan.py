"""Coverage for cansniff.discovery.scan.scan_bitrate_candidates -- the
per-candidate lifecycle (configure/settle/observe/close) and the
never-auto-selects-a-winner contract behind the Auto Scan popup. Mirrors
tests/test_discovery.py's style/fakes for the older, untouched engine."""

from __future__ import annotations

import threading
import unittest

from cansniff.discovery.model import ScanResult, ScoredCandidate
from cansniff.discovery.scan import scan_bitrate_candidates
from cansniff.discovery.scoring import ScoringConfig
from cansniff.model import CanFrame
from cansniff.socketcan import SocketCanError, SocketCanErrorKind, SocketCanState
from cansniff.sources import PassiveSafetyError, SourceError


def _frame(index, arb_id=0x123):
    return CanFrame(timestamp=float(index), arb_id=arb_id, data=b"\x01\x02",
                    dlc=2, channel="0")


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _FakeLink:
    """Stands in for SocketCanLink -- records every configure()/down()
    call, never touches a real subprocess."""

    def __init__(self, interface="can0", configure_error=None):
        self.interface = interface
        self.calls = []
        self._configure_error = configure_error

    def configure(self, bitrate, listen_only=True):
        self.calls.append(("configure", bitrate, listen_only))
        if self._configure_error is not None:
            raise self._configure_error
        return SocketCanState(exists=True, up=True, listen_only=True, bitrate=bitrate)

    def down(self):
        self.calls.append(("down",))


class _FakeSource:
    """One scripted (delay, frame_or_exception_or_None) list per bitrate --
    settling-period receives are scripted the same way as observation
    receives; the test decides how many of each a scenario needs."""

    def __init__(self, clock, items, cancel_event=None, opens=None, closes=None):
        self.clock = clock
        self.items = list(items)
        self.cancel_event = cancel_event
        self._opens = opens
        self._closes = closes

    def open(self):
        if self._opens is not None:
            self._opens.append(True)

    def receive(self, timeout=0.1):
        if self.items:
            delay, item = self.items.pop(0)
            self.clock.advance(delay)
            if self.cancel_event is not None and self.cancel_event.is_set():
                return None
            if isinstance(item, BaseException):
                raise item
            return item
        self.clock.advance(timeout)
        return None

    def close(self):
        if self._closes is not None:
            self._closes.append(True)


class _Factory:
    def __init__(self, clock, scenarios, cancel_event=None):
        self.clock = clock
        self.scenarios = scenarios
        self.cancel_event = cancel_event
        self.opened_settings = []
        self.opens = []
        self.closes = []

    def __call__(self, settings):
        self.opened_settings.append(dict(settings))
        scenario = self.scenarios.get(settings["bitrate"], [])
        return _FakeSource(self.clock, scenario, self.cancel_event, self.opens, self.closes)


class ScanLifecycleTests(unittest.TestCase):
    def test_only_the_given_candidates_are_scanned(self):
        clock = _Clock()
        factory = _Factory(clock, {})
        link = _FakeLink()
        result = scan_bitrate_candidates(
            "can0", candidates=(125000, 500000), duration=10.0, settle_seconds=0.1,
            link=link, source_factory=factory, clock=clock)
        self.assertEqual(
            sorted(item["bitrate"] for item in factory.opened_settings),
            [125000, 500000])
        self.assertEqual(len(result.candidates), 2)

    def test_duration_is_clamped_up_to_the_minimum(self):
        clock = _Clock()
        factory = _Factory(clock, {})
        link = _FakeLink()
        result = scan_bitrate_candidates(
            "can0", candidates=(500000,), duration=1.0, settle_seconds=0.05,
            link=link, source_factory=factory, clock=clock)
        self.assertEqual(result.candidates[0].requested_duration, 10.0)

    def test_never_selects_or_transmits_and_always_ends_with_the_link_down(self):
        clock = _Clock()
        scenarios = {500000: [(0.1, _frame(0)), (0.1, _frame(1))]}
        factory = _Factory(clock, scenarios)
        link = _FakeLink()
        result = scan_bitrate_candidates(
            "can0", candidates=(500000,), duration=10.0, settle_seconds=0.05,
            link=link, source_factory=factory, clock=clock)
        self.assertIsInstance(result, ScanResult)
        self.assertEqual(link.calls[-1], ("down",))
        for call in link.calls:
            self.assertNotEqual(call[0], "send")

    def test_settling_period_frames_are_discarded_from_scoring(self):
        clock = _Clock()
        # The fake source's clock advances by each scripted item's own delay
        # regardless of the timeout the settle/observation loops request, so
        # the settle-phase item's delay must, by itself, reach past
        # settle_seconds for the settle loop to stop *before* consuming the
        # frames meant to represent real observation evidence.
        scenarios = {
            500000: [
                (0.15, _frame(0, arb_id=0xDEAD)),   # consumed while settling
                (0.01, _frame(1, arb_id=0x111)),
                (0.01, _frame(2, arb_id=0x111)),
            ]
        }
        factory = _Factory(clock, scenarios)
        link = _FakeLink()
        result = scan_bitrate_candidates(
            "can0", candidates=(500000,), duration=10.0, settle_seconds=0.1,
            link=link, source_factory=factory, clock=clock)
        candidate = result.candidates[0]
        self.assertIsNotNone(candidate.components)
        # The 0xDEAD settling-period frame must never appear as evidence.
        self.assertEqual(candidate.components.unique_ids, 1)
        self.assertEqual(candidate.components.total_records, 2)

    def test_stale_frames_never_leak_between_candidates(self):
        clock = _Clock()
        scenarios = {
            125000: [(0.05, _frame(0, arb_id=0xAAA)) for _ in range(5)],
            250000: [(0.05, _frame(0, arb_id=0xBBB)) for _ in range(5)],
        }
        factory = _Factory(clock, scenarios)
        link = _FakeLink()
        result = scan_bitrate_candidates(
            "can0", candidates=(125000, 250000), duration=1.0, settle_seconds=0.01,
            link=link, source_factory=factory, clock=clock)
        by_rate = {c.bitrate: c for c in result.candidates}
        self.assertEqual(by_rate[125000].components.unique_ids, 1)
        self.assertEqual(by_rate[250000].components.unique_ids, 1)
        # A fresh source is opened for every candidate, and each opened
        # source is closed before the loop moves on.
        self.assertEqual(len(factory.opens), 2)
        self.assertEqual(len(factory.closes), 2)

    def test_a_fresh_receiver_is_used_for_every_candidate(self):
        clock = _Clock()
        scenarios = {125000: [], 250000: [], 500000: []}
        factory = _Factory(clock, scenarios)
        link = _FakeLink()
        scan_bitrate_candidates(
            "can0", candidates=(125000, 250000, 500000), duration=1.0,
            settle_seconds=0.01, link=link, source_factory=factory, clock=clock)
        self.assertEqual(len(factory.opened_settings), 3)
        self.assertEqual(len(factory.closes), 3)

    def test_results_are_sorted_by_score_descending_but_none_is_auto_selected(self):
        clock = _Clock()
        scenarios = {
            125000: [(0.01, _frame(i, arb_id=0x10)) for i in range(3)],
            250000: [
                (0.3, _frame(i, arb_id=0x10 + (i % 5)))
                for i in range(20)
            ],
        }
        factory = _Factory(clock, scenarios)
        link = _FakeLink()
        result = scan_bitrate_candidates(
            "can0", candidates=(125000, 250000), duration=10.0, settle_seconds=0.01,
            link=link, source_factory=factory, clock=clock)
        scores = [c.total_score for c in result.candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertFalse(hasattr(result, "selected_bitrate"))

    def test_candidate_with_no_traffic_reports_zero_not_a_win(self):
        clock = _Clock()
        factory = _Factory(clock, {500000: []})
        link = _FakeLink()
        result = scan_bitrate_candidates(
            "can0", candidates=(500000,), duration=1.0, settle_seconds=0.01,
            link=link, source_factory=factory, clock=clock)
        candidate = result.candidates[0]
        self.assertEqual(candidate.total_score, 0.0)
        self.assertTrue(candidate.completed)
        self.assertIn("No frames were observed", candidate.reasons[0])

    def test_cancellation_stops_the_worker_safely_and_leaves_link_down(self):
        clock = _Clock()
        cancel_event = threading.Event()
        scenarios = {
            125000: [(0.01, _frame(i)) for i in range(3)],
            250000: [(0.01, _frame(i)) for i in range(3)],
        }
        factory = _Factory(clock, scenarios, cancel_event=cancel_event)

        progress_events = []

        def progress(item):
            progress_events.append(item)
            if item.stage == "candidate-listening":
                cancel_event.set()

        link = _FakeLink()
        result = scan_bitrate_candidates(
            "can0", candidates=(125000, 250000), duration=10.0, settle_seconds=0.01,
            cancel_event=cancel_event, progress=progress, link=link,
            source_factory=factory, clock=clock)
        self.assertTrue(result.cancelled)
        self.assertEqual(link.calls[-1], ("down",))
        # The second candidate must never have been configured.
        self.assertEqual(len(factory.opened_settings), 1)

    def test_configuration_rejected_records_a_zero_candidate_and_continues(self):
        clock = _Clock()
        link = _FakeLink(configure_error=SocketCanError(
            SocketCanErrorKind.BITRATE_REJECTED, "nope"))
        factory = _Factory(clock, {})
        result = scan_bitrate_candidates(
            "can0", candidates=(125000,), duration=1.0, settle_seconds=0.01,
            link=link, source_factory=factory, clock=clock)
        candidate = result.candidates[0]
        self.assertIsNone(candidate.components)
        self.assertEqual(candidate.total_score, 0.0)
        self.assertFalse(candidate.completed)

    def test_systemic_configuration_failure_aborts_remaining_candidates(self):
        clock = _Clock()
        link = _FakeLink(configure_error=SocketCanError(
            SocketCanErrorKind.PERMISSION_DENIED, "denied"))
        factory = _Factory(clock, {})
        result = scan_bitrate_candidates(
            "can0", candidates=(125000, 250000), duration=1.0, settle_seconds=0.01,
            link=link, source_factory=factory, clock=clock)
        self.assertEqual(result.candidates, ())
        self.assertTrue(result.reasons)
        self.assertEqual(len(factory.opened_settings), 0)

    def test_passive_safety_error_is_reported_not_raised(self):
        clock = _Clock()

        class _RefusingSource(_FakeSource):
            def open(self):
                raise PassiveSafetyError("listen-only not confirmed")

        def factory(settings):
            return _RefusingSource(clock, [])

        link = _FakeLink()
        result = scan_bitrate_candidates(
            "can0", candidates=(500000,), duration=1.0, settle_seconds=0.01,
            link=link, source_factory=factory, clock=clock)
        candidate = result.candidates[0]
        self.assertIsNone(candidate.components)
        self.assertIn("listen-only", candidate.reasons[0].lower())

    def test_source_error_on_open_is_reported_not_raised(self):
        clock = _Clock()

        def factory(settings):
            class _Broken(_FakeSource):
                def open(self):
                    raise SourceError("could not open bus")
            return _Broken(clock, [])

        link = _FakeLink()
        result = scan_bitrate_candidates(
            "can0", candidates=(500000,), duration=1.0, settle_seconds=0.01,
            link=link, source_factory=factory, clock=clock)
        self.assertIsNone(result.candidates[0].components)

    def test_progress_reports_current_bitrate_and_candidate_counts(self):
        clock = _Clock()
        scenarios = {125000: [(0.01, _frame(0))], 250000: [(0.01, _frame(0))]}
        factory = _Factory(clock, scenarios)
        link = _FakeLink()
        events = []
        scan_bitrate_candidates(
            "can0", candidates=(125000, 250000), duration=1.0, settle_seconds=0.01,
            progress=events.append, link=link, source_factory=factory, clock=clock)
        stages = [event.stage for event in events]
        self.assertIn("candidate-start", stages)
        self.assertIn("candidate-listening", stages)
        self.assertIn("candidate-complete", stages)
        complete_events = [e for e in events if e.stage == "candidate-complete"]
        self.assertEqual([e.completed for e in complete_events], [1, 2])
        self.assertEqual([e.total for e in complete_events], [2, 2])

    def test_scoring_config_is_threaded_through_to_the_scorer(self):
        clock = _Clock()
        extended_frame = CanFrame(timestamp=0.0, arb_id=0x18DA10F1, data=b"\x01\x02",
                                  dlc=2, is_extended=True, channel="0")
        # A settle-consuming dummy frame first -- see the settling-period
        # test above for why its delay alone must clear settle_seconds.
        scenarios = {500000: [(0.15, _frame(0)), (0.01, extended_frame)]}
        factory = _Factory(clock, scenarios)
        link = _FakeLink()
        config = ScoringConfig(expected_format="extended")
        result = scan_bitrate_candidates(
            "can0", candidates=(500000,), duration=1.0, settle_seconds=0.1,
            scoring_config=config, link=link, source_factory=factory, clock=clock)
        self.assertEqual(result.candidates[0].components.format_score, 10.0)

    def test_no_candidates_selected_returns_empty_result_without_touching_the_link(self):
        clock = _Clock()
        link = _FakeLink()
        factory = _Factory(clock, {})
        result = scan_bitrate_candidates(
            "can0", candidates=(), duration=30.0, settle_seconds=0.1,
            link=link, source_factory=factory, clock=clock)
        self.assertEqual(result.candidates, ())
        self.assertEqual(link.calls, [])


if __name__ == "__main__":
    unittest.main()
