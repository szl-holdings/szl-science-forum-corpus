"""Offline import of a normalized, explicitly authorized forum export.

This is a *restricted metadata ledger*, not a public corpus builder. The input
is an operator-prepared JSON snapshot; it is never fetched from the forum. An
active post supplies ``raw`` only so its UTF-8 digest can be recorded. Neither
raw text nor titles, authors, profiles, or attachments enter the output.

The snapshot format is exercised in ``tests/test_import_export.py``. A complete
snapshot starts and ends with a null cursor and may reconcile absent posts.
Partial, blocked, and unknown observations never imply that posts disappeared.
All records remain ineligible for public projection and model training here.
Each ledger covers one category ID. A topic moving categories cannot be inferred
from two category snapshots; reviewers must reconcile that event separately.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from urllib.parse import urlparse


ORIGIN = "ai4science.discourse.group"
INPUT_SCHEMA = "szl-forum-normalized-export-v1"
OUTPUT_SCHEMA = "szl-forum-import-manifest-v1"
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_MAX_PAGES = 32
DEFAULT_MAX_RECORDS = 2_000
HARD_MAX_BYTES = 32_000_000
HARD_MAX_PAGES = 256
HARD_MAX_RECORDS = 20_000
POST_FIELDS = {
    "source_id", "source_url", "topic_id", "post_number", "category_id", "source_revision",
    "updated_at", "deleted", "raw", "access", "publication_rights",
    "rights_evidence",
}
RECORD_FIELDS = {
    "source_id", "source_url", "topic_id", "post_number", "category_id", "source_revision",
    "source_sha256", "updated_at", "observed_at", "access",
    "publication_rights", "rights_evidence", "status", "tombstone_reason",
}
ACCESS = {"public_web", "members_only", "restricted", "unknown"}
RIGHTS = {"operator_authorized", "licensed", "unknown"}
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ID_RE = re.compile(r"^ai4science:([1-9][0-9]*):([1-9][0-9]*)$")
CURSOR_RE = re.compile(r"^[A-Za-z0-9._~-]{1,200}$")
SCOPE_RE = re.compile(r"^category_([1-9][0-9]{0,17})$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _object(value: object, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label}: expected exactly {sorted(fields)}")
    return value


def _text(value: object, label: str, limit: int) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > limit
        or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
               for char in value)
    ):
        raise ValueError(f"{label}: invalid text")
    return value


def _utc(value: object, label: str) -> str:
    if not isinstance(value, str) or not UTC_RE.fullmatch(value):
        raise ValueError(f"{label}: expected UTC timestamp with seconds")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label}: invalid UTC timestamp") from exc
    return value


def _cursor(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not CURSOR_RE.fullmatch(value):
        raise ValueError(f"{label}: invalid cursor")
    return value


def _source_identity(source_id: object, source_url: object, topic_id: object,
                     post_number: object, label: str) -> None:
    if (type(topic_id) is not int or not 1 <= topic_id < 10**18
            or type(post_number) is not int or not 1 <= post_number < 10**18):
        raise ValueError(f"{label}: invalid topic/post ID")
    if (not isinstance(source_id, str) or len(source_id) > 80
            or (match := ID_RE.fullmatch(source_id)) is None):
        raise ValueError(f"{label}: invalid source ID")
    if (topic_id, post_number) != tuple(map(int, match.groups())):
        raise ValueError(f"{label}: source ID mismatch")
    _text(source_url, f"{label}.source_url", 500)
    try:
        parsed = urlparse(source_url)
    except ValueError as exc:
        raise ValueError(f"{label}: invalid source URL") from exc
    path = re.fullmatch(rf"/t/[a-z0-9-]+/{topic_id}(?:/([1-9][0-9]*))?/?", parsed.path)
    if (
        parsed.scheme != "https" or parsed.netloc != ORIGIN
        or parsed.query or parsed.fragment or not path
        or int(path.group(1) or "1") != post_number
    ):
        raise ValueError(f"{label}: invalid or unsafe source URL")


def _strict_json(data: bytes, label: str) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"{label}: duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(_value: str) -> None:
        raise ValueError(f"{label}: non-finite JSON number")

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}: invalid UTF-8 JSON") from exc


def _file_identity(value: os.stat_result, *, cross_api: bool = False) -> tuple[int, ...]:
    # Reads may update atime. It is intentionally excluded from consistency checks.
    clock = value.st_ctime_ns
    if cross_api and sys.platform == "win32":
        # CPython 3.12 Windows lstat exposes creation time as ctime, while fstat
        # exposes ChangeTime. Compare their explicit creation clocks instead.
        # Older Windows Python lacks birthtime and uses creation time for both.
        clock = getattr(value, "st_birthtime_ns", clock)
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, clock)


def _read_bounded(path: Path, max_bytes: int, label: str) -> bytes:
    """Read a stable regular file within the byte budget.

    These checks assume a trusted, cooperative local filesystem. They detect
    observed replacement and mutation; they are not a filesystem sandbox or a
    transaction against concurrent writers or hostile parent directories.
    """
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label}: expected a regular file")
    if before.st_size > max_bytes:
        raise ValueError(f"{label}: byte budget exceeded")
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label}: file unavailable or changed before open") from exc
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode)
                or _file_identity(opened, cross_api=True) != _file_identity(before, cross_api=True)):
            raise ValueError(f"{label}: file changed before read")
        data = bytearray()
        while len(data) <= max_bytes:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > max_bytes:
            raise ValueError(f"{label}: byte budget exceeded")
        after = os.fstat(descriptor)
        current = path.lstat()
        if (_file_identity(after) != _file_identity(opened)
                or _file_identity(current) != _file_identity(before)
                or len(data) != opened.st_size):
            raise ValueError(f"{label}: file changed during read")
        return bytes(data)
    finally:
        os.close(descriptor)


def _budgets(max_bytes: int, max_pages: int, max_records: int) -> None:
    for name, value, ceiling in (
        ("max_bytes", max_bytes, HARD_MAX_BYTES),
        ("max_pages", max_pages, HARD_MAX_PAGES),
        ("max_records", max_records, HARD_MAX_RECORDS),
    ):
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError(f"{name}: must be between 1 and {ceiling}")


def _post(value: object, observed_at: str, category_id: int, label: str) -> dict:
    post = _object(value, POST_FIELDS, label)
    _source_identity(post["source_id"], post["source_url"], post["topic_id"],
                     post["post_number"], label)
    if type(post["category_id"]) is not int or post["category_id"] != category_id:
        raise ValueError(f"{label}: category does not match scope")
    revision = post["source_revision"]
    if revision is not None:
        _text(revision, f"{label}.source_revision", 80)
    updated = post["updated_at"]
    if updated is not None:
        _utc(updated, f"{label}.updated_at")
        if updated > observed_at:
            raise ValueError(f"{label}: source update is after observation")
    if type(post["deleted"]) is not bool:
        raise ValueError(f"{label}: deleted must be Boolean")
    if (not isinstance(post["access"], str) or post["access"] not in ACCESS
            or not isinstance(post["publication_rights"], str)
            or post["publication_rights"] not in RIGHTS):
        raise ValueError(f"{label}: invalid access or publication rights")
    evidence = _text(post["rights_evidence"], f"{label}.rights_evidence", 500)
    raw = post["raw"]
    if post["deleted"]:
        if raw is not None:
            raise ValueError(f"{label}: deleted post must not contain raw text")
        digest = None
    else:
        if not isinstance(raw, str) or any(0xD800 <= ord(char) <= 0xDFFF for char in raw):
            raise ValueError(f"{label}: invalid raw text")
        raw_bytes = raw.encode("utf-8")
        if len(raw_bytes) > 128_000:
            raise ValueError(f"{label}: invalid or oversized raw text")
        digest = hashlib.sha256(raw_bytes).hexdigest()
    return {
        "source_id": post["source_id"], "source_url": post["source_url"],
        "topic_id": post["topic_id"], "post_number": post["post_number"],
        "category_id": category_id,
        "source_revision": revision, "source_sha256": digest,
        "updated_at": updated, "observed_at": observed_at,
        "access": post["access"], "publication_rights": post["publication_rights"],
        "rights_evidence": evidence,
        "status": "tombstone" if post["deleted"] else "active",
        "tombstone_reason": "explicit_deletion" if post["deleted"] else None,
    }


def _snapshot(value: object, max_pages: int, max_records: int) -> tuple[dict, dict[str, dict]]:
    top = _object(value, {"schema", "origin", "observed_at", "acquisition", "coverage", "pages"}, "snapshot")
    if top["schema"] != INPUT_SCHEMA or top["origin"] != ORIGIN:
        raise ValueError("snapshot: unsupported schema or origin")
    observed_at = _utc(top["observed_at"], "snapshot.observed_at")
    acquisition = _object(top["acquisition"], {"state", "reference"}, "acquisition")
    if (not isinstance(acquisition["state"], str)
            or acquisition["state"] not in {"authorized_export", "blocked", "unknown"}):
        raise ValueError("acquisition: invalid state")
    _text(acquisition["reference"], "acquisition.reference", 240)
    coverage = _object(top["coverage"],
                       {"state", "scope", "start_cursor", "end_cursor", "empty_confirmed"}, "coverage")
    if (not isinstance(coverage["state"], str)
            or coverage["state"] not in {"complete", "partial", "blocked", "unknown"}):
        raise ValueError("coverage: invalid state")
    if not isinstance(coverage["scope"], str) or (scope_match := SCOPE_RE.fullmatch(coverage["scope"])) is None:
        raise ValueError("coverage: expected category_<positive ID> scope")
    category_id = int(scope_match.group(1))
    start = _cursor(coverage["start_cursor"], "coverage.start_cursor")
    end = _cursor(coverage["end_cursor"], "coverage.end_cursor")
    if type(coverage["empty_confirmed"]) is not bool:
        raise ValueError("coverage: empty_confirmed must be Boolean")
    pages = top["pages"]
    if not isinstance(pages, list) or len(pages) > max_pages:
        raise ValueError("snapshot: page budget exceeded")
    state = coverage["state"]
    if acquisition["state"] != "authorized_export":
        if (state != acquisition["state"] or pages or coverage["empty_confirmed"]
                or start != end):
            raise ValueError("snapshot: blocked/unknown acquisition cannot contain records")
    elif state not in {"complete", "partial"}:
        raise ValueError("snapshot: authorized export needs complete or partial coverage")
    if state == "complete" and (start is not None or end is not None):
        raise ValueError("coverage: complete snapshot must span the whole scope")
    if state == "partial" and (not pages or end is None):
        raise ValueError("coverage: partial snapshot needs pages and a continuation cursor")
    if not pages and state == "complete" and not coverage["empty_confirmed"]:
        raise ValueError("coverage: zero posts require explicit empty confirmation")
    if pages and coverage["empty_confirmed"]:
        raise ValueError("coverage: empty confirmation conflicts with pages")

    posts: dict[str, dict] = {}
    previous_cursor = start
    seen_cursors: set[str | None] = set()
    count = 0
    for page_index, page_value in enumerate(pages, 1):
        page = _object(page_value, {"cursor", "next_cursor", "posts"}, f"page {page_index}")
        cursor = _cursor(page["cursor"], f"page {page_index}.cursor")
        next_cursor = _cursor(page["next_cursor"], f"page {page_index}.next_cursor")
        if cursor != previous_cursor or cursor in seen_cursors:
            raise ValueError(f"page {page_index}: broken or repeated cursor chain")
        seen_cursors.add(cursor)
        if next_cursor is not None and next_cursor in seen_cursors:
            raise ValueError(f"page {page_index}: cursor did not advance")
        if not isinstance(page["posts"], list):
            raise ValueError(f"page {page_index}: posts must be a list")
        count += len(page["posts"])
        if count > max_records:
            raise ValueError("snapshot: record budget exceeded")
        for post_index, item in enumerate(page["posts"], 1):
            record = _post(item, observed_at, category_id, f"page {page_index} post {post_index}")
            if record["source_id"] in posts:
                raise ValueError("snapshot: duplicate source ID")
            posts[record["source_id"]] = record
        previous_cursor = next_cursor
    if pages and previous_cursor != end:
        raise ValueError("coverage: terminal cursor mismatch")
    if pages and state == "complete" and previous_cursor is not None:
        raise ValueError("coverage: complete snapshot has a continuation cursor")
    if pages and state == "complete" and count == 0:
        raise ValueError("coverage: empty pages cannot confirm zero posts")
    return top, posts


def _previous(value: object, scope: str, max_pages: int, max_records: int) -> dict:
    fields = {"schema", "origin", "scope", "observed_at", "acquisition", "coverage",
              "input_sha256", "state_sha256", "model_training_authorized",
              "public_projection_approved", "delta", "records"}
    prior = _object(value, fields, "previous manifest")
    state_sha = prior["state_sha256"]
    if (not isinstance(state_sha, str) or not SHA_RE.fullmatch(state_sha)
            or hashlib.sha256(_json_bytes({key: item for key, item in prior.items()
                                           if key != "state_sha256"})).hexdigest() != state_sha):
        raise ValueError("previous manifest: state digest mismatch")
    if prior["schema"] != OUTPUT_SCHEMA or prior["origin"] != ORIGIN or prior["scope"] != scope:
        raise ValueError("previous manifest: schema, origin, or scope mismatch")
    if prior["model_training_authorized"] is not False or prior["public_projection_approved"] is not False:
        raise ValueError("previous manifest: approval flags must be false")
    _utc(prior["observed_at"], "previous manifest.observed_at")
    if not isinstance(prior["input_sha256"], str) or not SHA_RE.fullmatch(prior["input_sha256"]):
        raise ValueError("previous manifest: invalid input digest")
    acquisition = _object(prior["acquisition"], {"state", "reference"}, "previous acquisition")
    if (not isinstance(acquisition["state"], str)
            or acquisition["state"] not in {"authorized_export", "blocked", "unknown"}):
        raise ValueError("previous acquisition: invalid state")
    _text(acquisition["reference"], "previous acquisition.reference", 240)
    coverage_fields = {"state", "scope", "start_cursor", "end_cursor", "empty_confirmed",
                       "pages_read", "posts_read", "known_active", "known_tombstones"}
    coverage = _object(prior["coverage"], coverage_fields, "previous coverage")
    if (coverage["scope"] != scope or not isinstance(coverage["state"], str)
            or coverage["state"] not in {"complete", "partial", "blocked", "unknown"}):
        raise ValueError("previous coverage: invalid scope or state")
    if ((acquisition["state"] == "authorized_export") !=
            (coverage["state"] in {"complete", "partial"})):
        raise ValueError("previous coverage: acquisition state mismatch")
    if acquisition["state"] != "authorized_export" and acquisition["state"] != coverage["state"]:
        raise ValueError("previous coverage: blocked/unknown state mismatch")
    start = _cursor(coverage["start_cursor"], "previous coverage.start_cursor")
    end = _cursor(coverage["end_cursor"], "previous coverage.end_cursor")
    if type(coverage["empty_confirmed"]) is not bool:
        raise ValueError("previous coverage: invalid empty confirmation")
    for key, limit in (("pages_read", max_pages), ("posts_read", max_records),
                       ("known_active", max_records), ("known_tombstones", max_records)):
        if type(coverage[key]) is not int or not 0 <= coverage[key] <= limit:
            raise ValueError(f"previous coverage: invalid {key}")
    if coverage["state"] == "complete" and (start is not None or end is not None):
        raise ValueError("previous coverage: complete snapshot has cursors")
    if coverage["state"] == "partial" and (coverage["pages_read"] == 0 or end is None):
        raise ValueError("previous coverage: partial snapshot lacks a continuation")
    if coverage["state"] in {"blocked", "unknown"} and (
        coverage["pages_read"] or coverage["posts_read"] or start != end or coverage["empty_confirmed"]
    ):
        raise ValueError("previous coverage: blocked/unknown snapshot advanced")
    if coverage["empty_confirmed"] and (
        coverage["state"] != "complete" or coverage["pages_read"] or coverage["posts_read"]
    ):
        raise ValueError("previous coverage: invalid empty confirmation")
    if coverage["state"] == "complete" and coverage["pages_read"] == 0 and not coverage["empty_confirmed"]:
        raise ValueError("previous coverage: unconfirmed empty snapshot")
    if coverage["state"] == "complete" and coverage["posts_read"] == 0 and coverage["pages_read"]:
        raise ValueError("previous coverage: empty pages cannot confirm zero posts")
    delta = _object(prior["delta"],
                    {"added", "updated", "tombstoned", "unchanged", "retained_unobserved"},
                    "previous delta")
    for key, count in delta.items():
        if type(count) is not int or count < 0 or count > max_records:
            raise ValueError(f"previous delta: invalid {key}")
    records = prior["records"]
    if not isinstance(records, list) or len(records) > max_records:
        raise ValueError("previous manifest: record budget exceeded")
    if coverage["posts_read"] > len(records) or sum(delta.values()) != len(records):
        raise ValueError("previous manifest: record counts mismatch")
    seen: set[str] = set()
    ordered_ids: list[str] = []
    for index, item in enumerate(records, 1):
        record = _object(item, RECORD_FIELDS, f"previous record {index}")
        _source_identity(record["source_id"], record["source_url"], record["topic_id"],
                         record["post_number"], f"previous record {index}")
        if (type(record["category_id"]) is not int
                or record["category_id"] != int(scope.removeprefix("category_"))):
            raise ValueError("previous manifest: record category mismatch")
        if record["source_id"] in seen:
            raise ValueError("previous manifest: duplicate source ID")
        seen.add(record["source_id"])
        ordered_ids.append(record["source_id"])
        _utc(record["observed_at"], f"previous record {index}.observed_at")
        if record["observed_at"] > prior["observed_at"]:
            raise ValueError("previous manifest: record observation after snapshot")
        if record["source_revision"] is not None:
            _text(record["source_revision"], f"previous record {index}.source_revision", 80)
        if record["updated_at"] is not None:
            _utc(record["updated_at"], f"previous record {index}.updated_at")
            if record["updated_at"] > record["observed_at"]:
                raise ValueError("previous manifest: source update after observation")
        if (not isinstance(record["access"], str) or record["access"] not in ACCESS
                or not isinstance(record["publication_rights"], str)
                or record["publication_rights"] not in RIGHTS):
            raise ValueError("previous manifest: invalid access or publication rights")
        _text(record["rights_evidence"], f"previous record {index}.rights_evidence", 500)
        if not isinstance(record["status"], str) or record["status"] not in {"active", "tombstone"}:
            raise ValueError("previous manifest: invalid record status")
        digest = record["source_sha256"]
        if digest is not None and (not isinstance(digest, str) or not SHA_RE.fullmatch(digest)):
            raise ValueError("previous manifest: invalid source digest")
        if record["status"] == "active" and (digest is None or record["tombstone_reason"] is not None):
            raise ValueError("previous manifest: invalid active record")
        if (record["status"] == "tombstone"
                and (not isinstance(record["tombstone_reason"], str)
                     or record["tombstone_reason"] not in {
                         "explicit_deletion", "absent_from_complete_snapshot"
                     })):
            raise ValueError("previous manifest: invalid tombstone")
    if (coverage["known_active"] != sum(item["status"] == "active" for item in records)
            or coverage["known_tombstones"] != sum(item["status"] == "tombstone" for item in records)):
        raise ValueError("previous manifest: coverage counts mismatch")
    if ordered_ids != sorted(ordered_ids):
        raise ValueError("previous manifest: records are not ordered")
    return prior


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def import_snapshot(export_path: Path, previous_path: Path | None = None, *,
                    max_bytes: int = DEFAULT_MAX_BYTES,
                    max_pages: int = DEFAULT_MAX_PAGES,
                    max_records: int = DEFAULT_MAX_RECORDS) -> bytes:
    """Return a deterministic metadata-only state manifest for one local snapshot.

    Identical input against its own prior manifest returns exactly the prior
    bytes. For a new snapshot, only complete whole-scope coverage reconciles
    absent IDs as tombstones. No function in this module grants publication.
    """
    _budgets(max_bytes, max_pages, max_records)
    raw_input = _read_bounded(Path(export_path), max_bytes, "snapshot")
    input_sha = hashlib.sha256(raw_input).hexdigest()
    snapshot, seen = _snapshot(_strict_json(raw_input, "snapshot"), max_pages, max_records)
    coverage = snapshot["coverage"]
    previous: dict | None = None
    previous_bytes: bytes | None = None
    if previous_path is not None:
        previous_bytes = _read_bounded(Path(previous_path), max_bytes, "previous manifest")
        previous = _previous(_strict_json(previous_bytes, "previous manifest"),
                             coverage["scope"], max_pages, max_records)
        if previous["input_sha256"] == input_sha:
            expected_coverage = {
                **coverage, "pages_read": len(snapshot["pages"]), "posts_read": len(seen)
            }
            if (previous["observed_at"] != snapshot["observed_at"]
                    or previous["acquisition"] != snapshot["acquisition"]
                    or any(previous["coverage"][key] != value
                           for key, value in expected_coverage.items())):
                raise ValueError("previous manifest: input provenance mismatch")
            prior_records = {record["source_id"]: record for record in previous["records"]}
            for source_id, current in seen.items():
                prior_record = prior_records.get(source_id)
                if prior_record is None:
                    raise ValueError("previous manifest: source identity mismatch")
                expected = {key: value for key, value in current.items() if key != "observed_at"}
                actual = {key: value for key, value in prior_record.items() if key != "observed_at"}
                if current["status"] == "tombstone":
                    # An explicit deletion retains the digest of the formerly
                    # active revision, which this snapshot cannot reproduce.
                    expected["source_sha256"] = actual["source_sha256"]
                if expected != actual:
                    raise ValueError("previous manifest: source identity mismatch")
            if (coverage["state"] == "complete"
                    and any(record["status"] == "active" and source_id not in seen
                            for source_id, record in prior_records.items())):
                raise ValueError("previous manifest: complete coverage left an active record unseen")
            return previous_bytes
        if snapshot["observed_at"] <= previous["observed_at"]:
            raise ValueError("snapshot: observation must be newer than previous manifest")

    known = {record["source_id"]: record for record in previous["records"]} if previous else {}
    delta = {"added": 0, "updated": 0, "tombstoned": 0,
             "unchanged": 0, "retained_unobserved": 0}
    for source_id, record in seen.items():
        old = known.get(source_id)
        if (old is not None and old["updated_at"] is not None
                and record["updated_at"] is not None
                and record["updated_at"] < old["updated_at"]):
            raise ValueError("snapshot: source update time regressed")
        if record["status"] == "tombstone" and old is not None:
            record["source_sha256"] = old["source_sha256"]
        if old is None:
            delta["added"] += 1
            known[source_id] = record
        elif {key: value for key, value in old.items() if key != "observed_at"} == {
            key: value for key, value in record.items() if key != "observed_at"
        }:
            delta["unchanged"] += 1
        else:
            delta["tombstoned" if record["status"] == "tombstone" and old["status"] == "active"
                  else "updated"] += 1
            known[source_id] = record
    for source_id in set(known) - set(seen):
        old = known[source_id]
        if coverage["state"] == "complete":
            if old["status"] == "active":
                known[source_id] = {**old, "status": "tombstone",
                                    "tombstone_reason": "absent_from_complete_snapshot",
                                    "observed_at": snapshot["observed_at"]}
                delta["tombstoned"] += 1
            else:
                delta["unchanged"] += 1
        else:
            delta["retained_unobserved"] += 1
    if len(known) > max_records:
        raise ValueError("manifest: record budget exceeded")

    manifest = {
        "schema": OUTPUT_SCHEMA,
        "origin": ORIGIN,
        "scope": coverage["scope"],
        "observed_at": snapshot["observed_at"],
        "acquisition": snapshot["acquisition"],
        "coverage": {**coverage, "pages_read": len(snapshot["pages"]), "posts_read": len(seen),
                     "known_active": sum(item["status"] == "active" for item in known.values()),
                     "known_tombstones": sum(item["status"] == "tombstone" for item in known.values())},
        "input_sha256": input_sha,
        "model_training_authorized": False,
        "public_projection_approved": False,
        "delta": delta,
        "records": [known[key] for key in sorted(known)],
    }
    manifest["state_sha256"] = hashlib.sha256(_json_bytes(manifest)).hexdigest()
    output = _json_bytes(manifest)
    if len(output) > max_bytes:
        raise ValueError("manifest: byte budget exceeded")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    args = parser.parse_args(argv)
    try:
        if args.snapshot.resolve() == args.out.resolve():
            raise ValueError("snapshot and output paths must differ")
        output = import_snapshot(args.snapshot, args.previous, max_bytes=args.max_bytes,
                                 max_pages=args.max_pages, max_records=args.max_records)
        if args.out.exists():
            if _read_bounded(args.out, args.max_bytes, "output") == output:
                state = "UNCHANGED"
            elif args.previous is None or args.previous.resolve() != args.out.resolve():
                raise ValueError("output exists; use --previous OUT to update it")
            else:
                state = "UPDATED"
        else:
            state = "CREATED"
        if state != "UNCHANGED":
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix=".forum-import-", suffix=".json",
                                             dir=args.out.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(output)
            try:
                os.replace(temporary, args.out)
            finally:
                temporary.unlink(missing_ok=True)
        manifest = json.loads(output)
        print(json.dumps({"state": state, "coverage": manifest["coverage"]["state"],
                          "known_active": manifest["coverage"]["known_active"],
                          "known_tombstones": manifest["coverage"]["known_tombstones"],
                          "delta": manifest["delta"]}, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
