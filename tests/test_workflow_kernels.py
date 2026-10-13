"""Layer A/D: pinned kernel bytes, capability manifest and fail-closed loading."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from szl_forum_corpus import kernels
from szl_forum_corpus.workflows import paper_review
from szl_forum_corpus.workflows.contracts import WorkflowError
from szl_forum_corpus.workflows.store import Context, Store

ROOT = Path(__file__).resolve().parents[1]
PIN = "3e1f02c137c1e8da4ab0f78196bfa55dc7263984"


class KernelPinTests(unittest.TestCase):
    def test_every_pinned_file_matches_its_recorded_digest(self):
        self.assertEqual(kernels.MANIFEST["commit"], PIN)
        self.assertEqual(kernels.MANIFEST["repository"], "szl-holdings/szl-skills")
        self.assertEqual(kernels.MANIFEST["license"], "Apache-2.0")
        self.assertEqual(set(kernels.MANIFEST["kernels"]),
                         {"paper_rescue.py", "research_change_impact.py", "figure_data_contract.py"})
        for filename, entry in kernels.MANIFEST["kernels"].items():
            raw = (kernels.ROOT / filename).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"], filename)
            self.assertNotIn(b"\r\n", raw, filename)

    def test_capability_manifest_reports_all_available(self):
        manifest = kernels.capability_manifest()
        self.assertEqual([item["state"] for item in manifest], ["AVAILABLE"] * 3)
        self.assertTrue(all(item["commit"] == PIN for item in manifest))

    def test_declared_operations_exist_in_each_kernel(self):
        for filename, entry in kernels.MANIFEST["kernels"].items():
            module = kernels.load(filename)
            for operation in entry["operations"]:
                self.assertTrue(callable(getattr(module, operation, None)), (filename, operation))

    def test_byte_identity_is_protected_from_line_ending_conversion(self):
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
        self.assertIn("szl_forum_corpus/kernels/*.py -text", attributes)
        self.assertTrue((kernels.ROOT / "NOTICE").is_file())

    def test_changed_missing_or_unpinned_kernels_are_blocked(self):
        with mock.patch.object(kernels, "_observed_sha256", return_value="0" * 64), \
                mock.patch.dict(kernels._LOADED, clear=True):
            with self.assertRaises(kernels.KernelUnavailable) as caught:
                kernels.load("paper_rescue.py")
            self.assertEqual(caught.exception.code, "KERNEL_DIGEST_MISMATCH")
            self.assertEqual({item["state"] for item in kernels.capability_manifest()}, {"BLOCKED"})
        with mock.patch.object(kernels, "_observed_sha256", return_value=None), \
                mock.patch.dict(kernels._LOADED, clear=True):
            with self.assertRaises(kernels.KernelUnavailable) as caught:
                kernels.load("paper_rescue.py")
            self.assertEqual(caught.exception.code, "KERNEL_SOURCE_MISSING")
        with self.assertRaises(kernels.KernelUnavailable) as caught:
            kernels.load("anything_else.py")
        self.assertEqual(caught.exception.code, "KERNEL_NOT_PINNED")

    def test_a_blocked_kernel_is_a_503_never_a_result(self):
        fixtures = ROOT / "tests" / "fixtures" / "science_workflows" / "paper_review"
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "w.sqlite3")
            context = Context("o", "t", "operator")
            payload = {"case_id": "c", "protocol_revision": "p", "evidence_kind": "synthetic",
                       "provenance": "p",
                       "records_document": (fixtures / "records.json").read_text(encoding="utf-8"),
                       "reference_document": (fixtures / "reference.json").read_text(encoding="utf-8"),
                       "reference_independent": False, "reference_complete": True,
                       "source_access": "available"}
            with mock.patch.object(kernels, "_observed_sha256", return_value="0" * 64), \
                    mock.patch.dict(kernels._LOADED, clear=True):
                with self.assertRaises(WorkflowError) as caught:
                    paper_review.register_case(store, context, "1" * 32, payload)
            self.assertEqual(caught.exception.code, "KERNEL_UNAVAILABLE")
            self.assertEqual(caught.exception.http_status, 503)
            self.assertEqual(store.list(context, paper_review.KIND), [])

    def test_sources_manifest_is_strict_json(self):
        raw = (kernels.ROOT / "SOURCES.json").read_bytes()
        self.assertEqual(json.loads(raw)["schema"], "szl.pinned-kernel-sources.v1")


if __name__ == "__main__":
    unittest.main()
