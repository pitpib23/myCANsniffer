"""Evidence-aware, deterministic Markdown report generation."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

from cansniff.investigation import (
    Annotation, Bookmark, BookmarkKind, CaptureReference,
    ComparisonDefinition, ProfileMatchAction, ProfileMatchDecision,
    ReportContext, generate_markdown_report, new_project,
)
from cansniff.investigation.model import new_id
from cansniff.analysis.matching import ProfileMatchCache
from cansniff.analysis.profile import TrafficProfileAccumulator
from cansniff.analysis.signals import Profile, ProfileStore, Signal
from cansniff.analysis.store import FrameStore
from cansniff.model import CanFrame
from cansniff.analysis.j1939_definitions import (
    decode_j1939_payload, parse_j1939_definition_file,
)
from cansniff.analysis.protocols.j1939_transport import J1939PayloadObservation
from cansniff.analysis.diagnostics import (
    analyze_diagnostic_transfers, normalize_single_frame,
)


class ReportTests(unittest.TestCase):
    def test_diagnostics_report_separates_observed_and_inferred_facts(self):
        def item(t, arb, payload, ordinal=0):
            raw = bytes((len(payload),)) + bytes(payload)
            frame = CanFrame(t, arb, raw, len(raw), channel="can0")
            return normalize_single_frame(frame, bytes(payload), ordinal)
        diagnostics = analyze_diagnostic_transfers((
            item(1, 0x7E0, [0x22, 0xF1, 0x90]),
            item(1.1, 0x7E8, [0x62, 0xF1, 0x90, 1, 2]),
            item(2, 0x7E0, [0x27, 1], 1),
            item(2.1, 0x7E8, [0x7F, 0x27, 0x33], 1),
        ), common_caveats=("driver visibility unavailable",))
        survey = SimpleNamespace(
            results=(), j1939_sources=(), j1939_transport_sessions=(),
            j1939_payloads=(), diagnostic_analysis=diagnostics)
        report = generate_markdown_report(
            new_project(now="now"),
            ReportContext("fixed", protocol_survey=survey))
        self.assertIn("ISO-TP / UDS Diagnostics", report)
        self.assertIn("Observed DID 0xF190", report)
        self.assertIn("Observed NRC: service 0x27", report)
        self.assertIn("Inferred request/response chronology", report)
        self.assertIn("Defined DID/DTC semantics: none", report)
        self.assertIn("driver visibility unavailable", report)

    def test_j1939_report_separates_observed_inferred_and_defined_values(self):
        fixture = os.path.join(os.path.dirname(__file__), "fixtures",
                               "synthetic_j1939.json")
        definition = parse_j1939_definition_file(fixture)
        payload = J1939PayloadObservation(
            "can0", 61184, 0x31, None, 6,
            bytes([1, 0xF4, 1, 0x78, 0x56, 0x34, 0x12, 0xFF]),
            "BAM", True, 1.0, 1.2, ())
        decoded = decode_j1939_payload(payload, (definition,))
        session = SimpleNamespace(
            complete=True, session_id="session-1",
            transport_kind=SimpleNamespace(value="BAM"),
            transported_pgn=61184, payload=payload.payload,
            sequence_status="COMPLETE",
            status=SimpleNamespace(value="Complete"))
        survey = SimpleNamespace(
            results=(),
            j1939_sources=(SimpleNamespace(pgns=(61184,)),),
            j1939_transport_sessions=(session,),
            j1939_payloads=(payload,))
        report = generate_markdown_report(
            new_project(now="now"), ReportContext(
                "fixed", protocol_survey=survey,
                j1939_definitions=(definition,), j1939_decoded=(decoded,)))
        self.assertIn("J1939 Passive Intelligence", report)
        self.assertIn("Inferred session session-1", report)
        self.assertIn("J1939 Definition Provenance", report)
        self.assertIn(definition.source.content_hash, report)
        self.assertIn("SPN 1001 Synthetic Temperature", report)
        self.assertIn("raw 500", report)

    def test_report_separates_provenance_categories_and_omits_paths(self):
        project = new_project("Machine <A>", "now")
        capture = CaptureReference(new_id(), "C:/secret/machine.asc", "machine.asc", "asc",
                                   "a" * 64, 100, "now")
        annotation = Annotation(new_id(), capture.capture_id, 12.413, None,
                                "brake <pressed>", "a", "a")
        bookmark = Bookmark(new_id(), BookmarkKind.MESSAGE, "State", capture.capture_id,
                            (("message_key", "can0:201:S"),), "now")
        project = project.changed(captures=(capture,), active_capture_id=capture.capture_id,
                                  annotations=(annotation,), bookmarks=(bookmark,))
        report = generate_markdown_report(project, ReportContext("fixed"))
        for heading in ("Observed", "Inferred", "Defined", "User-Annotated"):
            self.assertIn(heading, report)
        self.assertIn("User annotation", report)
        self.assertIn("machine.asc", report)
        self.assertNotIn("C:/secret", report)
        self.assertIn("&lt;pressed&gt;", report)

    def test_report_is_deterministic_for_fixed_context(self):
        project = new_project("Case", "now")
        context = ReportContext("fixed")
        self.assertEqual(generate_markdown_report(project, context),
                         generate_markdown_report(project, context))

    def test_report_does_not_dump_raw_frames_or_fake_semantics(self):
        report = generate_markdown_report(new_project(now="now"), ReportContext("fixed"))
        self.assertIn("not a raw-frame export", report)
        self.assertNotIn("Motor started", report)

    def test_report_includes_saved_input_reproducibility_caveats(self):
        report = generate_markdown_report(
            new_project(now="now"),
            ReportContext("fixed", comparison_issues=("bounds outside capture",)))
        self.assertIn("Reproducibility caveat: bounds outside capture", report)

    def test_user_markdown_and_html_are_rendered_as_inert_text(self):
        project = new_project("<script>alert(1)</script>", "now")
        capture = CaptureReference(new_id(), "x.asc", "x.asc", "asc",
                                   "a" * 64, 1, "now")
        annotation = Annotation(new_id(), capture.capture_id, 1, None,
                                "[open](javascript:alert(1)) <img src=x>",
                                "now", "now")
        project = project.changed(captures=(capture,), active_capture_id=capture.capture_id,
                                  annotations=(annotation,))
        report = generate_markdown_report(project, ReportContext("fixed"))
        self.assertNotIn("<script>", report)
        self.assertNotIn("[open](javascript:", report)
        self.assertIn("\\[open\\]", report)

    def test_suggestion_and_user_decision_have_distinct_provenance(self):
        profile = Profile("Suggested")
        profile.signals.append(Signal(
            "Value", can_id=0x123, start=0, length=8,
            message_name="Message", message_length=8))
        frame = CanFrame(0.0, 0x123, b"\0" * 8, 8, channel="can0")
        accumulator, store = TrafficProfileAccumulator(), FrameStore(10)
        accumulator.update((frame,))
        store.add((frame,))
        match = ProfileMatchCache().build(
            accumulator.snapshot(store), ProfileStore([profile]),
            capture_identity="a" * 64)
        decision = ProfileMatchDecision(
            new_id(), ProfileMatchAction.USE_PROFILE, profile.profile_id,
            profile.name, "a" * 64, match.candidate_set_identity,
            match.algorithm_version, "now")
        capture = CaptureReference(new_id(), "capture.asc", "capture.asc", "asc",
                                   "a" * 64, 8, "now")
        project = new_project(now="now").changed(
            captures=(capture,), active_capture_id=capture.capture_id,
            profile_match_decisions=(decision,))
        report = generate_markdown_report(
            project, ReportContext("fixed", profile_match=match))
        self.assertIn("inferred, not selected", report)
        self.assertIn("User decision: USE\_PROFILE", report)
        self.assertIn("observed IDs 100.0%", report)
        self.assertIn("Algorithm: 1.0", report)


if __name__ == "__main__":
    unittest.main()
