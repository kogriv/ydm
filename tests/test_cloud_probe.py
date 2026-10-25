#!/usr/bin/env python3
"""The watcher that says when Yandex Disk's resource API is back.

It exists because of the outage of 2026-10-11, and its failure modes are the
quiet kind: a watcher that never announces the reopening and one that cannot
log in look identical from the phone. So the tests spend their effort on when a
card is sent and when it is not — one per change, not one per run — and on the
two ways of going blind.

Nothing here touches the network: the opener is a fake that answers by URL.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import tempfile
import unittest
import urllib.error
from unittest import mock

from tools import cloud_probe
from tools.cloud_probe import (
    AUTH, CLOSED, OPEN, UNKNOWN, Probe, probe, watch_once,
)

SECRET = "y0_SECRET_TOKEN_VALUE"


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDisk:
    """Answers by URL. `root` and `files` are an HTTP code, or an exception."""

    def __init__(self, root=410, files=410):
        self.root, self.files = root, files
        self.requests = []
        self.tokens = []

    def __call__(self, request, timeout=None):
        self.requests.append(request.full_url)
        self.tokens.append(request.get_header("Authorization"))
        answer = self.files if "/resources/files" in request.full_url else self.root
        if isinstance(answer, Exception):
            raise answer
        if answer == 200:
            return FakeResponse(200)
        raise urllib.error.HTTPError(
            request.full_url, answer, "x", {}, io.BytesIO(b'{"message":"x"}'))


class ProbeVerdict(unittest.TestCase):
    def test_root_listing_200_is_open(self):
        self.assertEqual(probe(SECRET, FakeDisk(200, 200)).status, OPEN)

    def test_410_is_closed(self):
        result = probe(SECRET, FakeDisk(410, 410))
        self.assertEqual((result.status, result.root, result.files), (CLOSED, 410, 410))

    def test_the_root_decides_not_the_recent_files_endpoint(self):
        """The question is whether the tree is readable. /resources/files is
        context for the log line, and the smart scan standing on it is its own
        concern."""
        self.assertEqual(probe(SECRET, FakeDisk(200, 410)).status, OPEN)
        self.assertEqual(probe(SECRET, FakeDisk(410, 200)).status, CLOSED)

    def test_a_rejected_token_is_unknown_and_marked_as_auth(self):
        for code in (401, 403):
            result = probe(SECRET, FakeDisk(code, code))
            self.assertEqual((result.status, result.auth), (UNKNOWN, True), code)

    def test_a_server_error_or_no_network_is_unknown_but_not_auth(self):
        for answer in (500, 503, urllib.error.URLError("no route"), TimeoutError()):
            result = probe(SECRET, FakeDisk(answer, answer))
            self.assertEqual((result.status, result.auth), (UNKNOWN, False), answer)

    def test_it_asks_the_resource_layer_not_the_account_endpoint(self):
        """`rclone about` answers 200 for the whole outage because it hits
        /v1/disk; a probe that did the same would announce a reopening that
        never happened."""
        disk = FakeDisk(200, 200)
        probe(SECRET, disk)
        self.assertTrue(any("/v1/disk/resources?" in url and "path=%2F" in url
                            for url in disk.requests), disk.requests)
        self.assertFalse(any(url.rstrip("/").endswith("/v1/disk") for url in disk.requests))


class WatchTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ydm_watch_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = os.path.join(self.tmp, "var", "cloud_watch.json")
        self.log = os.path.join(self.tmp, "var", "cloud_watch.log")
        self.cards = []
        self.dismissed = 0
        self.refreshed = 0

    def notify(self, title, content):
        self.cards.append((title, content))

    def dismiss(self):
        self.dismissed += 1

    def refresh(self):
        self.refreshed += 1

    def tick(self, disk):
        return watch_once(
            lambda: SECRET, state_path=self.state, log_path=self.log,
            notify=self.notify, dismiss=self.dismiss, refresh=self.refresh,
            opener=disk,
        )

    def remembered(self):
        with open(self.state, encoding="utf-8") as handle:
            return json.load(handle)["status"]

    def log_lines(self):
        with open(self.log, encoding="utf-8") as handle:
            return handle.read().splitlines()


class WhenACardIsSent(WatchTestCase):
    def test_a_closed_disk_sends_nothing(self):
        self.tick(FakeDisk(410, 410))
        self.assertEqual(self.cards, [])
        self.assertEqual(self.remembered(), CLOSED)

    def test_the_change_to_open_sends_one_card(self):
        self.tick(FakeDisk(410, 410))
        self.tick(FakeDisk(200, 200))
        self.assertEqual(len(self.cards), 1)
        self.assertEqual(self.remembered(), OPEN)

    def test_it_says_look_and_does_not_say_sync(self):
        """Reopened is not safe: the checklist in the incident note comes
        first, and the card must send the reader there rather than invite a
        resync."""
        self.tick(FakeDisk(200, 200))
        title, content = self.cards[0]
        self.assertIn("incidents/yandex-disk-410-gone", content)
        self.assertIn("не включён", content)

    def test_staying_open_does_not_repeat_the_card(self):
        for _ in range(4):
            self.tick(FakeDisk(200, 200))
        self.assertEqual(len(self.cards), 1)

    def test_closing_again_takes_the_card_back(self):
        """It described a state that is no longer true."""
        self.tick(FakeDisk(200, 200))
        before = self.dismissed
        self.tick(FakeDisk(410, 410))
        self.assertEqual(self.dismissed, before + 1)

    def test_staying_closed_does_not_keep_dismissing(self):
        self.tick(FakeDisk(410, 410))
        after_first = self.dismissed
        self.tick(FakeDisk(410, 410))
        self.assertEqual(self.dismissed, after_first)

    def test_reopening_after_a_relapse_is_announced_again(self):
        self.tick(FakeDisk(200, 200))
        self.tick(FakeDisk(410, 410))
        self.tick(FakeDisk(200, 200))
        self.assertEqual(len(self.cards), 2)


class NoNetworkProvesNothing(WatchTestCase):
    def test_a_dead_signal_between_two_opens_does_not_announce_twice(self):
        self.tick(FakeDisk(200, 200))
        self.tick(FakeDisk(urllib.error.URLError("no route"), TimeoutError()))
        self.tick(FakeDisk(200, 200))
        self.assertEqual(len(self.cards), 1)
        self.assertEqual(self.remembered(), OPEN)

    def test_a_server_error_does_not_dismiss_a_standing_card(self):
        self.tick(FakeDisk(200, 200))
        before = self.dismissed
        self.tick(FakeDisk(503, 503))
        self.assertEqual(self.dismissed, before)

    def test_an_unreachable_first_run_leaves_no_state_behind(self):
        self.tick(FakeDisk(urllib.error.URLError("no route"), TimeoutError()))
        self.assertFalse(os.path.exists(self.state))

    def test_every_run_is_logged_even_when_nothing_is_said(self):
        self.tick(FakeDisk(410, 410))
        self.tick(FakeDisk(503, 503))
        self.assertEqual(len(self.log_lines()), 2)
        self.assertIn("unknown root=503", self.log_lines()[1])


class GoingBlind(WatchTestCase):
    def test_a_rejected_token_makes_the_job_refresh_it_and_ask_again(self):
        """rclone refreshes its own access_token and nothing runs rclone while
        the sync is off; one cheap call is the least worth trying."""
        answers = iter([401, 401, 200, 200])  # root, files; then root, files

        class Recovering(FakeDisk):
            def __call__(inner, request, timeout=None):
                inner.root = inner.files = next(answers)
                return FakeDisk.__call__(inner, request, timeout)

        self.tick(Recovering())
        self.assertEqual(self.refreshed, 1)
        self.assertEqual(self.remembered(), OPEN)
        self.assertEqual(len(self.cards), 1)
        self.assertIn("снова отвечает", self.cards[0][0])

    def test_a_token_that_stays_rejected_is_reported_once(self):
        """A watcher that cannot log in is silent in exactly the way a healthy
        one watching a closed disk is."""
        for _ in range(3):
            self.tick(FakeDisk(401, 401))
        self.assertEqual(len(self.cards), 1)
        self.assertIn("не может войти", self.cards[0][0])
        self.assertEqual(self.remembered(), AUTH)

    def test_recovering_from_blindness_is_still_a_change(self):
        self.tick(FakeDisk(401, 401))
        self.tick(FakeDisk(200, 200))
        self.assertEqual(len(self.cards), 2)
        self.assertIn("снова отвечает", self.cards[1][0])

    def test_recovering_to_closed_takes_the_blind_card_back(self):
        self.tick(FakeDisk(401, 401))
        before = self.dismissed
        self.tick(FakeDisk(410, 410))
        self.assertEqual(self.dismissed, before + 1)


class Hygiene(WatchTestCase):
    def test_the_token_never_reaches_the_log_state_or_a_card(self):
        for disk in (FakeDisk(410, 410), FakeDisk(200, 200), FakeDisk(401, 401)):
            self.tick(disk)
        with open(self.state, encoding="utf-8") as handle:
            written = handle.read()
        written += "\n".join(self.log_lines())
        written += json.dumps(self.cards, ensure_ascii=False)
        self.assertNotIn(SECRET, written)

    def test_the_token_does_go_to_yandex_in_the_header(self):
        """Pins the test above to something: it must not pass because nothing
        was ever sent."""
        disk = FakeDisk(410, 410)
        self.tick(disk)
        self.assertIn(f"OAuth {SECRET}", disk.tokens)

    def test_a_corrupt_state_file_is_treated_as_no_state(self):
        os.makedirs(os.path.dirname(self.state))
        with open(self.state, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.tick(FakeDisk(200, 200))
        self.assertEqual(len(self.cards), 1)
        self.assertEqual(self.remembered(), OPEN)

    def test_a_missing_var_directory_is_created(self):
        self.assertFalse(os.path.exists(os.path.dirname(self.log)))
        self.tick(FakeDisk(410, 410))
        self.assertTrue(os.path.exists(self.log))


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ydm_watch_cli_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.calls = os.path.join(self.tmp, "calls")
        recorder = os.path.join(self.tmp, "recorder")
        with open(recorder, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
        os.chmod(recorder, os.stat(recorder).st_mode | stat.S_IXUSR)
        self.env = {
            "YDM_VAR_DIR": os.path.join(self.tmp, "var"),
            "YDM_NOTIFY_BIN": recorder,
            "YDM_NOTIFY_REMOVE_BIN": recorder,
            "CALLS": self.calls,
        }
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        token = mock.patch.object(cloud_probe, "resolve_token", return_value=SECRET)
        token.start()
        self.addCleanup(token.stop)

    def run_main(self, command, result):
        with mock.patch.object(cloud_probe, "probe", return_value=result), \
                mock.patch("sys.stdout", new=io.StringIO()):
            return cloud_probe.main([command])

    def test_check_exit_codes_tell_the_three_verdicts_apart(self):
        self.assertEqual(self.run_main("check", Probe(OPEN, 200, 200)), 0)
        self.assertEqual(self.run_main("check", Probe(CLOSED, 410, 410)), 10)
        self.assertEqual(self.run_main("check", Probe(UNKNOWN, None, None)), 11)

    def test_check_remembers_nothing(self):
        self.run_main("check", Probe(OPEN, 200, 200))
        self.assertFalse(os.path.exists(os.path.join(self.env["YDM_VAR_DIR"], "cloud_watch.json")))

    def test_watch_sends_the_card_through_the_named_binary(self):
        """Through YDM_NOTIFY_BIN, the way job_run.sh names termux-notification:
        a test must never reach the real phone."""
        self.run_main("watch", Probe(OPEN, 200, 200))
        with open(self.calls, encoding="utf-8") as handle:
            sent = handle.read()
        self.assertIn("--id ydm-cloud-watch", sent)
        self.assertIn("--alert-once", sent)

    def test_watch_writes_under_the_redirected_var_directory(self):
        self.run_main("watch", Probe(CLOSED, 410, 410))
        var = self.env["YDM_VAR_DIR"]
        self.assertTrue(os.path.exists(os.path.join(var, "cloud_watch.json")))
        self.assertTrue(os.path.exists(os.path.join(var, "cloud_watch.log")))

    def test_a_watcher_that_breaks_says_so_in_the_log(self):
        """The job's output is thrown away. Without a line here a crash looks
        exactly like a disk that is still closed."""
        with mock.patch.object(cloud_probe, "resolve_token",
                               side_effect=SystemExit("rclone remote not found: yandex")), \
                mock.patch("sys.stdout", new=io.StringIO()):
            code = cloud_probe.main(["watch"])
        self.assertEqual(code, 12)
        with open(os.path.join(self.env["YDM_VAR_DIR"], "cloud_watch.log"), encoding="utf-8") as handle:
            last = handle.read().splitlines()[-1]
        self.assertIn("error rclone remote not found", last)

    def test_an_unexpected_exception_is_logged_with_its_type(self):
        with mock.patch.object(cloud_probe, "probe", side_effect=ValueError("boom")), \
                mock.patch("sys.stdout", new=io.StringIO()):
            code = cloud_probe.main(["watch"])
        self.assertEqual(code, 12)
        with open(os.path.join(self.env["YDM_VAR_DIR"], "cloud_watch.log"), encoding="utf-8") as handle:
            self.assertIn("error ValueError: boom", handle.read())

    def test_check_does_not_swallow_a_failure(self):
        """Run by hand, a traceback is the right answer."""
        with mock.patch.object(cloud_probe, "resolve_token", side_effect=SystemExit("nope")):
            with self.assertRaises(SystemExit):
                cloud_probe.main(["check"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
