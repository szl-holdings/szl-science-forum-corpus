"""Publish only the reviewed public projection from protected GitHub main.

The default is a local, offline preflight. Provider writes require --publish,
an exact expected commit, a main-branch Actions context and an explicit gate.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable

from scripts.verify_projection import DATASET, ROOT, main as verify_projection
from szl_forum_corpus.cli import may_publish, read_records


REPO_ID = "SZLHOLDINGS/szl-science-forum-corpus"
SOURCE_INPUT = ROOT / "examples" / "operator_topics.jsonl"
HEX_40 = re.compile(r"^[0-9a-f]{40}$")


def _provider_call(stage: str, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Keep the failing provider stage without printing response bodies or credentials."""
    try:
        return operation(*args, **kwargs)
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        code = f" HTTP {status}" if isinstance(status, int) else ""
        raise ValueError(f"{stage}: provider {type(exc).__name__}{code}") from None


def public_files() -> dict[str, bytes]:
    if verify_projection() != 0:
        raise ValueError("committed dataset differs from its deterministic source")
    records = read_records(SOURCE_INPUT)
    if not all(may_publish(record) for record in records):
        raise ValueError("source index includes a withheld or unreviewed record")
    manifest = json.loads((DATASET / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("model_training_authorized") is not False:
        raise ValueError("training authority drifted")
    if manifest.get("source_records_withheld") != 0:
        raise ValueError("withheld records cannot enter the Hugging Face mirror")
    files = {path.name: path.read_bytes() for path in DATASET.iterdir() if path.is_file()}
    if "README.md" not in files or len(files) != 7:
        raise ValueError("unexpected dataset file inventory")
    return files


def _local_git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def _provider_file_bytes(name: str, revision: str, token: str, stage: str) -> bytes:
    from huggingface_hub import hf_hub_download

    path = Path(_provider_call(stage, hf_hub_download,
        repo_id=REPO_ID, repo_type="dataset", filename=name,
        revision=revision, token=token
    ))
    return path.read_bytes()


def _read_back(api, revision: str, files: dict[str, bytes], token: str) -> None:
    info = _provider_call("read_back_info", api.dataset_info, REPO_ID, revision=revision)
    if info.sha != revision or info.private:
        raise ValueError("provider revision or visibility mismatch")
    observed = {entry.rfilename for entry in info.siblings}
    if observed - (set(files) | {".gitattributes"}) or set(files) - observed:
        raise ValueError("provider file inventory mismatch")
    for name, expected in files.items():
        if _provider_file_bytes(name, revision, token, "read_back_file") != expected:
            raise ValueError(f"provider byte mismatch: {name}")


def publish(files: dict[str, bytes], expected_sha: str) -> dict:
    if not HEX_40.fullmatch(expected_sha) or expected_sha != _local_git_sha():
        raise ValueError("expected Git commit differs from checked-out source")
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("provider publication requires GitHub main")
    if os.environ.get("GITHUB_SHA") != expected_sha:
        raise ValueError("GitHub Actions source SHA mismatch")
    if os.environ.get("SZL_HF_PUBLISH_APPROVED") != "1":
        raise ValueError("explicit publication gate is absent")
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise ValueError("HF_TOKEN is unavailable")
    if token != token.strip():
        raise ValueError("HF_TOKEN has surrounding whitespace")

    from huggingface_hub import CommitOperationAdd, HfApi

    api = HfApi(token=token)
    identity = _provider_call("authenticate", api.whoami)
    if not isinstance(identity, dict) or not identity.get("name"):
        raise ValueError("authenticate: provider returned no account identity")
    exists = _provider_call("repo_exists", api.repo_exists, REPO_ID, repo_type="dataset")
    if not exists:
        raise ValueError("canonical Hugging Face dataset does not exist; creation requires separate review")
    before = _provider_call("inspect_before", api.dataset_info, REPO_ID)
    if before.private:
        raise ValueError("existing dataset is private; visibility change requires separate review")
    if not isinstance(before.sha, str) or not HEX_40.fullmatch(before.sha):
        raise ValueError("provider did not return an exact existing commit SHA")
    existing = {entry.rfilename for entry in before.siblings}
    if existing - (set(files) | {".gitattributes"}):
        raise ValueError("existing dataset contains unreviewed extra files")

    changed = [
        name for name in sorted(files)
        if name not in existing
        or _provider_file_bytes(name, before.sha, token, "inspect_file") != files[name]
    ]
    if changed:
        # Upload the bytes checked by public_files(), rather than rereading a
        # path that may change between preflight and the provider commit.
        operations = [
            CommitOperationAdd(path_in_repo=name, path_or_fileobj=io.BytesIO(files[name]))
            for name in changed
        ]
        commit = _provider_call("create_commit", api.create_commit,
            repo_id=REPO_ID, repo_type="dataset", operations=operations,
            commit_message=f"Mirror signed GitHub source {expected_sha}",
            parent_commit=before.sha,
        )
        revision = commit.oid
        if not isinstance(revision, str) or not HEX_40.fullmatch(revision):
            raise ValueError("provider did not return an exact commit SHA")
        state = "PUBLISHED_AND_READ_BACK"
    else:
        revision = before.sha
        state = "UNCHANGED_AND_READ_BACK"
    _read_back(api, revision, files, token)
    return {
        "state": state,
        "source_git_sha": expected_sha,
        "dataset": REPO_ID,
        "hf_revision": revision,
        "changed_files": changed,
        "files_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--expected-sha", default="")
    args = parser.parse_args(argv)
    try:
        files = public_files()
        if not args.publish:
            print(json.dumps({
                "state": "READY_NO_PROVIDER_WRITE",
                "dataset": REPO_ID,
                "source_git_sha": _local_git_sha(),
                "files_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
            }, sort_keys=True))
            return 0
        print(json.dumps(publish(files, args.expected_sha), sort_keys=True))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Provider exceptions can contain request headers or response bodies.
        print(f"ERROR: provider operation failed ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
