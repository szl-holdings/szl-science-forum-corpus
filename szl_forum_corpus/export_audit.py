"""Audit a local, permitted Discourse export without emitting post content.

The inventory declares the scope; it is not proof of access rights or of a
forum-wide census. This module has no network, publication or training path.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys

from .cli import strict_json


SITE = "https://ai4science.discourse.group"
MAX_INVENTORY_BYTES = 4 * 1024 * 1024
MAX_TOPIC_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_TOPICS = 10_000
MAX_POSTS_PER_TOPIC = 100_000
INVENTORY_FIELDS = {"schema", "site", "scope", "collected_at", "topics"}
TOPIC_FIELDS = {"topic_id", "category_id", "file_sha256", "post_ids"}


def _positive_id(value: object) -> int:
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("invalid positive integer identifier")
    return value


def _ids(value: object) -> list[int]:
    if not isinstance(value, list) or not 0 < len(value) <= MAX_POSTS_PER_TOPIC:
        raise ValueError("post identifier list absent, empty or over limit")
    ids = [_positive_id(item) for item in value]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate post identifier")
    return ids


def _read(path: Path, limit: int) -> bytes:
    # Refuse links even when their resolved destination is inside the export.
    if path.is_symlink() or not path.is_file():
        raise ValueError("input must be a regular file, not a link")
    if path.stat().st_size > limit:
        raise ValueError("input exceeds byte limit")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("input exceeds byte limit")
    return data


def _decode(data: bytes) -> dict:
    try:
        value = strict_json(data.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        # Malformed keys, decoder messages and payloads can contain secrets.
        raise ValueError("invalid UTF-8 JSON or duplicate JSON key") from None
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _inventory(data: bytes) -> dict:
    value = _decode(data)
    if set(value) != INVENTORY_FIELDS:
        raise ValueError("inventory fields differ from schema")
    if (
        value["schema"] != "szl-discourse-export-inventory-v1"
        or value["site"] != SITE
        or value["scope"] != "declared_topic_set"
    ):
        raise ValueError("unsupported inventory schema, site or scope")
    timestamp = value["collected_at"]
    if not isinstance(timestamp, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", timestamp
    ):
        raise ValueError("collected_at must be a UTC timestamp")
    try:
        datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError:
        raise ValueError("invalid collected_at timestamp") from None
    topics = value["topics"]
    if not isinstance(topics, list) or not 0 < len(topics) <= MAX_TOPICS:
        raise ValueError("topic inventory absent, empty or over limit")
    topic_ids: set[int] = set()
    post_ids: set[int] = set()
    for entry in topics:
        if not isinstance(entry, dict) or set(entry) != TOPIC_FIELDS:
            raise ValueError("topic inventory fields differ from schema")
        topic_id = _positive_id(entry["topic_id"])
        _positive_id(entry["category_id"])
        if topic_id in topic_ids:
            raise ValueError("duplicate topic identifier")
        topic_ids.add(topic_id)
        digest = entry["file_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid topic file SHA-256")
        ids = set(_ids(entry["post_ids"]))
        if post_ids & ids:
            raise ValueError("post identifier reused across topics")
        post_ids.update(ids)
    return value


def _topic(value: dict, entry: dict) -> dict:
    topic_id = entry["topic_id"]
    if (
        _positive_id(value.get("id")) != topic_id
        or _positive_id(value.get("category_id")) != entry["category_id"]
    ):
        raise ValueError("topic or category identity differs from inventory")
    if (
        value.get("archetype") != "regular"
        or value.get("visible") is not True
        or value.get("deleted_at") is not None
        or value.get("is_shared_draft", False) is not False
    ):
        raise ValueError("private, invisible, deleted or draft topic unsupported")
    count = value.get("posts_count")
    if type(count) is not int or not 0 <= count <= MAX_POSTS_PER_TOPIC:
        raise ValueError("invalid posts_count")
    stream = value.get("post_stream")
    if not isinstance(stream, dict) or stream.get("isMegaTopic", False) is not False:
        raise ValueError("unsupported or absent post stream")
    expected = set(_ids(entry["post_ids"]))
    stream_ids = set(_ids(stream.get("stream")))
    posts = stream.get("posts")
    if not isinstance(posts, list) or len(posts) > MAX_POSTS_PER_TOPIC:
        raise ValueError("hydrated posts absent or over limit")
    loaded: set[int] = set()
    numbers: set[int] = set()
    for post in posts:
        if not isinstance(post, dict):
            raise ValueError("post must be an object")
        if not isinstance(post.get("cooked"), str):
            raise ValueError("post content field absent or unsupported")
        identifier = _positive_id(post.get("id"))
        number = _positive_id(post.get("post_number"))
        if _positive_id(post.get("topic_id")) != topic_id:
            raise ValueError("hydrated post belongs to another topic")
        if identifier in loaded or number in numbers:
            raise ValueError("duplicate hydrated post ID or post number")
        if (
            post.get("deleted_at") is not None
            or post.get("hidden", False) is not False
            or post.get("user_deleted", False) is not False
            or type(post.get("post_type")) is not int
            or post["post_type"] not in {1, 2, 3}
        ):
            raise ValueError("deleted, hidden, whisper or unsupported post type")
        loaded.add(identifier)
        numbers.add(number)
    # The filtered stream can include small actions and omit filtered posts.
    # A counter discrepancy needs review, never an invented adjustment.
    ids_complete = expected == stream_ids == loaded
    state = "INCOMPLETE"
    if ids_complete:
        state = "COMPLETE" if count == len(stream_ids) else "COUNT_UNRESOLVED"
    return {
        "topic_id": topic_id,
        "category_id": entry["category_id"],
        "file_sha256": entry["file_sha256"],
        "state": state,
        "declared_post_count": len(expected),
        "stream_post_count": len(stream_ids),
        "hydrated_post_count": len(loaded),
        "reported_posts_count": count,
        "reported_count_matches_stream": count == len(stream_ids),
        "missing_stream_ids": sorted(expected - stream_ids),
        "unexpected_stream_ids": sorted(stream_ids - expected),
        "missing_hydrated_ids": sorted(expected - loaded),
        "unexpected_hydrated_ids": sorted(loaded - expected),
    }


def audit_export(root: Path) -> dict:
    """Return a deterministic content-free receipt for one declared local set."""
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("export root must be a directory")
    topic_root = root / "topics"
    if topic_root.is_symlink() or not topic_root.is_dir():
        raise ValueError("topics directory absent or linked")
    if topic_root.resolve() != topic_root:
        raise ValueError("topics directory resolves outside its declared path")
    inventory_data = _read(root / "inventory.json", MAX_INVENTORY_BYTES)
    inventory = _inventory(inventory_data)
    expected_names = {f"{entry['topic_id']}.json" for entry in inventory["topics"]}
    for path in topic_root.iterdir():
        if path.name not in expected_names or path.is_symlink() or not path.is_file():
            raise ValueError("unlisted, linked or non-file topic input")
    total_bytes = len(inventory_data)
    topics = []
    for entry in sorted(inventory["topics"], key=lambda item: item["topic_id"]):
        path = topic_root / f"{entry['topic_id']}.json"
        if not path.exists():
            topics.append({"topic_id": entry["topic_id"], "state": "MISSING_TOPIC_FILE"})
            continue
        data = _read(path, min(MAX_TOPIC_BYTES, MAX_TOTAL_BYTES - total_bytes))
        total_bytes += len(data)
        if hashlib.sha256(data).hexdigest() != entry["file_sha256"]:
            raise ValueError("topic bytes differ from declared SHA-256")
        topics.append(_topic(_decode(data), entry))
    complete = all(topic["state"] == "COMPLETE" for topic in topics)
    return {
        "schema": "szl-discourse-export-audit-v1",
        "site": SITE,
        "scope": "declared_topic_set",
        "collected_at": inventory["collected_at"],
        "inventory_sha256": hashlib.sha256(inventory_data).hexdigest(),
        "input_bytes_read": total_bytes,
        "state": "DECLARED_EXPORT_COMPLETE" if complete else "INCOMPLETE",
        "declared_topic_count": len(topics),
        "complete_topic_count": sum(topic["state"] == "COMPLETE" for topic in topics),
        "topics": topics,
        "forum_wide_coverage": "NOT_ESTABLISHED",
        "access_and_rights_verification": "NOT_PERFORMED",
        "publication_state": "REVIEW_REQUIRED",
        "model_training_authorized": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path, help="restricted local export directory")
    parser.add_argument("--out", type=Path, required=True, help="new local receipt file")
    args = parser.parse_args(argv)
    try:
        root = args.export.resolve(strict=True)
        if args.out.resolve().is_relative_to(root / "topics"):
            raise ValueError("receipt cannot modify the audited topic directory")
        receipt = audit_export(root)
        data = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("xb") as stream:
            stream.write(data)
        print(json.dumps({key: receipt[key] for key in (
            "state", "declared_topic_count", "complete_topic_count",
            "forum_wide_coverage", "model_training_authorized",
        )}, sort_keys=True))
        return 0 if receipt["state"] == "DECLARED_EXPORT_COMPLETE" else 1
    except (OSError, ValueError, RuntimeError):
        # Never echo paths, raw JSON keys, post content or exception payloads.
        print("ERROR: export audit rejected input or could not create a new receipt", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
