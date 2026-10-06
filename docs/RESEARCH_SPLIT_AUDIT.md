# Check declared families before using a held-out set

The offline split auditor implements the family and time checks in the
[research protocol](RESEARCH_PROTOCOL.md). It examines supplied metadata only:
whole thread, author, paper and duplicate families must remain in one split,
and discovery must precede development, which must precede held-out evaluation.
It does not assign splits, move examples or adjust dates to make a check pass.

The implementation uses connected components. If A shares an author with B,
and B shares a paper with C, all three are one family even when A and C share
no direct key. This is a conservative dependency rule, not a measured estimate
of statistical independence. Group separation and temporal validation are
established evaluation practices; see the official
[scikit-learn cross-validation guidance](https://scikit-learn.org/stable/modules/cross_validation.html#cross-validation-iterators-for-grouped-data).
The implementation here uses only the Python standard library.

## Input and handling

Use the [versioned metadata schema](../schema/research-splits-v1.schema.json).
Each row supplies an opaque `record_id`, positive `thread_id`, one of
`discovery`, `development`, `held_out`, `event_time` in UTC with seconds,
and `author_hashes`, `paper_hashes`, `duplicate_hashes`.
Every field is required. Titles, post text, author names, emails, links and
arbitrary extra fields are rejected. Real inputs and receipts stay in restricted
storage outside Git clones and Hub upload directories. The source site and
source snapshot must be established separately; never combine unrelated sites
that reuse the same thread IDs in one manifest.

Family keys are opaque 64-character lowercase hexadecimal digests. Prepare
consistent keys across the entire declared snapshot; use a keyed digest for
author identities and keep the key and identity mapping in restricted storage.
Hashing alone does not anonymize a low-entropy identity. Paper and duplicate
families must be reviewed consistently; the auditor performs no identity
resolution, semantic similarity search or duplicate discovery itself.

For a family field, `null` means unknown; `[]` asserts no association of that
kind. Missing dates use `null`. Empty arrays are operator declarations, not
independent evidence of completeness. Use the post/task event date for
`event_time`, not the download date. Preserve unknown dates; do not invent them.
All three splits must be present. Their known time windows must be strictly
ordered: equal boundary timestamps require review and return `BLOCKED`.

Inputs are limited to 4,000,000 bytes, 10,000 rows and 32 keys per family kind
per row. The reader rejects nonregular files, observed replacement/mutation,
duplicate JSON keys, nonfinite values, repeated row IDs and invalid timestamps.
It reuses the normalized importer's Windows-aware file identity checks. Those
checks assume trusted cooperative storage; they are not a hostile-filesystem
sandbox or an atomic transaction against concurrent writers.

## Run the original synthetic examples

Create an output directory first, then choose new receipt filenames:

```powershell
New-Item -ItemType Directory -Force build | Out-Null
python -B -m szl_forum_corpus.split_audit examples/split-audit-clean-synthetic.json --out build/split-clean.json
python -B -m szl_forum_corpus.split_audit examples/split-audit-leaking-synthetic.json --out build/split-leaking.json
```

The clean fixture exits 0. The leaking fixture exits 3 and identifies a single
connected family across all three splits. Both have `input_kind: SIMULATED`;
their results establish software behavior, not a qualified research dataset.

| Exit | State | Meaning |
| --- | --- | --- |
| 0 | `MEASURED` | Supplied metadata satisfies the declared family and strict time checks. |
| 3 | `BLOCKED` | An observed family crosses splits or known time windows conflict. |
| 3 | `UNKNOWN` | No conflict was found, but a split, family metadata or date is missing. |
| 2 | No accepted receipt | Input/output is unavailable, invalid, changed or over budget. |

Known conflicts remain `BLOCKED` even if other metadata is unknown. Receipts
bind exact input bytes, the auditor and the shared reader by SHA-256. They
contain counts, split time windows and conflicting row ordinals (1-based in the
exact input), without row IDs, thread IDs, family keys or source payloads.
They are unsigned. Existing receipts are never overwritten.

## Interpretation boundary

Every run measures a structural metadata check. `metadata_completeness`,
`pretraining_contamination` and `rights_verification` remain `UNKNOWN`, and
`model_training_authorized` remains `false`. A result does not establish source
truth, unseen paraphrase separation, a representative sample, scientific benefit
or absence of model pretraining contamination. It cannot supply the missing
[forum access and reuse approval](ACQUISITION_AND_RIGHTS.md), admit sources to
Second Brain or Anatomy, publish data, or activate model training. There are no
network calls, model loads or tool-execution paths.
