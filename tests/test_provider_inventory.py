"""Synthetic tests for provider-aware, read-only inventory."""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout
from io import StringIO

from ydm import CloudScanner, InventoryError, RcloneClient, StorageManager, YDM_CLI
from tools.providers import provider_for


class ProviderInventoryTests(unittest.TestCase):
    def test_google_native_and_shortcut_classification(self):
        provider = provider_for("google_drive")
        native = provider.normalize_rclone_item({"Name": "sheet", "Size": -1,
            "MimeType": "application/vnd.google-apps.spreadsheet", "ID": "synthetic-native"})
        shortcut = provider.normalize_rclone_item({"Name": "link", "Size": 1,
            "MimeType": "application/vnd.google-apps.shortcut", "ID": "synthetic-shortcut"})
        ordinary = provider.normalize_rclone_item({"Name": "file", "Size": 2,
            "MimeType": "application/octet-stream", "ID": "synthetic-file"})
        self.assertTrue(native["is_native"])
        self.assertIsNone(native["size"])
        self.assertEqual(shortcut["type"], "shortcut")
        self.assertTrue(shortcut["is_shortcut"])
        self.assertFalse(ordinary["is_native"])

    @mock.patch("ydm.subprocess.run")
    def test_rclone_retry_is_bounded_and_cache_only_after_success(self, run):
        run.side_effect = [
            __import__("subprocess").CalledProcessError(1, ["rclone"]),
            mock.Mock(stdout=json.dumps([{ "Name": "file", "Size": 1,
                "ModTime": "2026-01-01T00:00:00Z", "ID": "synthetic-id"}])),
        ]
        client = RcloneClient("synthetic", provider="google_drive", retry_attempts=2)
        self.assertEqual(client.get_resources("/")["total"], 1)
        self.assertEqual(run.call_count, 2)
        client.get_resources("/")
        self.assertEqual(run.call_count, 2)

    @mock.patch("ydm.subprocess.run")
    def test_failed_listing_is_not_empty_or_cached(self, run):
        run.side_effect = __import__("subprocess").CalledProcessError(1, ["rclone"])
        client = RcloneClient("synthetic", retry_attempts=2)
        with self.assertRaises(InventoryError):
            client.get_resources("/broken")
        self.assertNotIn("/broken", client._folder_cache)
        self.assertEqual(run.call_count, 2)

    @mock.patch("ydm.subprocess.run")
    def test_google_missing_file_id_fails_instead_of_path_fallback(self, run):
        run.return_value.stdout = json.dumps([{ "Name": "missing-id", "Size": 1,
            "ModTime": "2026-01-01T00:00:00Z", "MimeType": "text/plain" }])
        with self.assertRaisesRegex(InventoryError, "stable object ID"):
            RcloneClient("synthetic", provider="google_drive").get_resources("/")

    def test_listing_failure_leaves_scan_resumable_not_completed(self):
        class FailingClient:
            def get_resources(self, *_args):
                raise InventoryError("synthetic listing failure")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "monitor.db")
            storage = StorageManager(path, use_temp_storage=False)
            storage.init_db()
            scan_id = storage.start_scan("cloud")
            with self.assertRaises(InventoryError):
                CloudScanner(FailingClient()).scan(scan_id, storage)
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("SELECT status FROM scan_progress WHERE scan_id=?", (scan_id,)).fetchone()[0], "in_progress")
            conn.close()

    @mock.patch.object(RcloneClient, "get_disk_info", return_value={"total_space": 1, "used_space": 0, "trash_size": 0})
    @mock.patch.object(RcloneClient, "get_resources", return_value={"items": [], "total": 0})
    def test_doctor_is_read_only_and_sanitizes_probe(self, _listing, _about):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.argv", ["ydm.py", "--format", "json", "--targets-file", os.path.join(tmp, "targets.json"), "targets", "doctor"]):
            config = {"schema": "ydm_targets:v1", "default_target": "synthetic", "targets": {
                "synthetic": {"provider": "google_drive", "inventory": {"kind": "rclone", "remote": "synthetic"},
                 "sync": {"kind": "rclone", "remote": "synthetic"}, "local_root": os.path.join(tmp, "local"),
                 "state_dir": os.path.join(tmp, "state"), "config_profile": "prod"}}}
            with open(os.path.join(tmp, "targets.json"), "w", encoding="utf-8") as handle:
                json.dump(config, handle)
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(YDM_CLI().run())
            payload = json.loads(out.getvalue())["data"]
            self.assertTrue(payload["read_only"])
            self.assertNotIn("total_space", json.dumps(payload))
            self.assertFalse(os.path.exists(os.path.join(tmp, "state")))


if __name__ == "__main__":
    unittest.main()
