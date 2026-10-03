import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.publish_hf import _provider_call, public_files, publish


SOURCE_SHA = "a" * 40
BEFORE_SHA = "b" * 40
AFTER_SHA = "c" * 40


class FakeDatasetApi:
    def __init__(self, files: dict[str, bytes], *, exists: bool = True, private: bool = False):
        self.files = dict(files)
        self.exists = exists
        self.private = private
        self.sha = BEFORE_SHA
        self.commits: list[dict] = []
        self.downloads: list[tuple[str, str]] = []

    def whoami(self):
        return {"name": "authorized-test-user"}

    def repo_exists(self, repo_id, *, repo_type):
        self._assert_target(repo_id, repo_type)
        return self.exists

    def dataset_info(self, repo_id, *, revision=None):
        self._assert_target(repo_id, "dataset")
        if revision is not None and revision != self.sha:
            raise AssertionError("publisher queried a stale or unexpected revision")
        return SimpleNamespace(
            sha=self.sha,
            private=self.private,
            siblings=[SimpleNamespace(rfilename=name) for name in self.files],
        )

    def create_commit(self, *, repo_id, repo_type, operations, commit_message, parent_commit):
        self._assert_target(repo_id, repo_type)
        if parent_commit != self.sha:
            raise AssertionError("publisher omitted exact parent binding")
        uploaded = {operation.path_in_repo: operation.path_or_fileobj.read() for operation in operations}
        self.commits.append({"files": uploaded, "parent": parent_commit, "message": commit_message})
        self.files.update(uploaded)
        self.sha = AFTER_SHA
        return SimpleNamespace(oid=self.sha)

    @staticmethod
    def _assert_target(repo_id, repo_type):
        if (repo_id, repo_type) != ("SZLHOLDINGS/szl-science-forum-corpus", "dataset"):
            raise AssertionError("publisher used a noncanonical target")


def fake_hub(api: FakeDatasetApi, directory: str) -> ModuleType:
    module = ModuleType("huggingface_hub")
    module.HfApi = lambda *, token: api

    def operation_add(*, path_in_repo, path_or_fileobj):
        return SimpleNamespace(path_in_repo=path_in_repo, path_or_fileobj=path_or_fileobj)

    def download(*, repo_id, repo_type, filename, revision, token):
        api._assert_target(repo_id, repo_type)
        if revision != api.sha:
            raise AssertionError("publisher downloaded from a mutable or unexpected revision")
        api.downloads.append((filename, revision))
        path = Path(directory) / filename
        path.write_bytes(api.files[filename])
        return str(path)

    module.CommitOperationAdd = operation_add
    module.hf_hub_download = download
    return module


class ProviderGateTests(unittest.TestCase):
    def test_provider_error_names_stage_without_echoing_response(self):
        class RejectedRequest(Exception):
            response = type("Response", (), {"status_code": 400})()

        def reject():
            raise RejectedRequest("secret-bearing response text")

        with self.assertRaisesRegex(ValueError, "inspect_before: provider RejectedRequest HTTP 400") as caught:
            _provider_call("inspect_before", reject)
        self.assertNotIn("secret-bearing", str(caught.exception))

    def test_offline_preflight_has_only_reviewed_metadata(self):
        files = public_files()
        self.assertEqual(len(files), 7)
        self.assertIn("README.md", files)
        self.assertNotIn(b"Following my earlier", files["sources.public.jsonl"])
        manifest = json.loads(files["manifest.json"])
        self.assertFalse(manifest["model_training_authorized"])
        self.assertEqual(manifest["source_records_withheld"], 0)

    def test_publish_rejects_wrong_commit_before_provider_import(self):
        with patch("scripts.publish_hf._local_git_sha", return_value=SOURCE_SHA):
            with self.assertRaisesRegex(ValueError, "expected Git commit"):
                publish({}, "0" * 40)

    def test_publish_rejects_nonmain_and_missing_explicit_gate(self):
        with patch("scripts.publish_hf._local_git_sha", return_value=SOURCE_SHA), patch.dict("os.environ", {"GITHUB_REF": "refs/heads/feature"}):
            with self.assertRaisesRegex(ValueError, "GitHub main"):
                publish({}, SOURCE_SHA)
        with patch("scripts.publish_hf._local_git_sha", return_value=SOURCE_SHA), patch.dict("os.environ", {"GITHUB_REF": "refs/heads/main", "GITHUB_SHA": SOURCE_SHA, "SZL_HF_PUBLISH_APPROVED": ""}):
            with self.assertRaisesRegex(ValueError, "explicit publication gate"):
                publish({}, SOURCE_SHA)

    def _publish_with_fake(self, api: FakeDatasetApi, files: dict[str, bytes]) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            module = fake_hub(api, directory)
            environment = {
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_SHA": SOURCE_SHA,
                "SZL_HF_PUBLISH_APPROVED": "1",
                "HF_TOKEN": "test-token",
            }
            with patch("scripts.publish_hf._local_git_sha", return_value=SOURCE_SHA), \
                 patch.dict("os.environ", environment), patch.dict(sys.modules, {"huggingface_hub": module}):
                return publish(files, SOURCE_SHA)

    def test_missing_canonical_dataset_fails_without_creation(self):
        api = FakeDatasetApi({}, exists=False)
        with self.assertRaisesRegex(ValueError, "canonical Hugging Face dataset does not exist"):
            self._publish_with_fake(api, {"README.md": b"public"})
        self.assertEqual(api.commits, [])

    def test_existing_private_dataset_remains_private(self):
        api = FakeDatasetApi({"README.md": b"restricted"}, private=True)
        with self.assertRaisesRegex(ValueError, "existing dataset is private"):
            self._publish_with_fake(api, {"README.md": b"public"})
        self.assertEqual(api.commits, [])
        self.assertEqual(api.downloads, [])

    def test_identical_files_are_read_back_without_a_commit(self):
        files = {"README.md": b"public card", "manifest.json": b"{}"}
        api = FakeDatasetApi({**files, ".gitattributes": b"provider generated"})
        result = self._publish_with_fake(api, files)
        self.assertEqual(result["state"], "UNCHANGED_AND_READ_BACK")
        self.assertEqual(result["changed_files"], [])
        self.assertEqual(result["hf_revision"], BEFORE_SHA)
        self.assertEqual(api.commits, [])
        self.assertEqual({name for name, _ in api.downloads}, set(files))

    def test_only_changed_files_are_committed_and_read_back(self):
        files = {"README.md": b"public card", "manifest.json": b"new manifest", "needs.json": b"new needs"}
        api = FakeDatasetApi({"README.md": files["README.md"], "manifest.json": b"old manifest"})
        result = self._publish_with_fake(api, files)
        self.assertEqual(result["state"], "PUBLISHED_AND_READ_BACK")
        self.assertEqual(result["changed_files"], ["manifest.json", "needs.json"])
        self.assertEqual(result["hf_revision"], AFTER_SHA)
        self.assertEqual(len(api.commits), 1)
        self.assertEqual(api.commits[0]["parent"], BEFORE_SHA)
        self.assertEqual(api.commits[0]["files"], {
            "manifest.json": files["manifest.json"], "needs.json": files["needs.json"]
        })
        self.assertEqual(api.files["README.md"], files["README.md"])

    def test_changed_file_requires_exact_provider_readback(self):
        class TamperingApi(FakeDatasetApi):
            def create_commit(self, **kwargs):
                result = super().create_commit(**kwargs)
                self.files["README.md"] = b"different provider bytes"
                return result

        api = TamperingApi({"README.md": b"old"})
        with self.assertRaisesRegex(ValueError, "provider byte mismatch: README.md"):
            self._publish_with_fake(api, {"README.md": b"reviewed"})
        self.assertEqual(len(api.commits), 1)

    def test_unreviewed_provider_file_blocks_mutation(self):
        api = FakeDatasetApi({"README.md": b"old", "private-dump.json": b"restricted"})
        with self.assertRaisesRegex(ValueError, "unreviewed extra files"):
            self._publish_with_fake(api, {"README.md": b"new"})
        self.assertEqual(api.commits, [])


if __name__ == "__main__":
    unittest.main()
