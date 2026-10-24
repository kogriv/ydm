#!/usr/bin/env python3
"""Synthetic regression coverage for the v2 inventory storage migration."""
import os
import json
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ydm import DEFAULT_CONFIG, RcloneClient, StorageManager


class StorageV2Test(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ydm_storage_v2_")
        self.db_path = os.path.join(self.tmpdir, "monitor.db")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def legacy_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE scans (id INTEGER PRIMARY KEY, scan_type TEXT NOT NULL,
              status TEXT NOT NULL, duration REAL);
            CREATE TABLE files (id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL,
              parent_path TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL,
              size INTEGER, md5 TEXT, created DATETIME, modified DATETIME);
            CREATE UNIQUE INDEX idx_files_unique ON files(scan_id, parent_path, name);
            INSERT INTO scans VALUES (1, 'cloud', 'success', 0);
            INSERT INTO files VALUES (1, 1, '/synthetic', 'legacy.txt', 'file',
              3, 'legacy-md5', NULL, NULL);
        """)
        conn.commit()
        return conn

    def test_transactional_idempotent_legacy_migration_and_md5_projection(self):
        conn = self.legacy_db()
        StorageManager._migrate_schema(conn)
        StorageManager._migrate_schema(conn)  # safe to resume after success
        columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
        self.assertTrue({"object_id", "parent_id", "mime_type", "hash_name",
                         "content_hash", "is_native", "is_shortcut", "metadata_json"} <= columns)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(conn.execute(
            "SELECT md5, hash_name, content_hash FROM files WHERE id=1"
        ).fetchone(), ("legacy-md5", "md5", "legacy-md5"))
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(files)")}
        self.assertIn("idx_files_object_id", indexes)
        self.assertIn("idx_files_path_legacy", indexes)
        self.assertNotIn("idx_files_unique", indexes)
        conn.close()

    def test_migration_rolls_back_on_fault(self):
        conn = self.legacy_db()
        def fail(stage, _conn):
            if stage == "columns_added":
                raise RuntimeError("synthetic migration fault")
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            StorageManager._migrate_schema(conn, fault_inject=fail)
        self.assertNotIn("object_id", {row[1] for row in conn.execute("PRAGMA table_info(files)")})
        self.assertIn("idx_files_unique", {row[1] for row in conn.execute("PRAGMA index_list(files)")})
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        conn.close()

    def test_identity_indexes_allow_name_siblings_but_reject_duplicate_object_id(self):
        storage = StorageManager(self.db_path, use_temp_storage=False, config=dict(DEFAULT_CONFIG))
        self.assertTrue(storage.init_db()[0])
        scan_id = storage.start_scan("cloud", target_name="synthetic-google",
                                     provider="google_drive", inventory_kind="rclone",
                                     remote_name="synthetic")
        storage.save_files_batch([
            {"scan_id": scan_id, "parent_path": "/docs", "name": "same-name",
             "type": "file", "size": 1, "object_id": "obj-a", "parent_id": "folder-a",
             "mime_type": "application/octet-stream", "hash_name": "sha1",
             "content_hash": "a", "metadata": {"synthetic": True}},
            {"scan_id": scan_id, "parent_path": "/docs", "name": "same-name",
             "type": "file", "size": 2, "object_id": "obj-b", "parent_id": "folder-a",
             "is_native": True, "is_shortcut": False},
        ])
        conn = storage.get_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT target_name, provider, inventory_kind, remote_name "
                                      "FROM scans WHERE id=?", (scan_id,)).fetchone(),
                         ("synthetic-google", "google_drive", "rclone", "synthetic"))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO files (scan_id,parent_path,name,type,object_id) "
                         "VALUES (?, '/docs', 'other', 'file', 'obj-a')", (scan_id,))
        conn.close()

    def test_reinitializing_v2_does_not_restore_legacy_name_uniqueness(self):
        storage = StorageManager(self.db_path, use_temp_storage=False, config=dict(DEFAULT_CONFIG))
        self.assertTrue(storage.init_db()[0])
        self.assertTrue(storage.init_db()[0])
        with sqlite3.connect(self.db_path) as conn:
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(files)")}
        self.assertIn("idx_files_object_id", indexes)
        self.assertIn("idx_files_path_legacy", indexes)
        self.assertNotIn("idx_files_unique", indexes)

    def test_failed_batch_does_not_leave_database_locked(self):
        storage = StorageManager(self.db_path, use_temp_storage=False, config=dict(DEFAULT_CONFIG))
        self.assertTrue(storage.init_db()[0])
        scan_id = storage.start_scan("cloud")
        rows = [
            {"scan_id": scan_id, "parent_path": "/docs", "name": "same",
             "type": "file", "object_id": None},
            {"scan_id": scan_id, "parent_path": "/docs", "name": "same",
             "type": "file", "object_id": None},
        ]
        with self.assertRaises(sqlite3.IntegrityError):
            storage.save_files_batch(rows)
        storage.finish_scan(scan_id, "failed", 0)
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT status FROM scans WHERE id=?", (scan_id,)
            ).fetchone()[0], "failed")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM files WHERE scan_id=?", (scan_id,)
            ).fetchone()[0], 0)

    def test_future_schema_refuses_writes(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA user_version=99")
        conn.commit()
        conn.close()
        storage = StorageManager(self.db_path, use_temp_storage=False, config=dict(DEFAULT_CONFIG))
        ok, message = storage.init_db()
        self.assertFalse(ok)
        self.assertIn("future", message)
        self.assertEqual(sqlite3.connect(self.db_path).execute("PRAGMA user_version").fetchone()[0], 99)

    @mock.patch("ydm.subprocess.run")
    def test_rclone_normalization_preserves_every_available_hash(self, run):
        run.return_value.stdout = json.dumps([{
            "Name": "fixture.bin", "Size": 3, "ModTime": "2026-01-01T00:00:00Z",
            "Hashes": {"sha1": "sha-one", "md5": "md-five"},
            "Metadata": {"synthetic": "true"},
        }])
        item = RcloneClient("synthetic")._list_folder("/")[0]
        self.assertEqual(item["hash_name"], "md5")
        self.assertEqual(item["content_hash"], "md-five")
        self.assertEqual(
            item["metadata"]["rclone_hashes"],
            {"sha1": "sha-one", "md5": "md-five"},
        )

    @unittest.skipUnless(os.path.exists("/dev/shm"), "tmpfs unavailable")
    def test_checkpoint_and_restore_keep_v2_fields(self):
        storage = StorageManager(self.db_path, use_temp_storage=True, config=dict(DEFAULT_CONFIG))
        try:
            self.assertTrue(storage.init_db()[0])
            scan_id = storage.start_scan("cloud", target_name="synthetic-google",
                                         provider="google_drive", inventory_kind="rclone",
                                         remote_name="synthetic")
            storage.save_files_batch([{"scan_id": scan_id, "parent_path": "/",
                "name": "duplicate", "type": "file", "size": 5, "object_id": "id-1",
                "parent_id": "root", "mime_type": "application/test", "hash_name": "sha1",
                "content_hash": "hash", "is_native": True, "metadata": {"fixture": "only"}}])
            storage.checkpoint_to_disk(force=True)
            disk = sqlite3.connect(self.db_path)
            self.assertEqual(disk.execute("SELECT object_id, mime_type, hash_name, content_hash, is_native, metadata_json FROM files").fetchone(),
                             ("id-1", "application/test", "sha1", "hash", 1, '{"fixture": "only"}'))
            disk.close()
            os.remove(storage.db_path)
            storage.restore_from_disk(scan_id)
            restored = sqlite3.connect(storage.db_path)
            self.assertEqual(restored.execute("SELECT object_id, parent_id, is_native FROM files").fetchone(),
                             ("id-1", "root", 1))
            restored.close()
        finally:
            if os.path.exists(storage.db_path):
                os.remove(storage.db_path)


if __name__ == "__main__":
    unittest.main()
