"""Offline runner contracts; simulated provider results establish no cloud proof."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request
import zipfile
import zlib

from scripts import backup_onedrive as backup
from scripts import run_onedrive_backup as runner
from scripts import verify_onedrive_backup as verify


REAL_POPEN = subprocess.Popen
REAL_CURRENT_MAIN = runner.current_main
SECRET = "OFFLINE_TEST_SECRET"


def fixture(revision="a" * 40):
    bodies = {"dataset/manifest.json": b'{"model_training_authorized":false}\n',
              "README.md": b"Reviewed public source fixture.\n"}
    entries = [{"path": name, "bytes": len(body),
                "git_blob_id": hashlib.sha1(
                    b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest(),
                "sha256": hashlib.sha256(body).hexdigest()}
               for name, body in bodies.items()]
    return revision, entries, bodies


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="szl-runner-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.sync = self.base / "OneDrive-fixture"
        self.repo = self.base / "repo-fixture"
        self.state = self.base / "private-state"
        for directory in (self.sync, self.repo, self.state):
            directory.mkdir(mode=0o700)
        self.config = self.base / (SECRET + "-private.conf")
        self.config.write_text(SECRET, encoding="ascii")
        self.config.chmod(0o600)
        self.executable = self.base / (SECRET + "-rclone.exe")
        self.executable.write_bytes(b"OFFLINE FIXTURE; NEVER EXECUTED")
        self.executable.chmod(0o700)
        self.executable_hash = hashlib.sha256(self.executable.read_bytes()).hexdigest()
        self.environment = {"GOMEMLIMIT": "64MiB"}
        self.revision, self.entries, self.bodies = fixture()
        self.archives = []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(backup, "ROOT", self.repo))
        self.snapshot = self.mock(backup, "snapshot", return_value=fixture())
        self.registration = self.mock(backup, "registered_onedrive", return_value=self.sync)
        self.private_remote = self.mock(verify, "private_remote", return_value=(
            self.config, self.executable, self.environment))
        self.main_revision = self.mock(runner, "current_main")
        self.stage = self.mock(backup, "stage", side_effect=self.stage_fixture)
        self.copy = self.mock(runner.subprocess, "run", return_value=SimpleNamespace(returncode=0))
        self.readback = self.mock(verify, "verify_remote", side_effect=self.remote_fixture)
        self.git = self.mock(backup, "git", side_effect=AssertionError("Real Git access forbidden"))
        self.spawn = self.mock(subprocess, "Popen", side_effect=AssertionError("Provider spawn forbidden"))
        self.mock(socket, "create_connection", side_effect=AssertionError("Network forbidden"))
        self.mock(urllib.request, "urlopen", side_effect=AssertionError("Network forbidden"))

    def mock(self, module, name, **kwargs):
        return self.stack.enter_context(patch.object(module, name, **kwargs))

    def stage_fixture(self, root, revision, entries, bodies):
        self.assertEqual(root, self.sync)
        directory = root.joinpath(*verify.SUBTREE,
                                  f"20261003T120000Z_{revision[:12]}_{len(self.archives):08x}")
        directory.mkdir(parents=True)
        archive = directory / f"szl-science-forum-corpus-{revision[:12]}.zip"
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as stream:
            for name, body in bodies.items():
                info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                stream.writestr(info, body)
            stream.writestr(zipfile.ZipInfo("BACKUP_MANIFEST.json", (2026, 1, 1, 0, 0, 0)),
                            backup.manifest_bytes(revision, entries))
            stream.comment = revision.encode("ascii")
        self.archives.append(archive)
        return {"archive_path": str(archive),
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}

    def remote_fixture(self, archive, measured, config, executable, remote, timeout, *, cloud_copy):
        self.assertTrue(cloud_copy)
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 300)
        self.assertEqual((config, executable), (self.config, self.executable))
        body = archive.read_bytes()
        checksum = hashlib.sha256(body).hexdigest()
        self.assertEqual(checksum, measured["archive_sha256"])
        return {**measured, "remote_readback_verified": True, "remote_evidence_class": "MEASURED",
                "remote_sha256": checksum, "remote_bytes": len(body),
                "provider_path": "/".join((*verify.CLOUD_COPY_SUBTREE, checksum, archive.name)),
                "target_choice": "CLOUD_COPY"}

    def args(self, **changes):
        values = dict(apply=True, expected_sha=self.revision, state_dir=str(self.state),
                      rclone_config=str(self.config), rclone_executable=str(self.executable),
                      remote="personal", expected_rclone_sha256=self.executable_hash, timeout=30.0)
        values.update(changes)
        return SimpleNamespace(**values)

    def argv(self, **changes):
        args = self.args(**changes)
        result = ["--apply"] if args.apply else []
        for field in ("expected_sha", "state_dir", "rclone_config", "rclone_executable",
                      "remote", "expected_rclone_sha256", "timeout"):
            value = getattr(args, field)
            if value is not None:
                result.extend(["--" + field.replace("_", "-"), str(value)])
        return result

    def invoke(self, argv=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = runner.main(self.argv() if argv is None else argv)
        combined = out.getvalue() + err.getvalue()
        for private in (SECRET, str(self.config), str(self.executable), str(self.state)):
            self.assertNotIn(private, combined)
        self.assertEqual(err.getvalue(), "")
        return code, json.loads(out.getvalue())

    def receipts(self):
        directory = self.state / "receipts"
        return {path.name: json.loads(path.read_text(encoding="utf-8"))
                for path in directory.glob("*.json")} if directory.exists() else {}

    def assert_blocked(self, code, report, reason, *, persisted=False):
        self.assertEqual(code, 1)
        self.assertEqual(report["state"], "BLOCKED")
        self.assertEqual(report["reason"], reason)
        self.assertEqual(report["remote_evidence_class"], "UNKNOWN")
        self.assertIs(report["remote_readback_verified"], False)
        self.assertIs(report["signed"], False)
        if persisted:
            saved = self.receipts()[report["run_id"] + ".json"]
            self.assertEqual(saved, report)
            self.assertIs(report["training_authorized"], False)
            self.assertIs(report["full_disaster_recovery"], False)
            encoded = json.dumps(saved)
            for private in (SECRET, str(self.config), str(self.executable), str(self.state)):
                self.assertNotIn(private, encoded)
            self.assertNotIn("archive_path", saved)

    def assert_no_copy(self):
        self.copy.assert_not_called()
        self.readback.assert_not_called()
        self.spawn.assert_not_called()
        self.git.assert_not_called()

    def write_state(self, **changes):
        if not self.archives:
            self.stage_fixture(self.sync, self.revision, self.entries, self.bodies)
        archive = self.archives[-1]
        value = dict(schema=runner.STATE_SCHEMA, source_revision=self.revision,
                     archive_path=str(archive),
                     archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
        value.update(changes)
        (self.state / "state.json").write_text(json.dumps(value), encoding="utf-8")
        return value

    def test_plan_is_local_only_even_when_provider_and_state_arguments_are_supplied(self):
        with contextlib.ExitStack() as stack:
            forbidden = [stack.enter_context(patch.object(module, name, side_effect=AssertionError(name)))
                         for module, name in ((runner, "private_state"), (runner, "exclusive"),
                                              (runner, "load_state"), (runner, "atomic_json"),
                                              (runner, "digest_file"), (verify, "checked_path"),
                                              (verify, "validate_local"), (Path, "open"),
                                              (Path, "stat"), (Path, "lstat"))]
            code, report = self.invoke(self.argv(apply=False))
        self.assertEqual(code, 0)
        self.assertEqual(report["state"], "PLAN_ONLY")
        self.assertEqual(report["source_revision"], self.revision)
        self.assertEqual(report["source_files"], len(self.entries))
        self.assertEqual(report["source_bytes"], sum(map(len, self.bodies.values())))
        self.assertEqual(report["remote_evidence_class"], "UNKNOWN")
        for field in ("remote_readback_verified", "signed", "training_authorized"):
            self.assertIs(report[field], False)
        self.snapshot.assert_called_once_with()
        for mocked in (*forbidden, self.registration, self.private_remote, self.main_revision, self.stage):
            mocked.assert_not_called()
        self.assert_no_copy()
        self.assertEqual(list(self.state.iterdir()), [])
        self.assertEqual(list(self.sync.iterdir()), [])

    def test_missing_malformed_or_different_expected_revision_blocks_before_provider(self):
        for apply in (False, True):
            for revision in (None, "", "A" * 40, "a" * 39, "b" * 40, "a" * 40 + "\n"):
                with self.subTest(apply=apply, revision=revision):
                    with self.assertRaisesRegex(runner.RunError, "^EXPECTED_SOURCE_REQUIRED_OR_CHANGED$"):
                        runner.execute(self.args(apply=apply, expected_sha=revision))
        self.registration.assert_not_called()
        self.private_remote.assert_not_called()
        self.stage.assert_not_called()
        self.assert_no_copy()
        self.assertEqual(list(self.state.iterdir()), [])

    def test_apply_requires_every_explicit_provider_and_state_argument(self):
        for field in ("state_dir", "rclone_config", "rclone_executable", "remote",
                      "expected_rclone_sha256"):
            for absent in (None, ""):
                with self.subTest(field=field, absent=absent):
                    code, report = self.invoke(self.argv(**{field: absent}))
                    self.assert_blocked(code, report, "EXPLICIT_PROVIDER_CONFIGURATION_REQUIRED")
        self.registration.assert_not_called()
        self.private_remote.assert_not_called()
        self.assert_no_copy()
        self.assertEqual(list(self.state.iterdir()), [])

    def test_hash_and_timeout_syntax_blocks_before_discovery(self):
        cases = [dict(expected_rclone_sha256=value) for value in
                 ("A" * 64, "a" * 63, "../escape", "a" * 64 + "\n")]
        cases += [dict(timeout=value) for value in (0, -1, 0.999, 301, float("nan"), float("inf"))]
        for changes in cases:
            with self.subTest(changes=changes):
                code, report = self.invoke(self.argv(**changes))
                self.assert_blocked(code, report, "INVALID_EXECUTABLE_HASH_OR_TIMEOUT")
        self.registration.assert_not_called()
        self.private_remote.assert_not_called()
        self.assert_no_copy()

    def test_provider_rejection_is_fixed_and_does_not_create_state(self):
        self.private_remote.side_effect = verify.VerificationError("PERSONAL_DRIVE_REQUIRED")
        code, report = self.invoke()
        self.assert_blocked(code, report, "PERSONAL_DRIVE_REQUIRED")
        self.stage.assert_not_called()
        self.main_revision.assert_not_called()
        self.assert_no_copy()
        self.assertEqual(list(self.state.iterdir()), [])

    def test_executable_digest_mismatch_blocks_before_state_lock_or_staging(self):
        code, report = self.invoke(self.argv(expected_rclone_sha256="0" * 64))
        self.assert_blocked(code, report, "EXECUTABLE_HASH_MISMATCH")
        self.private_remote.assert_called_once_with(str(self.config), str(self.executable),
                                                   "personal", self.sync)
        self.stage.assert_not_called()
        self.main_revision.assert_not_called()
        self.assert_no_copy()
        self.assertEqual(list(self.state.iterdir()), [])

    def test_success_records_a_redacted_receipt_and_exact_private_state(self):
        code, report = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(report["state"], "REMOTE_CONTENT_VERIFIED")
        self.assertIs(report["remote_readback_verified"], True)
        self.assertEqual(report["remote_evidence_class"], "MEASURED")
        self.assertIs(report["cloud_copy_requested"], True)
        self.assertIs(report["snapshot_reused"], False)
        self.assertIs(report["signed"], False)
        self.assertIs(report["training_authorized"], False)
        self.assertIs(report["full_disaster_recovery"], False)
        self.assertEqual(report["native_client_sync"], "UNKNOWN")
        self.assertEqual(report["executable_sha256"], self.executable_hash)
        self.assertEqual(report["runner_sha256"], hashlib.sha256(Path(runner.__file__).read_bytes()).hexdigest())
        self.assertEqual(report["verifier_sha256"], hashlib.sha256(Path(verify.__file__).read_bytes()).hexdigest())
        self.assertEqual(self.receipts(), {report["run_id"] + ".json": report})
        state = runner.load_state(self.state)
        self.assertEqual(state, {"schema": runner.STATE_SCHEMA, "source_revision": self.revision,
                                 "archive_path": str(self.archives[0]),
                                 "archive_sha256": report["archive_sha256"]})
        self.assertEqual(list(self.state.glob(".pending-*")), [])
        self.assertEqual(list((self.state / "receipts").glob(".pending-*")), [])
        self.assertEqual(self.main_revision.call_args_list, [unittest.mock.call(self.revision)] * 3)
        self.git.assert_not_called()
        self.spawn.assert_not_called()

    def test_same_revision_reuses_validated_state_without_duplicate_staging(self):
        first_code, first = self.invoke()
        original_state = (self.state / "state.json").read_bytes()
        original_archive = self.archives[0].read_bytes()
        with patch.object(verify, "validate_local", wraps=verify.validate_local) as validate:
            second_code, second = self.invoke()
        self.assertEqual((first_code, second_code), (0, 0))
        self.stage.assert_called_once_with(self.sync, self.revision, self.entries, self.bodies)
        self.assertTrue(second["snapshot_reused"])
        self.assertGreaterEqual(validate.call_count, 3)
        self.assertEqual(Path(validate.call_args_list[0].args[0]), self.archives[0])
        self.assertEqual((self.state / "state.json").read_bytes(), original_state)
        self.assertEqual(self.archives[0].read_bytes(), original_archive)
        self.assertEqual(self.copy.call_count, 2)
        self.assertEqual(self.copy.call_args_list[0].args[0], self.copy.call_args_list[1].args[0])
        self.assertEqual(len(self.receipts()), 2)
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_new_revision_stages_once_and_preserves_old_snapshot_and_receipt(self):
        _, first = self.invoke()
        original_path = self.archives[0]
        original_bytes = original_path.read_bytes()
        old_receipt = (self.state / "receipts" / (first["run_id"] + ".json")).read_bytes()
        revision, entries, bodies = fixture("b" * 40)
        self.snapshot.return_value = (revision, entries, bodies)
        code, report = self.invoke(self.argv(expected_sha=revision))
        self.assertEqual(code, 0)
        self.assertFalse(report["snapshot_reused"])
        self.assertEqual(report["source_revision"], revision)
        self.stage.assert_called_with(self.sync, revision, entries, bodies)
        self.assertEqual(self.stage.call_count, 2)
        self.assertNotEqual(self.archives[-1], original_path)
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertEqual((self.state / "receipts" / (first["run_id"] + ".json")).read_bytes(), old_receipt)
        self.assertEqual(runner.load_state(self.state)["source_revision"], revision)
        self.assertEqual(len(self.receipts()), 2)

    def test_reused_archive_hash_corruption_blocks_and_never_restages(self):
        state = self.write_state()
        self.archives[-1].write_bytes(b"corrupted fixture " + SECRET.encode())
        code, report = self.invoke()
        self.assert_blocked(code, report, "ARCHIVE_SHA256_MISMATCH", persisted=True)
        self.assertFalse(report["snapshot_reused"])
        self.assertEqual(runner.load_state(self.state), state)
        self.stage.assert_not_called()
        self.assert_no_copy()

    def test_invalid_state_bytes_duplicate_keys_and_bounds_fail_closed_without_repair(self):
        valid = self.write_state()
        duplicate = json.dumps(valid)[:-1] + ', "source_revision": "' + self.revision + '"}'
        cases = [b"", b"{", b"\xff", b"null", b"[]", b"true", b"17", b'"text"',
                 duplicate.encode(), b" " * (runner.MAX_STATE_BYTES + 1)]
        path = self.state / "state.json"
        for body in cases:
            with self.subTest(size=len(body), prefix=body[:16]):
                path.write_bytes(body)
                code, report = self.invoke()
                self.assert_blocked(code, report, "INVALID_STATE")
                self.assertEqual(path.read_bytes(), body)
                self.assertEqual(self.receipts(), {})
        self.main_revision.assert_not_called()
        self.stage.assert_not_called()
        self.assert_no_copy()

    def test_wrong_state_keys_schema_revision_hash_and_field_types_fail_closed(self):
        valid = self.write_state()
        cases = [{key: value for key, value in valid.items() if key != "schema"},
                 {**valid, "unexpected": SECRET}, {**valid, "schema": "other"}]
        for field in valid:
            cases.extend({**valid, field: value} for value in (None, True, 7, [], {}))
        cases.extend({**valid, field: value} for field, value in
                     (("source_revision", "b" * 39), ("source_revision", "A" * 40),
                      ("archive_sha256", "z" * 64), ("archive_sha256", "a" * 64 + "\n")))
        path = self.state / "state.json"
        for value in cases:
            with self.subTest(value=value):
                raw = json.dumps(value).encode()
                path.write_bytes(raw)
                code, report = self.invoke()
                self.assert_blocked(code, report, "INVALID_STATE")
                self.assertEqual(path.read_bytes(), raw)
        self.stage.assert_not_called()
        self.main_revision.assert_not_called()
        self.assert_no_copy()
        self.assertEqual(self.receipts(), {})

    def test_hardlinked_state_fails_closed(self):
        self.write_state()
        path = self.state / "state.json"
        os.link(path, self.base / "linked-state.json")
        code, report = self.invoke()
        self.assert_blocked(code, report, "INVALID_STATE")
        self.stage.assert_not_called()
        self.assert_no_copy()

    def test_state_directory_inside_repository_or_sync_is_rejected_before_writes(self):
        for directory in (self.sync, self.sync / "nested", self.repo, self.repo / "nested"):
            with self.subTest(directory=directory):
                directory.mkdir(mode=0o700, exist_ok=True)
                code, report = self.invoke(self.argv(state_dir=str(directory)))
                self.assert_blocked(code, report, "STATE_IN_REPOSITORY_OR_SYNC_ROOT")
                self.assertFalse((directory / "runner.lock").exists())
                self.assertFalse((directory / "state.json").exists())
        self.main_revision.assert_not_called()
        self.stage.assert_not_called()
        self.assert_no_copy()

    def test_git_directory_and_worktree_file_ancestors_reject_state_before_lock(self):
        for marker_kind in ("directory", "file"):
            for nested in (False, True):
                with self.subTest(marker_kind=marker_kind, nested=nested):
                    checkout = self.base / f"git-{marker_kind}-{nested}"
                    checkout.mkdir(mode=0o700)
                    marker = checkout / ".git"
                    if marker_kind == "directory":
                        marker.mkdir()
                    else:
                        marker.write_text("gitdir: offline-fixture-only\n", encoding="ascii")
                    directory = checkout / "private" / "nested" if nested else checkout
                    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                    code, report = self.invoke(self.argv(state_dir=str(directory)))
                    self.assert_blocked(code, report, "STATE_IN_GIT_CHECKOUT")
                    self.assertFalse((directory / "runner.lock").exists())
        self.assert_no_copy()
        self.stage.assert_not_called()

    def test_relative_parent_traversal_missing_or_file_state_paths_are_rejected(self):
        file = self.base / "state-file"
        file.write_bytes(b"keep")
        cases = [("relative-state", "UNSAFE_LOCAL_PATH"),
                 (str(self.state / ".." / self.state.name), "UNSAFE_LOCAL_PATH"),
                 (str(file), "LOCAL_PATH_TYPE_MISMATCH"),
                 (str(self.base / "missing-state"), "RUN_FAILED")]
        for raw, reason in cases:
            with self.subTest(raw=raw):
                code, report = self.invoke(self.argv(state_dir=raw))
                self.assert_blocked(code, report, reason)
        self.assertEqual(file.read_bytes(), b"keep")
        self.stage.assert_not_called()
        self.assert_no_copy()

    def test_linked_state_directory_escape_is_rejected(self):
        linked = self.base / "linked-state"
        try:
            linked.symlink_to(self.sync, target_is_directory=True)
        except OSError:
            self.skipTest("Host does not permit unprivileged symlinks")
        code, report = self.invoke(self.argv(state_dir=str(linked)))
        self.assert_blocked(code, report, "LINKED_LOCAL_PATH")
        self.stage.assert_not_called()
        self.assert_no_copy()
        self.assertEqual(list(self.sync.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "POSIX permission gate")
    def test_nonprivate_posix_state_directory_is_rejected(self):
        self.state.chmod(0o755)
        code, report = self.invoke()
        self.assert_blocked(code, report, "STATE_DIRECTORY_NOT_PRIVATE")
        self.assertEqual(list(self.state.iterdir()), [])
        self.assert_no_copy()

    def test_copy_is_immutable_bounded_argument_array_with_discarded_output(self):
        with patch.object(runner.time, "monotonic", side_effect=[100.0, 100.25, 101.0]):
            code, report = self.invoke()
        self.assertEqual(code, 0)
        self.copy.assert_called_once()
        call = self.copy.call_args
        archive = self.archives[0]
        target = "personal:" + "/".join((*verify.CLOUD_COPY_SUBTREE,
                                         report["archive_sha256"], archive.name))
        self.assertEqual(call.args, ([str(self.executable), "copyto", str(archive), target,
                                    "--config", str(self.config), "--immutable", "--checksum",
                                    "--no-traverse", "--transfers", "1", "--checkers", "1",
                                    "--retries", "1", "--low-level-retries", "1", "--max-duration",
                                    "29.750s", "--max-transfer", str(archive.stat().st_size + 65536),
                                    "--no-update-modtime", "--buffer-size", "0"],))
        self.assertIsInstance(call.args[0], list)
        self.assertTrue(all(isinstance(value, str) for value in call.args[0]))
        self.assertIs(call.kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(call.kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(call.kwargs.get("shell", False))
        self.assertFalse(call.kwargs.get("capture_output", False))
        self.assertEqual(call.kwargs["env"], self.environment)
        self.assertEqual(call.kwargs["timeout"], 29.75)
        self.assertEqual(self.readback.call_args.args[-1], 29.0)
        self.assertEqual(self.readback.call_args.kwargs, {"cloud_copy": True})

    def test_failed_upload_retains_snapshot_and_writes_redacted_blocked_receipt(self):
        self.copy.return_value = SimpleNamespace(returncode=9, stdout=SECRET, stderr=SECRET)
        code, report = self.invoke()
        self.assert_blocked(code, report, "CLOUD_COPY_FAILED", persisted=True)
        self.assertTrue(report["cloud_copy_requested"])
        self.assertTrue(self.archives[0].is_file())
        self.assertEqual(runner.load_state(self.state)["archive_path"], str(self.archives[0]))
        self.readback.assert_not_called()

    def test_failed_upload_retry_reuses_original_archive_and_keeps_failed_receipt(self):
        self.copy.side_effect = [SimpleNamespace(returncode=9), SimpleNamespace(returncode=0)]
        code, failed = self.invoke()
        self.assert_blocked(code, failed, "CLOUD_COPY_FAILED", persisted=True)
        failed_bytes = (self.state / "receipts" / (failed["run_id"] + ".json")).read_bytes()
        code, success = self.invoke()
        self.assertEqual(code, 0)
        self.assertTrue(success["snapshot_reused"])
        self.stage.assert_called_once()
        self.assertEqual(len(self.receipts()), 2)
        self.assertEqual((self.state / "receipts" / (failed["run_id"] + ".json")).read_bytes(), failed_bytes)

    def test_readback_mismatch_writes_blocked_receipt_and_clears_remote_claim(self):
        self.readback.side_effect = verify.VerificationError("REMOTE_SHA256_MISMATCH")
        code, report = self.invoke()
        self.assert_blocked(code, report, "REMOTE_SHA256_MISMATCH", persisted=True)
        self.copy.assert_called_once()
        self.assertTrue(report["cloud_copy_requested"])
        self.assertNotIn("remote_sha256", report)

    def test_remote_main_move_before_copy_is_blocked_and_receipted(self):
        self.main_revision.side_effect = [None, runner.RunError("REMOTE_MAIN_CHANGED")]
        code, report = self.invoke()
        self.assert_blocked(code, report, "REMOTE_MAIN_CHANGED", persisted=True)
        self.assertFalse(report["cloud_copy_requested"])
        self.assertEqual(self.stage.call_count, 1)
        self.assert_no_copy()

    def test_remote_main_move_after_readback_never_promotes_success(self):
        self.main_revision.side_effect = [None, None, runner.RunError("REMOTE_MAIN_CHANGED")]
        code, report = self.invoke()
        self.assert_blocked(code, report, "REMOTE_MAIN_CHANGED", persisted=True)
        self.copy.assert_called_once()
        self.readback.assert_called_once()
        self.assertTrue(report["cloud_copy_requested"])
        self.assertNotIn("remote_sha256", report)

    def test_source_move_during_staging_blocks_before_cloud_copy(self):
        def moved_source(*args):
            staged = self.stage_fixture(*args)
            self.snapshot.return_value = fixture("b" * 40)
            return staged

        self.stage.side_effect = moved_source
        code, report = self.invoke()
        self.assert_blocked(code, report, "STAGED_PATH_BINDING_MISMATCH", persisted=True)
        self.assert_no_copy()
        self.assertFalse((self.state / "state.json").exists())

    def test_archive_corruption_during_upload_blocks_before_readback(self):
        def corrupt(command, **kwargs):
            Path(command[2]).write_bytes(b"corrupted during simulated upload")
            return SimpleNamespace(returncode=0)

        self.copy.side_effect = corrupt
        code, report = self.invoke()
        self.assert_blocked(code, report, "ARCHIVE_SHA256_MISMATCH", persisted=True)
        self.readback.assert_not_called()

    def test_source_move_after_readback_is_blocked_before_success_receipt(self):
        def moved_source(*args, **kwargs):
            measured = self.remote_fixture(*args, **kwargs)
            self.snapshot.return_value = fixture("b" * 40)
            return measured

        self.readback.side_effect = moved_source
        code, report = self.invoke()
        self.assertEqual(code, 1, "Local canonical source moved after readback but runner promoted success")
        self.assertEqual(report["state"], "BLOCKED")
        self.assertEqual(report["remote_evidence_class"], "UNKNOWN")
        self.assertFalse(report["remote_readback_verified"])
        self.assertEqual(self.receipts()[report["run_id"] + ".json"], report)

    def test_deadline_before_copy_and_before_readback_is_fixed_and_receipted(self):
        for clocks, copy_count in (([100.0, 129.5], 0), ([100.0, 100.0, 129.5], 1)):
            with self.subTest(clocks=clocks):
                self.copy.reset_mock()
                with patch.object(runner.time, "monotonic", side_effect=clocks):
                    code, report = self.invoke()
                self.assert_blocked(code, report, "RUN_DEADLINE", persisted=True)
                self.assertEqual(self.copy.call_count, copy_count)
                self.readback.assert_not_called()

    def test_process_errors_use_fixed_redacted_diagnostics(self):
        errors = [OSError(SECRET), ValueError(SECRET), RuntimeError(SECRET),
                  backup.BackupError(SECRET), zipfile.BadZipFile(SECRET), zlib.error(SECRET),
                  EOFError(SECRET), subprocess.SubprocessError(SECRET),
                  subprocess.TimeoutExpired([SECRET], 1, output=SECRET, stderr=SECRET),
                  subprocess.CalledProcessError(7, [SECRET], output=SECRET, stderr=SECRET)]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.copy.side_effect = error
                code, report = self.invoke()
                self.assert_blocked(code, report, "RUN_FAILED", persisted=True)
        self.readback.assert_not_called()
        self.assertEqual(len(self.receipts()), len(errors))
        self.stage.assert_called_once()

    def test_staging_failure_is_redacted_and_retains_a_blocked_receipt(self):
        self.stage.side_effect = backup.BackupError(SECRET)
        code, report = self.invoke()
        self.assert_blocked(code, report, "RUN_FAILED", persisted=True)
        self.assertFalse(report["cloud_copy_requested"])
        self.assertFalse((self.state / "state.json").exists())
        self.assert_no_copy()

    def test_snapshot_failure_is_redacted_without_provider_or_state_operations(self):
        self.snapshot.side_effect = backup.BackupError(SECRET)
        code, report = self.invoke()
        self.assert_blocked(code, report, "RUN_FAILED")
        self.registration.assert_not_called()
        self.private_remote.assert_not_called()
        self.assert_no_copy()
        self.assertEqual(list(self.state.iterdir()), [])

    def test_receipt_capacity_blocks_without_deletion_or_duplicate_staging(self):
        receipts = self.state / "receipts"
        receipts.mkdir(mode=0o700)
        for index in range(2):
            (receipts / f"existing-{index}.json").write_bytes(b"preserve receipt")
        saved = {path.name: path.read_bytes() for path in receipts.iterdir()}
        with patch.object(runner, "MAX_RECEIPTS", 2):
            code, report = self.invoke()
        self.assert_blocked(code, report, "RECEIPT_CAPACITY_REACHED")
        self.assertEqual({path.name: path.read_bytes() for path in receipts.iterdir()}, saved)
        self.stage.assert_not_called()
        self.assert_no_copy()
        self.assertFalse((self.state / "state.json").exists())

    def test_last_available_receipt_slot_succeeds_then_blocks_next_run(self):
        with patch.object(runner, "MAX_RECEIPTS", 1):
            first_code, first = self.invoke()
            original = (self.state / "receipts" / (first["run_id"] + ".json")).read_bytes()
            second_code, second = self.invoke()
        self.assertEqual(first_code, 0)
        self.assert_blocked(second_code, second, "RECEIPT_CAPACITY_REACHED")
        self.assertEqual(len(self.receipts()), 1)
        self.assertEqual((self.state / "receipts" / (first["run_id"] + ".json")).read_bytes(), original)
        self.stage.assert_called_once()
        self.copy.assert_called_once()

    def test_invalid_cli_arguments_are_fixed_redacted_blocked_json(self):
        cases = [[*self.argv(), "--unknown", SECRET],
                 [*self.argv(), "--timeout", SECRET],
                 ["--apply", "--remote", SECRET]]
        for argv in cases:
            with self.subTest(argv=argv):
                try:
                    code, report = self.invoke(argv)
                except SystemExit as error:
                    self.fail(f"CLI parser exited {error.code} instead of fixed redacted BLOCKED JSON")
                self.assert_blocked(code, report, "INVALID_ARGUMENTS")
        self.registration.assert_not_called()
        self.assert_no_copy()

    def test_real_owned_child_holds_exclusive_lock_and_normal_exit_releases_it(self):
        # No provider executable is launched. Child opens only the tempfile lock.
        program = "\n".join([
            "import os, sys",
            "from pathlib import Path",
            "lock = Path(sys.argv[1]).open('a+b', buffering=0)",
            "if not lock.seek(0, 2): lock.write(b'0')",
            "lock.seek(0)",
            "if os.name == 'nt':",
            "    import msvcrt",
            "    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)",
            "else:",
            "    import fcntl",
            "    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)",
            "print('LOCKED', flush=True)",
            "sys.stdin.buffer.read(1)",
            "lock.close()",
        ])
        child = REAL_POPEN([sys.executable, "-I", "-B", "-c", program,
                            str(self.state / "runner.lock")],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, shell=False)
        ready = threading.Event()
        observed = []

        def observe():
            try:
                observed.append(child.stdout.readline())
            finally:
                ready.set()

        reader = threading.Thread(target=observe, daemon=True)
        reader.start()
        try:
            self.assertTrue(ready.wait(10), "Owned lock child did not report readiness")
            self.assertEqual([line.rstrip(b"\r\n") for line in observed], [b"LOCKED"])
            self.assertIsNone(child.poll())
            code, report = self.invoke()
            self.assert_blocked(code, report, "RUN_ALREADY_ACTIVE")
            self.assert_no_copy()
            self.stage.assert_not_called()
            self.main_revision.assert_not_called()
            self.assertFalse((self.state / "state.json").exists())
            self.assertEqual(self.receipts(), {})
            self.assertIsNone(child.poll())
            child.stdin.write(b"x")
            child.stdin.flush()
            self.assertEqual(child.wait(timeout=10), 0)
            with runner.exclusive(self.state):
                pass
            self.assertEqual((self.state / "runner.lock").read_bytes(), b"0")
        finally:
            # Cleanup can terminate/kill only the Popen instance created above.
            if child.poll() is None:
                try:
                    child.stdin.close()
                    child.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    child.terminate()
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=2)
            for stream in (child.stdin, child.stdout):
                if stream is not None:
                    stream.close()
            reader.join(timeout=2)

    def test_oversized_or_hardlinked_lock_is_rejected_before_snapshot_staging(self):
        lock = self.state / "runner.lock"
        lock.write_bytes(b"too large")
        code, report = self.invoke()
        self.assert_blocked(code, report, "UNSAFE_LOCK_FILE")
        lock.write_bytes(b"0")
        os.link(lock, self.base / "linked-lock")
        code, report = self.invoke()
        self.assert_blocked(code, report, "UNSAFE_LOCK_FILE")
        self.stage.assert_not_called()
        self.assert_no_copy()

    def test_current_main_requires_exact_single_ref_without_git_writes(self):
        # Override the global offline guard only with an in-memory Git result.
        with patch.object(backup, "git", return_value=(
                self.revision + "\trefs/heads/main\n").encode("ascii")) as git:
            REAL_CURRENT_MAIN(self.revision)
            git.assert_called_once_with("ls-remote", "--exit-code", "origin", "refs/heads/main")
            for output in (b"", ("b" * 40 + "\trefs/heads/main\n").encode(),
                           (self.revision + "\trefs/heads/other\n").encode(),
                           (self.revision + "\trefs/heads/main\n").encode() * 2):
                with self.subTest(output=output):
                    git.return_value = output
                    with self.assertRaisesRegex(runner.RunError, "^REMOTE_MAIN_CHANGED$"):
                        REAL_CURRENT_MAIN(self.revision)
        self.copy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
