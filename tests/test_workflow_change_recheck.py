"""Build 2: evidence-change recheck queue over the pinned research-change-impact kernel."""

import itertools
import json
import tempfile
import unittest
from pathlib import Path

from szl_forum_corpus.workflows import change_recheck as recheck
from szl_forum_corpus.workflows.contracts import WorkflowError
from szl_forum_corpus.workflows.store import Context, Store

FIXTURES = Path(recheck.__file__).resolve().parent / "fixtures" / "change_recheck"
OPERATOR = Context("operator-1", "tenant-a", "operator")
REVIEWER = Context("reviewer-1", "tenant-a", "reviewer")
SECOND = Context("reviewer-2", "tenant-a", "reviewer")
VIEWER = Context("viewer-1", "tenant-a", "viewer")
OTHER = Context("operator-9", "tenant-b", "operator")
CLAIMS = ["c1", "c2", "c3", "c4", "c5"]


def text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def snapshot(snapshot_id, document, coverage="complete", **extra):
    payload = {"snapshot_id": snapshot_id, "evidence_kind": "synthetic",
               "provenance": "szl_forum_corpus/workflows/fixtures/PROVENANCE.md",
               "coverage": coverage, "graph_document": document}
    payload.update(extra)
    return payload


def graph(*nodes, links=()):
    return json.dumps({"schema": "szl.research-anatomy.v1", "nodes": list(nodes),
                       "evidence_links": list(links)})


class ChangeRecheckTests(unittest.TestCase):
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

    def register(self, snapshot_id, document, **extra):
        return recheck.register_snapshot(self.store, OPERATOR, self.rid(),
                                         snapshot(snapshot_id, document, **extra))

    def change(self, change_set_id, old, new, observed, claims=CLAIMS, request=None,
               queue_revision="current", context=OPERATOR):
        if queue_revision == "current":
            queue_revision = recheck.queue_view(self.store, OPERATOR)["queue_revision"]
        payload = {"change_set_id": change_set_id, "baseline_snapshot_id": old,
                   "current_snapshot_id": new, "required_claims": list(claims),
                   "observed_digests": observed, "as_of": "2026-10-13T00:00:00Z",
                   "expected_queue_revision": queue_revision}
        return recheck.create_change_set(self.store, context, request or self.rid(), payload)

    def fixtures(self, second=False):
        self.register("v1", text("baseline.json"))
        self.register("v2", text("current.json"))
        first = self.change("cs1", "v1", "v2", self.expected["first"]["observed_digests"])
        if not second:
            return first
        self.register("v3", text("later.json"), disputed_edges=[
            {"dependency": "d2", "dependent": "r1", "note": "batch-to-fit mapping disputed"}])
        return self.change("cs2", "v2", "v3", self.expected["second"]["observed_digests"])

    def task(self, change_set_id, claim, context=REVIEWER):
        return self.store.get(context, recheck.TASK, recheck.task_id_for(change_set_id, claim))

    def event(self, change_set_id, claim, event, context=REVIEWER, revision=None):
        task = self.task(change_set_id, claim)
        return recheck.record_task_event(self.store, context, self.rid(), task["task_id"],
                                         task["revision"] if revision is None else revision, event)

    # ---- impact against independently derived expectations ---------------------------------

    def test_first_change_set_matches_hand_derived_impact(self):
        self.fixtures()
        expected = self.expected["first"]
        change_set = self.store.get(REVIEWER, recheck.CHANGE_SET, "cs1")
        report = change_set["report"]
        self.assertEqual({claim["id"]: claim["state"] for claim in report["claims"]},
                         expected["claim_states"])
        self.assertEqual({seed["id"]: seed["reasons"] for seed in report["seeds"]}, expected["seeds"])
        self.assertEqual(report["affected_nodes"], expected["affected_nodes"])
        self.assertEqual(sorted(self.store.get(REVIEWER, recheck.TASK, task)["claim_id"]
                                for task in change_set["tasks"]), expected["task_claims"])
        for claim in expected["task_claims"]:
            task = self.task("cs1", claim)
            self.assertEqual(task["affected_paths"], expected["affected_paths"][claim], claim)
            self.assertEqual(task["witness_path"], expected["witness_paths"][claim], claim)
            self.assertEqual(task["dependency_order"], expected["dependency_order"][claim], claim)
            self.assertEqual(task["execution"], "open")
            self.assertEqual(task["label"], "Needs recheck")
        queue = recheck.queue_view(self.store, REVIEWER)
        self.assertEqual([task["claim_id"] for task in queue["tasks"]], expected["queue_order"])
        self.assertIsNone(queue["coverage_warning"])
        self.assertEqual(change_set["kernel_binding"]["traversal"], "union_of_edges")

    def test_removed_dependency_with_unchanged_output_bytes_keeps_the_claim_affected(self):
        self.fixtures()
        paths = self.task("cs1", "c1")["affected_paths"]
        self.assertIn(["k1", "r1", "c1"], paths)
        report = self.store.get(REVIEWER, recheck.CHANGE_SET, "cs1")["report"]
        self.assertIn({"dependency": "d1", "dependent": "k1"}, report["removed_edges"])

    def test_one_changed_ancestor_yields_one_task_per_claim_with_all_paths(self):
        self.fixtures()
        change_set = self.store.get(REVIEWER, recheck.CHANGE_SET, "cs1")
        claims = [self.store.get(REVIEWER, recheck.TASK, task)["claim_id"]
                  for task in change_set["tasks"]]
        self.assertEqual(len(claims), len(set(claims)))
        self.assertEqual(len(self.task("cs1", "c3")["affected_paths"]), 2)

    def test_removed_required_claim_stays_visible_and_cannot_be_satisfied(self):
        self.fixtures()
        task = self.task("cs1", "c4")
        self.assertEqual(task["kernel_state"], "MISSING_REQUIRED_CLAIM")
        self.event("cs1", "c4", {"type": "start_review"})
        self.event("cs1", "c4", {"type": "set_predicate", "text": "survey frame re-justified"})
        with self.assertRaises(WorkflowError) as caught:
            self.event("cs1", "c4", {"type": "resolve", "evidence": "x", "change_set_id": "cs1",
                                     "predicate_revision": 1})
        self.assertEqual(caught.exception.code, "INVALID_INPUT")
        self.assertEqual(self.task("cs1", "c4")["execution"], "in_review")

    def test_second_change_set_supersedes_and_flags_unknown_access_and_disputes(self):
        self.fixtures(second=True)
        expected = self.expected["second"]
        report = self.store.get(REVIEWER, recheck.CHANGE_SET, "cs2")["report"]
        self.assertEqual({claim["id"]: claim["state"] for claim in report["claims"]},
                         expected["claim_states"])
        self.assertEqual({seed["id"]: seed["reasons"] for seed in report["seeds"]}, expected["seeds"])
        for claim, triggers in expected["task_triggers"].items():
            task = self.task("cs2", claim)
            self.assertEqual(task["triggers"], triggers, claim)
            self.assertEqual(task["condition"], expected["task_conditions"][claim], claim)
            self.assertEqual(task["affected_paths"], expected["affected_paths"][claim], claim)
        for claim in expected["superseded_first_claims"]:
            old = self.task("cs1", claim)
            self.assertEqual(old["execution"], "superseded")
            self.assertEqual(old["superseded_by"], recheck.task_id_for("cs2", claim))
        queue = recheck.queue_view(self.store, REVIEWER)
        self.assertEqual(queue["counts"]["execution"]["superseded"], 4)
        self.assertEqual(queue["counts"]["execution"]["open"], 5)
        self.assertEqual(queue["counts"]["total"], 9)

    def test_old_resolution_cannot_clear_a_new_change_set(self):
        self.fixtures()
        self.event("cs1", "c2", {"type": "start_review"})
        self.event("cs1", "c2", {"type": "set_predicate", "text": "assay rerun within 5% of prior"})
        self.event("cs1", "c2", {"type": "resolve", "evidence": "rerun log 2026-10-13",
                                 "change_set_id": "cs1", "predicate_revision": 1})
        resolved = self.task("cs1", "c2")
        self.assertEqual(resolved["execution"], "resolved")
        self.assertEqual(resolved["condition"], "human_confirmed_satisfied")
        self.assertIn("does not make the claim true", resolved["resolution"]["meaning"])
        self.register("v3", text("later.json"))
        self.change("cs2", "v2", "v3", self.expected["second"]["observed_digests"])
        old = self.task("cs1", "c2")
        self.assertEqual(old["execution"], "superseded")
        self.assertEqual(old["resolution"]["by"], "reviewer-1")
        self.assertEqual(self.task("cs2", "c2")["execution"], "open")
        with self.assertRaises(WorkflowError):
            self.event("cs1", "c2", {"type": "reopen", "reason": "try to revive"})

    def test_stale_reviewer_resolution_is_refused(self):
        self.fixtures()
        self.event("cs1", "c1", {"type": "start_review"})
        self.event("cs1", "c1", {"type": "set_predicate", "text": "refit matches within CI"})
        seen = self.task("cs1", "c1")["revision"]
        self.event("cs1", "c1", {"type": "set_predicate", "text": "refit matches and d1 re-pinned"},
                   context=SECOND)
        with self.assertRaises(WorkflowError) as caught:
            self.event("cs1", "c1", {"type": "resolve", "evidence": "x", "change_set_id": "cs1",
                                     "predicate_revision": 1}, revision=seen)
        self.assertEqual(caught.exception.code, "STALE_REVISION")
        with self.assertRaises(WorkflowError) as caught:
            self.event("cs1", "c1", {"type": "resolve", "evidence": "x", "change_set_id": "cs1",
                                     "predicate_revision": 1})
        self.assertEqual(caught.exception.code, "STALE_REVISION")
        with self.assertRaises(WorkflowError) as caught:
            self.event("cs1", "c1", {"type": "resolve", "evidence": "x", "change_set_id": "cs0",
                                     "predicate_revision": 2})
        self.assertEqual(caught.exception.code, "STALE_REVISION")
        self.assertEqual(self.task("cs1", "c1")["execution"], "in_review")

    def test_resolution_requires_review_and_a_predicate(self):
        self.fixtures()
        with self.assertRaises(WorkflowError):
            self.event("cs1", "c1", {"type": "resolve", "evidence": "x", "change_set_id": "cs1",
                                     "predicate_revision": 0})
        self.event("cs1", "c1", {"type": "start_review"})
        with self.assertRaises(WorkflowError):
            self.event("cs1", "c1", {"type": "resolve", "evidence": "x", "change_set_id": "cs1",
                                     "predicate_revision": 0})
        with self.assertRaises(WorkflowError):
            self.event("cs1", "c1", {"type": "record_condition",
                                     "condition": "human_confirmed_satisfied", "note": "x"})

    def test_waiting_reasons_and_reopen(self):
        self.fixtures()
        self.event("cs1", "c3", {"type": "wait", "condition": "access_limited",
                                 "note": "p1 behind a licence wall"})
        task = self.task("cs1", "c3")
        self.assertEqual((task["execution"], task["condition"]), ("waiting", "access_limited"))
        self.event("cs1", "c3", {"type": "start_review"})
        self.event("cs1", "c3", {"type": "set_predicate", "text": "p1 re-read"})
        self.event("cs1", "c3", {"type": "resolve", "evidence": "read", "change_set_id": "cs1",
                                 "predicate_revision": 1})
        self.event("cs1", "c3", {"type": "reopen", "reason": "new concern"})
        task = self.task("cs1", "c3")
        self.assertEqual((task["execution"], task["condition"]), ("open", "unknown"))
        self.assertEqual([entry["event"] for entry in task["history"]],
                         ["wait", "start_review", "set_predicate", "resolve", "reopen"])

    # ---- refusals that must leave the queue untouched ----------------------------------------

    def test_cycles_are_refused_with_their_path(self):
        document = graph({"id": "a", "kind": "run", "title": "A", "depends_on": ["b"]},
                         {"id": "b", "kind": "run", "title": "B", "depends_on": ["a"]})
        with self.assertRaises(WorkflowError) as caught:
            self.register("loop", document)
        self.assertEqual(caught.exception.fields, ["cycle:a -> b -> a"])

    def test_unknown_endpoints_duplicates_and_undeclared_disputes_are_refused(self):
        bad = [graph({"id": "a", "kind": "run", "title": "A", "depends_on": ["ghost"]}),
               graph({"id": "a", "kind": "run", "title": "A"}, {"id": "a", "kind": "run", "title": "B"}),
               graph({"id": "c", "kind": "claim", "title": "C"},
                     links=[{"claim": "c", "evidence": "ghost", "relation": "supports"}]),
               json.dumps({"schema": "szl.research-anatomy.v1", "nodes": [], "extra": 1}),
               json.dumps({"schema": "szl.research-anatomy.v2", "nodes": []})]
        for number, document in enumerate(bad):
            with self.subTest(number=number):
                with self.assertRaises(WorkflowError):
                    self.register(f"bad{number}", document)
        with self.assertRaises(WorkflowError):
            self.register("bad-dispute", graph({"id": "a", "kind": "run", "title": "A"}),
                          disputed_edges=[{"dependency": "x", "dependent": "a", "note": "n"}])
        self.assertEqual(self.store.list(OPERATOR, recheck.SNAPSHOT), [])

    def test_failed_change_set_leaves_the_old_queue_unchanged(self):
        self.fixtures()
        before = recheck.queue_view(self.store, REVIEWER)
        self.register("v3", text("later.json"))
        bad_observed = dict(self.expected["second"]["observed_digests"], r2="e2" * 32)
        with self.assertRaises(WorkflowError):
            self.change("cs2", "v2", "v3", bad_observed)
        with self.assertRaises(WorkflowError) as caught:
            self.change("cs2", "v2", "missing", self.expected["second"]["observed_digests"])
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.assertEqual(recheck.queue_view(self.store, REVIEWER), before)

    def test_competing_change_sets_get_one_success_and_one_conflict(self):
        self.fixtures()
        self.register("v3", text("later.json"))
        revision = recheck.queue_view(self.store, OPERATOR)["queue_revision"]
        observed = self.expected["second"]["observed_digests"]
        self.change("cs2", "v2", "v3", observed, queue_revision=revision)
        with self.assertRaises(WorkflowError) as caught:
            self.change("cs3", "v2", "v3", observed, queue_revision=revision)
        self.assertEqual(caught.exception.code, "STALE_REVISION")

    def test_identical_replay_returns_the_original_change_set_result(self):
        self.register("v1", text("baseline.json"))
        self.register("v2", text("current.json"))
        observed = self.expected["first"]["observed_digests"]
        request = self.rid()
        first = self.change("cs1", "v1", "v2", observed, request=request, queue_revision=None)
        self.event("cs1", "c1", {"type": "start_review"})
        again = self.change("cs1", "v1", "v2", observed, request=request, queue_revision=None)
        self.assertEqual(first, again)

    def test_coverage_gaps_are_reported(self):
        self.register("v1", text("baseline.json"))
        self.register("v2", text("current.json"), coverage="partial")
        self.change("cs1", "v1", "v2", self.expected["first"]["observed_digests"])
        self.assertEqual(self.task("cs1", "c1")["discovery_coverage"], "incomplete")
        self.assertIn("not evidence", recheck.queue_view(self.store, REVIEWER)["coverage_warning"])

    def test_scope_roles_and_restart(self):
        self.fixtures()
        self.assertEqual(recheck.queue_view(self.store, OTHER)["tasks"], [])
        with self.assertRaises(WorkflowError) as caught:
            self.store.get(OTHER, recheck.TASK, recheck.task_id_for("cs1", "c1"))
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        with self.assertRaises(WorkflowError) as caught:
            recheck.register_snapshot(self.store, VIEWER, self.rid(),
                                      snapshot("v9", text("later.json")))
        self.assertEqual(caught.exception.code, "FORBIDDEN")
        reopened = Store(self.path)
        self.assertEqual(len(reopened.list(REVIEWER, recheck.TASK)), 4)

    def test_snapshot_bytes_are_retained_and_identifiers_are_immutable(self):
        self.register("v1", text("baseline.json"))
        stored = self.store.get(OPERATOR, recheck.SNAPSHOT, "v1")
        self.assertEqual(self.store.blob(stored["graph"]["blob"]), text("baseline.json").encode())
        self.assertNotEqual(stored["graph"]["original"]["value"], stored["graph"]["canonical"]["value"])
        with self.assertRaises(WorkflowError) as caught:
            self.register("v1", text("current.json"))
        self.assertEqual(caught.exception.code, "EXISTS")


if __name__ == "__main__":
    unittest.main()
