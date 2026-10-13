"""Build 1: paper extraction review ledger, corrections and effort accounting."""

import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from szl_forum_corpus.workflows import paper_review
from szl_forum_corpus.workflows.contracts import WorkflowError
from szl_forum_corpus.workflows.store import Context, Store

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "science_workflows" / "paper_review"
OPERATOR = Context("operator-1", "tenant-a", "operator")
REVIEWER = Context("reviewer-1", "tenant-a", "reviewer")
SECOND = Context("reviewer-2", "tenant-a", "reviewer")
VIEWER = Context("viewer-1", "tenant-a", "viewer")


def text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def case_payload(case_id="case-1", records=None, reference=None, access="available", **changes):
    payload = {
        "case_id": case_id, "protocol_revision": "synthetic-protocol.v1",
        "evidence_kind": "synthetic", "provenance": "tests/fixtures/science_workflows/PROVENANCE.md",
        "records_document": records if records is not None else text("records.json"),
        "reference_document": reference if reference is not None else text("reference.json"),
        "reference_independent": False, "reference_complete": True, "source_access": access,
    }
    payload.update(changes)
    return payload


class PaperReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "workflows.sqlite3"
        self.store = Store(self.path)
        self.ids = (f"{number:032x}" for number in itertools.count(1))
        self.expected = json.loads(text("expected.json"))

    def tearDown(self):
        self.directory.cleanup()

    def rid(self):
        return next(self.ids)

    def case(self, case_id="case-1", context=REVIEWER):
        return self.store.get(context, paper_review.KIND, case_id)

    def registered(self, case_id="case-1", **changes):
        paper_review.register_case(self.store, OPERATOR, self.rid(), case_payload(case_id, **changes))
        paper_review.run_comparison(self.store, OPERATOR, self.rid(), case_id, 1)
        return self.case(case_id)

    def event(self, event, context=REVIEWER, case_id="case-1"):
        revision = self.case(case_id)["revision"]
        return paper_review.record_review_event(self.store, context, self.rid(), case_id, revision, event)

    def effort(self, event, context=REVIEWER, case_id="case-1"):
        revision = self.case(case_id)["revision"]
        return paper_review.record_effort(self.store, context, self.rid(), case_id, revision, event)

    def correct(self, item_id, correction_id, changes, decision="accept"):
        self.event({"type": "propose_correction", "item_id": item_id, "correction_id": correction_id,
                    "changes": changes, "reason_code": "value_error", "evidence": "page 1 table"})
        self.event({"type": "decide_correction", "item_id": item_id, "correction_id": correction_id,
                    "decision": decision, "reason": "checked against the source table"})

    def qc(self, item_id, outcome="accepted", version=None):
        if version is None:
            version = self.case()["items"][item_id]["version"]
        return self.event({"type": "final_qc", "item_id": item_id, "outcome": outcome,
                           "reviewed_version": version, "note": "final check"})

    # ---- comparison against independently written expectations ------------------------------

    def test_comparison_matches_hand_written_expectations(self):
        case = self.registered()
        comparison = case["comparison"]
        self.assertEqual(comparison["kernel_status"], self.expected["kernel_status"])
        self.assertEqual(comparison["denominator"], self.expected["denominator"])
        self.assertEqual({key: value["kernel_result"] for key, value in comparison["findings"].items()},
                         self.expected["kernel_results"])
        self.assertEqual([extra["id"] for extra in case["candidate_only_extras"]],
                         self.expected["candidate_only_extras"])
        summary = paper_review.summary(case)
        for key in ("initial_extraction", "initial_locator", "initial_discrepancies",
                    "unresolved_reference_items"):
            self.assertEqual(summary[key], self.expected[key], key)
        self.assertEqual(summary["declared_reference_items"], 10)
        self.assertEqual(summary["candidate_only_extras"], 1)
        self.assertEqual(summary["rates"]["state"], "UNAVAILABLE")
        self.assertFalse(comparison["kernel_flags"]["scientific_truth_verified"])
        self.assertEqual(case["kernel_binding"]["commit"], "3e1f02c137c1e8da4ab0f78196bfa55dc7263984")

    def test_candidate_only_extras_never_enter_the_denominator(self):
        case = self.registered()
        self.assertNotIn("x1_unexpected", case["items"])
        self.assertNotIn("x1_unexpected", case["comparison"]["findings"])

    def test_initial_comparison_is_immutable(self):
        self.registered()
        with self.assertRaises(WorkflowError) as caught:
            paper_review.run_comparison(self.store, OPERATOR, self.rid(), "case-1", 2)
        self.assertEqual(caught.exception.code, "EXISTS")

    def test_unmapped_kernel_result_is_refused_not_defaulted(self):
        paper_review.register_case(self.store, OPERATOR, self.rid(), case_payload())
        partial = {key: value for key, value in paper_review.RESULT_MAP.items()
                   if key != "ABSTAINED"}
        with mock.patch.object(paper_review, "RESULT_MAP", partial):
            with self.assertRaises(WorkflowError) as caught:
                paper_review.run_comparison(self.store, OPERATOR, self.rid(), "case-1", 1)
        self.assertEqual(caught.exception.code, "KERNEL_UNAVAILABLE")
        self.assertIsNone(self.case()["comparison"])

    # ---- corrections preserve the initial error --------------------------------------------

    def test_accepted_correction_changes_current_value_but_not_initial_metrics(self):
        before = paper_review.summary(self.registered())
        self.correct("t2_wrong_value", "fix-1", {"text": "18.3"})
        case = self.case()
        item = case["items"]["t2_wrong_value"]
        self.assertEqual(item["current"]["text"], "18.3")
        self.assertEqual(item["initial_candidate"]["text"], "13.8")
        self.assertEqual(case["comparison"]["findings"]["t2_wrong_value"]["extraction"], "mismatch")
        after = paper_review.summary(case)
        self.assertEqual(after["initial_discrepancies"], before["initial_discrepancies"])
        self.assertEqual(after["corrections"], {"proposed": 0, "accepted": 1, "rejected": 0})

    def test_rejected_correction_keeps_the_value_and_its_history(self):
        self.registered()
        self.correct("t2_wrong_value", "fix-1", {"text": "18.4"}, decision="reject")
        item = self.case()["items"]["t2_wrong_value"]
        self.assertEqual(item["current"]["text"], "13.8")
        self.assertEqual(item["corrections"][0]["status"], "rejected")
        self.assertEqual(item["corrections"][0]["before"], {"text": "13.8"})

    def test_correction_proposed_against_an_old_value_cannot_be_accepted(self):
        self.registered()
        self.event({"type": "propose_correction", "item_id": "t3_wrong_unit", "correction_id": "a",
                    "changes": {"unit": "µM"}, "reason_code": "unit_error", "evidence": "page 2"})
        self.event({"type": "propose_correction", "item_id": "t3_wrong_unit", "correction_id": "b",
                    "changes": {"unit": "nM"}, "reason_code": "unit_error", "evidence": "page 2"})
        self.event({"type": "decide_correction", "item_id": "t3_wrong_unit", "correction_id": "a",
                    "decision": "accept", "reason": "matches the source"})
        with self.assertRaises(WorkflowError) as caught:
            self.event({"type": "decide_correction", "item_id": "t3_wrong_unit",
                        "correction_id": "b", "decision": "accept", "reason": "late"})
        self.assertEqual(caught.exception.code, "STALE_REVISION")

    def test_corrections_need_an_existing_value_and_a_real_change(self):
        self.registered()
        cases = [("t6_missing", {"text": "3"}), ("t7_abstained", {"text": "0.05"}),
                 ("t2_wrong_value", {"text": "13.8"}), ("t2_wrong_value", {"page": 1}),
                 ("c1_caption", {"unit": "g"})]
        for number, (item_id, changes) in enumerate(cases):
            with self.subTest(item_id=item_id, changes=changes):
                with self.assertRaises(WorkflowError):
                    self.event({"type": "propose_correction", "item_id": item_id,
                                "correction_id": f"c{number}", "changes": changes,
                                "reason_code": "other", "evidence": "x"})
        self.assertEqual(paper_review.summary(self.case())["corrections"]["proposed"], 0)

    def test_unknown_item_is_not_found(self):
        self.registered()
        with self.assertRaises(WorkflowError) as caught:
            self.event({"type": "request_evidence", "item_id": "x1_unexpected", "note": "where?"})
        self.assertEqual(caught.exception.code, "NOT_FOUND")

    # ---- final QC ---------------------------------------------------------------------------

    def test_final_qc_is_invalidated_when_the_corrected_value_changes(self):
        self.registered()
        self.correct("t2_wrong_value", "fix-1", {"text": "18.0"})
        self.qc("t2_wrong_value")
        self.assertEqual(self.case()["items"]["t2_wrong_value"]["qc"]["state"], "accepted")
        self.correct("t2_wrong_value", "fix-2", {"text": "18.3"})
        qc = self.case()["items"]["t2_wrong_value"]["qc"]
        self.assertEqual(qc["state"], "pending")
        self.assertEqual(qc["history"][-1]["state"], "reopened")

    def test_final_qc_must_bind_the_current_item_version(self):
        self.registered()
        self.correct("t2_wrong_value", "fix-1", {"text": "18.3"})
        with self.assertRaises(WorkflowError) as caught:
            self.qc("t2_wrong_value", version=0)
        self.assertEqual(caught.exception.code, "STALE_REVISION")

    def test_hard_items_cannot_receive_accepted_qc(self):
        self.registered()
        self.event({"type": "record_adjudication", "item_id": "t4_ambiguous_identity",
                    "state": "unresolved_identity", "note": "two samples share the label"})
        self.event({"type": "record_adjudication", "item_id": "t5_noncomparable_endpoint",
                    "state": "noncomparable_endpoint", "note": "day 28 versus day 30 endpoint"})
        for item_id in ("t3_wrong_unit", "t4_ambiguous_identity", "t5_noncomparable_endpoint",
                        "t6_missing", "t7_abstained", "t8_unresolved_reference",
                        "t9_wrong_locator"):
            with self.subTest(item_id=item_id):
                with self.assertRaises(WorkflowError) as caught:
                    self.qc(item_id)
                self.assertEqual(caught.exception.code, "INVALID_INPUT")
        self.qc("t1_correct")
        self.qc("t6_missing", outcome="unresolved")
        summary = paper_review.summary(self.case())
        self.assertEqual(summary["final_qc"], {"accepted": 1, "pending": 8, "unresolved": 1})

    def test_inaccessible_source_stays_unresolved(self):
        case = self.registered(access="unavailable")
        self.assertEqual(case["comparison"]["findings"]["t1_correct"]["locator"],
                         "source_unavailable")
        with self.assertRaises(WorkflowError):
            self.qc("t1_correct")

    def test_reopen_returns_an_item_to_pending(self):
        self.registered()
        self.qc("t1_correct")
        self.event({"type": "reopen", "item_id": "t1_correct", "reason": "second look"})
        self.assertEqual(self.case()["items"]["t1_correct"]["qc"]["state"], "pending")

    # ---- admission refusals ----------------------------------------------------------------

    def test_replaced_source_bytes_are_refused(self):
        reference = json.loads(text("reference.json"))
        reference["pdf_sha256"] = "0" * 64
        with self.assertRaises(WorkflowError) as caught:
            paper_review.register_case(self.store, OPERATOR, self.rid(),
                                       case_payload(reference=json.dumps(reference)))
        self.assertIn("GOLD_PDF_HASH_MISMATCH", caught.exception.message)

    def test_ambiguous_reference_with_duplicate_ids_is_refused(self):
        reference = json.loads(text("reference.json"))
        reference["items"].append(dict(reference["items"][0], text="12.6"))
        with self.assertRaises(WorkflowError) as caught:
            paper_review.register_case(self.store, OPERATOR, self.rid(),
                                       case_payload(reference=json.dumps(reference)))
        self.assertIn("DUPLICATE_ITEM_ID", caught.exception.message)

    def test_records_cannot_borrow_rights_or_pipeline_from_the_reference(self):
        for field in ("rights", "pipeline"):
            records = json.loads(text("records.json"))
            del records[field]
            with self.subTest(field=field):
                with self.assertRaises(WorkflowError):
                    paper_review.register_case(self.store, OPERATOR, self.rid(),
                                               case_payload(records=json.dumps(records)))

    def test_candidate_and_reference_kinds_must_agree(self):
        records = json.loads(text("records.json"))
        records["items"][-2] = {"id": "c1_caption", "kind": "cell", "status": "UNRESOLVED",
                                "reason": "kind changed"}
        with self.assertRaises(WorkflowError) as caught:
            paper_review.register_case(self.store, OPERATOR, self.rid(),
                                       case_payload(records=json.dumps(records)))
        self.assertIn("KIND_MISMATCH", caught.exception.message)

    def test_strict_envelope_and_json_rules(self):
        bad = [case_payload(extra=True),
               case_payload(records=text("records.json").replace('"declared_pages": 3',
                                                                 '"declared_pages": 3, "declared_pages": 3')),
               case_payload(reference_independent="yes"),
               case_payload(evidence_kind="unknown")]
        for payload in bad:
            with self.assertRaises(WorkflowError):
                paper_review.register_case(self.store, OPERATOR, self.rid(), payload)
        self.assertEqual(self.store.list(OPERATOR, paper_review.KIND), [])

    def test_viewers_cannot_mutate(self):
        with self.assertRaises(WorkflowError) as caught:
            paper_review.register_case(self.store, VIEWER, self.rid(), case_payload())
        self.assertEqual(caught.exception.code, "FORBIDDEN")
        self.registered()
        with self.assertRaises(WorkflowError) as caught:
            self.event({"type": "request_evidence", "item_id": "t1_correct", "note": "x"},
                       context=VIEWER)
        self.assertEqual(caught.exception.code, "FORBIDDEN")

    def test_registered_input_bytes_are_retained_exactly(self):
        case = self.registered()
        self.assertEqual(self.store.blob(case["inputs"]["records_blob"]),
                         text("records.json").encode("utf-8"))
        self.assertEqual(case["inputs"]["records"]["byte_domain"], "submitted records document bytes")

    # ---- effort -----------------------------------------------------------------------------

    def test_open_and_interrupted_sessions_are_unknown_not_zero(self):
        self.registered()
        self.effort({"action": "start", "session_id": "s1", "activity": "review"})
        session = self.case()["effort"]["sessions"]["s1"]
        self.assertEqual(paper_review.session_effort(session),
                         {"active_seconds": None, "basis": "interrupted_or_open"})
        summary = paper_review.summary(self.case())
        self.assertEqual(summary["effort"]["review"]["unknown_sessions"], 1)
        self.assertEqual(summary["effort"]["review"]["known_seconds"], 0)
        # Restart keeps the unknown.
        reopened = Store(self.path)
        self.assertEqual(reopened.get(REVIEWER, paper_review.KIND, "case-1")["effort"]["sessions"]
                         ["s1"]["state"], "running")

    def test_pause_resume_stop_counts_only_active_intervals(self):
        self.registered()
        self.effort({"action": "start", "session_id": "s1", "activity": "repair"})
        self.effort({"action": "pause", "session_id": "s1"})
        self.effort({"action": "resume", "session_id": "s1"})
        self.effort({"action": "stop", "session_id": "s1"})
        session = self.case()["effort"]["sessions"]["s1"]
        self.assertEqual(len(session["intervals"]), 2)
        measured = paper_review.session_effort(session)
        self.assertEqual(measured["basis"], "server_clock")
        self.assertIsInstance(measured["active_seconds"], int)

    def test_one_reviewer_cannot_run_two_sessions_but_two_reviewers_can(self):
        self.registered()
        self.effort({"action": "start", "session_id": "s1", "activity": "review"})
        with self.assertRaises(WorkflowError):
            self.effort({"action": "start", "session_id": "s2", "activity": "repair"})
        self.effort({"action": "start", "session_id": "s3", "activity": "review"}, context=SECOND)
        with self.assertRaises(WorkflowError) as caught:
            self.effort({"action": "stop", "session_id": "s1"}, context=SECOND)
        self.assertEqual(caught.exception.code, "FORBIDDEN")

    def test_amendments_append_with_reason_and_null_stays_unknown(self):
        self.registered()
        self.effort({"action": "start", "session_id": "s1", "activity": "review"})
        self.effort({"action": "amend", "session_id": "s1", "active_seconds": 600,
                     "reason": "timer left running over lunch"})
        self.effort({"action": "amend", "session_id": "s1", "active_seconds": None,
                     "reason": "actual time not recalled"})
        session = self.case()["effort"]["sessions"]["s1"]
        self.assertEqual(len(session["amendments"]), 2)
        self.assertEqual(paper_review.session_effort(session),
                         {"active_seconds": None, "basis": "unknown"})
        with self.assertRaises(WorkflowError):
            self.effort({"action": "amend", "session_id": "s1", "active_seconds": -1, "reason": "x"})
        with self.assertRaises(WorkflowError):
            self.effort({"action": "amend", "session_id": "s1", "active_seconds": True, "reason": "x"})

    def test_wait_time_is_not_staff_effort(self):
        self.registered()
        self.effort({"action": "start", "session_id": "w1", "activity": "wait"})
        self.effort({"action": "amend", "session_id": "w1", "active_seconds": 3600,
                     "reason": "waited on author reply"})
        summary = paper_review.summary(self.case())
        self.assertEqual(summary["effort"]["wait"]["known_seconds"], 3600)
        self.assertEqual(summary["staff_effort"]["known_seconds"], 0)

    def test_review_events_append_history_in_revision_order(self):
        self.registered()
        self.correct("t2_wrong_value", "fix-1", {"text": "18.3"})
        history = self.store.history(REVIEWER, "case-1")
        self.assertEqual([entry["event_type"] for entry in history],
                         ["case_registered", "initial_comparison_recorded",
                          "review_propose_correction", "review_decide_correction"])
        self.assertEqual([entry["revision"] for entry in history], [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
