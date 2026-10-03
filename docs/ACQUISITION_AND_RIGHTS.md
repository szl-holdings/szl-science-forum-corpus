# Acquisition and reuse gate

**Observed 2026-10-02:** Anonymous requests to Claude Science forum pages, RSS, and sitemap were redirected to login. The topic and latest JSON endpoints returned HTTP 403 to direct requests. `robots.txt` allows some topic paths, but it does not confer access or reuse rights. No complete forum export has been obtained. The first record indexes the operator's own topic [#426](https://ai4science.discourse.group/t/three-testable-science-skills-and-provenance-aware-github-imports/426). A second record indexes the operator's [topic #396](https://ai4science.discourse.group/t/feature-request-easier-skill-sharing-in-claude-science-from-a-real-attempt/396/1) from text the operator supplied in this conversation. The #396 forum page was not independently accessible; its summary is an original SZL paraphrase, not a copied post body or a claim of current page verification.

Before expanding the corpus, obtain from the forum operator a documented access route (prefer an admin-approved export) and a decision for each use: metadata indexing, derived summaries, quotation, retrieval, benchmark construction, and model training. Record the policy version, author/organizer permission where required, retention period, deletion process, and handling of edits and removed posts. A forum account and technical access are insufficient evidence for redistribution.

A [request draft](FORUM_EXPORT_REQUEST_DRAFT.md) is prepared for the operator. It has not been sent.

The [offline export checker](OFFLINE_EXPORT_AUDIT.md) can validate a permitted
local export against a separately declared topic/post inventory. Its synthetic
example requires no forum access. A structural pass is not approval to acquire,
publish, retrieve or train on any real data, and does not establish forum-wide
coverage.

The approved export should exclude private messages, email addresses, invite tokens, user profiles, attachments without separate rights, and closed/private categories unless specifically authorized. Keep any raw export in an access-controlled local store; never commit it to this repository or upload it to Hugging Face by default. Use the public builder only with independently authored summaries and explicit publication approvals. A reviewer must check that paraphrases do not reproduce substantial passages or expose personal information.

Only after a separate model-data decision should the team consider training. Prefer access-controlled retrieval first because a source can then be corrected or removed without retraining weights. A training proposal must name the specific permitted data, purpose, retention, removal process, model version, memorization tests, and an evaluation against a simpler retrieval baseline. `model_training_authorized` remains `false` in this repository's manifest.

For a permitted bulk collection, follow the site's documented export/API and rate limits. The [Discourse API](https://docs.discourse.org/) supports category pagination and topic post streams; this does not mean this particular forum exposes those routes to us. Do not bypass the observed 403, use session cookies outside the browser, or automate the signed-in UI as a substitute for an operator-approved export.

Research references: [W3C PROV](https://www.w3.org/TR/prov-o/), [Datasheets for Datasets](https://arxiv.org/abs/1803.09010), [Creative Commons FAQ](https://creativecommons.org/faq/index.html), [HHS internet research considerations](https://www.hhs.gov/ohrp/sachrp-committee/recommendations/2013-may-20-letter-attachment-b/index.html). These are design references; they do not establish the forum's own terms.
