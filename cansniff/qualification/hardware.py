"""Explicitly opted-in hardware qualification through the production live path."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict

from ..sources.live import LiveSource
from .model import (
    EvidenceReference, QualificationLevel, QualificationRecord,
    QualificationStatus, collect_environment,
)
from .runner import current_commit


EXECUTE_CONFIRMATION = "PASSIVE_HARDWARE_QUALIFICATION"


def qualify(settings: Dict[str, Any], duration: float, execute: bool = False,
            confirmation: str = "", metadata: Dict[str, str] = None) -> QualificationRecord:
    """Inspect by default; receive for a bounded period only after explicit consent."""
    if isinstance(duration, bool) or not math.isfinite(float(duration)) \
            or not 0 <= float(duration) <= 86400:
        raise ValueError("duration must be finite and between 0 and 86400 seconds")
    duration = float(duration)
    source = LiveSource(settings)
    plan = source.qualification_plan()
    metadata = dict(metadata or {})
    physical = source.interface not in ("virtual", "udp_multicast")
    now = datetime.now(timezone.utc).isoformat()
    common = dict(
        record_id="hardware-{}-{}".format(source.interface, int(time.time())),
        capability="live.backend.{}".format(source.interface),
        test_id="passive-receive-harness", timestamp=now,
        environment=collect_environment(current_commit()), backend=source.interface,
        adapter=metadata.get("adapter", ""),
        hardware_revision=metadata.get("hardware_revision", ""),
        firmware_version=metadata.get("firmware_version", ""),
        driver_version=metadata.get("driver_version", ""), channel=source.channel,
        bitrate=source.bitrate, fd=source.fd, duration_seconds=duration,
        evidence=(EvidenceReference("passive-plan", json.dumps(plan, sort_keys=True),
                                    notes="Configuration is rechecked by LiveSource.open"),),
    )
    if not execute:
        return QualificationRecord(
            level=QualificationLevel.UNTESTED, status=QualificationStatus.SKIPPED,
            frame_count=0,
            limitations=("Dry run only: no CAN interface was opened.",), **common)
    if confirmation != EXECUTE_CONFIRMATION:
        return QualificationRecord(
            level=QualificationLevel.UNTESTED, status=QualificationStatus.REFUSED,
            frame_count=0,
            limitations=("Execution refused: exact confirmation token was absent.",),
            **common)
    frames, opened, error = 0, False, ""
    try:
        source.open()
        opened = True
        deadline = time.monotonic() + max(0.0, float(duration))
        while time.monotonic() < deadline:
            if source.receive(timeout=min(0.1, max(0.0, deadline - time.monotonic()))) is not None:
                frames += 1
    except Exception as exc:
        error = "{}: {}".format(type(exc).__name__, exc)
    finally:
        source.close()
    level = (QualificationLevel.HARDWARE_TESTED if physical
             else QualificationLevel.SOFTWARE_TESTED)
    if not opened:
        level = QualificationLevel.UNTESTED
    provenance = {
        "can_mode": "CAN FD" if source.fd else "Classic CAN",
        "frame_rate_hz": "{:.6g}".format(frames / duration) if duration else "unavailable",
        "observed_error_conditions": error or "none observed by harness",
        "passive_verification_method": metadata.get(
            "passive_verification_method",
            "production passive policy; electrical verification not performed"),
    }
    limitations = (
        "Independent electrical passivity verification was not performed; "
        "this record must not be promoted to ELECTRICALLY_PASSIVE_VERIFIED.",
    ) if physical else ("The selected interface is nonphysical virtual transport.",)
    if error:
        limitations = limitations + ("Hardware run failed: {}".format(error),)
    return QualificationRecord(
        level=level,
        status=QualificationStatus.FAIL if error else QualificationStatus.PASS,
        frame_count=frames, limitations=limitations,
        provenance=tuple(sorted(provenance.items())), **common)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Opt-in passive hardware receive qualification (dry-run by default)")
    parser.add_argument("--interface", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--bitrate", type=int, required=True)
    parser.add_argument("--data-bitrate", type=int, default=2000000)
    parser.add_argument("--fd", action="store_true")
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--adapter", default="")
    parser.add_argument("--hardware-revision", default="")
    parser.add_argument("--firmware-version", default="")
    parser.add_argument("--driver-version", default="")
    parser.add_argument("--passive-verification-method", default=(
        "production passive policy; electrical verification not performed"))
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    settings = {"interface": args.interface, "channel": args.channel,
                "bitrate": args.bitrate, "data_bitrate": args.data_bitrate,
                "fd": args.fd}
    plan = LiveSource(settings).qualification_plan()
    print("Intended passive configuration:\n{}".format(
        json.dumps(plan, indent=2, sort_keys=True)), file=sys.stderr)
    record = qualify(
        settings,
        args.duration, args.execute, args.confirm,
        {name: getattr(args, name) for name in (
            "adapter", "hardware_revision", "firmware_version", "driver_version",
            "passive_verification_method")})
    payload = json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload)
    else:
        print(payload, end="")
    return 0 if record.status in (QualificationStatus.PASS,
                                  QualificationStatus.SKIPPED) else 2


if __name__ == "__main__":
    raise SystemExit(main())
