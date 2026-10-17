#!/usr/bin/env python3
"""The three things `install_job.sh` says to termux-job-scheduler.

It had no tests, and on 2026-10-12 `remove` turned out not to work at all:
`termux-job-scheduler --cancel "$JOB_ID"` answers "too many arguments" and
leaves the job scheduled, because the id is a flag of its own
(`--cancel --job-id N`). That was found with the sync already down — Yandex's
resource API had been answering 410 for nineteen hours after the attack on the
Vladimir datacentre — and stopping the job was the one thing the owner had asked
for, so a stop that silently did nothing was the worst possible shape for it.

Nothing here reaches Termux: the binary is named through
`YDM_JOB_SCHEDULER_BIN`, which a recorder stands in for.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL_JOB = ROOT / "tools" / "termux" / "install_job.sh"


class InstallJobTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ydm_job_")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.calls = os.path.join(self.tmpdir, "calls")
        self.pending = os.path.join(self.tmpdir, "pending")
        self.write_pending("No pending jobs")
        self.stub = os.path.join(self.tmpdir, "scheduler-stub")
        self.write_stub(0)
        self.target = os.path.join(self.tmpdir, "ydm_bisync_job.sh")

    def write_pending(self, text):
        """What the stub answers `--pending` with: the scheduler's real state."""
        with open(self.pending, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")

    def write_stub(self, cancel_rc):
        with open(self.stub, "w", encoding="utf-8") as handle:
            handle.write(
                '#!/bin/sh\n'
                'printf "%s\\n" "$*" >> "$CALLS"\n'
                'case "$1" in\n'
                '  --pending|-p) cat "$PENDING" ;;\n'
                f'  --cancel) exit {cancel_rc} ;;\n'
                'esac\n'
            )
        os.chmod(self.stub, 0o755)

    def run_it(self, command, expect_rc=0):
        env = dict(os.environ)
        env.update({
            "YDM_JOB_SCHEDULER_BIN": self.stub,
            "YDM_JOB_PATH": self.target,
            "YDM_JOB_ID": "31415",
            "CALLS": self.calls,
            "PENDING": self.pending,
            "HOME": self.tmpdir,
        })
        result = subprocess.run(
            ["bash", str(INSTALL_JOB), command],
            env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, expect_rc, result.stderr)
        return result

    def calls_made(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls, encoding="utf-8") as handle:
            return [line for line in handle.read().splitlines() if line]


class RemoveActuallyCancels(InstallJobTestCase):
    def test_the_job_id_goes_in_as_a_flag(self):
        """`--cancel 31415` is rejected by the tool and the job survives."""
        self.run_it("remove")
        self.assertEqual(self.calls_made()[0], "--cancel --job-id 31415")

    def test_the_scheduler_is_asked_what_is_actually_scheduled(self):
        """Saying "cancelled" without looking is what hid the bug."""
        self.run_it("remove")
        self.assertIn("--pending", self.calls_made())

    def test_a_job_that_survived_the_cancel_is_reported_as_surviving(self):
        """The shape of the 2026-10-12 bug, told by end state rather than by
        exit code — the tool exits 0 for a job that was never there and 1 for
        a malformed cancel, so the code alone cannot tell them apart."""
        self.write_pending(
            "Pending Job 31415: /data/data/com.termux/files/home/ydm_bisync_job.sh"
            "\t(periodic: 1800000ms) (persisted)"
        )
        result = self.run_it("remove", expect_rc=1)
        self.assertIn("STILL scheduled", result.stderr)
        self.assertNotIn("is not scheduled", result.stdout)

    def test_a_refusal_from_the_tool_does_not_hide_a_successful_stop(self):
        """And the mirror case: a non-zero exit with the job actually gone is
        not worth an error. Only the pending list decides."""
        self.write_stub(cancel_rc=1)
        result = self.run_it("remove")
        self.assertIn("is not scheduled", result.stdout)

    def test_another_jobs_id_is_not_mistaken_for_this_one(self):
        """`grep "Job 31415:"` and not a bare id: Termux prints the period in
        milliseconds on the same line, and 1800000 contains no job id by luck
        alone."""
        self.write_pending(
            "Pending Job 314151: /data/data/com.termux/files/home/other.sh"
            "\t(periodic: 1800000ms)"
        )
        result = self.run_it("remove")
        self.assertIn("is not scheduled", result.stdout)


class StatusAndInstall(InstallJobTestCase):
    def test_status_asks_for_the_pending_list(self):
        self.run_it("status")
        self.assertEqual(self.calls_made(), ["--pending"])

    def test_install_registers_a_periodic_persisted_job(self):
        """Half an hour, surviving reboots. A job that does not persist stops
        at the first restart and nothing says so."""
        with open(ROOT / "tools" / "termux" / "ydm_bisync_job.sh", encoding="utf-8"):
            pass  # the template must exist; install copies it
        self.run_it("install")
        argv = self.calls_made()[0]
        self.assertIn("--job-id 31415", argv)
        self.assertIn("--period-ms 1800000", argv)
        self.assertIn("--persisted true", argv)
        self.assertIn(f"--script {self.target}", argv)

    def test_install_leaves_an_executable_copy_outside_shared_storage(self):
        """Shared storage ignores chmod +x and the job then never runs."""
        self.run_it("install")
        self.assertTrue(os.access(self.target, os.X_OK))

    def test_a_missing_scheduler_is_named_not_guessed_at(self):
        """The package and the Termux:API app are two separate installs, and
        having only one of them is the common way to get here."""
        os.remove(self.stub)
        result = self.run_it("status", expect_rc=1)
        self.assertIn("termux-job-scheduler not found", result.stderr)
        self.assertIn("termux-api", result.stderr)
        self.assertEqual(self.calls_made(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
