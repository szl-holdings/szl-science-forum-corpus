"""Read-only, revision-bound alignment audit of the reviewed public Hub mirror."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts.publish_hf import HEX_40, REPO_ID, source_files
from szl_forum_corpus.cli import strict_json


MAX_METADATA_BYTES = 256 * 1024
MAX_FILE_BYTES = 1024 * 1024
FILES = frozenset({"README.md", "manifest.json", "sources.public.jsonl", "needs.json",
                   "hypotheses.json", "graph.json", "second_brain.candidates.jsonl"})
INFO_URL = f"https://huggingface.co/api/datasets/{REPO_ID}"
Fetch = Callable[[str, int], bytes]


class ReadFailure(Exception):
    def __init__(self, stage: str, status: int | None = None):
        self.stage = stage
        self.status = status


def fetch_public(url: str, limit: int) -> bytes:
    # No credentials, cookies, disk cache, retries or provider write operations.
    request = Request(url, headers={"User-Agent": "szl-forum-alignment-audit/1"})
    with urlopen(request, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("provider response exceeds byte limit")
    return data


def _read(fetch: Fetch, url: str, limit: int, stage: str) -> bytes:
    try:
        data = fetch(url, limit)
        if not isinstance(data, bytes) or len(data) > limit:
            raise ValueError("invalid provider response")
        return data
    except HTTPError as exc:
        raise ReadFailure(stage, exc.code) from None
    except Exception:
        # Provider bodies and exception messages may contain sensitive data.
        raise ReadFailure(stage) from None


def _metadata(fetch: Fetch, url: str, stage: str) -> dict:
    try:
        value = strict_json(_read(fetch, url, MAX_METADATA_BYTES, stage).decode("utf-8"))
        if (
            not isinstance(value, dict) or value.get("id") != REPO_ID
            or value.get("private") is not False
            or not isinstance(value.get("sha"), str) or not HEX_40.fullmatch(value["sha"])
            or not isinstance(value.get("siblings"), list)
        ):
            raise ValueError("invalid public repository metadata")
        names = []
        for entry in value["siblings"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("rfilename"), str):
                raise ValueError("invalid repository file metadata")
            names.append(entry["rfilename"])
        if len(names) != len(set(names)):
            raise ValueError("duplicate repository file metadata")
        return {"sha": value["sha"], "files": set(names)}
    except ReadFailure:
        raise
    except Exception:
        raise ReadFailure(stage) from None


def audit_alignment(files: dict[str, bytes], source_sha: str, fetch: Fetch = fetch_public) -> dict:
    if not HEX_40.fullmatch(source_sha) or set(files) != FILES:
        raise ValueError("invalid source commit or reviewed file inventory")
    if any(not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES for data in files.values()):
        raise ValueError("invalid or oversized source file")
    receipt = {
        "schema": "szl-forum-hf-alignment-v1", "dataset": REPO_ID,
        "source_git_sha": source_sha, "state": "UNAVAILABLE",
        "hf_revision": None, "head_stable": False,
        "files_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
        "observed_files_sha256": {}, "missing_files": [], "unexpected_file_count": 0,
        "mismatched_files": [], "coverage": "SEVEN_REVIEWED_PUBLIC_FILES",
        "model_training_authorized": False, "provider_writes": False,
    }
    try:
        before = _metadata(fetch, INFO_URL, "info_before")
        revision = before["sha"]
        receipt["hf_revision"] = revision
        pinned = _metadata(fetch, f"{INFO_URL}/revision/{revision}", "info_pinned")
        if pinned["sha"] != revision:
            receipt.update(state="CONFLICT", reason="PINNED_REVISION_MISMATCH")
            return receipt
        receipt["missing_files"] = sorted(FILES - pinned["files"])
        receipt["unexpected_file_count"] = len(pinned["files"] - FILES - {".gitattributes"})
        # Only the seven fixed names can be read or enter the public receipt.
        for name in sorted(FILES & pinned["files"]):
            url = f"https://huggingface.co/datasets/{REPO_ID}/resolve/{revision}/{name}"
            observed = _read(fetch, url, MAX_FILE_BYTES, "file_read")
            receipt["observed_files_sha256"][name] = hashlib.sha256(observed).hexdigest()
            if observed != files[name]:
                receipt["mismatched_files"].append(name)
        after = _metadata(fetch, INFO_URL, "info_after")
        receipt["head_stable"] = after["sha"] == revision
        if not receipt["head_stable"] or before["files"] != pinned["files"] or after["files"] != pinned["files"]:
            receipt.update(state="CONFLICT", reason="PROVIDER_HEAD_OR_INVENTORY_CHANGED")
        elif receipt["missing_files"] or receipt["unexpected_file_count"] or receipt["mismatched_files"]:
            receipt.update(state="DRIFT", reason="PUBLIC_PROJECTION_DIFFERS")
        else:
            receipt.update(state="ALIGNED", reason="EXACT_BYTES_AND_STABLE_HEAD")
    except ReadFailure as exc:
        receipt.update(state="UNAVAILABLE", reason="PROVIDER_READ_FAILED", failed_stage=exc.stage)
        if exc.status is not None:
            receipt["http_status"] = exc.status
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        files = source_files(args.expected_sha)
        receipt = audit_alignment(files, args.expected_sha)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("xb") as stream:
            stream.write((json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode())
        print(json.dumps({key: receipt[key] for key in ("state", "source_git_sha", "hf_revision", "head_stable")}, sort_keys=True))
        return 0 if receipt["state"] == "ALIGNED" else 1
    except Exception:
        print("ERROR: alignment audit rejected source or could not create a new receipt", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
