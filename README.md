# SZL Science Forum Corpus

This repository is a **source-linked research index and reproducible analysis kit** for science-workflow needs. The reviewed GitHub projection contains two topics authored by the SZL operator: #426 and an original summary of #396 supplied by the operator in this conversation. The forum page for #396 was not independently accessible. This is **not** a scrape of the Claude Science forum, a representative sample, or permission to redistribute other members' posts.

The collection boundary is deliberate. The forum redirects anonymous readers to sign-in, and direct JSON topic/category requests returned HTTP 403 on 2026-10-02. We have no documented bulk export or reuse grant. Do not route around those controls, collect private messages, or publish third-party post bodies. An approved site export and explicit use rights are prerequisites for expanding the source index or using post text for retrieval, benchmarks, or training.

The Python standard-library build accepts reviewed source metadata in JSONL and emits only an approved public projection. It deduplicates by source ID, counts independent *topics* rather than posts, retains source links for review, and labels opportunities as exploratory. Unapproved/member-only records are counted as withheld but never copied into the public outputs.

```powershell
python -m unittest discover -s tests -v
python -m szl_forum_corpus validate examples/operator_topics.jsonl
python -m szl_forum_corpus build examples/operator_topics.jsonl --out build --as-of 2026-10-02
python -B -m scripts.verify_projection
```

An [offline export audit](docs/OFFLINE_EXPORT_AUDIT.md) is ready for a permitted
local export. It checks declared topic and post IDs, exact file hashes, truncated
hydration, duplicates and unresolved counters, and emits a receipt without post
text. Run its synthetic example with
`python -B -m szl_forum_corpus.export_audit examples/export-audit-synthetic --out build/synthetic-export-audit.json`.
This checks the declared set; it does not establish forum-wide coverage or reuse
rights. Real exports and their receipts stay in restricted storage outside Git
and Hub upload directories.

[Corpus automation](docs/CORPUS_AUTOMATION.md) publishes the reviewed projection
after owner-merged source changes and independently checks the seven GitHub/Hub
files after publication and daily. It preserves source/provider revision receipts,
returns nonzero for drift or unavailable evidence, and performs no forum scraping,
model training or automatic retrieval admission.

[Private OneDrive snapshots](docs/ONEDRIVE_BACKUPS.md) provide a bounded Windows
backup of immutable public source with per-file hashes and explicit client sync
states. The helper preserves originals and keeps local staging, client sync and
cloud restore as separate evidence.

A [bounded Killinchu dataset audit](docs/KILLINCHU_DATASET_AUDIT_2026-10-03.md)
connects these contracts to the public OSINT archive. It covers all 300 records
in five small snapshots and all 99 historical manifest entries, with explicit
gaps for the raw archive, viewer availability and row-level reuse rights.

The `build/` directory is ignored. The committed [dataset](dataset/) is the reviewable public projection; CI regenerates it byte-for-byte from the input and opportunities. Review its manifest, `sources.public.jsonl`, `needs.json`, `graph.json`, and `second_brain.candidates.jsonl` before any publication. The latter is a staging example using Second Brain's row shape; the separately admitted #396 and #426 forum frontier handles do **not** enter its fixed 575-row retrieval corpus. Two operator-authored topics cannot establish prevalence or product demand.

## Research method

1. Record the access method, reuse grant, collection time, source revision, and permitted purposes before ingesting anything. Store raw text only in a separately authorized restricted location; this project neither collects nor exports it.
2. Have two reviewers label a probability sample of independent threads for task, obstacle, workaround, consequence, and proposed intervention. Preserve disagreements and near-miss cases. Group cross-posts, edits, and linked papers to prevent duplicate evidence.
3. Freeze discovery, development, and held-out test sets by **thread family and time**. Keep related authors, papers, and paraphrases on one side of a split. Treat pretrained-model contamination as unknown unless independently assessed.
4. For each build, register a baseline, intervention, observable outcome, failure condition, and harm limit before opening the held-out set. A forum signal is a hypothesis, not proof of scientific value.
5. Compare workflows with blinded domain review and report uncertainty, abstentions, failure cases, cost, and latency. No model should be trained on member posts without a separate rights and privacy decision.

The three initial experiments are an experiment-contract skill, evidence adjudication, and reproduction triage. Their protocols are in [opportunities.json](opportunities.json), and the [skill gap map](docs/SKILL_GAP_MAP.md) distinguishes them from existing SZL science tools. The offline [`szl-experiment-contract`](https://github.com/szl-holdings/szl-skills/tree/372ff2d4fa72053151aeb77d3e1973090fdc07ab/skills/szl-experiment-contract) source skill is implemented; the other two remain proposals. None is an implemented Claude Science host registration or a validated scientific result.

## Publication boundary

- GitHub holds code, schemas, original annotations, operator-authorized metadata, and aggregate outputs after review.
- A Hugging Face dataset may mirror **only** that reviewed public projection through the repository's governed mirror workflow after its source PR is owner-merged. Reviewed source/data changes on `main` trigger publication; manual dispatch also supports an exact current-main SHA. The workflow binds immutable Git bytes and reads back every provider file byte. The independent alignment workflow runs after publication and daily. Check the provider revision and workflow result before treating it as published; source presence alone is not a release.
- The first [source-to-provider receipt](evidence/hf-publication-2026-10-02.json) covers only #426. The [two-topic readback receipt](evidence/hf-publication-two-topics-2026-10-02.json) binds merged GitHub source `330f519c8208eb2d6ba29492c778a0a40018195b` to public Hugging Face revision `76e90b85678b501d14090c2964f50c01907dfe1b`; all seven dataset files matched byte-for-byte. Neither version is a model-training dataset.
- The [automation readback](evidence/hf-automation-readback-2026-10-03.json) verifies all seven reviewed files at stable Hub revision `72964cdc1e325a39c0cb82737e795a23559ae088` against merged GitHub source `9ddef789493acf6e6f75189fa941748cc486d544`. The first automatic publisher and its independent audit succeeded after owner merge; the receipt links both runs. This still covers only the two approved topics.
- [Second Brain and Anatomy](docs/SECOND_BRAIN_AND_ANATOMY.md) expose both #396 and #426 as source-bound, review-required frontier handles. [Brain PR #31](https://github.com/szl-holdings/szl-second-brain/pull/31) is owner-merged, and the [two-topic live provider readback](evidence/anatomy-publication-two-topics-2026-10-02.json) records 575 retrieval chunks, 131 frontier candidates, and eight sources at Anatomy HF revision `12aa2a4152638b9cfd195f5b94c1f303657d78cd`. This remains `STRUCTURAL_ONLY`: no forum-wide ingest, retrieval-corpus admission, training, or measured research benefit. The [earlier one-topic receipt](evidence/anatomy-publication-2026-10-02.json) is retained as historical evidence.

The [Discourse API documentation](https://docs.discourse.org/) describes supported topic and category pagination. [W3C PROV](https://www.w3.org/TR/prov-o/) supplies the provenance vocabulary, and [Datasheets for Datasets](https://arxiv.org/abs/1803.09010) motivates explicit collection and intended-use documentation. Neither technical access nor robots.txt establishes a content license.

The Apache-2.0 [LICENSE](LICENSE) covers this repository's original code and documentation. It does **not** grant a license to any linked forum post or third-party dataset.
