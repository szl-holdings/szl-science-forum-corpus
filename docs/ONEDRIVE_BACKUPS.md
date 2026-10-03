# Private OneDrive source snapshots

The Windows helper backs up this repository's immutable tracked public source
from its locally fetched `origin/main` to the personal OneDrive folder already
registered for the current user. It uses the existing sync client and does not
request or inspect tokens, change sharing, remove original files, or change
Files On-Demand/pinning settings. It is bounded to 500 regular files and 10 MiB.
Raw forum exports, model weights and credentials do not belong in this source
snapshot. An operational backup is separate from dataset acquisition, publication
rights, model training and scientific validation. Lambda remains Conjecture 1
(OPEN).

Fetch the approved source first, then preview and apply:

```powershell
git fetch --no-tags origin main
python -B -m scripts.backup_onedrive
python -B -m scripts.backup_onedrive --apply
```

Preview performs no cloud-folder discovery or writes. Apply creates a fresh
folder under `OneDrive/SZL-Corpus-Backups/szl-science-forum-corpus/`. It never
replaces an existing backup. The ZIP contains all regular tracked source files,
an exact Git revision, per-file SHA-256/Git blob bindings and a backup manifest.
It is checked before staging and again after copying. A checksum sidecar is also
staged. Source clones, dirty edits, `.git`, ignored build outputs and files outside
the repository are not collected. Temporary archive preparation is confined to
an owned temporary directory.

The ZIP is a source snapshot rather than a complete Git history or a cloud API
receipt. The tool returns `LOCAL_ARCHIVE_VERIFIED_AWAITING_PROVIDER_SYNC` after
staging, even if the archive's native client query reports in-sync. A sync report
is client evidence, not independent remote content verification. A filesystem
copy or a running OneDrive process alone does not establish cloud upload.

Check the archive path returned by apply:

```powershell
python -B -m scripts.backup_onedrive --check '<returned archive path>'
```

The check is restricted to this project's backup folder. It uses Microsoft's
read-only [CfGetPlaceholderInfo](https://learn.microsoft.com/en-us/windows/win32/api/cfapi/nf-cfapi-cfgetplaceholderinfo)
API with READ_ATTRIBUTES access; it does not set sync state or disclose provider
identity blobs. Missing placeholders, transport failures and unsupported platforms
remain not-confirmed/unavailable and exit nonzero. `CLIENT_IN_SYNC` describes the
provider's native placeholder state. A separate restore from the cloud, followed
by byte/hash comparison, supplies independent content evidence. Native sync is
not an independent remote hash; a remote hash is not full disaster recovery.

## Bounded independent content verification

`scripts.verify_onedrive_backup` uses only the Python standard library. By
default it returns `PLAN_ONLY`: it checks a selected, already staged, locally
resident ZIP against `backup.snapshot()` at the local canonical `origin/main`
and an explicit lowercase SHA-256 retained from staging. It does not fetch Git,
discover OneDrive, open a private config, launch rclone, query a provider or write
an archive. The ZIP inventory, manifest, revision, regular member types, declared
sizes and decompressed hashes must match that snapshot. The staged filename and
unique-folder revision must also match. If local `origin/main` has moved since
staging, the older ZIP fails this check; this verifier does not choose a revision.

```powershell
python -B -m scripts.verify_onedrive_backup --archive '<absolute staged ZIP path>' --expected-sha256 '<64 lowercase hex digest>'
```

For remote verification, **manual authentication is a prerequisite**. Separately
install a trusted native rclone executable and configure the intended OneDrive
Personal account before invoking the verifier. The verifier never installs,
authenticates, discovers remotes or uploads. Pass the absolute executable path,
an explicit simple remote name (letter followed by letters/digits/underscores/
hyphens, up to 64 characters; for example `szl-personal`), and a private plaintext
config stored outside all Git checkouts and the registered OneDrive sync root.
A `.git` file or directory in any config ancestor is rejected, including other
repositories and worktrees. Protect the config directory with user-only
permissions/Windows ACLs, including token-refresh temporary files and backups.
Credentials and token/config files must never be committed or placed in a sync
folder. POSIX file permissions are checked; Windows ACL privacy remains the
operator's prerequisite. Encrypted configs, inherited defaults, external token
files, endpoint/root overrides and wrapper backends are rejected. The selected
section must explicitly declare `type = onedrive`, `drive_type = personal`, a
drive ID and an existing OAuth token. `client_credentials` may be absent (the
default is false) or explicitly `false`; true and other values are rejected.
This is a local configuration check, not
an independent attestation of account identity. The operator must bind that
configured drive to the intended registered native account.

```powershell
python -B -m scripts.verify_onedrive_backup --archive '<absolute staged ZIP path>' --expected-sha256 '<64 lowercase hex digest>' --verify-remote --remote szl-personal --rclone-config '<absolute private config path>' --rclone-executable '<absolute rclone.exe path>' --timeout 60
```

The default provider target is exactly the ZIP's path relative to the registered
Personal root:
`SZL-Corpus-Backups/szl-science-forum-corpus/<unique-folder>/<validated ZIP filename>`.
The source must be directly in this staged subtree; traversal, symlinks,
junctions, nonresident placeholders and paths outside it are rejected. No
arbitrary provider path is accepted. An independently uploaded CLI copy may
instead be verified by adding **`--cloud-copy`** to the opt-in command. That
selects exactly
`SZL-Verified-Corpus-Backups/szl-science-forum-corpus/<expected-sha256>/<validated ZIP filename>`.
The hash leaf is the explicit validated 64-character lowercase digest. This
separate fixed prefix avoids a second uploader writing to the native snapshot;
the verifier performs no upload for either target. `--cloud-copy` requires
`--verify-remote` and does not accept a path argument.

Only two read commands are allowed: bounded `lsjson <exact file> --stat` to
reject a directory or size mismatch, then
[`rclone cat <exact file> --count <archive_bytes+1>`](https://rclone.org/commands/rclone_cat/).
Argument arrays use no shell. The stream must end at exactly the expected byte
count, match the ZIP SHA-256 and exit successfully. Both calls share a deadline
of 60 seconds by default, selectable from 1 through 300 seconds. A limit, failure
or timeout terminates and, if needed, kills/reaps only the owned child. Child
stdout is consumed privately; stderr is discarded, and errors expose only fixed
reason codes. Config values, tokens and raw CLI output are never printed. Every
`RCLONE_*` environment variable is rejected before launching the executable,
and proxy/credential/logging settings are not inherited. There are no upload,
sync, deletion or hydration commands, downloaded content files or persistent
report files. rclone may refresh its OAuth token in the private config; remote
file content is never modified by this verifier.

The ZIP limit is 11 MiB (the helper's 10 MiB source bound plus archive overhead),
config input is limited to 64 KiB and stat output to 16 KiB. Stream reads are at
most 64 KiB with a two-chunk queue; archive bytes are hashed without accumulating
them or writing them to disk. rclone gets zero read-ahead buffering, a 1 MiB
transfer-buffer limit, one checker/transfer, no parallel transfer streams,
bounded listing depth and no recursive `ListR`. `GOMEMLIMIT=64MiB` supplies a Go
soft heap target; these settings are not an OS memory sandbox for an arbitrary
executable. The supplied executable and private configuration are trusted manual
prerequisites. The config can be rewritten by token refresh, so do not run a
concurrent config writer. Remote metadata and content reads are separate calls,
not an atomic or version-pinned provider snapshot.

Successful byte verification reports `remote_evidence_class: MEASURED` and
`REMOTE_CONTENT_VERIFIED` for the selected object. Failure is `BLOCKED` with
remote evidence `UNKNOWN`. `PLAN_ONLY` measures only local source integrity;
native client sync remains `UNKNOWN` in this separate report. OAuth permissions
are **not folder-enforced**: the fixed prefixes bound this program's requests,
not the token's wider capabilities. A content hash is not a signed receipt,
authorization, retention/availability guarantee, full recovery exercise, model
evaluation or scientific result. This backup conveys no all-forum acquisition,
redistribution or training authorization. Lambda remains Conjecture 1 (OPEN).

Offline validation:

```powershell
python -B -m unittest discover -s tests -v
```

The verifier tests use synthetic ZIP/config fixtures and process doubles, plus
local Python children to exercise actual pipe bounds and termination. They never
contact OneDrive and establish no cloud backup or recovery proof.

Microsoft documents [Files On-Demand states](https://learn.microsoft.com/en-us/sharepoint/files-on-demand-windows)
and [online-only storage](https://support.microsoft.com/en-us/onedrive/save-disk-space-with-onedrive-files-on-demand-for-windows).
Only a confirmed cloud backup should be considered for online-only retention;
this helper never dehydrates files or deletes originals. Do not put a live dirty
Git clone under OneDrive or run two writers against the same snapshot folder.

This is an on-demand helper. It does not install a scheduled task or promise
continuous backup. The reviewed GitHub-to-Hugging-Face automation remains
described separately in [CORPUS_AUTOMATION.md](CORPUS_AUTOMATION.md).

The [repeatable backup runner](ONEDRIVE_BACKUP_RUNNER.md) combines explicit source
approval, validated snapshot reuse, a cooperative writer lock, immutable cloud
copy and bounded independent readback. It also leaves schedule installation as
a separate operation.
