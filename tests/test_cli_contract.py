#!/usr/bin/env python3
"""One way to call the tools and one shape of answer.

`--format` went before the subcommand on some tools and after it on others, and
the wrong order was an argparse error. Every tool now takes both. The envelope is
checked from outside, on real invocations with nothing configured.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import gen_exclude_list  # noqa: E402
from ydm import place_format_flag  # noqa: E402


class TestPlaceFormatFlag(unittest.TestCase):
    def test_moved_to_the_front(self):
        self.assertEqual(place_format_flag(["report", "--format", "json", "status"], before_subcommand=True),
                         ["--format", "json", "report", "status"])

    def test_moved_behind_the_subcommand(self):
        self.assertEqual(place_format_flag(["--format", "json", "status", "--x"], before_subcommand=False),
                         ["status", "--format", "json", "--x"])

    def test_the_equals_form(self):
        self.assertEqual(place_format_flag(["--format=json", "status"], before_subcommand=False),
                         ["status", "--format=json"])

    def test_already_in_place_means_the_same(self):
        self.assertEqual(place_format_flag(["status", "--format", "json"], before_subcommand=False),
                         ["status", "--format", "json"])

    def test_no_flag_changes_nothing(self):
        self.assertEqual(place_format_flag(["status", "--apply"], before_subcommand=True), ["status", "--apply"])

    def test_a_dangling_flag_is_left_to_argparse(self):
        self.assertEqual(place_format_flag(["status", "--format"], before_subcommand=True), ["status", "--format"])

    def test_no_subcommand_is_left_to_argparse(self):
        self.assertEqual(place_format_flag(["--format", "json"], before_subcommand=False), ["--format", "json"])


class Calls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ydm_contract_")
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.var = os.path.join(self.tmp.name, "var")
        self.root = os.path.join(self.tmp.name, "mirror")
        for path in (self.home, self.var, self.root):
            os.makedirs(path)
        self.db = os.path.join(self.tmp.name, "monitor.db")
        subprocess.run([sys.executable, "ydm.py", "--db-path", self.db, "init"], cwd=ROOT,
                       capture_output=True, check=True, env=self.env())

    def env(self):
        env = dict(os.environ, HOME=self.home, YDM_VAR_DIR=self.var, YDM_LOCAL_ROOT=self.root)
        env.pop("YANDEX_DISK_TOKEN", None)
        return env

    def call(self, *argv):
        return subprocess.run([sys.executable, *argv], cwd=ROOT, env=self.env(),
                              capture_output=True, text=True, timeout=60)

    def envelope(self, *argv):
        proc = self.call(*argv)
        self.assertNotEqual(proc.returncode, 2, proc.stderr[-400:])
        data = json.loads(proc.stdout)
        self.assertIn("success", data, argv)
        return data, proc.returncode


class TestBothOrders(Calls):
    def test_each_tool_takes_both_orders(self):
        calls = [
            ("ydm.py", ("--db-path", self.db), ("report", "status")),
            ("tools/cloud_delta.py", (), ("check",)),
            ("tools/trash_scan.py", ("--db-path", self.db), ("summary", "--restore-root", "/x")),
        ]
        for script, lead, rest in calls:
            with self.subTest(script=script):
                before = self.envelope(script, *lead, "--format", "json", *rest)[1]
                after = self.envelope(script, *lead, *rest, "--format", "json")[1]
                self.assertEqual(before, after)
        for script, command in (("tools/sync_policy.py", "status"), ("tools/sync_bisync.py", "status"),
                                ("tools/sync_rename.py", "status"), ("tools/sync_exclude.py", "list"),
                                ("tools/sync_filters.py", "list")):
            with self.subTest(script=script):
                before = self.envelope(script, "--format", "json", command)[1]
                after = self.envelope(script, command, "--format", "json")[1]
                self.assertEqual(before, after)


class TestEnvelope(Calls):
    def test_tools_answer_success_and_data(self):
        for argv in (("tools/sync_policy.py", "status", "--format", "json"),
                     ("tools/sync_bisync.py", "status", "--format", "json"),
                     ("tools/sync_bisync.py", "doctor", "--format", "json"),
                     ("tools/sync_rename.py", "status", "--format", "json"),
                     ("tools/sync_exclude.py", "list", "--format", "json"),
                     ("tools/sync_filters.py", "list", "--format", "json"),
                     ("tools/gen_exclude_list.py", "--format", "json"),
                     ("tools/repair_android_names.py", "--path", "/x", "--db-path", self.db, "--format", "json")):
            with self.subTest(argv=argv):
                data, code = self.envelope(*argv)
                self.assertIn("data", data)
                self.assertEqual(data["success"], code == 0, data)

    def test_sync_tree_keeps_its_own_top_level_and_says_success(self):
        data, code = self.envelope("tools/sync_tree.py", "--db-path", self.db, "--no-local-scan",
                                   "--format", "json")
        self.assertEqual(data["success"], code == 0)


class TestCloudProbeJson(unittest.TestCase):
    def run_probe(self, status, root):
        from tools import cloud_probe
        with mock.patch.object(cloud_probe, "resolve_token", return_value="x"), \
                mock.patch.object(cloud_probe, "probe",
                                  return_value=cloud_probe.Probe(status, root, root)), \
                mock.patch("builtins.print") as printed:
            code = cloud_probe.main(["check", "--format", "json"])
        return code, json.loads(printed.call_args[0][0])

    def test_open(self):
        code, data = self.run_probe("open", 200)
        self.assertEqual(code, 0)
        self.assertTrue(data["success"])
        self.assertEqual(data["data"]["root"], 200)

    def test_closed_keeps_its_exit_code(self):
        code, data = self.run_probe("closed", 410)
        self.assertEqual(code, 10)
        self.assertFalse(data["success"])
        self.assertTrue(data["data"]["error"])


class TestGenExcludeList(unittest.TestCase):
    def test_help_needs_no_network_and_no_token(self):
        proc = subprocess.run([sys.executable, "tools/gen_exclude_list.py", "--help"], cwd=ROOT,
                              capture_output=True, text=True, timeout=30,
                              env={k: v for k, v in os.environ.items() if k != "YANDEX_DISK_TOKEN"})
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--keep", proc.stdout)

    def test_an_argument_is_never_a_token(self):
        with self.assertRaises(SystemExit) as caught, mock.patch("sys.stderr"):
            gen_exclude_list.main(["some-token-value"])
        self.assertEqual(caught.exception.code, 2)

    def test_no_token_is_a_failure_without_a_request(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(gen_exclude_list, "load_token_from_env", return_value=None), \
                mock.patch.object(gen_exclude_list, "root_folders") as request:
            os.environ.pop("YANDEX_DISK_TOKEN", None)
            self.assertIn("No token", gen_exclude_list.build(["pro"])["error"])
        request.assert_not_called()

    def test_the_line(self):
        with mock.patch.dict(os.environ, {"YANDEX_DISK_TOKEN": "t"}), \
                mock.patch.object(gen_exclude_list, "root_folders", return_value=["pro", "b", "a"]):
            payload = gen_exclude_list.build(["pro"])
        self.assertEqual(payload["exclude_dirs_line"], "exclude-dirs=a,b")


if __name__ == "__main__":
    unittest.main()
