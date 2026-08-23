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

from tools.sync_tree import count_cloud_files_for_path  # noqa: E402
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

    #: Every value display_marker() can return, with the state that produces
    #: it. Four of the nine used to be covered; the five that were not include
    #: `[.]`, the default every unmatched case falls into — a wrong branch
    #: order above it would return `[.]` and nothing would have noticed.
    #: These stay alongside the end-to-end checks in tests/test_sync_bench.py:
    #: this table says what the function decides, the bench says whether the
    #: right facts reach it.
    MARKER_TABLE = [
        ("[X]", dict(policy_mode="disabled", in_policy=True,
                     local_state_value="disabled",
                     has_synced_descendant=False, v1_sync_status="excluded")),
        ("[B]", dict(policy_mode="bidirectional", in_policy=True,
                     local_state_value="materialized",
                     has_synced_descendant=False, v1_sync_status="full")),
        ("[B?]", dict(policy_mode="bidirectional", in_policy=True,
                      local_state_value="missing",
                      has_synced_descendant=False, v1_sync_status="full")),
        ("[B~]", dict(policy_mode="bidirectional", in_policy=True,
                      local_state_value="partial",
                      has_synced_descendant=False, v1_sync_status="full")),
        ("[D]", dict(policy_mode="download_only", in_policy=True,
                     local_state_value="materialized",
                     has_synced_descendant=False, v1_sync_status="excluded")),
        ("[D?]", dict(policy_mode="download_only", in_policy=True,
                      local_state_value="missing",
                      has_synced_descendant=False, v1_sync_status="excluded")),
        ("[D?]", dict(policy_mode="download_only", in_policy=True,
                      local_state_value="cloud_only",
                      has_synced_descendant=False, v1_sync_status="excluded")),
        # A folder with a mode of its own keeps it even when synced folders sit
        # below. Without these two rows the [P] branch can be moved above the
        # mode branches and every test still passes — verified by mutation on
        # 2026-08-23, which is why they exist.
        ("[D]", dict(policy_mode="download_only", in_policy=True,
                     local_state_value="materialized",
                     has_synced_descendant=True, v1_sync_status="partial")),
        ("[B]", dict(policy_mode="bidirectional", in_policy=True,
                     local_state_value="materialized",
                     has_synced_descendant=True, v1_sync_status="partial")),
        ("[P]", dict(policy_mode=None, in_policy=False,
                     local_state_value="cloud_only",
                     has_synced_descendant=True, v1_sync_status="excluded")),
        ("[P]", dict(policy_mode=None, in_policy=False,
                     local_state_value="cloud_only",
                     has_synced_descendant=False, v1_sync_status="partial")),
        ("[L]", dict(policy_mode=None, in_policy=False,
                     local_state_value="orphan",
                     has_synced_descendant=False, v1_sync_status="excluded")),
        ("[L]", dict(policy_mode=None, in_policy=False,
                     local_state_value="materialized",
                     has_synced_descendant=False, v1_sync_status="excluded")),
        ("[.]", dict(policy_mode=None, in_policy=False,
                     local_state_value="cloud_only",
                     has_synced_descendant=False, v1_sync_status="excluded")),
    ]

    def test_display_markers(self):
        for expected, state in self.MARKER_TABLE:
            with self.subTest(expected=expected, **state):
                self.assertEqual(display_marker(**state), expected)

    def test_the_table_covers_every_marker(self):
        """A truth table that quietly stops being exhaustive is worse than none."""
        self.assertEqual(
            {expected for expected, _ in self.MARKER_TABLE},
            {"[B]", "[B?]", "[B~]", "[D]", "[D?]", "[X]", "[P]", "[L]", "[.]"},
        )

    def test_disabled_wins_over_everything_else(self):
        """`[X]` is checked first, and must stay first.

        A folder excluded from sync but sitting above synced ones would
        otherwise render `[P]` and read as participating in sync.
        """
        self.assertEqual(
            display_marker(
                policy_mode="disabled",
                in_policy=True,
                local_state_value="materialized",
                has_synced_descendant=True,
                v1_sync_status="partial",
            ),
            "[X]",
        )

    def test_a_mode_only_applies_to_its_own_entry(self):
        """`in_policy` is what separates "this folder" from "somewhere above it".

        Under whitelist semantics an inherited mode must not mark a child as
        synced in its own right — that is what makes `[L]` possible under a
        disabled ancestor.
        """
        self.assertEqual(
            display_marker(
                policy_mode="bidirectional",
                in_policy=False,
                local_state_value="orphan",
                has_synced_descendant=False,
                v1_sync_status="excluded",
            ),
            "[L]",
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


class CompositeFileCountTests(unittest.TestCase):
    """The composite resolves per folder: each folder is served by the newest
    scan that covered it, the base scan otherwise. Counting has to follow the
    same rule — the old implementation added recursive totals and subtracted
    recursive totals, which double-subtracted nested updates and matched
    siblings by raw prefix (`/pro` swallowed `/protein`)."""

    class _Storage:
        def __init__(self, path):
            self.path = path

        def get_connection(self):
            return sqlite3.connect(self.path)

    class _Snapshot:
        def __init__(self, base_scan_id, folder_updates):
            self.base_scan_id = base_scan_id
            self.folder_updates = folder_updates

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "cloud.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE files (id INTEGER PRIMARY KEY, scan_id INTEGER, "
            "parent_path TEXT, name TEXT, type TEXT, size INTEGER, md5 TEXT)"
        )
        rows = []
        # Base scan 1: two files in each folder, plus a same-prefix sibling.
        for folder in ("", "/pro", "/pro/a", "/pro/a/b", "/protein"):
            rows += [(1, folder, f"base{i}.txt", "file", 1, None) for i in range(2)]
        # Scan 2 refreshed /pro/a (3 files), scan 3 refreshed /pro/a/b (5).
        rows += [(2, "/pro/a", f"s2_{i}.txt", "file", 1, None) for i in range(3)]
        rows += [(3, "/pro/a/b", f"s3_{i}.txt", "file", 1, None) for i in range(5)]
        conn.executemany(
            "INSERT INTO files (scan_id, parent_path, name, type, size, md5) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()

        class FakeAnalyzer:
            pass

        self.analyzer = FakeAnalyzer()
        self.analyzer.storage = self._Storage(self.db_path)
        self.snapshot = self._Snapshot(1, {"/pro/a": 2, "/pro/a/b": 3})

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def count(self, path):
        return count_cloud_files_for_path(self.analyzer, self.snapshot, path)

    def test_nested_updates_are_counted_once_each(self):
        # /pro (2 from base) + /pro/a (3 from scan 2) + /pro/a/b (5 from scan 3)
        self.assertEqual(self.count("/pro"), 10)

    def test_updated_folder_uses_its_own_scan(self):
        self.assertEqual(self.count("/pro/a"), 8)   # 3 + 5
        self.assertEqual(self.count("/pro/a/b"), 5)

    def test_same_prefix_sibling_is_not_included(self):
        self.assertEqual(self.count("/protein"), 2)

    def test_root_covers_everything_including_root_level_files(self):
        # root 2 + /pro 2 + /pro/a 3 + /pro/a/b 5 + /protein 2
        self.assertEqual(self.count("/"), 14)

    def test_folder_without_an_update_falls_back_to_the_base(self):
        snapshot = self._Snapshot(1, {})
        self.assertEqual(count_cloud_files_for_path(self.analyzer, snapshot, "/pro"), 6)


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
