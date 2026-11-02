import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import unicodedata

from szl_forum_corpus.cli import build, main, may_publish, read_records, validate_record
from szl_forum_corpus.release_validation import validate_release_contract


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "operator_topics.jsonl"
OPPORTUNITIES = json.loads((ROOT / "opportunities.json").read_text(encoding="utf-8"))


class CorpusContractTests(unittest.TestCase):
    def setUp(self):
        self.record = json.loads(EXAMPLE.read_text(encoding="utf-8").splitlines()[0])

    def test_operator_sources_build_has_traceable_but_exploratory_hypotheses(self):
        outputs = build(read_records(EXAMPLE), OPPORTUNITIES, "2026-10-02")
        manifest = json.loads(outputs["manifest.json"])
        graph = json.loads(outputs["graph.json"])
        hypotheses = json.loads(outputs["hypotheses.json"])
        self.assertEqual(manifest["source_records_public"], 2)
        self.assertEqual(manifest["independent_public_topics"], 2)
        self.assertFalse(manifest["model_training_authorized"])
        self.assertEqual(len([node for node in graph["nodes"] if node["type"] == "Source"]), 2)
        self.assertTrue(any(edge["relation"] == "raises_candidate_need" for edge in graph["edges"]))
        self.assertTrue(all(card["evidence_state"] == "exploratory" for card in hypotheses))
        self.assertEqual(
            {node["id"] for node in graph["nodes"] if node["type"] == "ProposedNeed"},
            {"need:experiment_design", "need:evidence_provenance"},
        )
        self.assertTrue(all(node["independent_topic_count"] == 0 for node in graph["nodes"] if node["type"] == "ProposedNeed"))
        self.assertEqual(
            {need["need_id"] for need in json.loads(outputs["needs.json"])},
            {
                "artifact_replay", "blocked_allocation", "measurement_harmonization",
                "skill_import_provenance", "skill_import_slice", "skill_service_disclosure",
                "skill_update_notice", "skill_name_collision", "skill_share_action",
            },
        )
        self.assertEqual(hypotheses[0]["proposed_only_need_ids"], ["experiment_design"])
        self.assertEqual(hypotheses[0]["observed_need_ids"], ["blocked_allocation"])
        self.assertEqual(manifest["forum_inventory_status"], "not_established_by_this_build")
        self.assertRegex(manifest["public_source_records_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn(b"Following my earlier", outputs["sources.public.jsonl"])
        self.assertNotIn(b"Archive exceeds 100 MB uncompressed", outputs["sources.public.jsonl"])
        candidates = [json.loads(line) for line in outputs["second_brain.candidates.jsonl"].splitlines()]
        self.assertEqual({candidate["id"] for candidate in candidates}, {"forum_insight:396:1", "forum_insight:426:1"})
        for candidate in candidates:
            self.assertEqual(candidate["source"], "forum_insight")
            self.assertEqual(candidate["sha256"], hashlib.sha256(candidate["text"].encode()).hexdigest())
        shared_need = next(need for need in json.loads(outputs["needs.json"]) if need["need_id"] == "skill_import_provenance")
        self.assertEqual(shared_need["independent_topic_count"], 2)
        self.assertEqual(shared_need["source_ids"], ["ai4science:396:1", "ai4science:426:1"])
        public_rows = [json.loads(line) for line in outputs["sources.public.jsonl"].splitlines()]
        self.assertTrue(all("attribution" not in row for row in public_rows))
        self.assertTrue(all("source_content_license" in row for row in public_rows))
        release = manifest["release_contract"]
        self.assertEqual(release["schema"], "szl.forum-corpus-release/v1")
        self.assertEqual(release["source_scope"], "reviewed_public_projection_only")
        self.assertEqual(release["projection_license"], "Apache-2.0")
        self.assertEqual(
            release["projection_license_scope"],
            "original_szl_metadata_summaries_and_annotations",
        )
        self.assertFalse(release["source_content_license_inferred"])
        self.assertIn("model_training", release["excluded_purposes"])
        self.assertEqual(release["temporal_split"]["state"], "UNKNOWN")
        self.assertEqual(release["temporal_split"]["reason"], "NO_ADMITTED_SPLIT_MANIFEST")
        self.assertEqual(release["temporal_split"]["records_with_unknown_event_time"], 2)
        self.assertFalse(release["temporal_split"]["family_separation_verified"])
        self.assertFalse(release["temporal_split"]["temporal_order_verified"])
        bindings = {row["source_id"]: row for row in release["records"]}
        self.assertEqual(set(bindings), {row["source_id"] for row in public_rows})
        for row in public_rows:
            binding = bindings[row["source_id"]]
            encoded = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode()
            self.assertEqual(binding["row_sha256"], hashlib.sha256(encoded).hexdigest())
            self.assertRegex(binding["thread_family_sha256"], r"^[0-9a-f]{64}$")
            messages = [
                {
                    "role": "system",
                    "content": release["forge_disjoint_reference"]["system_message"],
                },
                {"role": "user", "content": f"{row['title']}\n\n{row['summary']}"},
            ]
            normalized = [
                {
                    "role": message["role"],
                    "content": unicodedata.normalize("NFC", message["content"]).strip(),
                }
                for message in messages
            ]
            canonical = json.dumps(
                normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            self.assertEqual(binding["forge_input_sha256"], hashlib.sha256(canonical).hexdigest())
        self.assertEqual(
            release["forge_disjoint_reference"]["fingerprint_schema"],
            "sha256-canonical-chat-input-v1",
        )
        forge = release["forge_disjoint_reference"]
        self.assertEqual(forge["profile_state"], "MERGED_DEPENDENCY")
        self.assertEqual(forge["validator"], {
            "repository": "szl-holdings/szl-forge",
            "revision": "e29ccf6438a0af0b27c5cbcb00f547bc13205c73",
            "path": "tools/validate_sft_dataset.py",
            "sha256": "f86ebe1fc0bd324551664356e29d7e9bb192a8b13ac01d638cd73f0a608dea20",
            "merged_pr": "szl-holdings/szl-forge#673",
            "merge_commit": "972af446bc7dc6e46d397f2d0cbafb22057545b5",
        })
        self.assertEqual(manifest["record_schema"], "schema/record-v2.schema.json")
        validate_release_contract(manifest, outputs["sources.public.jsonl"])

    def test_unknown_rights_and_third_party_member_post_are_withheld(self):
        unknown = copy.deepcopy(self.record)
        unknown["publication_rights"] = "unknown"
        self.assertFalse(may_publish(validate_record(unknown, 1)))
        third_party = copy.deepcopy(self.record)
        third_party["publication_rights"] = "licensed"
        third_party["source_content_license"] = "CC-BY-4.0"
        self.assertFalse(may_publish(validate_record(third_party, 1)))
        unreviewed = copy.deepcopy(self.record)
        unreviewed["review_state"] = "unreviewed"
        self.assertFalse(may_publish(validate_record(unreviewed, 1)))
        outputs = build([validate_record(third_party, 1)], OPPORTUNITIES, "2026-10-02")
        self.assertEqual(outputs["sources.public.jsonl"], b"")
        self.assertEqual(json.loads(outputs["manifest.json"])["source_records_withheld"], 1)

    def test_source_content_license_is_explicit_and_rights_bound(self):
        licensed_without_id = {**self.record, "publication_rights": "licensed"}
        with self.assertRaisesRegex(ValueError, "requires a reviewed source_content_license"):
            validate_record(licensed_without_id, 1)
        licensed = {**licensed_without_id, "source_content_license": "CC-BY-4.0"}
        self.assertEqual(
            validate_record(licensed, 1)["source_content_license"], "CC-BY-4.0"
        )
        with self.assertRaisesRegex(ValueError, "valid only for licensed content"):
            validate_record({**self.record, "source_content_license": "CC-BY-4.0"}, 1)
        with self.assertRaisesRegex(ValueError, "requires a reviewed source_content_license"):
            validate_record(
                {**licensed_without_id, "source_content_license": "LicenseRef-Unknown"}, 1
            )

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
        records = read_records(EXAMPLE)
        first = build(records, OPPORTUNITIES, "2026-10-02")
        revised = [validate_record({**record, "attribution": "different_private_handle"}, index)
                   for index, record in enumerate(records, 1)]
        second = build(revised, OPPORTUNITIES, "2026-10-02")
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
