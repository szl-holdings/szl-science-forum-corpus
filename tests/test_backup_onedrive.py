import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import backup_onedrive as backup


def fixture():
    body = b'{"model_training_authorized":false}\n'
    name = "dataset/manifest.json"
    blob_id = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
    entries = [{"path": name, "bytes": len(body), "git_blob_id": blob_id,
                "sha256": hashlib.sha256(body).hexdigest()}]
    return "a" * 40, entries, {name: body}


class BackupTests(unittest.TestCase):
    def test_unsafe_member_names_are_rejected(self):
        for name in ("../private", "/absolute", "C:/private", "a\\b", "./alias",
                     "a//b", ".Git/config", "a\nsecret", ""):
            with self.subTest(name=name):
                self.assertFalse(backup.safe_member(name))

    def test_staged_archive_restores_exact_source_and_does_not_remove_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "original.txt"
            original.write_bytes(b"keep original")
            revision, entries, bodies = fixture()
            receipt = backup.stage(root, revision, entries, bodies)
            archive = Path(receipt["archive_path"])
            self.assertEqual(backup.verify_archive(archive, revision, entries), receipt["archive_sha256"])
            self.assertEqual(original.read_bytes(), b"keep original")
            self.assertFalse(receipt["remote_readback_verified"])
            self.assertEqual(receipt["source_files_removed"], 0)
            self.assertEqual(receipt["state"], "LOCAL_ARCHIVE_VERIFIED_AWAITING_PROVIDER_SYNC")
            self.assertEqual(list(archive.parent.glob("*.tmp")), [])

    def test_repeated_backups_do_not_overwrite_existing_snapshots(self):
        with tempfile.TemporaryDirectory() as temp:
            args = fixture()
            first = backup.stage(Path(temp), *args)
            saved = Path(first["archive_path"]).read_bytes()
            second = backup.stage(Path(temp), *args)
            self.assertNotEqual(first["archive_path"], second["archive_path"])
            self.assertEqual(Path(first["archive_path"]).read_bytes(), saved)

    def test_archive_with_extra_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            revision, entries, bodies = fixture()
            receipt = backup.stage(Path(temp), revision, entries, bodies)
            path = Path(receipt["archive_path"])
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("unexpected.txt", b"extra")
            with self.assertRaises(backup.BackupError):
                backup.verify_archive(path, revision, entries)

    def test_low_disk_space_rejects_before_creating_backup_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(backup.shutil, "disk_usage", return_value=type("Usage", (), {"free": 0})()):
                with self.assertRaisesRegex(backup.BackupError, "headroom"):
                    backup.stage(root, *fixture())
            self.assertFalse((root / "SZL-Corpus-Backups").exists())

    def test_reserved_manifest_name_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as temp:
            body = b"untrusted"
            entries = [{"path": "BACKUP_MANIFEST.json"}]
            with self.assertRaisesRegex(backup.BackupError, "reserved"):
                backup.stage(Path(temp), "a" * 40, entries, {"BACKUP_MANIFEST.json": body})

    def test_archive_hash_or_source_binding_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            revision, entries, bodies = fixture()
            receipt = backup.stage(Path(temp), revision, entries, bodies)
            path = Path(receipt["archive_path"])
            with self.assertRaises(backup.BackupError):
                backup.verify_archive(path, "c" * 40, entries)
            wrong = [{**entries[0], "sha256": "0" * 64}]
            with self.assertRaises(backup.BackupError):
                backup.verify_archive(path, revision, wrong)

    def test_wrong_source_bindings_are_rejected_before_folder_creation(self):
        for field, value in (("bytes", 0), ("git_blob_id", "b" * 40), ("sha256", "0" * 64)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                revision, entries, bodies = fixture()
                wrong = [{**entries[0], field: value}]
                with self.assertRaisesRegex(backup.BackupError, "bindings"):
                    backup.stage(Path(temp), revision, wrong, bodies)
                self.assertFalse((Path(temp) / "SZL-Corpus-Backups").exists())

    def test_duplicate_source_paths_are_rejected_before_folder_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            revision, entries, bodies = fixture()
            with self.assertRaisesRegex(backup.BackupError, "binding"):
                backup.stage(Path(temp), revision, entries * 2, bodies)
            self.assertFalse((Path(temp) / "SZL-Corpus-Backups").exists())

    def test_source_origin_is_required_without_printing_its_value(self):
        with patch.object(backup, "git", return_value=b"https://private.invalid/token\n"):
            with self.assertRaises(backup.BackupError) as caught:
                backup.snapshot()
        self.assertNotIn("private.invalid", str(caught.exception))
        self.assertNotIn("token", str(caught.exception))

    def test_linux_client_query_is_unavailable_not_in_sync(self):
        with patch.object(backup.sys, "platform", "linux"):
            state = backup.client_state(Path("never-read"))
        self.assertEqual(state["state"], "UNAVAILABLE")
        self.assertFalse(state["in_sync"])

    def test_plan_does_not_discover_or_write_to_onedrive(self):
        with patch.object(backup.sys, "argv", ["backup_onedrive"]), \
             patch.object(backup, "snapshot", return_value=fixture()), \
             patch.object(backup, "registered_onedrive") as root, \
             patch.object(backup, "stage") as stage, patch("builtins.print"):
            self.assertEqual(backup.main(), 0)
        root.assert_not_called()
        stage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
