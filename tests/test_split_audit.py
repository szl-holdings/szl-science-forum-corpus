"""Synthetic, offline regression tests for the declared split metadata audit."""

import copy
import errno
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from szl_forum_corpus import import_export, split_audit


SPLITS = ("discovery", "development", "held_out")
KINDS = ("author", "paper", "duplicate")
TIMES = ("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z")
REPOSITORY = Path(__file__).resolve().parents[1]
ERROR = "ERROR: invalid or unavailable split input/output; receipt not accepted\n"


def family_key(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def record(index, split, event_time, **changes):
    value = {
        "record_id": f"synthetic_record_{index}",
        "thread_id": 800_000_000_000_000_000 + index,
        "split": split,
        "event_time": event_time,
        "author_hashes": [],
        "paper_hashes": [],
        "duplicate_hashes": [],
    }
    value.update(changes)
    return value


def manifest(records=None, input_kind="SIMULATED"):
    if records is None:
        records = [record(index + 1, split, TIMES[index])
                   for index, split in enumerate(SPLITS)]
    return {"schema": "szl-research-splits-v1", "input_kind": input_kind, "records": records}


class SplitAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="split-audit-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.input = self.root / "synthetic-input.json"

    def write_manifest(self, value):
        self.input.write_bytes(json.dumps(value, allow_nan=False).encode("utf-8"))
        return self.input

    def audit(self, value):
        return split_audit.audit_splits(self.write_manifest(value))

    def assert_state(self, receipt, state):
        self.assertEqual(receipt["state"], state)
        self.assertIs(receipt["declared_checks_passed"], state == "MEASURED")

    def run_cli(self, output, input_path=None):
        return subprocess.run(
            [sys.executable, "-B", "-m", "szl_forum_corpus.split_audit",
             str(self.input if input_path is None else input_path), "--out", str(output)],
            cwd=REPOSITORY, capture_output=True, text=True, timeout=10,
        )

    def assert_cli_failure(self, result):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, ERROR)

    def assert_direct_link(self, kind):
        value = manifest()
        if kind == "thread":
            value["records"][1]["thread_id"] = value["records"][0]["thread_id"]
        else:
            key = family_key(f"shared-{kind}")
            # Non-leading keys must link; a key present in three rows counts once.
            for index, row in enumerate(value["records"]):
                row[f"{kind}_hashes"] = [family_key(f"unique-{kind}-{index}"), key]
        receipt = self.audit(value)
        self.assert_state(receipt, "BLOCKED")
        self.assertEqual(receipt["direct_cross_split_keys"], {
            name: int(name == kind) for name in ("thread", *KINDS)
        })
        members = [1, 2] if kind == "thread" else [1, 2, 3]
        splits = list(SPLITS[:2]) if kind == "thread" else list(SPLITS)
        self.assertEqual(receipt["cross_split_families"], [
            {"record_ordinals": members, "splits": splits}
        ])
        self.assertEqual(receipt["connected_family_count"], 2 if kind == "thread" else 1)
        self.assertEqual(receipt["time_order_conflicts"], [])

    def test_complete_metadata_measures_declared_checks_without_authorizing_training(self):
        for input_kind in ("SIMULATED", "SOURCE_METADATA"):
            with self.subTest(input_kind=input_kind):
                value = manifest(input_kind=input_kind)
                # A connected family contained entirely in one split is allowed.
                extra = record(4, "discovery", TIMES[0],
                               thread_id=value["records"][0]["thread_id"])
                value["records"].append(extra)
                receipt = self.audit(value)
                self.assert_state(receipt, "MEASURED")
                self.assertEqual(receipt["schema"], "szl-research-split-audit-v1")
                self.assertEqual(receipt["evidence_class"], "MEASURED")
                self.assertEqual(receipt["input_kind"], input_kind)
                self.assertEqual(receipt["record_count"], 4)
                self.assertEqual(receipt["split_counts"], {
                    "discovery": 2, "development": 1, "held_out": 1,
                })
                self.assertEqual(receipt["connected_family_count"], 3)
                self.assertEqual(receipt["cross_split_families"], [])
                self.assertEqual(receipt["direct_cross_split_keys"], {
                    kind: 0 for kind in ("thread", *KINDS)
                })
                self.assertEqual(receipt["unknown_metadata_counts"], {
                    kind: 0 for kind in (*KINDS, "event_time")
                })
                self.assertEqual(receipt["absent_splits"], [])
                self.assertEqual(receipt["assessment_scope"], "DECLARED_METADATA_ONLY")
                for field in ("metadata_completeness", "pretraining_contamination",
                              "rights_verification"):
                    self.assertEqual(receipt[field], "UNKNOWN")
                self.assertIs(receipt["model_training_authorized"], False)
                self.assertIs(receipt["signed"], False)

    def test_thread_linkage_blocks_cross_split_family(self):
        self.assert_direct_link("thread")

    def test_author_linkage_counts_shared_keys_once(self):
        self.assert_direct_link("author")

    def test_paper_linkage_counts_shared_keys_once(self):
        self.assert_direct_link("paper")

    def test_duplicate_linkage_counts_shared_keys_once(self):
        self.assert_direct_link("duplicate")

    def test_transitive_links_merge_existing_families_across_all_splits(self):
        author_a, paper, duplicate, author_b = map(family_key, ("a", "p", "d", "b"))
        shared_thread = 800_000_000_000_000_004
        rows = [
            record(1, "discovery", TIMES[0], author_hashes=[author_a]),
            record(2, "discovery", TIMES[0], author_hashes=[author_a], paper_hashes=[paper]),
            record(3, "development", TIMES[1], paper_hashes=[paper], duplicate_hashes=[duplicate]),
            record(4, "development", TIMES[1], thread_id=shared_thread, duplicate_hashes=[duplicate]),
            record(5, "held_out", TIMES[2], thread_id=shared_thread,
                   author_hashes=[author_b]),
            record(6, "held_out", TIMES[2], author_hashes=[author_b]),
        ]
        for order in ((0, 1, 2, 3, 4, 5), (0, 5, 2, 4, 1, 3)):
            with self.subTest(order=order):
                receipt = self.audit(manifest([rows[index] for index in order]))
                self.assert_state(receipt, "BLOCKED")
                self.assertEqual(receipt["connected_family_count"], 1)
                self.assertEqual(receipt["cross_split_families"], [
                    {"record_ordinals": [1, 2, 3, 4, 5, 6], "splits": list(SPLITS)}
                ])
                self.assertEqual(receipt["direct_cross_split_keys"], {
                    "thread": 1, "author": 0, "paper": 1, "duplicate": 0,
                })
                self.assertEqual(receipt["time_order_conflicts"], [])

    def test_identical_hashes_in_different_namespaces_do_not_link(self):
        value = manifest()
        shared = family_key("same-bytes-different-namespaces")
        for row, kind in zip(value["records"], KINDS):
            row[f"{kind}_hashes"] = [shared]
        receipt = self.audit(value)
        self.assert_state(receipt, "MEASURED")
        self.assertEqual(receipt["connected_family_count"], 3)
        self.assertEqual(receipt["cross_split_families"], [])
        self.assertEqual(receipt["direct_cross_split_keys"], {
            kind: 0 for kind in ("thread", *KINDS)
        })

    def test_chronology_uses_extreme_times_and_ignores_input_order(self):
        rows = [
            record(1, "discovery", "2026-01-31T23:59:59Z"),
            record(2, "discovery", TIMES[0]),
            record(3, "development", "2026-02-28T23:59:59Z"),
            record(4, "development", TIMES[1]),
            record(5, "held_out", "2026-03-31T23:59:59Z"),
            record(6, "held_out", TIMES[2]),
        ]
        receipt = self.audit(manifest(list(reversed(rows))))
        self.assert_state(receipt, "MEASURED")
        self.assertEqual(receipt["time_windows"], {
            "discovery": {"earliest": TIMES[0], "latest": "2026-01-31T23:59:59Z"},
            "development": {"earliest": TIMES[1], "latest": "2026-02-28T23:59:59Z"},
            "held_out": {"earliest": TIMES[2], "latest": "2026-03-31T23:59:59Z"},
        })
        self.assertEqual(receipt["time_order_conflicts"], [])
        # Checking just one representative row would miss this late discovery.
        rows.append(record(7, "discovery", "2026-02-01T00:00:01Z"))
        receipt = self.audit(manifest(rows))
        self.assert_state(receipt, "BLOCKED")
        self.assertEqual(receipt["time_order_conflicts"], [
            {"earlier_split": "discovery", "later_split": "development"}
        ])

    def test_ties_and_inversions_block_every_chronological_split_pair(self):
        for times, expected in (
            ((TIMES[1], TIMES[1], TIMES[2]), [("discovery", "development")]),
            ((TIMES[0], TIMES[2], TIMES[2]), [("development", "held_out")]),
            ((TIMES[0],) * 3,
             [("discovery", "development"), ("discovery", "held_out"),
              ("development", "held_out")]),
            (tuple(reversed(TIMES)),
             [("discovery", "development"), ("discovery", "held_out"),
              ("development", "held_out")]),
        ):
            with self.subTest(times=times):
                value = manifest([record(i + 1, split, times[i])
                                  for i, split in enumerate(SPLITS)])
                receipt = self.audit(value)
                self.assert_state(receipt, "BLOCKED")
                self.assertEqual(receipt["cross_split_families"], [])
                self.assertEqual(receipt["time_order_conflicts"], [
                    {"earlier_split": left, "later_split": right} for left, right in expected
                ])

    def test_each_null_metadata_field_is_unknown_but_empty_families_are_known(self):
        for kind in (*KINDS, "event_time"):
            with self.subTest(kind=kind):
                value = manifest()
                field = "event_time" if kind == "event_time" else f"{kind}_hashes"
                value["records"][1][field] = None
                receipt = self.audit(value)
                self.assert_state(receipt, "UNKNOWN")
                self.assertEqual(receipt["unknown_metadata_counts"], {
                    name: int(name == kind) for name in (*KINDS, "event_time")
                })
                self.assertEqual(receipt["absent_splits"], [])
                self.assertEqual(receipt["cross_split_families"], [])
                self.assertEqual(receipt["time_order_conflicts"], [])
                if kind == "event_time":
                    self.assertEqual(receipt["time_windows"]["development"], {
                        "earliest": None, "latest": None,
                    })
        self.assert_state(self.audit(manifest()), "MEASURED")

    def test_all_missing_metadata_counts_records_and_retains_unknown_windows(self):
        value = manifest()
        for row in value["records"]:
            row["event_time"] = None
            for kind in KINDS:
                row[f"{kind}_hashes"] = None
        receipt = self.audit(value)
        self.assert_state(receipt, "UNKNOWN")
        self.assertEqual(receipt["unknown_metadata_counts"], {
            kind: 3 for kind in (*KINDS, "event_time")
        })
        self.assertEqual(receipt["time_windows"], {
            split: {"earliest": None, "latest": None} for split in SPLITS
        })
        self.assertEqual(receipt["time_order_conflicts"], [])

    def test_each_absent_split_and_multiple_absent_splits_are_unknown(self):
        for absent in (("discovery",), ("development",), ("held_out",),
                       ("development", "held_out")):
            with self.subTest(absent=absent):
                value = manifest()
                value["records"] = [row for row in value["records"] if row["split"] not in absent]
                receipt = self.audit(value)
                self.assert_state(receipt, "UNKNOWN")
                self.assertEqual(receipt["absent_splits"], list(absent))
                self.assertEqual(receipt["unknown_metadata_counts"], {
                    kind: 0 for kind in (*KINDS, "event_time")
                })
                for split in absent:
                    self.assertEqual(receipt["split_counts"][split], 0)
                    self.assertEqual(receipt["time_windows"][split], {
                        "earliest": None, "latest": None,
                    })

    def test_family_and_time_conflicts_remain_blocked_despite_unknowns(self):
        for conflict in ("family", "time"):
            with self.subTest(conflict=conflict):
                rows = [record(1, "discovery", TIMES[0]), record(2, "held_out", TIMES[2])]
                if conflict == "family":
                    rows[1]["thread_id"] = rows[0]["thread_id"]
                    for row in rows:
                        row["event_time"] = None
                else:
                    # Development is absent; known discovery/held-out dates still conflict.
                    rows[0]["event_time"] = TIMES[2]
                    rows.append(record(3, "held_out", None))
                for row in rows:
                    for kind in KINDS:
                        row[f"{kind}_hashes"] = None
                receipt = self.audit(manifest(rows))
                self.assert_state(receipt, "BLOCKED")
                self.assertEqual(receipt["absent_splits"], ["development"])
                self.assertEqual(receipt["unknown_metadata_counts"], {
                    "author": len(rows), "paper": len(rows), "duplicate": len(rows),
                    "event_time": 2 if conflict == "family" else 1,
                })
                self.assertEqual(receipt["cross_split_families"], [
                    {"record_ordinals": [1, 2], "splits": ["discovery", "held_out"]}
                ] if conflict == "family" else [])
                self.assertEqual(receipt["time_order_conflicts"], [] if conflict == "family" else [
                    {"earlier_split": "discovery", "later_split": "held_out"}
                ])

    def test_hash_bindings_cover_exact_input_and_both_source_files(self):
        value = manifest()
        hashes = []
        for data in (json.dumps(value, separators=(",", ":")).encode("utf-8"),
                     (json.dumps(value, indent=2, sort_keys=True) + "\r\n").encode("utf-8")):
            with self.subTest(bytes=len(data)):
                self.input.write_bytes(data)
                receipt = split_audit.audit_splits(self.input)
                self.assert_state(receipt, "MEASURED")
                self.assertEqual(receipt["input_sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(receipt["harness_sha256"],
                                 hashlib.sha256(Path(split_audit.__file__).read_bytes()).hexdigest())
                self.assertEqual(receipt["reader_sha256"],
                                 hashlib.sha256(Path(import_export.__file__).read_bytes()).hexdigest())
                self.assertEqual(self.input.read_bytes(), data)
                hashes.append(receipt["input_sha256"])
        self.assertNotEqual(*hashes)

    def test_conflict_receipt_contains_ordinals_without_identifiers_or_family_hashes(self):
        value = manifest()
        shared = family_key("synthetic-confidential-family")
        for row in value["records"]:
            for kind in KINDS:
                row[f"{kind}_hashes"] = [shared, family_key(f'{kind}-{row["record_id"]}')]
        value["records"][1]["thread_id"] = value["records"][0]["thread_id"]
        receipt = self.audit(value)
        self.assert_state(receipt, "BLOCKED")
        encoded = json.dumps(receipt, sort_keys=True)
        for row in value["records"]:
            self.assertNotIn(row["record_id"], encoded)
            self.assertNotIn(str(row["thread_id"]), encoded)
            for kind in KINDS:
                self.assertNotIn(f"{kind}_hashes", encoded)
                for key in row[f"{kind}_hashes"]:
                    self.assertNotIn(key, encoded)
        self.assertNotIn('"record_id"', encoded)
        self.assertNotIn('"thread_id"', encoded)
        self.assertEqual(receipt["cross_split_families"], [
            {"record_ordinals": [1, 2, 3], "splits": list(SPLITS)}
        ])

    def test_strict_json_rejects_duplicate_nonfinite_malformed_and_non_utf8_input(self):
        encoded = json.dumps(manifest()).encode("utf-8")
        cases = {
            "duplicate top key": encoded.replace(
                b'"schema":', b'"schema": "szl-research-splits-v1", "schema":', 1),
            "duplicate record key": encoded.replace(
                b'"thread_id":', b'"thread_id": 1, "thread_id":', 1),
            "malformed": encoded[:-1],
            "trailing object": encoded + b"{}",
            "invalid UTF-8": b"\xff" + encoded,
            "deep nesting": b"[" * 2000 + b"0" + b"]" * 2000,
        }
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            cases[token.decode("ascii")] = encoded.replace(b'"author_hashes": []',
                                                          b'"author_hashes": [' + token + b']', 1)
        for name, data in cases.items():
            with self.subTest(case=name):
                self.input.write_bytes(data)
                with self.assertRaises(ValueError):
                    split_audit.audit_splits(self.input)

    def test_manifest_and_record_fields_types_and_enums_are_closed(self):
        cases = [("root type", value) for value in (None, [], True, 7, "manifest")]
        for field in ("schema", "input_kind", "records"):
            value = manifest()
            del value[field]
            cases.append((f"missing root {field}", value))
        value = manifest()
        value["raw"] = "synthetic-unaccepted-content"
        cases.append(("extra root field", value))
        for field, invalids in (
            ("schema", ("szl-research-splits-v2", None, [], 1)),
            ("input_kind", ("MEASURED", "simulated", None, [], True)),
            ("records", (None, {}, "records", True, 1)),
        ):
            for invalid in invalids:
                value = manifest()
                value[field] = invalid
                cases.append((f"{field}={invalid!r}", value))
        for invalid in (None, [], "record", 1, True):
            value = manifest()
            value["records"][0] = invalid
            cases.append((f"record={invalid!r}", value))
        for field in manifest()["records"][0]:
            value = manifest()
            del value["records"][0][field]
            cases.append((f"missing record {field}", value))
        value = manifest()
        value["records"][0]["username"] = "synthetic-unaccepted-content"
        cases.append(("extra record field", value))
        for invalid in ("train", "HELD_OUT", None, True, 0, [], {}):
            value = manifest()
            value["records"][0]["split"] = invalid
            cases.append((f"split={invalid!r}", value))
        for name, value in cases:
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    self.audit(value)

    def test_record_and_thread_identifiers_enforce_types_bounds_and_uniqueness(self):
        for field, invalids in (
            ("record_id", (None, True, 1, [], {}, "", "Upper", "1record", "raw/id",
                           "a" * 81, "r\n", "récord")),
            ("thread_id", (None, True, False, 0, -1, 10**18, 1.0, "1", [], {})),
        ):
            for invalid in invalids:
                with self.subTest(field=field, value=invalid):
                    value = manifest()
                    value["records"][0][field] = invalid
                    with self.assertRaises(ValueError):
                        self.audit(value)
        value = manifest()
        value["records"][1]["record_id"] = value["records"][0]["record_id"]
        with self.assertRaisesRegex(ValueError, "duplicate record ID"):
            self.audit(value)
        value = manifest()
        value["records"][0].update(record_id="a" + "_-0" * 26 + "z", thread_id=1)
        value["records"][1]["thread_id"] = 10**18 - 1
        self.assert_state(self.audit(value), "MEASURED")

    def test_byte_record_and_family_list_budgets_accept_limits_and_reject_overflow(self):
        self.assertEqual((split_audit.MAX_BYTES, split_audit.MAX_RECORDS,
                          split_audit.MAX_KEYS_PER_KIND), (4_000_000, 10_000, 32))
        data = json.dumps(manifest()).encode("utf-8")
        padded = data + b" " * (4_000_000 - len(data))
        self.input.write_bytes(padded)
        receipt = split_audit.audit_splits(self.input)
        self.assert_state(receipt, "MEASURED")
        self.assertEqual(receipt["input_sha256"], hashlib.sha256(padded).hexdigest())
        self.input.write_bytes(padded + b" ")
        with self.assertRaisesRegex(ValueError, "byte budget exceeded"):
            split_audit.audit_splits(self.input)
        rows = [record(i + 1, SPLITS[i % 3], TIMES[i % 3]) for i in range(10_000)]
        receipt = self.audit(manifest(rows))
        self.assert_state(receipt, "MEASURED")
        self.assertEqual(receipt["record_count"], 10_000)
        self.assertEqual(receipt["connected_family_count"], 10_000)
        for invalid_rows in ([], rows + [record(10_001, "held_out", TIMES[2])]):
            with self.subTest(record_count=len(invalid_rows)):
                with self.assertRaisesRegex(ValueError, "record count"):
                    self.audit(manifest(invalid_rows))
        for kind in KINDS:
            with self.subTest(kind=kind):
                value = manifest()
                value["records"][0][f"{kind}_hashes"] = [
                    family_key(f"key-{i}") for i in range(32)
                ]
                self.assert_state(self.audit(value), "MEASURED")
                value["records"][0][f"{kind}_hashes"].append(family_key("overflow"))
                with self.assertRaisesRegex(ValueError, "family key list over limit"):
                    self.audit(value)

    def test_family_keys_require_lists_of_unique_lowercase_sha256_strings(self):
        key = family_key("valid-key")
        invalids = (key, {}, True, 1, [None], [True], [1], [[]], [{}], [""],
                    ["a" * 63], ["a" * 65], ["g" * 64], [key.upper()], [key + "\n"],
                    [key, key])
        for kind in KINDS:
            for invalid in invalids:
                with self.subTest(kind=kind, value=invalid):
                    value = manifest()
                    value["records"][0][f"{kind}_hashes"] = invalid
                    with self.assertRaises(ValueError):
                        self.audit(value)

    def test_event_times_require_valid_utc_seconds_without_coercion(self):
        invalids = (True, 0, [], {}, "", "2026-01-01", "2026-01-01T00:00:00",
                    "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00-05:00",
                    "2026-01-01T00:00:00.001Z", "2026-01-01T00:00:00z",
                    "2026-01-01 00:00:00Z", "2026-1-01T00:00:00Z",
                    "2026-02-29T00:00:00Z", "2026-13-01T00:00:00Z",
                    "2026-04-31T00:00:00Z", "2026-01-00T00:00:00Z",
                    "2026-01-01T24:00:00Z", "2026-01-01T00:60:00Z",
                    "2026-01-01T00:00:60Z", "0000-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z\n")
        for invalid in invalids:
            with self.subTest(value=invalid):
                value = manifest()
                value["records"][0]["event_time"] = invalid
                with self.assertRaises(ValueError):
                    self.audit(value)
        value = manifest()
        value["records"][0]["event_time"] = "2024-02-29T23:59:59Z"
        self.assert_state(self.audit(value), "MEASURED")

    def test_cli_exit_codes_zero_three_and_two_match_receipts_and_sanitized_errors(self):
        passed = manifest()
        blocked = copy.deepcopy(passed)
        blocked["records"][1]["thread_id"] = blocked["records"][0]["thread_id"]
        unknown = copy.deepcopy(passed)
        unknown["records"][1]["event_time"] = None
        for index, (value, state, code) in enumerate(
            ((passed, "MEASURED", 0), (blocked, "BLOCKED", 3), (unknown, "UNKNOWN", 3))
        ):
            with self.subTest(state=state):
                self.write_manifest(value)
                output = self.root / f"receipt-{index}.json"
                result = self.run_cli(output)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(json.loads(result.stdout), {"state": state, "record_count": 3})
                data = output.read_bytes()
                self.assertTrue(data.endswith(b"\n"))
                self.assertNotIn(b"\r", data)
                receipt = json.loads(data)
                self.assert_state(receipt, state)
                self.assertEqual(receipt, split_audit.audit_splits(self.input))
                for row in value["records"]:
                    self.assertNotIn(row["record_id"], data.decode("utf-8") + result.stdout)
                    self.assertNotIn(str(row["thread_id"]), data.decode("utf-8") + result.stdout)
        output = self.root / "invalid-receipt.json"
        self.input.write_bytes(b'{"synthetic_private_marker": NaN}')
        self.assert_cli_failure(self.run_cli(output))
        self.assertFalse(output.exists())

    def test_cli_preserves_existing_evidence_and_sanitizes_input_output_failures(self):
        self.write_manifest(manifest())
        original = self.input.read_bytes()
        existing = self.root / "existing-synthetic-receipt.json"
        sentinel = b"existing synthetic evidence must survive\x00\xff"
        existing.write_bytes(sentinel)
        result = self.run_cli(existing)
        self.assert_cli_failure(result)
        self.assertEqual(existing.read_bytes(), sentinel)
        self.assert_cli_failure(self.run_cli(self.input))
        self.assertEqual(self.input.read_bytes(), original)
        directory = self.root / "synthetic-output-directory"
        directory.mkdir()
        for output in (directory, self.root / "missing-synthetic-parent" / "receipt.json"):
            with self.subTest(output=output.name):
                self.assert_cli_failure(self.run_cli(output))
                self.assertTrue(directory.is_dir())
        for name in ("missing-synthetic-private-marker.json", "synthetic-input-directory"):
            with self.subTest(input=name):
                candidate = self.root / name
                if name.endswith("directory"):
                    candidate.mkdir()
                output = self.root / f"failed-{name}.json"
                self.assert_cli_failure(self.run_cli(output, candidate))
                self.assertFalse(output.exists())
        value = manifest()
        value["records"][0]["raw"] = "synthetic_private_marker@example.test"
        self.write_manifest(value)
        output = self.root / "unaccepted-content-receipt.json"
        self.assert_cli_failure(self.run_cli(output))
        self.assertFalse(output.exists())
        self.assertEqual(existing.read_bytes(), sentinel)

    def test_input_symlink_is_refused_when_native_platform_permits_creation(self):
        self.write_manifest(manifest())
        self.assert_state(split_audit.audit_splits(self.input), "MEASURED")
        original = self.input.read_bytes()
        link = self.root / "synthetic-input-symlink.json"
        try:
            link.symlink_to(self.input)
        except NotImplementedError:
            self.skipTest("native platform does not implement symlink creation")
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES, errno.ENOSYS, errno.ENOTSUP) \
                    or getattr(exc, "winerror", None) == 1314:
                self.skipTest("native symlink creation is unavailable or requires host privilege")
            raise
        self.assertTrue(link.is_symlink())
        with self.assertRaisesRegex(ValueError, "regular file"):
            split_audit.audit_splits(link)
        output = self.root / "symlink-receipt.json"
        self.assert_cli_failure(self.run_cli(output, link))
        self.assertFalse(output.exists())
        self.assertEqual(self.input.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
