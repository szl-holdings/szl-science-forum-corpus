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
by byte/hash comparison, is required before claiming remote recovery was verified.

Microsoft documents [Files On-Demand states](https://learn.microsoft.com/en-us/sharepoint/files-on-demand-windows)
and [online-only storage](https://support.microsoft.com/en-us/onedrive/save-disk-space-with-onedrive-files-on-demand-for-windows).
Only a confirmed cloud backup should be considered for online-only retention;
this helper never dehydrates files or deletes originals. Do not put a live dirty
Git clone under OneDrive or run two writers against the same snapshot folder.

This is an on-demand helper. It does not install a scheduled task or promise
continuous backup. The reviewed GitHub-to-Hugging-Face automation remains
described separately in [CORPUS_AUTOMATION.md](CORPUS_AUTOMATION.md).
