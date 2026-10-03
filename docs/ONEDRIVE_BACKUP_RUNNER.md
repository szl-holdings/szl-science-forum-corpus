# Repeatable private source backup

`scripts.run_onedrive_backup` composes the existing bounded source snapshot and
cloud-content verifier. It runs one job; it does not install a schedule, fetch
Git, update its own code, acquire forum material, or delete source/backup files.
The selected snapshot remains limited to this repository's immutable public
`origin/main`, 500 regular files and 10 MiB. Models, raw exports, credentials and
ignored working files are outside this scope. Lambda is Conjecture 1 (OPEN).

## Preview and explicit apply

Fetch reviewed main separately and supply its exact 40-character SHA. Preview
reads only local immutable Git source; it does not discover OneDrive, open a
configuration or state folder, take a lock, write files or contact the provider.

```powershell
git fetch --no-tags origin main
$sourceSha = git rev-parse origin/main
py -3.12 -B -m scripts.run_onedrive_backup --expected-sha $sourceSha
```

Before apply, authenticate and bind the Personal drive to the intended native
account as described in [OneDrive backups](ONEDRIVE_BACKUPS.md). Provide a trusted
native rclone executable and its independently retained lowercase SHA-256. The
runner rehashes that executable before use. Config privacy, account binding and
Windows directory ACLs remain explicit operator prerequisites; OAuth permissions
are not folder-enforced. Do not run another config writer against this profile.

Create a private state directory outside every Git checkout and the OneDrive
root. The directory must already exist. On Windows, protect it with the same
current-user/SYSTEM ACL as the private configuration, including child files. On
POSIX it must be owned by the current user and have no group/other permissions.
Config values and tokens never enter state, receipts or command output.

```powershell
py -3.12 -B -m scripts.run_onedrive_backup --apply --expected-sha $sourceSha --state-dir '<private state directory>' --remote '<explicit Personal remote>' --rclone-config '<private config path>' --rclone-executable '<trusted native executable>' --expected-rclone-sha256 '<retained executable SHA-256>' --timeout 120
```

Apply checks remote Git `main` against that explicit SHA before staging, before
the copy request and after cloud readback. An unfetched or moving head blocks the
run. It does not automatically rebase, choose a new revision or execute newly
fetched code. A future scheduler should keep the approved runner code pinned,
fetch only source objects/refs separately, and derive the explicit source SHA
from that fetched `origin/main`. A source fetch must never turn into an automatic
checkout of new executable code.

## Reuse, concurrency and recovery

The configured state directory holds an OS file lock for the job's lifetime.
Another cooperating runner using that directory exits with `RUN_ALREADY_ACTIVE`;
process exit releases the lock without PID-based termination. This is a
per-directory cooperative lock, not a global provider lock against unrelated
clients. Use one state directory for this project's configured backup writer.

`state.json` identifies the most recently staged archive. An unchanged source
revision reuses that file only after local SHA-256, member inventory, manifest,
per-file bytes and path checks pass again. A changed revision stages a fresh
unique snapshot while retaining old snapshots. Malformed, duplicate-key,
oversized or unsupported state blocks the run; missing state permits an initial
snapshot. A missing or changed same-revision archive blocks rather than silently
creating a replacement.

Apply requests `rclone copyto --immutable --checksum` into only the fixed
`SZL-Verified-Corpus-Backups/szl-science-forum-corpus/<ZIP-SHA256>/<ZIP-filename>`
prefix. It does not write to the native client's snapshot folder. The copy uses
an argument array, a minimal environment, one checker/transfer, bounded bytes,
one retry and discarded CLI diagnostics. It then independently streams the
exact provider file through the existing verifier. Metadata or content mismatch,
failed copy, timeout or moved source produces `BLOCKED` and a nonzero exit.

The configurable 1–300 second provider budget is shared by copy and readback;
the default is 120 seconds. Separate immutable Git reads, source validation and
staging have their existing bounds. The job is not an OS resource sandbox.
Subprocess timeout handling affects only owned children.

## Local operation records

A private `receipts/<run-id>.json` record is written before staging and updated
before the copy request and after success or failure. It retains runner/verifier
byte hashes, source revision, ZIP hash/size, whether a snapshot was reused and
whether a cloud copy was requested. Writes use owned temporary files and atomic
replacement. `state.json` is also updated atomically, allowing a later attempt to
reuse a successfully staged file after a provider failure. Receipt directories
stop accepting new jobs at 1,000 entries; the runner never deletes old evidence.

Records are unsigned local operation evidence, not tamper-proof or independently
witnessed receipts. Success reports `REMOTE_CONTENT_VERIFIED` and a `MEASURED`
remote byte comparison. `PLAN_ONLY` and failure retain remote evidence `UNKNOWN`.
Native client sync stays `UNKNOWN`; full disaster recovery and training
authorization stay false. Copy and readback are separate provider operations.

The job can be invoked by a scheduler after the code, profile and schedule have
been reviewed. No scheduled task is installed by this repository change. Windows
task registration, a real trigger firing and a successful cloud comparison are
separate observations; a saved task definition alone is not a successful run.
The [Windows native task guide](WINDOWS_NATIVE_TASKS.md) covers AppData path
redirection, profile relocation, normal-user setup and fresh task-context evidence.

Offline checks require no cloud credentials or provider access:

```powershell
py -3.12 -B -m unittest discover -s tests -p test_run_onedrive_backup.py -v
```
