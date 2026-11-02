# Corpus admission receipt

`szl_forum_corpus.admission` composes the repository's existing release and
research-split contracts into a content-free, deterministic receipt. It is an
offline precondition for model-training *candidate review*. It is not permission
to train and cannot start a job, publish a dataset, deploy a model, or write to a
provider.

The v1 policy requires all of the following:

- exact SHA-256 pins for the dataset manifest and, when supplied, the split
  manifest;
- independent reconstruction of `szl.forum-corpus-release/v1` from the public
  source projection;
- no duplicate exact Forge chat-input fingerprints;
- an explicit `model_training_authorized: true`, `model_training` in allowed
  purposes and outside excluded purposes, plus a record-scoped training-rights
  evidence digest;
- a complete revision-bound forum inventory and exact post revisions;
- at least 100 public records from 50 independent topics;
- a `SOURCE_METADATA` split audit with no connected-family or temporal
  conflict, at least 60 discovery, 20 development, and 20 held-out records;
- record-level binding between every split row and public release row; and
- the same exact split digest as a separately reviewed baseline.

These counts are a conservative operational floor that prevents a tiny pilot
from being treated as a training corpus. They do not establish statistical
power, representativeness, scientific value, or model quality. A project should
use a stronger task-specific design when one is justified.

Every required check is `OBSERVED`, `INVALID`, or `UNKNOWN`. `INVALID` takes
precedence over `UNKNOWN` in the overall result. Exact duplicate and declared
family/time leakage checks are enforced. Semantic/paraphrase duplication and
model pretraining contamination remain explicit, non-promoted `UNKNOWN` checks
until separate evidence exists.

The policy records two reference-only contracts:

- SZL Forge PR #673 at merge `972af446bc7dc6e46d397f2d0cbafb22057545b5`
  for canonical exact-input overlap; and
- ChaskaLLM PR #11 at merge `c8d08e1760923705a1e5d5e52dc6b12c60e1d128`
  for the fail-closed consumer boundary that requires exact corpus/base/split
  pins before training.

Neither repository is imported at runtime. Their exact source paths and SHA-256
digests are recorded in the policy so a reviewer can compare behavior without
turning a source reference into execution authority.

Run the committed pilot check from the repository root:

```powershell
python -B -m szl_forum_corpus.admission dataset `
  --manifest-sha256 6d086cee49703afc817baa58df4c0c59119518786be72628036f12054cc1ae00 `
  --out build/corpus-admission.json
```

The command writes the receipt once and returns exit code `3` because admission
is blocked. It returns `2` for invalid input/output handling and `0` only when
every required v1 check is observed. Existing output is never replaced.

For a real split proposal, supply all three split arguments:

```powershell
python -B -m szl_forum_corpus.admission dataset `
  --manifest-sha256 <dataset-manifest-sha256> `
  --split-manifest <research-splits.json> `
  --split-sha256 <exact-current-sha256> `
  --prior-split-sha256 <reviewed-baseline-sha256> `
  --out <new-receipt.json>
```

`szl-research-splits-v1` currently binds counts and thread multiplicity but has
no release-row digest field. Even an otherwise clean v1 split therefore leaves
record coverage `UNKNOWN`; a future split schema must add the binding rather
than infer it from row order or a similar-looking identifier.

The committed receipt in `evidence/corpus-admission-current-2026-11-02.json`
contains only hashes, aggregate counts, check states, policy, and boundary
flags. It contains no post body, title, summary, source ID, author identifier,
family key, private attribution, or model weight.
