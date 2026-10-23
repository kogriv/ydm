"""Synthetic Phase 5 native-document and shortcut safety coverage."""
import argparse
import os
import tempfile
import unittest
from unittest import mock

from tools.providers import provider_for
from tools import sync_policy
from ydm import Analyzer, DEFAULT_CONFIG, StorageManager


class NativeDocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "monitor.db")
        self.storage = StorageManager(self.db, use_temp_storage=False, config=dict(DEFAULT_CONFIG))
        self.storage.init_db()
        self.scan = self.storage.start_scan("cloud", scan_root="/")
        self.storage.save_files_batch([
            {"scan_id": self.scan, "parent_path": "/docs", "name": "ordinary.bin", "type": "file", "size": 3, "object_id": "synthetic-file"},
            {"scan_id": self.scan, "parent_path": "/docs", "name": "native-sheet", "type": "file", "size": None,
             "object_id": "synthetic-native", "mime_type": "application/vnd.google-apps.spreadsheet", "is_native": True},
            {"scan_id": self.scan, "parent_path": "/docs", "name": "shortcut", "type": "shortcut", "size": 0,
             "object_id": "synthetic-shortcut", "mime_type": "application/vnd.google-apps.shortcut", "is_shortcut": True},
        ])
        self.storage.update_folder_status(self.scan, "/", "completed")
        self.storage.finish_scan(self.scan, "success", 0)
        self.local = self.storage.start_scan("local")
        self.storage.finish_scan(self.local, "success", 0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_metadata_native_and_shortcut_are_not_missing_local(self):
        result = Analyzer(self.storage).get_diff(cloud_scan_id=self.scan, local_scan_id=self.local, use_composite=False)
        self.assertEqual(result["missing_local_sample"], ["/docs/ordinary.bin"])

    def test_export_plan_is_synthetic_and_download_only(self):
        plan = provider_for("google_drive").export_plan(
            object_id="synthetic-native", mime_type="application/vnd.google-apps.spreadsheet",
            artifact_root="/tmp/synthetic-artifacts",
        )
        self.assertEqual(plan["schema"], "ydm_native_export_plan:v1")
        self.assertEqual(plan["mode"], "download_only")
        self.assertEqual(plan["source_object_id"], "synthetic-native")
        self.assertTrue(plan["artifact_path"].endswith("synthetic-native.xlsx"))

    def test_bidirectional_native_or_export_is_rejected_before_filter_write(self):
        args = argparse.Namespace(db_path=self.db, local_root=self.tmp.name,
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            download_filter_path=os.path.join(self.tmp.name, "download.filters"),
            bisync_filter_path=os.path.join(self.tmp.name, "bisync.filters"), apply=True)
        policy = {"schema": sync_policy.SCHEMA, "paths": {"docs": {"mode": "bidirectional"}}}
        native = sync_policy.CloudFile("/docs", "native-sheet", 0, is_native=True)
        with mock.patch("tools.sync_policy.cloud_files_for_path", return_value=(self.scan, True, [native])):
            result = sync_policy.render_filters(args, policy)
        self.assertIn("rejects native", result["error"])
        self.assertFalse(os.path.exists(args.download_filter_path))
        export_policy = {"schema": sync_policy.SCHEMA, "paths": {"docs": {
            "mode": "bidirectional", "export_plan": {"mode": "download_only"}}}}
        self.assertIn("requires download_only", sync_policy.validate_native_policy(args, export_policy))

    def test_shortcuts_are_inventory_objects_not_descended_content(self):
        shortcut = sync_policy.CloudFile("/docs", "shortcut", 0, is_shortcut=True)
        self.assertTrue(shortcut.is_shortcut)
        self.assertEqual(shortcut.rel_path, "docs/shortcut")


if __name__ == "__main__":
    unittest.main()
