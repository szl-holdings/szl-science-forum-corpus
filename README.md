# SZL Science Forum Corpus

This repository is a **source-linked research index and reproducible analysis kit** for science-workflow needs. The initial dataset contains one topic authored by the SZL operator. It is **not** a scrape of the Claude Science forum, a representative sample, or permission to redistribute other members' posts.

The collection boundary is deliberate. The forum redirects anonymous readers to sign-in, and direct JSON topic/category requests returned HTTP 403 on 2026-10-02. We have no documented bulk export or reuse grant. Do not route around those controls, collect private messages, or publish third-party post bodies. An approved site export and explicit use rights are prerequisites for expanding the source index or using post text for retrieval, benchmarks, or training.

The Python standard-library build accepts reviewed source metadata in JSONL and emits only an approved public projection. It deduplicates by source ID, counts independent *topics* rather than posts, retains source links for review, and labels opportunities as exploratory. Unapproved/member-only records are counted as withheld but never copied into the public outputs.

```powershell
python -m unittest discover -s tests -v
python -m szl_forum_corpus validate examples/operator_topics.jsonl
python -m szl_forum_corpus build examples/operator_topics.jsonl --out build --as-of 2026-10-02
python -B -m scripts.verify_projection
```

The `build/` directory is ignored. The committed [dataset](dataset/) is the reviewable public projection; CI regenerates it byte-for-byte from the input and opportunities. Review its manifest, `sources.public.jsonl`, `needs.json`, `graph.json`, and `second_brain.candidates.jsonl` before any publication. The latter uses Second Brain's row shape but is **not admitted** to its 575-row corpus. A build from one operator-authored topic cannot establish prevalence or product demand.

## Research method

1. Record the access method, reuse grant, collection time, source revision, and permitted purposes before ingesting anything. Store raw text only in a separately authorized restricted location; this project neither collects nor exports it.
2. Have two reviewers label a probability sample of independent threads for task, obstacle, workaround, consequence, and proposed intervention. Preserve disagreements and near-miss cases. Group cross-posts, edits, and linked papers to prevent duplicate evidence.
3. Freeze discovery, development, and held-out test sets by **thread family and time**. Keep related authors, papers, and paraphrases on one side of a split. Treat pretrained-model contamination as unknown unless independently assessed.
4. For each build, register a baseline, intervention, observable outcome, failure condition, and harm limit before opening the held-out set. A forum signal is a hypothesis, not proof of scientific value.
5. Compare workflows with blinded domain review and report uncertainty, abstentions, failure cases, cost, and latency. No model should be trained on member posts without a separate rights and privacy decision.

The three initial experiments are an experiment-contract skill, evidence adjudication, and reproduction triage. Their protocols are in [opportunities.json](opportunities.json), and the [skill gap map](docs/SKILL_GAP_MAP.md) distinguishes them from existing SZL science tools. They are proposals, not implemented Claude Science registrations or validated scientific results.

## Publication boundary

- GitHub holds code, schemas, original annotations, operator-authorized metadata, and aggregate outputs after review.
- A Hugging Face dataset may mirror **only** that reviewed public projection through the repository's governed, manual-dispatch mirror workflow after its source PR is merged. The workflow binds an exact main commit and reads back every provider file byte. Check the provider revision and workflow result before treating it as published; source presence alone is not a release.
- Second-brain and anatomy links are candidate provenance edges until their maintainers accept an integration PR and provider readback confirms a release.

The [Discourse API documentation](https://docs.discourse.org/) describes supported topic and category pagination. [W3C PROV](https://www.w3.org/TR/prov-o/) supplies the provenance vocabulary, and [Datasheets for Datasets](https://arxiv.org/abs/1803.09010) motivates explicit collection and intended-use documentation. Neither technical access nor robots.txt establishes a content license.

The Apache-2.0 [LICENSE](LICENSE) covers this repository's original code and documentation. It does **not** grant a license to any linked forum post or third-party dataset.
