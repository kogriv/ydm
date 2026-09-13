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

from tools.sync_bisync import build_parser, cmd_run, human_notice  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
