#!/usr/bin/env python3
"""Unit tests for sync_tree policy overlay and cloud helpers."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.sync_tree_cloud import fetch_child_names, infer_dirs_from_files  # noqa: E402
from tools.sync_tree_policy import (  # noqa: E402
    display_marker,
    effective_policy_state,
    load_policy_context,
    local_state,
    path_in_policy,
    policy_mode_for_path,
    policy_summary_line,
)


class BlacklistSemanticsTests(unittest.TestCase):
    """The daemon's policy is a blacklist: everything not disabled is synced.

    Treating it as a whitelist made every synced folder render as `[L]` (local
    orphan) — the policy of a daemon host holds only `disabled` entries, so the
    whitelist was always empty.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.policy_path = os.path.join(self.tmpdir, "sync_policy.json")
        with open(self.policy_path, "w", encoding="utf-8") as handle:
            json.dump({
                "schema": "ydm_sync_policy:v1",
                "paths": {
                    "Books": {"mode": "disabled"},
                    "video/Обучение": {"mode": "disabled"},
                },
            }, handle, ensure_ascii=False)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _ctx(self, blacklist):
        return load_policy_context(
            self.policy_path, self.tmpdir, blacklist_semantics=blacklist
        )

    def test_daemon_treats_everything_not_disabled_as_bidirectional(self):
        ctx = self._ctx(True)
        self.assertEqual(effective_policy_state("/pro", ctx), ("bidirectional", True))
        self.assertEqual(effective_policy_state("/video", ctx), ("bidirectional", True))
        self.assertEqual(effective_policy_state("/", ctx), ("bidirectional", True))

    def test_daemon_inherits_disabled_from_an_ancestor(self):
        ctx = self._ctx(True)
        self.assertEqual(effective_policy_state("/Books", ctx), ("disabled", True))
        self.assertEqual(effective_policy_state("/Books/Math", ctx), ("disabled", True))
        self.assertEqual(
            effective_policy_state("/video/Обучение/x", ctx), ("disabled", True)
        )

    def test_rclone_keeps_whitelist_semantics(self):
        ctx = self._ctx(False)
        # Not in the policy at all -> not covered, which is what [L] needs.
        self.assertEqual(effective_policy_state("/pro", ctx), (None, False))
        self.assertEqual(effective_policy_state("/Books", ctx), ("disabled", True))

    def test_markers_follow_from_the_state(self):
        ctx = self._ctx(True)
        mode, covered = effective_policy_state("/pro", ctx)
        self.assertEqual(
            display_marker(
                policy_mode=mode,
                in_policy=covered,
                local_state_value="materialized",
                has_synced_descendant=False,
                v1_sync_status="full",
            ),
            "[B]",
        )
        mode, covered = effective_policy_state("/Books", ctx)
        self.assertEqual(
            display_marker(
                policy_mode=mode,
                in_policy=covered,
                local_state_value="disabled",
                has_synced_descendant=False,
                v1_sync_status="excluded",
            ),
            "[X]",
        )


class SyncTreePolicyTests(unittest.TestCase):
    def test_policy_mode_longest_prefix(self):
        policy = {
            "schema": "ydm_sync_policy:v1",
            "paths": {
                "Books/Math/База": {"mode": "bidirectional"},
                "Books/Math": {"mode": "download_only"},
            },
        }
        self.assertEqual(policy_mode_for_path("/Books/Math/База", policy), "bidirectional")
        self.assertEqual(policy_mode_for_path("/Books/Math/База/sub", policy), "bidirectional")
        self.assertTrue(path_in_policy("/Books/Math/База", policy))
        self.assertTrue(path_in_policy("/Books/Math", policy))
        self.assertEqual(policy_mode_for_path("/Books/Math", policy), "download_only")

    def test_display_markers(self):
        self.assertEqual(
            display_marker(
                policy_mode="bidirectional",
                in_policy=True,
                local_state_value="materialized",
                has_synced_descendant=False,
                v1_sync_status="full",
            ),
            "[B]",
        )
        self.assertEqual(
            display_marker(
                policy_mode=None,
                in_policy=False,
                local_state_value="orphan",
                has_synced_descendant=False,
                v1_sync_status="excluded",
            ),
            "[L]",
        )
        self.assertEqual(
            display_marker(
                policy_mode="download_only",
                in_policy=True,
                local_state_value="missing",
                has_synced_descendant=False,
                v1_sync_status="excluded",
            ),
            "[D?]",
        )

    def test_local_state_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            orphan = os.path.join(tmp, "Books", "Math", "АнГем")
            os.makedirs(orphan)
            state = local_state(
                policy_mode=None,
                in_policy=False,
                cloud_count=0,
                local_count=0,
                local_root=tmp,
                path="/Books/Math/АнГем",
            )
            self.assertEqual(state, "orphan")

    def test_policy_summary(self):
        import tempfile
        import json
        tmpdir = tempfile.mkdtemp()
        policy_path = os.path.join(tmpdir, "sync_policy.json")
        with open(policy_path, "w", encoding="utf-8") as handle:
            json.dump({
                "schema": "ydm_sync_policy:v1",
                "paths": {
                    "Books/Math/База": {"mode": "bidirectional"},
                }
            }, handle, ensure_ascii=False)
        ctx = load_policy_context(policy_path, "/sdcard/Download/ya_disk")
        summary = policy_summary_line(ctx)
        self.assertIn("[B]", summary)
        self.assertIn("Books/Math/База", summary)
        shutil.rmtree(tmpdir, ignore_errors=True)


class SyncTreeCloudTests(unittest.TestCase):
    def _make_db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE files (
                id INTEGER PRIMARY KEY,
                scan_id INTEGER,
                parent_path TEXT,
                name TEXT,
                type TEXT,
                size INTEGER,
                md5 TEXT,
                created TEXT,
                modified TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO files VALUES (1, 1, '/Books/Math', 'АнГем', 'dir', 0, NULL, NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO files VALUES (2, 1, '/Books/Math/АнГем', 'book.pdf', 'file', 1, NULL, NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO files VALUES (3, 1, '/Books/Math', 'База', 'dir', 0, NULL, NULL, NULL)"
        )
        conn.commit()
        conn.close()
        return path

    def test_infer_dirs_from_files(self):
        db_path = self._make_db()

        class Storage:
            def __init__(self, p):
                self._p = p

            def get_connection(self):
                return sqlite3.connect(self._p)

        storage = Storage(db_path)
        names = infer_dirs_from_files(storage, 1, "/Books/Math")
        self.assertIn("АнГем", names)
        self.assertIn("База", names)

    def test_fetch_child_names_prefers_dir_rows(self):
        db_path = self._make_db()

        class Storage:
            def __init__(self, p):
                self._p = p

            def get_connection(self):
                return sqlite3.connect(self._p)

        storage = Storage(db_path)
        names = fetch_child_names(storage, 1, "/Books/Math")
        self.assertEqual(sorted(names), ["АнГем", "База"])
        os.unlink(db_path)


class SyncTreeCliSmokeTests(unittest.TestCase):
    def test_help_and_v1_schema(self):
        import subprocess

        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools/sync_tree.py"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--schema", proc.stdout)
        self.assertIn("sync_tree:v2", proc.stdout)


if __name__ == "__main__":
    unittest.main()
