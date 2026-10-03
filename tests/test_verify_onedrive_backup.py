"""Offline fixtures and process doubles: these tests establish no cloud proof."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from scripts import backup_onedrive as backup
from scripts import verify_onedrive_backup as verify


def fixture():
    bodies = {"dataset/manifest.json": b'{"model_training_authorized":false}\n',
              "README.md": b"Reviewed public source.\n"}
    entries = [{"path": name, "bytes": len(body),
                "git_blob_id": hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest(),
                "sha256": hashlib.sha256(body).hexdigest()} for name, body in bodies.items()]
    return "a" * 40, entries, bodies


class Pipe(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.requests = []

    def read(self, size=-1):
        self.requests.append(size)
        if size < 0:
            raise AssertionError("Unbounded process read")
        return super().read(size)


class FakeChild:
    def __init__(self, body=b"", code=0, *, wait_stalls=False):
        self.stdout = Pipe(body)
        self.exit_code = code
        self.returncode = None
        self.wait_stalls = wait_stalls
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        if self.wait_stalls and not self.killed:
            raise subprocess.TimeoutExpired("private-test-command", timeout)
        self.returncode = -9 if self.killed else self.exit_code
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class StalledPipe:
    def __init__(self):
        self.release = threading.Event()

    def read(self, size):
        self.release.wait(5)
        return b""

    def close(self):
        self.release.set()


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "OneDrive"
        self.repo = self.base / "repo"
        self.root.mkdir()
        self.repo.mkdir()
        self.revision, self.entries, self.bodies = fixture()
        directory = self.root.joinpath(*verify.SUBTREE,
                                      "20261003T120000Z_aaaaaaaaaaaa_0123abcd")
        directory.mkdir(parents=True)
        self.archive = directory / "szl-science-forum-corpus-aaaaaaaaaaaa.zip"
        self.write_zip()
        self.config = self.base / "private.conf"
        self.write_config()
        self.executable = self.base / "rclone.exe"
        self.executable.write_bytes(b"OFFLINE TEST DOUBLE; NEVER EXECUTED")
        self.executable.chmod(0o700)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(backup, "ROOT", self.repo))
        self.snapshot = self.stack.enter_context(patch.object(backup, "snapshot", return_value=fixture()))
        self.registration = self.stack.enter_context(patch.object(backup, "registered_onedrive",
                                                                return_value=self.root))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))

    def write_zip(self, *, bodies=None, revision=None, manifest=None, symlink=False):
        with zipfile.ZipFile(self.archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, body in (self.bodies if bodies is None else bodies).items():
                if symlink and name == "README.md":
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, body)
                else:
                    archive.writestr(name, body)
            archive.writestr("BACKUP_MANIFEST.json", backup.manifest_bytes(self.revision, self.entries)
                             if manifest is None else manifest)
            archive.comment = (self.revision if revision is None else revision).encode()
        self.expected = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def write_config(self, text=None):
        self.config.write_text(text or (
            '[personal]\ntype = onedrive\ndrive_type = personal\ndrive_id = test-drive\n'
            'token = {"access_token":"TEST_SECRET","refresh_token":"TEST_REFRESH"}\n'),
            encoding="utf-8")
        self.config.chmod(0o600)

    def argv(self, *, remote=False, cloud_copy=False):
        args = ["--archive", str(self.archive), "--expected-sha256", self.expected]
        if remote:
            args += ["--verify-remote", "--remote", "personal", "--rclone-config", str(self.config),
                     "--rclone-executable", str(self.executable)]
        if cloud_copy:
            args += ["--cloud-copy"]
        return args

    def invoke(self, args, children=()):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(verify.subprocess, "Popen", side_effect=list(children)) as spawn, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = verify.main(args)
        self.assertNotIn("TEST_SECRET", out.getvalue() + err.getvalue())
        self.assertNotIn("TEST_REFRESH", out.getvalue() + err.getvalue())
        self.assertNotIn(str(self.config), out.getvalue() + err.getvalue())
        self.assertNotIn(str(self.executable), out.getvalue() + err.getvalue())
        return result, json.loads(out.getvalue() or err.getvalue()), spawn

    def metadata(self, **overrides):
        return FakeChild(json.dumps({"Name": self.archive.name, "Size": self.archive.stat().st_size,
                                     "IsDir": False, **overrides}).encode())

    def test_plan_validates_local_source_without_registration_config_or_process(self):
        with patch.object(verify, "private_remote", side_effect=AssertionError("config access")), \
                patch.object(backup, "client_state", side_effect=AssertionError("client probe")):
            code, report, spawn = self.invoke(self.argv())
        self.assertEqual(code, 0)
        self.assertEqual(report["state"], "PLAN_ONLY")
        self.assertEqual(report["local_evidence_class"], "MEASURED")
        self.assertEqual(report["remote_evidence_class"], "UNKNOWN")
        self.assertFalse(report["remote_readback_verified"])
        self.registration.assert_not_called()
        spawn.assert_not_called()

    def test_expected_hash_and_explicit_opt_in_required(self):
        cases = [self.argv()[:-2], [*self.argv(), "--remote", "personal"],
                 [*self.argv(), "--verify-remote"], self.argv(cloud_copy=True),
                 [*self.argv(), "--arbitrary-path", "TEST_SECRET"]]
        for args in cases:
            with self.subTest(args=args):
                code, report, spawn = self.invoke(args)
                self.assertEqual(code, 1)
                self.assertEqual(report["state"], "BLOCKED")
                spawn.assert_not_called()
        self.registration.assert_not_called()

    def test_invalid_or_wrong_expected_hash_blocks_before_provider(self):
        for digest in ("A" * 64, "../escape", "0" * 64):
            with self.subTest(digest=digest):
                args = self.argv(remote=True)
                args[args.index("--expected-sha256") + 1] = digest
                code, _, spawn = self.invoke(args)
                self.assertEqual(code, 1)
                spawn.assert_not_called()
        self.registration.assert_not_called()

    def test_zip_source_inventory_revision_manifest_and_member_bounds(self):
        cases = [dict(bodies={**self.bodies, "extra": b"extra"}),
                 dict(bodies={"README.md": self.bodies["README.md"]}),
                 dict(revision="b" * 40), dict(manifest=b"changed"), dict(symlink=True),
                 dict(bodies={**self.bodies, "README.md": b"X" * len(self.bodies["README.md"])}),
                 dict(bodies={**self.bodies, "README.md": b"X" * (verify.CHUNK_BYTES * 2)})]
        for changes in cases:
            with self.subTest(changes=list(changes)):
                self.write_zip(**changes)  # Retain new hash: rejection must be source-based.
                code, _, spawn = self.invoke(self.argv(remote=True))
                self.assertEqual(code, 1)
                spawn.assert_not_called()
        self.registration.assert_not_called()

    def test_duplicate_members_and_invalid_zip_are_rejected(self):
        with zipfile.ZipFile(self.archive, "a") as archive:
            with self.assertWarns(UserWarning):
                archive.writestr("README.md", self.bodies["README.md"])
        self.expected = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.assertEqual(self.invoke(self.argv())[0], 1)
        self.archive.write_bytes(b"not a zip")
        self.expected = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.assertEqual(self.invoke(self.argv())[0], 1)

    def test_missing_truncated_and_oversized_local_archive(self):
        saved = self.archive.read_bytes()
        self.archive.unlink()
        self.assertEqual(self.invoke(self.argv())[0], 1)
        self.archive.write_bytes(saved[:-12])
        self.assertEqual(self.invoke(self.argv())[0], 1)
        with patch.object(verify, "MAX_ARCHIVE_BYTES", len(saved) - 1):
            self.archive.write_bytes(saved)
            self.assertEqual(self.invoke(self.argv())[0], 1)

    def test_local_path_traversal_outside_subtree_and_revision_folder(self):
        paths = [str(self.archive.parent / ".." / self.archive.parent.name / self.archive.name),
                 str(self.archive.relative_to(self.base)), str(self.base / self.archive.name),
                 str(self.archive).replace("_aaaaaaaaaaaa_", "_bbbbbbbbbbbb_")]
        for value in paths:
            with self.subTest(path=value):
                args = self.argv(remote=True)
                args[1] = value
                self.assertEqual(self.invoke(args)[0], 1)
        other = self.base / "OtherOneDrive"
        other.mkdir()
        self.registration.return_value = other
        code, report, spawn = self.invoke(self.argv(remote=True, cloud_copy=True))
        self.assertEqual(code, 1)
        self.assertEqual(report["reason"], "ARCHIVE_OUTSIDE_REGISTERED_SUBTREE")
        spawn.assert_not_called()

    def test_symlink_archive_or_parent_is_rejected(self):
        linked = self.base / "linked"
        try:
            linked.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("Host does not permit unprivileged symlink creation")
        with self.assertRaisesRegex(verify.VerificationError, "LINKED_LOCAL_PATH"):
            verify.checked_path(linked.joinpath(*self.archive.relative_to(self.root).parts))
        saved = self.base / "source.zip"
        self.archive.rename(saved)
        self.archive.symlink_to(saved)
        self.assertEqual(self.invoke(self.argv())[0], 1)

    def test_placeholder_and_junction_attributes_are_rejected_without_content_read(self):
        original = Path.lstat
        for attributes, tag, reason in ((0x1000, 0, "LOCAL_FILE_NOT_RESIDENT"),
                                        (0, 0xA0000003, "LINKED_LOCAL_PATH")):
            with self.subTest(reason=reason):
                real = self.archive.stat()
                fake = type("Info", (), {"st_mode": real.st_mode, "st_file_attributes": attributes,
                                         "st_reparse_tag": tag})()
                with patch.object(Path, "lstat", lambda p: fake if p == self.archive else original(p)), \
                        patch.object(Path, "stat", return_value=fake):
                    with self.assertRaisesRegex(verify.VerificationError, reason):
                        verify.checked_path(self.archive)

    def test_backend_personal_root_override_and_config_inheritance_fail_closed(self):
        original = self.config.read_text()
        cases = [(original.replace("type = onedrive", "type = local"), "ONEDRIVE_BACKEND_REQUIRED"),
                 (original.replace("personal\ndrive_id", "business\ndrive_id"), "PERSONAL_DRIVE_REQUIRED"),
                 (original + "root_folder_id = alternate\n", "UNSUPPORTED_REMOTE_CONFIGURATION"),
                 (original + "token_url = https://private.invalid/TEST_SECRET\n",
                  "UNSUPPORTED_REMOTE_CONFIGURATION"),
                 (original.replace("[personal]", "[different]"), "EXPLICIT_CONFIG_SECTION_REQUIRED"),
                 ("[DEFAULT]\ntype=onedrive\n" + original, "EXPLICIT_CONFIG_SECTION_REQUIRED"),
                 (original + "type = onedrive\n", "INVALID_PRIVATE_CONFIGURATION"),
                 (original.replace("TEST_REFRESH", ""), "MANUAL_AUTH_REQUIRED")]
        for text, reason in cases:
            with self.subTest(reason=reason):
                self.write_config(text)
                code, report, spawn = self.invoke(self.argv(remote=True))
                self.assertEqual(code, 1)
                self.assertEqual(report["reason"], reason)
                spawn.assert_not_called()

    def test_config_must_be_outside_repo_and_sync_root_and_bounded(self):
        original = self.config
        for parent in (self.repo, self.root):
            self.config = parent / "private.conf"
            self.write_config()
            code, report, spawn = self.invoke(self.argv(remote=True))
            self.assertEqual(code, 1)
            self.assertEqual(report["reason"], "CONFIG_IN_REPOSITORY_OR_SYNC_ROOT")
            spawn.assert_not_called()
        self.config = original
        self.write_config("X" * (verify.MAX_CONFIG_BYTES + 1))
        self.assertEqual(self.invoke(self.argv(remote=True))[0], 1)

    def test_remote_names_and_environment_overrides_are_rejected(self):
        for remote in ("personal:", ":onedrive", "personal,root_folder_id=x", "a/b", "..", "-flag"):
            with self.subTest(remote=remote):
                args = self.argv(remote=True)
                args[args.index("--remote") + 1] = remote
                code, _, spawn = self.invoke(args)
                self.assertEqual(code, 1)
                spawn.assert_not_called()
        for key in ("RCLONE_CONFIG_PERSONAL_TYPE", "RCLONE_ONEDRIVE_DRIVE_TYPE", "RCLONE_DUMP",
                    "rclone_config", "RCLONE_CONFIG_PASS"):
            with self.subTest(key=key), patch.dict(os.environ, {key: "TEST_SECRET"}):
                code, report, spawn = self.invoke(self.argv(remote=True))
                self.assertEqual(code, 1)
                self.assertEqual(report["reason"], "RCLONE_ENVIRONMENT_OVERRIDE")
                spawn.assert_not_called()

    def test_hyphenated_remote_uses_explicit_section_and_exact_fixed_targets(self):
        self.write_config(self.config.read_text().replace("[personal]", "[szl-personal]")
                          + "access_scopes = Files.ReadWrite offline_access\n"
                            "disable_site_permission = false\nclient_credentials = false\n")
        for cloud_copy in (False, True):
            with self.subTest(cloud_copy=cloud_copy):
                args = self.argv(remote=True, cloud_copy=cloud_copy)
                args[args.index("--remote") + 1] = "szl-personal"
                code, report, spawn = self.invoke(args, [self.metadata(), FakeChild(self.archive.read_bytes())])
                self.assertEqual(code, 0)
                relative = ("/".join((*verify.CLOUD_COPY_SUBTREE, self.expected, self.archive.name))
                            if cloud_copy else self.archive.relative_to(self.root).as_posix())
                self.assertEqual(report["provider_path"], relative)
                for call in spawn.call_args_list:
                    self.assertEqual(call.args[0][2], "szl-personal:" + relative)

    def test_client_credentials_accepts_only_false_or_absent(self):
        original = self.config.read_text()
        for value in (None, "false", "true", "TRUE", "1", "yes", "", "FALSE"):
            with self.subTest(value=value):
                self.write_config(original + ("" if value is None else f"client_credentials = {value}\n"))
                allowed = value in {None, "false"}
                children = [self.metadata(), FakeChild(self.archive.read_bytes())] if allowed else []
                code, report, spawn = self.invoke(self.argv(remote=True), children)
                self.assertEqual(code, 0 if allowed else 1)
                if not allowed:
                    self.assertEqual(report["reason"], "CLIENT_CREDENTIALS_FORBIDDEN")
                    spawn.assert_not_called()

    def test_config_in_any_git_ancestor_is_rejected_before_open_or_process(self):
        for marker_type in ("directory", "file"):
            for nested in (False, True):
                with self.subTest(marker_type=marker_type, nested=nested):
                    checkout = self.base / f"other-{marker_type}-{nested}"
                    checkout.mkdir()
                    marker = checkout / ".git"
                    if marker_type == "directory":
                        marker.mkdir()
                    else:
                        marker.write_text("gitdir: unrelated-test-metadata\n", encoding="ascii")
                    directory = checkout / "private" / "nested" if nested else checkout
                    directory.mkdir(parents=True, exist_ok=True)
                    self.config = directory / "private.conf"
                    self.write_config()
                    # Detection must inspect markers only, never open credentials.
                    original_open = Path.open

                    def guarded_open(path, *args, **kwargs):
                        if path == self.config:
                            raise AssertionError("Credential file was opened inside a Git checkout")
                        return original_open(path, *args, **kwargs)

                    with patch.object(Path, "open", guarded_open):
                        code, report, spawn = self.invoke(self.argv(remote=True))
                    self.assertEqual(code, 1)
                    self.assertEqual(report["reason"], "CONFIG_IN_GIT_CHECKOUT")
                    spawn.assert_not_called()

    def test_success_streams_exact_native_and_cloud_copy_targets(self):
        body = self.archive.read_bytes()
        for cloud_copy in (False, True):
            with self.subTest(cloud_copy=cloud_copy):
                cat = FakeChild(body)
                code, report, spawn = self.invoke(self.argv(remote=True, cloud_copy=cloud_copy),
                                                  [self.metadata(), cat])
                self.assertEqual(code, 0)
                self.assertTrue(report["remote_readback_verified"])
                self.assertEqual(report["remote_evidence_class"], "MEASURED")
                relative = ("/".join((*verify.CLOUD_COPY_SUBTREE, self.expected, self.archive.name))
                            if cloud_copy else self.archive.relative_to(self.root).as_posix())
                self.assertEqual(report["provider_path"], relative)
                self.assertEqual(report["remote_sha256"], self.expected)
                self.assertEqual(report["remote_bytes"], len(body))
                self.assertEqual(spawn.call_count, 2)
                for call in spawn.call_args_list:
                    command = call.args[0]
                    self.assertIsInstance(command, list)
                    self.assertEqual(command[2], "personal:" + relative)
                    self.assertFalse(call.kwargs["shell"])
                    self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
                    self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
                    self.assertEqual(command[command.index("--buffer-size") + 1], "0")
                command = spawn.call_args_list[1].args[0]
                self.assertEqual(command[1], "cat")
                self.assertEqual(command[command.index("--count") + 1], str(len(body) + 1))
                self.assertTrue(all(0 < request <= verify.CHUNK_BYTES for request in cat.stdout.requests))

    def test_remote_missing_truncated_oversized_wrong_bytes_and_exit_failure(self):
        body = self.archive.read_bytes()
        cases = [([FakeChild(code=3)], "REMOTE_COMMAND_FAILED"),
                 ([self.metadata(), FakeChild(body[:-1])], "REMOTE_SIZE_MISMATCH"),
                 ([self.metadata(), FakeChild(body + b"extra")], "REMOTE_BYTE_LIMIT"),
                 ([self.metadata(), FakeChild(b"X" * len(body))], "REMOTE_SHA256_MISMATCH"),
                 ([self.metadata(), FakeChild(body, code=7)], "REMOTE_COMMAND_FAILED")]
        for children, reason in cases:
            with self.subTest(reason=reason):
                code, report, _ = self.invoke(self.argv(remote=True), children)
                self.assertEqual(code, 1)
                self.assertEqual(report["reason"], reason)
                self.assertFalse(report["remote_readback_verified"])
                self.assertEqual(report["remote_evidence_class"], "UNKNOWN")

    def test_remote_directory_metadata_mismatch_invalid_and_excessive_output(self):
        for child in (self.metadata(IsDir=True), self.metadata(Size=1), self.metadata(Name="other.zip"),
                      FakeChild(b"TEST_SECRET"), FakeChild(b"X" * (verify.MAX_STAT_BYTES + 1))):
            with self.subTest(child=child):
                code, _, spawn = self.invoke(self.argv(remote=True), [child])
                self.assertEqual(code, 1)
                self.assertEqual(spawn.call_count, 1)  # Never cat a directory/invalid stat.

    def test_timeout_range_and_launch_errors_do_not_disclose_details(self):
        for value in ("0", "301", "nan", "inf"):
            code, report, spawn = self.invoke([*self.argv(remote=True), "--timeout", value])
            self.assertEqual(code, 1)
            self.assertEqual(report["reason"], "TIMEOUT_OUT_OF_RANGE")
            spawn.assert_not_called()
        err = io.StringIO()
        with patch.object(verify.subprocess, "Popen", side_effect=OSError("TEST_SECRET")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(verify.main(self.argv(remote=True)), 1)
        self.assertNotIn("TEST_SECRET", err.getvalue())


class StreamTests(unittest.TestCase):
    def test_blocked_read_deadline_terminates_its_child(self):
        child = FakeChild()
        pipe = StalledPipe()
        child.stdout = pipe
        original_terminate = child.terminate

        def terminate():
            original_terminate()
            pipe.release.set()

        child.terminate = terminate
        with patch.object(verify.subprocess, "Popen", return_value=child):
            with self.assertRaisesRegex(verify.VerificationError, "REMOTE_TIMEOUT"):
                verify.stream_child(["offline-double"], {}, 10, 0.02)
        self.assertTrue(child.terminated)
        self.assertIsNotNone(child.poll())

    def test_exit_timeout_escalates_to_kill_and_reaps_owned_child(self):
        child = FakeChild(b"ok", wait_stalls=True)
        with patch.object(verify.subprocess, "Popen", return_value=child):
            with self.assertRaisesRegex(verify.VerificationError, "REMOTE_TIMEOUT"):
                verify.stream_child(["offline-double"], {}, 2, 0.02)
        self.assertTrue(child.terminated)
        self.assertTrue(child.killed)
        self.assertIsNotNone(child.poll())

    def test_reader_failure_terminates_child(self):
        child = FakeChild()
        child.stdout.read = lambda size: (_ for _ in ()).throw(OSError("TEST_SECRET"))
        with patch.object(verify.subprocess, "Popen", return_value=child):
            with self.assertRaisesRegex(verify.VerificationError, "REMOTE_STREAM_FAILED"):
                verify.stream_child(["offline-double"], {}, 10, 1)
        self.assertTrue(child.terminated)

    def test_real_local_python_child_streams_with_discarded_stderr(self):
        # Actual pipes/process, entirely offline; this executable is not rclone.
        command = [sys.executable, "-I", "-B", "-c",
                   "import os; os.write(2,b'TEST_SECRET'*10000); os.write(1,b'x'*200000)"]
        size, digest, captured = verify.stream_child(command, dict(os.environ), 200000, 10)
        self.assertEqual(size, 200000)
        self.assertEqual(digest, hashlib.sha256(b"x" * size).hexdigest())
        self.assertEqual(captured, b"")

    def test_real_local_python_oversize_and_timeout_leave_no_owned_child(self):
        programs = [("import os\nwhile True: os.write(1,b'x'*65536)", 10, "REMOTE_BYTE_LIMIT"),
                    ("import time; time.sleep(30)", 0.05, "REMOTE_TIMEOUT")]
        original = subprocess.Popen
        for program, timeout, reason in programs:
            owned = []

            def launch(*args, **kwargs):
                child = original(*args, **kwargs)
                owned.append(child)
                return child

            with self.subTest(reason=reason), patch.object(verify.subprocess, "Popen", side_effect=launch):
                with self.assertRaisesRegex(verify.VerificationError, reason):
                    verify.stream_child([sys.executable, "-I", "-B", "-c", program],
                                        dict(os.environ), 32, timeout)
            self.assertEqual(len(owned), 1)
            self.assertIsNotNone(owned[0].poll())


if __name__ == "__main__":
    unittest.main()
