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
        self.assertEqual(
            {item["reason"] for item in result["comparisons"]},
            {"native_document", "shortcut"},
        )

    def test_export_plan_is_synthetic_and_download_only(self):
        plan = provider_for("google_drive").export_plan(
            object_id="synthetic-native", mime_type="application/vnd.google-apps.spreadsheet",
            artifact_root="/tmp/synthetic-artifacts",
        )
        self.assertEqual(plan["schema"], "ydm_native_export_plan:v1")
        self.assertEqual(plan["mode"], "download_only")
        self.assertEqual(plan["source_object_id"], "synthetic-native")
        self.assertTrue(plan["artifact_path"].endswith("synthetic-native.xlsx"))
        self.assertIsNone(sync_policy.validate_export_plan(plan))

    def test_rclone_export_mime_keeps_native_source_mime_from_metadata(self):
        item = provider_for("google_drive").normalize_rclone_item({
            "Name": "native.docx", "Size": -1, "IsDir": False,
            "MimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "ID": "synthetic-native",
            "Metadata": {"content-type": "application/vnd.google-apps.document"},
        })
        self.assertTrue(item["is_native"])
        self.assertEqual(item["mime_type"], "application/vnd.google-apps.document")

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
        self.assertIn("unsupported schema", sync_policy.validate_native_policy(args, export_policy))

    def test_force_cannot_override_native_bidirectional_block(self):
        args = argparse.Namespace(
            db_path=self.db, local_root=self.tmp.name,
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            path="/docs", mode="bidirectional", force_risk=True,
            apply=True, export_plan=None, backend="rclone", remote="synthetic",
        )
        risk = {
            "safe_for_bidirectional": False,
            "recommended_mode": "download_only",
            "risks": [{"code": "native_documents", "severity": "blocker"}],
            "scan_id": self.scan,
        }
        with mock.patch("tools.sync_policy.inspect_path", return_value=risk):
            result = sync_policy.add_policy_path(args)
        self.assertIn("cannot use bidirectional", result["error"])
        self.assertFalse(os.path.exists(args.policy_path))

    def test_duplicate_siblings_block_policy_and_filter_writes(self):
        args = argparse.Namespace(
            db_path=self.db, local_root=self.tmp.name,
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            download_filter_path=os.path.join(self.tmp.name, "download.filters"),
            bisync_filter_path=os.path.join(self.tmp.name, "bisync.filters"),
            path="/docs", mode="bidirectional", force_risk=True,
            apply=True, export_plan=None, backend="rclone", remote="synthetic",
        )
        collision_risk = {
            "safe_for_bidirectional": False,
            "recommended_mode": "download_only",
            "risks": [{"code": "sanitization_collisions", "severity": "blocker"}],
            "scan_id": self.scan,
        }
        with mock.patch("tools.sync_policy.inspect_path", return_value=collision_risk):
            added = sync_policy.add_policy_path(args)
        self.assertIn("cannot use bidirectional", added["error"])
        self.assertFalse(os.path.exists(args.policy_path))
        policy = {"schema": sync_policy.SCHEMA, "paths": {"docs": {"mode": "bidirectional"}}}
        siblings = [
            sync_policy.CloudFile("/docs", "same", 1, object_id="synthetic-a"),
            sync_policy.CloudFile("/docs", "same", 1, object_id="synthetic-b"),
        ]
        with mock.patch(
            "tools.sync_policy.cloud_files_for_path",
            return_value=(self.scan, True, siblings),
        ):
            rendered = sync_policy.render_filters(args, policy)
        self.assertIn("materialization collisions", rendered["error"])
        self.assertFalse(os.path.exists(args.bisync_filter_path))

    def test_shortcuts_are_inventory_objects_not_descended_content(self):
        shortcut = sync_policy.CloudFile("/docs", "shortcut", 0, is_shortcut=True)
        self.assertTrue(shortcut.is_shortcut)
        self.assertEqual(shortcut.rel_path, "docs/shortcut")


if __name__ == "__main__":
    unittest.main()
