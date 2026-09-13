#!/usr/bin/env python3
"""What `sync_bisync run` interrupts a person for, and how often.

The scheduled job runs every half hour and a blocked run stays blocked until
somebody acts, so "notify on error" meant the same card arriving forever. On the
device that was four identical "Filter-file changed since the last resync"
notifications stacked in the shade overnight (2026-09-13) — which is how an
operator learns to swipe ydm's notifications away unread, the one that matters
included.

Nothing here runs rclone: the runs are blocked before it would be reached, which
is the case that repeated.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.sync_bisync import (  # noqa: E402
    build_parser,
    cmd_run,
    cmd_status,
    human_notice,
)


class BlockedRunNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.var_dir = os.path.join(self.tmpdir, "var")
        os.makedirs(self.var_dir)
        self.mirror = os.path.join(self.tmpdir, "mirror")
        os.makedirs(self.mirror)
        self.filter_path = os.path.join(self.tmpdir, "mirror.bisync.filters")
        with open(self.filter_path, "w", encoding="utf-8") as handle:
            handle.write("+ /A/**\n+ /RCLONE_TEST\n- **\n")
        patcher = mock.patch.dict(os.environ, {"YDM_VAR_DIR": self.var_dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _args(self):
        args = build_parser().parse_args(["run"])
        args.local_root = self.mirror
        args.filter_path = self.filter_path
        args.db_path = os.path.join(self.tmpdir, "monitor.db")
        args.apply = True
        return args

    def _run(self):
        """One scheduled run, counting the notifications it sends."""
        with mock.patch("tools.sync_bisync.notify") as notifier:
            payload = cmd_run(self._args())
        return payload, notifier.call_count

    def _state(self):
        path = os.path.join(self.var_dir, "bisync_state.json")
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def _log(self):
        path = os.path.join(self.var_dir, "bisync.log")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_the_first_blocked_run_says_so(self):
        payload, sent = self._run()
        self.assertIsNotNone(payload["error"])
        self.assertEqual(sent, 1)

    def test_the_next_twelve_do_not(self):
        """Half-hourly for six hours, and the shade holds one card, not twelve."""
        self._run()
        repeats = sum(self._run()[1] for _ in range(12))
        self.assertEqual(repeats, 0)

    def test_the_log_still_records_every_one(self):
        """The interruption is deduplicated; the audit trail is not.

        `var/bisync.log` is how anyone reconstructs what the job did — a gap in
        it looks exactly like a job that never fired, which is the confusion
        issue #14 was about.
        """
        for _ in range(3):
            self._run()
        self.assertEqual(self._log().count("run BLOCKED"), 3)

    def test_a_different_problem_is_a_new_notification(self):
        """Deduplication must not swallow news."""
        self._run()
        state = self._state()
        state["last_notified_error"] = "something else entirely"
        with open(os.path.join(self.var_dir, "bisync_state.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(state, handle)
        self.assertEqual(self._run()[1], 1)

    def test_a_cleared_flag_lets_the_same_problem_speak_again(self):
        """Whatever clears the flag, clearing it has to restore the interruption.

        This is the reset half of the rule, tested where it lives — in
        `notify_once`. That `cmd_resync` actually performs the reset is a
        different claim and is asserted against a real resync on the bench
        (`test_menu_six_applies_the_resync_it_offers`), because asserting it here
        would mean simulating the thing under test.
        """
        self._run()
        self.assertTrue(self._state().get("last_notified_error"))
        state = self._state()
        state["last_notified_error"] = None
        with open(os.path.join(self.var_dir, "bisync_state.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(state, handle)
        self.assertEqual(self._run()[1], 1)

    def test_the_message_points_at_the_menu_not_a_command(self):
        """The person reading it works through `ydm`, not the CLI.

        The error string keeps naming the command — it goes into the log and the
        JSON envelope, where that is the right answer — but the notification is
        addressed to somebody holding a phone.
        """
        notice = human_notice(
            "Filter-file changed since the last resync — bisync would block on "
            "this internally. Run `sync_bisync.py resync --apply` again before "
            "the next scheduled `run`."
        )
        self.assertIn("6 (Resync baseline)", notice)
        self.assertNotIn("sync_bisync.py", notice)

    def test_an_unrecognised_error_is_passed_through_verbatim(self):
        """Better a raw message than a friendly one that hides what happened."""
        self.assertEqual(human_notice("rclone exploded"), "rclone exploded")

    def test_the_log_keeps_the_command_even_when_the_card_does_not(self):
        self._run()
        self.assertIn("sync_bisync.py resync --apply", self._log())


class RcloneLosesItsListingsTests(BlockedRunNotificationTests):
    """rclone keeps state of its own and can lose it without ydm noticing.

    On 2026-09-13 one run died mid-flight; the next seven aborted on
    "cannot find prior Path1 or Path2 listings … Must run --resync to recover",
    while the menu header said `resync: not needed` — because ydm decided that
    from its own filter hash, which had not changed and could not see this.
    The log said `run FAILED rc=2`, and rc 2 is rclone's code for everything.
    """

    def _args_with_a_baseline(self):
        """A run that gets as far as calling rclone, rather than being blocked.

        Seeded once, not per call: rewriting the state before every run would
        wipe the notification flag, and the deduplication check below would then
        be measuring the fixture instead of the code.
        """
        from tools.sync_common import filter_file_hash, load_bisync_state, save_bisync_state

        state = load_bisync_state()
        if not state.get("last_resync_filter_hash"):
            state["last_resync_filter_hash"] = filter_file_hash(self.filter_path)
            state["last_resync_at"] = "2026-09-13T02:38:33"
            save_bisync_state(state)
        return self._args()

    def _run_against(self, returncode, stderr):
        from tools.sync_common import CommandResult

        result = CommandResult(cmd=["rclone"], returncode=returncode,
                               stdout="", stderr=stderr)
        with mock.patch("tools.sync_bisync.rclone_bisync_run", return_value=result), \
             mock.patch("tools.sync_bisync.LOCK_PATH",
                        os.path.join(self.tmpdir, "lock")), \
             mock.patch("tools.sync_bisync.notify") as notifier:
            payload = cmd_run(self._args_with_a_baseline())
        return payload, notifier

    def test_rclones_own_verdict_is_recorded(self):
        payload, _ = self._run_against(
            2, "ERROR : Bisync aborted. Must run --resync to recover.\n")
        self.assertIn("lost its bisync listings", payload["error"], payload["error"])
        self.assertTrue(self._state().get("rclone_wants_resync"))

    def test_another_kind_of_failure_is_not_read_as_that_one(self):
        """rc 2 is rclone's code for everything, so the code cannot decide it."""
        payload, _ = self._run_against(2, "ERROR : couldn't connect: no route to host\n")
        self.assertNotIn("lost its bisync listings", payload["error"])
        self.assertFalse(self._state().get("rclone_wants_resync"))

    def test_the_status_stops_saying_resync_is_not_needed(self):
        """The filter hash still matches; the answer must not come only from it."""
        self._run_against(2, "Must run --resync to recover.\n")
        args = build_parser().parse_args(["status"])
        args.local_root = self.mirror
        args.filter_path = self.filter_path
        args.db_path = os.path.join(self.tmpdir, "monitor.db")
        self.assertTrue(cmd_status(args)["resync_needed"])

    def test_the_card_says_what_to_do_about_it(self):
        notice = human_notice(
            "rclone lost its bisync listings and needs a fresh baseline "
            "(returncode=2)")
        self.assertIn("6 (Resync baseline)", notice)
        self.assertNotIn("returncode", notice)

    def test_seven_identical_failures_are_one_card(self):
        """Half-hourly, and it stayed broken for three hours on the device."""
        stderr = "Must run --resync to recover.\n"
        first = self._run_against(2, stderr)[1].call_count
        repeats = sum(self._run_against(2, stderr)[1].call_count for _ in range(6))
        self.assertEqual((first, repeats), (1, 0))


class TheCardIsTakenBackTests(RcloneLosesItsListingsTests):
    """A notification says "now", or it teaches the reader to ignore it.

    Android keeps a card until something removes it, and nothing did: on
    2026-09-13 the shade held "Sync is stuck: bisync lost its baseline and every
    run will fail" for hours after a resync had fixed exactly that and the job
    had gone back to `run OK`. Two older cards sat under it, one of them the
    pre-fix wording. Three statements about the past, none about the present.
    """

    def _succeeding_run(self):
        from tools.sync_common import CommandResult

        ok = CommandResult(cmd=["rclone"], returncode=0, stdout="", stderr="")
        with mock.patch("tools.sync_bisync.rclone_bisync_run", return_value=ok), \
             mock.patch("tools.sync_bisync.LOCK_PATH",
                        os.path.join(self.tmpdir, "lock")), \
             mock.patch("tools.sync_bisync.run_local_scan") as scan, \
             mock.patch("tools.sync_bisync.dismiss_notification") as dismiss, \
             mock.patch("tools.sync_bisync.notify"):
            scan.return_value = mock.Mock(started=True, scan_id=1, error=None,
                                          duration_sec=0.1)
            cmd_run(self._args_with_a_baseline())
        return dismiss

    def test_a_run_that_works_removes_the_card(self):
        self._run_against(2, "Must run --resync to recover.\n")
        self.assertTrue(self._state().get("last_notified_error"))
        self.assertEqual(self._succeeding_run().call_count, 1)

    def test_nothing_is_removed_when_nothing_was_said(self):
        """A card belonging to something else must not be swept up."""
        self.assertEqual(self._succeeding_run().call_count, 0)

    def test_the_id_makes_a_new_card_replace_the_old(self):
        """Without it every notification is a separate, unremovable entry."""
        from tools.sync_common import SYNC_NOTIFICATION_ID, notify

        with mock.patch("tools.sync_common.shutil.which", return_value="/bin/true"), \
             mock.patch("tools.sync_common.subprocess.run") as run:
            notify("t", "m")
        argv = run.call_args[0][0]
        self.assertIn("--id", argv)
        self.assertIn(SYNC_NOTIFICATION_ID, argv)
        self.assertIn("--alert-once", argv)


if __name__ == "__main__":
    unittest.main()
