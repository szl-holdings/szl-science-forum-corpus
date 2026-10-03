"""Back up immutable public source through the user's configured OneDrive client.

No source removal, hydration changes, credentials, sharing changes or provider
state setters. A local archive is not a remotely verified backup.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "szl-holdings/szl-science-forum-corpus"
MAX_FILES = 500
MAX_BYTES = 10 * 1024 * 1024
MIN_FREE_BYTES = 1_500_000_000
SHA = re.compile(r"^[0-9a-f]{40}$")
ORIGINS = {
    f"https://github.com/{REPOSITORY}.git",
    f"https://github.com/{REPOSITORY}",
    f"git@github.com:{REPOSITORY}.git",
}


class BackupError(RuntimeError):
    pass


def git(*args: str) -> bytes:
    result = subprocess.run(
        ["git", "--no-pager", "-C", str(ROOT), *args],
        capture_output=True, timeout=30,
    )
    if result.returncode:
        raise BackupError("Immutable Git source read failed")
    return result.stdout


def safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and str(path) == name and not path.is_absolute() and all(
        part.casefold() not in {"", ".", "..", ".git"} for part in path.parts
    ) and not any(char in name for char in "\\\x00\r\n:")


def snapshot() -> tuple[str, list[dict], dict[str, bytes]]:
    if git("remote", "get-url", "origin").decode().strip() not in ORIGINS:
        raise BackupError("Canonical public origin is required")
    revision = git("rev-parse", "origin/main").decode().strip()
    if SHA.fullmatch(revision) is None:
        raise BackupError("An exact source revision is required")
    entries: list[dict] = []
    bodies: dict[str, bytes] = {}
    total = 0
    for item in git("ls-tree", "-rlz", revision).split(b"\0"):
        if not item:
            continue
        metadata, raw_name = item.split(b"\t", 1)
        mode, kind, oid, raw_size = metadata.decode("ascii").split()
        name = raw_name.decode("utf-8")
        if kind != "blob" or mode not in {"100644", "100755"} or not safe_member(name):
            raise BackupError("Unsupported or unsafe tracked source entry")
        total += int(raw_size)
        if total > MAX_BYTES or len(entries) >= MAX_FILES:
            raise BackupError("Source snapshot exceeds the bounded backup scope")
        body = git("cat-file", "blob", oid)
        if len(body) != int(raw_size):
            raise BackupError("Git blob size changed during snapshot")
        bodies[name] = body
        entries.append({"path": name, "bytes": len(body), "git_blob_id": oid,
                        "sha256": hashlib.sha256(body).hexdigest()})
    if not entries or "dataset/manifest.json" not in bodies:
        raise BackupError("Reviewed corpus manifest is missing")
    if json.loads(bodies["dataset/manifest.json"]).get("model_training_authorized") is not False:
        raise BackupError("The reviewed training restriction is required")
    return revision, entries, bodies


def registered_onedrive() -> Path:
    if sys.platform != "win32":
        raise BackupError("The configured Windows OneDrive client is required")
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\OneDrive\Accounts\Personal") as key:
            root = Path(winreg.QueryValueEx(key, "UserFolder")[0]).resolve()
    except OSError as exc:
        raise BackupError("Personal OneDrive folder is not configured") from exc
    if not root.is_dir():
        raise BackupError("Configured OneDrive folder is unavailable")
    return root


def manifest_bytes(revision: str, entries: list[dict]) -> bytes:
    return (json.dumps({
        "schema": "szl.source-snapshot/v1", "source_repository": REPOSITORY,
        "source_revision": revision, "scope": "IMMUTABLE_TRACKED_PUBLIC_SOURCE_ONLY",
        "model_training_authorized": False, "files": entries,
    }, indent=2, sort_keys=True) + "\n").encode()


def verify_archive(path: Path, revision: str, entries: list[dict]) -> str:
    expected = {item["path"]: item for item in entries}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(expected) | {"BACKUP_MANIFEST.json"}:
            raise BackupError("Archive member inventory did not match source")
        if archive.comment != revision.encode() or archive.testzip() is not None:
            raise BackupError("Archive revision or integrity did not match")
        if archive.read("BACKUP_MANIFEST.json") != manifest_bytes(revision, entries):
            raise BackupError("Archive manifest did not match source")
        for name, item in expected.items():
            body = archive.read(name)
            if len(body) != item["bytes"] or hashlib.sha256(body).hexdigest() != item["sha256"]:
                raise BackupError("Archive bytes did not match immutable source")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage(root: Path, revision: str, entries: list[dict], bodies: dict[str, bytes]) -> dict:
    root = root.resolve()
    if (SHA.fullmatch(revision) is None or not entries
            or len(entries) != len({item["path"] for item in entries})
            or set(bodies) != {item["path"] for item in entries}):
        raise BackupError("Invalid snapshot binding")
    if any(not safe_member(name) for name in bodies) or "BACKUP_MANIFEST.json" in bodies:
        raise BackupError("Unsafe or reserved archive member")
    if len(entries) > MAX_FILES or sum(len(body) for body in bodies.values()) > MAX_BYTES:
        raise BackupError("Source snapshot exceeds the bounded backup scope")
    for item in entries:
        body = bodies[item["path"]]
        blob_id = hashlib.sha1(b"blob " + str(len(body)).encode("ascii") + b"\0" + body).hexdigest()
        if (item.get("bytes") != len(body) or item.get("git_blob_id") != blob_id
                or item.get("sha256") != hashlib.sha256(body).hexdigest()):
            raise BackupError("Source bytes did not match snapshot bindings")
    if shutil.disk_usage(root).free < MIN_FREE_BYTES + 2 * sum(len(body) for body in bodies.values()):
        raise BackupError("Insufficient local disk headroom")
    parent = root / "SZL-Corpus-Backups" / "szl-science-forum-corpus"
    if not parent.resolve().is_relative_to(root):
        raise BackupError("Backup parent escaped the configured OneDrive folder")
    parent.mkdir(parents=True, exist_ok=True)
    # Build and validate outside the sync folder; publish completed files only.
    with tempfile.TemporaryDirectory(prefix="szl-corpus-snapshot-") as temp:
        if not Path(temp).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise BackupError("Temporary archive escaped its owned staging directory")
        local = Path(temp) / f"szl-science-forum-corpus-{revision[:12]}.zip"
        with zipfile.ZipFile(local, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, body in bodies.items():
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, body)
            archive.writestr(zipfile.ZipInfo("BACKUP_MANIFEST.json", (2026, 1, 1, 0, 0, 0)),
                             manifest_bytes(revision, entries))
            archive.comment = revision.encode()
        checksum = verify_archive(local, revision, entries)
        directory = parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                              + "_" + revision[:12] + "_" + uuid.uuid4().hex[:8])
        if not directory.resolve().is_relative_to(root):
            raise BackupError("Backup destination escaped the configured OneDrive folder")
        directory.mkdir(exist_ok=False)
        target = directory / local.name
        pending = directory / (local.name + ".tmp")
        # A new destination is mandatory; existing backups are never replaced.
        with local.open("rb") as source, pending.open("xb") as destination:
            shutil.copyfileobj(source, destination)
        if verify_archive(pending, revision, entries) != checksum:
            raise BackupError("Staged archive did not match the verified snapshot")
        if target.exists():
            raise BackupError("Existing archive cannot be replaced")
        pending.rename(target)
        with (directory / (target.name + ".sha256")).open("x", encoding="ascii") as out:
            out.write(checksum + "  " + target.name + "\n")
    return {"schema": "szl.onedrive-source-backup/v1", "source_revision": revision,
            "source_files": len(entries), "source_bytes": sum(item["bytes"] for item in entries),
            "archive_bytes": target.stat().st_size, "archive_sha256": checksum,
            "archive_path": str(target), "destination_folder": str(directory),
            "source_files_removed": 0, "remote_readback_verified": False,
            "state": "LOCAL_ARCHIVE_VERIFIED_AWAITING_PROVIDER_SYNC"}


def client_state(path: Path) -> dict:
    if sys.platform != "win32":
        return {"state": "UNAVAILABLE", "in_sync": False, "reason": "WINDOWS_REQUIRED"}
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [("pin", ctypes.c_int), ("sync", ctypes.c_int),
                    ("file_id", ctypes.c_int64), ("root_id", ctypes.c_int64),
                    ("identity_length", wintypes.ULONG), ("identity", ctypes.c_ubyte * 1)]
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        api = ctypes.WinDLL("CldApi")
        api.CfGetPlaceholderInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        api.CfGetPlaceholderInfo.restype = ctypes.c_long
        handle = kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            return {"state": "UNAVAILABLE", "in_sync": False, "reason": "READ_ATTRIBUTES_FAILED"}
        try:
            buffer = ctypes.create_string_buffer(65536)
            returned = wintypes.DWORD()
            result = api.CfGetPlaceholderInfo(handle, 0, buffer, len(buffer), ctypes.byref(returned))
            if result != 0:
                return {"state": "NOT_CONFIRMED", "in_sync": False,
                        "hresult": f"0x{result & 0xffffffff:08x}"}
            if returned.value < ctypes.sizeof(Basic):
                return {"state": "UNAVAILABLE", "in_sync": False, "reason": "TRUNCATED_CLIENT_STATE"}
            info = ctypes.cast(buffer, ctypes.POINTER(Basic)).contents
            return {"state": "CLIENT_IN_SYNC" if info.sync == 1 else "CLIENT_NOT_IN_SYNC",
                    "in_sync": info.sync == 1, "pin_state": info.pin}
        finally:
            kernel.CloseHandle(handle)
    except (OSError, AttributeError):
        return {"state": "UNAVAILABLE", "in_sync": False, "reason": "CLIENT_QUERY_FAILED"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Stage a new private OneDrive source snapshot")
    parser.add_argument("--check", type=Path, help="Read native client state of an existing backup file")
    args = parser.parse_args()
    if args.check:
        root = registered_onedrive()
        target = args.check.resolve()
        if not target.is_relative_to(root / "SZL-Corpus-Backups" / "szl-science-forum-corpus"):
            raise BackupError("Client checks are restricted to this project's backup folder")
        state = client_state(target)
        print(json.dumps(state, sort_keys=True))
        return 0 if state.get("in_sync") else 2
    revision, entries, bodies = snapshot()
    if not args.apply:
        print(json.dumps({"state": "PLAN_ONLY", "source_revision": revision,
                          "source_files": len(entries), "source_bytes": sum(item["bytes"] for item in entries)}))
        return 0
    receipt = stage(registered_onedrive(), revision, entries, bodies)
    receipt["client_state"] = client_state(Path(receipt["archive_path"]))
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BackupError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
