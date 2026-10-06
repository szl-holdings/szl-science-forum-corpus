"""Audit declared research splits offline, without reading post text or models.

Connected thread/author/paper/duplicate families must remain in one split.
This audits supplied metadata, not its completeness, rights or scientific value.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys

from . import import_export


SCHEMA = "szl-research-splits-v1"
MAX_BYTES = 4_000_000
MAX_RECORDS = 10_000
MAX_KEYS_PER_KIND = 32
SPLITS = ("discovery", "development", "held_out")
KINDS = ("author", "paper", "duplicate")
FIELDS = {"record_id", "thread_id", "split", "event_time"} | {
    f"{kind}_hashes" for kind in KINDS
}
SHA = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,79}")
# Keep hours canonical for string ordering; Python 3.14 accepts 24:00:00.
UTC = re.compile(r"\d{4}-\d{2}-\d{2}T(?:[01][0-9]|2[0-3]):\d{2}:\d{2}Z")


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not UTC.fullmatch(value):
        raise ValueError("event_time must be null or a UTC timestamp with seconds")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError("invalid event_time") from None
    return value


def _validate(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"schema", "input_kind", "records"}:
        raise ValueError("split manifest fields differ from schema")
    if value["schema"] != SCHEMA or value["input_kind"] not in (
        "SIMULATED", "SOURCE_METADATA"
    ):
        raise ValueError("unsupported split schema or input kind")
    records = value["records"]
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_RECORDS:
        raise ValueError("record count absent or over limit")
    identifiers = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != FIELDS:
            raise ValueError("record fields differ from schema")
        identifier = record["record_id"]
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise ValueError("invalid opaque record ID")
        if identifier in identifiers:
            raise ValueError("duplicate record ID")
        identifiers.add(identifier)
        if type(record["thread_id"]) is not int or not 1 <= record["thread_id"] < 10**18:
            raise ValueError("invalid positive thread ID")
        if record["split"] not in SPLITS:
            raise ValueError("unsupported split")
        _timestamp(record["event_time"])
        for kind in KINDS:
            keys = record[f"{kind}_hashes"]
            if keys is None:
                continue
            if not isinstance(keys, list) or len(keys) > MAX_KEYS_PER_KIND:
                raise ValueError("family key list over limit or unsupported")
            if any(not isinstance(key, str) or not SHA.fullmatch(key) for key in keys):
                raise ValueError("family keys must be opaque lowercase SHA-256 values")
            if len(set(keys)) != len(keys):
                raise ValueError("duplicate family key within record")
    return value


def audit_splits(path: Path) -> dict:
    """Bind the exact stable input bytes and assess only declared associations."""
    data = import_export._read_bounded(path, MAX_BYTES, "split manifest")
    try:
        manifest = _validate(import_export._strict_json(data, "split manifest"))
    except RecursionError:
        raise ValueError("split manifest nesting exceeds parser limit") from None
    records = manifest["records"]
    parents = list(range(len(records)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    # Index each declared association once. Transitive connections join families
    # even if the endpoints share no direct identifier.
    seen: dict[tuple[str, object], int] = {}
    key_rows: dict[tuple[str, object], list[int]] = {}
    missing = {kind: 0 for kind in (*KINDS, "event_time")}
    for index, record in enumerate(records):
        keys: list[tuple[str, object]] = [("thread", record["thread_id"])]
        for kind in KINDS:
            values = record[f"{kind}_hashes"]
            if values is None:
                missing[kind] += 1
            else:
                keys.extend((kind, key) for key in values)
        if record["event_time"] is None:
            missing["event_time"] += 1
        for key in keys:
            key_rows.setdefault(key, []).append(index)
            if key in seen:
                parents[find(index)] = find(seen[key])
            else:
                seen[key] = index
    families: dict[int, list[int]] = {}
    for index in range(len(records)):
        families.setdefault(find(index), []).append(index)
    conflicts = []
    for members in families.values():
        splits = [split for split in SPLITS if any(records[i]["split"] == split for i in members)]
        if len(splits) > 1:
            conflicts.append({
                "record_ordinals": [i + 1 for i in members],
                "splits": splits,
            })
    direct_conflicts = {kind: 0 for kind in ("thread", *KINDS)}
    for (kind, _key), members in key_rows.items():
        if len({records[i]["split"] for i in members}) > 1:
            direct_conflicts[kind] += 1
    counts = {split: sum(record["split"] == split for record in records) for split in SPLITS}
    absent = [split for split in SPLITS if counts[split] == 0]
    windows = {}
    for split in SPLITS:
        times = [record["event_time"] for record in records
                 if record["split"] == split and record["event_time"] is not None]
        windows[split] = {"earliest": min(times) if times else None,
                          "latest": max(times) if times else None}
    time_conflicts = []
    # Check all split pairs, including discovery -> held_out if development is
    # absent. Unknown dates do not hide conflicts among the known dates.
    for left_index, left in enumerate(SPLITS):
        for right in SPLITS[left_index + 1:]:
            latest, earliest = windows[left]["latest"], windows[right]["earliest"]
            if latest is not None and earliest is not None and latest >= earliest:
                time_conflicts.append({"earlier_split": left, "later_split": right})
    outcome = "BLOCKED" if conflicts or time_conflicts else (
        "UNKNOWN" if absent or any(missing.values()) else "MEASURED"
    )
    return {
        "schema": "szl-research-split-audit-v1",
        "evidence_class": "MEASURED",
        "input_kind": manifest["input_kind"],
        "input_sha256": hashlib.sha256(data).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "reader_sha256": hashlib.sha256(Path(import_export.__file__).read_bytes()).hexdigest(),
        "state": outcome,
        "declared_checks_passed": outcome == "MEASURED",
        "record_count": len(records),
        "split_counts": counts,
        "connected_family_count": len(families),
        "cross_split_families": sorted(conflicts, key=lambda item: item["record_ordinals"]),
        "direct_cross_split_keys": direct_conflicts,
        "unknown_metadata_counts": missing,
        "absent_splits": absent,
        "time_windows": windows,
        "time_order_conflicts": time_conflicts,
        "assessment_scope": "DECLARED_METADATA_ONLY",
        "metadata_completeness": "UNKNOWN",
        "pretraining_contamination": "UNKNOWN",
        "rights_verification": "UNKNOWN",
        "model_training_authorized": False,
        "signed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = audit_splits(args.input)
        # Exclusive creation protects previous evidence. Output is restricted
        # metadata: no source IDs, family hashes or payload strings are emitted.
        with args.out.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
        print(json.dumps({"state": receipt["state"], "record_count": receipt["record_count"]}))
        return 0 if receipt["declared_checks_passed"] else 3
    except (OSError, ValueError, RecursionError):
        print("ERROR: invalid or unavailable split input/output; receipt not accepted", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
