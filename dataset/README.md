---
license: apache-2.0
language:
  - en
pretty_name: SZL Science Forum Insight Index (operator-authored pilot)
tags:
  - provenance
  - science-workflows
  - metadata-only
configs:
  - config_name: sources
    default: true
    data_files:
      - split: train
        path: sources.public.jsonl
  - config_name: needs
    data_files:
      - split: train
        path: needs.json
  - config_name: hypotheses
    data_files:
      - split: train
        path: hypotheses.json
  - config_name: second_brain_candidates
    data_files:
      - split: train
        path: second_brain.candidates.jsonl
---

<p><a href="https://huggingface.co/spaces/SZLHOLDINGS/szl-command-lab"><img src="https://raw.githubusercontent.com/szl-holdings/.github/main/profile/assets/szl/logos/szl_mark_holographic.svg" alt="SZL Holdings" width="112" /></a></p>

# Science Forum Pilot

Explore original summaries and metadata from two operator-authored topics used to formulate review hypotheses.

**Artifact:** Two-topic metadata pilot · **Stage:** Training unauthorized

[Explore in Command Lab](https://huggingface.co/spaces/SZLHOLDINGS/szl-command-lab) · [Build](https://github.com/szl-holdings/szl-science-forum-corpus) · [Evidence](https://github.com/szl-holdings/szl-science-forum-corpus/blob/e9cc13e07216ec1426fc29ad1ed7d8dc67b514bb/dataset/README.md)

## Before you use it

- This is not a forum scrape, representative sample, model-training dataset or scientific benchmark.
- Original annotations do not grant rights to linked forum posts; expansion requires separate access, reuse and privacy admission.

<details>
<summary>Technical details and original evidence</summary>

The retained source below is exact and may contain historical observations. Its dates, use restrictions, licenses and evidence boundaries continue to apply.

<!-- SZL-PRESERVED-TECHNICAL-BODY:START -->

# Dataset card: operator-authored pilot

This is a **two-topic, metadata-and-original-summary** pilot from the SZL operator's own [science-skills topic #426](https://ai4science.discourse.group/t/three-testable-science-skills-and-provenance-aware-github-imports/426) and [skill-sharing topic #396](https://ai4science.discourse.group/t/feature-request-easier-skill-sharing-in-claude-science-from-a-real-attempt/396/1). The operator supplied #396's text in conversation; the forum page was not independently accessible. It contains no copied forum post body, private message, user profile, or third-party member text. It is not a full forum scrape, representative sample, model-training dataset, or scientific benchmark.

`sources.public.jsonl` identifies the two operator topics and their manually assigned candidate needs. `needs.json` counts independent admitted topics, including the shared skill-import-provenance need across both. `hypotheses.json` lists proposed experiments with their evidence state. `graph.json` links sources, needs, and hypotheses. `second_brain.candidates.jsonl` is a staging projection; separately source-bound #396 and #426 frontier handles are live in Second Brain and Anatomy. The [two-topic runtime receipt](https://github.com/szl-holdings/szl-science-forum-corpus/blob/6c93512300967c4775c1d2028bac3ef10c3184c9/evidence/anatomy-publication-two-topics-2026-10-02.json) records that structural readback. Neither staging row enters the fixed 575-row retrieval corpus. `manifest.json` fixes the build date and hashes every generated file.

The Apache-2.0 license covers original SZL annotations and code only. It grants no rights to linked forum posts or external material. Expansion requires documented access, author/operator reuse permission, privacy review, and an exact source manifest. Model training is explicitly unauthorized in the manifest.

Source and methodology: [GitHub project](https://github.com/szl-holdings/szl-science-forum-corpus), [acquisition gate](https://github.com/szl-holdings/szl-science-forum-corpus/blob/main/docs/ACQUISITION_AND_RIGHTS.md), [research protocol](https://github.com/szl-holdings/szl-science-forum-corpus/blob/main/docs/RESEARCH_PROTOCOL.md).

<!-- SZL-PRESERVED-TECHNICAL-BODY:END -->

</details>
