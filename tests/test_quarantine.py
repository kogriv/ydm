#!/usr/bin/env python3
"""Files the sync sets aside because the cloud will not hand them over.

2026-10-29: Yandex answered 500/503 for a few percent of files, and bisync on
the device (rclone 1.60) aborted the whole resync on two of them. They were
excluded by hand in the filter file, which `render-filters` rewrites from the
policy — the next rewrite would have put them back without a word. In the
policy they survive, carry a reason, and are rendered ahead of the includes,
escaped, so a name like `x [1].jpg` matches itself and not `x1.jpg`.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sync_policy  # noqa: E402
from tools.sync_common import load_sync_filters  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from test_sync_policy_daemon import seed_cloud_db  # noqa: E402

ODD = "Books/Deti/Фото/16 В ремесленном училище [1].jpg"


class QuarantineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ydm_quarantine_")
        self.local_root = os.path.join(self.tmp, "ya_disk")
        os.makedirs(self.local_root)
        self.policy_path = os.path.join(self.tmp, "sync_policy.json")
        policy = sync_policy.empty_policy(self.local_root, "yandex")
        policy["paths"] = {"Books/Deti": {"mode": "bidirectional"},
                           "pro/physon": {"mode": "download_only"},
                           "Books": {"mode": "disabled"}}
        sync_policy.write_policy(self.policy_path, policy)
        seed_cloud_db(os.path.join(self.tmp, "monitor.db"), tree={
            "": ["Books", "pro"], "/Books": ["Deti", "Stargardt_FB2_[rutor.info]"],
            "/pro": ["physon"]})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ns(self, **kw):
        base = dict(policy_path=self.policy_path, path=None, reason=None, remove=False,
                    apply=True, local_root=self.local_root, remote="yandex",
                    download_filter_path=None, bisync_filter_path=None, format="json",
                    text_header=False, db_path=os.path.join(self.tmp, "monitor.db"))
        base.update(kw)
        return argparse.Namespace(**base)

    def quarantine(self, **kw):
        return sync_policy.quarantine_policy_path(self.ns(**kw))

    def render(self):
        policy = sync_policy.load_policy(self.policy_path)
        return sync_policy.render_filters(self.ns(), policy)


class TestQuarantineCommand(QuarantineCase):
    def test_add_records_reason_and_time(self):
        result = self.quarantine(path=f"/{ODD}", reason="Yandex 503 since 2026-10-29")
        self.assertIsNone(result["error"])
        entry = sync_policy.load_policy(self.policy_path)["quarantine"][ODD]
        self.assertEqual(entry["reason"], "Yandex 503 since 2026-10-29")
        self.assertIn("since", entry)

    def test_reason_is_required(self):
        self.assertIn("--reason", self.quarantine(path=f"/{ODD}")["error"])
        self.assertNotIn("quarantine", sync_policy.load_policy(self.policy_path))

    def test_path_outside_every_synced_path_is_refused(self):
        result = self.quarantine(path="/Books/Math/x.pdf", reason="r")
        self.assertIn("not inside any synced path", result["error"])

    def test_download_only_paths_count_as_synced(self):
        self.assertIsNone(self.quarantine(path="/pro/physon/a.pdf", reason="r")["error"])

    def test_dry_run_writes_nothing(self):
        self.quarantine(path=f"/{ODD}", reason="r", apply=False)
        self.assertNotIn("quarantine", sync_policy.load_policy(self.policy_path))

    def test_remove_puts_it_back_and_drops_the_empty_section(self):
        self.quarantine(path=f"/{ODD}", reason="r")
        result = self.quarantine(path=f"/{ODD}", remove=True)
        self.assertIsNone(result["error"])
        self.assertNotIn("quarantine", sync_policy.load_policy(self.policy_path))

    def test_removing_what_is_not_there_says_so(self):
        self.assertIn("not in quarantine", self.quarantine(path=f"/{ODD}", remove=True)["error"])

    def test_listing(self):
        self.quarantine(path=f"/{ODD}", reason="r")
        self.assertIn(ODD, self.quarantine()["quarantine"])


class TestQuarantineInFilters(QuarantineCase):
    def test_render_puts_exclusions_first_and_escaped(self):
        self.quarantine(path=f"/{ODD}", reason="r")
        text = self.render()["bisync_filter"].splitlines()
        self.assertEqual(text[0], "- /Books/Deti/Фото/16 В ремесленном училище \\[1\\].jpg")
        self.assertLess(text.index(text[0]), text.index("+ /Books/Deti/**"))
        self.assertEqual(text[-1], "- **")

    def test_survives_a_rewrite(self):
        # The point of keeping it in the policy: render twice, still there.
        self.quarantine(path=f"/{ODD}", reason="r")
        self.render()
        self.render()
        with open(os.path.join(self.tmp, "ya_disk.bisync.filters"), encoding="utf-8") as handle:
            self.assertIn("\\[1\\].jpg", handle.read())

    def test_download_filter_gets_it_too(self):
        self.quarantine(path="/pro/physon/a.pdf", reason="r")
        self.assertIn("- /pro/physon/a.pdf", self.render()["download_filter"])

    def test_filter_file_still_parses_to_the_same_folders(self):
        self.quarantine(path=f"/{ODD}", reason="r")
        self.render()
        parsed = load_sync_filters(os.path.join(self.tmp, "ya_disk.bisync.filters"))
        self.assertEqual(parsed.include_dirs, ["Books/Deti"])
        self.assertEqual(parsed.warnings, [])

    def test_include_with_glob_characters_round_trips(self):
        policy = sync_policy.load_policy(self.policy_path)
        policy["paths"]["Books/Stargardt_FB2_[rutor.info]"] = {"mode": "bidirectional"}
        sync_policy.write_policy(self.policy_path, policy)
        self.render()
        parsed = load_sync_filters(os.path.join(self.tmp, "ya_disk.bisync.filters"))
        self.assertIn("Books/Stargardt_FB2_[rutor.info]", parsed.include_dirs)

    def test_no_quarantine_renders_exactly_as_before(self):
        # The device's filter hash must not change just because the code did.
        self.assertEqual(self.render()["bisync_filter"],
                         "+ /Books/Deti/**\n+ /RCLONE_TEST\n- **\n")


class TestQuarantineCli(QuarantineCase):
    def test_cli_round_trip(self):
        out = io.StringIO()
        argv = ["sync_policy.py", "quarantine", "--path", f"/{ODD}", "--reason", "503",
                "--apply", "--policy-path", self.policy_path, "--local-root", self.local_root,
                "--format", "json", "--backend", "rclone"]
        with patch.object(sys, "argv", argv), patch("sys.stdout", out):
            sync_policy.main()
        payload = json.loads(out.getvalue())
        payload = payload.get("data") or payload
        self.assertIsNone(payload["error"])
        self.assertIn(ODD, sync_policy.load_policy(self.policy_path)["quarantine"])


if __name__ == "__main__":
    unittest.main()
