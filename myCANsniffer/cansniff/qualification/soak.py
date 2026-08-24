"""Small lifecycle loop reusable by opt-in long-running qualification jobs."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from ..analysis.profile import TrafficProfileAccumulator
from ..analysis.protocols.survey import build_protocol_survey
from ..analysis.store import FrameStore
from ..model import CanFrame
from .model import collect_environment
from .runner import current_commit


EXECUTE_CONFIRMATION = "RUN_QUALIFICATION_SOAK"


def run_soak(cycles: int, frames_per_cycle: int = 100):
    cycles, frames_per_cycle = max(0, int(cycles)), max(1, int(frames_per_cycle))
    completed = 0
    for cycle in range(cycles):
        frames = [CanFrame(cycle + index / 1000.0, 0x500,
                           bytes((index & 0xff,)), 1, False)
                  for index in range(frames_per_cycle)]
        store, profile = FrameStore(max_frames=max(100, frames_per_cycle)), TrafficProfileAccumulator()
        store.add(frames); profile.update(frames)
        build_protocol_survey(store.all_frames("soak"), profile.snapshot(store))
        store.clear(); profile.reset(); completed += 1
    return {"timestamp": datetime.now(timezone.utc).isoformat(),
            "environment": collect_environment(current_commit()).to_dict(),
            "cycles_requested": cycles, "cycles_completed": completed,
            "frames_per_cycle": frames_per_cycle, "cleanup": "completed"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Opt-in analysis lifecycle soak")
    parser.add_argument("--cycles", type=int, default=1000)
    parser.add_argument("--frames-per-cycle", type=int, default=100)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"status": "DRY_RUN", "planned_cycles": args.cycles}, indent=2))
        return 0
    if args.confirm != EXECUTE_CONFIRMATION:
        print(json.dumps({"status": "REFUSED", "required_confirmation":
                          EXECUTE_CONFIRMATION}, indent=2)); return 2
    payload = json.dumps(run_soak(args.cycles, args.frames_per_cycle), indent=2) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload)
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
