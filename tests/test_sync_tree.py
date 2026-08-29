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
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from query_count import counting_queries  # noqa: E402
from tools.sync_common import CompositeSnapshot  # noqa: E402
from tools.sync_tree import (  # noqa: E402
    FolderFileCounts,
    _count_files_for_prefix,
    _folder_file_counts,
    apply_sync_percent,
    build_tree,
    count_cloud_files_for_path,
)
from tools.sync_tree_cloud import (  # noqa: E402
    ChildIndex,
    fetch_child_names,
    infer_dirs_from_files,
    select_snapshot_for_tree,
)
from ydm import Analyzer, StorageManager  # noqa: E402
from tools.sync_tree_policy import (  # noqa: E402
    display_marker,
    effective_policy_state,
    load_policy_context,
    local_state,
    path_in_policy,
    policy_mode_for_path,
    policy_summary_line,
)


class _Storage:
    """The narrow slice of StorageManager the cloud helpers actually use."""

    def __init__(self, db_path):
        self._db_path = db_path

    def get_connection(self):
        return sqlite3.connect(self._db_path)


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

    # The real dataclass rather than a stand-in: it carries a sorted index of
    # `folder_updates` built on first use, and a double without it would let a
    # per-call sort through unnoticed — which would cost more than the sweep
    # the index replaced.
    @staticmethod
    def _Snapshot(base_scan_id, folder_updates):
        from tools.sync_common import CompositeSnapshot

        return CompositeSnapshot(base_scan_id=base_scan_id,
                                 folder_updates=folder_updates)

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

    def _make_indexed_db(self):
        """A snapshot carrying the production index, and a prefix sibling.

        `/Books/Math-old` is here to pin the range boundaries. The obvious
        range ['/Books/Math', '/Books/Math0') would swallow it, because '-'
        sorts below '0'; the correct one starts at '/Books/Math/'.
        """
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
            "CREATE UNIQUE INDEX idx_files_unique ON files(scan_id, parent_path, name)"
        )
        rows = [
            (1, 1, "/Books/Math/АнГем", "book.pdf", "file"),
            (2, 1, "/Books/Math/База/poya", "deep.pdf", "file"),
            (3, 1, "/Books/Math", "loose.pdf", "file"),
            (4, 1, "/Books/Math-old", "archived.pdf", "file"),
        ]
        for row in rows:
            conn.execute(
                "INSERT INTO files VALUES (?, ?, ?, ?, ?, 1, NULL, NULL, NULL)", row
            )
        conn.commit()
        conn.close()
        return path

    def test_a_prefix_sibling_is_not_taken_for_a_child(self):
        db_path = self._make_indexed_db()

        class Storage:
            def __init__(self, p):
                self._p = p

            def get_connection(self):
                return sqlite3.connect(self._p)

        names = infer_dirs_from_files(Storage(db_path), 1, "/Books/Math")
        self.assertEqual(sorted(names), ["АнГем", "База"])
        os.unlink(db_path)

    def test_child_inference_seeks_the_index_instead_of_scanning(self):
        """The subtree lookup must be a range, not a LIKE.

        SQLite folds LIKE into an index range only when LIKE is
        case-sensitive, and by default it is not — so `parent_path LIKE
        '/x/%'` read every row of the scan, once per folder the walk visits.
        That is what made a depth-5 `ydm_menu orphans` take minutes on an
        80k-row snapshot: the cost was per folder, and no index applied.
        """
        db_path = self._make_indexed_db()
        statements = []

        class Storage:
            def __init__(self, p):
                self._p = p

            def get_connection(self):
                conn = sqlite3.connect(self._p)
                conn.set_trace_callback(statements.append)
                return conn

        infer_dirs_from_files(Storage(db_path), 1, "/Books/Math")
        ranged = [s for s in statements if "parent_path >=" in s]
        self.assertTrue(ranged, f"no range query was issued: {statements}")

        conn = sqlite3.connect(db_path)
        try:
            plan = conn.execute("EXPLAIN QUERY PLAN " + ranged[0]).fetchall()
        finally:
            conn.close()
        detail = " ".join(str(row[-1]) for row in plan)
        self.assertIn(
            "parent_path>", detail, f"the index range is not being used: {detail}"
        )
        os.unlink(db_path)

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


class FileCountRangeTests(unittest.TestCase):
    """Counting files under a folder seeks the index too.

    PR #4 replaced `parent_path LIKE '<prefix>/%'` with a range in
    `infer_dirs_from_files`. The same LIKE survived in the counting half —
    `_folder_file_counts()` and `_count_files_for_prefix()` — where it runs
    once per node of the tree rather than once per folder without dir rows.
    Profiling the whole `sync_tree` command on 2026-08-25 put it at 12 s of
    an 18 s run, the largest single cost once the walk itself was fixed.

    The boundaries are the same ones and matter for the same reason:
    ['<prefix>/', '<prefix>0') and not ['<prefix>', '<prefix>0'), or
    `/Books/Math-old` is counted as part of `/Books/Math`.
    """

    def _db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE files (
                id INTEGER PRIMARY KEY, scan_id INTEGER, parent_path TEXT,
                name TEXT, type TEXT, size INTEGER, md5 TEXT,
                created TEXT, modified TEXT
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_files_unique ON files(scan_id, parent_path, name)"
        )
        rows = [
            (1, 1, "/Books/Math", "loose.pdf", "file"),
            (2, 1, "/Books/Math/АнГем", "a.pdf", "file"),
            (3, 1, "/Books/Math/База/poya", "deep.pdf", "file"),
            (4, 1, "/Books/Math-old", "archived.pdf", "file"),
            (5, 1, "/Books", "top.pdf", "file"),
        ]
        for row in rows:
            conn.execute(
                "INSERT INTO files VALUES (?, ?, ?, ?, ?, 1, NULL, NULL, NULL)", row
            )
        conn.commit()
        conn.close()
        return path

    def test_the_subtree_count_is_a_range_not_a_like(self):
        from tools.sync_tree import _folder_file_counts

        path = self._db()
        try:
            statements = []
            conn = sqlite3.connect(path)
            conn.set_trace_callback(statements.append)
            counts = _folder_file_counts(conn, 1, "/Books/Math")
            conn.close()

            ranged = [s for s in statements if "parent_path >=" in s]
            self.assertTrue(ranged, f"no range query was issued: {statements}")

            conn = sqlite3.connect(path)
            try:
                plan = conn.execute("EXPLAIN QUERY PLAN " + ranged[0]).fetchall()
            finally:
                conn.close()
            detail = " ".join(str(row[-1]) for row in plan)
            self.assertIn("parent_path>", detail,
                          f"the index range is not being used: {detail}")

            # And the answer is unchanged: the prefix sibling stays out.
            self.assertEqual(
                {"/Books/Math": 1, "/Books/Math/АнГем": 1, "/Books/Math/База/poya": 1},
                counts,
            )
        finally:
            os.unlink(path)

    def test_a_prefix_sibling_is_not_counted_as_a_child(self):
        from tools.sync_tree import _count_files_for_prefix

        path = self._db()
        try:
            conn = sqlite3.connect(path)
            try:
                # /Books/Math holds three files; /Books/Math-old is not one.
                self.assertEqual(3, _count_files_for_prefix(conn, 1, "/Books/Math"))
                self.assertEqual(1, _count_files_for_prefix(conn, 1, "/Books/Math-old"))
                self.assertEqual(5, _count_files_for_prefix(conn, 1, ""))
            finally:
                conn.close()
        finally:
            os.unlink(path)

    def test_the_whole_tree_counts_the_same_as_before(self):
        """Root-level totals do not change because the predicate did."""
        from tools.sync_tree import _folder_file_counts

        path = self._db()
        try:
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(5, sum(_folder_file_counts(conn, 1, "").values()))
                self.assertEqual(5, sum(_folder_file_counts(conn, 1, "/Books").values()))
            finally:
                conn.close()
        finally:
            os.unlink(path)


class SnapshotDescendantTests(unittest.TestCase):
    """Which updated folders sit *under* a path, found by search not by sweep.

    The mirror image of SnapshotLookupTests. `count_cloud_files_for_path()`
    rebuilt the set of relevant `folder_updates` on every call by testing
    `folder.startswith(subtree + "/")` against all 2 251 of them — once per
    node, so 9.38 million string comparisons in one `orphans` run.

    Descendants of a path are a contiguous slice of the sorted keys, between
    `<subtree>/` and `<subtree>0` — the same boundary as the SQL range, for
    the same reason.
    """

    def test_it_finds_exactly_the_descendants(self):
        from tools.sync_common import CompositeSnapshot, folder_updates_under

        snap = CompositeSnapshot(base_scan_id=1, folder_updates={
            "": 5, "/Books": 7, "/Books/Math": 8, "/Books/Math/ЛинАл": 9,
            "/Books/Math-old": 10, "/pro": 11,
        })
        self.assertEqual(
            {"/Books/Math": 8, "/Books/Math/ЛинАл": 9},
            folder_updates_under(snap, "/Books/Math"),
        )

    def test_a_prefix_sibling_is_not_a_descendant(self):
        from tools.sync_common import CompositeSnapshot, folder_updates_under

        snap = CompositeSnapshot(base_scan_id=1, folder_updates={
            "/Books/Math": 8, "/Books/Math-old": 10,
        })
        self.assertNotIn("/Books/Math-old", folder_updates_under(snap, "/Books/Math"))

    def test_the_root_takes_everything_except_the_root_entry(self):
        """`""` is the root's own key and is excluded, as the old filter did."""
        from tools.sync_common import CompositeSnapshot, folder_updates_under

        snap = CompositeSnapshot(base_scan_id=1, folder_updates={
            "": 5, "/Books": 7, "/pro": 11,
        })
        self.assertEqual({"/Books": 7, "/pro": 11}, folder_updates_under(snap, ""))

    def test_it_agrees_with_the_filter_it_replaces(self):
        from tools.sync_common import CompositeSnapshot, folder_updates_under

        updates = {
            "": 5, "/Books": 7, "/Books/Math": 8, "/Books/Math/ЛинАл": 9,
            "/Books/Math-old": 10, "/pro": 11, "/pro/agents": 12,
            "/proximity": 13,
        }
        snap = CompositeSnapshot(base_scan_id=1, folder_updates=updates)
        for subtree in ("", "/Books", "/Books/Math", "/pro", "/nothing"):
            expected = {
                folder: scan_id for folder, scan_id in updates.items()
                if folder and (not subtree or folder == subtree
                               or folder.startswith(subtree + "/"))
            }
            with self.subTest(subtree=subtree):
                self.assertEqual(expected, folder_updates_under(snap, subtree))

    def test_the_sorted_index_is_built_once(self):
        """Sorting per call would be worse than the sweep it replaces."""
        from tools.sync_common import CompositeSnapshot, folder_updates_under

        snap = CompositeSnapshot(base_scan_id=1, folder_updates={
            "/Books": 7, "/Books/Math": 8,
        })
        folder_updates_under(snap, "/Books")
        first = snap.sorted_folders()
        folder_updates_under(snap, "/Books/Math")
        self.assertIs(first, snap.sorted_folders())


class SnapshotLookupTests(unittest.TestCase):
    """Which scan serves a folder, found by climbing rather than scanning.

    `select_scan_id_for_path()` answered by walking every key of
    `folder_updates` and keeping the longest prefix match. That is once per
    node of the tree, and on the author's snapshot `folder_updates` holds
    2 251 entries — a depth-4 walk visits 2 816 nodes, so the lookup alone ran
    6.34 million `startswith` calls and was the single largest cost in the
    walk, ahead of every database query put together.

    The set of ancestors of a path is knowable without looking at the keys:
    it is the path, then its parent, and so on. That is at most as many dict
    lookups as the path has components — five or six, against 2 251.

    Found by profiling on 2026-08-25, after issue #3 had already named three
    other leftovers. This one was in none of the lists.
    """

    def snapshot(self, folder_updates, base=1):
        from tools.sync_common import CompositeSnapshot

        return CompositeSnapshot(base_scan_id=base, folder_updates=folder_updates)

    def select(self, path, folder_updates, base=1):
        from tools.sync_common import select_scan_id_for_path

        return select_scan_id_for_path(path, self.snapshot(folder_updates, base))

    def test_the_root_uses_its_own_entry_when_there_is_one(self):
        self.assertEqual(9, self.select("/", {"": 9}))
        self.assertEqual(1, self.select("/", {"/Books": 9}))

    def test_an_exact_match_wins(self):
        self.assertEqual(9, self.select("/Books", {"/Books": 9}))

    def test_the_longest_ancestor_wins(self):
        updates = {"/Books": 7, "/Books/Math": 8, "/Books/Math/ЛинАл": 9}
        self.assertEqual(9, self.select("/Books/Math/ЛинАл/Lay", updates))
        self.assertEqual(8, self.select("/Books/Math/ТерВер", updates))
        self.assertEqual(7, self.select("/Books/Other", updates))

    def test_a_prefix_sibling_is_not_an_ancestor(self):
        """`/Books/Math-old` is not inside `/Books/Math`.

        The same boundary PR #4 pinned for the SQL range, in the other half of
        the lookup: matching on the bare prefix would claim it.
        """
        self.assertEqual(7, self.select("/Books/Math-old", {"/Books": 7, "/Books/Math": 8}))

    def test_no_ancestor_falls_back_to_the_base(self):
        self.assertEqual(1, self.select("/elsewhere", {"/Books": 9}))

    def test_the_lookup_does_not_read_every_entry(self):
        """The regression guard, stated structurally rather than by stopwatch.

        A dict that refuses to be iterated: the answer has to come from
        lookups, so a return to prefix-scanning fails here rather than merely
        getting slower somewhere nobody is timing.
        """
        class NoScanDict(dict):
            def keys(self):
                raise AssertionError("the lookup scanned every entry again")

            def __iter__(self):
                raise AssertionError("the lookup scanned every entry again")

            def items(self):
                raise AssertionError("the lookup scanned every entry again")

        updates = NoScanDict({f"/dir{i}": i for i in range(5000)})
        updates["/Books/Math"] = 99
        self.assertEqual(99, self.select("/Books/Math/ЛинАл", updates))
        self.assertEqual(1, self.select("/nothing/here", updates))

    def test_it_agrees_with_the_prefix_scan_it_replaces(self):
        """Equivalence against the original implementation, spelled out here.

        The old code is short enough to keep as an oracle, which is worth more
        than trusting that the rewrite "looks the same".
        """
        def old(path, snapshot):
            from tools.sync_common import normalize_db_parent_path

            normalized = normalize_db_parent_path(path)
            if normalized == "":
                return snapshot.folder_updates.get("", snapshot.base_scan_id)
            best_match = None
            for candidate in snapshot.folder_updates.keys():
                if not candidate:
                    continue
                if normalized == candidate or normalized.startswith(candidate + "/"):
                    if best_match is None or len(candidate) > len(best_match):
                        best_match = candidate
            if best_match is None:
                return snapshot.base_scan_id
            return snapshot.folder_updates[best_match]

        from tools.sync_common import select_scan_id_for_path

        updates = {
            "": 5,
            "/Books": 7,
            "/Books/Math": 8,
            "/Books/Math/ЛинАл": 9,
            "/Books/Math-old": 10,
            "/pro": 11,
            "/pro/agents": 12,
        }
        snap = self.snapshot(updates, base=1)
        paths = [
            "/", "/Books", "/Books/", "/Books/Math", "/Books/Math/ЛинАл/Lay",
            "/Books/Math-old", "/Books/Math-old/x", "/Books/Mathematics",
            "/pro", "/pro/agents/x", "/proximity", "/elsewhere", "/pro/",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(old(path, snap), select_scan_id_for_path(path, snap))


class ChildIndexTests(unittest.TestCase):
    """Reading each scan once must answer exactly what asking per node did.

    `ChildIndex` replaces up to ten queries per node with two per scan
    (Phase 12). The whole value of it depends on the answers being the same
    ones, so most of what is checked here is equivalence against the
    un-indexed path rather than against hand-written expectations: the old
    code is the specification, including the parts of it that are quirks.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _db(self, rows, name="monitor.db"):
        """`rows` are (scan_id, parent_path, name, type) as the scan wrote them."""
        path = os.path.join(self.tmpdir, name)
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE files (
                id INTEGER PRIMARY KEY, scan_id INTEGER, parent_path TEXT,
                name TEXT, type TEXT, size INTEGER, modified TEXT, md5 TEXT, path TEXT
            );
            CREATE UNIQUE INDEX idx_files_unique ON files (scan_id, parent_path, name);
            """
        )
        conn.executemany(
            "INSERT INTO files (scan_id, parent_path, name, type, size)"
            " VALUES (?, ?, ?, ?, 1)",
            rows,
        )
        conn.commit()
        conn.close()
        return _Storage(path)

    def _assert_agrees(self, storage, scan_id, paths):
        index = ChildIndex(storage)
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(
                    fetch_child_names(storage, scan_id, path),
                    fetch_child_names(storage, scan_id, path, index=index),
                )

    def test_it_agrees_when_the_scan_recorded_its_directories(self):
        storage = self._db([
            (1, "/Books", "Math", "dir"),
            (1, "/Books", "Physics", "dir"),
            (1, "/Books/Math", "book.pdf", "file"),
            (1, "", "Books", "dir"),
        ])
        self._assert_agrees(storage, 1, ["/", "/Books", "/Books/Math", "/nope"])

    def test_it_agrees_when_the_scan_recorded_no_directories_at_all(self):
        """The device's shape: dir rows are sparse, so inference is the rule."""
        storage = self._db([
            (1, "/Books/Math/ЛинАл", "lay.pdf", "file"),
            (1, "/Books/Math/АнГем", "notes.pdf", "file"),
            (1, "/pro/agents", "main.py", "file"),
        ])
        self._assert_agrees(
            storage, 1, ["/", "/Books", "/Books/Math", "/Books/Math/ЛинАл", "/pro"]
        )
        index = ChildIndex(storage)
        self.assertEqual(
            index.children(1, "/Books/Math"), ["АнГем", "ЛинАл"]
        )

    def test_it_agrees_when_the_scan_wrote_paths_without_a_leading_slash(self):
        """rclone scans spell `parent_path` one way, API scans the other."""
        storage = self._db([
            (1, "Books/Math", "book.pdf", "file"),
            (1, "Books", "Math", "dir"),
        ])
        self._assert_agrees(storage, 1, ["/", "/Books", "/Books/Math"])
        self.assertEqual(fetch_child_names(storage, 1, "/"), ["Books"])

    def test_the_root_does_not_go_blind_below_two_levels(self):
        """Found while building the index, and fixed on both paths.

        Root is the one folder answered by extracting a first segment rather
        than by a range, and the query carried `parent_path NOT LIKE
        '/%/%/%'`. A top-level folder whose files all sit three or more levels
        down was therefore invisible at the root — and since the walk descends
        into what it lists, the whole subtree went missing. In `orphans` that
        reads as a local copy with no cloud counterpart.

        Reachable only when the scan has no dir row for that folder at the
        root, which is why the live snapshot renders identically either way
        (verified byte for byte at depths 3, 4 and 5 on 2026-08-28). The bound
        was dropped rather than reproduced in the index: leaving it would have
        meant the tree and the menu's cloud listing answering the same
        question differently, which is its own defect.
        """
        storage = self._db([
            (1, "/shallow", "a.pdf", "file"),
            (1, "/mid/x", "b.pdf", "file"),
            (1, "/deep/x/y", "c.pdf", "file"),
            (1, "/deeper/x/y/z", "d.pdf", "file"),
        ])
        expected = ["deep", "deeper", "mid", "shallow"]
        self.assertEqual(fetch_child_names(storage, 1, "/"), expected)
        self.assertEqual(ChildIndex(storage).children(1, "/"), expected)
        self._assert_agrees(storage, 1, ["/", "/deep", "/deep/x", "/deeper/x/y"])

    def test_a_prefix_sibling_is_not_swallowed(self):
        """`/Books/Math-old` is not under `/Books/Math`; '-' sorts below '0'."""
        storage = self._db([
            (1, "/Books/Math/ЛинАл", "a.pdf", "file"),
            (1, "/Books/Math-old/Ancient", "b.pdf", "file"),
        ])
        index = ChildIndex(storage)
        self.assertEqual(index.children(1, "/Books/Math"), ["ЛинАл"])
        self.assertEqual(index.children(1, "/Books/Math-old"), ["Ancient"])
        self.assertEqual(index.children(1, "/Books"), ["Math", "Math-old"])

    def test_recorded_directories_still_win_over_inferred_names(self):
        """Precedence is the old one: dir rows answer alone when they exist.

        `fetch_child_dirs` returned first and inference never ran, so a folder
        holding both a dir row and files under an unrecorded child listed only
        the recorded one. Preserved deliberately — this is a query-count
        change, not a semantics change.
        """
        storage = self._db([
            (1, "/Books", "Math", "dir"),
            (1, "/Books/Physics", "quantum.pdf", "file"),
        ])
        self.assertEqual(fetch_child_names(storage, 1, "/Books"), ["Math"])
        self.assertEqual(ChildIndex(storage).children(1, "/Books"), ["Math"])

    def test_one_scan_does_not_answer_for_another(self):
        storage = self._db([
            (1, "/Books/Math", "a.pdf", "file"),
            (2, "/pro/agents", "b.py", "file"),
        ])
        index = ChildIndex(storage)
        self.assertEqual(index.children(1, "/"), ["Books"])
        self.assertEqual(index.children(2, "/"), ["pro"])
        self._assert_agrees(storage, 1, ["/", "/Books"])
        self._assert_agrees(storage, 2, ["/", "/pro"])

    def test_a_missing_scan_has_no_children_rather_than_raising(self):
        storage = self._db([(1, "/Books/Math", "a.pdf", "file")])
        self.assertEqual(ChildIndex(storage).children(99, "/"), [])


class FolderFileCountsTests(unittest.TestCase):
    """Reading the counts once must answer what asking per node did.

    Same discipline as `ChildIndexTests`: the two functions it replaces are
    the specification, so what is checked is that they agree, not that the
    index matches numbers written down by hand.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "monitor.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE files (
                id INTEGER PRIMARY KEY, scan_id INTEGER, parent_path TEXT,
                name TEXT, type TEXT, size INTEGER, modified TEXT, md5 TEXT, path TEXT
            );
            CREATE UNIQUE INDEX idx_files_unique ON files (scan_id, parent_path, name);
            """
        )
        rows = [
            # `/Books/Math-old` pins the boundary: '-' sorts below '0', so a
            # range starting at the folder name rather than at its slash would
            # count it as a descendant of `/Books/Math`.
            (1, "/Books", "readme.txt", "file"),
            (1, "/Books/Math", "a.pdf", "file"),
            (1, "/Books/Math", "b.pdf", "file"),
            (1, "/Books/Math/ЛинАл", "c.pdf", "file"),
            (1, "/Books/Math-old", "d.pdf", "file"),
            (1, "/Books", "Math", "dir"),
            # A second scan, so the cache cannot answer for the wrong one.
            (2, "/pro", "main.py", "file"),
            # Relative spelling, which `_count_files_for_prefix` covers with
            # its three variants.
            (3, "pro/agents", "x.py", "file"),
        ]
        conn.executemany(
            "INSERT INTO files (scan_id, parent_path, name, type, size)"
            " VALUES (?, ?, ?, ?, 1)",
            rows,
        )
        conn.commit()
        conn.close()
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_the_per_subtree_counts_are_the_same(self):
        counts = FolderFileCounts()
        cases = [
            (1, ""), (1, "/Books"), (1, "/Books/Math"), (1, "/Books/Math-old"),
            (1, "/Books/Math/ЛинАл"), (1, "/nothing"), (2, ""), (2, "/pro"),
            (3, ""), (99, ""), (99, "/Books"),
        ]
        for scan_id, subtree in cases:
            with self.subTest(scan_id=scan_id, subtree=subtree):
                self.assertEqual(
                    _folder_file_counts(self.conn, scan_id, subtree),
                    counts.in_subtree(self.conn, scan_id, subtree),
                )

    def test_the_totals_at_and_under_a_prefix_are_the_same(self):
        counts = FolderFileCounts()
        cases = [
            (1, ""), (1, "Books"), (1, "/Books"), (1, "Books/Math"),
            (1, "/Books/Math"), (1, "Books/Math/"), (1, "Books/Math-old"),
            (1, "Books/Math/ЛинАл"), (1, "nothing"), (2, "pro"),
            (3, "pro"), (3, "pro/agents"), (99, "Books"),
        ]
        for scan_id, prefix in cases:
            with self.subTest(scan_id=scan_id, prefix=prefix):
                self.assertEqual(
                    _count_files_for_prefix(self.conn, scan_id, prefix),
                    counts.at_and_under(self.conn, scan_id, prefix),
                )

    def test_a_prefix_sibling_is_not_counted_as_a_descendant(self):
        counts = FolderFileCounts()
        self.assertEqual(counts.at_and_under(self.conn, 1, "Books/Math"), 3)
        self.assertEqual(counts.at_and_under(self.conn, 1, "Books/Math-old"), 1)
        self.assertEqual(counts.at_and_under(self.conn, 1, "Books"), 5)

    def test_the_composite_count_is_unchanged_by_the_index(self):
        """The end the index exists for, checked against the un-indexed path."""
        storage = _Storage(self.db_path)

        class _Analyzer:
            def __init__(self, storage):
                self.storage = storage

        analyzer = _Analyzer(storage)
        snapshot = CompositeSnapshot(base_scan_id=1, folder_updates={})
        counts = FolderFileCounts()
        for path in ("/", "/Books", "/Books/Math", "/Books/Math-old", "/nope"):
            with self.subTest(path=path):
                self.assertEqual(
                    count_cloud_files_for_path(analyzer, snapshot, path),
                    count_cloud_files_for_path(analyzer, snapshot, path, counts=counts),
                )


class TreeQueryBudgetTests(unittest.TestCase):
    """How many queries the walk costs, which is the cost that survived.

    Phase 9 made a query cheap; on the Android device that helped less than it
    reads, because proot bills every system call and a query still costs half a
    millisecond. What is left to cut is the number of them — 50 813 for 3 801
    nodes on 2026-08-28. Wall-clock cannot police that from this machine (it is
    5-15x faster, so the win hides in the noise), but a query count is the same
    number on both.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "monitor.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE files (
                id INTEGER PRIMARY KEY, scan_id INTEGER, parent_path TEXT,
                name TEXT, type TEXT, size INTEGER, modified TEXT, md5 TEXT, path TEXT
            );
            CREATE UNIQUE INDEX idx_files_unique ON files (scan_id, parent_path, name);
            CREATE TABLE scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME,
                scan_type TEXT NOT NULL, status TEXT NOT NULL, duration REAL,
                scan_root TEXT, scan_depth INTEGER
            );
            INSERT INTO scans (id, scan_type, status, duration)
                VALUES (1, 'cloud', 'success', 1);
            """
        )
        # No dir rows anywhere: the shape the device actually has, and the one
        # that used to cost the most, because every node fell through to
        # inference and then to the fallback loop behind it.
        rows = []
        self.folders = 0

        def grow(prefix, depth):
            self.folders += 1
            if depth == 0:
                return
            for index in range(4):
                child = f"{prefix}/d{depth}_{index}"
                rows.append((1, child, "file.bin", "file"))
                grow(child, depth - 1)

        grow("", 4)
        conn.executemany(
            "INSERT INTO files (scan_id, parent_path, name, type, size)"
            " VALUES (?, ?, ?, ?, 1)",
            rows,
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _walk(self, depth, with_percent=False):
        storage = StorageManager(self.db_path, use_temp_storage=False)
        analyzer = Analyzer(storage)
        snapshot = CompositeSnapshot(base_scan_id=1, folder_updates={})
        with counting_queries() as queries:
            # As the CLI does it. Outside this block every node opens its own
            # connection, and the PRAGMAs that costs drown out what is being
            # measured — which is how a first reading of these numbers came
            # out four times too high.
            with storage.reuse_connection():
                tree = build_tree(analyzer, snapshot, "/", depth)
                if with_percent:
                    apply_sync_percent(tree, analyzer, snapshot, 1, "/nowhere")

        def count(node):
            return 1 + sum(count(child) for child in node.children)

        return count(tree), queries

    def test_the_walk_does_not_pay_per_node(self):
        """The point of the change, stated as the thing that must not return.

        Before Phase 12 this was 9.2 queries per node at depth 4 and rose with
        depth, because every node asked its own questions. The bound is the
        number of scans in the snapshot, not the number of nodes.
        """
        nodes, queries = self._walk(4)
        self.assertGreater(nodes, 300, "the fixture must be big enough to matter")
        self.assertLess(queries.count, 20, queries.report())

    def test_a_deeper_walk_costs_the_same(self):
        """Depth 3 visits a quarter of the nodes depth 4 does.

        Under the old lookup that showed up directly in the query count. If it
        does again, something has gone back to asking per node.
        """
        shallow_nodes, shallow = self._walk(3)
        deep_nodes, deep = self._walk(4)
        self.assertGreater(deep_nodes, shallow_nodes * 3)
        self.assertEqual(deep.count, shallow.count, deep.report())

    def test_filling_in_the_counts_does_not_pay_per_node_either(self):
        """The counting half, which was the other 23 000 queries.

        `apply_sync_percent` asked for a file count per node, and the
        composite asked again for every scan serving an update beneath it.
        Both answers come from one `GROUP BY` per scan now.
        """
        nodes, queries = self._walk(4, with_percent=True)
        self.assertGreater(nodes, 300)
        self.assertLess(queries.count, 30, queries.report())

    def test_the_counts_do_not_grow_with_depth(self):
        _shallow_nodes, shallow = self._walk(3, with_percent=True)
        _deep_nodes, deep = self._walk(4, with_percent=True)
        self.assertEqual(deep.count, shallow.count, deep.report())


class SnapshotSelectionQueryBudgetTests(unittest.TestCase):
    """What choosing a snapshot costs, which is what Phase 12 left behind.

    Phase 12 stopped the walk paying per node. The cost that remained does
    not scale with nodes at all — it scales with how many scans the database
    holds, and it was invisible next to the walk until the walk got cheap.
    On the author's database an `orphans` run spent 872 queries, 360 of them
    opening 180 connections to ask questions that had already been answered;
    the device reported the same shape at its own scale. See
    `tasks/ydm_menu/BACKLOG.md`, Phase 13.
    """

    SCANS = 40

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "monitor.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE files (
                id INTEGER PRIMARY KEY, scan_id INTEGER, parent_path TEXT,
                name TEXT, type TEXT, size INTEGER, modified TEXT, md5 TEXT, path TEXT
            );
            CREATE UNIQUE INDEX idx_files_unique ON files (scan_id, parent_path, name);
            CREATE TABLE scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME,
                scan_type TEXT NOT NULL, status TEXT NOT NULL, duration REAL,
                scan_root TEXT, scan_depth INTEGER
            );
            CREATE TABLE scan_progress (
                id INTEGER PRIMARY KEY, scan_id INTEGER, path TEXT,
                status TEXT, last_checked DATETIME
            );
            """
        )
        # One full scan, then a history of partial ones on top: the shape a
        # database grows into, and the one that makes the selection walk the
        # list. Every partial scan is asked about, repeatedly.
        conn.execute(
            "INSERT INTO scans (id, scan_type, status, duration) VALUES (1, 'cloud', 'success', 1)"
        )
        conn.executemany(
            "INSERT INTO files (scan_id, parent_path, name, type, size) VALUES (1, ?, ?, ?, 1)",
            [("", "A", "dir"), ("/A", "f.txt", "file")],
        )
        for scan_id in range(2, self.SCANS + 1):
            conn.execute(
                "INSERT INTO scans (id, scan_type, status, duration) VALUES (?, 'cloud', 'success', 1)",
                (scan_id,),
            )
            conn.execute(
                "INSERT INTO files (scan_id, parent_path, name, type, size)"
                " VALUES (?, '/A', ?, 'file', 1)",
                (scan_id, f"f{scan_id}.txt"),
            )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _select(self):
        storage = StorageManager(self.db_path, use_temp_storage=False)
        analyzer = Analyzer(storage)
        with counting_queries() as queries:
            select_snapshot_for_tree(analyzer, "/")
        return queries

    def test_choosing_a_snapshot_opens_one_connection(self):
        """Not one per question asked along the way.

        `PRAGMA journal_mode=WAL` runs once per connection, so counting it
        counts connections — the 41% of the run that was pure overhead.
        """
        queries = self._select()
        opened = queries.by_statement.get("PRAGMA journal_mode=WAL", 0)
        self.assertEqual(opened, 1, queries.report())

    def test_no_scan_is_asked_the_same_question_twice(self):
        """Three readers wanted the same row; they used to fetch it each.

        The bound is per statement rather than a total, because the total
        legitimately grows with the number of scans. What must not come back
        is the same question about the same scan, repeated.
        """
        queries = self._select()
        repeated = {
            statement: times
            for statement, times in queries.by_statement.items()
            if times > 1 and statement.startswith("SELECT scan_root, scan_depth")
        }
        self.assertEqual(repeated, {}, queries.report(limit=10))


if __name__ == "__main__":
    unittest.main()
