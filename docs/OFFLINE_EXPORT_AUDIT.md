# Check an export before reviewing its content

The offline checker detects missing topic files, incomplete post hydration and
identity conflicts in a **declared topic set**. It accepts local JSON snapshots
provided through a permitted export route. It makes no forum requests and does
not import posts into the public corpus, Second Brain, Anatomy or model training.

Before acquiring real data, follow [the acquisition and reuse gate](ACQUISITION_AND_RIGHTS.md).
An export inventory cannot establish operator permission, category visibility,
content rights or a forum-wide census. In particular, an inventory generated only
from the downloaded files cannot reveal topics omitted by the downloader.

## Local input contract

Keep the real export and its receipt in a restricted directory outside Git clones
and Hub upload folders. Prepare an inventory from the independently declared
export scope and expected post IDs. Bind each topic file's exact bytes by SHA-256.
The checker accepts this directory layout only:

```text
restricted-export/
  inventory.json
  topics/
    900001.json
    900002.json
```

The illustrative topic IDs above belong to synthetic fixtures. The inventory
uses [the versioned schema](../schema/discourse-export-inventory-v1.schema.json):

```json
{
  "schema": "szl-discourse-export-inventory-v1",
  "site": "https://ai4science.discourse.group",
  "scope": "declared_topic_set",
  "collected_at": "2026-10-02T12:00:00Z",
  "topics": [
    {
      "topic_id": 900001,
      "category_id": 8,
      "file_sha256": "<64 lowercase hexadecimal characters>",
      "post_ids": [910001, 910002]
    }
  ]
}
```

There is no arbitrary file-path field: filenames derive from validated numeric
topic IDs. Each topic file contains one hydrated Discourse topic response:
`id`, `category_id`, `archetype`, `visible`, `posts_count`, and
`post_stream.stream` plus `post_stream.posts`. Each post requires numeric `id`,
`topic_id`, `post_number`, `post_type`, and a string `cooked` field. Empty cooked
strings are accepted for action records; the checker verifies record presence,
not the truth, usefulness or semantic completeness of post text. Extra ordinary
Discourse response fields are ignored and never copied to the receipt.

This is a local contract for assembled topic snapshots, not a database-backup
reader or an API downloader. Discourse's [post stream serializer](https://github.com/discourse/discourse/blob/main/app/serializers/post_stream_serializer_mixin.rb)
serializes the loaded posts and filtered stream separately; its
[topic serializer](https://github.com/discourse/discourse/blob/main/app/serializers/topic_view_serializer.rb)
also supplies a topic counter. The checker requires equality of the inventory,
stream and hydrated post ID sets. Counter disagreement produces
`COUNT_UNRESOLVED`, even when all those IDs match. It does not guess adjustments
for deleted posts, filters or action records. Mega-topic streams without a full
ID list are unsupported.

Private messages, invisible/deleted/shared-draft topics, hidden/deleted posts and
whispers are rejected. Category IDs must match the declared inventory; a match
does not independently prove that a category is ordinary-member-visible. A locked
topic (`closed: true`) alone is not a privacy classification. Attachments, profiles,
edits, moderation history and category enumeration are outside the coverage claim.
They must be excluded or handled separately under the acquisition policy.

## Run and interpret

This working example contains only original synthetic text, never forum posts:

```powershell
python -B -m szl_forum_corpus.export_audit examples/export-audit-synthetic --out build/synthetic-export-audit.json
```

For a permitted real export, point the same command at its restricted local path
and write the receipt to that restricted store. The output parent is created if
needed; an existing receipt is never overwritten.
Receipt destinations inside the audited `topics/` directory are rejected,
including destinations reached through linked parents, so writing a receipt
cannot change the set it just checked.

| Exit | Result | Meaning |
| --- | --- | --- |
| 0 | `DECLARED_EXPORT_COMPLETE` | Every declared topic and post record is present and bound to the declared bytes; counters agree. |
| 1 | `INCOMPLETE` | A topic file or post ID is missing, an unexpected post ID appears, or a topic counter is unresolved. A diagnostic receipt is written. |
| 2 | Rejected input/output | Invalid schema/JSON, duplicate or conflicting IDs, changed bytes, unsafe file input, unsupported scope, size limit, or receipt write failure. |

Receipts contain validated IDs, counts, hashes and missing-ID lists. They omit
titles, bodies, authors, credentials, paths and attachments. Even this metadata
can be sensitive, so receipts remain restricted until separately reviewed. CLI
diagnostics use fixed wording to avoid exposing attacker-controlled JSON keys or
exception payloads.

Every receipt keeps `forum_wide_coverage: NOT_ESTABLISHED`,
`access_and_rights_verification: NOT_PERFORMED`, `publication_state: REVIEW_REQUIRED`
and `model_training_authorized: false`. A structural pass never changes these
fields. Public projection still requires independently authored, rights-reviewed
records through the existing builder and separate GitHub/Hub publication gates.

The implementation bounds the inventory to 4 MiB, each topic to 16 MiB, total
input bytes to 256 MiB, topics to 10,000 and post IDs to 100,000 per topic. It
rejects unlisted entries in `topics/`, symlinked input files and linked topic
directories (including Windows junctions that resolve away from the declared
directory). It reads one topic at a time and performs no archive extraction.
