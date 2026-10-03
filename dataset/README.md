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

This is a **two-topic, metadata-and-original-summary** pilot from the SZL operator's own [science-skills topic #426](https://ai4science.discourse.group/t/three-testable-science-skills-and-provenance-aware-github-imports/426) and [skill-sharing topic #396](https://ai4science.discourse.group/t/feature-request-easier-skill-sharing-in-claude-science-from-a-real-attempt/396/1). The operator supplied #396's text in conversation; the forum page was not independently accessible. It contains no copied forum post body, private message, user profile, or third-party member text. It is not a full forum scrape, representative sample, model-training dataset, or scientific benchmark.

`sources.public.jsonl` identifies the two operator topics and their manually assigned candidate needs. `needs.json` counts independent admitted topics, including the shared skill-import-provenance need across both. `hypotheses.json` lists proposed experiments with their evidence state. `graph.json` links sources, needs, and hypotheses. `second_brain.candidates.jsonl` is a staging projection; a separately bound #426 frontier handle is live in Second Brain and Anatomy, while #396 is not. Neither staging row enters the fixed 575-row retrieval corpus. `manifest.json` fixes the build date and hashes every generated file.

The Apache-2.0 license covers original SZL annotations and code only. It grants no rights to linked forum posts or external material. Expansion requires documented access, author/operator reuse permission, privacy review, and an exact source manifest. Model training is explicitly unauthorized in the manifest.

Source and methodology: [GitHub project](https://github.com/szl-holdings/szl-science-forum-corpus), [acquisition gate](https://github.com/szl-holdings/szl-science-forum-corpus/blob/main/docs/ACQUISITION_AND_RIGHTS.md), [research protocol](https://github.com/szl-holdings/szl-science-forum-corpus/blob/main/docs/RESEARCH_PROTOCOL.md).
