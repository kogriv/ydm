#!/usr/bin/env python3
"""The scheduled Android job: what it does with the guard's verdict.

`tools/termux/job_run.sh` decides, every half hour on the device, whether
bisync runs. It had no tests at all, which is how it went for months writing
`run OK` while the guard was absent (issue #5) and, later, writing nothing at
all when the guard stopped a run (issue #14).

Nothing here runs rclone, sync_rename or sync_bisync: a stub `python3` earlier
on PATH answers for them with a canned envelope, so what is under test is the
script's own reasoning. The stub delegates anything else to the real
interpreter, because the script parses that envelope with `python3 -c` too.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JOB_RUN = ROOT / "tools" / "termux" / "job_run.sh"

STUB = """#!/bin/sh
case "$*" in
    *sync_rename.py*)
        cat "$STUB_PREFLIGHT_JSON"
        exit "${STUB_PREFLIGHT_RC:-0}"
        ;;
    *sync_bisync.py*)
        echo "bisync" >> "$STUB_TRACE"
        exit 0
        ;;
esac
exec "$STUB_REAL_PYTHON" "$@"
"""


class JobRunTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.var_dir = os.path.join(self.tmpdir, "var")
        os.makedirs(self.var_dir)
        self.log_path = os.path.join(self.var_dir, "bisync.log")
        self.trace = os.path.join(self.tmpdir, "trace")
        self.mirror = os.path.join(self.tmpdir, "mirror")
        os.makedirs(self.mirror)

        # A recorder in place of termux-notification. Without it the script
        # reaches Termux's own binary by absolute path — PATH cannot intercept
        # that — and a suite run on the device sent a real "bisync blocked"
        # notification to the phone, indistinguishable from the guard stopping
        # a live sync. It happened, on 2026-09-09.
        self.notify_log = os.path.join(self.tmpdir, "notifications")
        self.notify_bin = os.path.join(self.tmpdir, "notify-stub")
        with open(self.notify_bin, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\nprintf "%s\\n" "$*" >> "$NOTIFY_LOG"\n')
        os.chmod(self.notify_bin, 0o755)

        bindir = os.path.join(self.tmpdir, "bin")
        os.makedirs(bindir)
        stub = os.path.join(bindir, "python3")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(STUB)
        os.chmod(stub, 0o755)
        self.bindir = bindir

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_job(self, decision, reason, rc=0):
        """Drive the script with a preflight that answers `decision`/`reason`."""
        envelope = os.path.join(self.tmpdir, "preflight.json")
        with open(envelope, "w", encoding="utf-8") as handle:
            json.dump(
                {"success": rc == 0, "data": {"decision": decision, "reason": reason}},
                handle,
            )
        env = dict(os.environ)
        env.update({
            "PATH": self.bindir + os.pathsep + env["PATH"],
            "STUB_REAL_PYTHON": sys.executable,
            "STUB_PREFLIGHT_JSON": envelope,
            "STUB_PREFLIGHT_RC": str(rc),
            "STUB_TRACE": self.trace,
            "YDM_VAR_DIR": self.var_dir,
            "YDM_NOTIFY_BIN": self.notify_bin,
            "NOTIFY_LOG": self.notify_log,
        })
        result = subprocess.run(
            ["bash", str(JOB_RUN), "--local-root", self.mirror,
             "--db-path", os.path.join(self.tmpdir, "monitor.db")],
            env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def log(self):
        if not os.path.exists(self.log_path):
            return ""
        with open(self.log_path, encoding="utf-8") as handle:
            return handle.read()

    def bisync_ran(self):
        return os.path.exists(self.trace)

    def notifications(self):
        if not os.path.exists(self.notify_log):
            return []
        with open(self.notify_log, encoding="utf-8") as handle:
            return [line for line in handle.read().splitlines() if line]


class GuardDecisionsReachTheLog(JobRunTestCase):
    """A stopped sync must not look like a job that never fired."""

    def test_a_skip_is_written_down_with_its_reason(self):
        self.run_job("skip_bisync", "no_comparable_baseline")
        self.assertIn("skip_bisync (no_comparable_baseline)", self.log())
        self.assertFalse(self.bisync_ran())

    def test_a_block_is_written_down_with_its_reason(self):
        self.run_job("block_bisync", "ambiguous_candidates")
        self.assertIn("block_bisync (ambiguous_candidates)", self.log())
        self.assertFalse(self.bisync_ran())

    def test_a_block_is_the_only_decision_that_notifies(self):
        """The log is the record; the notification is the interruption.

        A block is the one verdict a person has to act on, so it is the one
        that reaches the phone. The rest resolve themselves in a cycle and must
        stay quiet — a notification per skip would train the operator to swipe
        the important one away too.

        This is also the assertion that keeps a test suite from notifying the
        device it runs on: it can only pass while the binary is reached through
        `YDM_NOTIFY_BIN`, which is what stops the real one from being called.
        """
        self.run_job("block_bisync", "ambiguous_candidates")
        self.assertEqual(len(self.notifications()), 1, self.notifications())
        self.assertIn("bisync blocked", self.notifications()[0])

    def test_a_skip_does_not_reach_the_phone(self):
        self.run_job("skip_bisync", "no_comparable_baseline")
        self.assertEqual(self.notifications(), [])

    def test_a_guardless_run_does_not_reach_the_phone_either(self):
        """`allow_bisync (error)` is issue #5, and it goes to the log only.

        It is the line worth chasing, but it is not actionable in the moment:
        bisync already ran, and the next run either repeats it or does not.
        """
        self.run_job("allow_bisync", "error")
        self.assertIn("allow_bisync (error)", self.log())
        self.assertEqual(self.notifications(), [])

    def test_the_reason_is_the_one_the_preflight_gave(self):
        """`baseline_unreadable` and `no_comparable_baseline` both skip.

        They send whoever reads the log to different places, so the line has
        to carry the one that happened rather than a single word for both.
        """
        self.run_job("skip_bisync", "baseline_unreadable")
        self.assertIn("skip_bisync (baseline_unreadable)", self.log())
        self.assertNotIn("no_comparable_baseline", self.log())

    def test_the_log_follows_ydm_var_dir_like_everything_else(self):
        """sync_bisync.py resolves this file through var_path().

        A bare `var/bisync.log` here would put the guard's lines in one file
        and the run's lines in another as soon as anything redirects it.
        """
        self.run_job("skip_bisync", "no_comparable_baseline")
        self.assertTrue(os.path.exists(self.log_path))
        self.assertFalse(os.path.exists(os.path.join(ROOT, "var", "bisync.log.test")))


class AnAbsentGuardIsNotAPassingGuard(JobRunTestCase):
    """Both allow bisync. Only one of them is fine, and they must not look alike."""

    def test_a_crash_is_written_down_and_bisync_still_runs(self):
        self.run_job("allow_bisync", "error", rc=1)
        self.assertIn("FAILED rc=1", self.log())
        self.assertTrue(self.bisync_ran())

    def test_an_error_inside_the_envelope_is_written_down_too(self):
        """The exit code is 0: sync_rename reports errors in its JSON.

        So the rc test cannot see this one, and without a line of its own a run
        with no guard at all is indistinguishable from a clean one — which is
        exactly how issue #5 stayed invisible for hours.
        """
        self.run_job("allow_bisync", "error", rc=0)
        self.assertIn("allow_bisync (error)", self.log())
        self.assertTrue(self.bisync_ran())

    def test_an_ordinary_pass_says_nothing_and_runs(self):
        """The guard passing is the common case; it must not add noise.

        `sync_bisync.py` already writes its own line for the run itself.
        """
        self.run_job("allow_bisync", "no_candidates")
        self.assertEqual(self.log(), "")
        self.assertTrue(self.bisync_ran())


if __name__ == "__main__":
    unittest.main(verbosity=2)
