"""Build 1: paper extraction review ledger with corrections and honest effort accounting.

Wraps the pinned SZL paper-rescue kernel (``kernels/paper_rescue.py``). The kernel scores declared
candidate records against a separately declared reference; this module keeps the initial result
frozen, records human corrections and decisions as events, and accounts for review effort without
inferring work that was not observed. Nothing here reads PDF pixels or decides scientific truth.
"""

from __future__ import annotations

from datetime import datetime

from .. import kernels
from .contracts import (EVIDENCE_KINDS, SCHEMA_VERSION, WorkflowError, bounded_text, boolean,
                        canonical_bytes, choice, exact_fields, fail, identifier, parse_json,
                        typed_digest, utc_now)
from .permissions import require
from .store import Context, Store, Transaction, Write

KIND = "paper_review"
KERNEL = "paper_rescue.py"
ADAPTER_REVISION = "paper-review-adapter.v1"
MAPPING_VERSION = "paper-rescue-result-map.v1"

# Every kernel finding maps to explicit application dimensions. An unmapped code is refused.
# An unresolved reference gives the candidate's locator nothing to bind to, so it is ambiguous.
RESULT_MAP = {
    "MATCHED_DECLARED_REFERENCE": ("match", "bound"),
    "REFERENCE_CONTENT_MISMATCH": ("mismatch", "bound"),
    "REFERENCE_LOCATOR_MISMATCH": ("not_comparable_locator", "invalid"),
    "MISSING": ("missing_candidate", "missing"),
    "ABSTAINED": ("abstained_candidate", "missing"),
    "UNSUPPORTED_LOCATED_CLAIM": ("not_comparable_unresolved_reference", "ambiguous"),
}
CORRECTABLE_FIELDS = ("text", "row_header", "column_header", "unit")
CORRECTION_REASONS = ("value_error", "unit_error", "identity_error", "locator_error", "other")
ADJUDICATIONS = ("not_assessed", "unresolved_identity", "noncomparable_endpoint", "unresolved_unit",
                 "adjudicated_comparable")
QC_OUTCOMES = ("accepted", "correction_required", "unresolved")
ACTIVITIES = ("setup", "review", "repair", "final_qc", "wait")
STAFF_ACTIVITIES = ("setup", "review", "repair", "final_qc")
EFFORT_ACTIONS = ("start", "pause", "resume", "stop", "amend")
MAX_SECONDS = 7 * 24 * 3600


def _kernel():
    try:
        return kernels.load(KERNEL)
    except kernels.KernelUnavailable as error:
        raise WorkflowError("KERNEL_UNAVAILABLE", f"pinned paper-rescue kernel is {error.code}")


def _kernel_call(function, *args):
    try:
        return function(*args)
    except ValueError as error:
        # Kernel codes are short uppercase identifiers; pass them through, never the input.
        code = str(error) if str(error).isupper() else "KERNEL_REFUSED"
        fail("INVALID_INPUT", f"paper-rescue kernel refused the input: {code}")


# ---- registration -------------------------------------------------------------------------------

def register_case(store: Store, context: Context, request_id: str, payload: dict) -> dict:
    """Admit one immutable review case: candidate records, declared reference and protocol."""

    require(context, "register")
    exact_fields(payload, {"case_id", "protocol_revision", "evidence_kind", "provenance",
                           "records_document", "reference_document", "reference_independent",
                           "reference_complete", "source_access"})
    case_id = identifier(payload["case_id"], "case_id")
    protocol = bounded_text(payload["protocol_revision"], "protocol_revision", maximum=128)
    evidence_kind = choice(payload["evidence_kind"], EVIDENCE_KINDS, "evidence_kind")
    provenance = bounded_text(payload["provenance"], "provenance", maximum=512)
    independent = boolean(payload["reference_independent"], "reference_independent")
    complete = boolean(payload["reference_complete"], "reference_complete")
    access = choice(payload["source_access"], ("available", "unavailable"), "source_access")
    records_raw = bounded_text(payload["records_document"], "records_document",
                               maximum=2_000_000).encode("utf-8")
    reference_raw = bounded_text(payload["reference_document"], "reference_document",
                                 maximum=2_000_000).encode("utf-8")
    records = parse_json(records_raw)
    reference = parse_json(reference_raw)
    kernel = _kernel()
    if not isinstance(records, dict) or not isinstance(reference, dict):
        fail("INVALID_INPUT", "records and reference must be JSON objects")
    pdf_sha256 = records.get("pdf_sha256")
    candidates = _kernel_call(kernel.validate_records, records, pdf_sha256)
    references = _kernel_call(kernel.validate_gold, reference, pdf_sha256, records["declared_pages"])
    if any(candidates[name]["kind"] != references[name]["kind"]
           for name in set(candidates) & set(references)):
        fail("INVALID_INPUT", "paper-rescue kernel refused the input: KIND_MISMATCH")

    def apply(tx: Transaction):
        records_digest = tx.put_blob(records_raw, "paper_records")
        reference_digest = tx.put_blob(reference_raw, "paper_reference")
        items = {}
        for item_id, expected in sorted(references.items()):
            items[item_id] = {
                "reference": expected,
                "reference_status": "unresolved" if expected["status"] == "UNRESOLVED" else "located",
                "initial_candidate": candidates.get(item_id),
                "current": candidates.get(item_id),
                "corrections": [],
                "adjudication": {"state": "not_assessed", "history": []},
                "evidence_requests": [],
                "qc": {"state": "pending", "history": []},
                "version": 0,
            }
        extras = sorted(set(candidates) - set(references))
        projection = {
            "schema_version": SCHEMA_VERSION, "workflow_kind": KIND, "case_id": case_id,
            "created_by": context.principal_id, "evidence_kind": evidence_kind,
            "provenance": provenance, "protocol_revision": protocol,
            "source": {"pdf_sha256": pdf_sha256, "declared_pages": records["declared_pages"],
                       "access": access, "rights": records["rights"]},
            "inputs": {
                "records": typed_digest(records_raw, "submitted records document bytes", "none"),
                "records_blob": records_digest,
                "reference": typed_digest(reference_raw, "submitted reference document bytes", "none"),
                "reference_blob": reference_digest,
                "pipeline": records["pipeline"],
            },
            "reference_declaration": {
                "annotation_basis": reference["annotation_basis"],
                "reviewer_count": reference["reviewer_count"],
                "independence_declared": independent, "completeness_declared": complete,
                "note": "Declared by the submitter; not independently verified.",
            },
            "kernel_binding": dict(kernels.binding(KERNEL), adapter_revision=ADAPTER_REVISION,
                                   mapping_version=MAPPING_VERSION),
            "comparison": None,
            "items": items,
            "candidate_only_extras": [
                {"id": item_id, "candidate": candidates[item_id]} for item_id in extras],
            "effort": {"sessions": {}},
        }
        return ([Write(case_id, KIND, "case_registered", projection,
                       {"records_sha256": projection["inputs"]["records"]["value"],
                        "reference_sha256": projection["inputs"]["reference"]["value"],
                        "reference_items": len(items), "candidate_only_extras": len(extras)})],
                {"case_id": case_id})

    return store.command(context, request_id, "paper.register_case",
                         {"payload": payload}, {case_id: None}, apply)


# ---- comparison ---------------------------------------------------------------------------------

def run_comparison(store: Store, context: Context, request_id: str, case_id: str,
                   expected_revision: int) -> dict:
    """Score the frozen initial records once. A later correction can never rewrite this result."""

    require(context, "compute")
    kernel = _kernel()

    def apply(tx: Transaction):
        case = tx.get(KIND, case_id)
        if case["comparison"] is not None:
            fail("EXISTS", "the initial comparison is already recorded and is immutable")
        records = parse_json(tx.get_blob(case["inputs"]["records_blob"]))
        reference = parse_json(tx.get_blob(case["inputs"]["reference_blob"]))
        reference_ids = set(case["items"])
        # The kernel refuses unregistered candidates; extras stay in their own visible set.
        scored_records = dict(records, items=[item for item in records["items"]
                                              if item["id"] in reference_ids])
        report = _kernel_call(kernel.score, scored_records, reference, records["pdf_sha256"])
        findings = {}
        for finding in report["findings"]:
            if finding["result"] not in RESULT_MAP:
                raise WorkflowError("KERNEL_UNAVAILABLE",
                                    "the kernel returned a result this adapter does not map")
            extraction, locator = RESULT_MAP[finding["result"]]
            if case["source"]["access"] == "unavailable" and locator == "bound":
                locator = "source_unavailable"
            findings[finding["id"]] = {"kernel_result": finding["result"],
                                       "extraction": extraction, "locator": locator}
        if set(findings) != reference_ids:
            raise WorkflowError("KERNEL_UNAVAILABLE",
                                "the kernel report does not cover the declared reference")
        comparison = {
            "recorded_at": utc_now(),
            "kernel_status": report["status"], "kernel_counts": report["counts"],
            "denominator": report["denominator"], "findings": findings,
            "kernel_flags": {key: report[key] for key in sorted(report) if key.endswith("_verified")
                             or key == "clinical_use_qualified"},
            "kernel_binding": case["kernel_binding"],
        }
        projection = _strip(case)
        projection["comparison"] = comparison
        return ([Write(case_id, KIND, "initial_comparison_recorded", projection,
                       {"kernel_status": report["status"], "denominator": report["denominator"],
                        "counts": report["counts"]})],
                {"kernel_status": report["status"], "denominator": report["denominator"]})

    return store.command(context, request_id, "paper.run_comparison", {"case_id": case_id},
                         {case_id: expected_revision}, apply)


# ---- review events ------------------------------------------------------------------------------

def record_review_event(store: Store, context: Context, request_id: str, case_id: str,
                        expected_revision: int, event: dict) -> dict:
    """Append one explicit reviewer event. Model output can never call this on its own."""

    require(context, "review")
    if not isinstance(event, dict) or "type" not in event:
        fail("INVALID_INPUT", "event requires a type")
    kind = choice(event["type"], ("propose_correction", "decide_correction", "record_adjudication",
                                  "request_evidence", "final_qc", "reopen"), "type")

    def apply(tx: Transaction):
        case = tx.get(KIND, case_id)
        if case["comparison"] is None:
            fail("INVALID_INPUT", "record the initial comparison before reviewing")
        projection = _strip(case)
        handler = {"propose_correction": _propose, "decide_correction": _decide,
                   "record_adjudication": _adjudicate, "request_evidence": _request_evidence,
                   "final_qc": _final_qc, "reopen": _reopen}[kind]
        item_id, detail = handler(projection, event, context)
        return ([Write(case_id, KIND, f"review_{kind}", projection,
                       dict(detail, item_id=item_id, reviewer=context.principal_id))],
                {"item_id": item_id, "event": kind})

    return store.command(context, request_id, f"paper.review.{kind}",
                         {"case_id": case_id, "event": event}, {case_id: expected_revision}, apply)


def _item(projection: dict, event: dict) -> tuple[str, dict]:
    item_id = identifier(event.get("item_id"), "item_id")
    if item_id not in projection["items"]:
        fail("NOT_FOUND", "the item is not in this case's declared reference", "item_id")
    return item_id, projection["items"][item_id]


def _invalidate_qc(item: dict, reason: str) -> None:
    if item["qc"]["state"] != "pending":
        item["qc"]["history"].append({"state": "reopened", "reason": reason, "at": utc_now()})
        item["qc"]["state"] = "pending"


def _propose(projection: dict, event: dict, context: Context):
    exact_fields(event, {"type", "item_id", "correction_id", "changes", "reason_code", "evidence"})
    item_id, item = _item(projection, event)
    correction_id = identifier(event["correction_id"], "correction_id")
    if any(existing["id"] == correction_id for existing in item["corrections"]):
        fail("EXISTS", "this correction ID is already recorded", "correction_id")
    current = item["current"]
    if current is None or current["status"] == "UNRESOLVED":
        fail("INVALID_INPUT", "there is no located candidate value to correct; adjudicate instead",
             "item_id")
    changes = event["changes"]
    if not isinstance(changes, dict) or not changes:
        fail("INVALID_INPUT", "changes must name at least one field", "changes")
    allowed = CORRECTABLE_FIELDS if current["kind"] == "cell" else ("text",)
    for field, value in changes.items():
        if field not in allowed:
            fail("INVALID_INPUT", "this field cannot be corrected", f"changes.{field}")
        bounded_text(value, f"changes.{field}", maximum=2048)
        if value == current[field]:
            fail("INVALID_INPUT", "a correction must change the value", f"changes.{field}")
    item["corrections"].append({
        "id": correction_id, "status": "proposed", "author": context.principal_id,
        "proposed_at": utc_now(), "reason_code": choice(event["reason_code"], CORRECTION_REASONS,
                                                        "reason_code"),
        "evidence": bounded_text(event["evidence"], "evidence"),
        "before": {field: current[field] for field in sorted(changes)},
        "after": dict(sorted(changes.items())), "decision": None,
    })
    return item_id, {"correction_id": correction_id, "fields": sorted(changes)}


def _decide(projection: dict, event: dict, context: Context):
    exact_fields(event, {"type", "item_id", "correction_id", "decision", "reason"})
    item_id, item = _item(projection, event)
    correction_id = identifier(event["correction_id"], "correction_id")
    matches = [entry for entry in item["corrections"] if entry["id"] == correction_id]
    if not matches:
        fail("NOT_FOUND", "the correction was not found", "correction_id")
    correction = matches[0]
    if correction["status"] != "proposed":
        fail("INVALID_INPUT", "the correction was already decided", "correction_id")
    decision = choice(event["decision"], ("accept", "reject"), "decision")
    if decision == "accept" and any(item["current"][field] != before
                                    for field, before in correction["before"].items()):
        fail("STALE_REVISION", "the item changed after this correction was proposed",
             "correction_id")
    correction["status"] = "accepted" if decision == "accept" else "rejected"
    correction["decision"] = {"by": context.principal_id, "at": utc_now(),
                              "reason": bounded_text(event["reason"], "reason")}
    if decision == "accept":
        item["current"] = dict(item["current"], **correction["after"])
        item["version"] += 1
        _invalidate_qc(item, "an accepted correction changed the reviewed value")
    return item_id, {"correction_id": correction_id, "decision": decision}


def _adjudicate(projection: dict, event: dict, context: Context):
    exact_fields(event, {"type", "item_id", "state", "note"})
    item_id, item = _item(projection, event)
    state = choice(event["state"], ADJUDICATIONS, "state")
    item["adjudication"]["history"].append({"state": state, "by": context.principal_id,
                                            "at": utc_now(), "note": bounded_text(event["note"], "note")})
    if state != item["adjudication"]["state"]:
        item["adjudication"]["state"] = state
        item["version"] += 1
        _invalidate_qc(item, "the domain adjudication changed")
    return item_id, {"state": state}


def _request_evidence(projection: dict, event: dict, context: Context):
    exact_fields(event, {"type", "item_id", "note"})
    item_id, item = _item(projection, event)
    item["evidence_requests"].append({"by": context.principal_id, "at": utc_now(),
                                      "note": bounded_text(event["note"], "note")})
    return item_id, {}


def _final_qc(projection: dict, event: dict, context: Context):
    exact_fields(event, {"type", "item_id", "outcome", "reviewed_version", "note"})
    item_id, item = _item(projection, event)
    outcome = choice(event["outcome"], QC_OUTCOMES, "outcome")
    reviewed = event["reviewed_version"]
    if type(reviewed) is not int or reviewed != item["version"]:
        fail("STALE_REVISION", "final QC must bind the item's current version", "reviewed_version")
    if outcome == "accepted":
        finding = projection["comparison"]["findings"][item_id]
        accepted_correction = any(entry["status"] == "accepted" for entry in item["corrections"])
        if item["reference_status"] != "located":
            fail("INVALID_INPUT", "an unresolved reference cannot receive accepted QC", "outcome")
        if item["adjudication"]["state"] in ("unresolved_identity", "noncomparable_endpoint",
                                             "unresolved_unit"):
            fail("INVALID_INPUT", "an unresolved adjudication cannot receive accepted QC", "outcome")
        if finding["locator"] != "bound":
            fail("INVALID_INPUT", "accepted QC needs a bound locator on an accessible source",
                 "outcome")
        if finding["extraction"] != "match" and not accepted_correction:
            fail("INVALID_INPUT", "accepted QC needs a matching value or an accepted correction",
                 "outcome")
        if item["current"] is None or item["current"]["status"] == "UNRESOLVED":
            fail("INVALID_INPUT", "there is no value to accept", "outcome")
    item["qc"]["history"].append({"state": outcome, "by": context.principal_id, "at": utc_now(),
                                  "reviewed_version": reviewed,
                                  "note": bounded_text(event["note"], "note")})
    item["qc"]["state"] = outcome
    return item_id, {"outcome": outcome, "reviewed_version": reviewed}


def _reopen(projection: dict, event: dict, context: Context):
    exact_fields(event, {"type", "item_id", "reason"})
    item_id, item = _item(projection, event)
    if item["qc"]["state"] == "pending":
        fail("INVALID_INPUT", "final QC is already pending", "item_id")
    item["qc"]["history"].append({"state": "reopened", "by": context.principal_id, "at": utc_now(),
                                  "reason": bounded_text(event["reason"], "reason")})
    item["qc"]["state"] = "pending"
    return item_id, {}


# ---- effort -------------------------------------------------------------------------------------

def _refuse_if_running_elsewhere(tx: Transaction, context: Context, case_id: str) -> None:
    """Enforce one running session per reviewer across every case of the tenant.

    This runs inside the store's single-writer transaction (``BEGIN IMMEDIATE``), so no other
    command can start or resume a session between this read and the commit. It reads the
    sessions themselves, so sessions recorded before this check existed are covered too.
    """

    for other in tx.list(KIND):
        if other["case_id"] == case_id:
            continue  # the case being changed is checked from its in-memory projection
        if any(session["actor"] == context.principal_id and session["state"] == "running"
               for session in other["effort"]["sessions"].values()):
            raise WorkflowError("INVALID_INPUT", "stop or pause your running session in another "
                                "case first; one reviewer's time is never counted twice",
                                fields=["session_id", f"running_case:{other['case_id']}"])


def record_effort(store: Store, context: Context, request_id: str, case_id: str,
                  expected_revision: int, event: dict) -> dict:
    """Start, pause, resume, stop or amend an effort session using the server clock only."""

    require(context, "effort")
    action = choice(event.get("action") if isinstance(event, dict) else None, EFFORT_ACTIONS, "action")

    def apply(tx: Transaction):
        case = tx.get(KIND, case_id)
        projection = _strip(case)
        sessions = projection["effort"]["sessions"]
        now = utc_now()
        if action == "start":
            exact_fields(event, {"action", "session_id", "activity"})
            session_id = identifier(event["session_id"], "session_id")
            if session_id in sessions:
                fail("EXISTS", "this session ID is already recorded", "session_id")
            if any(s["actor"] == context.principal_id and s["state"] == "running"
                   for s in sessions.values()):
                fail("INVALID_INPUT", "stop or pause your running session first; one reviewer's "
                                      "time is never counted twice")
            _refuse_if_running_elsewhere(tx, context, case_id)
            sessions[session_id] = {"actor": context.principal_id,
                                    "activity": choice(event["activity"], ACTIVITIES, "activity"),
                                    "state": "running", "intervals": [[now, None]],
                                    "amendments": [], "capture": "server_clock"}
        else:
            required = {"action", "session_id"} | ({"active_seconds", "reason"} if action == "amend"
                                                   else set())
            exact_fields(event, required)
            session_id = identifier(event["session_id"], "session_id")
            session = sessions.get(session_id)
            if session is None:
                fail("NOT_FOUND", "the session was not found", "session_id")
            if session["actor"] != context.principal_id:
                fail("FORBIDDEN", "only the session's reviewer can change it", "session_id")
            if action in ("pause", "stop") and session["state"] == "running":
                session["intervals"][-1][1] = now
            elif action in ("pause", "stop") and not (action == "stop" and session["state"] == "paused"):
                fail("INVALID_INPUT", f"cannot {action} a {session['state']} session", "action")
            if action == "pause":
                session["state"] = "paused"
            elif action == "stop":
                session["state"] = "stopped"
            elif action == "resume":
                if session["state"] != "paused":
                    fail("INVALID_INPUT", "only a paused session can resume", "action")
                if any(other["actor"] == context.principal_id and other["state"] == "running"
                       for other in sessions.values()):
                    fail("INVALID_INPUT", "stop or pause your running session first; one "
                                          "reviewer's time is never counted twice")
                _refuse_if_running_elsewhere(tx, context, case_id)
                session["state"] = "running"
                session["intervals"].append([now, None])
            elif action == "amend":
                seconds = event["active_seconds"]
                if seconds is not None and (type(seconds) is not int or not 0 <= seconds <= MAX_SECONDS):
                    fail("INVALID_INPUT", "active_seconds must be null or 0..604800",
                         "active_seconds")
                session["amendments"].append({"active_seconds": seconds, "at": now,
                                              "reason": bounded_text(event["reason"], "reason")})
        return ([Write(case_id, KIND, f"effort_{action}", projection,
                       dict(event, actor=context.principal_id))], {"action": action})

    return store.command(context, request_id, f"paper.effort.{action}",
                         {"case_id": case_id, "event": event}, {case_id: expected_revision}, apply)


def _seconds(start: str, end: str) -> int:
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    return max(0, round((datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds()))


def session_effort(session: dict) -> dict:
    """Active seconds for one session. Open or interrupted sessions are unknown, never zero."""

    if session["amendments"]:
        latest = session["amendments"][-1]
        return {"active_seconds": latest["active_seconds"],
                "basis": "manual_amendment" if latest["active_seconds"] is not None else "unknown"}
    if session["state"] != "stopped":
        return {"active_seconds": None, "basis": "interrupted_or_open"}
    return {"active_seconds": sum(_seconds(start, end) for start, end in session["intervals"]),
            "basis": "server_clock"}


# ---- summary ------------------------------------------------------------------------------------

def summary(case: dict) -> dict:
    """Private summary with the full declared denominator; rates stay unavailable by design."""

    items = case["items"]
    comparison = case["comparison"]
    extraction, locator = {}, {}
    if comparison is not None:
        for finding in comparison["findings"].values():
            extraction[finding["extraction"]] = extraction.get(finding["extraction"], 0) + 1
            locator[finding["locator"]] = locator.get(finding["locator"], 0) + 1
    adjudication, qc = {}, {}
    for item in items.values():
        adjudication[item["adjudication"]["state"]] = adjudication.get(item["adjudication"]["state"], 0) + 1
        qc[item["qc"]["state"]] = qc.get(item["qc"]["state"], 0) + 1
    corrections = [entry for item in items.values() for entry in item["corrections"]]
    effort: dict = {}
    for session in case["effort"]["sessions"].values():
        bucket = effort.setdefault(session["activity"], {"known_seconds": 0, "unknown_sessions": 0,
                                                         "sessions": 0, "by_actor": {}})
        measured = session_effort(session)
        bucket["sessions"] += 1
        actor = bucket["by_actor"].setdefault(session["actor"], {"known_seconds": 0,
                                                                 "unknown_sessions": 0})
        if measured["active_seconds"] is None:
            bucket["unknown_sessions"] += 1
            actor["unknown_sessions"] += 1
        else:
            bucket["known_seconds"] += measured["active_seconds"]
            actor["known_seconds"] += measured["active_seconds"]
    initial_discrepancies = 0 if comparison is None else sum(
        1 for finding in comparison["findings"].values()
        if finding["extraction"] in ("mismatch", "not_comparable_locator",
                                     "not_comparable_unresolved_reference"))
    return {
        "case_id": case["case_id"], "revision": case["revision"],
        "declared_reference_items": len(items),
        "unresolved_reference_items": sum(1 for item in items.values()
                                          if item["reference_status"] == "unresolved"),
        "candidate_only_extras": len(case["candidate_only_extras"]),
        "comparison_recorded": comparison is not None,
        "initial_extraction": dict(sorted(extraction.items())),
        "initial_locator": dict(sorted(locator.items())),
        "initial_discrepancies": initial_discrepancies,
        "corrections": {"proposed": sum(1 for c in corrections if c["status"] == "proposed"),
                        "accepted": sum(1 for c in corrections if c["status"] == "accepted"),
                        "rejected": sum(1 for c in corrections if c["status"] == "rejected")},
        "adjudication": dict(sorted(adjudication.items())),
        "final_qc": dict(sorted(qc.items())),
        "effort": dict(sorted(effort.items())),
        "staff_effort": {
            "known_seconds": sum(effort[name]["known_seconds"] for name in STAFF_ACTIVITIES
                                 if name in effort),
            "unknown_sessions": sum(effort[name]["unknown_sessions"] for name in STAFF_ACTIVITIES
                                    if name in effort),
            "activities": list(STAFF_ACTIVITIES),
            "note": "Known seconds are a lower bound whenever any session is unknown.",
        },
        "rates": {"state": "UNAVAILABLE",
                  "reason": "no predeclared scoring policy or consequential-error definition"},
        "limitations": [
            "Corrections never change the initial comparison; the initial discrepancy count stays.",
            "Durations come from the server clock or a reasoned amendment; open, paused or "
            "interrupted sessions are unknown, not zero. Wait time is not staff effort.",
            "Software test timings and synthetic fixtures are not a human time study.",
        ],
    }


def _strip(resource: dict) -> dict:
    projection = dict(resource)
    projection.pop("revision", None)
    # Deep copy through canonical JSON so in-place edits never alias stored values.
    return parse_json(canonical_bytes(projection))
