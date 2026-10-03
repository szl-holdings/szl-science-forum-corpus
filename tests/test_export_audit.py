import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from szl_forum_corpus.export_audit import audit_export, main


MARKER = "SYNTHETIC_PRIVATE_MARKER_researcher@example.test"


def topic(identifier=426, ids=(101, 102, 103)):
    return {
        "id": identifier, "category_id": 8, "archetype": "regular",
        "visible": True, "posts_count": len(ids), "title": MARKER,
        "post_stream": {
            "stream": list(ids),
            "posts": [
                {"id": post_id, "topic_id": identifier, "post_number": 1 + index * 2,
                 "post_type": 1, "cooked": MARKER, "raw": MARKER, "username": MARKER}
                for index, post_id in enumerate(ids)
            ],
        },
    }


class ExportAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "topics").mkdir()
        self.inventory = {
            "schema": "szl-discourse-export-inventory-v1",
            "site": "https://ai4science.discourse.group",
            "scope": "declared_topic_set",
            "collected_at": "2026-10-02T12:00:00Z", "topics": [],
        }
        self.payload = topic()
        self.write_topic(self.payload)

    def write_inventory(self):
        (self.root / "inventory.json").write_text(json.dumps(self.inventory), encoding="utf-8")

    def write_topic(self, payload, declared_id=None, expected_ids=None):
        identifier = declared_id if declared_id is not None else payload["id"]
        data = json.dumps(payload).encode()
        (self.root / "topics" / f"{identifier}.json").write_bytes(data)
        entry = {
            "topic_id": identifier, "category_id": 8,
            "post_ids": expected_ids if expected_ids is not None else list(payload["post_stream"]["stream"]),
            "file_sha256": hashlib.sha256(data).hexdigest(),
        }
        self.inventory["topics"] = [item for item in self.inventory["topics"] if item["topic_id"] != identifier] + [entry]
        self.write_inventory()

    def test_complete_receipt_binds_bytes_but_grants_no_rights_or_forum_census(self):
        receipt = audit_export(self.root)
        self.assertEqual(receipt["state"], "DECLARED_EXPORT_COMPLETE")
        self.assertEqual(receipt["inventory_sha256"], hashlib.sha256((self.root / "inventory.json").read_bytes()).hexdigest())
        self.assertEqual(receipt["topics"][0]["file_sha256"], self.inventory["topics"][0]["file_sha256"])
        self.assertEqual(receipt["forum_wide_coverage"], "NOT_ESTABLISHED")
        self.assertEqual(receipt["access_and_rights_verification"], "NOT_PERFORMED")
        self.assertEqual(receipt["publication_state"], "REVIEW_REQUIRED")
        self.assertFalse(receipt["model_training_authorized"])
        encoded = json.dumps(receipt)
        self.assertNotIn(MARKER, encoded)
        self.assertNotIn("cooked", encoded)
        self.assertNotIn("username", encoded)

    def test_truncated_hydration_and_equal_counts_with_wrong_ids_are_incomplete(self):
        for loaded in ([101, 102], [], [101, 102, 999]):
            with self.subTest(loaded=loaded):
                payload = topic(ids=tuple(loaded)) if loaded else topic()
                if not loaded:
                    payload["post_stream"]["posts"] = []
                payload["post_stream"]["stream"] = [101, 102, 103]
                payload["posts_count"] = 3
                self.write_topic(payload)
                receipt = audit_export(self.root)
                self.assertEqual(receipt["state"], "INCOMPLETE")
                self.assertIn(103, receipt["topics"][0]["missing_hydrated_ids"])
                if 999 in loaded:
                    self.assertEqual(receipt["topics"][0]["unexpected_hydrated_ids"], [999])

    def test_declared_inventory_catches_coherently_truncated_topic(self):
        self.write_topic(topic(ids=(101, 102)), expected_ids=[101, 102, 103])
        receipt = audit_export(self.root)
        self.assertEqual(receipt["state"], "INCOMPLETE")
        self.assertEqual(receipt["topics"][0]["missing_stream_ids"], [103])

    def test_count_disagreement_is_unresolved_even_when_all_ids_match(self):
        for count in (2, 4):
            self.payload["posts_count"] = count
            self.write_topic(self.payload)
            receipt = audit_export(self.root)
            self.assertEqual(receipt["state"], "INCOMPLETE")
            self.assertEqual(receipt["topics"][0]["state"], "COUNT_UNRESOLVED")

    def test_order_and_gaps_in_post_numbers_do_not_change_coverage(self):
        payload = topic()
        payload["post_stream"]["posts"].reverse()
        payload["post_stream"]["stream"].reverse()
        self.write_topic(payload)
        self.write_topic(topic(427, (201,)))
        receipt = audit_export(self.root)
        self.assertEqual(receipt["state"], "DECLARED_EXPORT_COMPLETE")
        self.assertEqual([item["topic_id"] for item in receipt["topics"]], [426, 427])
        self.inventory["topics"].reverse()
        self.write_inventory()
        second = audit_export(self.root)
        self.assertEqual(receipt["topics"], second["topics"])
        self.assertNotEqual(receipt["inventory_sha256"], second["inventory_sha256"])

    def test_committed_synthetic_example_is_complete_and_content_free(self):
        root = Path(__file__).resolve().parents[1] / "examples" / "export-audit-synthetic"
        receipt = audit_export(root)
        self.assertEqual(receipt["state"], "DECLARED_EXPORT_COMPLETE")
        self.assertEqual(receipt["declared_topic_count"], 2)
        self.assertEqual(sum(item["hydrated_post_count"] for item in receipt["topics"]), 3)
        self.assertFalse(receipt["model_training_authorized"])
        self.assertNotIn("Original synthetic test record", json.dumps(receipt))

    def test_duplicates_at_each_level_are_rejected(self):
        payloads = []
        duplicate_stream = topic()
        duplicate_stream["post_stream"]["stream"] = [101, 101, 103]
        payloads.append(duplicate_stream)
        duplicate_post = topic()
        duplicate_post["post_stream"]["posts"][1]["id"] = 101
        payloads.append(duplicate_post)
        duplicate_number = topic()
        duplicate_number["post_stream"]["posts"][1]["post_number"] = 1
        payloads.append(duplicate_number)
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "duplicate"):
                self.write_topic(payload, expected_ids=[101, 102, 103])
                audit_export(self.root)
        self.write_topic(topic())
        self.inventory["topics"].append(copy.deepcopy(self.inventory["topics"][0]))
        self.write_inventory()
        with self.assertRaisesRegex(ValueError, "duplicate topic"):
            audit_export(self.root)

    def test_identity_spoofing_and_cross_topic_id_reuse_are_rejected(self):
        payloads = [topic(427), topic()]
        payloads[1]["post_stream"]["posts"][0]["topic_id"] = 427
        for payload in payloads:
            self.write_topic(payload, declared_id=426)
            with self.assertRaises(ValueError):
                audit_export(self.root)
        self.write_topic(topic())
        self.write_topic(topic(427))
        with self.assertRaisesRegex(ValueError, "reused across topics"):
            audit_export(self.root)

    def test_missing_topic_file_and_unlisted_file_do_not_pass(self):
        (self.root / "topics" / "426.json").unlink()
        receipt = audit_export(self.root)
        self.assertEqual(receipt["state"], "INCOMPLETE")
        self.assertEqual(receipt["topics"][0]["state"], "MISSING_TOPIC_FILE")
        self.write_topic(topic())
        (self.root / "topics" / "427.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unlisted"):
            audit_export(self.root)

    def test_changed_bytes_fail_binding_before_parsing(self):
        path = self.root / "topics" / "426.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            audit_export(self.root)

    def test_invalid_identifiers_counters_and_missing_content_fail_closed(self):
        for value in (True, False, -1, 1.5, "3", None):
            for field in ("id", "posts_count"):
                payload = topic()
                payload[field] = value
                self.write_topic(payload, declared_id=426)
                with self.subTest(value=value, field=field), self.assertRaises(ValueError):
                    audit_export(self.root)
        payload = topic()
        del payload["post_stream"]["posts"][0]["cooked"]
        self.write_topic(payload)
        with self.assertRaisesRegex(ValueError, "content field"):
            audit_export(self.root)

    def test_private_deleted_hidden_draft_and_mega_topics_are_unsupported(self):
        changes = [
            ("archetype", "private_message"), ("visible", False),
            ("deleted_at", "2026-10-02"), ("is_shared_draft", True),
            ("category_id", 9),
        ]
        for key, value in changes:
            payload = topic()
            payload[key] = value
            self.write_topic(payload)
            with self.subTest(key=key), self.assertRaises(ValueError):
                audit_export(self.root)
        for key, value in (("hidden", True), ("deleted_at", "2026-10-02"), ("post_type", 4), ("user_deleted", True)):
            payload = topic()
            payload["post_stream"]["posts"][0][key] = value
            self.write_topic(payload)
            with self.subTest(key=key), self.assertRaises(ValueError):
                audit_export(self.root)
        payload = topic()
        payload["post_stream"]["isMegaTopic"] = True
        self.write_topic(payload)
        with self.assertRaisesRegex(ValueError, "post stream"):
            audit_export(self.root)

    def test_path_fields_and_links_are_rejected_without_reading_target(self):
        self.inventory["topics"][0]["file"] = "../outside.json"
        self.write_inventory()
        with self.assertRaisesRegex(ValueError, "fields differ"):
            audit_export(self.root)
        self.write_topic(topic())
        target = self.root / "outside.json"
        target.write_text(MARKER, encoding="utf-8")
        path = self.root / "topics" / "426.json"
        path.unlink()
        try:
            path.symlink_to(target)
        except OSError:
            # Windows CI can lack symlink privileges; still exercise rejection.
            path.write_text(MARKER, encoding="utf-8")
            with patch.object(Path, "is_symlink", lambda self: self.name == "426.json"):
                with self.assertRaisesRegex(ValueError, "linked"):
                    audit_export(self.root)
        else:
            with self.assertRaisesRegex(ValueError, "linked"):
                audit_export(self.root)

    def test_linked_topic_directory_and_inventory_are_rejected(self):
        for linked_name in ("topics", "inventory.json"):
            with self.subTest(linked_name=linked_name):
                with patch.object(Path, "is_symlink", lambda path: path.name == linked_name):
                    with self.assertRaisesRegex(ValueError, "link"):
                        audit_export(self.root)

    def test_nested_boolean_post_ids_are_not_accepted_as_integers(self):
        for field in ("id", "topic_id", "post_number", "post_type"):
            payload = topic()
            payload["post_stream"]["posts"][0][field] = True
            self.write_topic(payload)
            with self.subTest(field=field), self.assertRaises(ValueError):
                audit_export(self.root)

    def test_duplicate_keys_and_malformed_json_never_leak_into_cli_output(self):
        for raw in (
            ('{"' + MARKER + '":1,"' + MARKER + '":2}').encode(),
            b'{"id":426,"\\u0069d":427}',
            ('{"cooked":"' + MARKER).encode(),
            b'\xff',
        ):
            path = self.root / "topics" / "426.json"
            path.write_bytes(raw)
            self.inventory["topics"][0]["file_sha256"] = hashlib.sha256(raw).hexdigest()
            self.write_inventory()
            stdout, stderr = io.StringIO(), io.StringIO()
            output = self.root / "receipt.json"
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = main([str(self.root), "--out", str(output)])
            self.assertEqual(result, 2)
            self.assertFalse(output.exists())
            self.assertNotIn(MARKER, stdout.getvalue() + stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_nonzero_for_incomplete_and_refuses_receipt_overwrite(self):
        payload = topic()
        payload["post_stream"]["posts"].pop()
        self.write_topic(payload)
        output = self.root / "receipt.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.root), "--out", str(output)]), 1)
            before = output.read_bytes()
            self.write_topic(topic())
            self.assertEqual(main([str(self.root), "--out", str(output)]), 2)
        self.assertEqual(output.read_bytes(), before)

    def test_receipt_cannot_change_audited_topics_including_resolved_alias(self):
        output = self.root / "topics" / "receipt.json"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.root), "--out", str(output)]), 2)
        self.assertFalse(output.exists())
        alias = self.root / "alias" / "receipt.json"
        original_resolve = Path.resolve

        def resolved(path, *args, **kwargs):
            if path == alias:
                return output
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolved), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.root), "--out", str(alias)]), 2)
        self.assertFalse(alias.parent.exists())
        self.assertEqual(audit_export(self.root)["state"], "DECLARED_EXPORT_COMPLETE")

    def test_path_resolution_loop_failure_does_not_leak_paths_or_tracebacks(self):
        stderr = io.StringIO()
        with patch.object(Path, "resolve", side_effect=RuntimeError(MARKER)):
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(main([str(self.root), "--out", str(self.root / "receipt.json")]), 2)
        self.assertNotIn(MARKER, stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse((self.root / "receipt.json").exists())

    def test_limits_wrong_scope_empty_inventory_and_secret_filenames_are_rejected(self):
        with patch("szl_forum_corpus.export_audit.MAX_TOPIC_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "byte limit"):
                audit_export(self.root)
        for key, value in (("topics", []), ("scope", "entire_forum"), ("site", "https://other.test"), ("collected_at", "2026-02-30T12:00:00Z")):
            inventory = copy.deepcopy(self.inventory)
            self.inventory[key] = value
            self.write_inventory()
            with self.subTest(key=key), self.assertRaises(ValueError):
                audit_export(self.root)
            self.inventory = inventory
        self.write_inventory()
        (self.root / "topics" / MARKER).write_text(MARKER, encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main([str(self.root), "--out", str(self.root / "receipt.json")]), 2)
        self.assertNotIn(MARKER, stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
