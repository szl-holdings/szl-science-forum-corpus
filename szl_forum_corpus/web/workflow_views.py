"""Escaped Python HTML for the three science workflows.

Every value from a store, a fixture or a form passes through ``e()``. No view interpolates
trusted HTML from data, and status is always written as text, never shown by colour alone.
"""

from __future__ import annotations

import base64
from html import escape
import json
import secrets

from ..workflows import change_recheck, figure_check, paper_review
from ..workflows.contracts import LIMITATIONS

BASE = "/operator/science"
STYLE = """
:root{--ink:#080B12;--paper:#F5F3EE;--muted:#4A4F5C;--edge:#C9C4B8;--warn:#7A3E00;--bad:#8A1C1C;
--good:#1F5130;--panel:#FFFFFF}
@media (prefers-color-scheme:dark){:root{--ink:#F5F3EE;--paper:#080B12;--muted:#B9BCC6;
--edge:#3A3F4C;--warn:#F2B66B;--bad:#F29C9C;--good:#9FD8B0;--panel:#11151F}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header,main,footer{max-width:72rem;margin:0 auto;padding:1rem}
header{border-bottom:1px solid var(--edge)}nav a{margin-right:1rem}
a{color:inherit}.scope{font-weight:600;color:var(--warn)}
table{border-collapse:collapse;width:100%;margin:.5rem 0 1rem}
th,td{border:1px solid var(--edge);padding:.35rem .5rem;text-align:left;vertical-align:top}
th{background:var(--panel)}code,.hash{overflow-wrap:anywhere;word-break:break-all}
.status{font-weight:600}.status-fail{color:var(--bad)}.status-pass{color:var(--good)}
.status-warn{color:var(--warn)}section{margin:1.5rem 0}
details{border:1px solid var(--edge);background:var(--panel);padding:.5rem .75rem;margin:.5rem 0}
form.inline{display:inline}label{display:block;margin:.25rem 0}
input,select,textarea,button{font:inherit;color:inherit;background:var(--panel);
border:1px solid var(--edge);padding:.25rem .4rem;max-width:100%}
button{cursor:pointer}.notice{border:2px solid var(--warn);padding:.5rem .75rem}
.error{border:2px solid var(--bad);padding:.5rem .75rem}
img.figure{background:#FFFFFF;padding:.5rem;border:1px solid var(--edge)}pre{white-space:pre-wrap;overflow-wrap:anywhere}
@media (max-width:40rem){th,td{display:block}}
"""


def e(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def request_key() -> str:
    """A fresh idempotency key per rendered form; a double click resubmits the same key."""

    return secrets.token_hex(16)


def status(text: str) -> str:
    kind = ("pass" if text in ("PASSED", "AVAILABLE", "match", "accepted", "resolved") else
            "fail" if text in ("FAILED", "BLOCKED", "ERROR", "mismatch") else "warn")
    return f'<span class="status status-{kind}">{e(text)}</span>'


def layout(title: str, body: str, *, message: str | None = None, error: bool = False) -> str:
    banner = ""
    if message:
        banner = (f'<div class="{"error" if error else "notice"}" role="{"alert" if error else "status"}"'
                  f' tabindex="-1" id="message">{e(message)}</div>')
    limitations = "".join(f"<li>{e(item)}</li>" for item in LIMITATIONS)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{e(title)} · SZL science workflows</title><style>{STYLE}</style></head><body>"
        '<header><p class="scope">Private local workbench · owned or synthetic inputs only · '
        "unauthenticated local demo principal (not an identity)</p>"
        f'<nav aria-label="Workflows"><a href="{BASE}">Overview</a>'
        f'<a href="{BASE}/paper-review">Paper review</a>'
        f'<a href="{BASE}/rechecks">Evidence rechecks</a>'
        f'<a href="{BASE}/figures">Figure checks</a></nav></header>'
        f'<main id="main"><h1>{e(title)}</h1>{banner}{body}</main>'
        f"<footer><h2>Limitations</h2><ul>{limitations}</ul></footer></body></html>")


def hidden(csrf: str, request_id: str | None = None, **values: object) -> str:
    fields = {"csrf": csrf, "request_id": request_id or request_key(), **values}
    return "".join(f'<input type="hidden" name="{e(name)}" value="{e(value)}">'
                   for name, value in fields.items())


def options(values, selected: str | None = None, *, blank: bool = False) -> str:
    items = ['<option value="">Choose…</option>'] if blank else []
    for value in values:
        flag = " selected" if value == selected else ""
        items.append(f'<option value="{e(value)}"{flag}>{e(value)}</option>')
    return "".join(items)


def field(label: str, name: str, value: str = "", *, kind: str = "text", required: bool = True,
          maximum: int = 4000) -> str:
    flag = " required" if required else ""
    if kind == "textarea":
        return (f'<label>{e(label)}<br><textarea name="{e(name)}" rows="3" cols="60" '
                f'maxlength="{maximum}"{flag}>{e(value)}</textarea></label>')
    return (f'<label>{e(label)} <input type="{e(kind)}" name="{e(name)}" value="{e(value)}" '
            f'maxlength="{maximum}"{flag}></label>')


def select(label: str, name: str, values, selected: str | None = None, *,
           blank: bool = False) -> str:
    return (f'<label>{e(label)} <select name="{e(name)}" required>'
            f"{options(values, selected, blank=blank)}</select></label>")


def form(target: str, title: str, inner: str, csrf: str, retained: dict | None = None, *,
         key: str | None = None, **hidden_values: object) -> str:
    """One POST form. ``key`` names the intent so a rejected submission is retained in place."""

    request_id = None
    if retained and retained.get("_action") == (key or target):
        request_id = retained.get("request_id")
    return (f'<details{" open" if request_id else ""}><summary>{e(title)}</summary>'
            f'<form method="post" action="{e(target)}">{hidden(csrf, request_id, **hidden_values)}'
            f"{inner}<button type=\"submit\">{e(title)}</button></form></details>")


def kept(retained: dict | None, action: str, name: str, default: str = "") -> str:
    if retained and retained.get("_action") == action:
        return str(retained.get(name, default))
    return default


def json_block(value: object) -> str:
    return f"<pre>{e(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))}</pre>"


# ---- overview -----------------------------------------------------------------------------------

def overview(capabilities: list[dict], fixtures_available: bool, csrf: str,
             message: str | None = None, error: bool = False) -> str:
    rows = "".join(
        f"<tr><td><code>{e(item['kernel'])}</code></td><td>{status(item['state'])}</td>"
        f"<td><code>{e(item['repository'])}@{e(item['commit'][:12])}</code></td>"
        f'<td class="hash">{e(item["sha256"])}</td>'
        f'<td class="hash">{e(item["observed_sha256"] or "missing")}</td></tr>'
        for item in capabilities)
    if fixtures_available:
        loader = "".join(
            f'<form class="inline" method="post" action="{BASE}/fixtures">'
            f'{hidden(csrf, which=which)}<button type="submit">Load {e(label)}</button></form> '
            for which, label in (("paper", "synthetic paper case"),
                                 ("recheck", "synthetic research graphs"),
                                 ("figure", "synthetic figure bundles")))
    else:
        loader = ('<p class="status status-warn">BLOCKED: the bundled synthetic fixtures are not '
                  "present in this installation, so there is nothing to load.</p>")
    body = (
        "<section><h2>Pinned kernels</h2><table><thead><tr><th scope=\"col\">Kernel</th>"
        '<th scope="col">State</th><th scope="col">Source</th><th scope="col">Pinned SHA-256</th>'
        f'<th scope="col">Observed SHA-256</th></tr></thead><tbody>{rows}</tbody></table>'
        "<p>A kernel whose bytes differ from its pin is BLOCKED and is never imported.</p></section>"
        "<section><h2>Intake</h2><p>Uploads are not a qualified capability. Only the bundled, "
        f"hand-authored synthetic fixtures can be loaded.</p>{loader}</section>")
    return layout("Science workflows", body, message=message, error=error)


# ---- paper review -------------------------------------------------------------------------------

def paper_list(cases: list[dict]) -> str:
    if not cases:
        return layout("Paper review", "<p>No review cases yet. Load the synthetic paper case from "
                                      f'the <a href="{BASE}">overview</a>.</p>')
    rows = []
    for case in cases:
        summary = paper_review.summary(case)
        rows.append(
            f'<tr><td><a href="{BASE}/paper-review/{e(case["case_id"])}">{e(case["case_id"])}</a></td>'
            f"<td>{e(case['evidence_kind'])}</td><td>{e(summary['declared_reference_items'])}</td>"
            f"<td>{e(summary['initial_discrepancies'])}</td>"
            f"<td>{e(summary['final_qc'].get('pending', 0))}</td><td>{e(case['revision'])}</td></tr>")
    body = ('<table><thead><tr><th scope="col">Case</th><th scope="col">Evidence</th>'
            '<th scope="col">Declared reference items (denominator)</th>'
            '<th scope="col">Initial discrepancies</th><th scope="col">QC pending</th>'
            f'<th scope="col">Revision</th></tr></thead><tbody>{"".join(rows)}</tbody></table>')
    return layout("Paper review", body)


def _value(record: dict | None) -> str:
    if record is None:
        return "<em>no candidate (missing)</em>"
    if record["status"] == "UNRESOLVED":
        return f"<em>unresolved: {e(record['reason'])}</em>"
    parts = [f"<strong>{e(record['text'])}</strong>"]
    if record["kind"] == "cell":
        parts.append(f"{e(record['row_header'])} × {e(record['column_header'])} [{e(record['unit'])}]")
    parts.append(f"page {e(record['page'])}")
    return "<br>".join(parts)


def paper_case(case: dict, csrf: str, *, message: str | None = None, error: bool = False,
               retained: dict | None = None) -> str:
    summary = paper_review.summary(case)
    case_id = case["case_id"]
    action = f"{BASE}/paper-review/{case_id}"
    comparison = case["comparison"]
    rows = []
    for item_id, item in case["items"].items():
        finding = (comparison or {}).get("findings", {}).get(item_id, {})
        corrections = "".join(
            f"<li>{e(entry['id'])}: {status(entry['status'])} "
            f"{e(json.dumps(entry['before'], ensure_ascii=False))} → "
            f"{e(json.dumps(entry['after'], ensure_ascii=False))} ({e(entry['reason_code'])})</li>"
            for entry in item["corrections"])
        rows.append(
            f"<tr><th scope=\"row\"><code>{e(item_id)}</code><br>{e(item['reference_status'])}</th>"
            f"<td>{_value(item['initial_candidate'])}</td><td>{_value(item['reference'])}</td>"
            f"<td>{_value(item['current'])}<br>version {e(item['version'])}"
            f"{'<ul>' + corrections + '</ul>' if corrections else ''}</td>"
            f"<td>{e(finding.get('kernel_result', 'NOT_RUN'))}<br>extraction: "
            f"{e(finding.get('extraction', '—'))}<br>locator: {e(finding.get('locator', '—'))}</td>"
            f"<td>{e(item['adjudication']['state'])}</td><td>{status(item['qc']['state'])}</td></tr>")
    extras = "".join(f"<li><code>{e(extra['id'])}</code>: {_value(extra['candidate'])}</li>"
                     for extra in case["candidate_only_extras"]) or "<li>None</li>"
    items = list(case["items"])
    revision = case["revision"]
    forms = []
    if comparison is None:
        forms.append(form(f"{action}/comparisons", "Run the initial comparison", "", csrf, retained,
                          expected_revision=revision))
    else:
        act = f"{action}/review-events"

        def review(kind: str, title: str, build) -> str:
            key = f"{act}#{kind}"
            return form(act, title, build(lambda name, default="": kept(retained, key, name, default)),
                        csrf, retained, key=key, type=kind, expected_revision=revision)

        forms.append(review("propose_correction", "Propose a correction", lambda k: (
            select("Item", "item_id", items, k("item_id"), blank=True)
            + field("Correction ID", "correction_id", k("correction_id", "fix-" + request_key()[:8]),
                    maximum=64)
            + select("Field", "field", paper_review.CORRECTABLE_FIELDS, k("field"))
            + field("Corrected value", "value", k("value"), maximum=2048)
            + select("Reason", "reason_code", paper_review.CORRECTION_REASONS, k("reason_code"))
            + field("Source evidence", "evidence", k("evidence"), kind="textarea"))))
        forms.append(review("decide_correction", "Decide a correction", lambda k: (
            select("Item", "item_id", items, k("item_id"), blank=True)
            + field("Correction ID", "correction_id", k("correction_id"), maximum=64)
            + select("Decision", "decision", ("accept", "reject"), k("decision"), blank=True)
            + field("Reason", "reason", k("reason"), kind="textarea"))))
        forms.append(review("record_adjudication", "Record a domain adjudication", lambda k: (
            select("Item", "item_id", items, k("item_id"), blank=True)
            + select("State", "state", paper_review.ADJUDICATIONS, k("state"), blank=True)
            + field("Note", "note", k("note"), kind="textarea"))))
        forms.append(review("request_evidence", "Request further evidence", lambda k: (
            select("Item", "item_id", items, k("item_id"), blank=True)
            + field("Note", "note", k("note"), kind="textarea"))))
        forms.append(review("final_qc", "Record final QC", lambda k: (
            select("Item", "item_id", items, k("item_id"), blank=True)
            + select("Outcome", "outcome", paper_review.QC_OUTCOMES, k("outcome"), blank=True)
            + field("Reviewed item version", "reviewed_version", k("reviewed_version"),
                    kind="number", maximum=12)
            + field("Note", "note", k("note"), kind="textarea"))))
        forms.append(review("reopen", "Reopen final QC", lambda k: (
            select("Item", "item_id", items, k("item_id"), blank=True)
            + field("Reason", "reason", k("reason"), kind="textarea"))))
    sessions = []
    effort_action = f"{action}/effort-events"
    for session_id, session in case["effort"]["sessions"].items():
        measured = paper_review.session_effort(session)
        seconds = "unknown" if measured["active_seconds"] is None else f"{measured['active_seconds']} s"
        buttons = "".join(
            f'<form class="inline" method="post" action="{e(effort_action)}">'
            f"{hidden(csrf, action=verb, expected_revision=revision, session_id=session_id)}"
            f'<button type="submit">{e(verb.title())} {e(session_id)}</button></form> '
            for verb in ("pause", "resume", "stop"))
        sessions.append(f"<tr><td><code>{e(session_id)}</code></td><td>{e(session['actor'])}</td>"
                        f"<td>{e(session['activity'])}</td><td>{e(session['state'])}</td>"
                        f"<td>{e(seconds)} ({e(measured['basis'])})</td><td>{buttons}</td></tr>")
    start_key, amend_key = f"{effort_action}#start", f"{effort_action}#amend"
    effort_forms = (
        form(effort_action, "Start an effort session",
             select("Activity", "activity", paper_review.ACTIVITIES,
                    kept(retained, start_key, "activity")),
             csrf, retained, key=start_key, action="start", expected_revision=revision,
             session_id="s-" + request_key()[:10])
        + form(effort_action, "Amend a session's active time",
               field("Session ID", "session_id", kept(retained, amend_key, "session_id"), maximum=64)
               + field("Active seconds (blank = unknown)", "active_seconds",
                       kept(retained, amend_key, "active_seconds"), kind="number", required=False,
                       maximum=7)
               + field("Reason", "reason", kept(retained, amend_key, "reason"), kind="textarea"),
               csrf, retained, key=amend_key, action="amend", expected_revision=revision))
    staff = summary["staff_effort"]
    body = (
        f"<p>Case <code>{e(case_id)}</code> · revision {e(revision)} · {e(case['evidence_kind'])} "
        f"evidence · protocol {e(case['protocol_revision'])}</p>"
        "<section><h2>Source and reference</h2>"
        f"<p>PDF SHA-256 <span class=\"hash\">{e(case['source']['pdf_sha256'])}</span> · "
        f"{e(case['source']['declared_pages'])} declared pages · source access "
        f"{e(case['source']['access'])} · rights basis {e(case['source']['rights']['basis'])} "
        "(declared, not verified)</p>"
        f"<p>Reference: {e(case['reference_declaration']['annotation_basis'])}, "
        f"{e(case['reference_declaration']['reviewer_count'])} reviewers; independence declared "
        f"{e(case['reference_declaration']['independence_declared'])}; completeness declared "
        f"{e(case['reference_declaration']['completeness_declared'])}. "
        f"{e(case['reference_declaration']['note'])}</p></section>"
        "<section><h2>Summary (full declared denominator)</h2>"
        f"<p>{e(summary['declared_reference_items'])} declared reference items, "
        f"{e(summary['unresolved_reference_items'])} unresolved, "
        f"{e(summary['candidate_only_extras'])} candidate-only extras (outside the denominator). "
        f"Initial discrepancies: {e(summary['initial_discrepancies'])} (never rewritten by "
        f"corrections). Corrections: {e(json.dumps(summary['corrections']))}. Final QC: "
        f"{e(json.dumps(summary['final_qc']))}. Rates: {status(summary['rates']['state'])} — "
        f"{e(summary['rates']['reason'])}.</p>"
        f"<p>Staff effort known: {e(staff['known_seconds'])} s; sessions with unknown time: "
        f"{e(staff['unknown_sessions'])}. {e(staff['note'])}</p></section>"
        "<section><h2>Items: frozen initial versus current</h2><table><thead><tr>"
        '<th scope="col">Item</th><th scope="col">Initial candidate</th>'
        '<th scope="col">Declared reference</th><th scope="col">Current (after corrections)</th>'
        '<th scope="col">Initial comparison</th><th scope="col">Adjudication</th>'
        f'<th scope="col">Final QC</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
        f"<h3>Candidate-only extras</h3><ul>{extras}</ul></section>"
        f"<section><h2>Review</h2>{''.join(forms)}</section>"
        "<section><h2>Effort</h2><p>Closing this page does not stop, approve or discard a "
        "session; open sessions stay unknown until stopped or amended.</p>"
        '<table><thead><tr><th scope="col">Session</th><th scope="col">Reviewer</th>'
        '<th scope="col">Activity</th><th scope="col">State</th><th scope="col">Active time</th>'
        f'<th scope="col">Actions</th></tr></thead><tbody>{"".join(sessions)}</tbody></table>'
        f"{effort_forms}</section>")
    return layout(f"Paper review: {case_id}", body, message=message, error=error)


# ---- rechecks -----------------------------------------------------------------------------------

def recheck_queue(view: dict, snapshots: list[dict], csrf: str, *, message: str | None = None,
                  error: bool = False, retained: dict | None = None) -> str:
    rows = "".join(
        f'<tr><td><a href="{BASE}/rechecks/{e(task["task_id"])}">{e(task["claim_id"])}</a></td>'
        f"<td>{e(task['change_set_id'])}</td><td>{e(task['label'])}: "
        f"{e(', '.join(task['triggers']))}</td><td>{status(task['execution'])}</td>"
        f"<td>{e(task['condition'])}</td><td>{e(task['owner'] or 'unassigned')}</td>"
        f"<td>{e(len(task['affected_paths']))}</td></tr>"
        for task in view["tasks"])
    warning = ""
    if view["coverage_warning"]:
        gaps = "".join(
            f"<li><code>{e(item['change_set_id'])}</code>: baseline coverage {e(item['baseline'])}, "
            f"current coverage {e(item['current'])}, {e(item['tasks'])} task(s) created</li>"
            for item in view["coverage"]["incomplete_change_sets"])
        warning = (f'<div class="notice" role="status"><p>{e(view["coverage_warning"])}</p>'
                   f"{'<ul>' + gaps + '</ul>' if gaps else ''}"
                   f"<p>{e(view['coverage']['policy_text'])}</p></div>")
    snapshot_ids = [item["snapshot_id"] for item in snapshots]
    act = f"{BASE}/rechecks/change-sets"
    compare = ""
    if len(snapshot_ids) >= 2:
        compare = form(act, "Compare two registered snapshots",
                       field("Change-set ID", "change_set_id", kept(retained, act, "change_set_id", "cs-" + request_key()[:8]), maximum=64)
                       + select("Baseline snapshot", "baseline_snapshot_id", snapshot_ids, kept(retained, act, "baseline_snapshot_id"), blank=True)
                       + select("Current snapshot", "current_snapshot_id", snapshot_ids, kept(retained, act, "current_snapshot_id"), blank=True)
                       + field("Required claim IDs (JSON list)", "required_claims", kept(retained, act, "required_claims", '["c1","c2","c3","c4","c5"]'), kind="textarea")
                       + field("Observed digests (JSON object; null = access denied)", "observed_digests", kept(retained, act, "observed_digests", "{}"), kind="textarea", maximum=20000)
                       + field("As of (UTC YYYY-MM-DDTHH:MM:SSZ)", "as_of", kept(retained, act, "as_of", "2026-10-13T00:00:00Z"), maximum=20),
                       csrf, retained,
                       expected_queue_revision="" if view["queue_revision"] is None
                       else view["queue_revision"])
    body = (
        f"<p>Queue revision {e(view['queue_revision'] if view['queue_revision'] is not None else 'none')} · "
        f"{e(view['counts']['total'])} tasks · execution {e(json.dumps(view['counts']['execution']))}</p>"
        f"{warning}<p>“Needs recheck” never means “false”. A matching output digest does not "
        "resolve changed inputs.</p>"
        '<table><thead><tr><th scope="col">Claim</th><th scope="col">Change set</th>'
        '<th scope="col">Why</th><th scope="col">Execution</th><th scope="col">Evidence condition</th>'
        '<th scope="col">Owner</th><th scope="col">Known paths</th></tr></thead>'
        f'<tbody>{rows or "<tr><td colspan=7>No recheck tasks.</td></tr>"}</tbody></table>'
        f"<section><h2>Snapshots</h2><p>{e(', '.join(snapshot_ids) or 'none registered')}</p>"
        f"{compare}</section>")
    return layout("Evidence rechecks", body, message=message, error=error)


def recheck_task(task: dict, csrf: str, *, message: str | None = None, error: bool = False,
                 retained: dict | None = None) -> str:
    action = f"{BASE}/rechecks/{task['task_id']}/events"
    revision = task["revision"]
    paths = "".join(f"<li>{e(' → '.join(path))}</li>" for path in task["affected_paths"]) or \
        "<li>No changed ancestor reaches this claim; see the triggers.</li>"
    history = "".join(f"<li>{e(entry['at'])} · {e(entry['event'])} · {e(entry['by'])}</li>"
                      for entry in task["history"]) or "<li>None</li>"
    forms = ""
    if task["execution"] != "superseded":
        def event(kind: str, title: str, build, **extra: object) -> str:
            key = f"{action}#{kind}"
            return form(action, title, build(lambda name, default="": kept(retained, key, name, default)),
                        csrf, retained, key=key, type=kind, expected_revision=revision, **extra)

        forms = "".join([
            event("assign", "Assign an owner",
                  lambda k: field("Owner ID", "owner", k("owner"), maximum=64)),
            event("start_review", "Start review", lambda k: ""),
            event("wait", "Wait on a condition", lambda k: (
                select("Condition", "condition", change_recheck.WAIT_CONDITIONS, k("condition"),
                       blank=True)
                + field("Note", "note", k("note"), kind="textarea"))),
            event("record_condition", "Record an evidence condition", lambda k: (
                select("Condition", "condition", change_recheck.WAIT_CONDITIONS, k("condition"),
                       blank=True)
                + field("Note", "note", k("note"), kind="textarea"))),
            event("set_predicate", "Set the unblock predicate",
                  lambda k: field("Predicate", "text", k("text"), kind="textarea", maximum=2000)),
            event("resolve", "Record a human resolution",
                  lambda k: field("Evidence", "evidence", k("evidence"), kind="textarea"),
                  change_set_id=task["change_set_id"],
                  predicate_revision=task["predicate"]["revision"]),
            event("reopen", "Reopen",
                  lambda k: field("Reason", "reason", k("reason"), kind="textarea")),
        ])
    else:
        forms = (f"<p>Superseded by <a href=\"{BASE}/rechecks/{e(task['superseded_by'])}\">"
                 f"{e(task['superseded_by'])}</a>. Act on the newer task.</p>")
    resolution = json_block(task["resolution"]) if task["resolution"] else "<p>None.</p>"
    body = (
        f"<p>Claim <code>{e(task['claim_id'])}</code> · {e(task['label'])} · change set "
        f"<code>{e(task['change_set_id'])}</code> · revision {e(revision)}</p>"
        f"<p>Execution {status(task['execution'])} · evidence condition {e(task['condition'])} · "
        f"kernel state {e(task['kernel_state'])} · triggers {e(', '.join(task['triggers']))} · "
        f"discovery coverage {e(task['discovery_coverage'])}</p>"
        f"<section><h2>Known dependency paths (union of old and new edges)</h2><ul>{paths}</ul>"
        f"<p>Paths truncated: {e(task['paths_truncated'])}. Disputed edges: "
        f"{e(json.dumps(task['disputed_edges'], ensure_ascii=False))}</p></section>"
        f"<section><h2>Input bindings</h2>{json_block(task['input_bindings'])}</section>"
        f"<section><h2>Predicate</h2><p>Revision {e(task['predicate']['revision'])}: "
        f"{e(task['predicate']['text'] or 'not yet specified')}</p></section>"
        f"<section><h2>Resolution</h2>{resolution}</section>"
        f"<section><h2>Actions</h2>{forms}</section>"
        f"<section><h2>History</h2><ul>{history}</ul></section>")
    return layout(f"Recheck: {task['claim_id']}", body, message=message, error=error)


# ---- figures ------------------------------------------------------------------------------------

def figure_list(bundles: list[dict]) -> str:
    rows = "".join(
        f'<tr><td><a href="{BASE}/figures/{e(bundle["bundle_id"])}">{e(bundle["bundle_id"])}</a></td>'
        f"<td>{e(bundle['origin'])}</td><td>{e(bundle['derived_from'] or '—')}</td>"
        f"<td>{e(bundle['contract_state'])}</td>"
        f"<td>{status(bundle['latest_check']['execution']) if bundle['latest_check'] else 'NOT_RUN'}</td></tr>"
        for bundle in bundles)
    body = ("<p>Only the pinned kernel's canonical CSV-to-SVG scatter and line format is supported.</p>"
            '<table><thead><tr><th scope="col">Bundle</th><th scope="col">Origin</th>'
            '<th scope="col">Derived from</th><th scope="col">Contract</th>'
            '<th scope="col">Latest check of this version</th></tr></thead>'
            f'<tbody>{rows or "<tr><td colspan=5>No figure bundles.</td></tr>"}</tbody></table>')
    return layout("Figure checks", body)


def figure_bundle(bundle: dict, check: dict | None, svg: bytes | None, csrf: str, *,
                  message: str | None = None, error: bool = False,
                  retained: dict | None = None) -> str:
    bundle_id = bundle["bundle_id"]
    inventory = "".join(
        f"<tr><td>{e(role)}</td><td>{e(item['size'])}</td>"
        f'<td class="hash">{e(item["digest"]["value"])}</td></tr>'
        for role, item in bundle["inventory"].items())
    report = "<p>NOT_RUN: this version has not been checked.</p>"
    if check is not None:
        mismatch_rows = []
        for mismatch in check["mismatches"]:
            details = "".join(f"<li>{e(json.dumps(item, ensure_ascii=False, sort_keys=True))}</li>"
                              for item in mismatch.get("differences", []))
            mismatch_rows.append(
                f"<tr><td>{e(mismatch['member'])}</td><td>{e(mismatch['kernel_reason'])}</td>"
                f"<td>{e(mismatch['localization'])}{' (truncated)' if mismatch.get('truncated') else ''}"
                f"<ul>{details}</ul></td></tr>")
        table = ("" if not mismatch_rows else
                 '<table><thead><tr><th scope="col">Member</th><th scope="col">Kernel reason</th>'
                 f'<th scope="col">Where</th></tr></thead><tbody>{"".join(mismatch_rows)}</tbody></table>')
        report = (f"<p>{status(check['execution'])} · {e(check['statement'])} Check "
                  f"<code>{e(check['check_id'])}</code> at {e(check['checked_at'])}; plotted rows "
                  f"(n): {e(check['plotted_rows'])}; canonical format {e(check['canonical_format'])}.</p>"
                  f"<p>Source authenticity {e(check['states']['source_authenticity'])}; statistical "
                  f"interpretation {e(check['states']['statistical_interpretation'])}; human "
                  f"publication review {e(check['states']['human_publication_review'])}.</p>{table}")
    preview = ""
    if (svg is not None and bundle["origin"] == "rendered_by_pinned_kernel" and check is not None
            and check["execution"] == "PASSED"):
        data = base64.b64encode(svg).decode("ascii")
        # The kernel draws black strokes on a transparent canvas; keep a white backing in dark mode.
        preview = (f'<img class="figure" alt="Canonical figure rendered by the pinned kernel for {e(bundle_id)}" '
                   f'src="data:image/svg+xml;base64,{data}" width="640" height="480" '
                   'style="max-width:100%;height:auto">')
    elif svg is not None:
        preview = ("<p>Supplied SVG is shown as escaped source, never executed:</p>"
                   f"<pre>{e(svg.decode('utf-8', 'replace'))}</pre>")
    action = f"{BASE}/figures/{bundle_id}"
    forms = ""
    if bundle["contract_state"] == "LOADED":
        forms += form(action + "/renders", "Render a new canonical version", "", csrf, retained,
                      new_bundle_id=f"{bundle_id[:40]}.r{request_key()[:8]}")
    else:
        forms += ("<p>Render unavailable: the caption assertions disagree with the CSV, so no "
                  "output was written.</p>")
    if {"svg", "sidecar"} <= set(bundle["inventory"]):
        forms += form(action + "/checks", "Check this exact version", "", csrf, retained,
                      expected_revision=bundle["revision"], check_id="chk-" + request_key()[:12])
    body = (
        f"<p>Bundle <code>{e(bundle_id)}</code> · revision {e(bundle['revision'])} · origin "
        f"{e(bundle['origin'])} · derived from {e(bundle['derived_from'] or '—')} · contract "
        f"{e(bundle['contract_state'])}</p>"
        '<section><h2>Inventory</h2><table><thead><tr><th scope="col">Member</th>'
        f'<th scope="col">Bytes</th><th scope="col">SHA-256</th></tr></thead><tbody>{inventory}'
        f'</tbody></table><p><a href="/api/science/v1/figure-bundles/{e(bundle_id)}/download">'
        "Download the private closed-inventory package (JSON)</a></p></section>"
        f"<section><h2>Latest check of this version</h2>{report}</section>"
        f"<section><h2>Figure</h2>{preview or '<p>No SVG member.</p>'}</section>"
        f"<section><h2>Actions</h2>{forms}</section>")
    return layout(f"Figure bundle: {bundle_id}", body, message=message, error=error)


def failure_page(status_code: int, message: str) -> str:
    return layout("Request not completed",
                  f'<p>{e(message)}</p><p>HTTP {e(status_code)}. <a href="{BASE}">Return to the '
                  "overview</a> and inspect the current state before retrying.</p>", error=True)
