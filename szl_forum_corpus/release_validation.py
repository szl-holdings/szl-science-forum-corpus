"""Independent, fail-closed validation for public corpus release artifacts.

This module intentionally does not import the corpus generator. Publication preflight
reconstructs the release contract with a separate implementation so generator drift
cannot validate itself.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json
import re
import unicodedata
from urllib.parse import urlparse


PUBLIC_FIELDS = frozenset({
    "source_id", "source_url", "topic_id", "post_number", "title", "summary",
    "need_ids", "review_state", "observed_at", "posted_at", "publication_rights",
    "source_content_license", "rights_evidence",
})
SOURCE_ID = re.compile(r"^ai4science:(\d+):(\d+)$")
NEED_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
SOURCE_LICENSES = frozenset({
    "Apache-2.0", "CC-BY-4.0", "CC-BY-SA-4.0", "CC0-1.0", "MIT",
})
RECORD_SCHEMA = "schema/record-v2.schema.json"
RECORD_SCHEMA_SHA256 = "81f4c966da90f84d4d282332a30923ebc0cb647b04704fb2e30f410ab85a4520"
FORGE_SYSTEM = (
    "Review this source-linked science forum summary as reference evidence only. "
    "Do not infer model-training authority or forum-wide prevalence."
)
FORGE_REVISION = "e29ccf6438a0af0b27c5cbcb00f547bc13205c73"
FORGE_SHA256 = "f86ebe1fc0bd324551664356e29d7e9bb192a8b13ac01d638cd73f0a608dea20"
FORGE_MERGED_PR = "szl-holdings/szl-forge#673"
FORGE_MERGE_COMMIT = "972af446bc7dc6e46d397f2d0cbafb22057545b5"


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON value")


def parse_strict_json_bytes(raw: bytes, label: str) -> object:
    if not isinstance(raw, bytes):
        raise ValueError(f"{label} must be bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not UTF-8") from exc
    try:
        return json.loads(
            text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON") from exc


def _canonical_jsonl(rows: list[dict]) -> bytes:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")


def _checked_text(value: object, field: str, limit: int, line_number: int) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or len(value) > limit
        or any(ord(character) < 32 and character not in "\n\t" for character in value)
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise ValueError(f"public source line {line_number}: invalid {field}")
    return value


def _checked_date(value: object, field: str, line_number: int) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"public source line {line_number}: invalid {field}")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"public source line {line_number}: invalid {field}") from exc
    return value


def _validate_public_row(value: object, line_number: int) -> dict:
    if not isinstance(value, dict) or set(value) != PUBLIC_FIELDS:
        raise ValueError(f"public source line {line_number}: fields differ from record-v2 projection")
    source_id = value["source_id"]
    match = SOURCE_ID.fullmatch(source_id) if isinstance(source_id, str) else None
    if match is None:
        raise ValueError(f"public source line {line_number}: invalid source_id")
    topic_id = value["topic_id"]
    post_number = value["post_number"]
    if (
        type(topic_id) is not int or type(post_number) is not int
        or topic_id < 1 or post_number < 1
        or (topic_id, post_number) != tuple(map(int, match.groups()))
    ):
        raise ValueError(f"public source line {line_number}: source identity mismatch")

    source_url = value["source_url"]
    try:
        parsed = urlparse(source_url) if isinstance(source_url, str) else None
        port = parsed.port if parsed is not None else None
    except ValueError as exc:
        raise ValueError(f"public source line {line_number}: invalid source_url") from exc
    path_match = (
        re.fullmatch(rf"/t/[a-z0-9-]+/{topic_id}(?:/([1-9][0-9]*))?/?", parsed.path)
        if parsed is not None else None
    )
    if (
        parsed is None or parsed.scheme != "https"
        or parsed.hostname != "ai4science.discourse.group"
        or parsed.username or parsed.password or port or parsed.query or parsed.fragment
        or path_match is None or int(path_match.group(1) or "1") != post_number
        or len(source_url) > 500
    ):
        raise ValueError(f"public source line {line_number}: invalid source_url")

    _checked_text(value["title"], "title", 240, line_number)
    _checked_text(value["summary"], "summary", 1000, line_number)
    _checked_text(value["rights_evidence"], "rights_evidence", 500, line_number)
    rights = value["publication_rights"]
    if not isinstance(rights, str) or rights not in {"operator_authorized", "licensed"}:
        raise ValueError(f"public source line {line_number}: rights do not permit projection")
    source_license = value["source_content_license"]
    if rights == "licensed":
        if not isinstance(source_license, str) or source_license not in SOURCE_LICENSES:
            raise ValueError(f"public source line {line_number}: invalid source_content_license")
    elif source_license is not None:
        raise ValueError(f"public source line {line_number}: unexpected source_content_license")
    review_state = value["review_state"]
    if not isinstance(review_state, str) or review_state not in {"operator_labeled", "double_reviewed"}:
        raise ValueError(f"public source line {line_number}: unreviewed record")
    need_ids = value["need_ids"]
    if (
        not isinstance(need_ids, list) or not need_ids
        or any(not isinstance(need, str) or NEED_ID.fullmatch(need) is None for need in need_ids)
        or need_ids != sorted(set(need_ids))
    ):
        raise ValueError(f"public source line {line_number}: invalid need_ids")
    _checked_date(value["observed_at"], "observed_at", line_number)
    if value["posted_at"] is not None:
        _checked_date(value["posted_at"], "posted_at", line_number)
    return value


def _forge_fingerprint(row: dict) -> str:
    messages = [
        {"role": "system", "content": FORGE_SYSTEM},
        {"role": "user", "content": row["title"] + "\n\n" + row["summary"]},
    ]
    canonical = json.dumps(
        [
            {
                "role": message["role"],
                "content": unicodedata.normalize(
                    "NFC", message["content"].replace("\r\n", "\n").replace("\r", "\n")
                ).strip(),
            }
            for message in messages
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _thread_fingerprint(topic_id: int) -> str:
    payload = json.dumps(
        {
            "schema": "szl.forum-thread-family/v1",
            "source": "ai4science.discourse.group",
            "topic_id": topic_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _expected_contract(rows: list[dict]) -> dict:
    return {
        "schema": "szl.forum-corpus-release/v1",
        "source_scope": "reviewed_public_projection_only",
        "projection_license": "Apache-2.0",
        "projection_license_scope": "original_szl_metadata_summaries_and_annotations",
        "source_content_license_inferred": False,
        "allowed_purposes": ["public_metadata_index", "research_hypothesis_generation"],
        "excluded_purposes": [
            "model_training", "retrieval_benchmark", "forum_prevalence_claim",
            "third_party_post_republication",
        ],
        "forge_disjoint_reference": {
            "profile_state": "MERGED_DEPENDENCY",
            "fingerprint_schema": "sha256-canonical-chat-input-v1",
            "system_message": FORGE_SYSTEM,
            "user_message_template": "{title}\\n\\n{summary}",
            "reference_only": True,
            "validator": {
                "repository": "szl-holdings/szl-forge",
                "revision": FORGE_REVISION,
                "path": "tools/validate_sft_dataset.py",
                "sha256": FORGE_SHA256,
                "merged_pr": FORGE_MERGED_PR,
                "merge_commit": FORGE_MERGE_COMMIT,
            },
        },
        "temporal_split": {
            "schema": "szl-research-splits-v1",
            "state": "UNKNOWN",
            "reason": "NO_ADMITTED_SPLIT_MANIFEST",
            "family_separation_verified": False,
            "temporal_order_verified": False,
            "records_with_unknown_event_time": sum(row["posted_at"] is None for row in rows),
            "family_dimensions": {
                "thread": "MEASURED", "author": "UNKNOWN",
                "paper": "UNKNOWN", "duplicate": "UNKNOWN",
            },
        },
        "records": [
            {
                "source_id": row["source_id"],
                "row_sha256": hashlib.sha256(_canonical_jsonl([row])).hexdigest(),
                "thread_family_sha256": _thread_fingerprint(row["topic_id"]),
                "forge_input_sha256": _forge_fingerprint(row),
                "publication_rights": row["publication_rights"],
                "source_content_license": row["source_content_license"],
            }
            for row in rows
        ],
    }


def validate_release_contract(manifest: object, source_bytes: bytes) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    if manifest.get("model_training_authorized") is not False:
        raise ValueError("manifest must deny model training")
    if (
        manifest.get("record_schema") != RECORD_SCHEMA
        or manifest.get("record_schema_sha256") != RECORD_SCHEMA_SHA256
    ):
        raise ValueError("manifest record-v2 binding mismatch")
    if not isinstance(source_bytes, bytes):
        raise ValueError("public source projection must be bytes")
    try:
        lines = source_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("public source projection is not UTF-8") from exc
    if not lines:
        raise ValueError("public source projection has no records")
    rows = [
        _validate_public_row(
            parse_strict_json_bytes(line.encode("utf-8"), f"public source line {line_number}"),
            line_number,
        )
        for line_number, line in enumerate(lines, 1)
    ]
    source_ids = [row["source_id"] for row in rows]
    if source_ids != sorted(source_ids) or len(source_ids) != len(set(source_ids)):
        raise ValueError("public source projection order or identity is invalid")
    if _canonical_jsonl(rows) != source_bytes:
        raise ValueError("public source projection is not canonical JSONL")
    digest = hashlib.sha256(source_bytes).hexdigest()
    if manifest.get("public_source_records_sha256") != digest:
        raise ValueError("public source projection digest mismatch")
    if (
        manifest.get("source_records_seen") != len(rows)
        or manifest.get("source_records_public") != len(rows)
        or manifest.get("source_records_withheld") != 0
        or manifest.get("independent_public_topics") != len({row["topic_id"] for row in rows})
    ):
        raise ValueError("manifest public record counts mismatch")
    if manifest.get("release_contract") != _expected_contract(rows):
        raise ValueError("release contract does not match public source projection")
