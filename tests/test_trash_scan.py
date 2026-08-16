#!/usr/bin/env python3
"""Unit tests for tools/trash_scan.py.

Everything here runs offline against a fake API client and temp SQLite files.
The restore paths are the ones that matter: an HTTP 202 must never be recorded
as a completed restore, because `restore-plan` skips entries that have a
`success` row and a file that never came back would then be skipped forever.
"""
from __future__ import annotations

import argparse
import contextlib
import io
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

from tools import trash_scan  # noqa: E402


class FakeClient:
    """Stands in for YandexTrashClient. Records calls, replays scripted answers."""

    def __init__(self, restore_results, operation_results=None):
        self.restore_results = list(restore_results)
        self.operation_results = operation_results or {}
        self.restore_calls = []
        self.operation_calls = []

    def restore(self, trash_path, overwrite=False):
        self.restore_calls.append((trash_path, overwrite))
        return self.restore_results.pop(0)

    def get_operation(self, href):
        self.operation_calls.append(href)
        results = self.operation_results[href]
        return results.pop(0) if len(results) > 1 else results[0]


class PathHelperTests(unittest.TestCase):
    def test_restore_path_for_nested_entry(self):
        self.assertEqual(
            trash_scan.restore_path_for("trash:/Books_abc", "/Books", "trash:/Books_abc/Math/a.pdf"),
            "/Books/Math/a.pdf",
        )

    def test_restore_path_for_the_root_itself(self):
        self.assertEqual(
            trash_scan.restore_path_for("trash:/Books_abc", "/Books", "trash:/Books_abc"),
            "/Books",
        )

    def test_operation_href_from(self):
        self.assertEqual(
            trash_scan.operation_href_from('{"href": "https://x/operations/1"}'),
            "https://x/operations/1",
        )
        self.assertIsNone(trash_scan.operation_href_from("not json"))
        self.assertIsNone(trash_scan.operation_href_from("{}"))


class PollOperationTests(unittest.TestCase):
    def _client(self, statuses):
        return FakeClient([], {"href": [{"status": s} for s in statuses]})

    def test_resolves_to_success(self):
        client = self._client(["in-progress", "success"])
        status, payload = trash_scan.poll_operation(
            client, "href", interval_sec=0, timeout_sec=10, sleep=lambda _: None
        )
        self.assertEqual(status, "success")
        self.assertEqual(payload["status"], "success")

    def test_resolves_to_failed(self):
        client = self._client(["failed"])
        status, _ = trash_scan.poll_operation(client, "href", sleep=lambda _: None)
        self.assertEqual(status, "failed")

    def test_timeout_reports_accepted_not_success(self):
        client = self._client(["in-progress"])
        clock = iter([0.0, 100.0, 200.0])
        status, _ = trash_scan.poll_operation(
            client,
            "href",
            interval_sec=0,
            timeout_sec=1,
            sleep=lambda _: None,
            now=lambda: next(clock),
        )
        self.assertEqual(status, "accepted")


class RestoreFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ydm_trash_")
        self.db_path = os.path.join(self.tmpdir, "trash.db")
        trash_scan.StorageManager(self.db_path, use_temp_storage=False).init_db()
        trash_scan.ensure_extra_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO scans (id, scan_type, status) VALUES (1,'trash','success')")
        conn.execute(
            """
            INSERT INTO trash_entries
            (id, scan_id, restore_path, restore_parent_path, name, type, size, md5, trash_path)
            VALUES (1, 1, '/Books/a.pdf', '/Books', 'a.pdf', 'file', 10, 'aa', 'trash:/B/a.pdf')
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _args(self, **overrides):
        ns = argparse.Namespace(
            db_path=self.db_path,
            scan_id=1,
            restore_root="/Books",
            prefix=None,
            remote="yandex",
            token_source="auto",
            poll=True,
            poll_interval_sec=0,
            poll_timeout_sec=10,
            limit=10,
            order="path",
            retry_failed=False,
            overwrite=False,
            sleep_sec=0,
            progress=False,
            stop_on_error=False,
            apply=True,
            yes="RESTORE",
        )
        for key, value in overrides.items():
            setattr(ns, key, value)
        return ns

    def _ops(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT status, http_status, operation_href FROM restore_ops ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def test_200_is_recorded_as_success(self):
        client = FakeClient([(200, "{}")])
        result = trash_scan.restore_files(self._args(), client=client)
        self.assertEqual(result["successes"], 1)
        self.assertEqual(self._ops(), [("success", 200, None)])

    def test_202_polled_to_success(self):
        client = FakeClient(
            [(202, '{"href": "op1"}')],
            {"op1": [{"status": "in-progress"}, {"status": "success"}]},
        )
        result = trash_scan.restore_files(self._args(), client=client)
        self.assertEqual(result["successes"], 1)
        self.assertEqual(self._ops(), [("success", 202, "op1")])

    def test_202_polled_to_failed_is_not_success(self):
        client = FakeClient([(202, '{"href": "op1"}')], {"op1": [{"status": "failed"}]})
        result = trash_scan.restore_files(self._args(), client=client)
        self.assertEqual(result["successes"], 0)
        self.assertEqual(result["errors_count"], 1)
        self.assertEqual(self._ops(), [("failed", 202, "op1")])

    def test_202_without_polling_stays_accepted(self):
        client = FakeClient([(202, '{"href": "op1"}')])
        result = trash_scan.restore_files(self._args(poll=False), client=client)
        self.assertEqual(result["successes"], 0)
        self.assertEqual(result["accepted_unresolved"], 1)
        self.assertEqual(self._ops(), [("accepted", 202, "op1")])

    def test_an_unresolved_entry_is_selected_again(self):
        """The whole point: only a 'success' row may retire an entry."""
        trash_scan.restore_files(self._args(poll=False), client=FakeClient([(202, '{"href": "op1"}')]))
        client = FakeClient([(200, "{}")])
        result = trash_scan.restore_files(self._args(), client=client)
        self.assertEqual(result["selected"], 1)
        self.assertEqual(client.restore_calls, [("trash:/B/a.pdf", False)])

    def test_a_successful_entry_is_not_selected_again(self):
        trash_scan.restore_files(self._args(), client=FakeClient([(200, "{}")]))
        client = FakeClient([])
        result = trash_scan.restore_files(self._args(), client=client)
        self.assertEqual(result["selected"], 0)
        self.assertEqual(client.restore_calls, [])

    def test_poll_ops_resolves_leftovers(self):
        trash_scan.restore_files(self._args(poll=False), client=FakeClient([(202, '{"href": "op1"}')]))
        args = argparse.Namespace(
            db_path=self.db_path,
            scan_id=1,
            remote="yandex",
            token_source="auto",
            poll=True,
            poll_interval_sec=0,
            poll_timeout_sec=10,
            limit=100,
        )
        result = trash_scan.poll_ops(args, client=FakeClient([], {"op1": [{"status": "success"}]}))
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["resolved_success"], 1)
        self.assertEqual(self._ops()[0][0], "success")


class CompareMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ydm_trash_cmp_")
        self.db_path = os.path.join(self.tmpdir, "trash.db")
        self.monitor_path = os.path.join(self.tmpdir, "monitor.db")
        trash_scan.StorageManager(self.db_path, use_temp_storage=False).init_db()
        trash_scan.ensure_extra_schema(self.db_path)
        trash_scan.StorageManager(self.monitor_path, use_temp_storage=False).init_db()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed(self, trash_files, monitor_files):
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO scans (id, scan_type, status) VALUES (1,'trash','success')")
        for name, size, md5 in trash_files:
            conn.execute(
                """
                INSERT INTO trash_entries
                (scan_id, restore_path, restore_parent_path, name, type, size, md5, trash_path)
                VALUES (1, ?, '/Books', ?, 'file', ?, ?, ?)
                """,
                (f"/Books/{name}", name, size, md5, f"trash:/B/{name}"),
            )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(self.monitor_path)
        conn.execute("INSERT INTO scans (id, scan_type, status) VALUES (7,'cloud','success')")
        for name, size, md5 in monitor_files:
            conn.execute(
                "INSERT INTO files (scan_id,parent_path,name,type,size,md5) "
                "VALUES (7,'/Books',?,'file',?,?)",
                (name, size, md5),
            )
        conn.commit()
        conn.close()

    def _run(self):
        args = argparse.Namespace(
            db_path=self.db_path,
            scan_id=1,
            monitor_db=self.monitor_path,
            monitor_scan_id=7,
            restore_root="/Books",
            limit=10,
        )
        return trash_scan.compare_monitor(args)

    def test_identical_sets_report_no_mismatch(self):
        files = [("a.pdf", 10, "aa"), ("b.pdf", 20, "bb")]
        self._seed(files, files)
        result = self._run()
        self.assertEqual(result["matched_files"], 2)
        self.assertEqual(result["size_mismatch_matched_files"], 0)
        self.assertEqual(result["md5_mismatch_matched_files"], 0)
        self.assertEqual(result["missing_in_trash_sample"], [])
        self.assertEqual(result["mismatch_sample"], [])

    def test_size_and_md5_mismatches_are_counted_and_sampled(self):
        self._seed(
            [("a.pdf", 11, "aa"), ("b.pdf", 20, "ZZ")],
            [("a.pdf", 10, "aa"), ("b.pdf", 20, "bb")],
        )
        result = self._run()
        self.assertEqual(result["matched_files"], 2)
        self.assertEqual(result["size_mismatch_matched_files"], 1)
        self.assertEqual(result["md5_mismatch_matched_files"], 1)
        self.assertEqual(
            sorted(row["restore_path"] for row in result["mismatch_sample"]),
            ["/Books/a.pdf", "/Books/b.pdf"],
        )

    def test_file_missing_from_the_trash_snapshot_is_listed(self):
        self._seed([("a.pdf", 10, "aa")], [("a.pdf", 10, "aa"), ("gone.pdf", 5, "cc")])
        result = self._run()
        self.assertEqual(result["missing_in_trash_sample"], ["/Books/gone.pdf"])
        self.assertEqual(result["matched_files"], 1)


class CliTests(unittest.TestCase):
    def test_roots_are_required(self):
        for argv in (
            ["scan", "--trash-root", "trash:/X"],
            ["restore-root", "--restore-root", "/Books"],
            ["restore-files"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    trash_scan.parse_args(argv)

    def test_no_incident_specific_defaults_remain(self):
        source = (ROOT / "tools" / "trash_scan.py").read_text(encoding="utf-8")
        self.assertNotIn("Books_25639b9fb1cee52a5b58811baffa033cbf2896a3", source)

    def test_poll_defaults_to_on(self):
        args = trash_scan.parse_args(["restore-files", "--restore-root", "/Books"])
        self.assertTrue(args.poll)
        self.assertEqual(args.token_source, "auto")


if __name__ == "__main__":
    unittest.main()
