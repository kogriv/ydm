#!/usr/bin/env python3
"""A file the cloud will not hand over, seen before it stops the sync.

Scans read metadata, and a file Yandex answers 500 for has perfectly good
metadata: `add` said `safe_for_bidirectional: true`, the filter changed, and the
next resync died on it, with every other folder waiting behind that resync.

rclone here is a stub that reads a directory standing in for the cloud and fails
on the names it is told to fail on.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tools import sync_bisync, sync_common, sync_policy  # noqa: E402

STUB = """#!/usr/bin/env python3
import os, sys, time
args = sys.argv[1:]
cloud = os.environ["FAKE_CLOUD"]
path = args[-1].split(":", 1)[1]
behaviour = {}
for line in open(os.environ["FAKE_RULES"], encoding="utf-8"):
    name, _, what = line.rstrip("\\n").partition("=")
    behaviour[name] = what
what = behaviour.get(path, "")
if what == "500":
    sys.stderr.write("2026/01/31 12:00:00 ERROR : \\x1b[31mFailed to open: [500 - ]  (500 Internal Server Error)\\x1b[0m\\n")
    sys.exit(1)
if what == "hang":
    time.sleep(30)
data = open(os.path.join(cloud, path), "rb").read()
if what == "short":
    data = data[:1]
sys.stdout.buffer.write(data[:int(args[args.index("--count") + 1])])
"""

REAL_500 = (
    "2026/10/29 12:00:00 INFO  : Synching Path1\n"
    "2026/10/29 12:00:01 \x1b[31mERROR\x1b[0m : Folder/sub dir/файл [1].ext: Failed to copy: "
    "failed to open source object: [500 - ]  (500 Internal Server Error)\n"
    "2026/10/29 12:00:01 ERROR : Folder/other.ext: Failed to copy: failed to open source object: "
    "[503 - ]  (503 Service Unavailable)\n"
    "2026/10/29 12:00:01 ERROR : Bisync critical error: failed to open source object: [500 - ]\n"
    "2026/10/29 12:00:01 ERROR : \x1b[31mBisync aborted. Must run --resync to recover.\x1b[0m\n"
)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ydm_preflight_")
        self.cloud = os.path.join(self.tmp, "cloud")
        self.root = os.path.join(self.tmp, "mirror")
        self.rules = os.path.join(self.tmp, "rules")
        os.makedirs(self.cloud)
        os.makedirs(self.root)
        stub = os.path.join(self.tmp, "rclone")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(STUB)
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
        self.rule()
        env = {"YDM_RCLONE": stub, "FAKE_CLOUD": self.cloud, "FAKE_RULES": self.rules}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def rule(self, **_):
        pass

    def write_rules(self, rules):
        with open(self.rules, "w", encoding="utf-8") as handle:
            for name, what in rules.items():
                handle.write(f"{name}={what}\n")

    def put(self, rel, data=b"x" * 2000):
        path = os.path.join(self.cloud, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return rel


class TestProbe(Case):
    def setUp(self):
        super().setUp()
        self.write_rules({})

    def test_a_served_file_is_readable(self):
        self.put("Folder/a.ext")
        self.assertTrue(sync_common.probe_file("yandex", "Folder/a.ext", size=2000)["readable"])

    def test_an_empty_file_is_readable(self):
        self.put("Folder/empty", b"")
        self.assertTrue(sync_common.probe_file("yandex", "Folder/empty", size=0)["readable"])

    def test_a_500_is_not_and_says_why_without_colour(self):
        self.put("Folder/a.ext")
        self.write_rules({"Folder/a.ext": "500"})
        row = sync_common.probe_file("yandex", "Folder/a.ext", size=2000)
        self.assertFalse(row["readable"])
        self.assertIn("500 Internal Server Error", row["reason"])
        self.assertNotIn("\x1b", row["reason"])

    def test_a_timeout_is_not_a_pass(self):
        self.put("Folder/a.ext")
        self.write_rules({"Folder/a.ext": "hang"})
        row = sync_common.probe_file("yandex", "Folder/a.ext", size=2000, timeout=0.3)
        self.assertFalse(row["readable"])
        self.assertIn("no answer", row["reason"])

    def test_a_short_read_is_not_a_pass(self):
        self.put("Folder/a.ext")
        self.write_rules({"Folder/a.ext": "short"})
        row = sync_common.probe_file("yandex", "Folder/a.ext", size=2000)
        self.assertFalse(row["readable"])
        self.assertIn("short read", row["reason"])

    def test_only_the_first_bytes_are_asked_for(self):
        with patch("tools.sync_common.subprocess.run") as run:
            run.return_value = argparse.Namespace(returncode=0, stdout=b"x" * 1024, stderr=b"")
            sync_common.probe_file("yandex", "Folder/a.ext", size=9_000_000)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[1:3], ["cat", "--count"])
        self.assertEqual(cmd[3], "1024")

    def test_order_survives_the_pool(self):
        for name in "abcdef":
            self.put(f"F/{name}")
        rows = sync_common.probe_files("yandex", [(f"F/{n}", 2000) for n in "abcdef"], workers=3)
        self.assertEqual([row["path"] for row in rows], [f"F/{n}" for n in "abcdef"])


class TestFailedPaths(unittest.TestCase):
    def test_real_shape(self):
        rows = sync_common.rclone_failed_paths(REAL_500)
        self.assertEqual([row["path"] for row in rows],
                         ["Folder/sub dir/файл [1].ext", "Folder/other.ext"])
        self.assertIn("500 Internal Server Error", rows[0]["reason"])

    def test_unknown_format_gives_an_empty_list(self):
        self.assertEqual(sync_common.rclone_failed_paths("something else entirely\n"), [])
        self.assertEqual(sync_common.rclone_failed_paths(None), [])

    def test_one_file_failing_twice_is_one_entry(self):
        line = "2026/01/31 12:00:00 ERROR : A/b: Failed to copy: boom\n"
        self.assertEqual(len(sync_common.rclone_failed_paths(line * 3)), 1)


class TestNextAction(unittest.TestCase):
    def args(self, **kw):
        return argparse.Namespace(remote="yandex", check_access=kw.get("check_access", True))

    def action(self, stderr, **kw):
        clean = sync_common.strip_ansi(stderr)
        return sync_bisync.next_action(clean, sync_common.rclone_failed_paths(clean), self.args(**kw))

    def test_quarantine_with_a_ready_command(self):
        action = self.action(REAL_500)
        self.assertEqual(action["kind"], "quarantine")
        self.assertIn("'/Folder/sub dir/файл [1].ext'", action["commands"][0])
        self.assertIn("500 on download since", action["commands"][0])
        self.assertIn("503 on download since", action["commands"][1])
        self.assertIn("render-filters", action["commands"][-2])
        self.assertEqual(action["commands"][-1], "sync_bisync.py resync --apply")

    def test_a_resync_without_the_check_stays_without_it(self):
        self.assertIn("--no-check-access", self.action(REAL_500, check_access=False)["commands"][-1])

    def test_a_cloud_that_never_saw_bisync(self):
        action = self.action("ERROR : Access test failed: Path1 count 1, Path2 count 0 - RCLONE_TEST\n")
        self.assertEqual(action["kind"], "missing_sentinel")
        self.assertIn("--no-check-access", action["commands"][-1])

    def test_a_lost_local_sentinel_is_not_that_case(self):
        action = self.action("ERROR : Access test failed: Path1 count 0, Path2 count 1 - RCLONE_TEST\n")
        self.assertEqual(action["kind"], "read_stderr")

    def test_too_many_deletes_offers_nothing_to_run(self):
        action = self.action("ERROR : Safety abort: too many deletes (>20%, 3 of 12) on Path1\n")
        self.assertEqual(action["kind"], "stop")
        self.assertEqual(action["commands"], [])

    def test_an_unknown_failure_points_at_stderr(self):
        self.assertEqual(self.action("no idea\n")["kind"], "read_stderr")


class FakeFile:
    def __init__(self, parent, name, size=2000, native=False):
        self.parent_path, self.name, self.size = parent, name, size
        self.is_native, self.is_shortcut = native, False
        self.object_id, self.mime_type = None, None
        self.chars = []

    @property
    def rel_path(self):
        return f"{self.parent_path.strip('/')}/{self.name}"

    @property
    def sanitized_rel_path(self):
        return self.rel_path


class TestPolicyHelpers(unittest.TestCase):
    def test_quarantined_by(self):
        self.assertEqual(sync_policy.quarantined_by("A/b", {"A/b": {}}), "A/b")
        self.assertEqual(sync_policy.quarantined_by("A/b/c", {"A/b": {}}), "A/b")
        self.assertIsNone(sync_policy.quarantined_by("A/bc", {"A/b": {}}))
        self.assertIsNone(sync_policy.quarantined_by("A/b", None))

    def test_ignored_by_matches_what_the_rules_mean(self):
        ignore = {"A": ["build/", "*.log"]}
        self.assertEqual(sync_policy.ignored_by("A/build/x.o", ignore), "build/")
        self.assertEqual(sync_policy.ignored_by("A/s/build/t/x.o", ignore), "build/")
        self.assertEqual(sync_policy.ignored_by("A/x.log", ignore), "*.log")
        self.assertEqual(sync_policy.ignored_by("A/s/x.log", ignore), "*.log")
        self.assertIsNone(sync_policy.ignored_by("A/build", ignore), "a file called build is not a directory")
        self.assertIsNone(sync_policy.ignored_by("B/x.log", ignore))


class AddCase(Case):
    def setUp(self):
        super().setUp()
        self.write_rules({})
        self.policy_path = os.path.join(self.tmp, "policy.json")
        self.db = os.path.join(self.tmp, "monitor.db")
        self.files = []
        patches = [
            patch.object(sync_policy, "inspect_path", return_value={
                "risks": [], "recommended_mode": "bidirectional", "safe_for_bidirectional": True,
                "scan_id": 1}),
            patch.object(sync_policy, "cloud_files_for_path", side_effect=lambda *_: (1, True, self.files)),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def seed(self, *names, **kw):
        for name in names:
            self.put(f"Folder/{name}")
            self.files.append(FakeFile("/Folder", name, **kw))

    def ns(self, **kw):
        base = dict(path="/Folder", mode="bidirectional", apply=False, force_risk=False, db_path=self.db,
                    policy_path=self.policy_path, local_root=self.root, remote="yandex", backend="rclone",
                    preflight=True, quarantine_unreadable=False, probe_workers=2, probe_timeout=5)
        base.update(kw)
        return argparse.Namespace(**base)

    def policy(self):
        return sync_policy.load_policy(self.policy_path)


class TestAddPreflight(AddCase):
    def test_without_the_flag_nothing_is_probed(self):
        self.seed("a")
        result = sync_policy.add_policy_path(self.ns(preflight=False))
        self.assertNotIn("content_probe", result)

    def test_dry_run_reports_and_writes_nothing(self):
        self.seed("a", "b")
        self.write_rules({"Folder/b": "500"})
        result = sync_policy.add_policy_path(self.ns())
        self.assertIsNone(result["error"])
        self.assertEqual(result["content_probe"]["checked"], 2)
        self.assertEqual([row["path"] for row in result["content_probe"]["unreadable"]], ["/Folder/b"])
        self.assertTrue(result["risk"]["safe_for_bidirectional"], "availability is a separate field")
        self.assertIsNone(self.policy())

    def test_apply_refuses_before_the_policy_changes(self):
        self.seed("a", "b")
        self.write_rules({"Folder/b": "500"})
        result = sync_policy.add_policy_path(self.ns(apply=True))
        self.assertTrue(result["blocked"])
        self.assertIn("--quarantine-unreadable", result["error"])
        self.assertEqual(sync_common.exit_code(result), sync_common.EXIT_BLOCKED)
        self.assertIsNone(self.policy())

    def test_apply_goes_ahead_when_all_is_readable(self):
        self.seed("a")
        result = sync_policy.add_policy_path(self.ns(apply=True))
        self.assertIsNone(result["error"])
        self.assertIn("Folder", self.policy()["paths"])

    def test_quarantine_unreadable_writes_both_at_once(self):
        self.seed("a", "b")
        self.write_rules({"Folder/b": "500"})
        result = sync_policy.add_policy_path(self.ns(apply=True, preflight=False, quarantine_unreadable=True))
        self.assertIsNone(result["error"])
        policy = self.policy()
        self.assertIn("Folder", policy["paths"])
        self.assertIn("500", policy["quarantine"]["Folder/b"]["reason"])
        self.assertEqual(result["quarantined"], ["/Folder/b"])
        rendered = "\n".join(sync_policy.filter_lines(["Folder"], exclude=policy["quarantine"]))
        self.assertIn("- /Folder/b", rendered)

    def test_quarantine_unreadable_dry_run_writes_nothing(self):
        self.seed("a")
        self.write_rules({"Folder/a": "500"})
        sync_policy.add_policy_path(self.ns(quarantine_unreadable=True))
        self.assertIsNone(self.policy())

    def test_files_a_sync_would_not_fetch_are_not_probed(self):
        self.seed("a")
        self.files.append(FakeFile("/Folder", "doc", native=True))
        result = sync_policy.add_policy_path(self.ns())
        self.assertEqual(result["content_probe"]["checked"], 1)


class TestDownloadPlan(AddCase):
    def plan_args(self, **kw):
        base = dict(remote="yandex", local_root=self.root, download_filter_path=None, db_path=self.db,
                    apply=True, timeout_sec=5)
        base.update(kw)
        return argparse.Namespace(**base)

    def policy_with(self, **sections):
        policy = sync_policy.empty_policy(self.root, "yandex")
        policy["paths"] = {"Folder": {"mode": "download_only"}}
        policy.update(sections)
        return policy

    def plan(self, policy, **kw):
        open(self.db, "w").close()
        return sync_policy.download_plan(self.plan_args(**kw), policy)

    def test_quarantine_and_ignore_stay_out_of_the_manifest(self):
        self.seed("a", "bad", "x.log")
        plan = self.plan(self.policy_with(quarantine={"Folder/bad": {"reason": "r"}},
                                          ignore={"Folder": ["*.log"]}))
        self.assertEqual([row["path"] for row in plan["eligible"]], ["Folder/a"])
        reasons = {row["path"]: row["reason"] for row in plan["excluded"]}
        self.assertEqual(reasons, {"Folder/bad": "quarantined", "Folder/x.log": "ignored"})
        self.assertIsNone(plan["error"])

    def test_manifest_goes_to_var_when_the_parent_is_not_ours(self):
        self.seed("a")
        var = os.path.join(self.tmp, "var")
        with patch.dict(os.environ, {"YDM_VAR_DIR": var}), patch("tools.sync_policy.os.access", return_value=False):
            plan = self.plan(self.policy_with())
        self.assertTrue(plan["manifest_path"].startswith(var))

    def test_manifest_stays_beside_the_filter_when_it_can(self):
        self.seed("a")
        self.assertTrue(self.plan(self.policy_with())["manifest_path"].startswith(self.tmp + os.sep + "mirror"))

    def apply_with(self, run):
        self.seed("a", "b")
        open(self.db, "w").close()
        with patch("tools.sync_policy.subprocess.run", side_effect=run):
            return sync_policy.download_apply(self.plan_args(), self.policy_with())

    def test_partial_failure_names_the_files(self):
        def run(cmd, **_):
            return argparse.Namespace(returncode=1, stdout="", stderr=REAL_500)
        result = self.apply_with(run)
        self.assertIn("rclone copy failed", result["error"])
        self.assertEqual(result["failed_paths"][1]["path"], "Folder/other.ext")

    def test_a_stuck_rclone_does_not_hold_the_command(self):
        import subprocess

        def run(cmd, **kwargs):
            self.assertEqual(kwargs["timeout"], 5)
            raise subprocess.TimeoutExpired(cmd, 5, stderr=REAL_500.encode())
        result = self.apply_with(run)
        self.assertIn("no result after 5s", result["error"])
        self.assertEqual(len(result["failed_paths"]), 2)

    def test_success_adds_no_failure_fields(self):
        result = self.apply_with(lambda cmd, **_: argparse.Namespace(returncode=0, stdout="", stderr=""))
        self.assertIsNone(result["error"])
        self.assertNotIn("failed_paths", result)


class TestNextSteps(unittest.TestCase):
    def ns(self, backend="rclone"):
        return argparse.Namespace(backend=backend, target_managed=False)

    def test_bidirectional_needs_a_resync(self):
        steps = sync_policy.next_after_policy_change(self.ns(), {"action": "add", "mode": "bidirectional"})
        self.assertIn("resync --apply", steps[0])

    def test_download_only_goes_through_the_plan(self):
        steps = sync_policy.next_after_policy_change(self.ns(), {"action": "add", "mode": "download_only"})
        self.assertIn("download-apply", steps[1])

    def test_remove_needs_a_resync(self):
        self.assertTrue(sync_policy.next_after_policy_change(self.ns(), {"action": "remove"}))

    def test_nothing_after_a_failure_or_on_the_daemon(self):
        self.assertEqual(sync_policy.next_after_policy_change(self.ns(), {"action": "add", "error": "x"}), [])
        self.assertEqual(sync_policy.next_after_policy_change(self.ns("daemon"), {"action": "add"}), [])


class TestTimeout(unittest.TestCase):
    def test_run_command_reports_a_stop(self):
        result = sync_common.run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)
        self.assertEqual(result.returncode, sync_common.TIMEOUT_RETURNCODE)
        self.assertIn("no result after", result.stderr)


if __name__ == "__main__":
    unittest.main()
