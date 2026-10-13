"""Layer A: strict workflow envelopes, digests, error mapping and role admission."""

import json
import unittest

from szl_forum_corpus.workflows import change_recheck, paper_review, permissions
from szl_forum_corpus.workflows.contracts import (HTTP_STATUS, WorkflowError, bounded_text, boolean,
                                                  canonical_bytes, check_text, exact_fields,
                                                  identifier, parse_json, request_id, typed_digest,
                                                  utc_time)
from szl_forum_corpus.workflows.store import Context


def code(function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except WorkflowError as error:
        return error.code
    return None


class StrictJsonTests(unittest.TestCase):
    def test_duplicate_keys_and_non_finite_numbers_are_malformed(self):
        self.assertEqual(code(parse_json, b'{"a": 1, "a": 2}'), "MALFORMED_JSON")
        for constant in (b"NaN", b"Infinity", b"-Infinity", b'{"x": NaN}'):
            self.assertEqual(code(parse_json, constant), "MALFORMED_JSON", constant)

    def test_invalid_utf8_lone_surrogates_and_controls_are_refused(self):
        self.assertEqual(code(parse_json, b'"\xff"'), "MALFORMED_JSON")
        self.assertEqual(code(parse_json, b'"\\ud800"'), "INVALID_INPUT")
        self.assertEqual(code(parse_json, b'{"k": "a\\u0007b"}'), "INVALID_INPUT")
        self.assertEqual(code(check_text, "tab\tand\nnewline", "f"), None)

    def test_size_and_depth_are_bounded_before_use(self):
        self.assertEqual(code(parse_json, b"[" + b"1," * 10 + b"1]", limit=8), "TOO_LARGE")
        self.assertEqual(code(parse_json, b"[" * 40 + b"]" * 40), "INVALID_INPUT")
        self.assertEqual(code(parse_json, "not bytes"), "INVALID_INPUT")

    def test_canonical_bytes_are_order_independent_and_distinct_from_original_bytes(self):
        original = b'{ "b": 1, "a": "\xc3\xa9" }'
        parsed = parse_json(original)
        self.assertEqual(canonical_bytes(parsed), '{"a":"é","b":1}'.encode("utf-8"))
        self.assertEqual(canonical_bytes({"b": 1, "a": "é"}), canonical_bytes(parsed))
        raw = typed_digest(original, "submitted bytes", "none")
        normal = typed_digest(canonical_bytes(parsed), "parsed JSON", "sorted-keys compact UTF-8")
        self.assertNotEqual(raw["value"], normal["value"])
        self.assertEqual(set(raw), {"algorithm", "byte_domain", "canonicalization", "value"})


class EnvelopeTests(unittest.TestCase):
    def test_unknown_and_missing_fields_have_distinct_codes(self):
        self.assertEqual(code(exact_fields, {"a": 1, "z": 2}, {"a"}), "UNKNOWN_FIELD")
        self.assertEqual(code(exact_fields, {}, {"a"}), "MISSING_FIELD")
        self.assertEqual(code(exact_fields, [], {"a"}), "INVALID_INPUT")

    def test_booleans_are_not_integers_and_identifiers_are_bounded(self):
        self.assertEqual(code(boolean, 1, "f"), "INVALID_INPUT")
        self.assertIs(boolean(False, "f"), False)
        for bad in ("", "1abc", "a" * 65, "a/b", "a b", None, 3):
            self.assertEqual(code(identifier, bad, "id"), "INVALID_INPUT", bad)
        self.assertEqual(identifier("Case-1.v2_x", "id"), "Case-1.v2_x")
        self.assertEqual(code(request_id, "A" * 32), "INVALID_INPUT")
        self.assertEqual(code(bounded_text, "   ", "f"), "INVALID_INPUT")

    def test_times_must_be_real_utc_instants(self):
        self.assertEqual(utc_time("2026-10-13T00:00:00Z", "t"), "2026-10-13T00:00:00Z")
        for bad in ("2026-02-30T00:00:00Z", "2026-10-13 00:00:00", "2026-10-13T00:00:00+00:00"):
            self.assertEqual(code(utc_time, bad, "t"), "INVALID_INPUT", bad)

    def test_every_error_code_maps_to_one_http_status(self):
        self.assertEqual(HTTP_STATUS["STALE_REVISION"], 409)
        self.assertEqual(HTTP_STATUS["KERNEL_UNAVAILABLE"], 503)
        self.assertEqual(HTTP_STATUS["CAPABILITY_UNAVAILABLE"], 503)
        self.assertEqual(HTTP_STATUS["TOO_LARGE"], 413)
        self.assertTrue(all(400 <= status <= 599 for status in HTTP_STATUS.values()))
        with self.assertRaises(ValueError):
            WorkflowError("MADE_UP", "x")
        envelope = WorkflowError("STALE_REVISION", "changed", fields=["f"]).envelope("r" * 32)
        self.assertEqual(envelope, {"error": {"code": "STALE_REVISION", "message": "changed",
                                              "fields": ["f"], "retryable": False,
                                              "request_id": "r" * 32}})

    def test_error_messages_never_echo_submitted_content(self):
        secret = "PRIVATE-VALUE-123"
        for value in ({"case_id": secret}, json.dumps({"x": secret})):
            try:
                if isinstance(value, dict):
                    exact_fields(value, {"other"})
                else:
                    parse_json((value + "{").encode())
            except WorkflowError as error:
                self.assertNotIn(secret, error.message)


class MappingCoverageTests(unittest.TestCase):
    def test_paper_mapping_covers_exactly_the_kernel_results(self):
        kernel_results = {"MATCHED_DECLARED_REFERENCE", "REFERENCE_CONTENT_MISMATCH",
                          "REFERENCE_LOCATOR_MISMATCH", "MISSING", "ABSTAINED",
                          "UNSUPPORTED_LOCATED_CLAIM"}
        self.assertEqual(set(paper_review.RESULT_MAP), kernel_results)
        self.assertNotIn("match", [paper_review.RESULT_MAP[name][0] for name in kernel_results
                                   if name != "MATCHED_DECLARED_REFERENCE"])

    def test_recheck_tasks_come_only_from_reviewable_kernel_states(self):
        self.assertEqual(set(change_recheck.TASK_STATES),
                         {"RECHECK_REQUIRED", "MISSING_REQUIRED_CLAIM"})
        self.assertNotIn("human_confirmed_satisfied", change_recheck.WAIT_CONDITIONS)


class PermissionTests(unittest.TestCase):
    def test_roles_grant_only_their_operations(self):
        viewer = Context("v", "t", "viewer")
        reviewer = Context("r", "t", "reviewer")
        operator = Context("o", "t", "operator")
        unknown = Context("x", "t", "admin")
        self.assertIsNone(code(permissions.require, viewer, "read"))
        self.assertEqual(code(permissions.require, viewer, "review"), "FORBIDDEN")
        self.assertIsNone(code(permissions.require, reviewer, "effort"))
        self.assertEqual(code(permissions.require, reviewer, "register"), "FORBIDDEN")
        self.assertIsNone(code(permissions.require, operator, "compute"))
        self.assertEqual(code(permissions.require, unknown, "read"), "FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
