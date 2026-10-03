"""Build a public, source-linked projection from reviewed forum metadata.

This module intentionally has no forum client or raw-post-text field. Bulk access and
reuse rights must be resolved before an independently reviewed source manifest exists.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlparse


ALLOWED_FIELDS = frozenset(
    {
        "source_id", "source_url", "topic_id", "post_number", "access",
        "publication_rights", "rights_evidence", "public_projection_approved",
        "title", "summary", "need_ids", "review_state", "attribution",
        "observed_at", "posted_at",
    }
)
REQUIRED_FIELDS = ALLOWED_FIELDS
NEED_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
SOURCE_RE = re.compile(r"^ai4science:(\d+):(\d+)$")
REVIEW_STATES = {"unreviewed", "operator_labeled", "double_reviewed"}
ACCESS = {"public_web", "members_only"}
RIGHTS = {"operator_authorized", "licensed", "unknown"}
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?<![\w])\+?(?:\d[\s().-]*){10,15}(?![\w])")
SECRET_RE = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|access[_-]?token|bearer|password|secret)\b\s*[:=]\s*\S+"
    r"|\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|hf_[A-Za-z0-9]{20,})\b)"
)


def strict_json(text: str) -> object:
    def unique_pairs(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_nonfinite(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=reject_nonfinite)


def _clean_text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be a nonempty string of at most {limit} characters")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValueError(f"{field} contains a control character")
    return value.strip()


def validate_record(record: object, line_number: int) -> dict:
    if not isinstance(record, dict):
        raise ValueError(f"line {line_number}: expected an object")
    unknown = set(record) - ALLOWED_FIELDS
    missing = REQUIRED_FIELDS - set(record)
    if unknown or missing:
        raise ValueError(
            f"line {line_number}: unknown fields {sorted(unknown)}; missing fields {sorted(missing)}"
        )

    source_id = _clean_text(record["source_id"], "source_id", 80)
    match = SOURCE_RE.fullmatch(source_id)
    if not match:
        raise ValueError(f"line {line_number}: invalid source_id")
    topic_id, post_number = record["topic_id"], record["post_number"]
    if (
        type(topic_id) is not int or topic_id < 1
        or type(post_number) is not int or post_number < 1
        or (topic_id, post_number) != tuple(map(int, match.groups()))
    ):
        raise ValueError(f"line {line_number}: source_id/topic_id/post_number mismatch")

    source_url = _clean_text(record["source_url"], "source_url", 500)
    parsed = urlparse(source_url)
    post_path = re.fullmatch(rf"/t/[a-z0-9-]+/{topic_id}(?:/([1-9][0-9]*))?/?", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "ai4science.discourse.group"
        or parsed.username or parsed.password or parsed.port
        or parsed.query or parsed.fragment
        or not post_path
        or int(post_path.group(1) or "1") != post_number
    ):
        raise ValueError(f"line {line_number}: invalid or unsafe source_url")

    if record["access"] not in ACCESS or record["publication_rights"] not in RIGHTS:
        raise ValueError(f"line {line_number}: unknown access or publication_rights")
    if type(record["public_projection_approved"]) is not bool:
        raise ValueError(f"line {line_number}: public_projection_approved must be Boolean")
    if record["review_state"] not in REVIEW_STATES:
        raise ValueError(f"line {line_number}: unknown review_state")
    title = _clean_text(record["title"], "title", 240)
    summary = _clean_text(record["summary"], "summary", 1000)
    rights_evidence = _clean_text(record["rights_evidence"], "rights_evidence", 500)
    attribution = _clean_text(record["attribution"], "attribution", 120)
    if any(EMAIL_RE.search(value) for value in (title, summary, rights_evidence, attribution)):
        raise ValueError(f"line {line_number}: email address in public metadata")
    if any(PHONE_RE.search(value) or SECRET_RE.search(value) for value in (title, summary, rights_evidence)):
        raise ValueError(f"line {line_number}: contact number or credential in public metadata")
    observed_at = _clean_text(record["observed_at"], "observed_at", 10)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", observed_at):
        raise ValueError(f"line {line_number}: invalid observed_at")
    try:
        date.fromisoformat(observed_at)
    except ValueError as exc:
        raise ValueError(f"line {line_number}: invalid observed_at") from exc
    posted_at = record["posted_at"]
    if posted_at is not None:
        posted_at = _clean_text(posted_at, "posted_at", 10)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", posted_at):
            raise ValueError(f"line {line_number}: invalid posted_at")
        try:
            date.fromisoformat(posted_at)
        except ValueError as exc:
            raise ValueError(f"line {line_number}: invalid posted_at") from exc
    if not isinstance(record["need_ids"], list) or not record["need_ids"]:
        raise ValueError(f"line {line_number}: need_ids must be a nonempty list")
    needs = record["need_ids"]
    if any(not isinstance(need, str) or not NEED_RE.fullmatch(need) for need in needs):
        raise ValueError(f"line {line_number}: invalid need_id")
    if len(needs) != len(set(needs)):
        raise ValueError(f"line {line_number}: duplicate need_id")
    return {
        **record,
        "source_id": source_id,
        "source_url": source_url,
        "title": title,
        "summary": summary,
        "rights_evidence": rights_evidence,
        "attribution": attribution,
        "observed_at": observed_at,
        "posted_at": posted_at,
        "need_ids": sorted(needs),
    }


def read_records(path: Path) -> list[dict]:
    records: list[dict] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = validate_record(strict_json(line), number)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {number}: invalid JSON: {exc.msg}") from exc
        if record["source_id"] in seen:
            raise ValueError(f"line {number}: duplicate source_id {record['source_id']}")
        seen.add(record["source_id"])
        records.append(record)
    if not records:
        raise ValueError("source file has no records")
    return records


def may_publish(record: dict) -> bool:
    if (
        not record["public_projection_approved"]
        or record["publication_rights"] == "unknown"
        or record["review_state"] == "unreviewed"
    ):
        return False
    if record["access"] == "members_only":
        return record["publication_rights"] == "operator_authorized"
    return record["publication_rights"] in {"operator_authorized", "licensed"}


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _jsonl_bytes(values: list[dict]) -> bytes:
    return ("".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values)).encode("utf-8")


def _load_opportunities(path: Path) -> list[dict]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value:
        raise ValueError("opportunities must be a nonempty JSON list")
    ids: set[str] = set()
    required = {"id", "need_ids", "baseline", "intervention", "primary_outcome", "failure_condition", "status"}
    for item in value:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("opportunity fields differ from the reviewed schema")
        identifier = item["id"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", identifier):
            raise ValueError("invalid opportunity id")
        if identifier in ids:
            raise ValueError("duplicate opportunity id")
        ids.add(identifier)
        if (
            not isinstance(item["need_ids"], list)
            or not item["need_ids"]
            or any(not isinstance(need, str) or not NEED_RE.fullmatch(need) for need in item["need_ids"])
        ):
            raise ValueError("invalid opportunity need_ids")
        for field in ("baseline", "intervention", "primary_outcome", "failure_condition"):
            _clean_text(item[field], field, 1000)
        if item["status"] != "proposal":
            raise ValueError("opportunities must remain proposals until independently evaluated")
    return value


def build(records: list[dict], opportunities: list[dict], as_of: str) -> dict[str, bytes]:
    date.fromisoformat(as_of)
    approved = sorted((r for r in records if may_publish(r)), key=lambda r: r["source_id"])
    opportunities_sha256 = hashlib.sha256(_json_bytes(opportunities)).hexdigest()
    sources = [
        {
            "source_id": r["source_id"], "source_url": r["source_url"],
            "topic_id": r["topic_id"], "post_number": r["post_number"],
            "title": r["title"], "summary": r["summary"],
            "need_ids": r["need_ids"], "review_state": r["review_state"],
            "observed_at": r["observed_at"],
            "posted_at": r["posted_at"],
            "publication_rights": r["publication_rights"],
            "rights_evidence": r["rights_evidence"],
        }
        for r in approved
    ]
    # Public commitments cover exactly the public rows. The reviewed input
    # also contains private attribution, which must not affect a public hash.
    public_records_sha256 = hashlib.sha256(_jsonl_bytes(sources)).hexdigest()
    need_to_topics: dict[str, set[int]] = defaultdict(set)
    need_to_sources: dict[str, set[str]] = defaultdict(set)
    for record in approved:
        for need in record["need_ids"]:
            need_to_topics[need].add(record["topic_id"])
            need_to_sources[need].add(record["source_id"])
    needs = [
        {
            "need_id": need, "independent_topic_count": len(need_to_topics[need]),
            "source_ids": sorted(need_to_sources[need]),
            "inference": "descriptive_observation_only",
        }
        for need in sorted(need_to_topics)
    ]
    hypothesis_cards = []
    for opportunity in opportunities:
        observed_need_ids = sorted(set(opportunity["need_ids"]) & set(need_to_topics))
        proposed_only_need_ids = sorted(set(opportunity["need_ids"]) - set(need_to_topics))
        refs = sorted({source for need in observed_need_ids for source in need_to_sources[need]})
        hypothesis_cards.append({
            **opportunity,
            "observed_need_ids": observed_need_ids,
            "proposed_only_need_ids": proposed_only_need_ids,
            "source_ids": refs,
            "evidence_state": "exploratory" if refs else "no_admitted_source",
        })
    nodes = [
        {"id": f"source:{r['source_id']}", "type": "Source", "url": r["source_url"]}
        for r in sources
    ] + [
        {"id": f"need:{n['need_id']}", "type": "Need", "independent_topic_count": n["independent_topic_count"]}
        for n in needs
    ] + [
        {"id": f"need:{need}", "type": "ProposedNeed", "independent_topic_count": 0}
        for need in sorted({need for h in hypothesis_cards for need in h["proposed_only_need_ids"]})
    ] + [
        {"id": f"hypothesis:{h['id']}", "type": "Hypothesis", "evidence_state": h["evidence_state"]}
        for h in hypothesis_cards
    ]
    edges = [
        {"from": f"source:{r['source_id']}", "to": f"need:{need}", "relation": "raises_candidate_need"}
        for r in sources for need in r["need_ids"]
    ] + [
        {"from": f"need:{need}", "to": f"hypothesis:{h['id']}", "relation": "motivates_test"}
        for h in hypothesis_cards for need in h["observed_need_ids"]
    ] + [
        {"from": f"need:{need}", "to": f"hypothesis:{h['id']}", "relation": "proposed_for_test"}
        for h in hypothesis_cards for need in h["proposed_only_need_ids"]
    ]
    files = {
        "sources.public.jsonl": _jsonl_bytes(sources),
        "needs.json": _json_bytes(needs),
        "hypotheses.json": _json_bytes(hypothesis_cards),
        "graph.json": _json_bytes({"schema": "szl-science-needgraph-v1", "nodes": nodes, "edges": edges}),
    }
    # Compatible with the Second Brain corpus row shape, but not installed in
    # Second Brain. Admission requires its own source PR, digest update and CI.
    files["second_brain.candidates.jsonl"] = _jsonl_bytes([
        {
            "id": f"forum_insight:{r['topic_id']}:{r['post_number']}",
            "source": "forum_insight",
            "sourceId": r["source_url"],
            "title": r["title"],
            "text": r["summary"],
            "sha256": hashlib.sha256(r["summary"].encode("utf-8")).hexdigest(),
            "rightsEvidence": r["rights_evidence"],
            "reviewState": r["review_state"],
        }
        for r in sources
    ])
    manifest = {
        "schema": "szl-science-forum-corpus-v1",
        "as_of": as_of,
        "source_records_seen": len(records),
        "source_records_public": len(approved),
        "source_records_withheld": len(records) - len(approved),
        "independent_public_topics": len({r["topic_id"] for r in approved}),
        "sampling": "operator_convenience_sample_not_representative",
        "forum_inventory_status": "not_established_by_this_build",
        "source_revision_status": "not_recorded_in_public_pilot",
        "public_scope": "metadata_and_original_summaries_only",
        "rights_basis": "per_record_assertions_no_site_wide_grant_inferred",
        "public_source_records_sha256": public_records_sha256,
        "opportunities_sha256": opportunities_sha256,
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "record_schema_sha256": hashlib.sha256(
            (Path(__file__).resolve().parents[1] / "schema" / "record-v1.schema.json").read_bytes()
        ).hexdigest(),
        "model_training_authorized": False,
        "files_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
    }
    files["manifest.json"] = _json_bytes(manifest)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("input", type=Path)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("input", type=Path)
    build_parser.add_argument("--out", type=Path, required=True)
    build_parser.add_argument("--as-of", required=True)
    build_parser.add_argument("--opportunities", type=Path, default=Path(__file__).resolve().parents[1] / "opportunities.json")
    args = parser.parse_args(argv)
    try:
        records = read_records(args.input)
        if args.command == "validate":
            print(json.dumps({
                "records": len(records),
                "public": sum(may_publish(record) for record in records),
                "withheld": sum(not may_publish(record) for record in records),
            }, sort_keys=True))
            return 0
        if args.out.exists():
            raise ValueError(f"output already exists: {args.out}")
        opportunities = _load_opportunities(args.opportunities)
        files = build(records, opportunities, args.as_of)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".szl-forum-build-", dir=args.out.parent) as temporary:
            root = Path(temporary)
            for name, data in files.items():
                (root / name).write_bytes(data)
            root.rename(args.out)
        print(json.dumps(json.loads(files["manifest.json"]), sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
