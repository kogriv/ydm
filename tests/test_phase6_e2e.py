"""Disposable filesystem E2E for exact download-only materialization."""
import argparse
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tools import sync_policy
from ydm import DEFAULT_CONFIG, StorageManager


@unittest.skipUnless(shutil.which("rclone"), "rclone unavailable")
class DownloadOnlyFilesystemE2E(unittest.TestCase):
    def test_only_eligible_files_copy_remote_to_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote_root = os.path.join(tmp, "remote")
            local_root = os.path.join(tmp, "local")
            state = os.path.join(tmp, "state")
            os.makedirs(state)
            os.makedirs(os.path.join(remote_root, "selected"))
            os.makedirs(os.path.join(remote_root, "disabled"))
            os.makedirs(os.path.join(remote_root, "collision"))
            with open(os.path.join(remote_root, "selected", "keep.txt"), "w") as handle:
                handle.write("selected")
            with open(os.path.join(remote_root, "selected", "native.gdoc"), "w") as handle:
                handle.write("must-not-copy")
            with open(os.path.join(remote_root, "disabled", "skip.txt"), "w") as handle:
                handle.write("disabled")
            with open(os.path.join(remote_root, "collision", "same"), "w") as handle:
                handle.write("collision")
            os.makedirs(local_root)
            with open(os.path.join(local_root, "local-only.txt"), "w") as handle:
                handle.write("preserved")

            config_path = os.path.join(tmp, "rclone.conf")
            with open(config_path, "w") as handle:
                handle.write(f"[synthetic]\ntype = alias\nremote = {remote_root}\n")

            db_path = os.path.join(state, "monitor.db")
            storage = StorageManager(db_path, use_temp_storage=False, config=dict(DEFAULT_CONFIG))
            storage.init_db()
            scan_id = storage.start_scan(
                "cloud", scan_root="/", target_name="drive", provider="google_drive",
                inventory_kind="rclone", remote_name="synthetic",
            )
            storage.save_files_batch([
                {"scan_id": scan_id, "parent_path": "", "name": "selected", "type": "dir", "size": 0, "object_id": "dir-selected"},
                {"scan_id": scan_id, "parent_path": "", "name": "disabled", "type": "dir", "size": 0, "object_id": "dir-disabled"},
                {"scan_id": scan_id, "parent_path": "", "name": "collision", "type": "dir", "size": 0, "object_id": "dir-collision"},
                {"scan_id": scan_id, "parent_path": "/selected", "name": "keep.txt", "type": "file", "size": 8, "object_id": "file-keep"},
                {"scan_id": scan_id, "parent_path": "/selected", "name": "native.gdoc", "type": "file", "size": None, "object_id": "file-native", "is_native": True},
                {"scan_id": scan_id, "parent_path": "/disabled", "name": "skip.txt", "type": "file", "size": 8, "object_id": "file-disabled"},
                {"scan_id": scan_id, "parent_path": "/collision", "name": "same", "type": "file", "size": 9, "object_id": "collision-a"},
                {"scan_id": scan_id, "parent_path": "/collision", "name": "same", "type": "file", "size": 9, "object_id": "collision-b"},
            ])
            storage.update_folder_status(scan_id, "/", "completed")
            storage.update_folder_status(scan_id, "/selected", "completed")
            storage.update_folder_status(scan_id, "/disabled", "completed")
            storage.update_folder_status(scan_id, "/collision", "completed")
            storage.finish_scan(scan_id, "success", 0)

            args = argparse.Namespace(
                target_managed=True, target="drive", provider="google_drive",
                inventory_kind="rclone", sync_kind="rclone", remote="synthetic",
                local_root=local_root, db_path=db_path, apply=True,
                download_filter_path=os.path.join(state, "download.filters"),
                bisync_filter_path=os.path.join(state, "bisync.filters"),
            )
            policy = sync_policy.empty_policy(
                local_root, "synthetic", target="drive", provider="google_drive",
                inventory_kind="rclone", sync_kind="rclone",
            )
            policy["paths"] = {
                "selected": {"mode": "download_only"},
                "collision": {"mode": "download_only"},
                "disabled": {"mode": "disabled"},
            }
            with mock.patch.dict(os.environ, {"RCLONE_CONFIG": config_path}):
                result = sync_policy.download_apply(args, policy)
            self.assertIsNone(result["error"], result)
            self.assertTrue(os.path.exists(os.path.join(local_root, "selected", "keep.txt")))
            self.assertFalse(os.path.exists(os.path.join(local_root, "selected", "native.gdoc")))
            self.assertFalse(os.path.exists(os.path.join(local_root, "disabled", "skip.txt")))
            self.assertFalse(os.path.exists(os.path.join(local_root, "collision", "same")))
            self.assertTrue(os.path.exists(os.path.join(local_root, "local-only.txt")))
            self.assertTrue(os.path.exists(os.path.join(remote_root, "selected", "keep.txt")))
            self.assertTrue(result["manifest_path"].startswith(state + os.sep))


if __name__ == "__main__":
    unittest.main()
