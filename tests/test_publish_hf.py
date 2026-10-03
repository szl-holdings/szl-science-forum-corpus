import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.publish_hf import _local_git_sha, _provider_call, public_files, publish


class ProviderGateTests(unittest.TestCase):
    def test_provider_error_names_stage_without_echoing_response(self):
        class RejectedRequest(Exception):
            response = type("Response", (), {"status_code": 400})()

        def reject():
            raise RejectedRequest("secret-bearing response text")

        with self.assertRaisesRegex(ValueError, "create_repo: provider RejectedRequest HTTP 400") as caught:
            _provider_call("create_repo", reject)
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
        with self.assertRaisesRegex(ValueError, "expected Git commit"):
            publish({}, "0" * 40)

    def test_publish_rejects_nonmain_and_missing_explicit_gate(self):
        sha = _local_git_sha()
        with patch.dict("os.environ", {"GITHUB_REF": "refs/heads/feature"}):
            with self.assertRaisesRegex(ValueError, "GitHub main"):
                publish({}, sha)

    def test_stale_main_or_unbound_bytes_are_rejected_before_provider_calls(self):
        sha = _local_git_sha()
        environment = {"GITHUB_REF": "refs/heads/main", "GITHUB_SHA": sha,
                       "SZL_HF_PUBLISH_APPROVED": "1", "HF_TOKEN": "synthetic-test-token"}
        with patch.dict("os.environ", environment):
            with patch("scripts.publish_hf.source_files", return_value={"README.md": b"reviewed"}):
                with self.assertRaisesRegex(ValueError, "publication bytes"):
                    publish({}, sha)

    def test_publisher_commits_verified_bytes_at_provider_parent_and_reads_back(self):
        sha = _local_git_sha()
        files = {"README.md": b"reviewed-memory-bytes"}
        commit_calls = []

        def create_commit(**kwargs):
            commit_calls.append(kwargs)
            return SimpleNamespace(oid="b" * 40)

        api = SimpleNamespace(
            whoami=lambda: {"name": "synthetic-publisher"},
            repo_exists=lambda *args, **kwargs: True,
            dataset_info=lambda *args, **kwargs: SimpleNamespace(
                private=False, sha="a" * 40,
                siblings=[SimpleNamespace(rfilename="README.md")],
            ),
            create_commit=create_commit,
        )
        sdk = SimpleNamespace(HfApi=lambda **kwargs: api,
                              CommitOperationAdd=lambda **kwargs: kwargs)
        environment = {"GITHUB_REF": "refs/heads/main", "GITHUB_SHA": sha,
                       "SZL_HF_PUBLISH_APPROVED": "1", "HF_TOKEN": "synthetic-test-token"}
        with patch.dict("os.environ", environment), patch.dict(sys.modules, {"huggingface_hub": sdk}):
            with patch("scripts.publish_hf.source_files", return_value=files), patch("scripts.publish_hf._current_main_sha", return_value=sha):
                with patch("scripts.publish_hf._read_back") as read_back:
                    receipt = publish(files, sha)
        self.assertEqual(receipt["state"], "PUBLISHED_AND_READ_BACK")
        self.assertEqual(commit_calls[0]["parent_commit"], "a" * 40)
        self.assertEqual(commit_calls[0]["operations"][0]["path_or_fileobj"], files["README.md"])
        read_back.assert_called_once_with(api, "b" * 40, files, "synthetic-test-token")

    def test_main_advancing_during_provider_preflight_stops_before_commit(self):
        sha = _local_git_sha()
        api = SimpleNamespace(
            whoami=lambda: {"name": "synthetic-publisher"},
            repo_exists=lambda *args, **kwargs: True,
            dataset_info=lambda *args, **kwargs: SimpleNamespace(private=False, sha="a" * 40, siblings=[]),
        )
        sdk = SimpleNamespace(HfApi=lambda **kwargs: api, CommitOperationAdd=lambda **kwargs: kwargs)
        environment = {"GITHUB_REF": "refs/heads/main", "GITHUB_SHA": sha,
                       "SZL_HF_PUBLISH_APPROVED": "1", "HF_TOKEN": "synthetic-test-token"}
        with patch.dict("os.environ", environment), patch.dict(sys.modules, {"huggingface_hub": sdk}):
            with patch("scripts.publish_hf.source_files", return_value={}), patch("scripts.publish_hf._current_main_sha", side_effect=[sha, "0" * 40]):
                with self.assertRaisesRegex(ValueError, "changed before provider commit"):
                    publish({}, sha)
            with patch("scripts.publish_hf.source_files", return_value={}), patch("scripts.publish_hf._current_main_sha", return_value="0" * 40):
                with self.assertRaisesRegex(ValueError, "current remote main"):
                    publish({}, sha)
        with patch.dict("os.environ", {"GITHUB_REF": "refs/heads/main", "GITHUB_SHA": sha, "SZL_HF_PUBLISH_APPROVED": ""}):
            with self.assertRaisesRegex(ValueError, "explicit publication gate"):
                publish({}, sha)


if __name__ == "__main__":
    unittest.main()
