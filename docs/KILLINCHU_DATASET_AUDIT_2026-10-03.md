# Killinchu corpus audit: 2026-10-03 UTC

This is a bounded audit of the public
[Killinchu OSINT dataset](https://huggingface.co/datasets/SZLHOLDINGS/killinchu-osint-corpus/tree/b6f798c7ccd390888ed7d2c547f20d4812c7dadc)
and its canonical GitHub publisher. It contains aggregate findings and source
bindings, without copying article bodies or individual research records into
the forum corpus. It does not admit these records to Second Brain, authorize
model training, or establish operational intelligence quality. Lambda remains
Conjecture 1 (OPEN).

## Immutable scope and coverage

| Evidence | Observed scope |
| --- | --- |
| Canonical source | `szl-holdings/killinchu@0cad32a7434601262f2d948bab7f2ba66d6e08c2`; signature verified by GitHub during collection; 1,417 tree entries, not truncated. |
| Dataset snapshot | Public, ungated `SZLHOLDINGS/killinchu-osint-corpus@b6f798c7ccd390888ed7d2c547f20d4812c7dadc`; 115 files, 531,514,810 bytes in metadata. |
| Fully inspected content | Five small corpus snapshots, 308,011 bytes, 60 records each: advisories, counter-UAS, geopolitical, naval and procurement. All 300 records were read at the pinned revision and their files matched the advertised Git blob IDs. |
| Manifest | 34,040 bytes, all 99 entries inspected; sizes and Git blob IDs checked against its declared historical archive revision. |
| Uninspected raw content | 99 daily archive shards, 531,114,077 bytes. No full shard download, independent raw row count, raw-record privacy scan, or raw duplicate analysis. |
| Writer-maintained count | `intel/head.json` reported 693,794 records. This is a writer claim, not an independently counted total. |

The provider head advanced during collection to
`432eeaab8bb9363da595857a2c4ef86d307984e8`. These findings describe the pinned
snapshot above, not the latest head. The GitHub source head stayed stable during
the audit. A namespace listing found 45 datasets; only this dataset directly
matched the Killinchu/OSINT/intel name or tag filter. That filter does not establish
that all other datasets are unrelated.

## Cleaning findings from the 300 inspected records

| Check | Result and interpretation |
| --- | --- |
| Record structure | 13 item fields present in all 300 records; only `published` was nullable. All 300 IDs were unique and 16 characters long; no exact duplicate rows were found. |
| Source URLs | 217 distinct URLs across 108 hosts; 83 occurrences reused an earlier URL. All URLs used HTTPS and their host fields matched after normalizing the `www` prefix. Repeated URLs can represent refreshes or distinct annotations; they are not automatically duplicate articles or independent corroboration. |
| Ingestion dates | All 300 `ingest_ts` values parsed with a timezone, with no future value at collection. Their range was 2026-09-02 through 2026-10-03 UTC. |
| Publication dates | 113 were null. The remaining 187 strings did not parse as ISO timestamps. This does not establish that the dates are invalid: feed formats may use other conventions. Preserve the original string and add a normalized UTC value plus explicit parse status. |
| Freshness markers | 29 records were marked live and 271 cached. The snapshots' save time does not make every item newly collected. |
| Reuse evidence | None of the 300 records contained a row-level license or rights field. A source URL and a public repository are insufficient evidence of training rights. |

A cleaning implementation should preserve the original values, distinguish
article identity from observation identity, and group related URLs and sources
when forming evaluation splits. It should never silently replace unparseable
dates with the current time or count repeat observations as independent evidence.
These transformations are recommendations; this audit did not rewrite the Hub
data.

## Archive manifest and Dataset Viewer

All 99 manifest paths were unique, with a homogeneous eight-field schema and no
missing, null or blank fields. Every entry declared `training_eligible: false`
and `rights_status: MIXED_SOURCE_ROW_LEVEL_RIGHTS_NOT_ESTABLISHED`.

The manifest's archive binding is
`c3291de51b18ef124465feaf70b4830096510747`, and all 99 sizes and blob IDs matched
that revision. At the audited dataset revision, `intel/2026-10-03.ndjson` had
grown from the manifest's 352,020 bytes to 503,816 bytes with a different blob ID.
The historical binding was valid; it was not a fresh inventory of the moving
archive head. The existing `RELEASE_AUDIT.json` likewise described an earlier
28-file, 118,977,343-byte release rather than the current 115-file snapshot.

The Dataset Viewer returned an `archive_manifest/train` split at the audited
revision. Its validity, row and statistics endpoints returned HTTP 500; size and
Parquet endpoints returned HTTP 501 and named a different revision,
`2505d63f7a8d921dcb38c2fd6f3bf110a2174382`. Viewer usability, Parquet availability
and viewer-derived row totals remain **NOT_VERIFIED**. An endpoint error must not
be interpreted as an empty dataset or a passed quality gate.

## Canonical publication and repair targets

The dataset card matched the canonical template after its documented prefix,
schema and cell substitutions. `LICENSE.md` matched canonical Git blob
`93c049f996bc834e1e7f1b5dd8ee98d58ff18032`. The card and manifest retained the
mixed-source, privacy, factual-validation and training restrictions. The
[canonical dataset source](https://github.com/szl-holdings/killinchu/tree/0cad32a7434601262f2d948bab7f2ba66d6e08c2/datasets/killinchu-osint-corpus)
remains authoritative.

At this source revision, two concurrent-write risks need separate repairs:

1. The archive card publisher prepares derived metadata and calls
   `create_commit` without `parent_commit`. Pin its metadata reads to one exact
   provider revision and use that revision as the expected parent. Reject a
   competing head with a sanitized conflict receipt and no automatic retry.
   This publisher does not directly overwrite the raw archive shards.
2. The shared `szl_hf_bucket.py` shard path performs read/merge/replace without an
   expected parent. Concurrent runtime writers could lose updates. Coordinate
   any repair across the byte-guarded sibling repositories; do not change that
   shared module in Killinchu alone. Actions concurrency does not serialize a
   separate runtime flusher.

The publisher and shared bucket findings are source-level risks, not evidence of
an observed data-loss incident. A publisher repair must retain the exact current
GitHub-main gate, immutable readback, rights restrictions and ambiguous-outcome
handling. Regression coverage should include a moving provider head, mismatched
metadata revisions, expected-parent transmission, HTTP 409/412 rejection, and
an unknown outcome after a server failure.

The weekly/manual knowledge autosync validates before proposing a source PR. It
is separate from the HF archive publisher. Its commit signing, exact A11oy source
binding and actual PR-head CI should be reviewed independently. The classic
Actions-settings API returned 403 during collection, so that control was
**UNKNOWN** rather than inferred safe.

## How this connects to the science corpus

Keep this aggregate audit as evidence for data engineering work. Use the same
source-revision, rights, explicit-coverage and immutable-readback contracts as
the [reviewed forum corpus automation](CORPUS_AUTOMATION.md). A future permitted
forum export can pass the [offline export checker](OFFLINE_EXPORT_AUDIT.md) before
reviewed annotations are proposed. Killinchu records and other researchers'
forum text remain outside automatic retrieval admission and model training.

The full raw archive audit, date normalization, source-family split validation,
shared shard-writer repair, and measured research benefit remain open work. The
observations here must be refreshed against an exact current revision before a
release or scientific claim.
