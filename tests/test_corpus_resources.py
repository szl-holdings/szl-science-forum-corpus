"""Distribution resources must retain the reviewed source bytes and build semantics."""

import contextlib
import hashlib
from importlib import resources
import io
import json
from pathlib import Path
import tempfile
import tomllib
import unittest

from szl_forum_corpus.cli import main
from szl_forum_corpus.release_validation import RECORD_SCHEMA_SHA256


ROOT = Path(__file__).resolve().parents[1]
SOURCE_RESOURCES = {
    "opportunities.json": ROOT / "opportunities.json",
    "record-v2.schema.json": ROOT / "schema/record-v2.schema.json",
}


class CorpusResourceTests(unittest.TestCase):
    def test_package_resources_match_reviewed_source_and_declared_distribution(self):
        config = tomllib.loads((ROOT / "pyproject.toml").read_text())
        declared = config["tool"]["setuptools"]["package-data"]["szl_forum_corpus"]
        for name, source in SOURCE_RESOURCES.items():
            with self.subTest(name=name):
                self.assertIn(f"data/{name}", declared)
                bundled = resources.files("szl_forum_corpus").joinpath("data", name).read_bytes()
                self.assertEqual(bundled, source.read_bytes())
        self.assertEqual(
            hashlib.sha256(SOURCE_RESOURCES["record-v2.schema.json"].read_bytes()).hexdigest(),
            RECORD_SCHEMA_SHA256,
        )

    def test_default_resources_and_explicit_opportunities_build_identical_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            default, explicit = root / "default", root / "explicit"
            args = [
                "build", str(ROOT / "examples/operator_topics.jsonl"),
                "--as-of", "2026-10-02",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([*args, "--out", str(default)]), 0)
                self.assertEqual(main([
                    *args, "--out", str(explicit),
                    "--opportunities", str(ROOT / "opportunities.json"),
                ]), 0)
            self.assertEqual(
                {path.name: path.read_bytes() for path in default.iterdir()},
                {path.name: path.read_bytes() for path in explicit.iterdir()},
            )
            manifest = json.loads((default / "manifest.json").read_bytes())
            self.assertFalse(manifest["model_training_authorized"])
            self.assertIn("model_training", manifest["release_contract"]["excluded_purposes"])


if __name__ == "__main__":
    unittest.main()
