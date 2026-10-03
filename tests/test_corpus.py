import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from szl_forum_corpus.cli import build, main, may_publish, read_records, validate_record


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "operator_topics.jsonl"
OPPORTUNITIES = json.loads((ROOT / "opportunities.json").read_text(encoding="utf-8"))


class CorpusContractTests(unittest.TestCase):
    def setUp(self):
        self.record = json.loads(EXAMPLE.read_text(encoding="utf-8").splitlines()[0])

    def test_operator_source_build_has_traceable_but_exploratory_hypotheses(self):
        outputs = build(read_records(EXAMPLE), OPPORTUNITIES, "2026-10-02")
        manifest = json.loads(outputs["manifest.json"])
        graph = json.loads(outputs["graph.json"])
        hypotheses = json.loads(outputs["hypotheses.json"])
        self.assertEqual(manifest["source_records_public"], 1)
        self.assertEqual(manifest["independent_public_topics"], 1)
        self.assertFalse(manifest["model_training_authorized"])
        self.assertEqual(len(graph["nodes"]), 10)
        self.assertTrue(any(edge["relation"] == "raises_candidate_need" for edge in graph["edges"]))
        self.assertTrue(all(card["evidence_state"] == "exploratory" for card in hypotheses))
        self.assertEqual(
            {node["id"] for node in graph["nodes"] if node["type"] == "ProposedNeed"},
            {"need:experiment_design", "need:evidence_provenance"},
        )
        self.assertTrue(all(node["independent_topic_count"] == 0 for node in graph["nodes"] if node["type"] == "ProposedNeed"))
        self.assertEqual(
            {need["need_id"] for need in json.loads(outputs["needs.json"])},
            {"artifact_replay", "blocked_allocation", "measurement_harmonization", "skill_import_provenance"},
        )
        self.assertEqual(hypotheses[0]["proposed_only_need_ids"], ["experiment_design"])
        self.assertEqual(hypotheses[0]["observed_need_ids"], ["blocked_allocation"])
        self.assertEqual(manifest["forum_inventory_status"], "not_established_by_this_build")
        self.assertRegex(manifest["public_source_records_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn(b"Following my earlier", outputs["sources.public.jsonl"])
        self.assertNotIn(b"betterwithage", outputs["sources.public.jsonl"])
        candidate = json.loads(outputs["second_brain.candidates.jsonl"])
        self.assertEqual(candidate["source"], "forum_insight")
        self.assertEqual(candidate["sha256"], hashlib.sha256(candidate["text"].encode()).hexdigest())

    def test_unknown_rights_and_third_party_member_post_are_withheld(self):
        unknown = copy.deepcopy(self.record)
        unknown["publication_rights"] = "unknown"
        self.assertFalse(may_publish(validate_record(unknown, 1)))
        third_party = copy.deepcopy(self.record)
        third_party["publication_rights"] = "licensed"
        self.assertFalse(may_publish(validate_record(third_party, 1)))
        unreviewed = copy.deepcopy(self.record)
        unreviewed["review_state"] = "unreviewed"
        self.assertFalse(may_publish(validate_record(unreviewed, 1)))
        outputs = build([validate_record(third_party, 1)], OPPORTUNITIES, "2026-10-02")
        self.assertEqual(outputs["sources.public.jsonl"], b"")
        self.assertEqual(json.loads(outputs["manifest.json"])["source_records_withheld"], 1)

    def test_raw_text_and_sensitive_fields_are_rejected(self):
        for field in ("body", "cooked", "email", "username", "raw"):
            record = {**self.record, field: "private data"}
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_record(record, 1)
        email = {**self.record, "summary": "Contact researcher@example.org for details"}
        with self.assertRaisesRegex(ValueError, "email address"):
            validate_record(email, 1)
        for summary in ("Call +1 (415) 555-0199", "api_key=super-private-key", "hf_abcdefghijklmnopqrstuvwxyz123456"):
            with self.subTest(summary=summary), self.assertRaisesRegex(ValueError, "contact number or credential"):
                validate_record({**self.record, "summary": summary}, 1)

    def test_private_attribution_does_not_change_public_fingerprint(self):
        first = build([validate_record(self.record, 1)], OPPORTUNITIES, "2026-10-02")
        revised = {**self.record, "attribution": "different_private_handle"}
        second = build([validate_record(revised, 1)], OPPORTUNITIES, "2026-10-02")
        self.assertEqual(first, second)
        self.assertNotIn(b"different_private_handle", b"".join(second.values()))
        manifest = json.loads(second["manifest.json"])
        self.assertEqual(
            manifest["public_source_records_sha256"],
            hashlib.sha256(second["sources.public.jsonl"]).hexdigest(),
        )

    def test_unsafe_url_and_duplicate_source_fail_closed(self):
        unsafe = {**self.record, "source_url": self.record["source_url"] + "?api_key=secret"}
        with self.assertRaises(ValueError):
            validate_record(unsafe, 1)
        mismatched_reply = {
            **self.record,
            "source_id": "ai4science:426:2",
            "post_number": 2,
        }
        with self.assertRaisesRegex(ValueError, "source_url"):
            validate_record(mismatched_reply, 1)
        mismatched_reply["source_url"] += "/3"
        with self.assertRaisesRegex(ValueError, "source_url"):
            validate_record(mismatched_reply, 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.jsonl"
            path.write_text(json.dumps(self.record) + "\n" + json.dumps(self.record) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_records(path)

    def test_duplicate_json_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate-key.jsonl"
            path.write_text('{"source_id":"one","source_id":"two"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                read_records(path)

    def test_two_posts_in_one_topic_count_once(self):
        second = copy.deepcopy(self.record)
        second["source_id"] = "ai4science:426:2"
        second["post_number"] = 2
        second["source_url"] += "/2"
        outputs = build([validate_record(self.record, 1), validate_record(second, 2)], OPPORTUNITIES, "2026-10-02")
        self.assertEqual(json.loads(outputs["manifest.json"])["independent_public_topics"], 1)
        self.assertTrue(all(need["independent_topic_count"] == 1 for need in json.loads(outputs["needs.json"])))

    def test_cli_refuses_overwrite_without_partial_change(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "output"
            self.assertEqual(main(["build", str(EXAMPLE), "--out", str(target), "--as-of", "2026-10-02"]), 0)
            before = (target / "manifest.json").read_bytes()
            self.assertEqual(main(["build", str(EXAMPLE), "--out", str(target), "--as-of", "2026-10-03"]), 2)
            self.assertEqual((target / "manifest.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
