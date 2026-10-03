# Windows native task preparation

This guide covers invoking the reviewed source-backup runner from Windows Task
Scheduler. It does not install a task, grant credentials, expand the archive
scope, or establish that any particular machine's scheduled backup succeeded.
Keep the code, source revision, provider authorization and runtime observations
as separate evidence. Lambda remains Conjecture 1 (OPEN).

## Resolve paths outside the launching application

A packaged desktop application can present a merged AppData view while writes
land in its private package directory. A file visible to an interactive agent
can consequently be missing at the same logical path in a native scheduled
process. Windows documents this behavior in its
[MSIX filesystem description](https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes).

Check actual file resolution before selecting an entry script, configuration or
executable. For a non-secret code-file path, an isolated Python probe is:

```powershell
py -3.12 -I -S -B -c "from pathlib import Path; print(Path(r'<entry-script-path>').resolve(strict=True))"
```

Do not rely on an application-private cache for a durable external task. Create
a dedicated native folder outside Git checkouts, the OneDrive sync root and the
application package. Protect its ACL for the original user and SYSTEM before
copying any private profile. Preserve the original profile and existing operation
records. Verify executable and code hashes against the retained pins.

Copying a folder does not rewrite its configuration. Bind the state directory,
configuration and executable references to the native locations; validate their
resolution again. Never put profile contents, OAuth tokens or credentials into
source control, task arguments, public receipts or screenshots.

## Keep the task and its source reads bounded

Use the original user's interactive logon and normal privileges when the native
paths and ACLs permit it. Administrator privileges do not repair a missing path.
Use a single configured backup writer, a private state directory and
`IgnoreNew` for overlapping task starts. Retain failed attempts and old snapshots.

Keep executable runner code frozen at a reviewed revision. Fetch only source
objects and the explicit main ref; do not turn a source fetch into an automatic
checkout of newly fetched executable code. Pin the installed native Git path
rather than relying on an application-specific PATH. Disable interactive
credential prompts for unattended reads. Anonymous reads are appropriate only
for a separately verified public source; never silently drop required
authentication for a private repository.

Choose an outer task deadline that accommodates the launcher's complete bounded
path. Three 60-second Git calls plus a 420-second child wait already permit 600
seconds, before interpreter startup and final record writes. An eight-minute
task deadline cannot cover that path. Keep the child's cloud-copy and independent
readback budget separate from the outer limit.

## Verify execution in the real task context

Read back the stored action, principal, schedule and deadline. A manual start
through Task Scheduler must produce both a fresh task exit code and a fresh
operation record from that invocation. A small one-time startup probe can isolate
interpreter and path access without reading credentials or contacting the cloud.
Disable the completed probe and retain its result.

For a successful backup, independently verify the exact remote archive bytes,
the immutable Git source inventory and the local checksum. Check the record's
timestamp against the actual task start; an earlier interactive success is not
evidence for a later task. A natural calendar trigger remains unobserved until it
actually fires. Interactive tasks also depend on the user's session, device
availability and the stored power/network conditions.

The resulting records remain unsigned local operation evidence. They establish
only the reported source snapshot and byte readback, not full disaster recovery,
native OneDrive client synchronization, forum reuse rights or training approval.
