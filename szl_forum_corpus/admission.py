"""Compose corpus integrity, rights, evidence, and split checks offline.

This module emits a deterministic admission receipt.  It does not read the
forum, train a model, publish a dataset, or grant execution authority.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

from .release_validation import parse_strict_json_bytes, validate_release_contract
from .split_audit import MAX_BYTES as SPLIT_MAX_BYTES
from .split_audit import _validate as validate_split_manifest
from .split_audit import audit_splits


REPORT_SCHEMA = "szl.forum-corpus-admission/v1"
POLICY = {
    "schema": "szl.forum-corpus-admission-policy/v1",
    "purpose": "model_training_candidate_review",
    "minimum_public_records": 100,
    "minimum_independent_topics": 50,
    "minimum_split_records": {
        "discovery": 60,
        "development": 20,
        "held_out": 20,
    },
    "maximum_public_records": 100_000,
    "required_forum_inventory_status": "complete_revision_bound",
    "required_source_revision_status": "exact_post_revisions_recorded",
    "require_explicit_training_rights_scope": True,
    "require_record_level_split_binding": True,
    "require_stable_split_baseline": True,
    "contract_references": {
        "chaskallm": {
            "repository": "szl-holdings/ChaskaLLM",
            "revision": "c8d08e1760923705a1e5d5e52dc6b12c60e1d128",
            "path": "chaskallm_ops/corpus_admission.py",
            "sha256": "0b8ce4a5dcdbc51da591f50c9c33791a69009266dd1b599413683d35cbb3a66a",
            "role": "reference_only_fail_closed_consumer",
        },
        "forge": {
            "repository": "szl-holdings/szl-forge",
            "revision": "e29ccf6438a0af0b27c5cbcb00f547bc13205c73",
            "merge_commit": "972af446bc7dc6e46d397f2d0cbafb22057545b5",
            "path": "tools/validate_sft_dataset.py",
            "sha256": "f86ebe1fc0bd324551664356e29d7e9bb192a8b13ac01d638cd73f0a608dea20",
            "role": "reference_only_exact_input_overlap",
        },
    },
    "boundary": (
        "operational floor only; an observed receipt is not scientific sufficiency, "
        "training approval, or execution authority"
    ),
}

HEX_64 = re.compile(r"^[0-9a-f]{64}$")
MAX_MANIFEST_BYTES = 1_000_000
MAX_SOURCE_BYTES = 64_000_000
MAX_ERRORS = 20


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _normalized_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.removeprefix("sha256:").lower()
    return normalized if HEX_64.fullmatch(normalized) else None


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"{label}_UNAVAILABLE") from exc
    if size < 1 or size > limit:
        raise ValueError(f"{label}_SIZE_INVALID")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label}_UNAVAILABLE") from exc
    if len(raw) != size:
        raise ValueError(f"{label}_CHANGED_DURING_READ")
    return raw


def _check(
    name: str,
    state: str,
    *,
    required: bool,
    expected: object,
    observed: object,
) -> dict[str, object]:
    if state not in {"OBSERVED", "INVALID", "UNKNOWN"}:
        raise ValueError("unsupported check state")
    return {
        "name": name,
        "state": state,
        "required": required,
        "expected": expected,
        "observed": observed,
    }


def _overall_state(checks: list[dict[str, object]]) -> str:
    required = [check for check in checks if check["required"] is True]
    if any(check["state"] == "INVALID" for check in required):
        return "INVALID"
    if any(check["state"] == "UNKNOWN" for check in required):
        return "UNKNOWN"
    return "OBSERVED"


def _unknown_split_checks() -> list[dict[str, object]]:
    return [
        _check(
            "split.manifest_pin",
            "UNKNOWN",
            required=True,
            expected="exact SHA-256-bound szl-research-splits-v1 input",
            observed=None,
        ),
        _check(
            "split.family_and_time_audit",
            "UNKNOWN",
            required=True,
            expected="MEASURED SOURCE_METADATA audit",
            observed=None,
        ),
        _check(
            "split.record_coverage",
            "UNKNOWN",
            required=True,
            expected="record-level binding for every public source row",
            observed=None,
        ),
        _check(
            "split.minimum_counts",
            "UNKNOWN",
            required=True,
            expected=POLICY["minimum_split_records"],
            observed=None,
        ),
        _check(
            "split.stability",
            "UNKNOWN",
            required=True,
            expected="same exact split SHA-256 as a reviewed baseline",
            observed=None,
        ),
    ]


def _load_split(
    path: Path,
    expected_sha256: object,
    prior_sha256: object,
    public_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, object]], dict[str, object] | None, list[str]]:
    checks: list[dict[str, object]] = []
    errors: list[str] = []
    try:
        raw = _read_bounded(path, SPLIT_MAX_BYTES, "SPLIT_MANIFEST")
        actual_sha256 = _sha256(raw)
    except ValueError as exc:
        errors.append(str(exc))
        missing = _unknown_split_checks()
        missing[0] = _check(
            "split.manifest_pin",
            "INVALID",
            required=True,
            expected=_normalized_sha(expected_sha256),
            observed=None,
        )
        return missing, None, errors

    declared_sha256 = _normalized_sha(expected_sha256)
    pin_ok = declared_sha256 is not None and declared_sha256 == actual_sha256
    checks.append(
        _check(
            "split.manifest_pin",
            "OBSERVED" if pin_ok else "INVALID",
            required=True,
            expected=declared_sha256,
            observed=actual_sha256,
        )
    )
    if not pin_ok:
        errors.append("SPLIT_MANIFEST_PIN_MISMATCH")

    split_value: dict[str, Any] | None = None
    audit: dict[str, object] | None = None
    try:
        parsed = parse_strict_json_bytes(raw, "split manifest")
        split_value = validate_split_manifest(parsed)
        audit = audit_splits(path)
        if audit["input_sha256"] != actual_sha256:
            raise ValueError("SPLIT_MANIFEST_CHANGED_DURING_AUDIT")
    except (OSError, ValueError, RecursionError) as exc:
        errors.append(str(exc) if str(exc).isupper() else "SPLIT_MANIFEST_INVALID")
        unavailable = _unknown_split_checks()[1:]
        unavailable[0] = _check(
            "split.family_and_time_audit",
            "INVALID",
            required=True,
            expected="MEASURED SOURCE_METADATA audit",
            observed="INVALID_OR_UNAVAILABLE",
        )
        checks.extend(unavailable)
        return checks, audit, errors

    input_kind = split_value["input_kind"]
    audit_state = audit["state"]
    if input_kind != "SOURCE_METADATA":
        family_state = "INVALID"
        errors.append("SIMULATED_SPLIT_CANNOT_ADMIT_SOURCE_CORPUS")
    elif audit_state == "MEASURED":
        family_state = "OBSERVED"
    elif audit_state == "BLOCKED":
        family_state = "INVALID"
        errors.append("DECLARED_SPLIT_LEAKAGE_OR_TIME_CONFLICT")
    else:
        family_state = "UNKNOWN"
    checks.append(
        _check(
            "split.family_and_time_audit",
            family_state,
            required=True,
            expected="MEASURED SOURCE_METADATA audit",
            observed={"input_kind": input_kind, "state": audit_state},
        )
    )

    split_records = split_value["records"]
    source_threads = Counter(row["topic_id"] for row in public_rows)
    split_threads = Counter(record["thread_id"] for record in split_records)
    count_and_threads_match = len(split_records) == len(public_rows) and split_threads == source_threads
    # research-splits-v1 has no field that binds an opaque record_id to a
    # release row digest.  Matching counts and thread multiplicity are useful,
    # but cannot be promoted to record-level coverage.
    coverage_state = "UNKNOWN" if count_and_threads_match else "INVALID"
    if not count_and_threads_match:
        errors.append("SPLIT_PUBLIC_SOURCE_COVERAGE_MISMATCH")
    checks.append(
        _check(
            "split.record_coverage",
            coverage_state,
            required=True,
            expected="record-level binding for every public source row",
            observed={
                "record_count_matches": len(split_records) == len(public_rows),
                "thread_multiplicity_matches": split_threads == source_threads,
                "record_digest_binding": False,
            },
        )
    )

    counts = audit["split_counts"]
    minimums = POLICY["minimum_split_records"]
    counts_ok = all(counts[name] >= minimums[name] for name in minimums)
    if not counts_ok:
        errors.append("SPLIT_MINIMUM_COUNTS_NOT_MET")
    checks.append(
        _check(
            "split.minimum_counts",
            "OBSERVED" if counts_ok else "INVALID",
            required=True,
            expected=minimums,
            observed=counts,
        )
    )

    prior = _normalized_sha(prior_sha256)
    if prior is None:
        stability_state = "UNKNOWN"
    elif prior == actual_sha256:
        stability_state = "OBSERVED"
    else:
        stability_state = "INVALID"
        errors.append("SPLIT_BASELINE_DIVERGENCE")
    checks.append(
        _check(
            "split.stability",
            stability_state,
            required=True,
            expected=prior,
            observed=actual_sha256,
        )
    )
    return checks, audit, errors


def audit_corpus_admission(
    dataset_root: str | Path,
    *,
    manifest_sha256: str,
    split_manifest: str | Path | None = None,
    split_sha256: str | None = None,
    prior_split_sha256: str | None = None,
) -> dict[str, object]:
    """Return a content-free, deterministic corpus admission receipt."""

    checks: list[dict[str, object]] = []
    errors: list[str] = []
    root = Path(dataset_root)
    manifest: dict[str, Any] | None = None
    public_rows: list[dict[str, Any]] = []
    manifest_actual: str | None = None
    source_actual: str | None = None

    declared_manifest = _normalized_sha(manifest_sha256)
    try:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError("DATASET_ROOT_NOT_DIRECTORY")
        manifest_path = (resolved_root / "manifest.json").resolve(strict=True)
        source_path = (resolved_root / "sources.public.jsonl").resolve(strict=True)
        manifest_path.relative_to(resolved_root)
        source_path.relative_to(resolved_root)
        manifest_raw = _read_bounded(manifest_path, MAX_MANIFEST_BYTES, "DATASET_MANIFEST")
        source_raw = _read_bounded(source_path, MAX_SOURCE_BYTES, "PUBLIC_SOURCE_PROJECTION")
        manifest_actual = _sha256(manifest_raw)
        source_actual = _sha256(source_raw)
    except (OSError, ValueError) as exc:
        errors.append(str(exc) if str(exc).isupper() else "DATASET_UNAVAILABLE")
        manifest_raw = None
        source_raw = None

    manifest_pin_ok = (
        declared_manifest is not None
        and manifest_actual is not None
        and declared_manifest == manifest_actual
    )
    checks.append(
        _check(
            "dataset.manifest_pin",
            "OBSERVED" if manifest_pin_ok else "INVALID",
            required=True,
            expected=declared_manifest,
            observed=manifest_actual,
        )
    )
    if not manifest_pin_ok:
        errors.append("DATASET_MANIFEST_PIN_MISMATCH")

    release_valid = False
    if manifest_raw is not None and source_raw is not None:
        try:
            parsed_manifest = parse_strict_json_bytes(manifest_raw, "dataset manifest")
            if not isinstance(parsed_manifest, dict):
                raise ValueError("dataset manifest must be an object")
            manifest = parsed_manifest
            validate_release_contract(manifest, source_raw)
            lines = source_raw.decode("utf-8").splitlines()
            public_rows = [
                parse_strict_json_bytes(line.encode("utf-8"), f"public source line {number}")
                for number, line in enumerate(lines, 1)
            ]
            if any(not isinstance(row, dict) for row in public_rows):
                raise ValueError("public source row must be an object")
            if len(public_rows) > POLICY["maximum_public_records"]:
                raise ValueError("public source row limit exceeded")
            file_hashes = manifest.get("files_sha256")
            if (
                not isinstance(file_hashes, dict)
                or file_hashes.get("sources.public.jsonl") != source_actual
            ):
                raise ValueError("manifest file hash mismatch")
            release_valid = True
        except (UnicodeDecodeError, ValueError) as exc:
            errors.append("RELEASE_CONTRACT_INVALID")

    checks.append(
        _check(
            "dataset.release_contract",
            "OBSERVED" if release_valid else "INVALID",
            required=True,
            expected="independently reconstructed szl.forum-corpus-release/v1",
            observed="MATCHED" if release_valid else "UNAVAILABLE_OR_DIVERGENT",
        )
    )

    release = manifest.get("release_contract") if release_valid and manifest else None
    contract_records = release.get("records") if isinstance(release, dict) else None
    if isinstance(contract_records, list):
        fingerprints = [
            record.get("forge_input_sha256")
            for record in contract_records
            if isinstance(record, dict)
        ]
        fingerprints_complete = (
            len(fingerprints) == len(public_rows)
            and all(isinstance(value, str) and HEX_64.fullmatch(value) for value in fingerprints)
        )
        duplicate_count = len(fingerprints) - len(set(fingerprints)) if fingerprints_complete else None
        exact_duplicate_state = (
            "OBSERVED" if duplicate_count == 0
            else "INVALID" if duplicate_count is not None
            else "UNKNOWN"
        )
        if duplicate_count:
            errors.append("EXACT_FORGE_INPUT_DUPLICATES")
    else:
        duplicate_count = None
        exact_duplicate_state = "UNKNOWN"
    checks.append(
        _check(
            "duplicates.exact_forge_input",
            exact_duplicate_state,
            required=True,
            expected=0,
            observed=duplicate_count,
        )
    )
    checks.append(
        _check(
            "duplicates.semantic_or_paraphrase",
            "UNKNOWN",
            required=False,
            expected="separately reviewed semantic duplicate receipt",
            observed=None,
        )
    )

    authorized = manifest.get("model_training_authorized") if manifest else None
    authorization_state = "OBSERVED" if authorized is True else "INVALID"
    if authorization_state == "INVALID":
        errors.append("MODEL_TRAINING_NOT_AUTHORIZED")
    checks.append(
        _check(
            "rights.model_training_authorized",
            authorization_state,
            required=True,
            expected=True,
            observed=authorized,
        )
    )

    allowed = release.get("allowed_purposes") if isinstance(release, dict) else None
    excluded = release.get("excluded_purposes") if isinstance(release, dict) else None
    if isinstance(allowed, list) and isinstance(excluded, list):
        if "model_training" in excluded:
            purpose_state = "INVALID"
            errors.append("MODEL_TRAINING_EXPLICITLY_EXCLUDED")
        elif "model_training" in allowed:
            purpose_state = "OBSERVED"
        else:
            purpose_state = "UNKNOWN"
    else:
        purpose_state = "UNKNOWN"
    checks.append(
        _check(
            "rights.allowed_purpose",
            purpose_state,
            required=True,
            expected="model_training allowed and not excluded",
            observed={"allowed": allowed, "excluded": excluded},
        )
    )

    training_rights = release.get("model_training_rights") if isinstance(release, dict) else None
    source_ids = sorted(row.get("source_id") for row in public_rows if isinstance(row.get("source_id"), str))
    source_scope_sha256 = _sha256(_canonical_bytes(source_ids))
    if training_rights is None:
        rights_scope_state = "UNKNOWN"
    elif (
        isinstance(training_rights, dict)
        and set(training_rights) == {
            "state", "purpose", "record_source_ids_sha256", "evidence_sha256"
        }
        and training_rights.get("state") == "GRANTED"
        and training_rights.get("purpose") == "model_training"
        and training_rights.get("record_source_ids_sha256") == source_scope_sha256
        and _normalized_sha(training_rights.get("evidence_sha256")) is not None
    ):
        rights_scope_state = "OBSERVED"
    else:
        rights_scope_state = "INVALID"
        errors.append("MODEL_TRAINING_RIGHTS_SCOPE_INVALID")
    checks.append(
        _check(
            "rights.record_scope",
            rights_scope_state,
            required=True,
            expected={
                "state": "GRANTED",
                "purpose": "model_training",
                "record_source_ids_sha256": source_scope_sha256,
                "evidence_sha256": "exact external rights evidence digest",
            },
            observed=training_rights,
        )
    )

    inventory = manifest.get("forum_inventory_status") if manifest else None
    inventory_state = (
        "OBSERVED" if inventory == POLICY["required_forum_inventory_status"]
        else "UNKNOWN" if inventory is None or str(inventory).startswith("not_")
        else "INVALID"
    )
    revision_status = manifest.get("source_revision_status") if manifest else None
    revision_state = (
        "OBSERVED" if revision_status == POLICY["required_source_revision_status"]
        else "UNKNOWN" if revision_status is None or str(revision_status).startswith("not_")
        else "INVALID"
    )
    checks.extend([
        _check(
            "provenance.forum_inventory",
            inventory_state,
            required=True,
            expected=POLICY["required_forum_inventory_status"],
            observed=inventory,
        ),
        _check(
            "provenance.source_revisions",
            revision_state,
            required=True,
            expected=POLICY["required_source_revision_status"],
            observed=revision_status,
        ),
    ])

    public_count = manifest.get("source_records_public") if manifest else None
    topic_count = manifest.get("independent_public_topics") if manifest else None
    public_ok = type(public_count) is int and public_count >= POLICY["minimum_public_records"]
    topics_ok = type(topic_count) is int and topic_count >= POLICY["minimum_independent_topics"]
    if not public_ok:
        errors.append("MINIMUM_PUBLIC_RECORDS_NOT_MET")
    if not topics_ok:
        errors.append("MINIMUM_INDEPENDENT_TOPICS_NOT_MET")
    checks.extend([
        _check(
            "threshold.public_records",
            "OBSERVED" if public_ok else "INVALID",
            required=True,
            expected={"minimum": POLICY["minimum_public_records"]},
            observed=public_count,
        ),
        _check(
            "threshold.independent_topics",
            "OBSERVED" if topics_ok else "INVALID",
            required=True,
            expected={"minimum": POLICY["minimum_independent_topics"]},
            observed=topic_count,
        ),
    ])

    split_audit: dict[str, object] | None = None
    if split_manifest is None:
        checks.extend(_unknown_split_checks())
    else:
        split_checks, split_audit, split_errors = _load_split(
            Path(split_manifest), split_sha256, prior_split_sha256, public_rows
        )
        checks.extend(split_checks)
        errors.extend(split_errors)

    checks.append(
        _check(
            "leakage.pretraining_contamination",
            "UNKNOWN",
            required=False,
            expected="model-specific contamination assessment",
            observed=None,
        )
    )
    checks = sorted(checks, key=lambda item: str(item["name"]))
    state = _overall_state(checks)
    receipt: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "state": state,
        "admission_passed": state == "OBSERVED",
        "policy": POLICY,
        "policy_sha256": _sha256(_canonical_bytes(POLICY)),
        "inputs": {
            "dataset_manifest_sha256": manifest_actual,
            "declared_dataset_manifest_sha256": declared_manifest,
            "public_source_projection_sha256": source_actual,
            "split_manifest_sha256": (
                split_audit.get("input_sha256") if isinstance(split_audit, dict) else None
            ),
        },
        "counts": {
            "public_records": public_count,
            "independent_topics": topic_count,
        },
        "checks": checks,
        "errors": sorted(set(errors))[:MAX_ERRORS],
        "scope": {
            "source_payloads_in_receipt": False,
            "forum_network_access": False,
            "provider_write": False,
            "model_training_executed": False,
            "model_published": False,
            "execution_authority": False,
            "scientific_sufficiency_claimed": False,
        },
        "harness_sha256": _sha256(Path(__file__).read_bytes()),
    }
    if len(set(errors)) > MAX_ERRORS:
        receipt["errors_truncated"] = len(set(errors)) - MAX_ERRORS
    receipt["receipt_sha256"] = _sha256(_canonical_bytes(receipt))
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_root", type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--split-manifest", type=Path)
    parser.add_argument("--split-sha256")
    parser.add_argument("--prior-split-sha256")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists():
        print("ERROR: output already exists; receipt not replaced", file=sys.stderr)
        return 2
    try:
        receipt = audit_corpus_admission(
            args.dataset_root,
            manifest_sha256=args.manifest_sha256,
            split_manifest=args.split_manifest,
            split_sha256=args.split_sha256,
            prior_split_sha256=args.prior_split_sha256,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
    except (OSError, ValueError):
        print("ERROR: invalid or unavailable admission input/output", file=sys.stderr)
        return 2
    print(json.dumps({
        "state": receipt["state"],
        "admission_passed": receipt["admission_passed"],
        "receipt_sha256": receipt["receipt_sha256"],
    }, sort_keys=True))
    return 0 if receipt["admission_passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
