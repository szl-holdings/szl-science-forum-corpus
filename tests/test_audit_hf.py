import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts.audit_hf import FILES, INFO_URL, MAX_FILE_BYTES, audit_alignment, main
from scripts.publish_hf import REPO_ID, source_files


SOURCE_SHA = "a" * 40
HF_SHA = "b" * 40
SECRET = "synthetic-private-marker@example.test"


class PublicProvider:
    def __init__(self):
        self.files = {name: f"original:{name}".encode() for name in FILES}
        self.info = {"id": REPO_ID, "sha": HF_SHA, "private": False,
                     "siblings": [{"rfilename": name} for name in sorted(FILES | {".gitattributes"})]}
        self.after = None
        self.pinned = None
        self.calls = []
        self.head_reads = 0

    def fetch(self, url, limit):
        self.calls.append(url)
        if url == INFO_URL:
            self.head_reads += 1
            value = self.after if self.after is not None and self.head_reads > 1 else self.info
            return json.dumps(value).encode()
        if url == f"{INFO_URL}/revision/{HF_SHA}":
            return json.dumps(self.pinned if self.pinned is not None else self.info).encode()
        prefix = f"https://huggingface.co/datasets/{REPO_ID}/resolve/{HF_SHA}/"
        if not url.startswith(prefix):
            raise AssertionError("unexpected URL or unpinned file read")
        return self.files[url.removeprefix(prefix)]


class AlignmentAuditTests(unittest.TestCase):
    def setUp(self):
        self.provider = PublicProvider()
        self.files = copy.deepcopy(self.provider.files)

    def audit(self):
        return audit_alignment(self.files, SOURCE_SHA, self.provider.fetch)

    def test_exact_public_bytes_and_stable_revision_pass_without_provider_writes(self):
        receipt = self.audit()
        self.assertEqual(receipt["state"], "ALIGNED")
        self.assertEqual(receipt["hf_revision"], HF_SHA)
        self.assertEqual(receipt["source_git_sha"], SOURCE_SHA)
        self.assertTrue(receipt["head_stable"])
        self.assertEqual(receipt["files_sha256"], receipt["observed_files_sha256"])
        self.assertEqual(len(self.provider.calls), 10)
        self.assertFalse(receipt["provider_writes"])
        self.assertFalse(receipt["model_training_authorized"])

    def test_changed_bytes_are_drift_and_raw_content_never_enters_receipt(self):
        self.provider.files["README.md"] = SECRET.encode()
        receipt = self.audit()
        self.assertEqual(receipt["state"], "DRIFT")
        self.assertEqual(receipt["mismatched_files"], ["README.md"])
        self.assertNotIn(SECRET, json.dumps(receipt))

    def test_missing_and_extra_inventory_is_drift_without_echoing_unknown_paths(self):
        self.provider.info["siblings"] = [item for item in self.provider.info["siblings"] if item["rfilename"] != "README.md"]
        self.provider.info["siblings"].append({"rfilename": SECRET})
        receipt = self.audit()
        self.assertEqual(receipt["state"], "DRIFT")
        self.assertEqual(receipt["missing_files"], ["README.md"])
        self.assertEqual(receipt["unexpected_file_count"], 1)
        self.assertNotIn(SECRET, json.dumps(receipt))
        self.assertFalse(any(SECRET in url for url in self.provider.calls))

    def test_head_movement_and_inventory_change_are_conflicts_not_alignment(self):
        for field, value in (("sha", "c" * 40), ("siblings", [])):
            self.provider = PublicProvider()
            self.provider.after = {**self.provider.info, field: value}
            with self.subTest(field=field):
                self.assertEqual(self.audit()["state"], "CONFLICT")

    def test_wrong_pinned_revision_prevents_all_file_reads(self):
        self.provider.pinned = {**self.provider.info, "sha": "c" * 40}
        receipt = self.audit()
        self.assertEqual(receipt["state"], "CONFLICT")
        self.assertEqual(len(self.provider.calls), 2)

    def test_private_malformed_and_duplicate_inventory_never_pass(self):
        variants = [
            {**self.provider.info, "private": True},
            {**self.provider.info, "private": 0},
            {**self.provider.info, "sha": SECRET},
            {**self.provider.info, "id": "other/repository"},
            {**self.provider.info, "siblings": [{"rfilename": "README.md"}] * 2},
        ]
        for info in variants:
            self.provider.info = info
            with self.subTest(info=info):
                receipt = self.audit()
                self.assertEqual(receipt["state"], "UNAVAILABLE")
                self.assertNotIn(SECRET, json.dumps(receipt))

    def test_http_failure_is_unavailable_with_no_retry_or_error_body_disclosure(self):
        for status in (401, 403, 404, 429, 503):
            calls = []

            def failed(url, limit):
                calls.append(url)
                raise HTTPError(url, status, SECRET, None, None)

            receipt = audit_alignment(self.files, SOURCE_SHA, failed)
            self.assertEqual(receipt["state"], "UNAVAILABLE")
            self.assertEqual(receipt["http_status"], status)
            self.assertEqual(receipt["failed_stage"], "info_before")
            self.assertEqual(len(calls), 1)
            self.assertNotIn(SECRET, json.dumps(receipt))

    def test_duplicate_json_keys_and_oversized_files_are_unavailable(self):
        def duplicate(url, limit):
            return ('{"' + SECRET + '":1,"' + SECRET + '":2}').encode()

        receipt = audit_alignment(self.files, SOURCE_SHA, duplicate)
        self.assertEqual(receipt["state"], "UNAVAILABLE")
        self.assertNotIn(SECRET, json.dumps(receipt))
        self.provider.files["README.md"] = b"x" * (MAX_FILE_BYTES + 1)
        receipt = self.audit()
        self.assertEqual(receipt["state"], "UNAVAILABLE")
        self.assertEqual(receipt["failed_stage"], "file_read")

    def test_unreviewed_source_inventory_or_invalid_sha_fails_before_network(self):
        for files, sha in (({**self.files, "unreviewed.json": b"x"}, SOURCE_SHA), (self.files, "main")):
            with self.assertRaises(ValueError):
                audit_alignment(files, sha, self.provider.fetch)
        self.assertEqual(self.provider.calls, [])

    def test_source_bytes_must_equal_exact_git_blob(self):
        def git_show(args, **kwargs):
            name = args[-1].split("dataset/")[-1]
            return subprocess.CompletedProcess(args, 0, stdout=self.files[name])

        with patch("scripts.publish_hf._local_git_sha", return_value=SOURCE_SHA):
            with patch("scripts.publish_hf.public_files", return_value=self.files):
                with patch("scripts.publish_hf.subprocess.run", side_effect=git_show):
                    self.assertEqual(source_files(SOURCE_SHA), self.files)
                with patch("scripts.publish_hf.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=b"dirty")):
                    with self.assertRaisesRegex(ValueError, "exact Git source bytes"):
                        source_files(SOURCE_SHA)

    def test_cli_writes_nonzero_drift_receipt_and_refuses_overwrite(self):
        self.provider.files["README.md"] = b"drift"
        receipt = self.audit()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            with patch("scripts.audit_hf.source_files", return_value=self.files), patch("scripts.audit_hf.audit_alignment", return_value=receipt):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--expected-sha", SOURCE_SHA, "--out", str(output)]), 1)
                    original = output.read_bytes()
                    self.assertEqual(main(["--expected-sha", SOURCE_SHA, "--out", str(output)]), 2)
                self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
