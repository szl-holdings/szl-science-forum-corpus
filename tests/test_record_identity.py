"""Canonical ASCII source identities agree with the schema at both boundaries."""

import copy
import json
from pathlib import Path
import re
import tempfile
import unittest

from szl_forum_corpus.cli import read_records, validate_record
from szl_forum_corpus.release_validation import validate_release_contract


ROOT = Path(__file__).resolve().parents[1]
INVALID_COMPONENTS = ("0", "00", "01", "0426", "١", "٤٢٦", "１", "４２６", "4٢6")


class RecordIdentityTests(unittest.TestCase):
    def setUp(self):
        self.record = json.loads((ROOT / "examples/operator_topics.jsonl").read_text().splitlines()[0])
        self.manifest = json.loads((ROOT / "dataset/manifest.json").read_bytes())
        self.public = json.loads((ROOT / "dataset/sources.public.jsonl").read_bytes().splitlines()[1])
        schema = json.loads((ROOT / "schema/record-v2.schema.json").read_bytes())
        self.pattern = schema["properties"]["source_id"]["pattern"]

    def test_cli_and_independent_release_validator_reject_aliases_in_either_component(self):
        for component in INVALID_COMPONENTS:
            for topic, post in ((component, "1"), ("426", component)):
                source_id = f"ai4science:{topic}:{post}"
                with self.subTest(source_id=source_id):
                    self.assertIsNone(re.fullmatch(self.pattern, source_id))
                    record = {
                        **self.record, "source_id": source_id,
                        "topic_id": int(topic), "post_number": int(post),
                    }
                    # Make the numeric identity and URL agree: rejection must
                    # come from noncanonical source_id, not a later URL check.
                    record["source_url"] = (
                        f"https://ai4science.discourse.group/t/synthetic/{int(topic)}/{int(post)}"
                    )
                    with self.assertRaisesRegex(ValueError, "invalid source_id"):
                        validate_record(record, 1)
                    public = {**self.public, **{
                        key: record[key] for key in (
                            "source_id", "topic_id", "post_number", "source_url"
                        )
                    }}
                    raw = (json.dumps(public, sort_keys=True) + "\n").encode()
                    with self.assertRaisesRegex(ValueError, "invalid source_id"):
                        validate_release_contract(self.manifest, raw)

    def test_alias_cannot_create_a_second_row_for_the_same_numeric_post(self):
        alias = {**copy.deepcopy(self.record), "source_id": "ai4science:0426:01"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in (self.record, alias)) + "\n")
            with self.assertRaisesRegex(ValueError, "invalid source_id"):
                read_records(path)

    def test_canonical_positive_ascii_ids_remain_valid(self):
        for topic, post in ((1, 1), (426, 12), (900001, 123)):
            source_id = f"ai4science:{topic}:{post}"
            self.assertIsNotNone(re.fullmatch(self.pattern, source_id))
            record = {
                **self.record, "source_id": source_id, "topic_id": topic, "post_number": post,
                "source_url": f"https://ai4science.discourse.group/t/synthetic/{topic}/{post}",
            }
            self.assertEqual(validate_record(record, 1)["source_id"], source_id)
        # Independent validation of the unchanged real public projection.
        validate_release_contract(
            self.manifest, (ROOT / "dataset/sources.public.jsonl").read_bytes()
        )


if __name__ == "__main__":
    unittest.main()
