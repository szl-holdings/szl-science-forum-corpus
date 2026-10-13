"""Append-only SQLite store for science workflow records.

One local writer at a time (SQLite ``BEGIN IMMEDIATE``). Every mutation carries a request ID
bound to its exact action, expected revisions and canonical payload. The events, the updated
projections and the request's recovery row commit in one transaction, and success is reported
only after a separate read-only connection reads them back.

These rows are ordinary unsigned database records. They are not signed receipts and cannot
detect someone who rewrites the whole database file consistently.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Callable

from .contracts import (WorkflowError, canonical_bytes, fail, request_id as check_request_id,
                        sha256_hex, utc_now)

STORE_SCHEMA = "szl.science-workflow-store.v1"

DDL = (
    "CREATE TABLE meta (singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
    "schema TEXT NOT NULL)",
    "CREATE TABLE blobs (digest TEXT PRIMARY KEY, role TEXT NOT NULL, size INTEGER NOT NULL, "
    "content BLOB NOT NULL)",
    "CREATE TABLE resources (tenant TEXT NOT NULL, resource_id TEXT NOT NULL, kind TEXT NOT NULL, "
    "revision INTEGER NOT NULL CHECK (revision >= 1), created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, projection TEXT NOT NULL, PRIMARY KEY (tenant, resource_id))",
    "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL, "
    "resource_id TEXT NOT NULL, kind TEXT NOT NULL, revision INTEGER NOT NULL, "
    "prior_revision INTEGER, event_type TEXT NOT NULL, actor TEXT NOT NULL, "
    "server_time TEXT NOT NULL, request_id TEXT NOT NULL, payload TEXT NOT NULL, "
    "payload_sha256 TEXT NOT NULL, UNIQUE (tenant, resource_id, revision))",
    "CREATE TABLE requests (request_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, "
    "binding TEXT NOT NULL, result TEXT NOT NULL, event_count INTEGER NOT NULL)",
    "CREATE TRIGGER blobs_append_only_u BEFORE UPDATE ON blobs "
    "BEGIN SELECT RAISE(ABORT, 'blobs are immutable'); END",
    "CREATE TRIGGER blobs_append_only_d BEFORE DELETE ON blobs "
    "BEGIN SELECT RAISE(ABORT, 'blobs are immutable'); END",
    "CREATE TRIGGER events_append_only_u BEFORE UPDATE ON events "
    "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END",
    "CREATE TRIGGER events_append_only_d BEFORE DELETE ON events "
    "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END",
    "CREATE TRIGGER requests_append_only_u BEFORE UPDATE ON requests "
    "BEGIN SELECT RAISE(ABORT, 'requests are append-only'); END",
    "CREATE TRIGGER requests_append_only_d BEFORE DELETE ON requests "
    "BEGIN SELECT RAISE(ABORT, 'requests are append-only'); END",
    "CREATE TRIGGER resources_no_delete BEFORE DELETE ON resources "
    "BEGIN SELECT RAISE(ABORT, 'resources are never deleted'); END",
    "CREATE TRIGGER meta_fixed_u BEFORE UPDATE ON meta "
    "BEGIN SELECT RAISE(ABORT, 'schema marker is fixed'); END",
    "CREATE TRIGGER meta_fixed_d BEFORE DELETE ON meta "
    "BEGIN SELECT RAISE(ABORT, 'schema marker is fixed'); END",
)


@dataclass(frozen=True)
class Context:
    """Server-derived identity. Client-supplied identities are never authoritative."""

    principal_id: str
    tenant_id: str
    role: str


@dataclass(frozen=True)
class Write:
    resource_id: str
    kind: str
    event_type: str
    projection: dict
    event_payload: dict


class Transaction:
    """The view of the store inside one command. Reads are tenant-scoped."""

    def __init__(self, connection: sqlite3.Connection, context: Context):
        self._connection = connection
        self._context = context

    def get(self, kind: str, resource_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT kind, revision, projection FROM resources WHERE tenant = ? AND resource_id = ?",
            (self._context.tenant_id, resource_id)).fetchone()
        if row is None or row[0] != kind:
            return None
        projection = json.loads(row[2])
        projection["revision"] = row[1]
        return projection

    def list(self, kind: str) -> list[dict]:
        rows = self._connection.execute(
            "SELECT resource_id, revision, projection FROM resources WHERE tenant = ? AND kind = ? "
            "ORDER BY resource_id", (self._context.tenant_id, kind)).fetchall()
        result = []
        for _resource_id, revision, projection in rows:
            value = json.loads(projection)
            value["revision"] = revision
            result.append(value)
        return result

    def put_blob(self, raw: bytes, role: str) -> str:
        digest = sha256_hex(raw)
        row = self._connection.execute("SELECT size FROM blobs WHERE digest = ?", (digest,)).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO blobs (digest, role, size, content) VALUES (?, ?, ?, ?)",
                (digest, role, len(raw), sqlite3.Binary(raw)))
        elif row[0] != len(raw):
            fail("STORE_INTEGRITY", "stored blob size disagrees with its digest")
        return digest

    def get_blob(self, digest: str) -> bytes:
        return _read_blob(self._connection, digest)


def _read_blob(connection: sqlite3.Connection, digest: str) -> bytes:
    row = connection.execute("SELECT content FROM blobs WHERE digest = ?", (digest,)).fetchone()
    if row is None:
        fail("STORE_INTEGRITY", "a referenced input blob is missing")
    raw = bytes(row[0])
    if sha256_hex(raw) != digest:
        fail("STORE_INTEGRITY", "a stored input blob no longer matches its digest")
    return raw


class Store:
    """A single-writer local store. The path is fixed by the application, never by a request.

    Each Store instance is one runtime. Its ``epoch`` is a fresh random value unless supplied, so
    a process restart (a new Store over the same file) always has a different epoch. Workflows
    record it next to anything whose meaning depends on the runtime staying up, such as an open
    effort interval. The epoch lives in projections and event payloads, so the SQL schema (and
    every existing database) is unchanged.
    """

    def __init__(self, path: Path, *, faults: frozenset[str] = frozenset(),
                 epoch: str | None = None):
        self.path = Path(path)
        self._faults = faults  # test-only failure injection points
        if epoch is None:
            epoch = secrets.token_hex(16)
        if not isinstance(epoch, str) or re.fullmatch(r"[0-9a-f]{32}", epoch) is None:
            raise ValueError("a runtime epoch is 32 lowercase hexadecimal characters")
        self.epoch = epoch
        connection = self._connect()
        try:
            self._prepare(connection)
        finally:
            connection.close()

    # ---- connections -------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _connect_read_only(self) -> sqlite3.Connection:
        uri = self.path.resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)

    def _prepare(self, connection: sqlite3.Connection) -> None:
        existing = {name: sql for name, sql in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}
        expected = {self._ddl_name(statement): statement for statement in DDL}
        if not existing:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in DDL:
                    connection.execute(statement)
                connection.execute("INSERT INTO meta (singleton, schema) VALUES (1, ?)", (STORE_SCHEMA,))
                connection.execute("COMMIT")
            except sqlite3.Error:
                connection.execute("ROLLBACK")
                raise
            return
        if existing != expected:
            fail("STORE_INTEGRITY", "store schema or triggers differ from the expected definition")
        marker = connection.execute("SELECT schema FROM meta WHERE singleton = 1").fetchone()
        if marker is None or marker[0] != STORE_SCHEMA:
            fail("STORE_INTEGRITY", "store schema marker is missing or unexpected")

    @staticmethod
    def _ddl_name(statement: str) -> str:
        words = statement.split()
        return words[2]

    # ---- commands ----------------------------------------------------------------------------

    def command(self, context: Context, request_id: str, action: str, payload: dict,
                expected: dict[str, int | None],
                apply: Callable[[Transaction], tuple[list[Write], dict]], *,
                declared: dict[str, int | None] | None = None) -> dict:
        """Run one mutation exactly once for its request ID and report it only after readback.

        ``expected`` maps every resource the command may touch to its last observed revision,
        or to None for a resource the command creates. ``declared`` is the part of that map the
        client supplied; it defaults to ``expected`` and is what binds the request identity, so a
        replay is recognised even after server-derived revisions have moved on.
        """

        check_request_id(request_id)
        for value in expected.values():
            # JSON booleans are not revisions: True must never pass as revision 1.
            if value is not None and (type(value) is not int or value < 1):
                fail("INVALID_INPUT", "expected revisions must be null or positive integers",
                     "expected_revision")
        bound = expected if declared is None else declared
        if any(key not in expected or expected[key] != value for key, value in bound.items()):
            fail("STORE_INTEGRITY", "declared revisions must be part of the expected revisions")
        binding = canonical_bytes([1, action, sorted([key, value] for key, value in bound.items()),
                                   payload]).decode("utf-8")
        try:
            connection = self._connect()
        except sqlite3.Error:
            raise WorkflowError("STORE_UNAVAILABLE_BEFORE_COMMIT", "the store could not be opened",
                                retryable=True)
        committed = False
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
                prior = connection.execute(
                    "SELECT tenant, binding, result FROM requests WHERE request_id = ?",
                    (request_id,)).fetchone()
                if prior is not None:
                    connection.execute("ROLLBACK")
                    if prior[0] != context.tenant_id or prior[1] != binding:
                        fail("REQUEST_ID_CONFLICT",
                             "this request ID already recorded a different action")
                    return json.loads(prior[2])
                current: dict[str, int | None] = {}
                for resource_id, want in sorted(expected.items()):
                    row = connection.execute(
                        "SELECT revision FROM resources WHERE tenant = ? AND resource_id = ?",
                        (context.tenant_id, resource_id)).fetchone()
                    have = None if row is None else row[0]
                    if want is None and have is not None:
                        fail("EXISTS", "a resource with this identifier already exists", resource_id)
                    if want is not None and have is None:
                        fail("NOT_FOUND", "the resource was not found", resource_id)
                    if want is not None and have != want:
                        fail("STALE_REVISION", "the resource changed since it was last read",
                             resource_id)
                    current[resource_id] = have
                writes, outcome = apply(Transaction(connection, context))
                if not writes:
                    fail("INVALID_INPUT", "the command produced no recorded change")
                now = utc_now()
                revisions: dict[str, int] = {}
                event_digests = []
                for write in writes:
                    if write.resource_id not in current:
                        fail("STORE_INTEGRITY", "a command wrote an undeclared resource")
                    prior_revision = revisions.get(write.resource_id, current[write.resource_id])
                    revision = (prior_revision or 0) + 1
                    projection = canonical_bytes(write.projection).decode("utf-8")
                    if prior_revision is None:
                        connection.execute(
                            "INSERT INTO resources (tenant, resource_id, kind, revision, created_at, "
                            "updated_at, projection) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (context.tenant_id, write.resource_id, write.kind, revision, now, now,
                             projection))
                    else:
                        connection.execute(
                            "UPDATE resources SET revision = ?, updated_at = ?, projection = ? "
                            "WHERE tenant = ? AND resource_id = ? AND revision = ? AND kind = ?",
                            (revision, now, projection, context.tenant_id, write.resource_id,
                             prior_revision, write.kind))
                        if connection.execute("SELECT changes()").fetchone()[0] != 1:
                            fail("STORE_INTEGRITY", "a projection update did not apply exactly once")
                    payload_bytes = canonical_bytes(write.event_payload)
                    digest = sha256_hex(payload_bytes)
                    connection.execute(
                        "INSERT INTO events (tenant, resource_id, kind, revision, prior_revision, "
                        "event_type, actor, server_time, request_id, payload, payload_sha256) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (context.tenant_id, write.resource_id, write.kind, revision, prior_revision,
                         write.event_type, context.principal_id, now, request_id,
                         payload_bytes.decode("utf-8"), digest))
                    revisions[write.resource_id] = revision
                    event_digests.append(digest)
                result = {"request_id": request_id, "action": action, "recorded_at": now,
                          "revisions": dict(sorted(revisions.items())),
                          "events_sha256": sha256_hex("\n".join(event_digests).encode("ascii")),
                          "outcome": outcome}
                connection.execute(
                    "INSERT INTO requests (request_id, tenant, binding, result, event_count) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (request_id, context.tenant_id, binding,
                     canonical_bytes(result).decode("utf-8"), len(writes)))
                if "before_commit" in self._faults:
                    raise sqlite3.OperationalError("injected failure before commit")
            except WorkflowError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.Error:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise WorkflowError("STORE_UNAVAILABLE_BEFORE_COMMIT",
                                    "the store failed before the change was committed",
                                    retryable=True)
            try:
                if "commit" in self._faults:
                    raise sqlite3.OperationalError("injected failure during commit")
                connection.execute("COMMIT")
                committed = True
            except sqlite3.Error:
                raise WorkflowError("COMMIT_OUTCOME_UNKNOWN",
                                    "the commit outcome is unknown; recover this request ID before "
                                    "deciding anything")
        finally:
            if not committed and connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            connection.close()
        return self._readback(request_id, len(writes))

    def _readback(self, request_id: str, event_count: int) -> dict:
        try:
            if "readback" in self._faults:
                raise sqlite3.OperationalError("injected readback failure")
            reader = self._connect_read_only()
            try:
                row = reader.execute("SELECT result, event_count FROM requests WHERE request_id = ?",
                                     (request_id,)).fetchone()
                events = reader.execute("SELECT COUNT(*) FROM events WHERE request_id = ?",
                                        (request_id,)).fetchone()[0]
            finally:
                reader.close()
        except sqlite3.Error:
            raise WorkflowError("COMMITTED_READBACK_UNAVAILABLE",
                                "the change was committed but could not be read back; recover this "
                                "request ID instead of resubmitting")
        if row is None or row[1] != event_count or events != event_count:
            raise WorkflowError("COMMITTED_READBACK_UNAVAILABLE",
                                "the committed change did not read back completely")
        return json.loads(row[0])

    # ---- reads -------------------------------------------------------------------------------

    def recover(self, context: Context, request_id: str) -> dict | None:
        """Return the original result of a request ID, or None after a consistent read finds none."""

        check_request_id(request_id)
        reader = self._connect_read_only()
        try:
            row = reader.execute("SELECT tenant, result FROM requests WHERE request_id = ?",
                                 (request_id,)).fetchone()
        finally:
            reader.close()
        if row is None or row[0] != context.tenant_id:
            return None
        return json.loads(row[1])

    def get(self, context: Context, kind: str, resource_id: str) -> dict:
        reader = self._connect_read_only()
        try:
            value = Transaction(reader, context).get(kind, resource_id)
        finally:
            reader.close()
        if value is None:
            fail("NOT_FOUND", "the resource was not found")
        return value

    def list(self, context: Context, kind: str) -> list[dict]:
        reader = self._connect_read_only()
        try:
            return Transaction(reader, context).list(kind)
        finally:
            reader.close()

    def history(self, context: Context, resource_id: str) -> list[dict]:
        reader = self._connect_read_only()
        try:
            rows = reader.execute(
                "SELECT revision, prior_revision, event_type, actor, server_time, request_id, "
                "payload, payload_sha256 FROM events WHERE tenant = ? AND resource_id = ? "
                "ORDER BY revision", (context.tenant_id, resource_id)).fetchall()
        finally:
            reader.close()
        return [{"revision": revision, "prior_revision": prior, "event_type": event_type,
                 "actor": actor, "server_time": server_time, "request_id": request,
                 "payload": json.loads(payload), "payload_sha256": digest}
                for revision, prior, event_type, actor, server_time, request, payload, digest in rows]

    def blob(self, digest: str) -> bytes:
        reader = self._connect_read_only()
        try:
            return _read_blob(reader, digest)
        finally:
            reader.close()
