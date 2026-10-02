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

# Dataset card: operator-authored pilot

This is a **one-topic, metadata-and-original-summary** pilot from the SZL operator's own [Claude Science Product Feedback topic](https://ai4science.discourse.group/t/three-testable-science-skills-and-provenance-aware-github-imports/426). It contains no copied forum post body, private message, user profile, or third-party member text. It is not a full forum scrape, representative sample, model-training dataset, or scientific benchmark.

The viewer has separate `sources`, `needs`, `hypotheses`, and `second_brain_candidates` configurations so unlike records are not mixed into one table. `sources.public.jsonl` identifies the operator's topic and records four manually assigned candidate needs. `needs.json` counts independent admitted topics, which is one here. `hypotheses.json` separates observed need IDs from proposed taxonomy IDs. `graph.json` links the source, needs, and hypotheses; proposed nodes have zero observed topics. `second_brain.candidates.jsonl` has a shape compatible with Second Brain corpus admission but has not been admitted to Second Brain or Anatomy. `manifest.json` fixes the build date, reviewed-input digests, and hashes every generated file. Provider viewer behavior still requires readback after the next governed publication.

The Apache-2.0 license covers original SZL annotations and code only. It grants no rights to linked forum posts or external material. Expansion requires documented access, author/operator reuse permission, privacy review, and an exact source manifest. Model training is explicitly unauthorized in the manifest.

Source and methodology: [GitHub project](https://github.com/szl-holdings/szl-science-forum-corpus), [acquisition gate](https://github.com/szl-holdings/szl-science-forum-corpus/blob/main/docs/ACQUISITION_AND_RIGHTS.md), [research protocol](https://github.com/szl-holdings/szl-science-forum-corpus/blob/main/docs/RESEARCH_PROTOCOL.md).
