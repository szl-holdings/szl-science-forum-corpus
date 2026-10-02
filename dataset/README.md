---
license: apache-2.0
language:
  - en
pretty_name: SZL Science Forum Insight Index (operator-authored pilot)
tags:
  - provenance
  - science-workflows
  - metadata-only
---

# Dataset card: operator-authored pilot

This is a **one-topic, metadata-and-original-summary** pilot from the SZL operator's own [Claude Science Product Feedback topic](https://ai4science.discourse.group/t/three-testable-science-skills-and-provenance-aware-github-imports/426). It contains no copied forum post body, private message, user profile, or third-party member text. It is not a full forum scrape, representative sample, model-training dataset, or scientific benchmark.

`sources.public.jsonl` identifies the operator's topic and records four manually assigned candidate needs. `needs.json` counts independent admitted topics, which is one here. `hypotheses.json` lists proposed experiments with their evidence state. `graph.json` links the source, needs, and hypotheses. `second_brain.candidates.jsonl` has a shape compatible with Second Brain corpus admission but has not been admitted to Second Brain or Anatomy. `manifest.json` fixes the build date and hashes every generated file.

The Apache-2.0 license covers original SZL annotations and code only. It grants no rights to linked forum posts or external material. Expansion requires documented access, author/operator reuse permission, privacy review, and an exact source manifest. Model training is explicitly unauthorized in the manifest.

Source and methodology: [GitHub project](https://github.com/szl-holdings/szl-science-forum-corpus), [acquisition gate](https://github.com/szl-holdings/szl-science-forum-corpus/blob/main/docs/ACQUISITION_AND_RIGHTS.md), [research protocol](https://github.com/szl-holdings/szl-science-forum-corpus/blob/main/docs/RESEARCH_PROTOCOL.md).
