"""Malformed JSON enum shapes receive a controlled refusal before output."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from szl_forum_corpus.cli import validate_record


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "operator_topics.jsonl"
FIELDS = ("access", "publication_rights", "review_state")
INVALID_VALUES = ([], {}, ["public_web"], {"value": "operator_authorized"}, None, True, 1)


class RecordEnumBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.record = json.loads(EXAMPLE.read_text(encoding="utf-8").splitlines()[0])

    def test_non_string_enum_values_raise_validation_error(self):
        for field in FIELDS:
            for value in INVALID_VALUES:
                with self.subTest(field=field, value=value):
                    record = copy.deepcopy(self.record)
                    record[field] = value
                    with self.assertRaisesRegex(ValueError, field):
                        validate_record(record, 1)

    def test_cli_refuses_malformed_enums_without_traceback_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "records.jsonl"
            output = root / "public"
            for field in FIELDS:
                for value in ([], {"private": "not-for-output"}):
                    for command in ("validate", "build"):
                        with self.subTest(field=field, value=value, command=command):
                            record = copy.deepcopy(self.record)
                            record[field] = value
                            source.write_text(json.dumps(record) + "\n", encoding="utf-8")
                            args = [sys.executable, "-m", "szl_forum_corpus", command, str(source)]
                            if command == "build":
                                args += ["--out", str(output), "--as-of", "2026-10-02"]
                            result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                                                    timeout=10)
                            self.assertEqual(result.returncode, 2, result.stderr)
                            self.assertEqual(result.stdout, "")
                            self.assertIn(field, result.stderr)
                            self.assertNotIn("Traceback", result.stderr)
                            self.assertNotIn("not-for-output", result.stderr)
                            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
