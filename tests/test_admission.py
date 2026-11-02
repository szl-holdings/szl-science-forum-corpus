from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from szl_forum_corpus.admission import audit_corpus_admission, main
from szl_forum_corpus.cli import build, read_records


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "dataset"
EXAMPLE = ROOT / "examples" / "operator_topics.jsonl"
OPPORTUNITIES = json.loads((ROOT / "opportunities.json").read_text(encoding="utf-8"))
MANIFEST_SHA256 = hashlib.sha256((DATASET / "manifest.json").read_bytes()).hexdigest()
COMMITTED_RECEIPT = ROOT / "evidence" / "corpus-admission-current-2026-11-02.json"


def _check(receipt: dict, name: str) -> dict:
    return next(item for item in receipt["checks"] if item["name"] == name)


def _split(records: list[dict], *, input_kind: str = "SOURCE_METADATA") -> bytes:
    return (json.dumps(
        {"schema": "szl-research-splits-v1", "input_kind": input_kind, "records": records},
        indent=2,
        sort_keys=True,
    ) + "\n").encode("utf-8")


def _split_row(
    identifier: str,
    thread_id: int,
    split: str,
    event_time: str,
    *,
    author: str,
) -> dict:
    return {
        "record_id": identifier,
        "thread_id": thread_id,
        "split": split,
        "event_time": event_time,
        "author_hashes": [author],
        "paper_hashes": [],
        "duplicate_hashes": [],
    }


class CorpusAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".test-admission-", dir=ROOT)
        self.root = Path(self.temporary.name)
        self.dataset = self.root / "dataset"
        shutil.copytree(DATASET, self.dataset)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def audit(self, **kwargs) -> dict:
        return audit_corpus_admission(
            self.dataset,
            manifest_sha256=kwargs.pop("manifest_sha256", MANIFEST_SHA256),
            **kwargs,
        )

    def write_split(self, raw: bytes) -> tuple[Path, str]:
        path = self.root / "splits.json"
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest()

    def test_current_pilot_is_deterministically_invalid_without_exposing_rows(self) -> None:
        first = self.audit()
        second = self.audit()
        copy_root = self.root / "copy"
        shutil.copytree(DATASET, copy_root)
        third = audit_corpus_admission(copy_root, manifest_sha256=MANIFEST_SHA256)
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertEqual(first, json.loads(COMMITTED_RECEIPT.read_text(encoding="utf-8")))
        self.assertEqual(first["state"], "INVALID")
        self.assertFalse(first["admission_passed"])
        self.assertEqual(first["counts"], {"public_records": 2, "independent_topics": 2})
        self.assertEqual(_check(first, "dataset.release_contract")["state"], "OBSERVED")
        self.assertEqual(_check(first, "duplicates.exact_forge_input")["state"], "OBSERVED")
        self.assertEqual(_check(first, "rights.model_training_authorized")["state"], "INVALID")
        self.assertEqual(_check(first, "rights.record_scope")["state"], "UNKNOWN")
        self.assertEqual(_check(first, "split.manifest_pin")["state"], "UNKNOWN")
        self.assertFalse(first["scope"]["model_training_executed"])
        self.assertFalse(first["scope"]["provider_write"])
        self.assertFalse(first["scope"]["execution_authority"])
        encoded = json.dumps(first, sort_keys=True)
        self.assertNotIn("ai4science:396:1", encoded)
        self.assertNotIn("Feature request: easier skill sharing", encoded)

    def test_wrong_manifest_pin_and_duplicate_json_keys_fail_closed(self) -> None:
        wrong = self.audit(manifest_sha256="0" * 64)
        self.assertEqual(_check(wrong, "dataset.manifest_pin")["state"], "INVALID")
        malformed = b'{"schema":"x","schema":"y"}\n'
        (self.dataset / "manifest.json").write_bytes(malformed)
        receipt = self.audit(manifest_sha256=hashlib.sha256(malformed).hexdigest())
        self.assertEqual(receipt["state"], "INVALID")
        self.assertEqual(_check(receipt, "dataset.release_contract")["state"], "INVALID")

    def test_duplicate_forge_inputs_are_detected_after_release_validation(self) -> None:
        records = read_records(EXAMPLE)
        revised = copy.deepcopy(records)
        revised[1]["title"] = revised[0]["title"]
        revised[1]["summary"] = revised[0]["summary"]
        outputs = build(revised, OPPORTUNITIES, "2026-10-02")
        shutil.rmtree(self.dataset)
        self.dataset.mkdir()
        for name, raw in outputs.items():
            (self.dataset / name).write_bytes(raw)
        digest = hashlib.sha256(outputs["manifest.json"]).hexdigest()
        receipt = self.audit(manifest_sha256=digest)
        duplicate = _check(receipt, "duplicates.exact_forge_input")
        self.assertEqual(_check(receipt, "dataset.release_contract")["state"], "OBSERVED")
        self.assertEqual(duplicate["state"], "INVALID")
        self.assertEqual(duplicate["observed"], 1)
        self.assertIn("EXACT_FORGE_INPUT_DUPLICATES", receipt["errors"])

    def test_cross_split_family_leakage_is_invalid(self) -> None:
        shared = "a" * 64
        raw = _split([
            _split_row("case_a", 396, "discovery", "2026-01-01T00:00:00Z", author=shared),
            _split_row("case_b", 426, "held_out", "2026-03-01T00:00:00Z", author=shared),
        ])
        path, digest = self.write_split(raw)
        receipt = self.audit(
            split_manifest=path,
            split_sha256=digest,
            prior_split_sha256=digest,
        )
        family = _check(receipt, "split.family_and_time_audit")
        self.assertEqual(family["state"], "INVALID")
        self.assertEqual(family["observed"], {"input_kind": "SOURCE_METADATA", "state": "BLOCKED"})
        self.assertIn("DECLARED_SPLIT_LEAKAGE_OR_TIME_CONFLICT", receipt["errors"])

    def test_simulated_split_never_admits_source_corpus(self) -> None:
        raw = _split([
            _split_row("case_a", 396, "discovery", "2026-01-01T00:00:00Z", author="a" * 64),
            _split_row("case_b", 426, "held_out", "2026-03-01T00:00:00Z", author="b" * 64),
        ], input_kind="SIMULATED")
        path, digest = self.write_split(raw)
        receipt = self.audit(
            split_manifest=path,
            split_sha256=digest,
            prior_split_sha256=digest,
        )
        self.assertEqual(_check(receipt, "split.family_and_time_audit")["state"], "INVALID")
        self.assertIn("SIMULATED_SPLIT_CANNOT_ADMIT_SOURCE_CORPUS", receipt["errors"])

    def test_matching_thread_counts_remain_unknown_without_record_digest_binding(self) -> None:
        raw = _split([
            _split_row("case_a", 396, "discovery", "2026-01-01T00:00:00Z", author="a" * 64),
            _split_row("case_b", 426, "held_out", "2026-03-01T00:00:00Z", author="b" * 64),
        ])
        path, digest = self.write_split(raw)
        receipt = self.audit(
            split_manifest=path,
            split_sha256=digest,
            prior_split_sha256=digest,
        )
        coverage = _check(receipt, "split.record_coverage")
        self.assertEqual(coverage["state"], "UNKNOWN")
        self.assertTrue(coverage["observed"]["record_count_matches"])
        self.assertTrue(coverage["observed"]["thread_multiplicity_matches"])
        self.assertFalse(coverage["observed"]["record_digest_binding"])
        self.assertEqual(_check(receipt, "split.stability")["state"], "OBSERVED")

    def test_split_coverage_and_stability_mismatch_are_invalid(self) -> None:
        raw = _split([
            _split_row("case_a", 999, "discovery", "2026-01-01T00:00:00Z", author="a" * 64),
            _split_row("case_b", 426, "held_out", "2026-03-01T00:00:00Z", author="b" * 64),
        ])
        path, digest = self.write_split(raw)
        receipt = self.audit(
            split_manifest=path,
            split_sha256=digest,
            prior_split_sha256="f" * 64,
        )
        self.assertEqual(_check(receipt, "split.record_coverage")["state"], "INVALID")
        self.assertEqual(_check(receipt, "split.stability")["state"], "INVALID")
        self.assertIn("SPLIT_PUBLIC_SOURCE_COVERAGE_MISMATCH", receipt["errors"])
        self.assertIn("SPLIT_BASELINE_DIVERGENCE", receipt["errors"])

    def test_missing_and_malformed_split_inputs_are_invalid(self) -> None:
        missing = self.audit(
            split_manifest=self.root / "missing.json",
            split_sha256="a" * 64,
        )
        self.assertEqual(_check(missing, "split.manifest_pin")["state"], "INVALID")
        malformed_raw = b'{"schema":"szl-research-splits-v1","schema":"other"}\n'
        path, digest = self.write_split(malformed_raw)
        malformed = self.audit(split_manifest=path, split_sha256=digest)
        self.assertEqual(_check(malformed, "split.manifest_pin")["state"], "OBSERVED")
        self.assertEqual(
            _check(malformed, "split.family_and_time_audit")["state"], "INVALID"
        )
        self.assertIn("SPLIT_MANIFEST_INVALID", malformed["errors"])

    def test_cli_writes_once_and_returns_blocked_status(self) -> None:
        output = self.root / "receipt.json"
        code = main([
            str(self.dataset),
            "--manifest-sha256", MANIFEST_SHA256,
            "--out", str(output),
        ])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["state"], "INVALID")
        original = output.read_bytes()
        self.assertEqual(main([
            str(self.dataset),
            "--manifest-sha256", MANIFEST_SHA256,
            "--out", str(output),
        ]), 2)
        self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
