# Science workflows: review ledger, recheck queue and figure checks

Three bounded operator workflows wrap existing SZL Skills kernels. They keep review history,
check declared relationships and report mismatches. They do not extract papers, discover
dependencies, interpret statistics or decide that any scientific claim is true.

| Build | Module | Pinned kernel (szl-skills `3e1f02c`) | What it adds |
|---|---|---|---|
| 1. Paper extraction review ledger | `szl_forum_corpus/workflows/paper_review.py` | `pilots/paper-rescue-benchmark/.../kernel.py` | Frozen initial comparison, correction history, adjudication, final QC with invalidation, effort sessions |
| 2. Evidence-change recheck queue | `szl_forum_corpus/workflows/change_recheck.py` | `skills/szl-research-change-impact/kernel.py` | Immutable graph snapshots, change sets that atomically create recheck tasks, human resolution, supersession |
| 3. Canonical figure package checker | `szl_forum_corpus/workflows/figure_check.py` | `skills/szl-figure-data-contract/scripts/run.py` | Closed-inventory bundles, rendering into new versions, immutable checks with localized mismatches |

All three use the same strict envelope (`workflows/contracts.py`), role admission
(`workflows/permissions.py`) and append-only store (`workflows/store.py`). The local operator
pages are in `szl_forum_corpus/web/`. Everything is Python standard library only.

## Pinned kernels

`szl_forum_corpus/kernels/` holds byte-identical copies of the three upstream kernel files with
`SOURCES.json` (repository, commit, upstream path, SHA-256, allowed operations) and the Apache-2.0
`NOTICE`. `.gitattributes` marks them `-text` so line-ending conversion cannot change their bytes.
`kernels.load()` checks each file's digest before importing it; a changed or missing file is
`BLOCKED`, is never imported, and every workflow then returns `KERNEL_UNAVAILABLE` (HTTP 503)
instead of a result. Nothing is downloaded at runtime.

Upstream suites were run unchanged at the pin before the copies were taken: research change
impact 17 passed, research change retirement 7 passed, figure-data contract 18 passed with
1 skipped (symlink privileges unavailable on the local Windows machine), paper-rescue benchmark 13 passed,
paper-rescue Docling bridge 12 passed with 1 skipped (no local NASA Docling JSON supplied). The two
skips are environment-dependent and are not counted as passes.

## Store and request semantics

- One SQLite file, one writer. Every mutation names a 32-hex `request_id` and the revision it
  last saw. The same request ID with the same canonical request returns the original result; a
  different request under that ID is `REQUEST_ID_CONFLICT`. A stale revision is
  `STALE_REVISION` (409). There is no last-write-wins.
- Event rows, projection updates and the request record commit in one `BEGIN IMMEDIATE`
  transaction. Triggers make events, requests, input blobs and the schema marker append-only.
  The store refuses to open if its schema or triggers differ from the expected definition.
- Results are reported only after an independent read-only readback. Failures are typed:
  `STORE_UNAVAILABLE_BEFORE_COMMIT` (nothing recorded), `COMMIT_OUTCOME_UNKNOWN` (recover by
  request ID before deciding anything), `COMMITTED_READBACK_UNAVAILABLE` (committed; recover by
  request ID).
- Every read and write is scoped to the server-side tenant before lookup. Other tenants get the
  same not-found response.
- Original input bytes are kept as digest-addressed blobs. Typed digests name their byte domain
  and canonicalization, so original bytes and normalized JSON are never confused.

## Build 1: paper extraction review ledger

Registration admits the candidate records (`szl.paper-rescue.records.v1`), the declared reference
(`szl.paper-rescue.gold.v1`), a protocol revision and explicit declarations of reference
independence, completeness and source access. The kernel validates both documents; mismatched PDF
digests, duplicate reference IDs, missing rights or pipeline declarations and kind mismatches
are refused.

The initial comparison runs once on the frozen records. Candidate-only extras are split off
before scoring, so they never enter the denominator, and are shown as their own set. Each kernel
result maps explicitly to an extraction and locator dimension (`paper-rescue-result-map.v1`); an
unmapped result is refused, never defaulted. Corrections, adjudications, evidence requests,
final QC and reopen are separate events. Accepted corrections change the current value and
reopen final QC, but never the initial comparison or its discrepancy count. Accepted QC needs a
located reference, a bound locator on an accessible source, no unresolved adjudication and either
a matching value or an accepted correction.

Effort sessions use the server clock only. Open, paused or interrupted sessions are unknown, not
zero; one reviewer cannot run two sessions at once; amendments append with a reason; `wait` is
not staff effort. Rates stay `UNAVAILABLE` without a predeclared scoring policy.

## Build 2: evidence-change recheck queue

A snapshot is a manually supplied `szl.research-anatomy.v1` graph with declared coverage
(`complete`, `partial`, `unknown`) and optional disputed edges. The kernel validates it; cycles
are refused with their path, and unknown endpoints and duplicates are refused.

A change set compares two exact snapshots with `szl_plan_research_changes` (union of old and new
edges). In the same transaction it records the full report, creates one task per required claim
in `RECHECK_REQUIRED` or `MISSING_REQUIRED_CLAIM` (or reached by a disputed edge), and supersedes
each claim's earlier task. Tasks carry every known seed-to-claim path (at most 64 are kept; more
sets `paths_truncated`), the kernel witness, input bindings and dependency order. Path enumeration
first prunes, in linear time, every parent from which no seed is reachable, so unchanged ancestor
branches cost nothing, and then charges every step to an explicit budget of 1,000,000 operations
per change set (recorded as `path_enumeration`). Exceeding it refuses the change set with
`TOO_LARGE` before the store command runs, so nothing is recorded. Task execution (`open`, `in_review`, `waiting`,
`resolved`, `superseded`) is separate from the evidence condition. The queue's coverage warning is
derived from every recorded change set, not only from tasks: a partial or unknown comparison that
creates no task still warns, and an earlier incomplete change set keeps warning after later
complete ones, because comparing other snapshots does not rediscover what it could have missed
(`coverage.policy` = `every_recorded_change_set`; `coverage.incomplete_change_sets` names them).
A resolution needs a human-specified predicate, binds the current change set and predicate
revision, and means only that the predicate was judged satisfied for those inputs. Removed required claims can never be
confirmed satisfied. A new change set supersedes an old resolution rather than inheriting it.

## Build 3: canonical figure package checker

A bundle is a closed inventory of `spec`, `data` and optionally `svg` plus `sidecar` members,
admitted from bytes (supplied filenames are never paths). Only the kernel's canonical scatter and
line format is supported. If the caption assertions disagree with the CSV, the inputs are kept but
rendering is refused and checks fail. Rendering writes a new bundle version; checking records an
immutable check and points only that version at it, so a previous pass is never shown on a new
version.

The kernel decides pass or fail (`inspect_svg`, and sidecar equality as in the kernel's `verify`).
The adapter adds a structural diff to locate the row, label or attribute that differs; when the
kernel gives no detail (caption assertions) the report says localization is unavailable. Equivalent
SVG reformattings fail as unsupported formatting. n is the number of plotted rows, not independent
experimental units. The pinned kernel prints normalized decimals in captions and sidecars (40
becomes `4E+1`); that is its canonical form, so this adapter does not reformat it.

## Local workbench

```powershell
python -B -m szl_forum_corpus.web.workflow_routes --port 8771
```

The server binds `127.0.0.1` only and stores data in `~/.szl-science-workflows/` by default (a
path inside the repository is refused). It uses one fixed local demo principal that every page
labels unauthenticated. It accepts no uploads: the registration API routes return
`CAPABILITY_UNAVAILABLE` (503), and only the bundled synthetic fixtures can be loaded from the
overview page. All POSTs need the exact `Host`, a same-origin `Origin`, a CSRF token and a bounded
body; GET and HEAD never mutate; responses are `no-store` with a restrictive content security
policy; access logs record nothing. Pages are escaped Python HTML and work without JavaScript.

| Route | Method | Purpose |
|---|---|---|
| `/operator/science` | GET | Kernel capability manifest and fixture loader |
| `/operator/science/paper-review[/{case}]` | GET | Case list; frozen initial versus current items, forms, effort |
| `/operator/science/rechecks[/{task}]` | GET | Queue with coverage warning; task paths, bindings, predicate, history |
| `/operator/science/figures[/{bundle}]` | GET | Bundle list; inventory, latest check of this version, mismatches |
| `/api/science/v1/paper-cases/{id}[/summary]` | GET | Case projection; summary with full denominator |
| `/api/science/v1/paper-cases/{id}/{comparisons,review-events,effort-events}` | POST | JSON commands |
| `/api/science/v1/change-sets` | POST | Create a change set from registered snapshots |
| `/api/science/v1/change-sets/{id}` | GET | Full impact report and task membership |
| `/api/science/v1/recheck-tasks` | GET | Queue tied to one queue revision |
| `/api/science/v1/recheck-tasks/{id}/events` | POST | Task transitions |
| `/api/science/v1/figure-bundles/{id}/{renders,checks}` | POST | Render a new version; check one |
| `/api/science/v1/figure-checks/{id}` | GET | Check report |
| `/api/science/v1/figure-bundles/{id}/download` | GET | Closed-inventory private package |
| `/api/science/v1/{paper-cases,evidence-snapshots,figure-bundles}` | POST | Always 503: uploads are not qualified |

JSON commands need the `X-SZL-CSRF` header from `/api/science/v1/session`.

## Fixtures

`szl_forum_corpus/workflows/fixtures/` holds hand-authored synthetic fixtures (see its
`PROVENANCE.md`). They ship as package data, so a normal wheel installation loads them through
the workbench's default path (`importlib.resources`); `WorkflowApp` and `LocalServer` still accept
an explicit `fixtures=` directory, and `fixtures=None` reports the loader as `BLOCKED`.
`tests/test_workflow_packaging.py` checks that the package-data declaration covers every fixture
file and loads all three fixtures from a package copy outside the checkout; the CI
`installed-wheel` job builds the real wheel, installs it into a clean virtual environment outside
the checkout and does the same through HTTP (`tests/installed_wheel_acceptance.py`).

Expected results in `expected.json` files were derived by hand before the code ran. They
exercise every falsification case in the build contract: correct, wrong value, wrong unit,
ambiguous identity, noncomparable endpoint, missing, abstained, unresolved reference, inaccessible
source and unexpected candidate; shared-ancestor changes, a removed dependency with unchanged
output bytes, a removed required claim, unknown revision, access denial, a disputed edge, cycles,
stale resolution and malformed snapshots; moved, removed and relabelled points, altered sidecars,
reformatting, non-finite values, invalid captions, extra or missing members, malformed SVG, DTDs
and external references.

## Not established

- No authentication, multi-user identity, tenant isolation in production, or shared
  multi-process storage. SQLite here is a one-process, one-writer local profile.
- No upload, PDF parsing, network acquisition, watcher, scheduler, notification or model call.
- No human study: synthetic fixtures and test timings are not researcher benefit or time savings.
- No accessibility compliance claim; the pages use landmarks, labels and text status, but have not
  been audited with assistive technology.
- Rights declarations are recorded as declarations, not verified entitlements.
