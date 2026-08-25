#!/usr/bin/env python3
"""
Unit tests for Analyzer/StorageManager against synthetic data — no
network, no credentials, no tmpfs (StorageManager(use_temp_storage=False)
is a plain on-disk SQLite file, the same mode tools/sync_common.py's
create_storage() uses).

Run directly: python tests/test_analyzer.py -v
Or via CI: see .github/workflows/ci.yml

Fixtures insert rows directly through StorageManager.get_connection()
rather than replaying the full scan/checkpoint lifecycle — confirmed
equivalent for analysis purposes, and avoids depending on tmpfs/
checkpoint timing entirely.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# NOTE: importing ydm has a real, process-wide side effect — it registers
# SIGTERM/SIGUSR1 handlers and an atexit hook at module level (not gated
# behind `if __name__ == "__main__"`). Harmless for unittest, but worth
# knowing if this module is ever imported alongside something that relies
# on default signal handling.
from datetime import datetime, timezone  # noqa: E402

from ydm import Analyzer, StorageManager, DEFAULT_CONFIG  # noqa: E402


class AnalyzerTestCase(unittest.TestCase):
    """Base fixture: an isolated on-disk SQLite DB, fresh schema, a bound
    Analyzer, and exclude_config pointed at a guaranteed-nonexistent path
    so get_diff()'s exclude-dirs filtering can't pick up a real
    ~/.config/yandex-disk/config.cfg that happens to exist on whoever's
    machine runs these tests."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ydm_test_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.config = dict(DEFAULT_CONFIG)
        self.storage = StorageManager(self.db_path, use_temp_storage=False, config=self.config)
        ok, msg = self.storage.init_db()
        self.assertTrue(ok, msg)
        self.analyzer = Analyzer(self.storage)
        self.conn = self.storage.get_connection()

        self._orig_exclude_config = DEFAULT_CONFIG["exclude_config"]
        DEFAULT_CONFIG["exclude_config"] = os.path.join(self.tmpdir, "nonexistent-yandex-disk-config.cfg")

    def tearDown(self):
        DEFAULT_CONFIG["exclude_config"] = self._orig_exclude_config
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- fixture helpers -----------------------------------------------

    def insert_scan(self, scan_id, scan_type, status, timestamp=None, duration=0,
                    scan_root=None, scan_depth=None):
        """Insert a scan row.

        `scan_root`/`scan_depth` default to NULL, which is what every scan
        written before those columns existed carries — so tests that omit them
        exercise the inference path the old databases still rely on.
        """
        if timestamp is not None:
            self.conn.execute(
                "INSERT INTO scans (id, timestamp, scan_type, status, duration, scan_root, scan_depth)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (scan_id, timestamp, scan_type, status, duration, scan_root, scan_depth),
            )
        else:
            self.conn.execute(
                "INSERT INTO scans (id, scan_type, status, duration, scan_root, scan_depth)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (scan_id, scan_type, status, duration, scan_root, scan_depth),
            )
        self.conn.commit()

    def insert_files(self, scan_id, rows):
        """rows: iterable of (parent_path, name, type, size, md5)."""
        self.conn.executemany(
            "INSERT INTO files (scan_id, parent_path, name, type, size, md5) VALUES (?, ?, ?, ?, ?, ?)",
            [(scan_id, p, n, t, s, m) for (p, n, t, s, m) in rows],
        )
        self.conn.commit()

    def insert_progress(self, scan_id, path, status="completed", last_checked=None):
        self.conn.execute(
            "INSERT INTO scan_progress (scan_id, path, status, last_checked) VALUES (?, ?, ?, ?)",
            (scan_id, path, status, last_checked),
        )
        self.conn.commit()


class TestStorageManagerBasics(AnalyzerTestCase):
    def test_init_db_creates_all_tables(self):
        tables = {
            row[0]
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for expected in ("scans", "disk_info", "files", "scan_progress"):
            self.assertIn(expected, tables)

    def test_start_scan_and_finish_scan(self):
        scan_id = self.storage.start_scan("cloud")
        self.assertIsInstance(scan_id, int)
        row = self.conn.execute(
            "SELECT scan_type, status, duration FROM scans WHERE id=?", (scan_id,)
        ).fetchone()
        self.assertEqual(row, ("cloud", "started", 0))

        self.storage.finish_scan(scan_id, "success", 12.5)
        row = self.conn.execute("SELECT status, duration FROM scans WHERE id=?", (scan_id,)).fetchone()
        self.assertEqual(row, ("success", 12.5))

    def test_get_status_orders_most_recent_first_and_caps_at_5(self):
        for i in range(1, 8):
            self.insert_scan(i, "cloud", "success")
        status = self.analyzer.get_status()
        self.assertEqual(len(status), 5)
        self.assertEqual([s["id"] for s in status], [7, 6, 5, 4, 3])


class TestStartScanOnAPreExistingDatabase(unittest.TestCase):
    """A database written before `scans.scan_root` existed must still take a
    scan.

    Not hypothetical: the migration is only reachable from `init_db()` /
    `_init_final_db()`, and `tools/sync_common.py` builds a StorageManager
    with `use_temp_storage=False`, which calls neither. So `ydm.py scan local`
    worked — it goes through tmpfs, which migrates on the way — while every
    tool-driven scan died with `table scans has no column named scan_root` on
    any database that predated the columns.

    Worse than a crash, because of where it landed: the Android job treats a
    failing rename preflight as "sync anyway", so the guard was silently absent
    while the log kept saying `run OK`.
    """

    #: The `scans` table exactly as it was before scan_root/scan_depth.
    OLD_SCHEMA = """
        CREATE TABLE scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            scan_type TEXT NOT NULL,
            status TEXT NOT NULL,
            duration REAL
        )
    """

    def setUp(self):
        import sqlite3

        self.tmpdir = tempfile.mkdtemp(prefix="ydm_oldschema_")
        self.db_path = os.path.join(self.tmpdir, "old.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute(self.OLD_SCHEMA)
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_a_tool_can_start_a_scan_without_running_init_first(self):
        import sqlite3

        storage = StorageManager(self.db_path, use_temp_storage=False,
                                 config=dict(DEFAULT_CONFIG))
        scan_id = storage.start_scan("local", scan_root="/mirror")

        conn = sqlite3.connect(self.db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(scans)")}
            self.assertIn("scan_root", columns)
            self.assertIn("scan_depth", columns)
            row = conn.execute(
                "SELECT scan_type, scan_root, scan_depth FROM scans WHERE id=?",
                (scan_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("local", "/mirror", None))


class TestDiffSimple(AnalyzerTestCase):
    def test_missing_local_and_missing_cloud(self):
        self.insert_scan(1, "cloud", "success")
        self.insert_scan(2, "local", "success")
        self.insert_files(1, [
            ("/A", "only_cloud.txt", "file", 10, None),
            ("/A", "shared.txt", "file", 20, None),
        ])
        self.insert_files(2, [
            ("/A", "shared.txt", "file", 20, None),
            ("/A", "only_local.txt", "file", 30, None),
        ])
        result = self.analyzer.get_diff(cloud_scan_id=1, local_scan_id=2, use_composite=False)
        self.assertEqual(result["missing_local_count"], 1)
        self.assertEqual(result["missing_local_sample"], ["/A/only_cloud.txt"])
        self.assertEqual(result["missing_cloud_count"], 1)
        self.assertEqual(result["missing_cloud_sample"], ["/A/only_local.txt"])

    def test_exclude_dirs_filters_top_level(self):
        self.insert_scan(1, "cloud", "success")
        self.insert_scan(2, "local", "success")
        self.insert_files(1, [
            ("/Excluded", "secret.txt", "file", 5, None),
            ("/Kept", "visible.txt", "file", 5, None),
        ])
        with open(DEFAULT_CONFIG["exclude_config"], "w") as f:
            f.write("exclude-dirs=Excluded\n")
        result = self.analyzer.get_diff(cloud_scan_id=1, local_scan_id=2, use_composite=False)
        self.assertEqual(result["missing_local_sample"], ["/Kept/visible.txt"])

    def test_local_scan_id_must_be_successful_local_scan(self):
        self.insert_scan(1, "cloud", "success")
        self.insert_scan(2, "local", "started")
        result = self.analyzer.get_diff(cloud_scan_id=1, local_scan_id=2)
        self.assertIn("error", result)

    def test_no_local_scans_at_all(self):
        self.insert_scan(1, "cloud", "success")
        result = self.analyzer.get_diff(cloud_scan_id=1)
        self.assertIn("error", result)


class TestDiffPathConventions(AnalyzerTestCase):
    """Cloud scans store parent_path as "/A/B", local scans as "A/B".

    `report diff` compared them raw, so the two sets could never intersect and
    every file was reported missing on both sides. The tests that existed used
    "/A" on both sides — which is why nothing caught it for as long as it did.
    """

    def _cloud_and_local(self):
        self.insert_scan(1, "cloud", "success")
        self.insert_scan(2, "local", "success")

    def test_the_same_file_matches_across_conventions(self):
        self._cloud_and_local()
        self.insert_files(1, [("/pro/MuSy", "actor.hpp", "file", 10, None)])
        self.insert_files(2, [("pro/MuSy", "actor.hpp", "file", 10, None)])
        result = self.analyzer.get_diff(cloud_scan_id=1, local_scan_id=2, use_composite=False)
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["missing_local_count"], 0)
        self.assertEqual(result["missing_cloud_count"], 0)

    def test_root_level_files_match(self):
        self._cloud_and_local()
        self.insert_files(1, [("", "readme.txt", "file", 1, None)])
        self.insert_files(2, [("", "readme.txt", "file", 1, None)])
        result = self.analyzer.get_diff(cloud_scan_id=1, local_scan_id=2, use_composite=False)
        self.assertEqual(result["matched_count"], 1)

    def test_genuinely_missing_files_are_still_reported(self):
        self._cloud_and_local()
        self.insert_files(1, [
            ("/pro", "shared.txt", "file", 1, None),
            ("/pro", "cloud_only.txt", "file", 1, None),
        ])
        self.insert_files(2, [
            ("pro", "shared.txt", "file", 1, None),
            ("pro", "local_only.txt", "file", 1, None),
        ])
        result = self.analyzer.get_diff(cloud_scan_id=1, local_scan_id=2, use_composite=False)
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["missing_local_sample"], ["/pro/cloud_only.txt"])
        self.assertEqual(result["missing_cloud_sample"], ["/pro/local_only.txt"])

    def test_counts_add_up(self):
        self._cloud_and_local()
        self.insert_files(1, [
            ("/keep", "a.txt", "file", 1, None),
            ("/keep", "b.txt", "file", 1, None),
            ("/drop", "c.txt", "file", 1, None),
        ])
        self.insert_files(2, [("keep", "a.txt", "file", 1, None)])
        with open(DEFAULT_CONFIG["exclude_config"], "w") as handle:
            handle.write("exclude-dirs=drop\n")
        result = self.analyzer.get_diff(cloud_scan_id=1, local_scan_id=2, use_composite=False)
        self.assertEqual(
            result["cloud_files_count"],
            result["matched_count"]
            + result["excluded_from_sync_count"]
            + result["missing_local_count"],
        )

    def test_local_counts_add_up_too(self):
        """The local side has to balance as well.

        It did not: files under `.sync` were skipped without being counted, so
        14 138 local files reconciled to 14 104 and the missing 34 appeared
        nowhere. An undeclared hole on one side of a comparison is how the
        defects this suite exists for stayed invisible.
        """
        self._cloud_and_local()
        self.insert_files(1, [("/keep", "a.txt", "file", 1, None)])
        self.insert_files(2, [
            ("keep", "a.txt", "file", 1, None),
            ("keep", "local_only.txt", "file", 1, None),
            (".sync", "core.log", "file", 1, None),
            (".sync/nested", "core-1.log.gz", "file", 1, None),
        ])
        result = self.analyzer.get_diff(cloud_scan_id=1, local_scan_id=2, use_composite=False)
        self.assertEqual(result["local_ignored_count"], 2)
        self.assertEqual(
            result["local_files_count"],
            result["matched_count"]
            + result["local_ignored_count"]
            + result["missing_cloud_count"],
        )


class TestDiffInvariant(AnalyzerTestCase):
    """Invariant: a cloud scan and a local scan describing the *same* tree
    must diff to nothing.

    This is the test that would have caught the path-convention defect on day
    one. Every earlier diff test constructed both sides with the same string,
    so they agreed by accident rather than by comparison. Here each side is
    written in its own native convention, exactly as its scanner stores it.
    """

    TREE = [
        ("", "root.txt"),
        ("pro", "a.txt"),
        ("pro/MuSy", "actor.hpp"),
        ("pro/MuSy/.vscode", "settings.json"),
        ("Books/Math/АнГем", "book.pdf"),
        ("brtn/Перс/Дом", "скан.pdf"),
    ]

    def _populate(self, cloud_id, local_id, tree):
        self.insert_scan(cloud_id, "cloud", "success")
        self.insert_scan(local_id, "local", "success")
        # Cloud stores "/pro/MuSy"; local stores "pro/MuSy". Root is "" in both.
        self.insert_files(cloud_id, [
            (f"/{parent}" if parent else "", name, "file", 7, "md5")
            for parent, name in tree
        ])
        self.insert_files(local_id, [
            (parent, name, "file", 7, "md5") for parent, name in tree
        ])

    def test_identical_trees_diff_to_nothing(self):
        self._populate(1, 2, self.TREE)
        result = self.analyzer.get_diff(cloud_scan_id=1, local_scan_id=2, use_composite=False)
        self.assertEqual(result["matched_count"], len(self.TREE))
        self.assertEqual(result["missing_local_count"], 0, result["missing_local_sample"])
        self.assertEqual(result["missing_cloud_count"], 0, result["missing_cloud_sample"])

    def test_the_invariant_still_detects_a_single_difference(self):
        """A guard that reports zero no matter what is worse than none."""
        self._populate(1, 2, self.TREE)
        self.insert_files(1, [("/pro/MuSy", "extra.hpp", "file", 7, "md5")])
        result = self.analyzer.get_diff(cloud_scan_id=1, local_scan_id=2, use_composite=False)
        self.assertEqual(result["missing_local_count"], 1)
        self.assertEqual(result["missing_local_sample"], ["/pro/MuSy/extra.hpp"])

    def test_invariant_holds_through_the_composite_path(self):
        self.insert_scan(1, "cloud", "success")
        self.insert_scan(2, "local", "success")
        self.insert_files(1, [("", "Books", "dir", 0, None)])
        self.insert_files(1, [
            (f"/{parent}" if parent else "", name, "file", 7, "md5")
            for parent, name in self.TREE
        ])
        self.insert_files(2, [
            (parent, name, "file", 7, "md5") for parent, name in self.TREE
        ])
        # A partial rescan of /pro/MuSy that found exactly the same content.
        self.insert_scan(3, "cloud", "success")
        self.insert_files(3, [("/pro/MuSy", "actor.hpp", "file", 7, "md5")])
        self.insert_progress(3, "/pro/MuSy", last_checked="2026-08-16 10:00:00")

        result = self.analyzer.get_diff(local_scan_id=2, use_composite=True)
        self.assertIn("composite", str(result["compare_scans"]["cloud"]))
        self.assertEqual(result["missing_local_count"], 0, result["missing_local_sample"])
        self.assertEqual(result["missing_cloud_count"], 0, result["missing_cloud_sample"])


class TestDiffExcludeDirs(AnalyzerTestCase):
    """exclude-dirs is not a list of top-level names: 45 of the 55 entries on
    the reference machine are nested (`video/Обучение`). Matching only the
    first path component reported thousands of deliberately unsynced files as
    missing locally."""

    def setUp(self):
        super().setUp()
        self.insert_scan(1, "cloud", "success")
        self.insert_scan(2, "local", "success")

    def _diff(self, exclude_line):
        with open(DEFAULT_CONFIG["exclude_config"], "w") as handle:
            handle.write(f"exclude-dirs={exclude_line}\n")
        return self.analyzer.get_diff(cloud_scan_id=1, local_scan_id=2, use_composite=False)

    def test_nested_exclude_entry_is_honoured(self):
        self.insert_files(1, [
            ("/video/Обучение", "lecture.mp4", "file", 1, None),
            ("/video/Obsidian", "keep.mp4", "file", 1, None),
        ])
        result = self._diff("video/Обучение")
        self.assertEqual(result["excluded_from_sync_count"], 1)
        self.assertEqual(result["missing_local_sample"], ["/video/Obsidian/keep.mp4"])

    def test_exclusion_applies_to_the_whole_subtree(self):
        self.insert_files(1, [
            ("/video/Обучение/deep/deeper", "lecture.mp4", "file", 1, None),
        ])
        result = self._diff("video/Обучение")
        self.assertEqual(result["missing_local_count"], 0)

    def test_a_sibling_sharing_a_name_prefix_is_not_excluded(self):
        self.insert_files(1, [("/videos", "keep.mp4", "file", 1, None)])
        result = self._diff("video")
        self.assertEqual(result["missing_local_sample"], ["/videos/keep.mp4"])

    def test_top_level_exclusion_still_works(self):
        self.insert_files(1, [("/Books/Math", "book.pdf", "file", 1, None)])
        result = self._diff("Books")
        self.assertEqual(result["missing_local_count"], 0)


class TestCompositeCloudFiles(AnalyzerTestCase):
    """`WHERE scan_id IN (...) AND parent_path IN (...)` is a cross product of
    every scan with every folder, not the composite's folder→scan pairing: any
    scan holding rows for a folder could win, at random."""

    def test_only_the_assigned_scan_supplies_a_folder(self):
        for scan_id in (1, 2, 3):
            self.insert_scan(scan_id, "cloud", "success")
        self.insert_files(1, [("/A", "f.txt", "file", 100, None)])
        self.insert_files(2, [("/A", "f.txt", "file", 200, None)])
        # Scan 3 owns /B but also happens to hold a row for /A.
        self.insert_files(3, [
            ("/B", "g.txt", "file", 300, None),
            ("/A", "f.txt", "file", 999, None),
        ])
        composite = {"base_scan_id": 1, "folder_updates": {"/A": 2, "/B": 3}}
        files = self.analyzer._composite_cloud_files(composite)
        self.assertEqual(files[("A", "f.txt")], 200)
        self.assertEqual(files[("B", "g.txt")], 300)

    def test_base_supplies_folders_nobody_updated(self):
        for scan_id in (1, 2):
            self.insert_scan(scan_id, "cloud", "success")
        self.insert_files(1, [
            ("/A", "f.txt", "file", 100, None),
            ("/C", "h.txt", "file", 400, None),
        ])
        self.insert_files(2, [("/A", "f.txt", "file", 200, None)])
        files = self.analyzer._composite_cloud_files(
            {"base_scan_id": 1, "folder_updates": {"/A": 2}}
        )
        self.assertEqual(files[("A", "f.txt")], 200)
        self.assertEqual(files[("C", "h.txt")], 400)

    def test_a_file_deleted_in_the_newer_scan_disappears(self):
        for scan_id in (1, 2):
            self.insert_scan(scan_id, "cloud", "success")
        self.insert_files(1, [
            ("/A", "gone.txt", "file", 1, None),
            ("/A", "kept.txt", "file", 1, None),
        ])
        self.insert_files(2, [("/A", "kept.txt", "file", 1, None)])
        files = self.analyzer._composite_cloud_files(
            {"base_scan_id": 1, "folder_updates": {"/A": 2}}
        )
        self.assertNotIn(("A", "gone.txt"), files)
        self.assertIn(("A", "kept.txt"), files)


class TestCompositeScan(AnalyzerTestCase):
    # Bulks up "full" cloud scans well past any partial scan's file count,
    # so find_last_full_scan()'s size-based heuristic reliably picks the
    # intended base scan -- without it, a "full" scan with only 1-2 files
    # under test is indistinguishable (by file count) from a small partial
    # scan and the heuristic can pick the wrong one. Also seeded into any
    # local scan compared against a padded cloud scan, so the padding
    # files match on both sides and don't show up as spurious diff noise.
    PADDING_FILES = [("/_padding", f"pad{i}.bin", "file", 1, None) for i in range(20)]

    def make_full_scan(self, scan_id, files):
        # Root-level rows are what makes a scan a *full* scan in production:
        # `scan cloud --path /A` writes nothing with parent_path "", and
        # find_last_full_scan() refuses such a scan as a composite base.
        rows = list(files) + self.PADDING_FILES
        top_levels = sorted({
            parent.strip("/").split("/")[0]
            for parent, *_ in rows
            if parent.strip("/")
        })
        self.insert_scan(scan_id, "cloud", "success")
        self.insert_files(scan_id, [("", name, "dir", 0, None) for name in top_levels])
        self.insert_files(scan_id, rows)
        self.insert_progress(scan_id, "/", status="completed")

    def make_partial_scan(self, scan_id, root_path, files):
        self.insert_scan(scan_id, "cloud", "success")
        self.insert_files(scan_id, files)
        self.insert_progress(scan_id, root_path, status="completed")

    def make_local_scan(self, scan_id, files):
        self.insert_scan(scan_id, "local", "success")
        self.insert_files(scan_id, list(files) + self.PADDING_FILES)

    def test_folder_updates_maps_only_the_partial_scans_folder(self):
        self.make_full_scan(1, [
            ("/A", "old.txt", "file", 1, None),
            ("/B", "stable.txt", "file", 1, None),
        ])
        self.make_partial_scan(2, "/A", [("/A", "new.txt", "file", 1, None)])
        composite = self.analyzer.build_composite_scan(use_cache=False)
        self.assertEqual(composite["base_scan_id"], 1)
        self.assertEqual(composite["folder_updates"], {"/A": 2})

    def test_diff_uses_composite_version_of_updated_folder(self):
        self.make_full_scan(1, [("/A", "old.txt", "file", 1, None)])
        self.make_partial_scan(2, "/A", [("/A", "new.txt", "file", 1, None)])
        self.make_local_scan(3, [("/A", "old.txt", "file", 1, None)])

        result = self.analyzer.get_diff(local_scan_id=3, use_composite=True)

        # The partial scan's new.txt supersedes the base's old.txt entirely
        # for parent_path "/A" (composite comparison works by exact
        # parent_path match, not by merging file lists within a folder).
        self.assertIn("/A/new.txt", result["missing_local_sample"])
        self.assertIn("/A/old.txt", result["missing_cloud_sample"])
        self.assertEqual(result["composite_info"]["base_scan_id"], 1)

    def test_parent_and_nested_folder_updates_both_kept(self):
        """A folder ("/A") and a more specific nested folder ("/A/B") are
        independent keys in folder_updates -- they refer to disjoint sets
        of files (parent_path exactly "/A" vs exactly "/A/B"), so both
        should survive. (This used to be tested as "the parent gets
        dropped", which was real but wrong behavior -- fixed in
        build_composite_scan() to keep every distinct folder_path entry;
        see CHANGELOG.md.)"""
        self.make_full_scan(1, [("/A", "root.txt", "file", 1, None)])
        self.make_partial_scan(2, "/A", [
            ("/A", "a.txt", "file", 1, None),
            ("/A/B", "b_old.txt", "file", 1, None),
        ])
        self.make_partial_scan(3, "/A/B", [("/A/B", "b_new.txt", "file", 1, None)])

        composite = self.analyzer.build_composite_scan(use_cache=False)

        self.assertEqual(composite["folder_updates"], {"/A": 2, "/A/B": 3})

    def test_cache_returns_stale_result_until_cleared(self):
        self.make_full_scan(1, [
            ("/A", "old.txt", "file", 1, None),
            ("/C", "c_old.txt", "file", 1, None),
        ])
        self.make_partial_scan(2, "/A", [("/A", "new.txt", "file", 1, None)])

        first = self.analyzer.build_composite_scan(use_cache=True)
        self.assertEqual(first["folder_updates"], {"/A": 2})

        self.make_partial_scan(3, "/C", [("/C", "c_new.txt", "file", 1, None)])
        stale = self.analyzer.build_composite_scan(use_cache=True)
        self.assertEqual(stale["folder_updates"], {"/A": 2}, "expected the cached (stale) result, missing /C")

        self.analyzer.clear_composite_cache()
        fresh = self.analyzer.build_composite_scan(use_cache=True)
        self.assertEqual(fresh["folder_updates"], {"/A": 2, "/C": 3})

    def test_empty_result_is_also_cached(self):
        """Regression test: build_composite_scan() used to `return` its
        "no partial scans yet" result *before* the cache-store code ran,
        so that specific (empty) result was silently never cached -- fixed
        so every outcome goes through the same cache path."""
        self.make_full_scan(1, [("/A", "only.txt", "file", 1, None)])

        first = self.analyzer.build_composite_scan(use_cache=True)
        self.assertEqual(first["folder_updates"], {})
        self.assertIn("composite_auto", self.analyzer._composite_cache)

        # A new partial scan appears, but the cached (stale, empty) result
        # should still be returned since nothing cleared the cache.
        self.make_partial_scan(2, "/A", [("/A", "new.txt", "file", 1, None)])
        stale = self.analyzer.build_composite_scan(use_cache=True)
        self.assertEqual(stale["folder_updates"], {}, "expected the cached (stale, empty) result")


class TestFindLastFullScan(AnalyzerTestCase):
    def test_explicit_scan_id_success(self):
        self.insert_scan(1, "cloud", "success")
        result = self.analyzer.find_last_full_scan(scan_id=1)
        self.assertEqual(result["id"], 1)

    def test_explicit_scan_id_not_successful(self):
        self.insert_scan(1, "cloud", "started")
        result = self.analyzer.find_last_full_scan(scan_id=1)
        self.assertIn("error", result)

    def test_heuristic_picks_fresh_successful_scan(self):
        self.insert_scan(1, "cloud", "success")
        self.insert_files(1, [("", "A", "dir", 0, None)])
        self.insert_files(1, [("/A", f"f{i}.txt", "file", 1, None) for i in range(5)])
        result = self.analyzer.find_last_full_scan()
        self.assertEqual(result["id"], 1)
        self.assertEqual(result["files_count"], 5)

    def test_reference_full_scan_id_config_wins_over_heuristic(self):
        self.insert_scan(1, "cloud", "success")
        self.insert_files(1, [("", "A", "dir", 0, None)])
        self.insert_files(1, [("/A", f"f{i}.txt", "file", 1, None) for i in range(100)])
        self.insert_scan(2, "cloud", "success")
        self.insert_files(2, [("/A", "only_one.txt", "file", 1, None)])

        self.config["reference_full_scan_id"] = 2
        result = self.analyzer.find_last_full_scan()

        self.assertEqual(result["id"], 2)


class TestScanRootPath(AnalyzerTestCase):
    """`scan cloud --path X` stores X nowhere, so the composite has to infer
    it. Reading "the scan_progress row with the earliest last_checked" gave a
    deep leaf — last_checked marks when a folder *finished* — and the "folder
    must be under the scan root" filter then dropped almost every folder the
    scan had recorded. On the real database this cost 94% of every partial
    scan's coverage (136 folder updates where there should have been 2249).
    """

    def _partial(self, scan_id, folders, finish_order=None):
        self.insert_scan(scan_id, "cloud", "success")
        for folder in folders:
            self.insert_files(scan_id, [(folder, "f.txt", "file", 1, None)])
        # Folders complete in walk order, not depth order: the deepest leaf
        # often finishes first.
        for index, folder in enumerate(finish_order or folders):
            self.insert_progress(
                scan_id, folder, last_checked=f"2026-08-16 10:00:{index:02d}"
            )

    def test_root_is_the_common_ancestor_not_the_first_finished_folder(self):
        self._partial(
            1,
            ["/Books/cpp/new", "/Books/Math/Ferma", "/Books/История"],
            finish_order=["/Books/Math/Ferma", "/Books/cpp/new", "/Books/История"],
        )
        self.assertEqual(self.analyzer.scan_root_path(1), "/Books")

    def test_single_folder_scan(self):
        self._partial(1, ["/pro/salva"])
        self.assertEqual(self.analyzer.scan_root_path(1), "/pro/salva")

    def test_falls_back_to_files_when_progress_is_empty(self):
        self.insert_scan(1, "cloud", "success")
        self.insert_files(1, [("/A/B", "f.txt", "file", 1, None)])
        self.assertEqual(self.analyzer.scan_root_path(1), "/A/B")

    def test_root_level_scan_has_no_partial_root(self):
        self.insert_scan(1, "cloud", "success")
        self.insert_files(1, [("", "A", "dir", 0, None), ("/A", "f.txt", "file", 1, None)])
        self.assertIsNone(self.analyzer.scan_root_path(1))

    def test_every_folder_of_a_partial_scan_reaches_the_composite(self):
        self.insert_scan(1, "cloud", "success")
        self.insert_files(1, [("", "Books", "dir", 0, None)])
        self.insert_files(1, [
            ("/Books/cpp", "old.txt", "file", 1, None),
            ("/Books/Math", "old.txt", "file", 1, None),
            ("/Books/История", "old.txt", "file", 1, None),
        ])
        self._partial(
            2,
            ["/Books/cpp", "/Books/Math", "/Books/История"],
            finish_order=["/Books/Math", "/Books/cpp", "/Books/История"],
        )
        composite = self.analyzer.build_composite_scan(use_cache=False)
        self.assertEqual(composite["base_scan_id"], 1)
        self.assertEqual(
            composite["folder_updates"],
            {"/Books/cpp": 2, "/Books/Math": 2, "/Books/История": 2},
        )

    def test_common_ancestor_helper(self):
        lca = self.analyzer._common_ancestor
        self.assertEqual(lca(["/A/B/C", "/A/B/D"]), "/A/B")
        self.assertEqual(lca(["/A/B", "/A"]), "/A")
        self.assertEqual(lca(["/A", "/B"]), "/")
        self.assertEqual(lca(["/A/B"]), "/A/B")
        self.assertEqual(lca([""]), "/")
        # A sibling with a shared name prefix is not an ancestor.
        self.assertEqual(lca(["/pro", "/protein"]), "/")


class TestPartialScanCannotBecomeBase(AnalyzerTestCase):
    """A scan of one folder must never be the composite base.

    `scan cloud --path /Books` can easily be the largest recent scan while
    describing a single subtree. Picking it as the base made the composite
    claim the whole disk was that subtree: on 2026-08-16 `sync_tree --path /`
    showed exactly one child because the base was a /Books-only scan.
    """

    def _full_scan(self, scan_id, folders=("A", "B", "C")):
        self.insert_scan(scan_id, "cloud", "success")
        self.insert_files(scan_id, [("", name, "dir", 0, None) for name in folders])
        self.insert_files(
            scan_id, [(f"/{name}", "f.txt", "file", 1, None) for name in folders]
        )

    def _partial_scan(self, scan_id, folder="A", files=50):
        self.insert_scan(scan_id, "cloud", "success")
        self.insert_files(
            scan_id, [(f"/{folder}", f"f{i}.txt", "file", 1, None) for i in range(files)]
        )

    def test_scan_covers_root(self):
        self._full_scan(1)
        self._partial_scan(2)
        self.assertTrue(self.analyzer.scan_covers_root(1))
        self.assertFalse(self.analyzer.scan_covers_root(2))

    def test_bigger_partial_scan_does_not_win_over_smaller_full_scan(self):
        self._full_scan(1)
        self._partial_scan(2, files=50)
        result = self.analyzer.find_last_full_scan()
        self.assertEqual(result["id"], 1)

    def test_partial_scan_becomes_a_folder_update_instead(self):
        self._full_scan(1)
        self._partial_scan(2, folder="A", files=3)
        composite = self.analyzer.build_composite_scan(use_cache=False)
        self.assertEqual(composite["base_scan_id"], 1)
        self.assertIn("/A", composite["folder_updates"])
        self.assertEqual(composite["folder_updates"]["/A"], 2)

    def test_only_partial_scans_leaves_no_base(self):
        self._partial_scan(1, folder="A")
        self._partial_scan(2, folder="B")
        self.assertIsNone(self.analyzer.find_last_root_scan())

    def test_old_full_scan_beats_fresh_partial_ones(self):
        self._full_scan(1, folders=("A", "B"))
        self.conn.execute(
            "UPDATE scans SET timestamp = datetime('now', '-200 days') WHERE id = 1"
        )
        self.conn.commit()
        self._partial_scan(2, folder="A", files=99)
        result = self.analyzer.find_last_full_scan()
        self.assertEqual(result["id"], 1)


class TestBoundedRootScan(AnalyzerTestCase):
    """`scan cloud --path / --depth 1` refreshes the disk root cheaply.

    It is the one folder that had no cheap refresh: a full walk of `/` is the
    whole disk. A bounded scan writes root rows like a full scan does, which
    makes it the most dangerous thing that could be handed to base selection —
    promoting one would collapse the snapshot to whatever sits directly in the
    root. The recorded `scan_depth` is what keeps them apart.
    """

    def _full_scan(self, scan_id, folders=("A", "B", "C")):
        self.insert_scan(scan_id, "cloud", "success", scan_root="/")
        self.insert_files(scan_id, [("", name, "dir", 0, None) for name in folders])
        self.insert_files(scan_id, [("", "root.txt", "file", 1, None)])
        self.insert_files(
            scan_id, [(f"/{name}", "f.txt", "file", 1, None) for name in folders]
        )

    def _bounded_root_scan(self, scan_id, folders=("A", "B", "C"), files=("root.txt", "new.txt")):
        self.insert_scan(scan_id, "cloud", "success", scan_root="/", scan_depth=1)
        self.insert_files(scan_id, [("", name, "dir", 0, None) for name in folders])
        self.insert_files(scan_id, [("", name, "file", 1, None) for name in files])

    def test_bounded_scan_is_not_a_full_scan(self):
        self._full_scan(1)
        self._bounded_root_scan(2)
        self.assertTrue(self.analyzer.scan_covers_root(1))
        self.assertFalse(self.analyzer.scan_covers_root(2))

    def test_bounded_scan_never_becomes_the_base(self):
        self._full_scan(1)
        self._bounded_root_scan(2)
        self.assertEqual(self.analyzer.find_last_full_scan()["id"], 1)
        self.assertEqual(self.analyzer.find_last_root_scan()["id"], 1)

    def test_bounded_scan_updates_only_the_root_bucket(self):
        self._full_scan(1)
        self._bounded_root_scan(2)
        composite = self.analyzer.build_composite_scan(use_cache=False)
        self.assertEqual(composite["base_scan_id"], 1)
        # The root bucket now comes from the bounded scan...
        self.assertEqual(composite["folder_updates"].get(""), 2)
        # ...and the subtrees it did not walk still come from the base.
        for folder in ("/A", "/B", "/C"):
            self.assertNotIn(folder, composite["folder_updates"])

    def test_bounded_scan_does_not_lose_the_subtrees(self):
        """The whole risk in one assertion: the file count must not collapse."""
        self._full_scan(1)
        self._bounded_root_scan(2)
        composite = self.analyzer.build_composite_scan(use_cache=False)
        files = self.analyzer._composite_cloud_files(composite)
        # 2 root files from the bounded scan + 3 subtree files from the base.
        self.assertEqual(len(files), 5)
        self.assertIn(("A", "f.txt"), files)
        self.assertIn(("", "new.txt"), files)

    def test_recorded_root_beats_inference(self):
        """A scan that says where it started is not second-guessed."""
        self.insert_scan(1, "cloud", "success", scan_root="/Books")
        self.insert_files(1, [("/Books/Math", "f.txt", "file", 1, None)])
        self.assertEqual(self.analyzer.scan_root_path(1), "/Books")

    def test_scan_without_recorded_scope_still_infers(self):
        """Databases written before the columns existed keep working."""
        self.insert_scan(1, "cloud", "success")
        self.insert_files(1, [
            ("/Books/Math", "f.txt", "file", 1, None),
            ("/Books/Phys", "g.txt", "file", 1, None),
        ])
        self.assertEqual(self.analyzer.scan_root_path(1), "/Books")

    def test_depth_of_counts_levels_below_the_start(self):
        from ydm import CloudScanner
        self.assertEqual(CloudScanner._depth_of("/", "/"), 0)
        self.assertEqual(CloudScanner._depth_of("/A", "/"), 1)
        self.assertEqual(CloudScanner._depth_of("/A/B", "/"), 2)
        self.assertEqual(CloudScanner._depth_of("/Books/Math", "/Books"), 1)
        self.assertEqual(CloudScanner._depth_of("/Books", "/Books"), 0)


class TestRetiredFolders(AnalyzerTestCase):
    """A folder a partial scan looked for and did not find is gone.

    Serving it from the base is what kept `report diff` reporting a file under
    a folder that had been deleted from the cloud months earlier — too long ago
    to still be in the trash, so no delta sweep could see it, and rescanning
    the parent did not help because "absent" and "uncovered" looked the same.
    """

    def _base(self, scan_id=1):
        self.insert_scan(scan_id, "cloud", "success", scan_root="/")
        self.insert_files(scan_id, [
            ("", "brtn", "dir", 0, None),
            ("/brtn", "keep.txt", "file", 1, None),
            ("/brtn/gone", "ghost.txt", "file", 1, None),
            ("/brtn/stays", "real.txt", "file", 1, None),
            ("/other", "untouched.txt", "file", 1, None),
        ])

    def _rescan(self, scan_id=2, status="success", scan_root="/brtn", scan_depth=None):
        self.insert_scan(scan_id, "cloud", status, scan_root=scan_root, scan_depth=scan_depth)
        self.insert_files(scan_id, [
            ("/brtn", "keep.txt", "file", 1, None),
            ("/brtn/stays", "real.txt", "file", 1, None),
        ])

    def test_folder_the_rescan_did_not_find_stops_serving_files(self):
        self._base()
        self._rescan()
        composite = self.analyzer.build_composite_scan(use_cache=False)
        files = self.analyzer._composite_cloud_files(composite)
        self.assertNotIn(("brtn/gone", "ghost.txt"), files)
        self.assertIn(("brtn/stays", "real.txt"), files)

    def test_retirement_stays_inside_the_rescanned_subtree(self):
        self._base()
        self._rescan()
        composite = self.analyzer.build_composite_scan(use_cache=False)
        files = self.analyzer._composite_cloud_files(composite)
        self.assertIn(("other", "untouched.txt"), files)

    def test_a_crashed_scan_retires_nothing(self):
        """An interrupted walk has not finished looking."""
        self._base()
        self._rescan(status="crashed")
        composite = self.analyzer.build_composite_scan(use_cache=False)
        files = self.analyzer._composite_cloud_files(composite)
        self.assertIn(("brtn/gone", "ghost.txt"), files)

    def test_a_depth_limited_scan_retires_nothing(self):
        """It stopped early on purpose; absence proves nothing below the cut."""
        self._base()
        self._rescan(scan_depth=1)
        composite = self.analyzer.build_composite_scan(use_cache=False)
        files = self.analyzer._composite_cloud_files(composite)
        self.assertIn(("brtn/gone", "ghost.txt"), files)

    def test_a_scan_without_recorded_scope_retires_nothing(self):
        """Old databases keep their stale rows rather than lose live ones."""
        self._base()
        self._rescan(scan_root=None)
        composite = self.analyzer.build_composite_scan(use_cache=False)
        files = self.analyzer._composite_cloud_files(composite)
        self.assertIn(("brtn/gone", "ghost.txt"), files)

    def test_a_scan_rooted_at_the_disk_root_retires_nothing(self):
        """One ambiguous row must not be able to empty the whole snapshot."""
        self._base()
        self.insert_scan(2, "cloud", "success", scan_root="/")
        self.insert_files(2, [("/brtn", "keep.txt", "file", 1, None)])
        composite = self.analyzer.build_composite_scan(use_cache=False)
        files = self.analyzer._composite_cloud_files(composite)
        self.assertIn(("brtn/gone", "ghost.txt"), files)
        self.assertIn(("other", "untouched.txt"), files)


class TestNoBaseWithoutCoverage(AnalyzerTestCase):
    """The coverage gate must have no back door.

    `build_composite_scan()` used to fall back to "the most recent cloud scan,
    whatever it is" when no acceptable base was found — which turned the gate
    off at the exact moment it had just rejected everything. A `--path /Books`
    scan, a `--depth 1` scan of three files, or a crashed one could become the
    base for the whole disk, with no error and no warning. See
    tasks/diff_correctness/GAP.md.
    """

    def _partial(self, scan_id, folder="/Books", files=5, **scope):
        self.insert_scan(scan_id, "cloud", scope.pop("status", "success"),
                         scan_root=scope.pop("scan_root", f"/{folder.strip('/')}"),
                         scan_depth=scope.pop("scan_depth", None))
        self.insert_files(
            scan_id, [(folder, f"f{i}.txt", "file", 1, None) for i in range(files)]
        )

    def _bounded_root(self, scan_id):
        self.insert_scan(scan_id, "cloud", "success", scan_root="/", scan_depth=1)
        self.insert_files(scan_id, [("", "root.txt", "file", 1, None)])

    def test_partial_scans_only_yields_an_error_not_a_base(self):
        self._partial(1)
        composite = self.analyzer.build_composite_scan(use_cache=False)
        self.assertIn("error", composite)
        self.assertNotIn("base_scan_id", composite)

    def test_a_depth_limited_root_scan_is_not_a_base_of_last_resort(self):
        """The exact reproduction from the gap note."""
        self._partial(1)
        self._bounded_root(2)
        composite = self.analyzer.build_composite_scan(use_cache=False)
        self.assertIn("error", composite)

    def test_a_crashed_scan_is_not_a_base_of_last_resort(self):
        self.insert_scan(1, "cloud", "crashed", scan_root="/")
        self.insert_files(1, [("", "A", "dir", 0, None), ("/A", "f.txt", "file", 1, None)])
        composite = self.analyzer.build_composite_scan(use_cache=False)
        self.assertIn("error", composite)

    def test_the_error_says_what_to_run(self):
        """An error that names no remedy just relocates the confusion."""
        self._partial(1)
        composite = self.analyzer.build_composite_scan(use_cache=False)
        self.assertIn("scan cloud", composite["error"])

    def test_no_cloud_scans_at_all_is_its_own_message(self):
        self.insert_scan(1, "local", "success")
        composite = self.analyzer.build_composite_scan(use_cache=False)
        self.assertEqual(composite["error"], "No cloud scan found")

    def test_an_old_root_scan_is_still_an_acceptable_base(self):
        """Freshness is the soft criterion; only coverage is hard.

        Refusing an old-but-complete scan would trade one silent wrong answer
        for a loud refusal to answer at all, on a disk that is fine.
        """
        self.insert_scan(1, "cloud", "success", timestamp="2020-01-01 00:00:00",
                         scan_root="/")
        self.insert_files(1, [("", "A", "dir", 0, None), ("/A", "f.txt", "file", 1, None)])
        self._partial(2, folder="/A", files=3, scan_root="/A")
        composite = self.analyzer.build_composite_scan(use_cache=False)
        self.assertEqual(composite["base_scan_id"], 1)

    def test_diff_falls_back_to_an_honestly_labelled_single_scan(self):
        """Refusing the composite must not make the diff silently wrong.

        get_diff() drops to comparing one scan, and says so in compare_scans —
        the caller can see it is one subtree, not a whole-disk snapshot.
        """
        self._partial(1, folder="/Books", files=2)
        self.insert_scan(2, "local", "success")
        self.insert_files(2, [("Books", "f0.txt", "file", 1, None)])
        result = self.analyzer.get_diff(local_scan_id=2, use_composite=True)
        self.assertNotIn("error", result)
        self.assertEqual(result["compare_scans"]["cloud"], 1)


class TestSnapshotFreshness(AnalyzerTestCase):
    """Every consumer reads the composite as if it were current. It is not:
    folders nobody rescanned are served by the base. Reporting a finding
    without saying how old the evidence under it is invites chasing ghosts —
    `report diff` flagged a file as missing locally when it had in fact been
    deleted from the cloud in a folder unvisited since March."""

    def _base(self, scan_id, timestamp, folders):
        self.insert_scan(scan_id, "cloud", "success", timestamp=timestamp)
        self.insert_files(scan_id, [("", "A", "dir", 0, None)])
        for folder, count in folders.items():
            self.insert_files(
                scan_id, [(folder, f"f{i}.txt", "file", 1, None) for i in range(count)]
            )

    def _partial(self, scan_id, timestamp, folder, count):
        self.insert_scan(scan_id, "cloud", "success", timestamp=timestamp)
        self.insert_files(
            scan_id, [(folder, f"p{i}.txt", "file", 1, None) for i in range(count)]
        )
        self.insert_progress(scan_id, folder, last_checked=timestamp)

    def test_reports_which_share_comes_from_the_base(self):
        self._base(1, "2026-03-04 08:00:00", {"/A": 4, "/B": 6})
        self._partial(2, "2026-08-16 08:00:00", "/A", 4)
        freshness = self.analyzer.snapshot_freshness()
        self.assertEqual(freshness["base_scan_id"], 1)
        self.assertEqual(freshness["files_from_base"], 6)     # /B only
        self.assertEqual(freshness["files_from_updates"], 4)  # /A from scan 2
        self.assertEqual(freshness["base_share_percent"], 60.0)

    def test_an_old_base_warns(self):
        self._base(1, "2020-01-01 00:00:00", {"/A": 1})
        self._partial(2, "2026-08-16 08:00:00", "/A", 1)
        # /A is covered by scan 2, but /A's base rows are superseded, so add a
        # folder nobody rescanned to keep base-served files non-zero.
        self.insert_files(1, [("/B", "old.txt", "file", 1, None)])
        freshness = self.analyzer.snapshot_freshness()
        self.assertTrue(freshness["warnings"])
        self.assertIn("cloud_delta", freshness["warnings"][0])

    def test_a_fresh_base_does_not_warn(self):
        recent = (datetime.now(timezone.utc).replace(tzinfo=None)).strftime("%Y-%m-%d %H:%M:%S")
        self._base(1, recent, {"/A": 3})
        self.assertEqual(self.analyzer.snapshot_freshness()["warnings"], [])

    def test_a_base_covering_nothing_does_not_warn(self):
        """If every folder has been rescanned, the base's age is irrelevant."""
        self._base(1, "2020-01-01 00:00:00", {"/A": 2})
        self._partial(2, "2026-08-16 08:00:00", "/A", 2)
        freshness = self.analyzer.snapshot_freshness()
        self.assertEqual(freshness["files_from_base"], 0)
        self.assertEqual(freshness["warnings"], [])

    def test_diff_carries_the_warning(self):
        self._base(1, "2020-01-01 00:00:00", {"/A": 2})
        self._partial(2, "2026-08-16 08:00:00", "/B", 1)
        self.insert_scan(3, "local", "success")
        result = self.analyzer.get_diff(local_scan_id=3, use_composite=True)
        self.assertTrue(result["warnings"])
        self.assertEqual(result["snapshot_freshness"]["base_scan_id"], 1)

    def test_an_old_base_serving_only_excluded_folders_does_not_warn(self):
        """The whole point of the split.

        On the real disk every one of the 36 028 files still served by the
        March base sits under exclude-dirs. Warning about them would fire on
        every run forever while changing no decision — and a warning that
        always fires is one nobody reads when it finally means something.
        """
        self._base(1, "2020-01-01 00:00:00", {"/downloads": 5})
        self._partial(2, "2026-08-16 08:00:00", "/A", 1)
        freshness = self.analyzer.snapshot_freshness(exclude_dirs={"downloads"})
        self.assertEqual(freshness["files_from_base"], 5)
        self.assertEqual(freshness["stale_excluded_files"], 5)
        self.assertEqual(freshness["stale_compared_files"], 0)
        self.assertEqual(freshness["warnings"], [])

    def test_an_old_base_still_warns_about_the_synced_part(self):
        """Mixed case: the alarm must survive being made quieter."""
        self._base(1, "2020-01-01 00:00:00", {"/downloads": 5, "/pro": 2})
        self._partial(2, "2026-08-16 08:00:00", "/A", 1)
        freshness = self.analyzer.snapshot_freshness(exclude_dirs={"downloads"})
        self.assertEqual(freshness["stale_excluded_files"], 5)
        self.assertEqual(freshness["stale_compared_files"], 2)
        self.assertTrue(freshness["warnings"])
        self.assertIn("2 synced file(s)", freshness["warnings"][0])

    def test_the_split_covers_every_base_served_file(self):
        """No third bucket: each stale file is either compared or it is not."""
        self._base(1, "2020-01-01 00:00:00", {"/downloads": 5, "/pro": 2, "/music": 3})
        freshness = self.analyzer.snapshot_freshness(exclude_dirs={"downloads", "music"})
        self.assertEqual(
            freshness["files_from_base"],
            freshness["stale_compared_files"] + freshness["stale_excluded_files"],
        )

    def test_nested_exclude_entry_counts_as_excluded(self):
        """`video/Обучение` excludes its subtree; `video` alone does not."""
        self._base(1, "2020-01-01 00:00:00", {"/video/Обучение/курс": 4, "/video/личное": 1})
        self._partial(2, "2026-08-16 08:00:00", "/A", 1)
        freshness = self.analyzer.snapshot_freshness(exclude_dirs={"video/Обучение"})
        self.assertEqual(freshness["stale_excluded_files"], 4)
        self.assertEqual(freshness["stale_compared_files"], 1)


class TestPrune(AnalyzerTestCase):
    """Retention. The database only grows — every partial scan adds rows and
    old full scans are never reclaimed — but deleting scans destroys history,
    which is one of the things this project is for. So the rule protects
    everything the composite needs plus an explicit amount of history, and
    does nothing at all unless asked."""

    def _full_scan(self, scan_id, timestamp=None):
        self.insert_scan(scan_id, "cloud", "success", timestamp=timestamp)
        self.insert_files(scan_id, [("", "A", "dir", 0, None)])
        self.insert_files(scan_id, [("/A", f"f{scan_id}.txt", "file", 1, None)])

    def _partial_scan(self, scan_id, folder="/A"):
        self.insert_scan(scan_id, "cloud", "success")
        self.insert_files(scan_id, [(folder, f"p{scan_id}.txt", "file", 1, None)])
        self.insert_progress(scan_id, folder, last_checked="2026-08-16 10:00:00")

    def _local_scan(self, scan_id):
        self.insert_scan(scan_id, "local", "success")
        self.insert_files(scan_id, [("A", f"l{scan_id}.txt", "file", 1, None)])

    def _kept_ids(self, plan):
        return {item["scan_id"] for item in plan["kept"]}

    def _prunable_ids(self, plan):
        return {item["scan_id"] for item in plan["prunable"]}

    def test_composite_base_and_its_updates_are_protected(self):
        self._full_scan(1)
        self._full_scan(2)
        self._partial_scan(3)
        plan = self.analyzer.prune_plan(keep_root_scans=1)
        base = plan["base_scan_id"]
        self.assertIn(base, self._kept_ids(plan))
        self.assertIn(3, self._kept_ids(plan))

    def test_old_full_scans_beyond_the_history_budget_are_prunable(self):
        for scan_id in (1, 2, 3):
            self._full_scan(scan_id)
        plan = self.analyzer.prune_plan(keep_root_scans=1)
        # 3 is the base (newest full scan); 1 and 2 are older history.
        self.assertEqual(plan["base_scan_id"], 3)
        self.assertEqual(self._prunable_ids(plan), {1, 2})

    def test_keep_root_scans_widens_the_history_budget(self):
        for scan_id in (1, 2, 3):
            self._full_scan(scan_id)
        plan = self.analyzer.prune_plan(keep_root_scans=3)
        self.assertEqual(self._prunable_ids(plan), set())

    def test_only_the_newest_local_scans_survive(self):
        self._full_scan(1)
        for scan_id in (2, 3, 4, 5):
            self._local_scan(scan_id)
        plan = self.analyzer.prune_plan(keep_local=2, keep_root_scans=1)
        self.assertEqual(self._prunable_ids(plan), {2, 3})

    def test_reference_full_scan_id_is_never_touched(self):
        for scan_id in (1, 2, 3):
            self._full_scan(scan_id)
        self.config["reference_full_scan_id"] = 1
        plan = self.analyzer.prune_plan(keep_root_scans=1)
        self.assertIn(1, self._kept_ids(plan))
        self.assertNotIn(1, self._prunable_ids(plan))

    def test_cloud_scans_newer_than_the_base_are_kept(self):
        self._full_scan(1)
        self.insert_scan(2, "cloud", "failed")  # newer, empty, may yet matter
        plan = self.analyzer.prune_plan(keep_root_scans=1)
        self.assertIn(2, self._kept_ids(plan))

    def test_dry_run_deletes_nothing(self):
        for scan_id in (1, 2, 3):
            self._full_scan(scan_id)
        before = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        result = self.analyzer.prune(keep_root_scans=1)
        self.assertFalse(result["applied"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0], 3)

    def test_apply_removes_the_scan_its_files_and_its_progress(self):
        for scan_id in (1, 2, 3):
            self._full_scan(scan_id)
        self.insert_progress(1, "/A", last_checked="2026-08-16 10:00:00")
        result = self.analyzer.prune(keep_root_scans=1, apply=True)
        self.assertTrue(result["applied"])
        remaining = {row[0] for row in self.conn.execute("SELECT id FROM scans")}
        self.assertEqual(remaining, {3})
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM files WHERE scan_id IN (1,2)").fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM scan_progress WHERE scan_id = 1").fetchone()[0], 0
        )

    def test_the_composite_still_builds_after_pruning(self):
        for scan_id in (1, 2, 3):
            self._full_scan(scan_id)
        self._partial_scan(4)
        self.analyzer.prune(keep_root_scans=1, apply=True)
        composite = self.analyzer.build_composite_scan(use_cache=False)
        self.assertNotIn("error", composite)
        self.assertEqual(composite["base_scan_id"], 3)
        self.assertIn("/A", composite["folder_updates"])

    def test_row_accounting(self):
        for scan_id in (1, 2, 3):
            self._full_scan(scan_id)
        plan = self.analyzer.prune_plan(keep_root_scans=1)
        self.assertEqual(
            plan["prunable_rows"], sum(item["rows"] for item in plan["prunable"])
        )
        self.assertLess(plan["prunable_rows"], plan["total_rows"])


class TestDuplicates(AnalyzerTestCase):
    def test_get_duplicates_by_hash_groups_matching_md5(self):
        self.insert_scan(1, "cloud", "success")
        self.insert_files(1, [
            ("/A", "a.txt", "file", 10, "hash1"),
            ("/B", "b.txt", "file", 10, "hash1"),
            ("/C", "c.txt", "file", 5, "hash2"),
            ("/D", "d.txt", "file", 5, None),
        ])
        result = self.analyzer.get_duplicates(1, by_hash=True)
        self.assertEqual(result["total_duplicates"], 1)
        dup = result["duplicates"][0]
        self.assertEqual(dup["hash"], "hash1")
        self.assertEqual(dup["file_count"], 2)
        self.assertEqual(set(dup["files"]), {"/A/a.txt", "/B/b.txt"})

    def test_get_duplicates_by_name_groups_size_and_name(self):
        self.insert_scan(1, "cloud", "success")
        self.insert_files(1, [
            ("/A", "same.txt", "file", 10, None),
            ("/B", "same.txt", "file", 10, None),
            ("/C", "same.txt", "file", 99, None),
        ])
        result = self.analyzer.get_duplicates(1, by_hash=False)
        self.assertEqual(result["total_duplicates"], 1)
        self.assertEqual(result["duplicates"][0]["occurrence_count"], 2)

    def test_clean_duplicates_keeps_min_id_per_group(self):
        self.insert_scan(1, "cloud", "success")
        # The schema's UNIQUE(scan_id, parent_path, name) index blocks
        # normal duplicate inserts -- drop it to simulate a legacy
        # pre-index DB, exactly the scenario `report clean-duplicates`
        # was built for (see CHANGELOG.md).
        self.conn.execute("DROP INDEX idx_files_unique")
        self.conn.executemany(
            "INSERT INTO files (scan_id, parent_path, name, type, size, md5) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "/A", "dup.txt", "file", 1, None),
                (1, "/A", "dup.txt", "file", 1, None),
                (1, "/A", "dup.txt", "file", 1, None),
                (1, "/A", "unique.txt", "file", 1, None),
            ],
        )
        self.conn.commit()
        min_id = self.conn.execute(
            "SELECT MIN(id) FROM files WHERE parent_path='/A' AND name='dup.txt'"
        ).fetchone()[0]

        result = self.analyzer.clean_duplicates(scan_id=1)

        self.assertEqual(result["deleted_duplicates"], 2)
        self.assertEqual(result["total_files_after"], 2)
        remaining_ids = {
            row[0]
            for row in self.conn.execute(
                "SELECT id FROM files WHERE parent_path='/A' AND name='dup.txt'"
            ).fetchall()
        }
        self.assertEqual(remaining_ids, {min_id})


class TestLongPaths(AnalyzerTestCase):
    def test_threshold_and_sorting(self):
        self.insert_scan(1, "cloud", "success")
        short_name = "short.txt"
        long_name = "x" * 250 + ".txt"
        longer_name = "y" * 300 + ".txt"
        self.insert_files(1, [
            ("/A", short_name, "file", 1, None),
            ("/A", long_name, "file", 1, None),
            ("/A", longer_name, "file", 1, None),
        ])
        result = self.analyzer.get_long_paths(1, limit_chars=240)
        self.assertEqual(result["long_paths_count"], 2)
        self.assertEqual(result["long_paths"][0]["path"], f"A/{longer_name}")
        self.assertEqual(result["long_paths"][1]["path"], f"A/{long_name}")


class TestExitCodes(unittest.TestCase):
    """`ydm.py` as a program: does a failure look like one to the shell?

    Until 2026-08-24 every command exited 0, including "token not found" and
    "scan failed" — handlers return True for "I handled this", not for "it
    went well", and `__main__` discarded the result. So `ydm.py … && next`
    ran `next` after any error, and the two callers in tools/sync_backends.py
    that already test the return code could never see a failure.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ydm_exit_")
        self.db_path = os.path.join(self.tmpdir, "monitor.db")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def ydm(self, *args):
        import subprocess

        # YDM_LOCAL_ROOT is stripped deliberately. It is exported by
        # tools/aliases.sh, so on a developer machine it is simply present in
        # the environment — and with it inherited, "scan local with no path"
        # is not a failure at all: it scans the real mirror. Which is exactly
        # what this test did until 2026-08-24, reading 14 138 real files to
        # assert that nothing should have happened.
        env = {k: v for k, v in os.environ.items() if k != "YDM_LOCAL_ROOT"}
        env.update({"HOME": self.tmpdir, "YDM_VAR_DIR": self.tmpdir})
        return subprocess.run(
            [sys.executable, str(ROOT_DIR / "ydm.py"), "--db-path", self.db_path, *args],
            capture_output=True, text=True, check=False, env=env,
        )

    def test_success_is_zero(self):
        proc = self.ydm("init")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_a_rendered_failure_is_not_zero(self):
        """`scan local` with no path anywhere: a real failure, plainly stated."""
        proc = self.ydm("scan", "local")
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("No local path", proc.stdout)

    def test_the_json_payload_and_the_exit_code_agree(self):
        """Two ways of reporting the same outcome must not contradict."""
        import json as _json

        proc = self.ydm("--format", "json", "scan", "local")
        payload = _json.loads(proc.stdout)
        self.assertFalse(payload["success"], payload)
        self.assertNotEqual(proc.returncode, 0)

    def test_a_missing_scan_id_fails(self):
        """A second failure path, so the check is not pinned to one message.

        Note the syntax: the report type is positional. Written as
        `report --type scan-info` this test passed against the old code too —
        argparse rejected the unknown flag and exited 2, which is non-zero for
        an entirely unrelated reason.
        """
        self.ydm("init")
        proc = self.ydm("report", "scan-info")
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("--scan-id required", proc.stdout)

    def test_a_working_report_is_zero(self):
        """The other direction: an ordinary report must not start failing."""
        self.ydm("init")
        proc = self.ydm("report", "scan-list")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_help_is_still_zero(self):
        """argparse exits on its own; --help must not be dragged into failing."""
        self.assertEqual(self.ydm("--help").returncode, 0)


class TestGetConnectionIsDefinedOnce(unittest.TestCase):
    """`StorageManager.get_connection` was written twice, and the second won.

    Found 2026-08-25 while trying to add a shared-connection scope: the edit
    landed in a method that had never run. The dead one carried
    `PRAGMA synchronous`, `PRAGMA journal_mode` and the whole "Level 2"
    tmpfs schema recovery — so on every machine using /dev/shm for scans, the
    recovery written for exactly that case could not fire.

    Python keeps only the last definition, so nothing warns and nothing
    fails. The guard has to read the source.
    """

    def _class_body(self):
        import ast

        source = (ROOT_DIR / "ydm.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "StorageManager":
                return node
        self.fail("StorageManager not found in ydm.py")

    def test_no_method_of_storage_manager_is_defined_twice(self):
        import ast
        from collections import Counter

        names = Counter(
            item.name for item in self._class_body().body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        duplicates = {name: count for name, count in names.items() if count > 1}
        self.assertEqual({}, duplicates,
                         "a later definition silently replaces an earlier one")


class TestConnectionPragmas(unittest.TestCase):
    """The pragmas the shadowed method meant to set, now actually set.

    Restoring them costs nothing measurable: on a 138 MB database,
    connect+query is 0.522 ms and connect+both pragmas+query is 0.511 ms.
    The ~0.6 ms once attributed to the pragmas is the price of first touching
    the file, which any first statement pays.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "monitor.db")
        StorageManager(self.db_path, use_temp_storage=False,
                       config=DEFAULT_CONFIG).init_db()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_a_disk_connection_asks_for_normal_synchronous(self):
        storage = StorageManager(self.db_path, use_temp_storage=False,
                                 config=DEFAULT_CONFIG)
        conn = storage.get_connection()
        try:
            # 1 == NORMAL. FULL (2) is the sqlite default and is slower than
            # this project needs for a cache it can rebuild by rescanning.
            self.assertEqual(1, conn.execute("PRAGMA synchronous").fetchone()[0])
            self.assertEqual("wal", conn.execute("PRAGMA journal_mode").fetchone()[0])
        finally:
            conn.close()

    def test_a_temp_connection_keeps_its_journal_in_memory(self):
        """Scanning into tmpfs wants speed, not durability — it is a scratch copy."""
        storage = StorageManager(self.db_path, use_temp_storage=False,
                                 config=DEFAULT_CONFIG)
        storage.temp_mode = True  # without needing /dev/shm to exist here
        conn = storage.get_connection()
        try:
            self.assertEqual(0, conn.execute("PRAGMA synchronous").fetchone()[0])
            self.assertEqual("memory",
                             conn.execute("PRAGMA journal_mode").fetchone()[0])
        finally:
            conn.close()


class TestInitDbClosesWhatItOpens(unittest.TestCase):
    """`with sqlite3.connect(...)` commits; it does not close.

    init_db() used the `with` form, so its connection outlived the call and
    was released only by the next garbage collection. Enough to make the very
    next `PRAGMA journal_mode` fail with "database is locked" — which is how
    this was found: the tmpfs recovery calls init_db() and then reopens the
    file, so the leak stood in the way of the case the recovery exists for.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "monitor.db")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_nothing_holds_the_file_after_init(self):
        import sqlite3

        StorageManager(self.db_path, use_temp_storage=False,
                       config=DEFAULT_CONFIG).init_db()
        # No gc.collect() on purpose: needing one is the defect.
        conn = sqlite3.connect(self.db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode=MEMORY").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("memory", mode,
                         "a connection from init_db is still holding the file")


class TestTmpfsSchemaRecovery(unittest.TestCase):
    """"Level 2": the scratch database vanished mid-scan, so rebuild it.

    tmpfs is memory — a reboot, an OOM kill or a stray cleanup takes the file
    with it, and the scan then writes into a database with no tables. The
    recovery for that has been unreachable code since it was written.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "scratch.db")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _storage(self):
        storage = StorageManager(self.db_path, use_temp_storage=False,
                                 config=DEFAULT_CONFIG)
        storage.temp_mode = True
        storage.db_path = self.db_path
        return storage

    def test_a_missing_schema_is_rebuilt(self):
        import io
        import contextlib
        import sqlite3

        sqlite3.connect(self.db_path).close()  # an empty file, no tables
        storage = self._storage()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            conn = storage.get_connection()
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertIn("scan_progress", tables, "the schema was not rebuilt")
        self.assertIn("CRITICAL", stderr.getvalue(),
                      "recovery happened silently, which is how it stayed unnoticed")

    def test_a_healthy_scratch_database_is_left_alone(self):
        import io
        import contextlib

        StorageManager(self.db_path, use_temp_storage=False,
                       config=DEFAULT_CONFIG).init_db()
        storage = self._storage()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            conn = storage.get_connection()
        conn.close()
        self.assertNotIn("CRITICAL", stderr.getvalue())

    def test_recovery_does_not_run_for_a_disk_database(self):
        """A missing schema on disk is not a lost scratch copy — do not rebuild."""
        import io
        import contextlib
        import sqlite3

        sqlite3.connect(self.db_path).close()
        storage = StorageManager(self.db_path, use_temp_storage=False,
                                 config=DEFAULT_CONFIG)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            conn = storage.get_connection()
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertNotIn("scan_progress", tables)
        self.assertNotIn("CRITICAL", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
