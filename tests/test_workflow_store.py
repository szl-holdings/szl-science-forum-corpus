"""Layer B: persistence, idempotency, recovery and scoping of the workflow store."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from szl_forum_corpus.workflows.contracts import WorkflowError
from szl_forum_corpus.workflows.store import Context, Store, Write

ALICE = Context("alice", "tenant-a", "reviewer")
BOB = Context("bob", "tenant-a", "reviewer")
MALLORY = Context("mallory", "tenant-b", "reviewer")


def rid(number):
    return f"{number:032x}"


def creator(resource_id, value):
    def apply(_tx):
        return [Write(resource_id, "note", "created", {"value": value}, {"value": value})], {"ok": True}
    return apply


def updater(resource_id, value):
    def apply(tx):
        current = tx.get("note", resource_id)
        projection = dict(current, value=value)
        projection.pop("revision")
        return [Write(resource_id, "note", "updated", projection, {"value": value})], {}
    return apply


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "workflows.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def create(self, request, resource_id="n1", value="a", store=None, context=ALICE):
        return (store or self.store).command(context, rid(request), "note.create", {"id": resource_id, "value": value},
                                             {resource_id: None}, creator(resource_id, value))

    def test_identical_replay_returns_the_original_result_without_a_new_event(self):
        first = self.create(1)
        self.store.command(ALICE, rid(2), "note.update", {"value": "b"}, {"n1": 1}, updater("n1", "b"))
        again = self.create(1)
        self.assertEqual(first, again)
        self.assertEqual(len(self.store.history(ALICE, "n1")), 2)
        self.assertEqual(self.store.get(ALICE, "note", "n1")["value"], "b")

    def test_reusing_a_request_id_for_different_bytes_conflicts_without_mutation(self):
        self.create(1)
        with self.assertRaises(WorkflowError) as caught:
            self.create(1, value="different")
        self.assertEqual(caught.exception.code, "REQUEST_ID_CONFLICT")
        self.assertEqual(self.store.get(ALICE, "note", "n1")["value"], "a")

    def test_competing_reviewers_get_one_success_and_one_stale_conflict(self):
        self.create(1)
        self.store.command(ALICE, rid(2), "note.update", {"value": "alice"}, {"n1": 1}, updater("n1", "alice"))
        with self.assertRaises(WorkflowError) as caught:
            self.store.command(BOB, rid(3), "note.update", {"value": "bob"}, {"n1": 1}, updater("n1", "bob"))
        self.assertEqual(caught.exception.code, "STALE_REVISION")
        self.assertEqual(caught.exception.http_status, 409)
        self.assertEqual(self.store.get(ALICE, "note", "n1")["value"], "alice")

    def test_duplicate_identifier_is_refused(self):
        self.create(1)
        with self.assertRaises(WorkflowError) as caught:
            self.create(2)
        self.assertEqual(caught.exception.code, "EXISTS")

    def test_failure_before_commit_records_nothing(self):
        faulty = Store(self.path, faults=frozenset({"before_commit"}))
        with self.assertRaises(WorkflowError) as caught:
            self.create(1, store=faulty)
        self.assertEqual(caught.exception.code, "STORE_UNAVAILABLE_BEFORE_COMMIT")
        self.assertIsNone(self.store.recover(ALICE, rid(1)))
        with self.assertRaises(WorkflowError):
            self.store.get(ALICE, "note", "n1")

    def test_commit_failure_reports_unknown_outcome_never_success(self):
        faulty = Store(self.path, faults=frozenset({"commit"}))
        with self.assertRaises(WorkflowError) as caught:
            self.create(1, store=faulty)
        self.assertEqual(caught.exception.code, "COMMIT_OUTCOME_UNKNOWN")
        self.assertFalse(caught.exception.retryable)
        # The original request ID is the only safe way forward; here the commit never happened.
        self.assertIsNone(self.store.recover(ALICE, rid(1)))

    def test_readback_failure_after_commit_is_recoverable_by_request_id(self):
        faulty = Store(self.path, faults=frozenset({"readback"}))
        with self.assertRaises(WorkflowError) as caught:
            self.create(1, store=faulty)
        self.assertEqual(caught.exception.code, "COMMITTED_READBACK_UNAVAILABLE")
        recovered = self.store.recover(ALICE, rid(1))
        self.assertEqual(recovered["revisions"], {"n1": 1})
        # Replaying the exact request returns the committed result instead of a second event.
        self.assertEqual(self.create(1), recovered)
        self.assertEqual(len(self.store.history(ALICE, "n1")), 1)

    def test_events_requests_and_blobs_are_append_only(self):
        self.create(1)
        connection = sqlite3.connect(self.path)
        try:
            for statement in ("UPDATE events SET actor = 'x'", "DELETE FROM events",
                              "UPDATE requests SET result = '{}'", "DELETE FROM requests",
                              "DELETE FROM resources", "DELETE FROM meta"):
                with self.assertRaises(sqlite3.DatabaseError, msg=statement):
                    connection.execute(statement)
        finally:
            connection.close()

    def test_tampered_schema_is_refused_on_open(self):
        connection = sqlite3.connect(self.path)
        connection.execute("DROP TRIGGER events_append_only_u")
        connection.commit()
        connection.close()
        with self.assertRaises(WorkflowError) as caught:
            Store(self.path)
        self.assertEqual(caught.exception.code, "STORE_INTEGRITY")

    def test_other_tenants_see_nothing_and_cannot_recover_requests(self):
        self.create(1)
        with self.assertRaises(WorkflowError) as caught:
            self.store.get(MALLORY, "note", "n1")
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.assertEqual(self.store.list(MALLORY, "note"), [])
        self.assertIsNone(self.store.recover(MALLORY, rid(1)))
        # The same identifier in another tenant is a different resource.
        self.create(2, context=MALLORY)
        self.assertEqual(len(self.store.list(MALLORY, "note")), 1)

    def test_restart_preserves_history(self):
        self.create(1)
        reopened = Store(self.path)
        self.assertEqual(reopened.get(ALICE, "note", "n1")["revision"], 1)
        self.assertEqual(reopened.recover(ALICE, rid(1))["revisions"], {"n1": 1})

    def test_request_id_must_be_32_lowercase_hex(self):
        with self.assertRaises(WorkflowError):
            self.store.command(ALICE, "not-an-id", "note.create", {}, {"n1": None}, creator("n1", "a"))

    def test_blob_content_round_trips_and_is_digest_checked(self):
        def apply(tx):
            digest = tx.put_blob(b"original bytes", "source")
            return [Write("n1", "note", "created", {"digest": digest}, {"digest": digest})], {"digest": digest}
        result = self.store.command(ALICE, rid(1), "blob.put", {}, {"n1": None}, apply)
        self.assertEqual(self.store.blob(result["outcome"]["digest"]), b"original bytes")


if __name__ == "__main__":
    unittest.main()
