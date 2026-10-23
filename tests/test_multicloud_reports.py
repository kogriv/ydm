"""Synthetic, offline coverage for provider-aware read-only reports."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ydm import Analyzer, DEFAULT_CONFIG, StorageManager


class ReportsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ydm_reports_")
        self.db = os.path.join(self.tmp, "monitor.db")
        self.storage = StorageManager(self.db, use_temp_storage=False, config=dict(DEFAULT_CONFIG))
        self.assertTrue(self.storage.init_db()[0])
        self.analyzer = Analyzer(self.storage)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self, kind, **source):
        ident = self.storage.start_scan(kind, **source)
        self.storage.finish_scan(ident, "success", 0)
        return ident

    def rows(self, scan, values):
        self.storage.save_files_batch([dict(value, scan_id=scan) for value in values])

    def test_status_list_and_info_expose_scan_source(self):
        scan = self.scan("cloud", target_name="synthetic-drive", provider="google_drive",
                         inventory_kind="rclone", remote_name="synthetic")
        self.assertEqual(self.analyzer.get_status()[0]["provider"], "google_drive")
        self.assertEqual(self.analyzer.get_scans_list()[0]["target"], "synthetic-drive")
        self.assertEqual(self.analyzer.get_scan_info(scan)["remote"], "synthetic")

    def test_diff_preserves_sibling_collision_and_classifies_evidence(self):
        cloud = self.scan("cloud")
        local = self.scan("local")
        self.rows(cloud, [
            {"parent_path": "/docs", "name": "same", "type": "file", "size": 1, "object_id": "a"},
            {"parent_path": "/docs", "name": "same", "type": "file", "size": 2, "object_id": "b"},
            {"parent_path": "/docs", "name": "hash", "type": "file", "size": 1,
             "object_id": "c", "hash_name": "sha1", "content_hash": "one"},
            {"parent_path": "/docs", "name": "native", "type": "file", "size": None,
             "object_id": "d", "is_native": True, "mime_type": "application/vnd.google-apps.document"},
        ])
        self.rows(local, [
            {"parent_path": "docs", "name": "same", "type": "file", "size": 1},
            {"parent_path": "docs", "name": "hash", "type": "file", "size": 1,
             "hash_name": "sha1", "content_hash": "one"},
            {"parent_path": "docs", "name": "native", "type": "file", "size": 1},
        ])
        report = self.analyzer.get_diff(cloud_scan_id=cloud, local_scan_id=local, use_composite=False, exclude_dirs=[])
        self.assertEqual(report["materialization_conflict_count"], 1)
        self.assertEqual(report["materialization_conflicts"][0]["status"], "materialization_conflict")
        self.assertEqual(report["strong_comparison_count"], 1)
        self.assertEqual(report["uncomparable_count"], 1)
        self.assertNotIn("/docs/native", report["missing_local_sample"])

    def test_typed_hash_duplicates_do_not_mix_types_and_count_unhashable(self):
        scan = self.scan("cloud")
        self.rows(scan, [
            {"parent_path": "/", "name": "one", "type": "file", "size": 1, "object_id": "1", "hash_name": "sha1", "content_hash": "x"},
            {"parent_path": "/", "name": "two", "type": "file", "size": 1, "object_id": "2", "hash_name": "sha1", "content_hash": "x"},
            {"parent_path": "/", "name": "md5", "type": "file", "size": 1, "object_id": "3", "hash_name": "md5", "content_hash": "x"},
            {"parent_path": "/", "name": "native", "type": "file", "size": None, "object_id": "4"},
        ])
        report = self.analyzer.get_duplicates(scan)
        self.assertEqual(report["total_duplicates"], 1)
        self.assertEqual(report["duplicates"][0]["hash_type"], "sha1")
        self.assertEqual(report["unhashable_count"], 1)

    def test_inventory_tree_and_long_paths_do_not_need_local_root(self):
        scan = self.scan("cloud")
        self.rows(scan, [{"parent_path": "/synthetic", "name": "x" * 12, "type": "file",
                          "size": 1, "object_id": "id"}])
        self.assertFalse(self.analyzer.get_inventory_tree(scan)["local_root_required"])
        self.assertEqual(self.analyzer.get_long_paths(scan, limit_chars=5)["long_paths_count"], 1)

    def test_incomplete_cloud_scan_is_never_authoritative_for_diff(self):
        cloud = self.storage.start_scan("cloud")
        local = self.scan("local")
        self.rows(cloud, [{"parent_path": "/partial", "name": "unsafe", "type": "file", "size": 1}])
        self.storage.update_folder_status(cloud, "/failed", "in_progress")
        self.storage.finish_scan(cloud, "failed", 0)
        explicit = self.analyzer.get_diff(
            cloud_scan_id=cloud, local_scan_id=local, use_composite=False, exclude_dirs=[]
        )
        fallback = self.analyzer.get_diff(
            local_scan_id=local, use_composite=False, exclude_dirs=[]
        )
        self.assertEqual(explicit["error"], "incomplete_scan")
        self.assertEqual(fallback["error"], "incomplete_scan")
        self.assertNotIn("missing_local_count", explicit)

    def test_empty_hash_values_are_not_strong_and_timestamps_normalize(self):
        cloud = self.scan("cloud")
        local = self.scan("local")
        instant = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        local_wall = instant.astimezone().replace(tzinfo=None)
        self.rows(cloud, [{"parent_path": "/docs", "name": "same", "type": "file",
                           "size": 7, "hash_name": "sha1", "content_hash": None,
                           "modified": instant}])
        self.rows(local, [{"parent_path": "docs", "name": "same", "type": "file",
                           "size": 7, "hash_name": "sha1", "content_hash": None,
                           "modified": local_wall}])
        report = self.analyzer.get_diff(
            cloud_scan_id=cloud, local_scan_id=local, use_composite=False, exclude_dirs=[]
        )
        self.assertEqual(report["strong_comparison_count"], 0)
        self.assertEqual(report["comparisons"][0]["status"], "weak_match")


if __name__ == "__main__":
    unittest.main()
