"""Explicitly record a completed ordinary-suite run as software evidence."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone

from .model import (
    EvidenceReference, QualificationLevel, QualificationRecord,
    QualificationStatus, collect_environment,
)
from .runner import current_commit


SUITE_CAPABILITIES = (
    "repository.no_transmit_guard", "qualification.framework",
    "backend.socketcan.enumeration", "backend.socketcan.passive_policy",
    "backend.socketcan.passive_bitrate_discovery",
    "backend.virtual.passive_open", "backend.virtual.passive_policy",
    "backend.virtual.classic_capture", "capture.timestamps",
    "capture.esi_brs", "capture.error_frames", "capture.shutdown_restart",
    "protocol.canopen.detection", "protocol.canopen.sdo_pdo",
    "protocol.j1939.detection", "protocol.j1939.bam",
    "protocol.j1939.rts_cts", "protocol.isotp.reassembly",
    "protocol.uds.conversations", "definition.dbc", "definition.eds",
    "definition.dcf", "definition.j1939_json", "profile.matching",
    "investigation.projects_reports",
)


def records_for_passing_suite(test_count: int, duration: float,
                              command: str):
    if (test_count <= 0 or duration < 0 or not math.isfinite(duration)
            or not command.strip()):
        raise ValueError("passing suite evidence requires count, duration, and command")
    timestamp = datetime.now(timezone.utc).isoformat()
    environment = collect_environment(current_commit())
    note = "{} tests passed in {:.3f}s".format(test_count, duration)
    evidence = (EvidenceReference("ordinary-unittest-suite", command,
                                  notes=note),)
    return tuple(QualificationRecord(
        "ordinary-suite-{}".format(capability.replace(".", "-")), capability,
        QualificationLevel.SOFTWARE_TESTED, QualificationStatus.PASS,
        "ordinary-full-unittest-discovery", timestamp, environment,
        duration_seconds=duration, evidence=evidence,
        limitations=("Software/synthetic/mocked evidence only; no real capture or "
                     "physical adapter qualification is implied.",),
        provenance=(("test_count", str(test_count)),))
        for capability in SUITE_CAPABILITIES)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record an explicitly completed passing ordinary suite")
    parser.add_argument("--passed", action="store_true", required=True)
    parser.add_argument("--tests", type=int, required=True)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    records = records_for_passing_suite(args.tests, args.seconds, args.command)
    payload = {"schema_version": 1,
               "source": "explicit completed ordinary suite",
               "records": [item.to_dict() for item in records]}
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
