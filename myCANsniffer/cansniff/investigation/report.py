"""Deterministic factual Markdown reports; never a raw-frame export."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from typing import Any, Optional

from .. import __version__
from .model import InvestigationProject


@dataclass(frozen=True)
class ReportContext:
    generated_at: str
    traffic_profile: Any = None
    protocol_survey: Any = None
    comparison: Any = None
    capture_verification: Any = None
    comparison_issues: tuple = ()
    profile_match: Any = None
    j1939_definitions: tuple = ()
    j1939_decoded: tuple = ()


def _safe(value: Any) -> str:
    text = html.escape(str(value).replace("\x00", ""), quote=False)
    text = text.replace("\r", " ").replace("\n", " ").replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]"):
        text = text.replace(character, "\\" + character)
    return text


def generate_markdown_report(project: InvestigationProject,
                             context: ReportContext) -> str:
    capture = project.active_capture
    lines = ["# CAN Network Investigation", "", "## Project", "",
             "- Title: {}".format(_safe(project.title)),
             "- Project ID: `{}`".format(project.project_id),
             "- Project schema: {}".format(project.schema_version),
             "- Application version: {}".format(__version__),
             "- Generated: {}".format(_safe(context.generated_at)),
             "- Analysis versions: {}".format(
                 ", ".join("{}={}".format(k, v)
                           for k, v in project.analysis_versions) or "not recorded"), ""]
    lines.extend(["## Observed — Capture Evidence", ""])
    if capture is None:
        lines.append("No capture is attached. Investigation context remains available.")
    else:
        lines.extend(["- Capture: {}".format(_safe(capture.display_name)),
                      "- SHA-256: `{}`".format(capture.content_hash or "unknown"),
                      "- Size: {} bytes".format(capture.size),
                      "- Format: {}".format(_safe(capture.format or "unknown"))])
        if context.capture_verification is not None:
            lines.append("- Availability: {} — {}".format(
                _safe(context.capture_verification.status.value),
                _safe(context.capture_verification.explanation)))
    traffic = context.traffic_profile
    if traffic is not None:
        horizon = (getattr(traffic, "retained_horizon", None) or
                   getattr(traffic, "horizon", None))
        if horizon is not None:
            lines.extend(["- Retained frames: {}".format(horizon.frame_count),
                          "- Retained duration: {:.6f} s".format(horizon.duration),
                          "- Horizon complete: {}".format(horizon.complete)])
        integrity = getattr(traffic, "integrity", None)
        if integrity is not None:
            caveats = (getattr(integrity, "reasons", ()) or
                       getattr(integrity, "caveats", ()) or ())
            lines.append("- Integrity caveats: {}".format(
                "; ".join(_safe(item) for item in caveats) or "none reported"))
    lines.extend(["", "## Inferred — Reproducible Analysis", ""])
    survey = context.protocol_survey
    if survey is None:
        lines.append("Protocol Survey was not supplied; it should be recomputed from the capture.")
    else:
        lines.append("### Protocol Survey")
        for result in getattr(survey, "results", ()):
            reason = "; ".join(result.reasons)
            caveats = getattr(result, "caveats", ()) or ()
            if caveats:
                reason += ("; " if reason else "") + "caveats: " + "; ".join(caveats)
            lines.append("- {} — {}: {}".format(
                _safe(result.protocol.value), _safe(result.level.value),
                _safe(reason)))
        sources = getattr(survey, "j1939_sources", ())
        sessions = getattr(survey, "j1939_transport_sessions", ())
        payloads = getattr(survey, "j1939_payloads", ())
        if sources or sessions or payloads:
            lines.append("\n### J1939 Passive Intelligence")
            observed_pgns = sorted({pgn for source in sources for pgn in source.pgns})
            lines.append("- Observed: {} source address(es); numeric PGNs {}".format(
                len(sources), _safe(", ".join(str(value) for value in observed_pgns)
                                    or "none")))
            complete = sum(getattr(item, "complete", False) for item in sessions)
            lines.append(
                "- Inferred transport: {} complete; {} incomplete/malformed/orphan; "
                "{} normalized application payload(s)".format(
                    complete, len(sessions) - complete, len(payloads)))
            for session in sessions[:20]:
                lines.append(
                    "- Inferred session {}: {} PGN {}; {} bytes; sequence {}; {}"
                    .format(_safe(session.session_id),
                            _safe(session.transport_kind.value),
                            session.transported_pgn, len(session.payload),
                            _safe(session.sequence_status),
                            _safe(session.status.value)))
            if len(sessions) > 20:
                lines.append("- {} additional transport sessions omitted from this "
                             "compact report.".format(len(sessions) - 20))
        diagnostics = getattr(survey, "diagnostic_analysis", None)
        if diagnostics is not None:
            lines.append("\n### ISO-TP / UDS Diagnostics")
            endpoints = sorted({
                item.endpoint.identity for item in diagnostics.transfers})
            complete = sum(item.complete for item in diagnostics.transfers)
            lines.append(
                "- Observed: {} endpoint(s); {} complete and {} incomplete ISO-TP "
                "transfer(s); raw payload/frame provenance retained.".format(
                    len(endpoints), complete,
                    len(diagnostics.transfers) - complete))
            services = {}
            for event in diagnostics.events:
                services[event.uds.service] = event.uds.service_name
            lines.append("- Observed services: {}".format(_safe(
                ", ".join("0x{:02X} {}".format(service, name)
                          for service, name in sorted(services.items()))
                or "none")))
            negative = [event for event in diagnostics.events
                        if event.uds.is_negative]
            lines.append("- Observed negative responses: {}".format(
                len(negative)))
            for event in negative[:20]:
                lines.append(
                    "- Observed NRC: service 0x{:02X} {} -> NRC 0x{:02X} {} "
                    "at {:.6f} s.".format(
                        event.uds.service or 0,
                        _safe(event.uds.service_name or "unnamed"),
                        event.uds.nrc or 0,
                        _safe(event.uds.nrc_text or "unknown standard name"),
                        event.transfer.first_timestamp))
            for item in diagnostics.dids[:30]:
                lines.append(
                    "- Observed DID 0x{:04X}: raw value `{}`; observation `{}`."
                    .format(item.did, _safe(" ".join(
                        "{:02X}".format(value) for value in item.raw_value)
                        or "not safely separable"), _safe(item.observation_id)))
            for item in diagnostics.dtcs[:30]:
                lines.append(
                    "- Observed DTC 0x{:06X}: status 0x{:02X}; ReadDTCInformation "
                    "subfunction 0x{:02X}; observation `{}`.".format(
                        item.dtc, item.status, item.sub_function,
                        _safe(item.observation_id)))
            status_counts = {}
            for item in diagnostics.conversations:
                status_counts[item.correlation_status.value] = (
                    status_counts.get(item.correlation_status.value, 0) + 1)
            lines.append("- Inferred request/response chronology: {}".format(_safe(
                "; ".join("{}={}".format(name, count)
                          for name, count in sorted(status_counts.items()))
                or "none")))
            latencies = [item.latency for item in diagnostics.conversations
                         if item.latency is not None]
            if latencies:
                lines.append(
                    "- Inferred paired latency range: {:.3f} to {:.3f} ms."
                    .format(min(latencies) * 1000, max(latencies) * 1000))
            lines.append(
                "- Defined DID/DTC semantics: none; numeric identifiers and raw "
                "values are reported without OEM meaning.")
            caveats = tuple(getattr(diagnostics, "caveats", ()) or ())
            if caveats:
                lines.append("- Diagnostics limitations: {}".format(
                    _safe("; ".join(caveats))))
    comparison = context.comparison
    if comparison is None:
        if project.comparisons:
            lines.append("\nComparison inputs are saved; derived ranking was not embedded.")
    else:
        lines.append("\n### Baseline/Event Comparison")
        for item in getattr(comparison, "messages", ())[:20]:
            score = getattr(item, "ranking_score", getattr(item, "score", 0.0))
            lines.append("- {} — score {:.3f}: {}".format(
                _safe(item.key), score, _safe("; ".join(item.reasons))))
        caveats = getattr(comparison, "caveats", ()) or ()
        if caveats:
            lines.append("- Comparison caveats: {}".format(
                _safe("; ".join(caveats))))
    for issue in context.comparison_issues:
        lines.append("- Reproducibility caveat: {}".format(_safe(issue)))
    match = context.profile_match
    if match is not None:
        lines.append("\n### Profile Matching Suggestion")
        candidates = getattr(match, "candidates", ())
        if not candidates:
            lines.append("- No local profile candidates were available.")
        else:
            candidate = candidates[0]
            coverage = candidate.coverage
            lines.extend([
                "- Suggested structural candidate (inferred, not selected): {} — {}"
                .format(_safe(candidate.display_name), _safe(candidate.level.value)),
                "- Algorithm: {}".format(_safe(match.algorithm_version)),
                "- Coverage: observed IDs {:.1%}; defined IDs {:.1%}; "
                "weighted frames {:.1%}; balanced frames {:.1%}; structural {:.1%}"
                .format(coverage.observed_key_coverage,
                        coverage.definition_key_coverage,
                        coverage.weighted_frame_coverage,
                        coverage.balanced_frame_coverage,
                        coverage.structural_compatibility),
                "- Provenance: {}".format(_safe("; ".join(
                    "{} {} SHA-256 {}".format(
                        item.kind, item.state, item.content_hash or "unknown")
                    for item in candidate.provenance) or "none")),
                "- Explicit conflicts: {}".format(_safe("; ".join(
                    item.explanation for item in candidate.conflicts) or "none")),
            ])
        for caveat in getattr(match, "caveats", ()):
            lines.append("- Matching caveat: {}".format(_safe(caveat)))
    lines.extend(["", "## Defined — Imported Interpretation", ""])
    snapshot = project.profile_snapshot
    if snapshot is None:
        lines.append("No project-local profile snapshot is attached.")
    else:
        lines.append("- Profile: {}".format(_safe(snapshot.get("name", "unnamed"))))
        lines.append("- Manual/DBC signals: {}".format(len(snapshot.get("signals", []))))
        for reference in snapshot.get("definitions", []):
            source = reference.get("source", {}) if isinstance(reference, dict) else {}
            lines.append("- {} — {} — SHA-256 `{}`".format(
                _safe(source.get("kind", "definition")),
                _safe(source.get("display_name", "unnamed")),
                _safe(source.get("content_hash", "unknown"))))
    if context.j1939_definitions:
        lines.append("\n### J1939 Definition Provenance")
        for definition in context.j1939_definitions:
            lines.append(
                "- {} - SHA-256 `{}`; schema {}; {} PGNs; validation {}; license {}"
                .format(_safe(definition.source.display_name),
                        _safe(definition.source.content_hash),
                        definition.schema_version, len(definition.pgns),
                        _safe(definition.validation_state.value),
                        _safe(definition.license_text or "not declared")))
    if context.j1939_decoded:
        lines.append("\n### J1939 Decoded Values")
        emitted = 0
        for message in context.j1939_decoded:
            if not message.values:
                lines.append("- PGN {} remains numeric/unknown; {} via {}".format(
                    message.pgn, len(message.payload), _safe(message.transport)))
                emitted += 1
            for value in message.values:
                lines.append(
                    "- PGN {} {} / SPN {} {}: raw {}; value {} {}; status {}; "
                    "source SHA-256 `{}`".format(
                        message.pgn, _safe(message.pgn_name), value.spn,
                        _safe(value.name), value.raw if value.raw is not None else "n/a",
                        _safe(value.display_value), _safe(value.unit),
                        _safe(value.status.value),
                        _safe(value.source.content_hash)))
                emitted += 1
                if emitted >= 100:
                    break
            if emitted >= 100:
                break
        if emitted >= 100:
            lines.append("- Further decoded rows omitted from this compact report.")
    lines.append("\n### User Profile-Matching Decisions")
    if not project.profile_match_decisions:
        lines.append("No profile-matching suggestions were accepted by the user.")
    for decision in project.profile_match_decisions:
        capture_state = ("current capture" if capture is not None
                         and capture.content_hash == decision.capture_hash
                         else "different or unavailable capture")
        detail = ""
        if decision.definition_hash:
            detail = "; definition SHA-256 {}; node {}; channel {}".format(
                decision.definition_hash, decision.node_id,
                _safe(decision.channel or "any"))
        lines.append(
            "- User decision: {} profile {} (`{}`); {}; algorithm {}; "
            "candidate set `{}`{}"
            .format(_safe(decision.action.value), _safe(decision.profile_name),
                    decision.profile_id, capture_state,
                    _safe(decision.algorithm_version or "unknown"),
                    _safe(decision.candidate_set_identity or "unknown"), detail))
    lines.extend(["", "## User-Annotated — Investigator Assertions", ""])
    if not project.annotations:
        lines.append("No user annotations.")
    for item in sorted(project.annotations,
                       key=lambda value: (value.start_timestamp, value.annotation_id)):
        span = ("{:.6f} s".format(item.start_timestamp) if item.end_timestamp is None
                else "{:.6f}–{:.6f} s".format(item.start_timestamp,
                                               item.end_timestamp))
        lines.append("- {} — {} _(User annotation)_".format(span, _safe(item.text)))
    lines.extend(["", "## Bookmarks", ""])
    if not project.bookmarks:
        lines.append("No bookmarks.")
    for item in project.bookmarks:
        lines.append("- {} — {} — `{}`".format(
            _safe(item.label), item.kind.value,
            _safe(json.dumps(dict(item.target), sort_keys=True))))
    lines.extend(["", "## Limitations", "",
                  "- This report summarizes selected evidence; it is not a raw-frame export.",
                  "- Derived analysis may change under newer algorithm versions.",
                  "- Project files reference external captures; they do not embed raw evidence.",
                  "- Broad vendor EDS/DCF interoperability remains unverified.", ""])
    return "\n".join(lines)


__all__ = ["ReportContext", "generate_markdown_report"]
