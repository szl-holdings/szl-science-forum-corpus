"""Build 2: human-owned evidence-change recheck queue.

Wraps the pinned SZL research-change-impact kernel (``kernels/research_change_impact.py``). The
kernel compares two declared research graphs over the union of their edges; this module keeps the
snapshots immutable, turns affected required claims into recheck tasks atomically, and records
human decisions. Nothing here fetches sources, watches anything, or decides that a claim is true.
"""

from __future__ import annotations

import hashlib

from .. import kernels
from .contracts import (EVIDENCE_KINDS, SCHEMA_VERSION, WorkflowError, bounded_text, canonical_bytes,
                        choice, exact_fields, fail, identifier, parse_json, typed_digest, utc_now,
                        utc_time)
from .permissions import require
from .store import Context, Store, Transaction, Write

SNAPSHOT = "evidence_snapshot"
CHANGE_SET = "change_set"
TASK = "recheck_task"
QUEUE = "recheck_queue"
QUEUE_ID = "recheck-queue"
KERNEL = "research_change_impact.py"
ADAPTER_REVISION = "change-recheck-adapter.v1"
COVERAGE = ("complete", "partial", "unknown")
EXECUTION = ("open", "in_review", "waiting", "resolved", "superseded")
CONDITIONS = ("access_limited", "unavailable_in_declared_scope", "insufficient_coverage",
              "underpowered", "condition_unsatisfied", "human_confirmed_satisfied", "unknown")
WAIT_CONDITIONS = tuple(name for name in CONDITIONS if name != "human_confirmed_satisfied")
TASK_STATES = ("RECHECK_REQUIRED", "MISSING_REQUIRED_CLAIM")
MAX_PATHS = 64
NEEDS_RECHECK = "Needs recheck"
UNCHANGED_OUTPUT = "Output unchanged; changed inputs still require review."
COVERAGE_POLICY = "every_recorded_change_set"
COVERAGE_POLICY_TEXT = (
    "Every change set recorded in this queue counts, including earlier ones and ones that created "
    "no task: a later comparison of other snapshots does not rediscover what an earlier partial or "
    "unknown comparison could have missed, and superseding its tasks does not complete it.")
COVERAGE_WARNING = ("Discovery coverage is incomplete for at least one recorded change set: an "
                    "absent task is not evidence that a claim is unaffected.")


def _kernel():
    try:
        return kernels.load(KERNEL)
    except kernels.KernelUnavailable as error:
        raise WorkflowError("KERNEL_UNAVAILABLE", f"pinned change-impact kernel is {error.code}")


def _refused(error: ValueError) -> WorkflowError:
    # Kernel messages can quote node identifiers; report only the refusal category.
    text = str(error)
    category = text.split(":", 1)[0] if ":" in text else text
    return WorkflowError("INVALID_INPUT", f"change-impact kernel refused the input: {category}")


def _cycle(graph: dict) -> list[str]:
    """Return one dependency cycle as a path, so a refusal can name it instead of dropping edges."""

    parents = {node["id"]: list(node.get("depends_on", [])) for node in graph["nodes"]}
    for link in graph.get("evidence_links", []):
        parents.setdefault(link["claim"], []).append(link["evidence"])
    state: dict[str, int] = {}
    for start in sorted(parents):
        stack = [(start, iter(sorted(parents.get(start, []))))]
        trail = [start]
        state[start] = 1
        while stack:
            node, remaining = stack[-1]
            parent = next(remaining, None)
            if parent is None:
                state[node] = 2
                stack.pop()
                trail.pop()
            elif state.get(parent) == 1:
                return trail[trail.index(parent):] + [parent]
            elif state.get(parent) is None and parent in parents:
                state[parent] = 1
                stack.append((parent, iter(sorted(parents[parent]))))
                trail.append(parent)
    return []


# ---- snapshots ----------------------------------------------------------------------------------

def register_snapshot(store: Store, context: Context, request_id: str, payload: dict) -> dict:
    """Admit one immutable, manually supplied research graph snapshot."""

    require(context, "register")
    exact_fields(payload, {"snapshot_id", "evidence_kind", "provenance", "coverage",
                           "graph_document"}, {"source_time", "disputed_edges"})
    snapshot_id = identifier(payload["snapshot_id"], "snapshot_id")
    evidence_kind = choice(payload["evidence_kind"], EVIDENCE_KINDS, "evidence_kind")
    provenance = bounded_text(payload["provenance"], "provenance", maximum=512)
    coverage = choice(payload["coverage"], COVERAGE, "coverage")
    source_time = (utc_time(payload["source_time"], "source_time")
                   if payload.get("source_time") is not None else None)
    raw = bounded_text(payload["graph_document"], "graph_document",
                       maximum=2_000_000).encode("utf-8")
    graph = parse_json(raw)
    kernel = _kernel()
    try:
        index, deps, order, _children = kernel.szl_change_graph(graph)
    except ValueError as error:
        if str(error).startswith("Cycle"):
            path = _cycle(graph)
            raise WorkflowError("INVALID_INPUT", "the dependency graph has a cycle",
                                fields=[f"cycle:{' -> '.join(path)}"] if path else [])
        raise _refused(error)
    if set(graph) - {"schema", "nodes", "evidence_links"}:
        fail("UNKNOWN_FIELD", "the graph has fields the kernel does not read", "graph_document")
    disputed = payload.get("disputed_edges", [])
    if not isinstance(disputed, list) or len(disputed) > 1000:
        fail("INVALID_INPUT", "disputed_edges must be a list of at most 1000 edges", "disputed_edges")
    seen = set()
    for number, edge in enumerate(disputed):
        where = f"disputed_edges[{number}]"
        exact_fields(edge, {"dependency", "dependent", "note"}, where=where)
        key = (edge["dependency"], edge["dependent"])
        if edge["dependent"] not in deps or edge["dependency"] not in deps[edge["dependent"]]:
            fail("INVALID_INPUT", "a disputed edge must be a declared edge", where)
        if key in seen:
            fail("INVALID_INPUT", "a disputed edge is listed twice", where)
        seen.add(key)
        bounded_text(edge["note"], f"{where}.note", maximum=1024)

    def apply(tx: Transaction):
        blob = tx.put_blob(raw, "evidence_snapshot")
        projection = {
            "schema_version": SCHEMA_VERSION, "workflow_kind": SNAPSHOT, "snapshot_id": snapshot_id,
            "created_by": context.principal_id, "evidence_kind": evidence_kind,
            "provenance": provenance, "coverage": coverage, "source_time": source_time,
            "captured_at": utc_now(),
            "graph": {
                "original": typed_digest(raw, "submitted graph document bytes", "none"),
                "canonical": typed_digest(canonical_bytes(graph), "parsed graph JSON",
                                          "sorted-keys compact UTF-8 JSON"),
                "blob": blob, "vocabulary": graph["schema"], "nodes": len(index),
                "edges": sum(len(parents) for parents in deps.values()), "order": order,
            },
            "disputed_edges": sorted(disputed, key=lambda e: (e["dependent"], e["dependency"])),
            "kernel_binding": dict(kernels.binding(KERNEL), operation="szl_change_graph",
                                   adapter_revision=ADAPTER_REVISION),
            "note": "Absence from a snapshot does not prove that a source was deleted.",
        }
        return ([Write(snapshot_id, SNAPSHOT, "snapshot_registered", projection,
                       {"original_sha256": projection["graph"]["original"]["value"],
                        "nodes": len(index), "coverage": coverage})],
                {"snapshot_id": snapshot_id})

    return store.command(context, request_id, "recheck.register_snapshot", {"payload": payload},
                         {snapshot_id: None}, apply)


# ---- change sets --------------------------------------------------------------------------------

def task_id_for(change_set_id: str, claim_id: str) -> str:
    """Stable task identity: one task per claim per change set, whatever the number of paths."""

    digest = hashlib.sha256(canonical_bytes([change_set_id, claim_id])).hexdigest()
    return f"task-{digest[:24]}"


def _union_parents(old: dict, new: dict) -> dict[str, set[str]]:
    return {key: set(old.get(key, set())) | set(new.get(key, set())) for key in set(old) | set(new)}


def _paths(parents: dict[str, set[str]], seeds: set[str], claim: str) -> tuple[list[list[str]], bool]:
    """All known seed-to-claim paths over the union of old and new edges, bounded."""

    paths: list[list[str]] = []
    truncated = False
    stack = [(claim, [claim])]
    while stack:
        node, suffix = stack.pop()
        if node in seeds:
            if len(paths) == MAX_PATHS:
                truncated = True
                break
            paths.append(list(suffix))
        for parent in sorted(parents.get(node, set()), reverse=True):
            if parent not in suffix:
                stack.append((parent, [parent] + suffix))
    return sorted(paths, key=lambda path: (len(path), path)), truncated


def _ancestors(parents: dict[str, set[str]], claim: str) -> set[str]:
    seen, stack = set(), [claim]
    while stack:
        for parent in parents.get(stack.pop(), set()):
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen


def create_change_set(store: Store, context: Context, request_id: str, payload: dict) -> dict:
    """Compare two exact snapshots and atomically retain the impact report plus its tasks."""

    require(context, "compute")
    exact_fields(payload, {"change_set_id", "baseline_snapshot_id", "current_snapshot_id",
                           "required_claims", "observed_digests", "as_of", "expected_queue_revision"})
    change_set_id = identifier(payload["change_set_id"], "change_set_id")
    baseline_id = identifier(payload["baseline_snapshot_id"], "baseline_snapshot_id")
    current_id = identifier(payload["current_snapshot_id"], "current_snapshot_id")
    as_of = utc_time(payload["as_of"], "as_of")
    queue_revision = payload["expected_queue_revision"]
    if queue_revision is not None and (type(queue_revision) is not int or queue_revision < 1):
        fail("INVALID_INPUT", "expected_queue_revision must be null or a positive integer",
             "expected_queue_revision")
    if baseline_id == current_id:
        fail("INVALID_INPUT", "baseline and current snapshots must differ", "current_snapshot_id")
    kernel = _kernel()
    baseline = store.get(context, SNAPSHOT, baseline_id)
    current = store.get(context, SNAPSHOT, current_id)
    old_graph = parse_json(store.blob(baseline["graph"]["blob"]))
    new_graph = parse_json(store.blob(current["graph"]["blob"]))
    document = {"schema": "szl.research-change-impact.v1", "baseline": old_graph,
                "current": new_graph, "observed_digests": payload["observed_digests"],
                "required_claims": payload["required_claims"], "as_of": as_of}
    try:
        report = kernel.szl_plan_research_changes(document)
        _, old_parents, _, _ = kernel.szl_change_graph(old_graph)
        _, new_parents, _, _ = kernel.szl_change_graph(new_graph)
    except ValueError as error:
        raise _refused(error)
    parents = _union_parents(old_parents, new_parents)
    seeds = {seed["id"] for seed in report["seeds"]}
    seed_reasons = {seed["id"]: seed["reasons"] for seed in report["seeds"]}
    disputed = {(edge["dependency"], edge["dependent"]): edge
                for edge in baseline["disputed_edges"] + current["disputed_edges"]}
    order = {claim: position for position, claim in enumerate(report["recheck_order"])}
    discovery = ("complete" if baseline["coverage"] == current["coverage"] == "complete"
                 else "incomplete")

    planned = []
    for claim in report["claims"]:
        reach = _ancestors(parents, claim["id"]) | {claim["id"]}
        disputes = sorted([{"dependency": a, "dependent": b, "note": edge["note"]}
                           for (a, b), edge in disputed.items() if b in reach],
                          key=lambda e: (e["dependent"], e["dependency"]))
        triggers = []
        if claim["state"] in TASK_STATES:
            triggers.append(claim["state"])
        if disputes:
            triggers.append("DISPUTED_EDGE")
        if not triggers:
            continue
        paths, truncated = _paths(parents, seeds, claim["id"])
        planned.append({
            "claim_id": claim["id"], "kernel_state": claim["state"], "triggers": triggers,
            "witness_path": claim["witness_path"], "affected_paths": paths,
            "paths_truncated": truncated,
            "seed_reasons": {node: seed_reasons[node] for node in sorted(
                {path[0] for path in paths})},
            "disputed_edges": disputes,
            "dependency_order": order.get(claim["id"]),
        })

    queue = None
    if queue_revision is not None:
        queue = store.get(context, QUEUE, QUEUE_ID)
    current_tasks = {} if queue is None else queue["current_task_by_claim"]
    expected: dict[str, int | None] = {QUEUE_ID: queue_revision, change_set_id: None}
    superseding = {}
    for task in planned:
        new_id = task_id_for(change_set_id, task["claim_id"])
        expected[new_id] = None
        previous = current_tasks.get(task["claim_id"])
        if previous is not None:
            expected[previous] = store.get(context, TASK, previous)["revision"]
            superseding[previous] = new_id

    def apply(tx: Transaction):
        live_queue = tx.get(QUEUE, QUEUE_ID) if queue_revision is not None else None
        if (live_queue or {}).get("current_task_by_claim", {}) != current_tasks:
            fail("STALE_REVISION", "the recheck queue changed while the change set was prepared")
        now = utc_now()
        bindings = {
            "baseline_snapshot": {"id": baseline_id,
                                  "canonical_sha256": baseline["graph"]["canonical"]["value"]},
            "current_snapshot": {"id": current_id,
                                 "canonical_sha256": current["graph"]["canonical"]["value"]},
            "kernel_input_sha256": report["input_sha256"],
        }
        writes = []
        created = []
        for task in planned:
            new_id = task_id_for(change_set_id, task["claim_id"])
            created.append(new_id)
            projection = dict(task, **{
                "schema_version": SCHEMA_VERSION, "workflow_kind": TASK, "task_id": new_id,
                "change_set_id": change_set_id, "created_by": context.principal_id,
                "input_bindings": bindings, "discovery_coverage": discovery,
                "execution": "open",
                "condition": "insufficient_coverage" if "DISPUTED_EDGE" in task["triggers"]
                             else "unknown",
                "label": NEEDS_RECHECK, "owner": None,
                "predicate": {"revision": 0, "text": None},
                "supersedes": next((old for old, new in superseding.items() if new == new_id), None),
                "superseded_by": None, "resolution": None, "history": [],
            })
            writes.append(Write(new_id, TASK, "task_created", projection,
                                {"claim_id": task["claim_id"], "change_set_id": change_set_id,
                                 "triggers": task["triggers"]}))
        for old_id, new_id in sorted(superseding.items()):
            old = tx.get(TASK, old_id)
            old.pop("revision")
            old["history"].append({"event": "superseded", "at": now, "by": context.principal_id,
                                   "prior_execution": old["execution"], "superseded_by": new_id})
            old["execution"] = "superseded"
            old["superseded_by"] = new_id
            writes.append(Write(old_id, TASK, "task_superseded", old,
                                {"superseded_by": new_id, "change_set_id": change_set_id}))
        change_set = {
            "schema_version": SCHEMA_VERSION, "workflow_kind": CHANGE_SET,
            "change_set_id": change_set_id, "created_by": context.principal_id,
            "recorded_at": now, "input_bindings": bindings,
            "required_claims": list(payload["required_claims"]),
            "observed_digests": dict(sorted(payload["observed_digests"].items())),
            "as_of": as_of, "report": report, "tasks": sorted(created),
            "superseded_tasks": sorted(superseding),
            "coverage": {"baseline": baseline["coverage"], "current": current["coverage"],
                         "discovery": discovery},
            "kernel_binding": dict(kernels.binding(KERNEL), operation="szl_plan_research_changes",
                                   adapter_revision=ADAPTER_REVISION, traversal="union_of_edges"),
            "limitations": [
                "Only declared nodes and edges are compared; synthetic coverage proves graph "
                "mechanics, not complete scientific dependency discovery.",
                UNCHANGED_OUTPUT,
            ],
        }
        writes.append(Write(change_set_id, CHANGE_SET, "change_set_recorded", change_set,
                            {"report_status": report["status"], "tasks": sorted(created),
                             "superseded": sorted(superseding)}))
        queue_projection = {
            "schema_version": SCHEMA_VERSION, "workflow_kind": QUEUE,
            "current_task_by_claim": dict(sorted(dict(current_tasks, **{
                task["claim_id"]: task_id_for(change_set_id, task["claim_id"])
                for task in planned}).items())),
            "change_sets": ([] if live_queue is None else live_queue["change_sets"]) + [change_set_id],
        }
        writes.append(Write(QUEUE_ID, QUEUE, "queue_updated", queue_projection,
                            {"change_set_id": change_set_id}))
        return writes, {"change_set_id": change_set_id, "tasks": sorted(created),
                        "superseded": sorted(superseding), "report_status": report["status"]}

    return store.command(context, request_id, "recheck.create_change_set", {"payload": payload},
                         expected, apply, declared={QUEUE_ID: queue_revision})


# ---- task events --------------------------------------------------------------------------------

def record_task_event(store: Store, context: Context, request_id: str, task_id: str,
                      expected_revision: int, event: dict) -> dict:
    """Append one explicit owner/reviewer transition. Nothing here resolves a task on its own."""

    require(context, "review")
    if not isinstance(event, dict) or "type" not in event:
        fail("INVALID_INPUT", "event requires a type")
    kind = choice(event["type"], ("assign", "start_review", "wait", "record_condition",
                                  "set_predicate", "resolve", "reopen"), "type")
    fields = {
        "assign": {"owner"}, "start_review": set(), "wait": {"condition", "note"},
        "record_condition": {"condition", "note"}, "set_predicate": {"text"},
        "resolve": {"evidence", "change_set_id", "predicate_revision"}, "reopen": {"reason"},
    }[kind]
    exact_fields(event, fields | {"type"})

    def apply(tx: Transaction):
        task = tx.get(TASK, task_id)
        task.pop("revision")
        execution = task["execution"]
        if execution == "superseded":
            fail("INVALID_INPUT", "this task was superseded by a later change set; act on the "
                                  "newer task", "task_id")
        now = utc_now()
        entry = {"event": kind, "at": now, "by": context.principal_id}
        if kind == "assign":
            task["owner"] = identifier(event["owner"], "owner")
            entry["owner"] = task["owner"]
        elif kind == "start_review":
            if execution not in ("open", "waiting"):
                fail("INVALID_INPUT", f"cannot start review from {execution}", "type")
            task["execution"] = "in_review"
        elif kind in ("wait", "record_condition"):
            if execution == "resolved":
                fail("INVALID_INPUT", "reopen a resolved task before changing it", "type")
            task["condition"] = choice(event["condition"], WAIT_CONDITIONS, "condition")
            entry.update(condition=task["condition"], note=bounded_text(event["note"], "note"))
            if kind == "wait":
                task["execution"] = "waiting"
        elif kind == "set_predicate":
            if execution == "resolved":
                fail("INVALID_INPUT", "reopen a resolved task before changing its predicate", "type")
            task["predicate"] = {"revision": task["predicate"]["revision"] + 1,
                                 "text": bounded_text(event["text"], "text", maximum=2000)}
            entry["predicate_revision"] = task["predicate"]["revision"]
        elif kind == "resolve":
            if task["kernel_state"] == "MISSING_REQUIRED_CLAIM":
                fail("INVALID_INPUT", "a removed required claim cannot be confirmed satisfied",
                     "type")
            if execution != "in_review":
                fail("INVALID_INPUT", "only a task in review can be resolved", "type")
            if event["change_set_id"] != task["change_set_id"]:
                fail("STALE_REVISION", "the resolution names a different change set",
                     "change_set_id")
            if task["predicate"]["revision"] == 0:
                fail("INVALID_INPUT", "record a human-specified predicate before resolving",
                     "predicate_revision")
            if event["predicate_revision"] != task["predicate"]["revision"]:
                fail("STALE_REVISION", "the resolution evaluated an older predicate",
                     "predicate_revision")
            task["execution"] = "resolved"
            task["condition"] = "human_confirmed_satisfied"
            task["resolution"] = {"by": context.principal_id, "at": now,
                                  "evidence": bounded_text(event["evidence"], "evidence"),
                                  "input_bindings": task["input_bindings"],
                                  "predicate": dict(task["predicate"]),
                                  "meaning": "the declared predicate was judged satisfied for these "
                                             "exact inputs; this does not make the claim true"}
        elif kind == "reopen":
            if execution != "resolved":
                fail("INVALID_INPUT", "only a resolved task can be reopened", "type")
            task["execution"] = "open"
            task["condition"] = "unknown"
            entry["reason"] = bounded_text(event["reason"], "reason")
        task["history"].append(entry)
        return ([Write(task_id, TASK, f"task_{kind}", task, dict(event, by=context.principal_id))],
                {"task_id": task_id, "execution": task["execution"],
                 "condition": task["condition"]})

    return store.command(context, request_id, f"recheck.task.{kind}",
                         {"task_id": task_id, "event": event}, {task_id: expected_revision}, apply)


# ---- reads --------------------------------------------------------------------------------------

def queue_view(store: Store, context: Context) -> dict:
    """The authorized queue with counts tied to one queue revision. Superseded tasks stay listed."""

    require(context, "read")
    try:
        queue = store.get(context, QUEUE, QUEUE_ID)
    except WorkflowError as error:
        if error.code != "NOT_FOUND":
            raise
        queue = None
    tasks = store.list(context, TASK)
    execution = {name: 0 for name in EXECUTION}
    condition = {name: 0 for name in CONDITIONS}
    for task in tasks:
        execution[task["execution"]] += 1
        if task["execution"] != "superseded":
            condition[task["condition"]] += 1
    # Coverage is a property of each comparison, not of the tasks it happened to create: a
    # partial or unknown change set with no task must still warn (see COVERAGE_POLICY_TEXT).
    queued = [] if queue is None else queue["change_sets"]
    recorded = {item["change_set_id"]: item for item in store.list(context, CHANGE_SET)}
    incomplete_sets = [
        {"change_set_id": change_set_id,
         "baseline": recorded[change_set_id]["coverage"]["baseline"],
         "current": recorded[change_set_id]["coverage"]["current"],
         "tasks": len(recorded[change_set_id]["tasks"])}
        for change_set_id in queued
        if change_set_id in recorded and recorded[change_set_id]["coverage"]["discovery"] != "complete"]
    active_incomplete = sum(1 for task in tasks if task["execution"] != "superseded"
                            and task["discovery_coverage"] != "complete")
    incomplete = bool(incomplete_sets) or active_incomplete > 0
    return {"queue_revision": None if queue is None else queue["revision"],
            "change_sets": [] if queue is None else queue["change_sets"],
            "current_task_by_claim": {} if queue is None else queue["current_task_by_claim"],
            "tasks": sorted(tasks, key=lambda task: (task["execution"] == "superseded",
                                                     task["dependency_order"] is None,
                                                     task["dependency_order"] or 0,
                                                     task["claim_id"], task["task_id"])),
            "counts": {"total": len(tasks), "execution": execution,
                       "condition_of_active_tasks": condition},
            "coverage": {"policy": COVERAGE_POLICY, "policy_text": COVERAGE_POLICY_TEXT,
                         "incomplete_change_sets": incomplete_sets,
                         "active_tasks_with_incomplete_discovery": active_incomplete},
            "coverage_warning": COVERAGE_WARNING if incomplete else None}
