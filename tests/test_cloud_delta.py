#!/usr/bin/env python3
"""Unit tests for tools/cloud_delta.py — offline, against a fake API client.

The interesting logic is not the HTTP: it is deciding whether a change is
*news*. The composite snapshot has a different freshness date per folder, so
the same timestamp can be stale in one folder and already covered in another.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import cloud_delta  # noqa: E402


def dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


class FakeClient:
    """Serves canned pages; records how many were asked for."""

    def __init__(self, file_pages=None, trash_pages=None, trash_total=0):
        self.file_pages = list(file_pages or [])
        self.trash_pages = list(trash_pages or [])
        self.trash_total = trash_total
        self.file_calls = 0
        self.trash_calls = 0

    def recent_files(self, offset, limit=cloud_delta.PAGE_SIZE):
        self.file_calls += 1
        index = offset // cloud_delta.PAGE_SIZE
        return self.file_pages[index] if index < len(self.file_pages) else []

    def recent_trash(self, offset, limit=cloud_delta.PAGE_SIZE):
        self.trash_calls += 1
        index = offset // cloud_delta.PAGE_SIZE
        page = self.trash_pages[index] if index < len(self.trash_pages) else []
        return page, self.trash_total


def a_file(path, modified, size=10):
    return {"path": f"disk:{path}", "modified": modified, "size": size, "type": "file"}


def a_trash_entry(origin, deleted, kind="file"):
    return {
        "path": "trash:/x",
        "origin_path": f"disk:{origin}",
        "deleted": deleted,
        "type": kind,
    }


class TimeParsingTests(unittest.TestCase):
    """scans.timestamp is naive UTC; the API returns ISO with an offset.
    Comparing them without converting silently shifts everything by the
    machine's timezone (+07 here)."""

    def test_api_time_converted_to_utc(self):
        self.assertEqual(
            cloud_delta.parse_api_time("2026-08-15T01:53:18+00:00"),
            dt("2026-08-15 01:53:18"),
        )
        self.assertEqual(
            cloud_delta.parse_api_time("2026-08-15T08:53:18+07:00"),
            dt("2026-08-15 01:53:18"),
        )
        self.assertEqual(
            cloud_delta.parse_api_time("2026-08-15T01:53:18Z"),
            dt("2026-08-15 01:53:18"),
        )

    def test_db_time(self):
        self.assertEqual(cloud_delta.parse_db_time("2026-03-04 08:03:42"), dt("2026-03-04 08:03:42"))
        self.assertIsNone(cloud_delta.parse_db_time(None))
        self.assertIsNone(cloud_delta.parse_api_time("not a date"))

    def test_cloud_path_to_db(self):
        self.assertEqual(cloud_delta.cloud_path_to_db("disk:/A/B/f.txt"), "/A/B")
        self.assertEqual(cloud_delta.cloud_path_to_db("disk:/f.txt"), "")
        self.assertEqual(cloud_delta.cloud_path_to_db("/A/f.txt"), "/A")


class RescanRootsTests(unittest.TestCase):
    def test_nested_folders_collapse_to_their_ancestor(self):
        roots, root_stale = cloud_delta.rescan_roots([
            "/obsidian_vault",
            "/obsidian_vault/linalg",
            "/obsidian_vault/linalg/angem/assets",
            "/pro/salva",
        ])
        self.assertEqual(roots, ["/obsidian_vault", "/pro/salva"])
        self.assertFalse(root_stale)

    def test_sibling_prefix_is_not_an_ancestor(self):
        roots, _ = cloud_delta.rescan_roots(["/pro", "/protein"])
        self.assertEqual(roots, ["/pro", "/protein"])

    def test_disk_root_never_swallows_the_plan(self):
        # "" is the disk root; `scan cloud --path /` is the full scan this
        # tool exists to avoid, so it must not collapse everything into one.
        roots, root_stale = cloud_delta.rescan_roots(["", "/pro", "/Books/Math"])
        self.assertEqual(roots, ["/Books/Math", "/pro"])
        self.assertTrue(root_stale)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = cloud_delta.Snapshot(
            base_scan_id=72,
            folder_updates={"/pro": 94, "/pro/done": 95},
            scan_times={
                72: dt("2026-03-04 08:00:00"),
                94: dt("2026-08-15 13:14:00"),
                95: dt("2026-08-15 13:25:00"),
            },
        )

    def test_folder_with_a_patch_uses_the_patch(self):
        self.assertEqual(self.snapshot.covering_scan("/pro"), (94, dt("2026-08-15 13:14:00")))
        self.assertEqual(self.snapshot.covering_scan("/pro/done"), (95, dt("2026-08-15 13:25:00")))

    def test_folder_without_a_patch_falls_back_to_the_base(self):
        self.assertEqual(self.snapshot.covering_scan("/video"), (72, dt("2026-03-04 08:00:00")))

    def test_oldest_covered_at_is_the_sweep_floor(self):
        self.assertEqual(self.snapshot.oldest_covered_at, dt("2026-03-04 08:00:00"))


class SweepModifiedTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = cloud_delta.Snapshot(
            base_scan_id=72,
            folder_updates={"/pro": 94},
            scan_times={72: dt("2026-03-04 08:00:00"), 94: dt("2026-08-15 13:00:00")},
        )

    def _sweep(self, client, max_pages=5):
        return cloud_delta.sweep_modified(client, self.snapshot, max_pages=max_pages)

    def test_change_newer_than_the_folders_scan_is_stale(self):
        client = FakeClient([[a_file("/video/new.mp4", "2026-08-14T10:00:00+00:00")]])
        stale, examined, truncated = self._sweep(client)
        self.assertEqual(list(stale), ["/video"])
        self.assertEqual(stale["/video"]["changed_files"], 1)
        self.assertEqual(stale["/video"]["covering_scan_id"], 72)
        self.assertEqual(examined, 1)
        self.assertFalse(truncated)

    def test_change_already_covered_by_a_folder_patch_is_not_news(self):
        # Same timestamp, different folder: /pro was rescanned on 2026-08-15.
        client = FakeClient([[
            a_file("/pro/x.txt", "2026-08-14T10:00:00+00:00"),
            a_file("/video/x.txt", "2026-08-14T10:00:00+00:00"),
        ]])
        stale, _, _ = self._sweep(client)
        self.assertEqual(list(stale), ["/video"])

    def test_sweep_stops_at_the_snapshot_floor(self):
        client = FakeClient([
            [
                a_file("/video/new.mp4", "2026-08-14T10:00:00+00:00"),
                a_file("/video/old.mp4", "2026-01-01T10:00:00+00:00"),
            ],
            [a_file("/video/older.mp4", "2025-01-01T10:00:00+00:00")],
        ])
        stale, examined, truncated = self._sweep(client)
        self.assertEqual(stale["/video"]["changed_files"], 1)
        self.assertEqual(client.file_calls, 1, "must not page past the floor")
        self.assertEqual(examined, 2)
        self.assertFalse(truncated)

    def test_hitting_the_page_cap_is_reported_not_hidden(self):
        page = [a_file(f"/video/{i}.mp4", "2026-08-14T10:00:00+00:00")
                for i in range(cloud_delta.PAGE_SIZE)]
        client = FakeClient([page, page, page])
        stale, examined, truncated = self._sweep(client, max_pages=2)
        self.assertTrue(truncated)
        self.assertEqual(client.file_calls, 2)
        self.assertEqual(examined, 2 * cloud_delta.PAGE_SIZE)

    def test_examples_are_capped_per_folder(self):
        client = FakeClient([[
            a_file(f"/video/{i}.mp4", "2026-08-14T10:00:00+00:00") for i in range(10)
        ]])
        stale, _, _ = self._sweep(client)
        self.assertEqual(stale["/video"]["changed_files"], 10)
        self.assertEqual(len(stale["/video"]["examples"]), 3)


class SweepTrashTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = cloud_delta.Snapshot(
            base_scan_id=72,
            folder_updates={"/pro": 94},
            scan_times={72: dt("2026-03-04 08:00:00"), 94: dt("2026-08-15 13:00:00")},
        )

    def test_deletion_marks_the_origin_folder_stale(self):
        client = FakeClient(trash_pages=[[a_trash_entry("/video/gone.mp4", "2026-08-14T10:00:00+00:00")]],
                            trash_total=1)
        stale = {}
        examined, truncated = cloud_delta.sweep_trash(client, self.snapshot, stale, max_pages=3)
        self.assertEqual(stale["/video"]["deleted_entries"], 1)
        self.assertEqual(examined, 1)
        self.assertFalse(truncated)

    def test_deletion_older_than_the_folders_scan_is_not_news(self):
        client = FakeClient(trash_pages=[[a_trash_entry("/pro/gone.txt", "2026-08-14T10:00:00+00:00")]],
                            trash_total=1)
        stale = {}
        cloud_delta.sweep_trash(client, self.snapshot, stale, max_pages=3)
        self.assertEqual(stale, {})

    def test_deletion_and_change_land_in_the_same_bucket(self):
        stale, _, _ = cloud_delta.sweep_modified(
            FakeClient([[a_file("/video/new.mp4", "2026-08-14T10:00:00+00:00")]]),
            self.snapshot, max_pages=2,
        )
        cloud_delta.sweep_trash(
            FakeClient(trash_pages=[[a_trash_entry("/video/gone.mp4", "2026-08-14T11:00:00+00:00")]],
                       trash_total=1),
            self.snapshot, stale, max_pages=2,
        )
        self.assertEqual(stale["/video"]["changed_files"], 1)
        self.assertEqual(stale["/video"]["deleted_entries"], 1)


class RetryTests(unittest.TestCase):
    def _client(self):
        return cloud_delta.CloudDeltaClient("token", retries=3, sleep=lambda _: None)

    def _http_error(self, code):
        return urllib.error.HTTPError("url", code, "boom", {}, io_bytes(b'{"error":"x"}'))

    def test_server_error_is_retried_then_succeeds(self):
        client = self._client()
        responses = [self._http_error(500), FakeResponse({"revision": 7})]

        def fake_urlopen(request, timeout=None):
            nxt = responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(client.disk_revision(), 7)
        self.assertEqual(client.retried, 1)

    def test_client_error_is_not_retried(self):
        client = self._client()
        with patch("urllib.request.urlopen", side_effect=self._http_error(401)):
            with self.assertRaises(cloud_delta.CloudDeltaError) as ctx:
                client.disk_revision()
        self.assertIn("401", str(ctx.exception))
        self.assertEqual(client.retried, 0)
        self.assertEqual(client.requests, 1)

    def test_gives_up_after_the_retry_budget(self):
        client = self._client()
        with patch("urllib.request.urlopen", side_effect=self._http_error(500)):
            with self.assertRaises(cloud_delta.CloudDeltaError) as ctx:
                client.disk_revision()
        self.assertIn("after 3 attempts", str(ctx.exception))
        self.assertEqual(client.requests, 3)


class RevisionStateTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "cloud_revision.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_roundtrip(self):
        self.assertIsNone(cloud_delta.read_revision_state(self.path))
        cloud_delta.write_revision_state(self.path, 123, "2026-08-16 10:00:00", clean=True)
        state = cloud_delta.read_revision_state(self.path)
        self.assertEqual(state["revision"], 123)
        self.assertTrue(state["clean"])

    def test_a_revision_saved_while_stale_is_not_clean(self):
        """`check --save` records "nothing happened since then", which is not
        the same as "the snapshot is up to date" — only a clean run may let
        the next one skip its sweeps."""
        cloud_delta.write_revision_state(self.path, 123, "2026-08-16 10:00:00")
        self.assertIsNone(cloud_delta.read_revision_state(self.path)["clean"])

    def test_corrupt_state_is_ignored_not_fatal(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(cloud_delta.read_revision_state(self.path))


class CliTests(unittest.TestCase):
    def test_format_accepted_on_either_side_of_the_subcommand(self):
        self.assertEqual(cloud_delta.parse_args(["--format", "json", "check"]).format, "json")
        self.assertEqual(cloud_delta.parse_args(["check", "--format", "json"]).format, "json")
        self.assertEqual(cloud_delta.parse_args(["check"]).format, "text")

    def test_changes_defaults(self):
        args = cloud_delta.parse_args(["changes"])
        self.assertEqual(args.token_source, "auto")
        self.assertEqual(args.max_pages, 20)
        self.assertFalse(args.no_trash)


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self, *_args):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def io_bytes(data):
    import io

    return io.BytesIO(data)


if __name__ == "__main__":
    unittest.main()
