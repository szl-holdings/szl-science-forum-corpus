"""Build 2: evidence-change recheck queue over the pinned research-change-impact kernel."""

import itertools
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from szl_forum_corpus.web import workflow_views as views
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


def ladder(layers, seed_title, *, seed_below_ladder):
    """A seed s and a claim c over a ladder of question nodes with 2**layers routes through it.

    With ``seed_below_ladder`` every route ends at s (2**layers seed paths); otherwise c depends
    on s directly and no route through the ladder ever reaches a seed.
    """

    nodes = [{"id": "s", "kind": "question", "title": seed_title}]
    for layer in range(layers):
        below = ([f"a{layer + 1}", f"b{layer + 1}"] if layer + 1 < layers
                 else (["s"] if seed_below_ladder else []))
        for side in "ab":
            nodes.append({"id": f"{side}{layer}", "kind": "question",
                          "title": f"Unchanged step {side}{layer}", "depends_on": below})
    nodes.append({"id": "c", "kind": "claim", "title": "Synthetic claim",
                  "depends_on": ["a0", "b0"] + ([] if seed_below_ladder else ["s"])})
    return graph(*nodes)


def unbounded_visits(parents, claim):
    """How many nodes the merged #31 enumeration visited: every simple route, seed or not."""

    visits, stack = 0, [(claim, (claim,))]
    while stack:
        node, suffix = stack.pop()
        visits += 1
        stack.extend((parent, (parent,) + suffix) for parent in parents.get(node, ())
                     if parent not in suffix)
    return visits


def merged_paths(parents, seeds, claim, limit):
    """The merged #31 enumeration, kept verbatim as the reference for which paths are kept."""

    paths, truncated, stack = [], False, [(claim, [claim])]
    while stack:
        node, suffix = stack.pop()
        if node in seeds:
            if len(paths) == limit:
                truncated = True
                break
            paths.append(list(suffix))
        for parent in sorted(parents.get(node, set()), reverse=True):
            if parent not in suffix:
                stack.append((parent, [parent] + suffix))
    return sorted(paths, key=lambda path: (len(path), path)), truncated


# A claim whose only ancestor declares no artifact: comparing it with itself has no declared impact.
QUIET = graph({"id": "q", "kind": "question", "title": "Synthetic question"},
              {"id": "c", "kind": "claim", "title": "Synthetic claim", "depends_on": ["q"]})


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

    # ---- coverage of change sets that create no task -----------------------------------------

    def quiet_change_set(self, change_set_id, baseline, current):
        self.register(f"{change_set_id}-old", QUIET, coverage=baseline)
        self.register(f"{change_set_id}-new", QUIET, coverage=current)
        return self.change(change_set_id, f"{change_set_id}-old", f"{change_set_id}-new", {},
                           claims=["c"])

    def html(self, view):
        return views.recheck_queue(view, self.store.list(REVIEWER, recheck.SNAPSHOT), "token")

    def test_incomplete_change_set_without_tasks_still_warns_in_json_and_html(self):
        for number, (baseline, current) in enumerate((("complete", "partial"),
                                                      ("unknown", "complete"),
                                                      ("partial", "unknown"))):
            with self.subTest(baseline=baseline, current=current):
                self.directory.cleanup()
                self.setUp()
                outcome = self.quiet_change_set(f"cs{number}", baseline, current)
                self.assertEqual((outcome["outcome"]["tasks"], outcome["outcome"]["report_status"]),
                                 ([], "NO_DECLARED_IMPACT"))
                view = recheck.queue_view(self.store, REVIEWER)
                self.assertEqual(view["tasks"], [])
                self.assertEqual(view["change_sets"], [f"cs{number}"])
                self.assertEqual(view["coverage_warning"], recheck.COVERAGE_WARNING)
                self.assertEqual(view["coverage"]["incomplete_change_sets"], [
                    {"change_set_id": f"cs{number}", "baseline": baseline, "current": current,
                     "tasks": 0}])
                self.assertEqual(view["coverage"]["active_tasks_with_incomplete_discovery"], 0)
                page = self.html(view)
                self.assertIn("No recheck tasks.", page)
                self.assertIn("an absent task is not evidence that a claim is unaffected", page)
                self.assertIn(f"<code>cs{number}</code>: baseline coverage {baseline}", page)

    def test_complete_change_set_without_tasks_shows_no_warning(self):
        self.quiet_change_set("cs1", "complete", "complete")
        view = recheck.queue_view(self.store, REVIEWER)
        self.assertEqual((view["tasks"], view["change_sets"]), ([], ["cs1"]))
        self.assertIsNone(view["coverage_warning"])
        self.assertEqual(view["coverage"]["incomplete_change_sets"], [])
        page = self.html(view)
        self.assertIn("No recheck tasks.", page)
        self.assertNotIn('class="notice"', page)
        self.assertNotIn("not evidence that a claim is unaffected", page)

    def test_an_earlier_incomplete_change_set_keeps_warning_after_a_complete_one(self):
        # Policy: every recorded change set counts; a later complete comparison of other
        # snapshots does not rediscover what the earlier partial comparison could have missed.
        self.assertEqual(recheck.COVERAGE_POLICY, "every_recorded_change_set")
        self.quiet_change_set("cs1", "complete", "partial")
        self.quiet_change_set("cs2", "complete", "complete")
        view = recheck.queue_view(self.store, REVIEWER)
        self.assertEqual(view["change_sets"], ["cs1", "cs2"])
        self.assertEqual([item["change_set_id"] for item in view["coverage"]["incomplete_change_sets"]],
                         ["cs1"])
        self.assertEqual(view["coverage_warning"], recheck.COVERAGE_WARNING)
        self.assertEqual(view["coverage"]["policy_text"], recheck.COVERAGE_POLICY_TEXT)
        self.assertIn("<code>cs1</code>", self.html(view))
        self.assertNotIn("<code>cs2</code>", self.html(view))

    def test_superseding_tasks_does_not_clear_an_earlier_incomplete_change_set(self):
        self.register("v1", text("baseline.json"))
        self.register("v2", text("current.json"), coverage="partial")
        self.change("cs1", "v1", "v2", self.expected["first"]["observed_digests"])
        self.register("v2c", text("current.json"))
        self.register("v3", text("later.json"), disputed_edges=[
            {"dependency": "d2", "dependent": "r1", "note": "batch-to-fit mapping disputed"}])
        self.change("cs2", "v2c", "v3", self.expected["second"]["observed_digests"])
        self.assertEqual(sorted(self.task("cs1", claim)["execution"] for claim in
                                self.expected["first"]["task_claims"]), ["superseded"] * 4)
        view = recheck.queue_view(self.store, REVIEWER)
        self.assertEqual(view["coverage"]["active_tasks_with_incomplete_discovery"], 0)
        self.assertEqual([item["change_set_id"] for item in view["coverage"]["incomplete_change_sets"]],
                         ["cs1"])
        self.assertEqual(view["coverage_warning"], recheck.COVERAGE_WARNING)

    # ---- bounded known-path enumeration -------------------------------------------------------

    def ladder_change_set(self, change_set_id, layers, *, seed_below_ladder, request=None):
        old, new = f"{change_set_id}-old", f"{change_set_id}-new"
        self.register(old, ladder(layers, "Seed v1", seed_below_ladder=seed_below_ladder))
        self.register(new, ladder(layers, "Seed v2", seed_below_ladder=seed_below_ladder))
        return self.change(change_set_id, old, new, {}, claims=["c"], request=request)

    def test_branches_that_never_reach_a_seed_cost_no_enumeration_work(self):
        operations = {}
        for layers in (6, 14):
            self.ladder_change_set(f"cs{layers}", layers, seed_below_ladder=False)
            change_set = self.store.get(REVIEWER, recheck.CHANGE_SET, f"cs{layers}")
            task = self.task(f"cs{layers}", "c")
            self.assertEqual((task["affected_paths"], task["paths_truncated"]), ([["s", "c"]], False))
            enumeration = change_set["path_enumeration"]
            self.assertEqual((enumeration["pruning"], enumeration["budget_operations"],
                              enumeration["paths_kept_per_claim"]),
                             ("seed_reachability", recheck.MAX_PATH_WORK, 64))
            operations[layers] = enumeration["operations"]
        # The unchanged ladder never reaches a seed: its 2**14 routes cost nothing, so the work is
        # the same small count at 6 and at 14 layers.
        self.assertEqual(operations[6], operations[14])
        self.assertLessEqual(operations[14], 8)
        old = recheck.kernels.load(recheck.KERNEL).szl_change_graph(
            json.loads(ladder(14, "Seed v1", seed_below_ladder=False)))[1]
        self.assertEqual(unbounded_visits(old, "c"), 2 ** 15)  # what the merged search visited

    def test_more_than_64_known_paths_are_kept_at_64_and_reported_truncated(self):
        self.ladder_change_set("cs1", 7, seed_below_ladder=True)
        task = self.task("cs1", "c")
        self.assertTrue(task["paths_truncated"])
        self.assertEqual(len(task["affected_paths"]), recheck.MAX_PATHS)
        self.assertEqual(len({tuple(path) for path in task["affected_paths"]}), recheck.MAX_PATHS)
        for path in task["affected_paths"]:
            self.assertEqual((path[0], path[-1], len(path)), ("s", "c", 9))
        operations = self.store.get(REVIEWER, recheck.CHANGE_SET, "cs1")["path_enumeration"]["operations"]
        self.assertLess(operations, 2000)

    def test_exceeding_the_work_budget_refuses_the_change_set_atomically(self):
        self.fixtures()
        before = recheck.queue_view(self.store, REVIEWER)
        request = self.rid()
        with mock.patch.object(recheck, "MAX_PATH_WORK", 50):
            with self.assertRaises(WorkflowError) as caught:
                self.ladder_change_set("cs-big", 7, seed_below_ladder=True, request=request)
        self.assertEqual((caught.exception.code, caught.exception.fields),
                         ("TOO_LARGE", ["path_enumeration"]))
        self.assertIn("nothing was recorded", caught.exception.message)
        self.assertEqual(recheck.queue_view(self.store, REVIEWER), before)
        self.assertNotIn("cs-big", [item["change_set_id"] for item in
                                    self.store.list(REVIEWER, recheck.CHANGE_SET)])
        self.assertIsNone(self.store.recover(OPERATOR, request))
        with self.assertRaises(WorkflowError):
            self.task("cs-big", "c")
        # Under the real budget the same request ID now records the change set once.
        outcome = self.change("cs-big", "cs-big-old", "cs-big-new", {}, claims=["c"],
                              request=request)
        self.assertEqual(outcome["outcome"]["tasks"], [recheck.task_id_for("cs-big", "c")])

    def test_bounded_search_keeps_exactly_the_paths_the_merged_search_kept(self):
        generator = random.Random(20261013)
        for trial in range(300):
            names = [f"n{index}" for index in range(generator.randint(3, 9))]
            parents = {name: {other for other in names if other != name
                              and generator.random() < 0.3} for name in names}
            seeds = set(generator.sample(names, generator.randint(1, 3)))
            limit = generator.choice([1, 2, 3, 64])
            pruned = recheck._seed_reachable_parents(parents, seeds)
            for claim in names:
                with self.subTest(trial=trial, claim=claim),                         mock.patch.object(recheck, "MAX_PATHS", limit):
                    self.assertEqual(recheck._paths(pruned, seeds, claim, recheck._Work(10 ** 7)),
                                     merged_paths(parents, seeds, claim, limit))

    def test_budget_counts_operations_in_a_union_cycle(self):
        # Old x <- y and new y <- x are each acyclic; their union is a cycle the search must cut.
        parents = {"c": {"x"}, "x": {"y"}, "y": {"x", "s"}, "s": set(), "z": {"s"}}
        seeds = {"s"}
        pruned = recheck._seed_reachable_parents(parents, seeds)
        self.assertEqual(pruned, {"s": [], "z": ["s"], "y": ["s", "x"], "x": ["y"], "c": ["x"]})
        work = recheck._Work(100)
        self.assertEqual(recheck._paths(pruned, seeds, "c", work), ([["s", "y", "x", "c"]], False))
        self.assertEqual(work.used, 12)
        with self.assertRaises(WorkflowError) as caught:
            recheck._paths(pruned, seeds, "c", recheck._Work(11))
        self.assertEqual(caught.exception.code, "TOO_LARGE")
        self.assertEqual(recheck._paths(pruned, seeds, "q", recheck._Work(0)), ([], False))

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
