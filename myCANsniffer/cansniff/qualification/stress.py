"""Opt-in bounded-memory synthetic stress measurement; dry-run by default."""

from __future__ import annotations

import argparse
import json
import time
import tracemalloc
from datetime import datetime, timezone

from ..analysis.profile import TrafficProfileAccumulator
from ..analysis.protocols.survey import build_protocol_survey
from ..analysis.compare import ComparisonCache, ComparisonInput, ComparisonWindow
from ..analysis.matching import ProfileMatchCache
from ..analysis.signals import Profile, ProfileStore, Signal
from ..analysis.store import DEFAULT_MAX_FRAMES, FrameStore
from ..model import CanFrame
from .model import collect_environment
from .runner import current_commit


EXECUTE_CONFIRMATION = "RUN_QUALIFICATION_STRESS"
PRESETS = {"200k": 200_000, "1m": 1_000_000, "5m": 5_000_000}


def run_stress(frame_count: int, retained: int = DEFAULT_MAX_FRAMES,
               include_analysis: bool = True):
    frame_count, retained = max(0, int(frame_count)), max(100, int(retained))
    store, profile = FrameStore(retained), TrafficProfileAccumulator()
    tracemalloc.start()
    begin = time.perf_counter()
    batch = []
    for index in range(frame_count):
        batch.append(CanFrame(index / 1000.0, 0x100 + index % 64,
                              bytes((index & 0xff,)), 1, False))
        if len(batch) == 4096:
            store.add(batch); profile.update(batch); batch = []
    if batch:
        store.add(batch); profile.update(batch)
    elapsed = time.perf_counter() - begin
    snapshot_started = time.perf_counter()
    snapshot = profile.snapshot(store)
    snapshot_seconds = time.perf_counter() - snapshot_started
    analysis = {}
    if include_analysis and len(store):
        started = time.perf_counter()
        survey = build_protocol_survey(store.all_frames("stress"), snapshot)
        analysis["protocol_survey_seconds"] = time.perf_counter() - started
        analysis["protocol_levels"] = {
            item.protocol.value: item.level.value for item in survey.results}

        first, last = store.time_span()
        midpoint = (first + last) / 2.0
        comparison_input = ComparisonInput(
            ComparisonWindow("Baseline", first, midpoint, store.revision),
            ComparisonWindow("Event", midpoint, last, store.revision))
        started = time.perf_counter()
        comparison = ComparisonCache().build(store, comparison_input, snapshot)
        analysis["compare_seconds"] = time.perf_counter() - started
        analysis["compare_messages"] = len(comparison.messages)

        profiles = ProfileStore([
            Profile("Synthetic {:02d}".format(index), signals=[
                Signal("Value", can_id=0x100 + index, start=0, length=8,
                       message_name="Message {:02d}".format(index), message_length=1)])
            for index in range(64)])
        started = time.perf_counter()
        matches = ProfileMatchCache().build(snapshot, profiles, survey)
        analysis["profile_matching_seconds"] = time.perf_counter() - started
        analysis["profile_candidates"] = len(matches.candidates)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"timestamp": datetime.now(timezone.utc).isoformat(),
            "environment": collect_environment(current_commit()).to_dict(),
            "frames": frame_count, "retained": len(store),
            "bounded_to": store.max_frames, "processed": snapshot.processed_frames,
            "ingest_seconds": elapsed,
            "profile_snapshot_seconds": snapshot_seconds,
            "frames_per_second": frame_count / elapsed if elapsed else 0,
            "analysis": analysis,
            "traced_current_bytes": current, "traced_peak_bytes": peak}


def main() -> int:
    parser = argparse.ArgumentParser(description="Opt-in synthetic stress harness")
    parser.add_argument("--preset", choices=tuple(PRESETS), default="200k")
    parser.add_argument("--retained", type=int, default=DEFAULT_MAX_FRAMES)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--ingest-only", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"status": "DRY_RUN", "planned_frames": PRESETS[args.preset],
                          "retained": args.retained}, indent=2))
        return 0
    if args.confirm != EXECUTE_CONFIRMATION:
        print(json.dumps({"status": "REFUSED", "required_confirmation":
                          EXECUTE_CONFIRMATION}, indent=2))
        return 2
    payload = json.dumps(run_stress(PRESETS[args.preset], args.retained,
                                    not args.ingest_only), indent=2) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload)
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
