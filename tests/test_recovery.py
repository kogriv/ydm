#!/usr/bin/env python3
"""Pausing the sync, taking files back out of quarantine, and asking how it is.

Each of these used to be something an agent did around YDM: `systemctl stop`, a
home-made loop of `rclone cat`, a dozen status commands. rclone here is a small
stub over a directory, so the md5 checks are real and no network is involved.
"""

from __future__ import annotations

import argparse
import hashlib
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

from tools import sync_bisync, sync_common, sync_doctor, sync_policy  # noqa: E402

STUB = """#!/usr/bin/env python3
import hashlib, json, os, shutil, sys
args = sys.argv[1:]
cloud = os.environ["FAKE_CLOUD"]
broken = set(open(os.environ["FAKE_BROKEN"]).read().split()) if os.path.exists(os.environ["FAKE_BROKEN"]) else set()
def where(spec):
    return os.path.join(cloud, spec.split(":", 1)[1])
if args[0] == "version":
    print("rclone v1.75.2"); sys.exit(0)
if args[0] == "lsjson":
    path = where(args[-1])
    if not os.path.exists(path):
        sys.stderr.write("ERROR : object not found\\n"); sys.exit(3)
    if os.path.isdir(path):
        print(json.dumps({"IsDir": True})); sys.exit(0)
    data = open(path, "rb").read()
    print(json.dumps({"IsDir": False, "Size": len(data), "ModTime": "2024-03-01T10:00:00.123456789Z",
                      "Hashes": {"md5": os.environ.get("FAKE_MD5") or hashlib.md5(data).hexdigest()}}))
    sys.exit(0)
if args[0] == "lsf":
    print("\\n".join(os.listdir(cloud))); sys.exit(0)
name = args[-2 if args[0] == "copyto" else -1].split(":", 1)[1]
if name in broken:
    sys.stderr.write("NOTICE: Failed: [500 - ]  (500 Internal Server Error)\\n"); sys.exit(1)
if args[0] == "cat":
    sys.stdout.buffer.write(open(where(args[-1]), "rb").read()[:1024]); sys.exit(0)
if args[0] == "copyto":
    shutil.copyfile(where(args[-2]), args[-1]); sys.exit(0)
sys.exit(9)
"""


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ydm_recovery_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cloud = os.path.join(self.tmp, "cloud")
        self.root = os.path.join(self.tmp, "mirror")
        self.var = os.path.join(self.tmp, "var")
        for path in (self.cloud, self.root, self.var):
            os.makedirs(path)
        self.broken = os.path.join(self.tmp, "broken")
        stub = os.path.join(self.tmp, "rclone")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(STUB)
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
        env = {"YDM_RCLONE": stub, "FAKE_CLOUD": self.cloud, "FAKE_BROKEN": self.broken,
               "YDM_VAR_DIR": self.var}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in sync_common.PROXY_VARIABLES:  # whatever this machine's shell exports
            os.environ.pop(name, None)
        self.addCleanup(sync_common._RCLONE_VERSIONS.clear)
        lock = patch.object(sync_bisync, "LOCK_PATH", os.path.join(self.var, "bisync.lock"))
        lock.start()
        self.addCleanup(lock.stop)
        self.policy_path = os.path.join(self.tmp, "policy.json")
        policy = sync_policy.empty_policy(self.root, "yandex")
        policy["paths"] = {"Folder": {"mode": "bidirectional"}}
        sync_policy.write_policy(self.policy_path, policy)

    def cloud_file(self, name, data=b"payload"):
        path = os.path.join(self.cloud, "Folder", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    def break_file(self, *names):
        with open(self.broken, "w") as handle:
            handle.write("\n".join(f"Folder/{name}" for name in names))

    def quarantine(self, *names):
        policy = sync_policy.load_policy(self.policy_path)
        policy["quarantine"] = {f"Folder/{name}": {"reason": "500", "since": "2026-01-01T00:00:00"}
                                for name in names}
        sync_policy.write_policy(self.policy_path, policy)

    def policy(self):
        return sync_policy.load_policy(self.policy_path)

    def local(self, name):
        return os.path.join(self.root, "Folder", name)

    def ns(self, **kw):
        base = dict(policy_path=self.policy_path, path=None, reason=None, remove=False, recheck=True,
                    apply=False, local_root=self.root, remote="yandex", timeout_sec=30)
        base.update(kw)
        return argparse.Namespace(**base)

    def recheck(self, **kw):
        return sync_policy.quarantine_policy_path(self.ns(**kw))


class TestRecheck(Case):
    def setUp(self):
        super().setUp()
        self.cloud_file("back")
        self.cloud_file("gone")
        self.break_file("gone")
        self.quarantine("back", "gone")

    def test_dry_run_only_reports_and_says_how_far_it_looked(self):
        result = self.recheck()
        self.assertEqual(result["readable"], ["/Folder/back"])
        self.assertEqual(result["still_unreadable"], ["/Folder/gone"])
        self.assertEqual(result["recheck"][0]["verified"], "first_bytes")
        self.assertEqual(len(self.policy()["quarantine"]), 2)
        self.assertFalse(os.path.exists(self.local("back")))

    def test_apply_needs_the_whole_file_to_match(self):
        result = self.recheck(apply=True)
        self.assertEqual(result["readable"], ["/Folder/back"])
        self.assertEqual(list(self.policy()["quarantine"]), ["Folder/gone"])
        self.assertEqual(result["recheck"][0]["verified"], "md5")
        self.assertEqual(result["recheck"][0]["local"], "placed")
        self.assertEqual(Path(self.local("back")).read_bytes(), b"payload")
        self.assertIn("render-filters", result["next"][0])

    def test_a_download_that_does_not_match_stays_in(self):
        with patch.dict(os.environ, {"FAKE_MD5": "0" * 32}):
            result = self.recheck(apply=True)
        self.assertEqual(result["readable"], [])
        self.assertIn("md5 mismatch", result["recheck"][0]["reason"])
        self.assertEqual(len(self.policy()["quarantine"]), 2)
        self.assertFalse(os.path.exists(self.local("back")))
        self.assertEqual(os.listdir(self.var), [], "nothing half-fetched left behind")

    def test_a_local_copy_that_differs_is_a_persons_call(self):
        os.makedirs(os.path.dirname(self.local("back")))
        Path(self.local("back")).write_bytes(b"other")
        result = self.recheck(apply=True, path="/Folder/back")
        self.assertEqual(result["recheck"][0]["local"], "differs")
        self.assertIn("/Folder/back", result["still_unreadable"])
        self.assertIn("Folder/back", self.policy()["quarantine"])
        self.assertEqual(Path(self.local("back")).read_bytes(), b"other")

    def test_the_same_local_copy_is_left_and_the_entry_comes_out(self):
        os.makedirs(os.path.dirname(self.local("back")))
        Path(self.local("back")).write_bytes(b"payload")
        result = self.recheck(apply=True, path="/Folder/back")
        self.assertEqual(result["recheck"][0]["local"], "same")
        self.assertNotIn("Folder/back", self.policy()["quarantine"])

    def test_nothing_readable_changes_nothing(self):
        self.break_file("back", "gone")
        before = Path(self.policy_path).read_text()
        result = self.recheck(apply=True)
        self.assertEqual(result["readable"], [])
        self.assertNotIn("next", result)
        self.assertEqual(Path(self.policy_path).read_text(), before)

    def test_a_folder_entry_is_not_a_file(self):
        self.quarantine("sub")
        os.makedirs(os.path.join(self.cloud, "Folder", "sub"))
        result = self.recheck(apply=True)
        self.assertIn("folder", result["recheck"][0]["reason"])

    def test_one_path_must_be_in_quarantine(self):
        self.assertIn("not in quarantine", self.recheck(path="/Folder/other")["error"])

    def test_the_cli(self):
        with patch.object(sys, "argv", ["sync_policy.py", "quarantine", "--recheck", "--format", "json",
                                        "--policy-path", self.policy_path, "--local-root", self.root,
                                        "--backend", "rclone"]):
            with patch("builtins.print"):
                self.assertEqual(sync_policy.main(), 0)


class TestImport(Case):
    def setUp(self):
        super().setUp()
        self.cloud_file("wanted", b"the real bytes")
        self.source = os.path.join(self.tmp, "from_device")
        Path(self.source).write_bytes(b"the real bytes")

    def run_import(self, **kw):
        base = dict(path="/Folder/wanted", source=self.source, apply=True, local_root=self.root,
                    remote="yandex", policy_path=self.policy_path)
        base.update(kw)
        return sync_policy.import_file(argparse.Namespace(**base), self.policy())

    def test_dry_run_checks_and_copies_nothing(self):
        result = self.run_import(apply=False)
        self.assertIsNone(result["error"])
        self.assertTrue(result["matches_cloud"])
        self.assertFalse(os.path.exists(self.local("wanted")))

    def test_a_match_lands_with_the_clouds_date(self):
        result = self.run_import()
        self.assertTrue(result["imported"])
        self.assertEqual(Path(self.local("wanted")).read_bytes(), b"the real bytes")
        self.assertEqual(int(os.path.getmtime(self.local("wanted"))) % 60, 0)
        self.assertTrue(os.path.exists(self.source), "the source stays")

    def test_a_different_file_is_refused(self):
        Path(self.source).write_bytes(b"something else")
        result = self.run_import()
        self.assertIn("not the cloud's file", result["error"])
        self.assertFalse(os.path.exists(self.local("wanted")))

    def test_nothing_is_overwritten(self):
        os.makedirs(os.path.dirname(self.local("wanted")))
        Path(self.local("wanted")).write_bytes(b"local changes")
        self.assertIn("already at", self.run_import()["error"])
        self.assertEqual(Path(self.local("wanted")).read_bytes(), b"local changes")

    def test_the_same_file_is_fine(self):
        os.makedirs(os.path.dirname(self.local("wanted")))
        Path(self.local("wanted")).write_bytes(b"the real bytes")
        self.assertTrue(self.run_import()["already_there"])

    def test_outside_the_synced_paths_is_refused(self):
        self.assertIn("not inside", self.run_import(path="/Other/x")["error"])

    def test_a_quarantined_file_gets_the_steps_to_come_out(self):
        self.quarantine("wanted")
        result = self.run_import()
        self.assertIn("--remove", result["next"][0])

    def test_a_running_sync_blocks_it(self):
        with open(sync_bisync.LOCK_PATH, "w") as handle:
            handle.write(str(os.getpid()))
        result = self.run_import()
        self.assertTrue(result["blocked"])
        self.assertFalse(os.path.exists(self.local("wanted")))


class BisyncCase(Case):
    def setUp(self):
        super().setUp()
        self.filter_path = self.root + ".bisync.filters"
        Path(self.filter_path).write_text(sync_policy.expected_bisync_filter(self.policy()))
        self.state = os.path.join(self.var, "bisync_state.json")
        patcher = patch("tools.sync_common.bisync_state_path", return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        sync_common.save_bisync_state({
            "last_resync_filter_hash": sync_common.filter_file_hash(self.filter_path),
            "last_resync_at": "2026-01-01T00:00:00", "last_run_at": sync_common.datetime.now().isoformat(),
            "last_status": "ok"}, self.state)

    def ns(self, command="run", **kw):
        args = sync_bisync.build_parser().parse_args([command] + (["--reason", "x"] if command == "pause" else []))
        args.local_root, args.policy_path, args.filter_path = self.root, self.policy_path, self.filter_path
        args.db_path = os.path.join(self.tmp, "monitor.db")
        for key, value in kw.items():
            setattr(args, key, value)
        return args


class TestPause(BisyncCase):
    def test_dry_run_does_not_pause(self):
        sync_bisync.cmd_pause(self.ns("pause"))
        self.assertIsNone(sync_bisync.cmd_status(self.ns("status"))["paused"])

    def test_a_paused_run_is_blocked_without_calling_rclone(self):
        sync_bisync.cmd_pause(self.ns("pause", reason="moving disks", apply=True))
        with patch("tools.sync_bisync.rclone_bisync_run") as run:
            payload = sync_bisync.cmd_run(self.ns(apply=True))
        run.assert_not_called()
        self.assertIn("moving disks", payload["error"])
        self.assertEqual(sync_common.exit_code(payload), sync_common.EXIT_BLOCKED)

    def test_a_pause_tells_nobody_off(self):
        sync_bisync.cmd_pause(self.ns("pause", apply=True))
        with patch("tools.sync_bisync.notify_once") as notify:
            sync_bisync.cmd_run(self.ns(apply=True))
        notify.assert_not_called()

    def test_status_shows_it_and_resume_ends_it(self):
        sync_bisync.cmd_pause(self.ns("pause", reason="r", apply=True))
        self.assertEqual(sync_bisync.cmd_status(self.ns("status"))["paused"]["reason"], "r")
        self.assertEqual(sync_bisync.cmd_resume(self.ns("resume"))["was_paused"]["reason"], "r")
        self.assertTrue(sync_bisync.load_pause(sync_bisync._pause_path(self.ns("status"))), "dry run")
        sync_bisync.cmd_resume(self.ns("resume", apply=True))
        self.assertIsNone(sync_bisync.cmd_status(self.ns("status"))["paused"])

    def test_a_pause_survives_a_state_rewrite(self):
        sync_bisync.cmd_pause(self.ns("pause", apply=True))
        state = sync_common.load_bisync_state(self.state)
        state["last_status"] = "ok"
        sync_common.save_bisync_state(state, self.state)
        self.assertTrue(sync_bisync.cmd_status(self.ns("status"))["paused"])

    def test_resync_still_works_during_a_pause(self):
        sync_bisync.cmd_pause(self.ns("pause", apply=True))
        payload = sync_bisync.cmd_resync(self.ns("resync"))
        self.assertIsNone(payload["error"])

    def test_a_run_in_progress_is_mentioned(self):
        with open(sync_bisync.LOCK_PATH, "w") as handle:
            handle.write(str(os.getpid()))
        self.assertTrue(sync_bisync.cmd_pause(self.ns("pause"))["warnings"])


class TestEnvironment(BisyncCase):
    def test_a_proxy_shows_in_the_plan(self):
        with patch.dict(os.environ, {"https_proxy": "http://127.0.0.1:9"}):
            payload = sync_bisync.cmd_resync(self.ns("resync"))
        self.assertTrue(any("https_proxy" in line for line in payload["warnings"]))

    def test_no_proxy_quiets_it_and_clears_the_environment(self):
        with patch.dict(os.environ, {"https_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "x"}):
            payload = sync_bisync.cmd_resync(self.ns("resync", no_proxy=True))
            env = sync_common.env_without_proxy()
        self.assertFalse(any("proxy" in line for line in payload["warnings"]))
        self.assertNotIn("https_proxy", env)
        self.assertNotIn("HTTP_PROXY", env)

    def test_an_old_rclone_is_named(self):
        warnings = sync_common.bisync_environment_warnings((1, 60, 1))
        self.assertTrue(any("1.60.1" in line for line in warnings))
        self.assertEqual(sync_common.bisync_environment_warnings((1, 75, 2)), [])

    def test_a_timeout_reaches_the_command_and_is_diagnosed(self):
        result = sync_common.CommandResult(cmd=[], returncode=sync_common.TIMEOUT_RETURNCODE,
                                           stdout="", stderr="Stopped: no result after 5s")
        with patch("tools.sync_bisync.rclone_bisync_run", return_value=result) as run, \
                patch("tools.sync_bisync.run_local_scan"):
            payload = sync_bisync.cmd_run(self.ns(apply=True, timeout_sec=5, no_proxy=True, notify_on_error=False))
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertTrue(run.call_args.kwargs["no_proxy"])
        self.assertEqual(payload["next_action"]["kind"], "timed_out")


class TestRecordedBinary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ydm_binary_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.record = os.path.join(self.tmp, "rclone_binary")
        self.binary = os.path.join(self.tmp, "rclone")
        Path(self.binary).write_text("#!/bin/sh\n")
        os.chmod(self.binary, 0o755)

    def resolve(self, **env):
        environment = {key: value for key, value in os.environ.items() if key != "YDM_RCLONE"}
        environment.update(YDM_VAR_DIR=self.tmp, **env)
        with patch.dict(os.environ, environment, clear=True):
            return sync_common.rclone_bin()

    def test_the_recorded_binary_is_used(self):
        Path(self.record).write_text(self.binary + "\n")
        self.assertEqual(self.resolve(), self.binary)

    def test_the_environment_still_wins(self):
        Path(self.record).write_text(self.binary)
        self.assertEqual(self.resolve(YDM_RCLONE="/elsewhere/rclone"), "/elsewhere/rclone")

    def test_a_record_of_a_missing_binary_falls_back(self):
        Path(self.record).write_text(os.path.join(self.tmp, "gone"))
        self.assertEqual(self.resolve(), "rclone")

    def test_no_record_is_the_old_behaviour(self):
        self.assertEqual(self.resolve(), "rclone")


class TestDoctor(BisyncCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(sync_doctor, "check_timer", return_value=sync_doctor.check(
            "timer", sync_doctor.SKIPPED, "test"))
        patcher.start()
        self.addCleanup(patcher.stop)
        conflict = patch.object(sync_doctor, "daemon_conflict_error", return_value=None)
        conflict.start()
        self.addCleanup(conflict.stop)

    def doctor(self, **kw):
        payload = sync_doctor.cmd_doctor(self.ns("doctor", **kw))
        return payload, {row["name"]: row for row in payload["checks"]}

    def test_a_healthy_machine(self):
        payload, checks = self.doctor()
        self.assertTrue(payload["healthy"], payload["checks"])
        self.assertIsNone(payload["error"])
        self.assertEqual(sync_common.exit_code(payload), 0)
        self.assertEqual(checks["network"]["status"], "skipped")

    def test_it_writes_nothing(self):
        before = sorted(os.listdir(self.var)), Path(self.state).read_text()
        self.doctor()
        self.assertEqual((sorted(os.listdir(self.var)), Path(self.state).read_text()), before)

    def test_it_does_not_touch_the_network_by_default(self):
        with patch("tools.sync_doctor.run_command") as run:
            self.doctor()
        run.assert_not_called()

    def test_a_changed_filter_fails_with_the_next_step(self):
        Path(self.filter_path).write_text("+ /other/**\n- **\n")
        payload, checks = self.doctor()
        self.assertFalse(payload["healthy"])
        self.assertIn("resync", checks["baseline"]["next"])
        self.assertEqual(checks["filter"]["status"], "warn")
        self.assertEqual(sync_common.exit_code(payload), 1)

    def test_quarantine_and_pause_warn_but_do_not_fail(self):
        self.quarantine("a")
        Path(self.filter_path).write_text(sync_policy.expected_bisync_filter(self.policy()))
        state = sync_common.load_bisync_state(self.state)
        state["last_resync_filter_hash"] = sync_common.filter_file_hash(self.filter_path)
        sync_common.save_bisync_state(state, self.state)
        sync_bisync.cmd_pause(self.ns("pause", reason="r", apply=True))
        payload, checks = self.doctor()
        self.assertTrue(payload["healthy"], payload["checks"])
        self.assertEqual(sorted(payload["warned"]), ["pause", "quarantine"])
        self.assertIn("--recheck", checks["quarantine"]["next"])

    def test_a_quarantine_does_not_read_as_a_stale_filter(self):
        self.quarantine("a")
        Path(self.filter_path).write_text(sync_policy.expected_bisync_filter(self.policy()))
        self.assertEqual(self.doctor()[1]["filter"]["status"], "ok")

    def test_a_failed_last_run(self):
        state = sync_common.load_bisync_state(self.state)
        state["last_status"] = "error"
        sync_common.save_bisync_state(state, self.state)
        self.assertEqual(self.doctor()[1]["last_run"]["status"], "fail")

    def test_a_missing_policy(self):
        os.remove(self.policy_path)
        self.assertEqual(self.doctor()[1]["policy"]["status"], "fail")

    def test_network_finds_a_missing_sentinel(self):
        self.assertEqual(self.doctor(check_network=True)[1]["network"]["status"], "warn")
        Path(self.cloud, "RCLONE_TEST").write_text("x")
        self.assertEqual(self.doctor(check_network=True)[1]["network"]["status"], "ok")


class TestStaleFilterWarning(Case):
    def test_sync_tree_does_not_cry_wolf_over_a_quarantine(self):
        """It compared the filter with a render that left quarantine and ignore
        out, so any machine with either showed a permanent false warning."""
        from tools.sync_tree_policy import load_policy_context
        self.quarantine("a")
        Path(self.root + ".bisync.filters").write_text(sync_policy.expected_bisync_filter(self.policy()))
        context = load_policy_context(self.policy_path, self.root)
        self.assertFalse(context.filter_mismatch)


if __name__ == "__main__":
    unittest.main()
