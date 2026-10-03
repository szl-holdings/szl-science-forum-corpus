# Reviewed corpus automation

Automation operates on the reviewed public projection. Forum acquisition and
rights review remain prerequisites under [the acquisition gate](ACQUISITION_AND_RIGHTS.md).
Raw exports never enter GitHub, Hub publication or model training through these
workflows. Lambda remains Conjecture 1 (OPEN).

## Owner merge to verified public mirror

1. A source PR updates approved annotations, opportunities or the dataset card.
   CI reruns offline tests and regenerates all six projection files exactly.
2. After the owner merges changes to `main`, the existing publisher runs
   automatically for the listed source/data paths. Manual dispatch remains
   available with an explicit expected main SHA.
3. The publisher binds all seven working files to immutable Git blobs, checks
   that its source is still remote `main`, and preserves the explicit write gate,
   reviewed-record checks, training prohibition, public-visibility check and
   single-writer concurrency group. It uses the provider parent commit to detect
   competing writes and reads back every published byte at the returned revision.
4. Completion of a main-branch publisher run triggers the independent alignment
   workflow. It reads the provider anonymously, pins one exact revision, verifies
   the expected file inventory and all seven byte hashes, then rechecks the head.

The automation does not merge PRs, change dataset visibility, delete extra Hub
files, restart Spaces, train a model or admit records to Second Brain's retrieval
corpus. Those actions have separate source and promotion boundaries. Main-branch
changes that do not affect the publisher's path filters do not create a new Hub
commit.

## Detect drift after publication

The alignment workflow also runs daily at 04:23 UTC and supports manual dispatch.
Schedules can be delayed by GitHub; this is not a continuous availability monitor.
PR runs perform offline contracts only and receive no provider credentials.
Alignment reads use the public Hub endpoints without tokens and are capped at
256 KiB per metadata response and 1 MiB per reviewed file, with a 30-second request
timeout and no automatic retry loop. A provider error stops the run rather than
being interpreted as an empty or healthy dataset.

| Receipt state | Result |
| --- | --- |
| `ALIGNED` | Exact reviewed bytes and inventory at one stable public provider head. |
| `DRIFT` | Missing/extra provider files or mismatched reviewed bytes; job exits nonzero. |
| `CONFLICT` | Provider head/inventory moved during the audit or a pinned revision disagreed; job exits nonzero. |
| `UNAVAILABLE` | Provider access, response validation, size limit or transport failure; job exits nonzero. |

The job preserves a JSON receipt as a GitHub Actions artifact for 14 days even
when the provider comparison fails. Receipts contain fixed filenames, commit
revisions and hashes; unknown provider paths are counted rather than echoed.
They contain no post bodies, credentials or private dataset contents. The code
has no provider write capability. Alignment is a structural comparison, not a
scientific-quality, representativeness or training-rights result.

Run the same audit locally from a clean source checkout:

```powershell
$sourceSha = git rev-parse HEAD
python -B -m scripts.audit_hf --expected-sha $sourceSha --out build/hf-alignment.json
```

The output file must be new. A local projection differing from its exact Git
commit is rejected before network access. For an acquired export, first use
[the offline export audit](OFFLINE_EXPORT_AUDIT.md), review independently authored
summary records, then submit the source PR. This pipeline does not fetch the forum
or convert other researchers' text into public records automatically.
