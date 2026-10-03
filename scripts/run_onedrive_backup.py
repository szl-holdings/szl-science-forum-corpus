"""Run a bounded backup, with one writer per configured private state directory.

Plan is local-only. Apply reuses a validated snapshot, requests an immutable
cloud copy, and records independent content readback. It never schedules itself.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
import zipfile
import zlib

from scripts import backup_onedrive as backup
from scripts import verify_onedrive_backup as verify

STATE_SCHEMA = "szl.onedrive-runner-state/v1"
MAX_STATE_BYTES = 16 * 1024
MAX_RECEIPTS = 1000
SHA = re.compile(r"[0-9a-f]{40}", re.ASCII)


class RunError(RuntimeError):
    """Fixed, credential-free diagnostics only."""


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def private_state(raw: str, sync_root: Path) -> Path:
    root = verify.checked_path(raw, directory=True)
    if root.is_relative_to(sync_root) or root.is_relative_to(backup.ROOT.resolve()):
        raise RunError("STATE_IN_REPOSITORY_OR_SYNC_ROOT")
    for parent in (root, *root.parents):
        try:
            (parent / ".git").lstat()
        except FileNotFoundError:
            continue
        raise RunError("STATE_IN_GIT_CHECKOUT")
    info = root.stat()
    if os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
        raise RunError("STATE_DIRECTORY_NOT_PRIVATE")
    return root


def existing_file(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    verify.checked_path(path)
    return True


@contextmanager
def exclusive(root: Path):
    path = root / "runner.lock"
    existing_file(path)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as lock:
        info = os.fstat(lock.fileno())
        if info.st_nlink != 1 or info.st_size > 1:
            raise RunError("UNSAFE_LOCK_FILE")
        if not info.st_size:
            lock.write(b"0")
        lock.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise RunError("RUN_ALREADY_ACTIVE") from None
        try:
            if (info.st_dev, info.st_ino) != (path.stat().st_dev, path.stat().st_ino):
                raise RunError("LOCK_IDENTITY_CHANGED")
            yield
        finally:
            if acquired:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def atomic_json(path: Path, value: dict) -> None:
    if existing_file(path):
        if path.stat().st_nlink != 1:
            raise RunError("LINKED_STATE_FILE")
    pending = path.parent / (".pending-" + uuid.uuid4().hex + ".json")
    try:
        with pending.open("x", encoding="utf-8") as stream:
            if os.name != "nt":
                os.chmod(pending, 0o600)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def unique_keys(items):
    result = {}
    for key, value in items:
        if key in result:
            raise RunError("INVALID_STATE")
        result[key] = value
    return result


def load_state(root: Path) -> dict | None:
    path = root / "state.json"
    if not existing_file(path):
        return None
    if not 0 < path.stat().st_size <= MAX_STATE_BYTES or path.stat().st_nlink != 1:
        raise RunError("INVALID_STATE")
    with path.open("rb") as stream:
        raw = stream.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise RunError("INVALID_STATE")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys)
    except (ValueError, UnicodeError):
        raise RunError("INVALID_STATE") from None
    keys = {"schema", "source_revision", "archive_path", "archive_sha256"}
    if (not isinstance(value, dict) or set(value) != keys or value.get("schema") != STATE_SCHEMA
            or not isinstance(value.get("source_revision"), str)
            or SHA.fullmatch(value["source_revision"]) is None
            or not isinstance(value.get("archive_path"), str)
            or not isinstance(value.get("archive_sha256"), str)
            or verify.SHA256.fullmatch(value["archive_sha256"]) is None):
        raise RunError("INVALID_STATE")
    return value


def current_main(expected: str) -> None:
    rows = backup.git("ls-remote", "--exit-code", "origin", "refs/heads/main").decode("ascii").splitlines()
    if rows != [expected + "\trefs/heads/main"]:
        raise RunError("REMOTE_MAIN_CHANGED")


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds < 1:
        raise RunError("RUN_DEADLINE")
    return seconds


def execute(args) -> dict:
    revision, entries, bodies = backup.snapshot()
    if not args.expected_sha or SHA.fullmatch(args.expected_sha) is None or revision != args.expected_sha:
        raise RunError("EXPECTED_SOURCE_REQUIRED_OR_CHANGED")
    plan = {"schema": "szl.onedrive-backup-run/v1", "state": "PLAN_ONLY",
            "source_revision": revision, "source_files": len(entries),
            "source_bytes": sum(item["bytes"] for item in entries),
            "remote_evidence_class": "UNKNOWN", "remote_readback_verified": False,
            "signed": False, "training_authorized": False}
    if not args.apply:
        return plan
    if not all((args.state_dir, args.rclone_config, args.rclone_executable, args.remote,
                args.expected_rclone_sha256)):
        raise RunError("EXPLICIT_PROVIDER_CONFIGURATION_REQUIRED")
    if (verify.SHA256.fullmatch(args.expected_rclone_sha256) is None
            or not 1 <= args.timeout <= 300):
        raise RunError("INVALID_EXECUTABLE_HASH_OR_TIMEOUT")
    sync_root = verify.checked_path(backup.registered_onedrive(), directory=True)
    config, executable, environment = verify.private_remote(
        args.rclone_config, args.rclone_executable, args.remote, sync_root)
    if digest_file(executable) != args.expected_rclone_sha256:
        raise RunError("EXECUTABLE_HASH_MISMATCH")
    state_root = private_state(args.state_dir, sync_root)
    with exclusive(state_root):
        state = load_state(state_root)
        current_main(revision)
        receipts = state_root / "receipts"
        if receipts.exists():
            verify.checked_path(receipts, directory=True)
            for count, _ in enumerate(receipts.iterdir(), 1):
                if count >= MAX_RECEIPTS:
                    raise RunError("RECEIPT_CAPACITY_REACHED")
        else:
            receipts.mkdir(mode=0o700)
        run_id = uuid.uuid4().hex
        receipt_path = receipts / (run_id + ".json")
        report = dict(plan, state="STARTED", run_id=run_id,
                      observed_at=datetime.now(timezone.utc).isoformat(),
                      executable_sha256=args.expected_rclone_sha256,
                      cloud_copy_requested=False, snapshot_reused=False,
                      native_client_sync="UNKNOWN", full_disaster_recovery=False,
                      runner_sha256=digest_file(Path(__file__)),
                      verifier_sha256=digest_file(Path(verify.__file__)))
        atomic_json(receipt_path, report)
        try:
            if state and state["source_revision"] == revision:
                archive, _ = verify.validate_local(state["archive_path"], state["archive_sha256"])
                checksum = state["archive_sha256"]
                report["snapshot_reused"] = True
            else:
                staged = backup.stage(sync_root, revision, entries, bodies)
                archive, checksum = Path(staged["archive_path"]), staged["archive_sha256"]
                verify.validate_local(archive, checksum)
                atomic_json(state_root / "state.json", {
                    "schema": STATE_SCHEMA, "source_revision": revision,
                    "archive_path": str(archive), "archive_sha256": checksum})
            current_main(revision)
            target = "/".join((*verify.CLOUD_COPY_SUBTREE, checksum, archive.name))
            report.update(state="COPY_REQUESTED", archive_sha256=checksum,
                          archive_bytes=archive.stat().st_size, cloud_copy_requested=True)
            atomic_json(receipt_path, report)
            deadline = time.monotonic() + args.timeout
            budget = remaining(deadline)
            result = subprocess.run([
                str(executable), "copyto", str(archive), args.remote + ":" + target,
                "--config", str(config), "--immutable", "--checksum", "--no-traverse",
                "--transfers", "1", "--checkers", "1", "--retries", "1",
                "--low-level-retries", "1", "--max-duration", f"{budget:.3f}s",
                "--max-transfer", str(report["archive_bytes"] + 65536),
                "--no-update-modtime", "--buffer-size", "0"],
                env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=budget)
            if result.returncode:
                raise RunError("CLOUD_COPY_FAILED")
            _, measured = verify.validate_local(archive, checksum)
            measured = verify.verify_remote(archive, measured, config, executable,
                                            args.remote, remaining(deadline), cloud_copy=True)
            verify.validate_local(archive, checksum)
            current_main(revision)
            report.update(measured, state="REMOTE_CONTENT_VERIFIED",
                          observed_at=datetime.now(timezone.utc).isoformat())
        except (RunError, verify.VerificationError, backup.BackupError, OSError,
                ValueError, subprocess.SubprocessError, zipfile.BadZipFile,
                zlib.error, EOFError, RuntimeError) as error:
            reason = str(error) if isinstance(error, (RunError, verify.VerificationError)) else "RUN_FAILED"
            report.update(state="BLOCKED", reason=reason, remote_evidence_class="UNKNOWN",
                          remote_readback_verified=False)
            atomic_json(receipt_path, report)
            return report
        atomic_json(receipt_path, report)
        return report


def main(argv=None) -> int:
    parser = verify.SanitizedParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--state-dir")
    parser.add_argument("--remote")
    parser.add_argument("--rclone-config")
    parser.add_argument("--rclone-executable")
    parser.add_argument("--expected-rclone-sha256")
    parser.add_argument("--timeout", type=float, default=120)
    try:
        args = parser.parse_args(argv)
        report = execute(args)
    except (RunError, verify.VerificationError, backup.BackupError, OSError,
            ValueError, subprocess.SubprocessError, zipfile.BadZipFile,
            zlib.error, EOFError, RuntimeError) as error:
        reason = str(error) if isinstance(error, (RunError, verify.VerificationError)) else "RUN_FAILED"
        report = {"state": "BLOCKED", "reason": reason, "remote_evidence_class": "UNKNOWN",
                  "remote_readback_verified": False, "signed": False}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["state"] in {"PLAN_ONLY", "REMOTE_CONTENT_VERIFIED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
