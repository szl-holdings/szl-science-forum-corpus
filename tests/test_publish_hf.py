import json
import unittest
from unittest.mock import patch

from scripts.publish_hf import _local_git_sha, public_files, publish


class ProviderGateTests(unittest.TestCase):
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
        with patch.dict("os.environ", {"GITHUB_REF": "refs/heads/main", "GITHUB_SHA": sha, "SZL_HF_PUBLISH_APPROVED": ""}):
            with self.assertRaisesRegex(ValueError, "explicit publication gate"):
                publish({}, sha)


if __name__ == "__main__":
    unittest.main()
