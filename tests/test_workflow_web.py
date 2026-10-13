"""Layer C: the real loopback server, routes, admission checks and rendered pages over HTTP."""

import http.client
import json
import secrets
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from szl_forum_corpus import kernels
from szl_forum_corpus.web import workflow_routes as routes
from szl_forum_corpus.workflows import change_recheck, figure_check, paper_review
from szl_forum_corpus.workflows.store import Store

FIXTURES = Path(paper_review.__file__).resolve().parent / "fixtures"
BASE = "/operator/science"
API = "/api/science/v1"


class WebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "workflows.sqlite3"
        self.start()

    def start(self):
        self.store = Store(self.db)
        # No fixtures argument: every web test exercises the default bundled-fixture path.
        self.server = routes.LocalServer(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.host = self.server.app.host
        self.csrf = self.server.app.csrf

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def tearDown(self):
        self.stop()
        self.directory.cleanup()

    # ---- raw HTTP helpers -----------------------------------------------------------------

    def request(self, method, path, body=b"", headers=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            all_headers = [("Host", host or self.host)] + list(headers or [])
            if body or method == "POST":
                all_headers.append(("Content-Length", str(len(body))))
            for name, value in all_headers:
                connection.putheader(name, value)
            connection.endheaders(body or None)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def form(self, path, fields, origin=None, csrf=None):
        values = {"csrf": self.csrf if csrf is None else csrf,
                  "request_id": fields.pop("request_id", secrets.token_hex(16)), **fields}
        body = urlencode(values).encode("utf-8")
        return self.request("POST", path, body, [
            ("Origin", origin or f"http://{self.host}"),
            ("Content-Type", "application/x-www-form-urlencoded")])

    def api(self, path, value, csrf=None, raw=None):
        body = raw if raw is not None else json.dumps(value).encode("utf-8")
        status, headers, payload = self.request("POST", path, body, [
            ("Origin", f"http://{self.host}"), ("Content-Type", "application/json"),
            ("X-SZL-CSRF", self.csrf if csrf is None else csrf)])
        return status, json.loads(payload)

    def get_json(self, path):
        status, _, body = self.request("GET", path)
        self.assertEqual(status, 200, body[:300])
        return json.loads(body)

    def page(self, path):
        status, _, body = self.request("GET", path)
        self.assertEqual(status, 200, body[:300])
        return body.decode("utf-8")

    def load(self, which):
        status, headers, _ = self.form(f"{BASE}/fixtures", {"which": which})
        self.assertEqual(status, 303)
        return headers["Location"]

    def case_revision(self):
        return self.get_json(f"{API}/paper-cases/demo-paper")["revision"]

    def review(self, fields, revision=None):
        fields = dict(fields)
        fields.setdefault("expected_revision", str(revision or self.case_revision()))
        return self.form(f"{BASE}/paper-review/demo-paper/review-events", fields)

    def nothing_stored(self):
        context = self.server.app.context
        return (self.store.list(context, paper_review.KIND) == []
                and self.store.list(context, change_recheck.SNAPSHOT) == []
                and self.store.list(context, figure_check.BUNDLE) == [])

    # ---- admission --------------------------------------------------------------------------

    def test_overview_carries_security_headers_and_capabilities(self):
        status, headers, body = self.request("GET", BASE)
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        # A real browser sends "Origin: null" under no-referrer, which would refuse every form.
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        text = body.decode("utf-8")
        self.assertIn("unauthenticated local demo principal", text)
        self.assertEqual(text.count(">AVAILABLE<"), 3)
        capabilities = self.get_json(f"{API}/capabilities")
        self.assertEqual(capabilities["uploads"], "UNAVAILABLE")
        self.assertFalse(self.get_json(f"{API}/session")["authenticated"])

    def test_host_origin_csrf_and_framing_refusals_change_nothing(self):
        form = urlencode({"csrf": self.csrf, "request_id": secrets.token_hex(16),
                          "which": "paper"}).encode()
        good = [("Origin", f"http://{self.host}"),
                ("Content-Type", "application/x-www-form-urlencoded")]
        cases = [
            ("GET", BASE, b"", [], "localhost:%d" % self.server.server_port, 403),
            ("GET", BASE, b"", [], "evil.example", 403),
            ("GET", BASE, b"", [("Host", self.host)], None, 400),
            ("GET", BASE, b"", [("Sec-Fetch-Site", "cross-site")], None, 403),
            ("GET", BASE + "?x=1", b"", [], None, 400),
            ("GET", BASE + "/%2e%2e", b"", [], None, 400),
            ("GET", BASE + "/nowhere/else", b"", [], None, 404),
            ("GET", BASE, b"abcde", [], None, 400),
            ("PUT", BASE, b"", [], None, 405),
            ("DELETE", BASE + "/paper-review", b"", [], None, 405),
            ("POST", BASE, form, good, None, 405),
            ("POST", f"{BASE}/fixtures", form, [("Origin", "http://evil.example"), good[1]], None, 403),
            ("POST", f"{BASE}/fixtures", form, [good[1]], None, 400),
            ("POST", f"{BASE}/fixtures", form, [good[0], ("Content-Type", "application/json")], None, 415),
            ("POST", f"{BASE}/fixtures", form, good + [("Transfer-Encoding", "chunked")], None, 400),
            ("POST", f"{BASE}/fixtures", b"x" * (routes.MAX_FORM + 1), good, None, 413),
        ]
        for method, path, body, headers, host, expected in cases:
            with self.subTest(method=method, path=path, host=host, headers=headers):
                status, _, _ = self.request(method, path, body, headers, host)
                self.assertEqual(status, expected)
        status, _, body = self.form(f"{BASE}/fixtures", {"which": "paper"}, csrf="forged")
        self.assertEqual(status, 403)
        self.assertNotIn(self.csrf.encode(), body)
        status, _, _ = self.form(f"{BASE}/fixtures", {"which": "paper", "extra": "1"})
        self.assertEqual(status, 400)
        self.assertTrue(self.nothing_stored())

    def test_get_and_head_never_mutate(self):
        self.load("paper")
        before = self.store.history(self.server.app.context, "demo-paper")
        for path in (BASE, f"{BASE}/paper-review", f"{BASE}/paper-review/demo-paper",
                     f"{API}/paper-cases/demo-paper", f"{API}/paper-cases/demo-paper/summary"):
            self.request("GET", path)
            status, headers, body = self.request("HEAD", path)
            self.assertEqual(status, 200)
            self.assertEqual(body, b"")
            self.assertGreater(int(headers["Content-Length"]), 0)
        self.assertEqual(self.store.history(self.server.app.context, "demo-paper"), before)

    def test_uploads_are_an_unavailable_capability(self):
        for route in ("paper-cases", "evidence-snapshots", "figure-bundles"):
            status, payload = self.api(f"{API}/{route}", {"request_id": "0" * 32})
            self.assertEqual(status, 503)
            self.assertEqual(payload["error"]["code"], "CAPABILITY_UNAVAILABLE")
        self.assertTrue(self.nothing_stored())

    def test_api_errors_use_the_typed_envelope(self):
        self.load("paper")
        path = f"{API}/paper-cases/demo-paper/review-events"
        event = {"type": "request_evidence", "item_id": "t1_correct", "note": "check"}
        status, payload = self.api(path, {"request_id": "1" * 32, "expected_revision": 1,
                                          "event": event})
        self.assertEqual((status, payload["error"]["code"]), (409, "STALE_REVISION"))
        status, payload = self.api(path, {"request_id": "2" * 32, "expected_revision": True,
                                          "event": event})
        self.assertEqual((status, payload["error"]["code"]), (422, "INVALID_INPUT"))
        status, payload = self.api(path, {}, csrf="forged")
        self.assertEqual((status, payload["error"]["code"]), (403, "FORBIDDEN"))
        status, payload = self.api(path, None, raw=b'{"request_id": "a", "request_id": "b"}')
        self.assertEqual((status, payload["error"]["code"]), (400, "MALFORMED_JSON"))
        status, payload = self.api(path, {"request_id": "3" * 32, "expected_revision": 2,
                                          "event": event})
        self.assertEqual(status, 200)
        self.assertEqual(payload["revisions"], {"demo-paper": 3})

    # ---- end to end -------------------------------------------------------------------------

    def test_paper_review_end_to_end_survives_restart(self):
        self.assertEqual(self.load("paper"), f"{BASE}/paper-review/demo-paper")
        text = self.page(f"{BASE}/paper-review/demo-paper")
        self.assertIn("10 declared reference items", text)
        self.assertIn("Initial discrepancies: 5", text)
        for fields in (
                {"type": "propose_correction", "item_id": "t2_wrong_value", "correction_id": "web-fix",
                 "field": "text", "value": "18.3", "reason_code": "value_error",
                 "evidence": "table 1, treated row"},
                {"type": "decide_correction", "item_id": "t2_wrong_value", "correction_id": "web-fix",
                 "decision": "accept", "reason": "matches the source table"},
                {"type": "final_qc", "item_id": "t2_wrong_value", "outcome": "accepted",
                 "reviewed_version": "1", "note": "verified"}):
            status, headers, body = self.review(fields)
            self.assertEqual(status, 303, body[:400])
            self.assertEqual(headers["Location"], f"{BASE}/paper-review/demo-paper")
        case = self.get_json(f"{API}/paper-cases/demo-paper")
        self.assertEqual(case["items"]["t2_wrong_value"]["qc"]["state"], "accepted")
        self.assertEqual(case["comparison"]["findings"]["t2_wrong_value"]["extraction"], "mismatch")
        events = [entry["event_type"] for entry in
                  self.store.history(self.server.app.context, "demo-paper")]
        self.assertEqual(events, ["case_registered", "initial_comparison_recorded",
                                  "review_propose_correction", "review_decide_correction",
                                  "review_final_qc"])
        status, _, _ = self.form(f"{BASE}/paper-review/demo-paper/effort-events",
                                 {"action": "start", "session_id": "web-s1", "activity": "review",
                                  "expected_revision": str(self.case_revision())})
        self.assertEqual(status, 303)
        self.stop()
        self.start()
        text = self.page(f"{BASE}/paper-review/demo-paper")
        self.assertIn("Initial discrepancies: 5", text)
        self.assertIn("unknown (interrupted_or_open)", text)
        self.assertEqual(self.get_json(f"{API}/paper-cases/demo-paper/summary")
                         ["staff_effort"]["unknown_sessions"], 1)

    def test_stale_form_keeps_unsaved_values_and_explains(self):
        self.load("paper")
        old = self.case_revision()
        self.review({"type": "request_evidence", "item_id": "t1_correct", "note": "first"}, old)
        status, _, body = self.review({"type": "request_evidence", "item_id": "t3_wrong_unit",
                                       "note": "my unsaved observation"}, old)
        self.assertEqual(status, 409)
        text = body.decode("utf-8")
        self.assertIn("STALE_REVISION: the state changed", text)
        self.assertIn("my unsaved observation", text)

    def test_double_submit_with_one_request_id_records_one_event(self):
        self.load("paper")
        revision = self.case_revision()
        fields = {"type": "request_evidence", "item_id": "t1_correct", "note": "once",
                  "request_id": "ab" * 16}
        self.assertEqual(self.review(dict(fields), revision)[0], 303)
        self.assertEqual(self.review(dict(fields), revision)[0], 303)
        self.assertEqual(len(self.store.history(self.server.app.context, "demo-paper")), 3)

    def test_submitted_text_is_escaped_in_pages(self):
        self.load("paper")
        self.review({"type": "propose_correction", "item_id": "t2_wrong_value",
                     "correction_id": "xss", "field": "text",
                     "value": '<script>alert(1)</script>"><img src=x onerror=alert(2)>',
                     "reason_code": "other", "evidence": "x"})
        text = self.page(f"{BASE}/paper-review/demo-paper")
        self.assertNotIn("<script>alert(1)", text)
        self.assertNotIn("<img src=x", text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)

    def test_recheck_queue_end_to_end(self):
        self.assertEqual(self.load("recheck"), f"{BASE}/rechecks")
        text = self.page(f"{BASE}/rechecks")
        self.assertIn("Needs recheck", text)
        self.assertIn("never means “false”", text)
        task_id = change_recheck.task_id_for("demo-cs1", "c2")
        page = f"{BASE}/rechecks/{task_id}/events"

        def event(fields):
            revision = self.get_json(f"{API}/recheck-tasks")
            current = next(task for task in revision["tasks"] if task["task_id"] == task_id)
            return self.form(page, dict(fields, expected_revision=str(current["revision"])))

        self.assertEqual(event({"type": "start_review"})[0], 303)
        self.assertEqual(event({"type": "set_predicate", "text": "assay rerun agrees"})[0], 303)
        self.assertEqual(event({"type": "resolve", "evidence": "rerun log", "change_set_id": "demo-cs1",
                                "predicate_revision": "1"})[0], 303)
        self.assertIn("does not make the claim true", self.page(f"{BASE}/rechecks/{task_id}"))
        queue = self.get_json(f"{API}/recheck-tasks")
        observed = json.loads((FIXTURES / "change_recheck" / "expected.json").read_text())
        status, _, body = self.form(f"{BASE}/rechecks/change-sets", {
            "change_set_id": "web-cs2", "baseline_snapshot_id": "demo-v2",
            "current_snapshot_id": "demo-v3", "required_claims": '["c1","c2","c3","c4","c5"]',
            "observed_digests": json.dumps(observed["second"]["observed_digests"]),
            "as_of": "2026-10-13T00:00:00Z",
            "expected_queue_revision": str(queue["queue_revision"])})
        self.assertEqual(status, 303, body[:400])
        old = self.page(f"{BASE}/rechecks/{task_id}")
        self.assertIn("Superseded by", old)
        self.assertEqual(self.get_json(f"{API}/change-sets/web-cs2")["tasks"].__len__(), 5)

    def test_figure_checks_end_to_end(self):
        self.assertEqual(self.load("figure"), f"{BASE}/figures")
        listing = self.page(f"{BASE}/figures")
        self.assertIn("demo-figure.edited", listing)
        edited = self.page(f"{BASE}/figures/demo-figure.edited")
        self.assertIn("FAILED", edited)
        self.assertIn("&quot;row&quot;: &quot;s3&quot;", edited)
        self.assertIn("SVG is not the canonical generated artifact", edited)
        self.assertNotIn("<svg", edited)  # supplied SVG is escaped source, not markup
        status, headers, _ = self.form(f"{BASE}/figures/demo-figure/renders",
                                       {"new_bundle_id": "web-render"})
        self.assertEqual((status, headers["Location"]), (303, f"{BASE}/figures/web-render"))
        status, _, _ = self.form(f"{BASE}/figures/web-render/checks",
                                 {"expected_revision": "1", "check_id": "web-check"})
        self.assertEqual(status, 303)
        passed = self.page(f"{BASE}/figures/web-render")
        self.assertIn("Canonical data-to-render checks passed.", passed)
        self.assertIn('src="data:image/svg+xml;base64,', passed)
        self.assertEqual(self.get_json(f"{API}/figure-checks/web-check")["execution"], "PASSED")
        status, headers, body = self.request("GET", f"{API}/figure-bundles/web-render/download")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Disposition"],
                         'attachment; filename="figure-bundle-package.json"')
        self.assertEqual(sorted(json.loads(body)["members"]), ["data", "sidecar", "spec", "svg"])

    def test_blocked_kernel_is_reported_and_never_passes(self):
        self.load("figure")
        real = kernels._observed_sha256

        def tampered(filename):
            return "0" * 64 if filename == "figure_data_contract.py" else real(filename)

        with mock.patch.object(kernels, "_observed_sha256", side_effect=tampered), \
                mock.patch.dict(kernels._LOADED, clear=True):
            overview = self.page(BASE)
            self.assertIn(">BLOCKED<", overview)
            status, _, body = self.form(f"{BASE}/figures/demo-figure.r1/checks",
                                        {"expected_revision": "2", "check_id": "blocked-check"})
            self.assertEqual(status, 503)
            self.assertIn(b"KERNEL_UNAVAILABLE", body)
            status, payload = self.api(f"{API}/figure-bundles/demo-figure.r1/checks", {
                "request_id": "c" * 32, "expected_revision": 2, "check_id": "blocked-check"})
            self.assertEqual((status, payload["error"]["code"]), (503, "KERNEL_UNAVAILABLE"))
        status, _, _ = self.request("GET", f"{API}/figure-checks/blocked-check")
        self.assertEqual(status, 404)

    def test_default_fixture_path_is_the_packaged_copy(self):
        self.assertEqual(Path(str(self.server.app.fixtures)), FIXTURES)
        self.assertEqual(self.get_json(f"{API}/capabilities")["fixtures"], "AVAILABLE")
        self.load("paper")
        case = self.get_json(f"{API}/paper-cases/demo-paper")
        self.assertEqual(case["provenance"], "szl_forum_corpus/workflows/fixtures/PROVENANCE.md")

    def test_explicit_fixture_override_still_works_and_none_is_blocked(self):
        headers = {"host": ["127.0.0.1:1"], "origin": ["http://127.0.0.1:1"],
                   "content-type": ["application/x-www-form-urlencoded"]}
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "fixtures"
            shutil.copytree(FIXTURES, copy)
            store = Store(Path(directory) / "override.sqlite3")
            app = routes.WorkflowApp(store, "127.0.0.1:1", fixtures=copy)
            body = urlencode({"csrf": app.csrf, "request_id": "e" * 32, "which": "paper"}).encode()
            response = app.handle("POST", f"{BASE}/fixtures",
                                  dict(headers, **{"content-length": [str(len(body))]}), body)
            self.assertEqual((response.status, response.headers["Location"]),
                             (303, f"{BASE}/paper-review/demo-paper"))
            blocked = routes.WorkflowApp(Store(Path(directory) / "none.sqlite3"), "127.0.0.1:1",
                                         fixtures=None)
            body = urlencode({"csrf": blocked.csrf, "request_id": "f" * 32,
                              "which": "paper"}).encode()
            response = blocked.handle("POST", f"{BASE}/fixtures",
                                      dict(headers, **{"content-length": [str(len(body))]}), body)
            self.assertEqual(response.status, 503)
            self.assertIn(b"CAPABILITY_UNAVAILABLE", response.body)
            capabilities = json.loads(blocked.handle("GET", f"{API}/capabilities",
                                                     {"host": ["127.0.0.1:1"]}, b"").body)
            self.assertEqual(capabilities["fixtures"], "BLOCKED")
            self.assertEqual(blocked.store.list(blocked.context, paper_review.KIND), [])

    def test_database_must_live_outside_the_repository(self):
        with self.assertRaises(SystemExit):
            routes.safe_database(routes.REPO_ROOT / "workflows.sqlite3")


if __name__ == "__main__":
    unittest.main()
