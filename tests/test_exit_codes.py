#!/usr/bin/env python3
"""The exit code says how a command ended, and a JSON caller always gets JSON.

Until 2026-10-31 sync_policy, sync_bisync and the legacy editors exited 0 on any
failure, so a resync that died on files the cloud would not serve looked clean
to a script, and a failed `run` looked clean to systemd.

0 is done, 1 is failed, 3 is a guard refusing to run; 2 stays argparse's. Every
command here runs as a real process: the point is what a caller sees. The state
directory is redirected, and rclone is a stub.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sync_bisync, sync_common, sync_policy, sync_rename  # noqa: E402

EXIT_ERROR, EXIT_BLOCKED = sync_common.EXIT_ERROR, sync_common.EXIT_BLOCKED


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ydm_exit_")
        self.root = os.path.join(self.tmp, "mirror")
        os.makedirs(self.root)
        self.var = os.path.join(self.tmp, "var")
        os.makedirs(self.var)
        self.db = os.path.join(self.tmp, "monitor.db")
        self.policy = os.path.join(self.tmp, "policy.json")
        self.filter = os.path.join(self.tmp, "mirror.bisync.filters")
        with open(self.filter, "w", encoding="utf-8") as handle:
            handle.write("+ /kept/**\n+ /RCLONE_TEST\n- **\n")
        policy = sync_policy.empty_policy(self.root, "synthetic")
        policy["paths"] = {"kept": {"mode": "bidirectional"}}
        sync_policy.write_policy(self.policy, policy)
        self.rclone = self.stub_rclone(0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stub_rclone(self, code: int) -> str:
        path = os.path.join(self.tmp, f"rclone{code}")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\nif [ "$1" = version ]; then echo "rclone v1.75.2"; exit 0; fi\n'
                         f"exit {code}\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def run_tool(self, *argv, rclone=None):
        env = dict(os.environ, HOME=self.tmp, YDM_VAR_DIR=self.var, YDM_RCLONE=rclone or self.rclone)
        for name in ("YDM_TARGET", "YDM_TARGETS_FILE", "YDM_LOCAL_ROOT"):
            env.pop(name, None)
        return subprocess.run([sys.executable, *argv], cwd=ROOT, env=env, capture_output=True, text=True)

    def policy_tool(self, *argv):
        return self.run_tool("tools/sync_policy.py", *argv, "--policy-path", self.policy,
                             "--local-root", self.root, "--db-path", self.db,
                             "--backend", "rclone", "--format", "json")

    def bisync(self, *argv, rclone=None):
        return self.run_tool("tools/sync_bisync.py", *argv, "--local-root", self.root,
                             "--db-path", self.db, "--remote", "synthetic", "--policy-path", self.policy,
                             "--filter-path", self.filter, "--format", "json", rclone=rclone)

    def baseline(self):
        sync_common.save_bisync_state(
            {"last_resync_filter_hash": sync_common.filter_file_hash(self.filter)},
            os.path.join(self.var, "bisync_state.json"))


class TestSyncPolicy(CliCase):
    def test_success_and_dry_run_exit_zero(self):
        self.assertEqual(self.policy_tool("status").returncode, 0)
        self.assertEqual(self.policy_tool("quarantine").returncode, 0)

    def test_refusal_exits_one_and_still_prints_the_reason(self):
        proc = self.policy_tool("quarantine", "--path", "/elsewhere/x", "--reason", "r")
        self.assertEqual(proc.returncode, EXIT_ERROR)
        self.assertFalse(json.loads(proc.stdout)["success"])

    def test_a_policy_that_cannot_be_applied_is_a_failure(self):
        self.assertEqual(self.policy_tool("add", "--path", "/not/scanned").returncode, EXIT_ERROR)


class TestSyncBisync(CliCase):
    def test_no_baseline_is_blocked_not_failed(self):
        proc = self.bisync("run")
        self.assertEqual(proc.returncode, EXIT_BLOCKED, proc.stdout)
        self.assertTrue(json.loads(proc.stdout)["data"]["blocked"])

    def test_changed_filter_is_blocked(self):
        self.baseline()
        with open(self.filter, "a", encoding="utf-8") as handle:
            handle.write("# changed\n")
        self.assertEqual(self.bisync("run", "--apply").returncode, EXIT_BLOCKED)

    def test_dry_run_with_a_baseline_exits_zero(self):
        self.baseline()
        self.assertEqual(self.bisync("run").returncode, 0)

    def test_run_that_rclone_fails_exits_one(self):
        self.baseline()
        proc = self.bisync("run", "--apply", "--no-check-access", rclone=self.stub_rclone(7))
        self.assertEqual(proc.returncode, EXIT_ERROR, proc.stdout)
        self.assertNotIn("blocked", json.loads(proc.stdout)["data"])

    def test_resync_that_rclone_fails_exits_one(self):
        # The case that exposed all this: rc=7, `error` filled in, exit 0.
        proc = self.bisync("resync", "--apply", "--no-check-access", rclone=self.stub_rclone(7))
        self.assertEqual(proc.returncode, EXIT_ERROR, proc.stdout)

    def test_success_exits_zero_and_carries_no_blocked_key(self):
        self.baseline()
        proc = self.bisync("run", "--apply", "--no-check-access")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertNotIn("blocked", json.loads(proc.stdout)["data"])

    def test_lock_held_elsewhere_is_blocked(self):
        args = sync_bisync.build_parser().parse_args(
            ["run", "--apply", "--local-root", self.root, "--remote", "synthetic",
             "--policy-path", self.policy, "--filter-path", self.filter])
        with patch.dict(os.environ, {"YDM_VAR_DIR": self.var, "YDM_RCLONE": self.rclone}):
            self.baseline()
            held = SimpleNamespace(acquired=False, holder_pid=4242)
            with patch("tools.sync_bisync.acquire_lock", return_value=held):
                payload = sync_bisync.cmd_run(args)
        self.assertEqual(sync_common.exit_code(payload), EXIT_BLOCKED)


class TestRenamePreflight(CliCase):
    """job_run.sh takes a non-zero preflight to mean the guard is absent, and
    runs bisync without it. A decision must therefore always exit 0, or an
    error next to a block_bisync would turn the block into a run."""

    def main_with(self, payload):
        with patch.object(sys, "argv", ["sync_rename.py", "preflight", "--format", "json",
                                        "--local-root", self.root, "--db-path", self.db]), \
                patch("tools.sync_rename.configure_target_args"), \
                patch("tools.sync_rename.cmd_preflight", return_value=payload), \
                patch("sys.stdout"):
            return sync_rename.main()

    def test_a_block_with_an_error_still_exits_zero(self):
        self.assertEqual(self.main_with({"action": "preflight", "decision": "block_bisync",
                                         "error": "partial"}), 0)

    def test_no_decision_is_a_failure(self):
        self.assertEqual(self.main_with({"action": "preflight", "error": "broken"}), EXIT_ERROR)

    def test_the_job_still_reads_the_envelope_for_its_decision(self):
        script = (ROOT / "tools" / "termux" / "job_run.sh").read_text(encoding="utf-8")
        self.assertIn('if [ "$preflight_rc" -ne 0 ]; then\n    decision="allow_bisync"', script)


class TestLegacyEditors(CliCase):
    def test_sync_filters_refusal(self):
        proc = self.run_tool("tools/sync_filters.py", "remove", "--path", "/x", "--format", "json",
                             "--db-path", self.db, "--local-root", self.root, "--filter-path", self.filter)
        self.assertFalse(json.loads(proc.stdout)["success"])
        self.assertEqual(proc.returncode, EXIT_ERROR)

    def test_sync_filters_list(self):
        proc = self.run_tool("tools/sync_filters.py", "list", "--format", "json", "--db-path", self.db,
                             "--local-root", self.root, "--filter-path", self.filter)
        self.assertEqual(proc.returncode, 0, proc.stdout)


    def test_sync_exclude_refusal(self):
        proc = self.run_tool("tools/sync_exclude.py", "remove", "--path", "/", "--format", "json",
                             "--db-path", self.db, "--local-root", self.root)
        self.assertFalse(json.loads(proc.stdout)["success"], proc.stderr[-300:])
        self.assertEqual(proc.returncode, EXIT_ERROR)

    def test_sync_rename_failure(self):
        proc = self.run_tool("tools/sync_rename.py", "plan", "--old", "/nowhere/a", "--new", "/nowhere/b",
                             "--format", "json", "--db-path", self.db, "--local-root", self.root,
                             "--bisync-filter-path", self.filter)
        self.assertFalse(json.loads(proc.stdout)["success"], proc.stderr[-300:])
        self.assertEqual(proc.returncode, EXIT_ERROR)


class TestJsonOnAnEmptyDatabase(CliCase):
    """`--format json` is a promise to the caller: a crash answers in JSON too,
    with the traceback on stderr where a person can still find it."""

    def assert_json_failure(self, proc):
        self.assertEqual(proc.returncode, EXIT_ERROR, proc.stderr[-300:])
        self.assertFalse(json.loads(proc.stdout)["success"])
        self.assertNotIn("Traceback", proc.stdout)

    def empty_db(self):
        open(self.db, "w").close()
        return self.db

    def test_ydm(self):
        self.assert_json_failure(self.run_tool("ydm.py", "--db-path", self.empty_db(),
                                               "--format", "json", "report", "status"))

    def test_trash_scan(self):
        self.assert_json_failure(self.run_tool("tools/trash_scan.py", "--db-path", self.empty_db(),
                                               "--format", "json", "summary", "--restore-root", "/x"))

    def test_sync_tree(self):
        self.assert_json_failure(self.run_tool("tools/sync_tree.py", "--db-path", self.empty_db(),
                                               "--local-root", self.root, "--format", "json"))

    def test_cloud_delta(self):
        self.empty_db()
        proc = self.run_tool("tools/cloud_delta.py", "--format", "json", "changes", "--db-path", self.db)
        self.assertEqual(proc.returncode, EXIT_ERROR, proc.stderr[-300:])
        self.assertIn("no such table", proc.stdout)

    def test_text_format_keeps_the_traceback(self):
        proc = self.run_tool("ydm.py", "--db-path", self.empty_db(), "report", "status")
        self.assertEqual(proc.returncode, EXIT_ERROR)
        self.assertIn("Traceback", proc.stderr)


class TestEveryTool(unittest.TestCase):
    def test_a_tool_with_the_envelope_exits_with_main(self):
        # A new CLI that copies render() but not this line brings the bug back.
        for path in sorted((ROOT / "tools").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            if '__name__ == "__main__"' not in source or not re.search(r'"success":', source):
                continue
            with self.subTest(tool=path.name):
                self.assertIn("sys.exit(main(", source)


if __name__ == "__main__":
    unittest.main()
