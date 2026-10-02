# Source-bound handoff to Second Brain and Living Anatomy

The forum pilot has a published dataset, a review-required Second Brain frontier candidate, and a source-bound live Anatomy projection. This is a structural research link, not an evaluated scientific result. The authorized input remains the operator's one public topic; no other forum threads or post bodies were admitted and no model was trained.

## Exact source and publication

- The operator-authored source record is [`dataset/sources.public.jsonl`](https://github.com/szl-holdings/szl-science-forum-corpus/blob/ac85ddde85c1ae494803c2b16421688c6d3fe7de/dataset/sources.public.jsonl) at Git commit `ac85ddde85c1ae494803c2b16421688c6d3fe7de`, SHA-256 `3edf511b4d021c7eb8286035c8fa444031abe6eb04f06e8718dc0bf29bfba0f9`.
- The [Hugging Face corpus](https://huggingface.co/datasets/SZLHOLDINGS/szl-science-forum-corpus) is at revision `d8f8fec38361d988fddebec0be7898138af15275`. Its seven published source files were read back byte-for-byte against GitHub source commit `f7359cfbaeb98e724fdf0b7cbcb501b42e5e685e`; the [publication receipt](../evidence/hf-publication-2026-10-02.json) records this boundary.
- The corpus contains one source topic. It carries no forum post body, third-party reply, private message, or model weight. Its `model_training_authorized` value is `false`.

## Second Brain frontier admission

Owner-merged [`szl-second-brain` PR #28](https://github.com/szl-holdings/szl-second-brain/pull/28), merged main `4f45d201be0b3334c26cf9cb5fe7161f65a229d7`, binds the source record above as a fixed eighth public frontier source. A strict parser accepts only the declared operator-owned topic, exact rights and review fields, and four need IDs. It emits one independently authored `forum-insight` candidate with `DISCOVERED_REVIEW_REQUIRED` state. The previous 129 frontier candidates remain byte-identical; the released source has 130 candidates, eight sources, and candidate-set SHA-256 `85d3f0d422d1bd0988e2a7d83d4823db7e043a3e7454c3453dba129c27e5414a`.

This is the **separate frontier candidate plane**. The fixed 575-row retrieval corpus, its NeoMME/model locks, and the [existing Hugging Face retrieval dataset](https://huggingface.co/datasets/SZLHOLDINGS/szl-second-brain-inrepo) are unchanged by this PR. The old `dataset/second_brain.candidates.jsonl` file is a staging example; it is not a live retrieval-corpus admission. The frontier candidate remains review-required, handles-only in public APIs, and without training, promotion, execution, or merge authority.

## Anatomy compatibility and release gate

At Anatomy Git commit [`a078ebc15e586cbb9e4e7cfb063fc715456ba9ee`](https://github.com/szl-holdings/anatomy/tree/a078ebc15e586cbb9e4e7cfb063fc715456ba9ee), the existing [materializer](https://github.com/szl-holdings/anatomy/blob/a078ebc15e586cbb9e4e7cfb063fc715456ba9ee/scripts/materialize_second_brain.py) resolves an exact Second Brain Git revision and validates each source binding, row digest, candidate-set digest, and authority constraint. It already accepts additional sources above its seven-source minimum; a new Anatomy code PR is not needed for this source.

On 2026-10-02, local materialization from exact PR head `577b2a9804f0f3eeff3b850bf6c43da294e9babe` passed. All 127 Anatomy unit tests passed against that snapshot. `PublicSecondBrain.health()` reported ready, 575 retrieval chunks, eight frontier sources, and 130 candidates. A source-filtered forum query returned one handle at source revision `ac85ddde85c1ae494803c2b16421688c6d3fe7de` with `HANDLES_ONLY`, `DISCOVERED_REVIEW_REQUIRED`, and no content field. These are **local compatibility results**, not a provider deployment or scientific validation.

## Live deployment readback

[Anatomy sync run 37075907043](https://github.com/szl-holdings/anatomy/actions/runs/37075907043) completed successfully from Anatomy main `a078ebc15e586cbb9e4e7cfb063fc715456ba9ee`. Its [Hugging Face Space](https://huggingface.co/spaces/betterwithage/anatomy) and live source receipt both reported deployment revision `e72da1584406e068c99dc7701725a89eb4261962`. The provider was public and `RUNNING` at the 2026-10-02 readback.

The independent [Brain health](https://betterwithage-anatomy.hf.space/api/anatomy/v1/brain/health) readback reported merged Brain revision `4f45d201be0b3334c26cf9cb5fe7161f65a229d7`, 575 retrieval chunks, eight frontier sources, 130 candidates, and candidate-set digest `85d3f0d422d1bd0988e2a7d83d4823db7e043a3e7454c3453dba129c27e5414a`. A [source-filtered frontier query](https://betterwithage-anatomy.hf.space/api/anatomy/v1/brain/frontier?q=science%20forum&repository=szl-holdings%2Fszl-science-forum-corpus&k=5) returned exactly one forum handle at source commit `ac85ddde85c1ae494803c2b16421688c6d3fe7de`, with `HANDLES_ONLY` and no content field. Direct access to the internal frontier candidate file returned HTTP 404. The [readback receipt](../evidence/anatomy-publication-2026-10-02.json) binds these observations.

Retain `STRUCTURAL_ONLY` until an independent, rights-cleared research evaluation shows benefit. The one-topic pilot cannot estimate community prevalence or justify model training. Lambda remains **Conjecture 1 (OPEN)**.
