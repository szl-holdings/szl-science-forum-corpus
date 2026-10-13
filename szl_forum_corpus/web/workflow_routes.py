"""Loopback-only operator server for the three science workflows (standard library only).

This is a local demonstration surface, not an authenticated service. It binds 127.0.0.1 only,
uses one fixed local demo principal that is labelled unauthenticated on every page, accepts no
uploads (only the bundled synthetic fixtures can be loaded), and never fetches anything. Every
state change goes through the same workflow functions and store the tests exercise directly.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import resources
from importlib.resources.abc import Traversable
import json
from pathlib import Path
import re
import secrets
import socket
import sys
from urllib.parse import parse_qsl

from .. import kernels
from ..workflows import change_recheck, figure_check, paper_review
from ..workflows.contracts import (ID_RE, WorkflowError, canonical_bytes, fail, parse_json,
                                   request_id as check_request_id)
from ..workflows.store import Context, Store
from . import workflow_views as views

BASE = views.BASE
API = "/api/science/v1"
DEMO_CONTEXT = Context("local-demo-operator", "local-demo", "operator")
MAX_FORM = 64 * 1024
MAX_JSON = 4 * 1024 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_FIELDS = 24
REPO_ROOT = Path(__file__).resolve().parents[2]


def bundled_fixtures() -> Traversable:
    """The synthetic fixtures shipped as package data, wherever the package is installed."""

    return resources.files("szl_forum_corpus.workflows").joinpath("fixtures")


DEFAULT_FIXTURES = bundled_fixtures()
# Recorded as each fixture record's provenance: the package-relative location of PROVENANCE.md.
FIXTURE_PROVENANCE = "szl_forum_corpus/workflows/fixtures/PROVENANCE.md"
ID = r"([A-Za-z][A-Za-z0-9_.-]{0,63})"
TASK_ID = r"(task-[0-9a-f]{24})"


class Rejected(Exception):
    """A request refused before any workflow code ran. Messages are fixed, never echo input."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "text/html; charset=utf-8"
    headers: dict = field(default_factory=dict)


ROUTES = [
    ("GET", re.compile(r"/\Z"), "overview"),
    ("GET", re.compile(BASE + r"\Z"), "overview"),
    ("GET", re.compile(BASE + r"/paper-review\Z"), "paper_list"),
    ("GET", re.compile(BASE + r"/paper-review/" + ID + r"\Z"), "paper_case"),
    ("GET", re.compile(BASE + r"/rechecks\Z"), "recheck_queue"),
    ("GET", re.compile(BASE + r"/rechecks/" + TASK_ID + r"\Z"), "recheck_task"),
    ("GET", re.compile(BASE + r"/figures\Z"), "figure_list"),
    ("GET", re.compile(BASE + r"/figures/" + ID + r"\Z"), "figure_bundle"),
    ("GET", re.compile(API + r"/session\Z"), "api_session"),
    ("GET", re.compile(API + r"/capabilities\Z"), "api_capabilities"),
    ("GET", re.compile(API + r"/paper-cases/" + ID + r"\Z"), "api_paper_case"),
    ("GET", re.compile(API + r"/paper-cases/" + ID + r"/summary\Z"), "api_paper_summary"),
    ("GET", re.compile(API + r"/change-sets/" + ID + r"\Z"), "api_change_set"),
    ("GET", re.compile(API + r"/recheck-tasks\Z"), "api_recheck_tasks"),
    ("GET", re.compile(API + r"/figure-checks/" + ID + r"\Z"), "api_figure_check"),
    ("GET", re.compile(API + r"/figure-bundles/" + ID + r"/download\Z"), "api_figure_download"),
    ("POST", re.compile(BASE + r"/fixtures\Z"), "form_fixtures"),
    ("POST", re.compile(BASE + r"/paper-review/" + ID + r"/comparisons\Z"), "form_comparison"),
    ("POST", re.compile(BASE + r"/paper-review/" + ID + r"/review-events\Z"), "form_review"),
    ("POST", re.compile(BASE + r"/paper-review/" + ID + r"/effort-events\Z"), "form_effort"),
    ("POST", re.compile(BASE + r"/rechecks/change-sets\Z"), "form_change_set"),
    ("POST", re.compile(BASE + r"/rechecks/" + TASK_ID + r"/events\Z"), "form_task_event"),
    ("POST", re.compile(BASE + r"/figures/" + ID + r"/renders\Z"), "form_render"),
    ("POST", re.compile(BASE + r"/figures/" + ID + r"/checks\Z"), "form_check"),
    ("POST", re.compile(API + r"/(paper-cases|evidence-snapshots|figure-bundles)\Z"), "api_intake"),
    ("POST", re.compile(API + r"/paper-cases/" + ID + r"/comparisons\Z"), "api_comparison"),
    ("POST", re.compile(API + r"/paper-cases/" + ID + r"/review-events\Z"), "api_review"),
    ("POST", re.compile(API + r"/paper-cases/" + ID + r"/effort-events\Z"), "api_effort"),
    ("POST", re.compile(API + r"/change-sets\Z"), "api_change_set_create"),
    ("POST", re.compile(API + r"/recheck-tasks/" + TASK_ID + r"/events\Z"), "api_task_event"),
    ("POST", re.compile(API + r"/figure-bundles/" + ID + r"/renders\Z"), "api_render"),
    ("POST", re.compile(API + r"/figure-bundles/" + ID + r"/checks\Z"), "api_check"),
]

REVIEW_FIELDS = {
    "propose_correction": {"item_id", "correction_id", "field", "value", "reason_code", "evidence"},
    "decide_correction": {"item_id", "correction_id", "decision", "reason"},
    "record_adjudication": {"item_id", "state", "note"},
    "request_evidence": {"item_id", "note"},
    "final_qc": {"item_id", "outcome", "reviewed_version", "note"},
    "reopen": {"item_id", "reason"},
}
EFFORT_FIELDS = {"start": {"session_id", "activity"}, "pause": {"session_id"},
                 "resume": {"session_id"}, "stop": {"session_id"},
                 "amend": {"session_id", "active_seconds", "reason"}}
TASK_FIELDS = {"assign": {"owner"}, "start_review": set(), "wait": {"condition", "note"},
               "record_condition": {"condition", "note"}, "set_predicate": {"text"},
               "resolve": {"evidence", "change_set_id", "predicate_revision"},
               "reopen": {"reason"}}
COMMON = {"csrf", "request_id"}


def _one(headers: dict[str, list[str]], name: str, required: bool = False) -> str | None:
    values = headers.get(name, [])
    if len(values) > 1 or (required and not values):
        raise Rejected(400, "Missing or duplicate request header.")
    return values[0] if values else None


def _integer(text: str, name: str) -> int:
    if not text.isascii() or not text.isdecimal() or len(text) > 12:
        fail("INVALID_INPUT", "expected a non-negative whole number", name)
    return int(text)


def parse_form(body: bytes) -> dict[str, str]:
    try:
        text = body.decode("utf-8", errors="strict")
        if re.search(r"%(?![0-9A-Fa-f]{2})", text):
            raise ValueError("bad percent encoding")
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True, encoding="utf-8",
                          errors="strict", max_num_fields=MAX_FIELDS)
    except (UnicodeError, ValueError):
        raise Rejected(400, "Malformed, duplicate or excessive form fields.")
    result: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in result or "\x00" in key or "\x00" in value:
            raise Rejected(400, "Malformed, duplicate or excessive form fields.")
        result[key] = value
    return result


def exact_form(form: dict[str, str], names: set[str]) -> None:
    if set(form) != names | COMMON:
        raise Rejected(400, "Unexpected or missing form fields.")


class WorkflowApp:
    """Route registration and request admission, independent of the socket server."""

    def __init__(self, store: Store, host: str, *,
                 fixtures: Path | Traversable | None = DEFAULT_FIXTURES,
                 context: Context = DEMO_CONTEXT):
        self.store = store
        self.host = host
        self.fixtures = fixtures
        self.context = context
        self.csrf = secrets.token_urlsafe(32)

    # ---- admission ------------------------------------------------------------------------

    def admit(self, method: str, target: str, headers: dict[str, list[str]]) -> tuple[str, tuple, int]:
        """Check origin, framing and route. Return the handler name, its groups and a body cap."""

        if sum(len(name) + sum(len(value) for value in values)
               for name, values in headers.items()) > MAX_HEADER_BYTES:
            raise Rejected(431, "Request headers are too large.")
        if _one(headers, "host", True) != self.host:
            raise Rejected(403, "This local origin is not allowed.")
        if headers.get("transfer-encoding") or headers.get("expect"):
            raise Rejected(400, "Unsupported request framing.")
        if _one(headers, "sec-fetch-site") not in (None, "same-origin", "none"):
            raise Rejected(403, "Cross-origin requests are not allowed.")
        if method not in ("GET", "HEAD", "POST"):
            raise Rejected(405, "Use an allowed page or form.")
        if any(character in target for character in "?#%\\") or "//" in target:
            raise Rejected(400, "Query strings and encoded paths are not accepted.")
        verb = "GET" if method == "HEAD" else method
        for route_method, pattern, name in ROUTES:
            match = pattern.match(target)
            if match:
                if route_method != verb:
                    continue
                break
        else:
            if any(pattern.match(target) for _, pattern, _ in ROUTES):
                raise Rejected(405, "This route does not accept that method.")
            raise Rejected(404, "Page not found.")
        length_text = _one(headers, "content-length", verb == "POST")
        if length_text is not None and not re.fullmatch(r"[0-9]{1,8}", length_text):
            raise Rejected(400, "Invalid content length.")
        length = int(length_text or 0)
        if verb == "GET":
            if length:
                raise Rejected(400, "GET requests cannot carry a body.")
            return name, match.groups(), 0
        if _one(headers, "origin", True) != "http://" + self.host:
            raise Rejected(403, "Submit from the local workbench page.")
        content_type = [part.strip().lower()
                        for part in (_one(headers, "content-type", True) or "").split(";")]
        api = name.startswith("api_")
        wanted = "application/json" if api else "application/x-www-form-urlencoded"
        if content_type not in ([wanted], [wanted, "charset=utf-8"]):
            raise Rejected(415, "Unsupported content type for this route.")
        if length > (MAX_JSON if api else MAX_FORM):
            raise Rejected(413, "The request body is too large.")
        if length == 0:
            raise Rejected(400, "The request body is empty.")
        if api:
            supplied = _one(headers, "x-szl-csrf", True) or ""
            if not supplied.isascii() or not secrets.compare_digest(supplied, self.csrf):
                raise Rejected(403, "Missing or invalid CSRF token.")
        return name, match.groups(), length

    def handle(self, method: str, target: str, headers: dict[str, list[str]],
               body: bytes, *, admitted: tuple | None = None) -> Response:
        api = target.startswith(API)
        try:
            name, groups, _ = admitted or self.admit(method, target, headers)
        except Rejected as error:
            return self._rejected(error, api)
        try:
            if name.startswith("form_"):
                form = parse_form(body)
                supplied = form.get("csrf", "")
                if not supplied.isascii() or not secrets.compare_digest(supplied, self.csrf):
                    raise Rejected(403, "Form expired or invalid. Reload the page and review "
                                        "before submitting.")
                return self._form(name, groups, form)
            if name.startswith("api_") and method == "POST":
                return self._api_post(name, groups, body)
            return getattr(self, name)(*groups)
        except Rejected as error:
            return self._rejected(error, api)
        except WorkflowError as error:
            if api:
                return self._json(error.http_status, error.envelope())
            return Response(error.http_status, views.failure_page(
                error.http_status, f"{error.code}: {error.message}").encode("utf-8"))

    def _rejected(self, error: Rejected, api: bool) -> Response:
        if api:
            code = {400: "INVALID_INPUT", 403: "FORBIDDEN", 404: "NOT_FOUND",
                    405: "METHOD_NOT_ALLOWED", 413: "TOO_LARGE", 415: "UNSUPPORTED",
                    431: "TOO_LARGE"}.get(error.status, "INVALID_INPUT")
            return self._json(error.status, {"error": {"code": code, "message": error.message,
                                                       "fields": [], "retryable": False,
                                                       "request_id": None}})
        # A rejected request never receives store data or a fresh token.
        return Response(error.status, views.failure_page(error.status, error.message).encode("utf-8"))

    @staticmethod
    def _json(status: int, value: object, headers: dict | None = None) -> Response:
        return Response(status, canonical_bytes(value) + b"\n", "application/json; charset=utf-8",
                        headers or {})

    @staticmethod
    def _html(text: str, status: int = 200) -> Response:
        return Response(status, text.encode("utf-8"))

    # ---- pages ----------------------------------------------------------------------------

    def overview(self, message: str | None = None, error: bool = False, retained=None,
                 status: int = 200):
        return self._html(views.overview(kernels.capability_manifest(), self._fixtures_ready(),
                                         self.csrf, message, error), status)

    def paper_list(self):
        return self._html(views.paper_list(self.store.list(self.context, paper_review.KIND)))

    def paper_case(self, case_id, message=None, error=False, retained=None, status=200):
        case = self.store.get(self.context, paper_review.KIND, case_id)
        return self._html(views.paper_case(case, self.csrf, message=message, error=error,
                                           retained=retained), status)

    def recheck_queue(self, message=None, error=False, retained=None, status=200):
        return self._html(views.recheck_queue(
            change_recheck.queue_view(self.store, self.context),
            self.store.list(self.context, change_recheck.SNAPSHOT), self.csrf, message=message,
            error=error, retained=retained), status)

    def recheck_task(self, task_id, message=None, error=False, retained=None, status=200):
        task = self.store.get(self.context, change_recheck.TASK, task_id)
        return self._html(views.recheck_task(task, self.csrf, message=message, error=error,
                                             retained=retained), status)

    def figure_list(self):
        return self._html(views.figure_list(self.store.list(self.context, figure_check.BUNDLE)))

    def figure_bundle(self, bundle_id, message=None, error=False, retained=None, status=200):
        bundle = self.store.get(self.context, figure_check.BUNDLE, bundle_id)
        check = None
        if bundle["latest_check"]:
            check = self.store.get(self.context, figure_check.CHECK,
                                   bundle["latest_check"]["check_id"])
        svg = (self.store.blob(bundle["inventory"]["svg"]["blob"])
               if "svg" in bundle["inventory"] else None)
        return self._html(views.figure_bundle(bundle, check, svg, self.csrf, message=message,
                                              error=error, retained=retained), status)

    # ---- JSON reads -----------------------------------------------------------------------

    def api_session(self):
        return self._json(200, {"csrf": self.csrf, "principal": self.context.principal_id,
                                "authenticated": False,
                                "note": "Fixed local demo principal; not an identity."})

    def api_capabilities(self):
        return self._json(200, {"kernels": kernels.capability_manifest(),
                                "fixtures": "AVAILABLE" if self._fixtures_ready() else "BLOCKED",
                                "uploads": "UNAVAILABLE", "network_acquisition": "UNAVAILABLE",
                                "model_inference": "UNAVAILABLE"})

    def api_paper_case(self, case_id):
        return self._json(200, self.store.get(self.context, paper_review.KIND, case_id))

    def api_paper_summary(self, case_id):
        return self._json(200, paper_review.summary(
            self.store.get(self.context, paper_review.KIND, case_id)))

    def api_change_set(self, change_set_id):
        return self._json(200, self.store.get(self.context, change_recheck.CHANGE_SET, change_set_id))

    def api_recheck_tasks(self):
        return self._json(200, change_recheck.queue_view(self.store, self.context))

    def api_figure_check(self, check_id):
        return self._json(200, self.store.get(self.context, figure_check.CHECK, check_id))

    def api_figure_download(self, bundle_id):
        return self._json(200, figure_check.package(self.store, self.context, bundle_id), {
            "Content-Disposition": 'attachment; filename="figure-bundle-package.json"'})

    # ---- forms ----------------------------------------------------------------------------

    def _form(self, name: str, groups: tuple, form: dict[str, str]) -> Response:
        kind = form.get("type") or form.get("action") or ""
        page, args, key = self._form_page(name, groups, kind)
        try:
            check_request_id(form.get("request_id"))
            location = getattr(self, name)(*groups, form)
        except WorkflowError as error:
            stale = error.code in ("STALE_REVISION", "REQUEST_ID_CONFLICT", "EXISTS")
            message = (f"{error.code}: the state changed. Your unsaved values are kept below; review "
                       "the current state before submitting a new decision."
                       if stale else f"{error.code}: {error.message}")
            retained = {k: v for k, v in form.items() if k != "csrf"}
            retained["_action"] = key
            try:
                return getattr(self, page)(*args, message=message, error=True, retained=retained,
                                           status=error.http_status)
            except WorkflowError:
                raise error
        return Response(303, b"", headers={"Location": location})

    @staticmethod
    def _form_page(name: str, groups: tuple, kind: str) -> tuple[str, tuple, str]:
        if name == "form_fixtures":
            return "overview", (), BASE + "/fixtures"
        if name in ("form_comparison", "form_review", "form_effort"):
            suffix = {"form_comparison": "/comparisons", "form_review": "/review-events",
                      "form_effort": "/effort-events"}[name]
            key = f"{BASE}/paper-review/{groups[0]}{suffix}"
            return "paper_case", groups, key + (f"#{kind}" if name != "form_comparison" else "")
        if name == "form_change_set":
            return "recheck_queue", (), f"{BASE}/rechecks/change-sets"
        if name == "form_task_event":
            return "recheck_task", groups, f"{BASE}/rechecks/{groups[0]}/events#{kind}"
        suffix = "/renders" if name == "form_render" else "/checks"
        return "figure_bundle", groups, f"{BASE}/figures/{groups[0]}{suffix}"

    def form_fixtures(self, form):
        exact_form(form, {"which"})
        which = form["which"]
        if which not in ("paper", "recheck", "figure"):
            raise Rejected(400, "Unknown fixture.")
        return self.load_fixture(which)

    def form_comparison(self, case_id, form):
        exact_form(form, {"expected_revision"})
        paper_review.run_comparison(self.store, self.context, form["request_id"], case_id,
                                    _integer(form["expected_revision"], "expected_revision"))
        return f"{BASE}/paper-review/{case_id}"

    def form_review(self, case_id, form):
        kind = form.get("type", "")
        if kind not in REVIEW_FIELDS:
            raise Rejected(400, "Unknown review event.")
        exact_form(form, REVIEW_FIELDS[kind] | {"type", "expected_revision"})
        event = {key: form[key] for key in REVIEW_FIELDS[kind] | {"type"}}
        if kind == "propose_correction":
            event = {"type": kind, "item_id": form["item_id"],
                     "correction_id": form["correction_id"],
                     "changes": {form["field"]: form["value"]},
                     "reason_code": form["reason_code"], "evidence": form["evidence"]}
        elif kind == "final_qc":
            event["reviewed_version"] = _integer(form["reviewed_version"], "reviewed_version")
        paper_review.record_review_event(self.store, self.context, form["request_id"], case_id,
                                         _integer(form["expected_revision"], "expected_revision"),
                                         event)
        return f"{BASE}/paper-review/{case_id}"

    def form_effort(self, case_id, form):
        action = form.get("action", "")
        if action not in EFFORT_FIELDS:
            raise Rejected(400, "Unknown effort action.")
        exact_form(form, EFFORT_FIELDS[action] | {"action", "expected_revision"})
        event = {key: form[key] for key in EFFORT_FIELDS[action] | {"action"}}
        if action == "amend":
            text = form["active_seconds"].strip()
            event["active_seconds"] = None if text == "" else _integer(text, "active_seconds")
        paper_review.record_effort(self.store, self.context, form["request_id"], case_id,
                                   _integer(form["expected_revision"], "expected_revision"), event)
        return f"{BASE}/paper-review/{case_id}"

    def form_change_set(self, form):
        exact_form(form, {"change_set_id", "baseline_snapshot_id", "current_snapshot_id",
                          "required_claims", "observed_digests", "as_of", "expected_queue_revision"})
        revision = form["expected_queue_revision"].strip()
        payload = {key: form[key] for key in ("change_set_id", "baseline_snapshot_id",
                                              "current_snapshot_id", "as_of")}
        payload["required_claims"] = parse_json(form["required_claims"].encode("utf-8"), limit=MAX_FORM)
        payload["observed_digests"] = parse_json(form["observed_digests"].encode("utf-8"),
                                                 limit=MAX_FORM)
        payload["expected_queue_revision"] = (None if revision == ""
                                              else _integer(revision, "expected_queue_revision"))
        change_recheck.create_change_set(self.store, self.context, form["request_id"], payload)
        return f"{BASE}/rechecks"

    def form_task_event(self, task_id, form):
        kind = form.get("type", "")
        if kind not in TASK_FIELDS:
            raise Rejected(400, "Unknown task event.")
        exact_form(form, TASK_FIELDS[kind] | {"type", "expected_revision"})
        event = {key: form[key] for key in TASK_FIELDS[kind] | {"type"}}
        if kind == "resolve":
            event["predicate_revision"] = _integer(form["predicate_revision"], "predicate_revision")
        change_recheck.record_task_event(self.store, self.context, form["request_id"], task_id,
                                         _integer(form["expected_revision"], "expected_revision"),
                                         event)
        return f"{BASE}/rechecks/{task_id}"

    def form_render(self, bundle_id, form):
        exact_form(form, {"new_bundle_id"})
        figure_check.render_bundle(self.store, self.context, form["request_id"], bundle_id,
                                   form["new_bundle_id"])
        return f"{BASE}/figures/{form['new_bundle_id']}"

    def form_check(self, bundle_id, form):
        exact_form(form, {"expected_revision", "check_id"})
        figure_check.check_bundle(self.store, self.context, form["request_id"], bundle_id,
                                  _integer(form["expected_revision"], "expected_revision"),
                                  form["check_id"])
        return f"{BASE}/figures/{bundle_id}"

    # ---- JSON commands --------------------------------------------------------------------

    def _api_post(self, name: str, groups: tuple, body: bytes) -> Response:
        if name == "api_intake":
            raise WorkflowError("CAPABILITY_UNAVAILABLE",
                                "uploads are not a qualified capability here; load the bundled "
                                "synthetic fixtures from the overview page instead")
        value = parse_json(body, limit=MAX_JSON)
        if not isinstance(value, dict):
            fail("INVALID_INPUT", "expected a JSON object")
        request = check_request_id(value.get("request_id"))
        result = getattr(self, name)(*groups, request, value)
        return self._json(200, result)

    @staticmethod
    def _fields(value: dict, names: set[str]) -> dict:
        if set(value) != names | {"request_id"}:
            fail("INVALID_INPUT", "unexpected or missing request fields")
        return value

    def api_comparison(self, case_id, request, value):
        self._fields(value, {"expected_revision"})
        return paper_review.run_comparison(self.store, self.context, request, case_id,
                                           value["expected_revision"])

    def api_review(self, case_id, request, value):
        self._fields(value, {"expected_revision", "event"})
        return paper_review.record_review_event(self.store, self.context, request, case_id,
                                                value["expected_revision"], value["event"])

    def api_effort(self, case_id, request, value):
        self._fields(value, {"expected_revision", "event"})
        return paper_review.record_effort(self.store, self.context, request, case_id,
                                          value["expected_revision"], value["event"])

    def api_change_set_create(self, request, value):
        self._fields(value, {"payload"})
        return change_recheck.create_change_set(self.store, self.context, request, value["payload"])

    def api_task_event(self, task_id, request, value):
        self._fields(value, {"expected_revision", "event"})
        return change_recheck.record_task_event(self.store, self.context, request, task_id,
                                                value["expected_revision"], value["event"])

    def api_render(self, bundle_id, request, value):
        self._fields(value, {"new_bundle_id"})
        return figure_check.render_bundle(self.store, self.context, request, bundle_id,
                                          value["new_bundle_id"])

    def api_check(self, bundle_id, request, value):
        self._fields(value, {"expected_revision", "check_id"})
        return figure_check.check_bundle(self.store, self.context, request, bundle_id,
                                         value["expected_revision"], value["check_id"])

    # ---- bundled synthetic fixtures --------------------------------------------------------

    def _fixtures_ready(self) -> bool:
        return self.fixtures is not None and self.fixtures.joinpath("PROVENANCE.md").is_file()

    def _fixture(self, *parts: str) -> str:
        if not self._fixtures_ready():
            raise WorkflowError("CAPABILITY_UNAVAILABLE", "the bundled synthetic fixtures are not "
                                                          "present in this installation")
        location = self.fixtures
        for part in parts:  # one segment at a time: Traversable.joinpath may take only one
            location = location.joinpath(part)
        return location.read_text(encoding="utf-8")

    @staticmethod
    def _fixture_request(*parts: str) -> str:
        # Deterministic keys make reloading a fixture a replay, never a duplicate.
        return hashlib.sha256(":".join(("fixture",) + parts).encode("utf-8")).hexdigest()[:32]

    def load_fixture(self, which: str) -> str:
        provenance = FIXTURE_PROVENANCE
        ctx, store, key = self.context, self.store, self._fixture_request
        if which == "paper":
            paper_review.register_case(store, ctx, key("paper", "case"), {
                "case_id": "demo-paper", "protocol_revision": "synthetic-protocol.v1",
                "evidence_kind": "synthetic", "provenance": provenance,
                "records_document": self._fixture("paper_review", "records.json"),
                "reference_document": self._fixture("paper_review", "reference.json"),
                "reference_independent": False, "reference_complete": True,
                "source_access": "available"})
            paper_review.run_comparison(store, ctx, key("paper", "comparison"), "demo-paper", 1)
            return f"{BASE}/paper-review/demo-paper"
        if which == "recheck":
            expected = json.loads(self._fixture("change_recheck", "expected.json"))
            for snapshot_id, name, disputed in (
                    ("demo-v1", "baseline.json", []), ("demo-v2", "current.json", []),
                    ("demo-v3", "later.json", [{"dependency": "d2", "dependent": "r1",
                                                "note": "synthetic disputed mapping"}])):
                change_recheck.register_snapshot(store, ctx, key("recheck", snapshot_id), {
                    "snapshot_id": snapshot_id, "evidence_kind": "synthetic",
                    "provenance": provenance, "coverage": "complete",
                    "graph_document": self._fixture("change_recheck", name),
                    "disputed_edges": disputed})
            change_recheck.create_change_set(store, ctx, key("recheck", "cs1"), {
                "change_set_id": "demo-cs1", "baseline_snapshot_id": "demo-v1",
                "current_snapshot_id": "demo-v2", "required_claims": ["c1", "c2", "c3", "c4", "c5"],
                "observed_digests": expected["first"]["observed_digests"],
                "as_of": "2026-10-13T00:00:00Z", "expected_queue_revision": None})
            return f"{BASE}/rechecks"
        members = [{"role": "spec", "content": self._fixture("figure_check", "spec.json")},
                   {"role": "data", "content": self._fixture("figure_check", "data.csv")}]
        figure_check.register_bundle(store, ctx, key("figure", "base"), {
            "bundle_id": "demo-figure", "evidence_kind": "synthetic", "provenance": provenance,
            "members": members})
        figure_check.render_bundle(store, ctx, key("figure", "render"), "demo-figure",
                                   "demo-figure.r1")
        figure_check.check_bundle(store, ctx, key("figure", "check"), "demo-figure.r1", 1,
                                  "demo-figure.r1.check")
        rendered = figure_check.package(store, ctx, "demo-figure.r1")["members"]
        moved = rendered["svg"]["content"].replace('cy="244.179104"', 'cy="236.000000"')
        figure_check.register_bundle(store, ctx, key("figure", "edited"), {
            "bundle_id": "demo-figure.edited", "evidence_kind": "synthetic",
            "provenance": provenance + " (synthetic edit: point s3 moved)",
            "derived_from": "demo-figure.r1",
            "members": members + [{"role": "svg", "content": moved},
                                  {"role": "sidecar", "content": rendered["sidecar"]["content"]}]})
        figure_check.check_bundle(store, ctx, key("figure", "edited-check"), "demo-figure.edited",
                                  1, "demo-figure.edited.check")
        return f"{BASE}/figures"


# ---- socket server ------------------------------------------------------------------------------

SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0", "Pragma": "no-cache",
    # "no-referrer" makes Chromium send "Origin: null" on same-origin form posts, which the
    # Origin check must then refuse; "same-origin" still sends nothing to other sites.
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY", "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"),
}


class LocalServer(HTTPServer):
    """One sequential loopback listener; the bind address is never configurable."""

    allow_reuse_address = False

    def __init__(self, store: Store, port: int = 0, *,
                 fixtures: Path | Traversable | None = DEFAULT_FIXTURES):
        super().__init__(("127.0.0.1", port), Handler)
        self.app = WorkflowApp(store, f"127.0.0.1:{self.server_port}", fixtures=fixtures)

    def handle_error(self, request, client_address) -> None:
        # The default handler prints a traceback, which can quote submitted content.
        sys.stderr.write("A local request failed; no input or exception detail was logged.\n")


class Handler(BaseHTTPRequestHandler):
    server: LocalServer
    protocol_version = "HTTP/1.0"
    server_version = "SZLScienceWorkflows"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args) -> None:  # noqa: A002 - stdlib signature
        return  # no URLs, bodies, identities or tokens in logs

    def send_error(self, code, message=None, explain=None) -> None:
        self._send(Response(code, views.failure_page(code, "Malformed request.").encode("utf-8")))

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for name, value in {**SECURITY_HEADERS, **response.headers}.items():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(response.body)

    def _dispatch(self) -> None:
        app = self.server.app
        headers: dict[str, list[str]] = {}
        for name in {key.lower() for key in self.headers.keys()}:
            headers[name] = self.headers.get_all(name, [])
        api = self.path.startswith(API)
        consumed = False
        try:
            admitted = app.admit(self.command, self.path, headers)
            body = self.rfile.read(admitted[2]) if admitted[2] else b""
            consumed = True
            if len(body) != admitted[2]:
                raise Rejected(400, "Incomplete request body.")
            response = app.handle(self.command, self.path, headers, body, admitted=admitted)
        except Rejected as error:
            response = app._rejected(error, api)
        except (TimeoutError, ConnectionError):
            self.close_connection = True
            return
        except Exception:
            # Never expose exception text; the outcome of a write may be unknown.
            message = ("The local store or operation is unavailable. The outcome may be unknown; "
                       "inspect the current state before retrying.")
            response = (app._json(503, {"error": {"code": "COMMIT_OUTCOME_UNKNOWN",
                                                  "message": message, "fields": [],
                                                  "retryable": False, "request_id": None}})
                        if api else Response(503, views.failure_page(503, message).encode("utf-8")))
        self._send(response)
        if not consumed:
            self._linger()

    def _linger(self) -> None:
        """Drain an unread request body (bounded) after a refusal, then close.

        Closing a socket with unread bytes makes Windows send a reset, which can destroy the
        refusal before the client reads it.
        """

        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            self.connection.settimeout(1)
            remaining = MAX_JSON + MAX_FORM
            while remaining > 0:
                chunk = self.connection.recv(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    do_GET = do_HEAD = do_POST = _dispatch

    def __getattr__(self, name: str):
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def _method_not_allowed(self) -> None:
        self._send(Response(405, views.failure_page(405, "Use an allowed page or form.")
                            .encode("utf-8")))


def default_database() -> Path:
    return Path.home() / ".szl-science-workflows" / "workflows.sqlite3"


def safe_database(path: Path) -> Path:
    """The operator chooses the store path; it must live outside the repository and not be a link."""

    path = path.absolute()
    if path.is_symlink() or path.parent.is_symlink():
        raise SystemExit("refusing a symlinked database path")
    if path.resolve().is_relative_to(REPO_ROOT):
        raise SystemExit("keep the workflow database outside the repository")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Loopback-only local science workflow workbench "
                                                 "(unauthenticated demo; owned or synthetic inputs)")
    parser.add_argument("--db", type=Path, default=default_database())
    parser.add_argument("--port", type=int, default=8771)
    args = parser.parse_args(argv)
    if not (args.port == 0 or 1024 <= args.port <= 65535):
        parser.error("port must be 0 or 1024..65535")
    server = LocalServer(Store(safe_database(args.db)), args.port)
    print(f"Local workbench: http://{server.app.host}{BASE}")
    print("Unauthenticated loopback demo. Owned or synthetic inputs only; no uploads, no network.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
