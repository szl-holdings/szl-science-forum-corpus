# Source-bound handoff to Second Brain and Living Anatomy

This is a candidate integration contract, **not a deployed link**.

At the 2026-10-02 readback, [`szl-second-brain`](https://github.com/szl-holdings/szl-second-brain/tree/364bd1efc4354d0fb05730c9147333bedfbb80dd) was at signed Git commit `364bd1efc4354d0fb05730c9147333bedfbb80dd`. Its public projection still had 575 rows and [`data/manifest.json`](https://github.com/szl-holdings/szl-second-brain/blob/364bd1efc4354d0fb05730c9147333bedfbb80dd/data/manifest.json) listed only `doc`, `formula`, `ingest`, and `invariant` sources. The Hugging Face copy, [`SZLHOLDINGS/szl-second-brain-inrepo`](https://huggingface.co/datasets/SZLHOLDINGS/szl-second-brain-inrepo), was at revision `c9823ec107fc1fd4df6166c4ad0a37c776ee7e64`. A byte comparison of its corpus and manifest with the earlier GitHub source passed at that revision; the subsequent Second Brain change affected retrieval handles, not corpus bytes. This dataset is retrieval data, not a trained model.

[`anatomy`](https://github.com/szl-holdings/anatomy/tree/a078ebc15e586cbb9e4e7cfb063fc715456ba9ee) was at Git commit `a078ebc15e586cbb9e4e7cfb063fc715456ba9ee`. Its [Second Brain materializer](https://github.com/szl-holdings/anatomy/blob/a078ebc15e586cbb9e4e7cfb063fc715456ba9ee/scripts/materialize_second_brain.py) pins an exact source revision and expects the existing 575-row public projection. Anatomy reports a structural, read-only binding; that is not scientific validation or authority to train or publish.

This kit emits `second_brain.candidates.jsonl` using the current row shape: `id`, `source`, `sourceId`, `title`, `text`, and the SHA-256 of `text`. `source` is `forum_insight`, `sourceId` is the canonical post URL, and `text` is an independently authored summary, never the forum post body. Extra `rightsEvidence` and `reviewState` fields document why the row is eligible. Its presence here is **not admission** to Second Brain.

To make the link operational after rights review:

1. Review every candidate, source link, attribution, and right-to-publish record. Admit only approved rows through a signed Second Brain PR. Update its source validation, generated corpus files, manifest counts/digests, tests, and package contract together. `SecondBrainIndex.handle()` now retains `sourceId` on public retrieval handles, but no forum candidate has been admitted.
2. Merge the Second Brain source PR through its protected process. Let the repository's governed Hugging Face mirror publish the exact dataset projection. Read back the dataset commit and compare corpus and manifest bytes before saying it is published.
3. Update Anatomy's fixed row count and source pin through a separate PR. Anatomy already retains `sourceId` on retrieval handles; verify that the newly admitted forum citation survives that projection. Run its materializer and tests, then read back its source binding and Brain health on the deployed Space. Report `STRUCTURAL_ONLY` until independent scientific evaluation exists.

Do not change the live 575-row count or claim a forum-backed release based on this staging kit. Lambda remains **Conjecture 1 (OPEN)**.
