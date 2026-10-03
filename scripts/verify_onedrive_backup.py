"""Validate a staged source ZIP; opt in to bounded OneDrive content readback."""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import stat
import subprocess
import sys
import threading
import time
import zipfile
import zlib

from scripts import backup_onedrive as backup

MAX_ARCHIVE_BYTES = backup.MAX_BYTES + 1024 * 1024
MAX_CONFIG_BYTES = 64 * 1024
MAX_STAT_BYTES = 16 * 1024
CHUNK_BYTES = 64 * 1024
SUBTREE = ("SZL-Corpus-Backups", "szl-science-forum-corpus")
CLOUD_COPY_SUBTREE = ("SZL-Verified-Corpus-Backups", "szl-science-forum-corpus")
SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
REMOTE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}", re.ASCII)
FOLDER = re.compile(r"[0-9]{8}T[0-9]{6}Z_([0-9a-f]{12})_[0-9a-f]{8}", re.ASCII)


class VerificationError(RuntimeError):
    """Only fixed, credential-free reason codes cross the CLI boundary."""


def checked_path(raw: str | Path, *, directory: bool = False) -> Path:
    value = str(raw)
    path = Path(value)
    if (not path.is_absolute() or any(c in value for c in "\x00\r\n")
            or any(p in {".", ".."} for p in re.split(r"[/\\]", value))
            or (os.name == "nt" and (value.startswith(("\\\\", "//")) or ":" in value[2:]))):
        raise VerificationError("UNSAFE_LOCAL_PATH")
    # Check before resolve: resolving first would hide symlinks or junctions.
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_reparse_tag", 0) in {0xA0000003, 0xA000000C}):
            raise VerificationError("LINKED_LOCAL_PATH")
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise VerificationError("LOCAL_PATH_TYPE_MISMATCH")
    # Reading online-only placeholders could implicitly hydrate the source ZIP.
    if not directory and getattr(info, "st_file_attributes", 0) & (0x1000 | 0x40000 | 0x400000):
        raise VerificationError("LOCAL_FILE_NOT_RESIDENT")
    return resolved


def staged_relative(path: Path, revision: str) -> str:
    parts = path.parts[-4:]
    folder = FOLDER.fullmatch(parts[2]) if len(parts) == 4 else None
    if (len(parts) != 4 or tuple(parts[:2]) != SUBTREE or folder is None
            or folder[1] != revision[:12]
            or parts[3] != f"szl-science-forum-corpus-{revision[:12]}.zip"):
        raise VerificationError("STAGED_PATH_BINDING_MISMATCH")
    return "/".join(parts)


def fingerprint(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def validate_local(raw: str | Path, expected: str) -> tuple[Path, dict]:
    if SHA256.fullmatch(expected) is None:
        raise VerificationError("EXPECTED_SHA256_REQUIRED")
    path = checked_path(raw)
    path_info = path.stat()
    path_initial = fingerprint(path_info)
    size = path_info.st_size
    if not 0 < size <= MAX_ARCHIVE_BYTES:
        raise VerificationError("ARCHIVE_SIZE_LIMIT")
    revision, entries, _ = backup.snapshot()  # Local canonical origin/main; no fetch.
    staged_relative(path, revision)
    manifest = backup.manifest_bytes(revision, entries)
    if len(manifest) > 1024 * 1024:
        raise VerificationError("MANIFEST_SIZE_LIMIT")
    expected_members = {item["path"]: item for item in entries}
    with path.open("rb") as source:
        initial = fingerprint(os.fstat(source.fileno()))
        if initial[:3] != path_initial[:3]:
            raise VerificationError("LOCAL_ARCHIVE_CHANGED")
        digest = hashlib.sha256()
        total = 0
        while chunk := source.read(min(CHUNK_BYTES, size + 1 - total)):
            total += len(chunk)
            if total > size:
                raise VerificationError("LOCAL_ARCHIVE_CHANGED")
            digest.update(chunk)
        if total != size or digest.hexdigest() != expected:
            raise VerificationError("ARCHIVE_SHA256_MISMATCH")
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            names = [item.filename for item in members]
            if (len(members) > backup.MAX_FILES + 1 or len(names) != len(set(names))
                    or set(names) != set(expected_members) | {"BACKUP_MANIFEST.json"}):
                raise VerificationError("ARCHIVE_INVENTORY_MISMATCH")
            if archive.comment != revision.encode("ascii"):
                raise VerificationError("ARCHIVE_REVISION_MISMATCH")
            # Check every declared size/type before decompressing any member.
            for item in members:
                bound = (len(manifest) if item.filename == "BACKUP_MANIFEST.json"
                         else expected_members[item.filename]["bytes"])
                kind = stat.S_IFMT(item.external_attr >> 16)
                if (item.file_size != bound or item.is_dir() or kind not in {0, stat.S_IFREG}
                        or item.flag_bits & 1
                        or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                    raise VerificationError("ARCHIVE_MEMBER_BINDING_MISMATCH")
            for item in members:
                is_manifest = item.filename == "BACKUP_MANIFEST.json"
                checksum = hashlib.sha256()
                count = 0
                with archive.open(item) as member:
                    while chunk := member.read(min(CHUNK_BYTES, item.file_size + 1 - count)):
                        count += len(chunk)
                        if count > item.file_size:
                            raise VerificationError("ARCHIVE_MEMBER_SIZE_LIMIT")
                        checksum.update(chunk)
                wanted = (hashlib.sha256(manifest).hexdigest() if is_manifest
                          else expected_members[item.filename]["sha256"])
                if count != item.file_size or checksum.hexdigest() != wanted:
                    raise VerificationError("ARCHIVE_SOURCE_MISMATCH")
        # Windows 3.12 can report different ctime values through stat/fstat.
        # Preserve both change guards, comparing each API to its own baseline.
        if (initial != fingerprint(os.fstat(source.fileno()))
                or path_initial != fingerprint(checked_path(path).stat())):
            raise VerificationError("LOCAL_ARCHIVE_CHANGED")
    return path, {"schema": "szl.onedrive-content-integrity/v1", "state": "PLAN_ONLY",
                  "local_evidence_class": "MEASURED", "source_revision": revision,
                  "source_files": len(entries), "archive_bytes": size,
                  "archive_sha256": expected, "remote_readback_verified": False,
                  "remote_evidence_class": "UNKNOWN", "native_client_sync": "UNKNOWN"}


def private_remote(config_raw: str | Path, executable_raw: str | Path,
                   remote: str, root: Path) -> tuple[Path, Path, dict[str, str]]:
    if REMOTE.fullmatch(remote) is None:
        raise VerificationError("UNSAFE_REMOTE_NAME")
    if any(key.upper().startswith("RCLONE_") for key in os.environ):
        raise VerificationError("RCLONE_ENVIRONMENT_OVERRIDE")
    config = checked_path(config_raw)
    executable = checked_path(executable_raw)
    if any(config.is_relative_to(parent) for parent in (backup.ROOT.resolve(), root)):
        raise VerificationError("CONFIG_IN_REPOSITORY_OR_SYNC_ROOT")
    for parent in config.parents:
        try:
            (parent / ".git").lstat()
        except FileNotFoundError:
            continue
        # Reject any marker, including a worktree's .git file or a linked marker.
        raise VerificationError("CONFIG_IN_GIT_CHECKOUT")
    info = config.stat()
    if (not 0 < info.st_size <= MAX_CONFIG_BYTES or info.st_nlink != 1
            or (os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()))):
        raise VerificationError("CONFIG_NOT_PRIVATE_OR_BOUNDED")
    if ((os.name == "nt" and executable.suffix.lower() != ".exe")
            or (os.name != "nt" and not os.access(executable, os.X_OK))):
        raise VerificationError("NATIVE_EXECUTABLE_REQUIRED")
    parser = configparser.ConfigParser(interpolation=None, strict=True,
                                       empty_lines_in_values=False)
    with config.open("rb") as source:
        raw = source.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise VerificationError("CONFIG_SIZE_LIMIT")
    try:
        parser.read_string(raw.decode("utf-8"))
        if parser.defaults() or not parser.has_section(remote):
            raise VerificationError("EXPLICIT_CONFIG_SECTION_REQUIRED")
        section = parser[remote]
        if section.get("type") != "onedrive":
            raise VerificationError("ONEDRIVE_BACKEND_REQUIRED")
        if section.get("drive_type") != "personal":
            raise VerificationError("PERSONAL_DRIVE_REQUIRED")
        # No alternate root, endpoint, token file, wrapper or password command.
        allowed = {"type", "drive_type", "drive_id", "token", "client_id", "client_secret",
                   "access_scopes", "disable_site_permission", "client_credentials"}
        if set(section) - allowed or not section.get("drive_id", "").strip():
            raise VerificationError("UNSUPPORTED_REMOTE_CONFIGURATION")
        if section.get("client_credentials", "false") != "false":
            raise VerificationError("CLIENT_CREDENTIALS_FORBIDDEN")
        token = json.loads(section.get("token", ""))
        if not isinstance(token, dict) or any(
                not isinstance(token.get(key), str) or not token[key]
                for key in ("access_token", "refresh_token")):
            raise VerificationError("MANUAL_AUTH_REQUIRED")
    except (configparser.Error, UnicodeError, ValueError):
        raise VerificationError("INVALID_PRIVATE_CONFIGURATION") from None
    # Explicit executable/config and a minimal environment; no proxy, credential,
    # logging, remote-control or backend overrides are inherited.
    allowed_env = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
                   "LOCALAPPDATA", "APPDATA", "PATH"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed_env}
    environment["GOMEMLIMIT"] = "64MiB"  # Go soft heap target, not an OS sandbox.
    return config, executable, environment


def stop_child(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=1)


def stream_child(command: list[str], environment: dict[str, str], limit: int,
                 timeout: float, *, capture: bool = False) -> tuple[int, str, bytes]:
    """One owned child, a two-chunk queue, no content files or stderr capture."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise VerificationError("REMOTE_TIMEOUT")
    deadline = time.monotonic() + timeout
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, shell=False, bufsize=0,
                             env=environment, close_fds=True)
    chunks: queue.Queue = queue.Queue(maxsize=2)
    stopped = threading.Event()

    def send(chunk: bytes | None | VerificationError) -> None:
        while not stopped.is_set():
            try:
                chunks.put(chunk, timeout=0.05)
                return
            except queue.Full:
                pass

    def reader() -> None:
        try:
            read = 0
            while not stopped.is_set():
                chunk = child.stdout.read(min(CHUNK_BYTES, limit + 1 - read))
                if not chunk:
                    send(None)
                    return
                read += len(chunk)
                send(chunk)
                if read > limit:
                    return
        except (OSError, ValueError):
            send(VerificationError("REMOTE_STREAM_FAILED"))

    worker = threading.Thread(target=reader, daemon=True)
    digest = hashlib.sha256()
    total = 0
    collected = bytearray()
    try:
        worker.start()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VerificationError("REMOTE_TIMEOUT")
            try:
                chunk = chunks.get(timeout=remaining)
            except queue.Empty:
                raise VerificationError("REMOTE_TIMEOUT") from None
            if isinstance(chunk, VerificationError):
                raise chunk
            if chunk is None:
                break
            total += len(chunk)
            if total > limit:
                raise VerificationError("REMOTE_BYTE_LIMIT")
            digest.update(chunk)
            if capture:
                collected.extend(chunk)
        try:
            code = child.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise VerificationError("REMOTE_TIMEOUT") from None
        if code != 0:
            raise VerificationError("REMOTE_COMMAND_FAILED")
        if time.monotonic() > deadline:
            raise VerificationError("REMOTE_TIMEOUT")
        return total, digest.hexdigest(), bytes(collected)
    finally:
        stopped.set()
        stop_child(child)
        if worker.ident is not None:
            worker.join(timeout=1)
        child.stdout.close()


def verify_remote(path: Path, report: dict, config_raw: str | Path,
                  executable_raw: str | Path, remote: str, timeout: float,
                  *, cloud_copy: bool = False) -> dict:
    if not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise VerificationError("TIMEOUT_OUT_OF_RANGE")
    root = checked_path(backup.registered_onedrive(), directory=True)
    path = checked_path(path)
    relative = staged_relative(path, report["source_revision"])
    if not path.is_relative_to(root) or path.relative_to(root).as_posix() != relative:
        raise VerificationError("ARCHIVE_OUTSIDE_REGISTERED_SUBTREE")
    if cloud_copy:
        if SHA256.fullmatch(report["archive_sha256"]) is None:
            raise VerificationError("EXPECTED_SHA256_REQUIRED")
        relative = "/".join((*CLOUD_COPY_SUBTREE, report["archive_sha256"], path.name))
    config, executable, environment = private_remote(config_raw, executable_raw, remote, root)
    target = remote + ":" + relative
    common = ["--config", str(config), "--stats", "0", "--log-level", "ERROR",
              "--retries", "1", "--low-level-retries", "1", "--checkers", "1",
              "--transfers", "1", "--buffer-size", "0", "--max-buffer-memory", "1Mi",
              "--multi-thread-streams", "0", "--max-depth", "1", "--no-traverse",
              "--disable", "ListR", "--onedrive-list-chunk", "1"]
    deadline = time.monotonic() + timeout
    _, _, metadata = stream_child([str(executable), "lsjson", target, "--stat", *common],
                                  environment, MAX_STAT_BYTES, timeout, capture=True)
    try:
        item = json.loads(metadata)
        if (not isinstance(item, dict) or item.get("IsDir") is not False
                or type(item.get("Size")) is not int or item["Size"] != report["archive_bytes"]
                or item.get("Name") != path.name):
            raise VerificationError("REMOTE_FILE_METADATA_MISMATCH")
    except (ValueError, UnicodeError):
        raise VerificationError("REMOTE_FILE_METADATA_INVALID") from None
    size, checksum, _ = stream_child(
        [str(executable), "cat", target, "--count", str(report["archive_bytes"] + 1), *common],
        environment, report["archive_bytes"], deadline - time.monotonic())
    if size != report["archive_bytes"]:
        raise VerificationError("REMOTE_SIZE_MISMATCH")
    if checksum != report["archive_sha256"]:
        raise VerificationError("REMOTE_SHA256_MISMATCH")
    return {**report, "state": "REMOTE_CONTENT_VERIFIED", "remote_readback_verified": True,
            "remote_evidence_class": "MEASURED", "remote_bytes": size, "remote_sha256": checksum,
            "provider_path": relative, "target_choice": "CLOUD_COPY" if cloud_copy else "NATIVE"}


class SanitizedParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise VerificationError("INVALID_ARGUMENTS")


def main(argv: list[str] | None = None) -> int:
    parser = SanitizedParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--archive", required=True, help="Absolute path returned by backup staging")
    parser.add_argument("--expected-sha256", required=True, help="Explicit independently retained ZIP digest")
    parser.add_argument("--verify-remote", action="store_true", help="Opt in to exact-file cloud readback")
    parser.add_argument("--cloud-copy", action="store_true",
                        help="Select the fixed content-addressed CLI-upload copy prefix")
    parser.add_argument("--rclone-config", help="Private plaintext config outside repository and sync root")
    parser.add_argument("--rclone-executable", help="Absolute path to the user-supplied native rclone binary")
    parser.add_argument("--remote", help="Explicit configured OneDrive Personal remote name")
    parser.add_argument("--timeout", type=float, default=60, help="Remote deadline in seconds (1..300)")
    try:
        args = parser.parse_args(argv)
        supplied = (args.rclone_config, args.rclone_executable, args.remote)
        if (args.verify_remote and not all(supplied)) or (not args.verify_remote
                                                       and (any(supplied) or args.cloud_copy)):
            raise VerificationError("EXPLICIT_REMOTE_OPT_IN_REQUIRED")
        path, report = validate_local(args.archive, args.expected_sha256)
        if args.verify_remote:
            report = verify_remote(path, report, *supplied, args.timeout, cloud_copy=args.cloud_copy)
        print(json.dumps(report, sort_keys=True))
        return 0
    except VerificationError as error:
        reason = str(error)
    except (backup.BackupError, OSError, ValueError, zipfile.BadZipFile, zlib.error, EOFError, RuntimeError,
            subprocess.SubprocessError):
        reason = "LOCAL_OR_PROCESS_VALIDATION_FAILED"
    print(json.dumps({"state": "BLOCKED", "remote_evidence_class": "UNKNOWN",
                      "remote_readback_verified": False, "reason": reason}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
