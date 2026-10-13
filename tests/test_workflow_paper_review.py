"""Build 1: paper extraction review ledger, corrections and effort accounting."""

import itertools
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from szl_forum_corpus.workflows import paper_review
from szl_forum_corpus.workflows.contracts import WorkflowError
from szl_forum_corpus.workflows.store import Context, Store, Write

FIXTURES = Path(paper_review.__file__).resolve().parent / "fixtures" / "paper_review"
OPERATOR = Context("operator-1", "tenant-a", "operator")
REVIEWER = Context("reviewer-1", "tenant-a", "reviewer")
SECOND = Context("reviewer-2", "tenant-a", "reviewer")
VIEWER = Context("viewer-1", "tenant-a", "viewer")
OTHER_OPERATOR = Context("operator-9", "tenant-b", "operator")
SAME_NAME_OTHER_TENANT = Context("reviewer-1", "tenant-b", "reviewer")


def text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


EPOCH_1, EPOCH_2 = "1" * 32, "2" * 32


class Clock:
    """A controlled server clock: each call returns the next scripted UTC time."""

    def __init__(self, *times):
        self.times = list(times)

    def __call__(self):
        return self.times.pop(0)


def at(clock_time):
    return f"2026-10-13T{clock_time}.000000Z"


def case_payload(case_id="case-1", records=None, reference=None, access="available", **changes):
    payload = {
        "case_id": case_id, "protocol_revision": "synthetic-protocol.v1",
        "evidence_kind": "synthetic", "provenance": "szl_forum_corpus/workflows/fixtures/PROVENANCE.md",
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

    def effort(self, event, context=REVIEWER, case_id="case-1", request=None):
        revision = self.case(case_id, context)["revision"]
        return paper_review.record_effort(self.store, context, request or self.rid(), case_id,
                                          revision, event)

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

    # ---- one running session per reviewer across cases -------------------------------------

    def unchanged(self, case_id, test):
        """Run ``test`` (which must be refused) and require that it wrote nothing to the case."""

        before = (self.case(case_id), self.store.history(REVIEWER, case_id))
        with self.assertRaises(WorkflowError) as caught:
            test()
        self.assertEqual((self.case(case_id), self.store.history(REVIEWER, case_id)), before)
        return caught.exception

    def test_running_session_in_one_case_blocks_start_and_resume_in_another(self):
        self.registered("case-a")
        self.registered("case-b")
        self.effort({"action": "start", "session_id": "b1", "activity": "review"}, case_id="case-b")
        self.effort({"action": "pause", "session_id": "b1"}, case_id="case-b")
        self.effort({"action": "start", "session_id": "a1", "activity": "review"}, case_id="case-a")
        refused = self.unchanged("case-b", lambda: self.effort(
            {"action": "start", "session_id": "b2", "activity": "repair"}, case_id="case-b"))
        self.assertEqual((refused.code, refused.fields),
                         ("INVALID_INPUT", ["session_id", "running_case:case-a"]))
        refused = self.unchanged("case-b", lambda: self.effort(
            {"action": "resume", "session_id": "b1"}, case_id="case-b"))
        self.assertEqual(refused.code, "INVALID_INPUT")
        # Pausing releases the reviewer: resume in case B now succeeds.
        self.effort({"action": "pause", "session_id": "a1"}, case_id="case-a")
        self.effort({"action": "resume", "session_id": "b1"}, case_id="case-b")
        self.unchanged("case-a", lambda: self.effort(
            {"action": "resume", "session_id": "a1"}, case_id="case-a"))
        # Stopping releases too.
        self.effort({"action": "stop", "session_id": "b1"}, case_id="case-b")
        self.effort({"action": "resume", "session_id": "a1"}, case_id="case-a")
        running = [(case_id, session_id) for case_id in ("case-a", "case-b")
                   for session_id, session in self.case(case_id)["effort"]["sessions"].items()
                   if session["state"] == "running"]
        self.assertEqual(running, [("case-a", "a1")])

    def test_other_reviewers_and_tenants_are_not_blocked(self):
        self.registered("case-a")
        self.registered("case-b")
        paper_review.register_case(self.store, OTHER_OPERATOR, self.rid(), case_payload("case-a"))
        self.effort({"action": "start", "session_id": "a1", "activity": "review"}, case_id="case-a")
        self.effort({"action": "start", "session_id": "b1", "activity": "review"}, context=SECOND,
                    case_id="case-b")
        # The same principal name in another tenant is a different reviewer scope.
        self.effort({"action": "start", "session_id": "t1", "activity": "review"},
                    context=SAME_NAME_OTHER_TENANT, case_id="case-a")
        self.assertEqual(self.case("case-a", SAME_NAME_OTHER_TENANT)["effort"]["sessions"]["t1"]
                         ["state"], "running")
        self.assertEqual(self.case("case-b")["effort"]["sessions"]["b1"]["state"], "running")
        self.assertEqual(self.case("case-a")["effort"]["sessions"]["a1"]["state"], "running")

    def test_competing_starts_for_one_reviewer_record_exactly_one(self):
        self.registered("case-a")
        self.registered("case-b")
        barrier, outcomes = threading.Barrier(2), {}
        revisions = {case_id: self.case(case_id)["revision"] for case_id in ("case-a", "case-b")}
        requests = {"case-a": "a" * 32, "case-b": "b" * 32}

        def start(case_id):
            store = Store(self.path)  # each competitor has its own runtime and connections
            barrier.wait()
            try:
                outcomes[case_id] = paper_review.record_effort(
                    store, REVIEWER, requests[case_id], case_id, revisions[case_id],
                    {"action": "start", "session_id": f"s-{case_id}", "activity": "review"})
            except WorkflowError as error:
                outcomes[case_id] = error.code

        threads = [threading.Thread(target=start, args=(case_id,)) for case_id in requests]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(sorted(type(value).__name__ for value in outcomes.values()),
                         ["dict", "str"])
        loser = next(case_id for case_id, value in outcomes.items() if value == "INVALID_INPUT")
        winner = next(case_id for case_id in requests if case_id != loser)
        self.assertEqual(self.case(loser)["revision"], revisions[loser])
        self.assertEqual(self.case(loser)["effort"]["sessions"], {})
        self.assertIsNone(self.store.recover(REVIEWER, requests[loser]))
        self.assertEqual(self.case(winner)["effort"]["sessions"][f"s-{winner}"]["state"],
                         "running")

    def test_request_id_replay_cannot_bypass_the_reviewer_guard(self):
        self.registered("case-a")
        self.registered("case-b")
        start_a = {"action": "start", "session_id": "a1", "activity": "review"}
        start_b = {"action": "start", "session_id": "b1", "activity": "review"}
        first = self.effort(start_a, case_id="case-a", request="1" * 32)
        refused_request = "2" * 32
        self.unchanged("case-b", lambda: self.effort(start_b, case_id="case-b",
                                                     request=refused_request))
        self.effort({"action": "pause", "session_id": "a1"}, case_id="case-a")
        # Replaying the original start returns its recorded result; it does not restart a1.
        revision = self.case("case-a")["revision"]
        self.assertEqual(paper_review.record_effort(self.store, REVIEWER, "1" * 32, "case-a", 2,
                                                    start_a), first)
        self.assertEqual(self.case("case-a")["revision"], revision)
        self.assertEqual(self.case("case-a")["effort"]["sessions"]["a1"]["state"], "paused")
        # A request ID cannot be reused for a different action.
        with self.assertRaises(WorkflowError) as caught:
            self.effort(start_b, case_id="case-b", request="1" * 32)
        self.assertEqual(caught.exception.code, "REQUEST_ID_CONFLICT")
        # A refused request recorded nothing, so its ID is evaluated afresh once a1 is paused.
        self.assertIsNone(self.store.recover(REVIEWER, refused_request))
        self.effort(start_b, case_id="case-b", request=refused_request)
        self.unchanged("case-a", lambda: self.effort({"action": "resume", "session_id": "a1"},
                                                     case_id="case-a"))

    # ---- restarts never become measured effort ----------------------------------------------

    def timed(self, store, clock_time, event, case_id="case-1"):
        revision = store.get(REVIEWER, paper_review.KIND, case_id)["revision"]
        with mock.patch.object(paper_review, "utc_now", Clock(at(clock_time))):
            return paper_review.record_effort(store, REVIEWER, self.rid(), case_id, revision, event)

    def session(self, session_id="s1"):
        return self.case()["effort"]["sessions"][session_id]

    def runtime(self, epoch):
        return Store(self.path, epoch=epoch)

    def test_uninterrupted_pause_resume_stop_counts_only_observed_intervals(self):
        self.registered()
        store = self.runtime(EPOCH_1)
        self.timed(store, "10:00:00", {"action": "start", "session_id": "s1", "activity": "review"})
        self.timed(store, "10:01:00", {"action": "pause", "session_id": "s1"})
        self.timed(store, "10:01:40", {"action": "resume", "session_id": "s1"})
        self.timed(store, "10:02:10", {"action": "stop", "session_id": "s1"})
        session = self.session()
        self.assertEqual(session["interval_epochs"], [[EPOCH_1, EPOCH_1], [EPOCH_1, EPOCH_1]])
        self.assertEqual(paper_review.session_effort(session),
                         {"active_seconds": 90, "basis": "server_clock"})
        self.assertEqual(paper_review.summary(self.case())["staff_effort"]["known_seconds"], 90)

    def test_stop_after_a_restart_keeps_effort_unknown(self):
        self.registered()
        self.timed(self.runtime(EPOCH_1), "09:00:00",
                   {"action": "start", "session_id": "s1", "activity": "review"})
        restarted = self.runtime(EPOCH_2)  # same database, new runtime, eight hours later
        self.timed(restarted, "17:00:00", {"action": "stop", "session_id": "s1"})
        session = self.session()
        self.assertEqual(session["state"], "stopped")
        # The closing time is kept as an observation; it is not counted as work.
        self.assertEqual(session["intervals"], [[at("09:00:00"), at("17:00:00")]])
        self.assertEqual(session["interval_epochs"], [[EPOCH_1, EPOCH_2]])
        self.assertEqual(paper_review.session_effort(session),
                         {"active_seconds": None, "basis": "interrupted_by_restart"})
        summary = paper_review.summary(self.case())
        self.assertEqual((summary["staff_effort"]["known_seconds"],
                          summary["staff_effort"]["unknown_sessions"]), (0, 1))
        events = [entry for entry in self.store.history(REVIEWER, "case-1")
                  if entry["event_type"].startswith("effort_")]
        self.assertEqual([(entry["event_type"], entry["payload"]["runtime_epoch"]) for entry in events],
                         [("effort_start", EPOCH_1), ("effort_stop", EPOCH_2)])

    def test_pause_after_a_restart_then_stop_stays_unknown_even_after_new_observed_work(self):
        self.registered()
        self.timed(self.runtime(EPOCH_1), "09:00:00",
                   {"action": "start", "session_id": "s1", "activity": "repair"})
        restarted = self.runtime(EPOCH_2)
        self.timed(restarted, "12:00:00", {"action": "pause", "session_id": "s1"})
        self.assertEqual(paper_review.session_effort(self.session())["basis"], "interrupted_or_open")
        self.timed(restarted, "12:10:00", {"action": "stop", "session_id": "s1"})
        self.assertEqual(paper_review.session_effort(self.session()),
                         {"active_seconds": None, "basis": "interrupted_by_restart"})
        # Resuming in the new runtime adds an observed interval, but the outage stays unknown.
        self.timed(restarted, "13:00:00", {"action": "start", "session_id": "s2",
                                          "activity": "repair"})
        self.timed(restarted, "13:05:00", {"action": "pause", "session_id": "s2"})
        self.timed(restarted, "13:06:00", {"action": "resume", "session_id": "s2"})
        self.timed(restarted, "13:07:00", {"action": "stop", "session_id": "s2"})
        self.assertEqual(paper_review.session_effort(self.session("s2")),
                         {"active_seconds": 360, "basis": "server_clock"})
        self.assertEqual(paper_review.summary(self.case())["effort"]["repair"],
                         {"known_seconds": 360, "unknown_sessions": 1, "sessions": 2,
                          "by_actor": {"reviewer-1": {"known_seconds": 360, "unknown_sessions": 1}}})

    def test_paused_before_restart_then_resumed_counts_only_if_each_interval_is_observed(self):
        self.registered()
        first = self.runtime(EPOCH_1)
        self.timed(first, "09:00:00", {"action": "start", "session_id": "s1", "activity": "review"})
        self.timed(first, "09:10:00", {"action": "pause", "session_id": "s1"})
        restarted = self.runtime(EPOCH_2)
        self.timed(restarted, "15:00:00", {"action": "resume", "session_id": "s1"})
        self.timed(restarted, "15:05:00", {"action": "stop", "session_id": "s1"})
        self.assertEqual(self.session()["interval_epochs"], [[EPOCH_1, EPOCH_1], [EPOCH_2, EPOCH_2]])
        self.assertEqual(paper_review.session_effort(self.session()),
                         {"active_seconds": 900, "basis": "server_clock"})

    def test_a_reasoned_amendment_can_set_an_interrupted_session(self):
        self.registered()
        self.timed(self.runtime(EPOCH_1), "09:00:00",
                   {"action": "start", "session_id": "s1", "activity": "review"})
        restarted = self.runtime(EPOCH_2)
        self.timed(restarted, "17:00:00", {"action": "stop", "session_id": "s1"})
        self.timed(restarted, "17:01:00", {"action": "amend", "session_id": "s1",
                                          "active_seconds": 1500,
                                          "reason": "reviewer log: 25 minutes before the outage"})
        self.assertEqual(paper_review.session_effort(self.session()),
                         {"active_seconds": 1500, "basis": "manual_amendment"})
        self.timed(restarted, "17:02:00", {"action": "amend", "session_id": "s1",
                                          "active_seconds": None, "reason": "log disputed"})
        self.assertEqual(paper_review.session_effort(self.session()),
                         {"active_seconds": None, "basis": "unknown"})
        session = self.session()
        self.assertEqual([entry["active_seconds"] for entry in session["amendments"]], [1500, None])
        self.assertEqual(session["intervals"], [[at("09:00:00"), at("17:00:00")]])

    def legacy_sessions(self, sessions):
        """Write sessions exactly as the merged #31 adapter did: no runtime epochs anywhere."""

        def apply(tx):
            case = tx.get(paper_review.KIND, "case-1")
            case.pop("revision")
            case["effort"]["sessions"].update(sessions)
            return [Write("case-1", paper_review.KIND, "effort_start", case,
                          {"action": "start", "actor": "reviewer-1"})], {"action": "start"}

        revision = self.case()["revision"]
        self.store.command(REVIEWER, self.rid(), "paper.effort.start", {"legacy": True},
                           {"case-1": revision}, apply)

    def test_sessions_recorded_before_epochs_never_become_measured_effort(self):
        self.registered()
        legacy = {"actor": "reviewer-1", "activity": "review", "amendments": [],
                  "capture": "server_clock"}
        self.legacy_sessions({
            "running": dict(legacy, state="running", intervals=[[at("08:00:00"), None]]),
            "paused": dict(legacy, state="paused",
                           intervals=[[at("08:00:00"), at("08:30:00")]]),
            "stopped": dict(legacy, state="stopped",
                            intervals=[[at("07:00:00"), at("07:20:00")]]),
        })
        self.assertNotIn("interval_epochs", self.session("running"))
        upgraded = self.runtime(EPOCH_2)
        self.timed(upgraded, "18:00:00", {"action": "stop", "session_id": "running"})
        self.assertEqual(self.session("running")["interval_epochs"], [[None, EPOCH_2]])
        self.assertEqual(paper_review.session_effort(self.session("running")),
                         {"active_seconds": None, "basis": "interrupted_by_restart"})
        self.timed(upgraded, "18:01:00", {"action": "resume", "session_id": "paused"})
        self.timed(upgraded, "18:02:00", {"action": "stop", "session_id": "paused"})
        self.assertEqual(self.session("paused")["interval_epochs"], [[None, None],
                                                                     [EPOCH_2, EPOCH_2]])
        self.assertEqual(paper_review.session_effort(self.session("paused")),
                         {"active_seconds": None, "basis": "recorded_without_runtime_epoch"})
        self.assertEqual(paper_review.session_effort(self.session("stopped")),
                         {"active_seconds": None, "basis": "recorded_without_runtime_epoch"})
        summary = paper_review.summary(self.case())
        self.assertEqual(summary["effort"]["review"]["known_seconds"], 0)
        self.assertEqual(summary["effort"]["review"]["unknown_sessions"], 3)

    def test_runtime_epochs_are_fresh_and_strict(self):
        self.assertNotEqual(Store(self.path).epoch, Store(self.path).epoch)
        for bad in ("", "1" * 31, "G" * 32, 7):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    Store(self.path, epoch=bad)

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
