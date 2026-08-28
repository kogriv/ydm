#!/usr/bin/env python3
"""Rename detection: the baseline must describe the mirror being scanned.

`sync_rename` finds renames by diffing the two most recent successful local
scans. Until 2026-08-28 it took the newest local scan whatever tree it had
covered, so a single hand-run preflight against a different `--local-root`
became the baseline for the next run, and the difference between two unrelated
directories was read as renames. On the author's device that produced two
`blocked` candidates pointing at `/RCLONE_TEST` — the bisync sentinel file,
matched by size — and stopped the scheduled sync for a cycle. Issue #14.

Nothing here shells out to rclone: every check stops before a candidate would
need confirming against the remote, which is also the point — with the baseline
fixed, unrelated trees never reach that stage.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sync_rename  # noqa: E402
from tools.sync_common import normalized_local_root, run_local_scan  # noqa: E402
from tools.sync_rename import (  # noqa: E402
    cmd_detect,
    cmd_preflight,
    decide_no_baseline,
    latest_successful_local_scan_id,
)


def make_tree(base: str, names) -> str:
    os.makedirs(base, exist_ok=True)
    for name in names:
        path = os.path.join(base, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(name)
    return base


class NormalizationTests(unittest.TestCase):
    """Both sides of the comparison have to agree on what a path is."""

    def test_a_trailing_slash_does_not_make_a_different_mirror(self):
        self.assertEqual(
            normalized_local_root("/tmp/mirror/"), normalized_local_root("/tmp/mirror")
        )

    def test_a_relative_path_is_resolved(self):
        self.assertTrue(normalized_local_root("mirror").startswith("/"))

    def test_the_root_directory_survives_stripping(self):
        self.assertEqual(normalized_local_root("/"), "/")

    def test_a_home_relative_path_is_expanded(self):
        self.assertEqual(
            normalized_local_root("~/mirror"),
            os.path.join(os.path.expanduser("~"), "mirror"),
        )


class ScanAttributionTests(unittest.TestCase):
    """A local scan records which tree it walked."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "monitor.db")
        self.mirror = make_tree(os.path.join(self.tmpdir, "mirror"), ["a.txt", "sub/b.txt"])
        self.other = make_tree(os.path.join(self.tmpdir, "other"), ["c.txt"])

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _scan_root_of(self, scan_id):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT scan_root FROM scans WHERE id = ?", (scan_id,)
            ).fetchone()[0]
        finally:
            conn.close()

    def test_a_tool_driven_local_scan_records_its_root(self):
        result = run_local_scan(self.db_path, self.mirror)
        self.assertTrue(result.started, result.error)
        self.assertEqual(self._scan_root_of(result.scan_id), self.mirror)

    def test_the_recorded_root_is_normalized_not_verbatim(self):
        result = run_local_scan(self.db_path, self.mirror + "/")
        self.assertTrue(result.started, result.error)
        self.assertEqual(self._scan_root_of(result.scan_id), self.mirror)

    def test_the_newest_scan_of_this_mirror_is_the_baseline(self):
        first = run_local_scan(self.db_path, self.mirror)
        second = run_local_scan(self.db_path, self.mirror)
        self.assertEqual(
            latest_successful_local_scan_id(self.db_path, self.mirror), second.scan_id
        )
        self.assertNotEqual(first.scan_id, second.scan_id)

    def test_a_newer_scan_of_another_tree_is_not_the_baseline(self):
        """The defect, reduced to one assertion.

        The foreign scan is newer, so the old query returned it and the next
        run diffed two unrelated directories.
        """
        mine = run_local_scan(self.db_path, self.mirror)
        foreign = run_local_scan(self.db_path, self.other)
        self.assertGreater(foreign.scan_id, mine.scan_id)
        self.assertEqual(
            latest_successful_local_scan_id(self.db_path, self.mirror), mine.scan_id
        )

    def test_a_mirror_never_scanned_here_has_no_baseline(self):
        run_local_scan(self.db_path, self.other)
        self.assertIsNone(latest_successful_local_scan_id(self.db_path, self.mirror))

    def test_an_unattributed_scan_belongs_to_no_mirror(self):
        """Rows written before the root was recorded cannot be claimed.

        Treating NULL as "matches anything" would have kept the defect alive
        for every database that already existed — which, when this landed, was
        all of them.
        """
        run_local_scan(self.db_path, self.mirror)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE scans SET scan_root = NULL")
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(latest_successful_local_scan_id(self.db_path, self.mirror))
        self.assertIsNotNone(latest_successful_local_scan_id(self.db_path))


class NoBaselineDecisionTests(unittest.TestCase):
    """Nothing to compare against is not the same as nothing changed."""

    def test_guard_skips_rather_than_running_unguarded(self):
        decision = decide_no_baseline("guard")
        self.assertEqual(decision["decision"], "skip_bisync")
        self.assertEqual(decision["reason"], "no_comparable_baseline")

    def test_auto_skips_too(self):
        self.assertEqual(decide_no_baseline("auto")["decision"], "skip_bisync")

    def test_observe_mode_still_gets_out_of_the_way(self):
        decision = decide_no_baseline("observe")
        self.assertEqual(decision["decision"], "allow_bisync")
        self.assertEqual(decision["reason"], "observe_mode")


class DetectWithoutABaselineTests(unittest.TestCase):
    """End to end, without a remote: the first run on a mirror."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "monitor.db")
        self.var_dir = os.path.join(self.tmpdir, "var")
        os.makedirs(self.var_dir, exist_ok=True)
        self._saved_var = os.environ.get("YDM_VAR_DIR")
        os.environ["YDM_VAR_DIR"] = self.var_dir
        self.mirror = make_tree(os.path.join(self.tmpdir, "mirror"), ["a.txt", "sub/b.txt"])
        self.other = make_tree(os.path.join(self.tmpdir, "other"), ["c.txt"])

    def tearDown(self):
        if self._saved_var is None:
            os.environ.pop("YDM_VAR_DIR", None)
        else:
            os.environ["YDM_VAR_DIR"] = self._saved_var
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _args(self, local_root, mode="guard"):
        return argparse.Namespace(
            db_path=self.db_path,
            local_root=local_root,
            remote="nonexistent-remote",
            policy_path=os.path.join(self.tmpdir, "sync_policy.json"),
            rename_policy_path=os.path.join(self.tmpdir, "rename_policy.json"),
            bisync_filter_path=os.path.join(self.tmpdir, "mirror.bisync.filters"),
            mode=mode,
            notify=False,
        )

    def test_the_first_run_skips_and_leaves_a_baseline_behind(self):
        payload = cmd_detect(self._args(self.mirror))
        self.assertIsNone(payload["previous_scan_id"])
        self.assertEqual(payload["decision"]["decision"], "skip_bisync")
        self.assertIsNone(payload["error"])
        self.assertIsNotNone(payload["current_scan_id"])
        self.assertEqual(
            latest_successful_local_scan_id(self.db_path, self.mirror),
            payload["current_scan_id"],
        )

    def test_the_skip_survives_preflight_rather_than_being_recomputed(self):
        """`cmd_preflight` derives its own verdict from the candidate list.

        An empty list there means "nothing changed", so without carrying the
        reason across, a run that could not compare anything would report
        `allow_bisync (no_candidates)` — the fail-open this fixes.
        """
        payload = cmd_preflight(self._args(self.mirror))
        self.assertEqual(payload["decision"], "skip_bisync")
        self.assertEqual(payload["reason"], "no_comparable_baseline")
        self.assertIsNone(payload["error"])

    def test_a_scan_of_another_tree_does_not_become_this_mirrors_baseline(self):
        """The reported incident, end to end.

        The foreign scan runs first and is newer than nothing at all; under the
        old query it was the baseline, and the diff of two unrelated trees
        produced candidates. Here it is ignored, so the mirror's first run is
        still a first run.
        """
        run_local_scan(self.db_path, self.other)
        payload = cmd_detect(self._args(self.mirror))
        self.assertIsNone(payload["previous_scan_id"])
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["decision"]["decision"], "skip_bisync")

    def test_observe_mode_never_stops_bisync_even_without_a_baseline(self):
        payload = cmd_preflight(self._args(self.mirror, mode="observe"))
        self.assertEqual(payload["decision"], "allow_bisync")

    def test_a_database_that_will_not_answer_skips_under_its_own_reason(self):
        """Unreadable and unscanned both mean "nothing to compare".

        They do not mean the same thing to whoever reads the log line:
        `no_comparable_baseline` reads as "you passed the wrong --local-root",
        which for a locked database sends them looking for a fault that is not
        there. Same verdict, different name.
        """
        run_local_scan(self.db_path, self.mirror)
        with patch.object(
            sync_rename, "latest_successful_local_scan_id",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            payload = cmd_detect(self._args(self.mirror))
        self.assertEqual(payload["decision"]["decision"], "skip_bisync")
        self.assertEqual(payload["decision"]["reason"], "baseline_unreadable")
        self.assertIsNone(payload["error"])
        # The scan is still taken, so a lock that clears costs one cycle.
        self.assertIsNotNone(payload["current_scan_id"])

    def test_the_unreadable_reason_survives_preflight_too(self):
        run_local_scan(self.db_path, self.mirror)
        with patch.object(
            sync_rename, "latest_successful_local_scan_id",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            payload = cmd_preflight(self._args(self.mirror))
        self.assertEqual(payload["decision"], "skip_bisync")
        self.assertEqual(payload["reason"], "baseline_unreadable")


class ADatabaseThatCannotAnswerTests(unittest.TestCase):
    """The query distinguishes "column absent" from "cannot read"."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "monitor.db")
        self.mirror = make_tree(os.path.join(self.tmpdir, "mirror"), ["a.txt"])

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _roll_back_to_the_schema_before_scan_root(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE scans_v0 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    scan_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration REAL
                );
                INSERT INTO scans_v0 (id, timestamp, scan_type, status, duration)
                    SELECT id, timestamp, scan_type, status, duration FROM scans;
                DROP TABLE scans;
                ALTER TABLE scans_v0 RENAME TO scans;
                """
            )
            conn.commit()
        finally:
            conn.close()

    def test_a_database_from_before_the_column_reports_no_baseline(self):
        """Every database in existence when this landed was one of these."""
        run_local_scan(self.db_path, self.mirror)
        self._roll_back_to_the_schema_before_scan_root()
        self.assertIsNone(latest_successful_local_scan_id(self.db_path, self.mirror))

    def test_the_column_comes_back_and_costs_exactly_one_cycle(self):
        run_local_scan(self.db_path, self.mirror)
        self._roll_back_to_the_schema_before_scan_root()
        first = run_local_scan(self.db_path, self.mirror)
        self.assertTrue(first.started, first.error)
        self.assertEqual(
            latest_successful_local_scan_id(self.db_path, self.mirror), first.scan_id
        )

    def test_a_damaged_database_is_not_reported_as_an_unattributed_mirror(self):
        """It raises, and the caller names it separately.

        The missing column is the one unreadable state that means "no
        baseline". Anything else — a damaged file, a database that will not
        open — is a different fault with the same shape, and answering it with
        `no_comparable_baseline` would put a wrong cause in the log line that
        exists to carry the right one. Corruption is the reachable case here:
        the database runs in WAL, where a reader is not blocked by a writer.
        """
        damaged = os.path.join(self.tmpdir, "damaged.db")
        with open(damaged, "wb") as handle:
            handle.write(b"not a database, but it is on disk" * 64)
        with self.assertRaises(sqlite3.DatabaseError):
            latest_successful_local_scan_id(damaged, self.mirror)


if __name__ == "__main__":
    unittest.main(verbosity=2)
