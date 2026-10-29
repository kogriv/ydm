#!/usr/bin/env python3
"""Which rclone ydm runs, and what it asks of it.

On a PC rclone is shared: other projects copy and mount with the system
package. ydm's bisync needs a newer one, and gets it by path (`YDM_RCLONE`)
rather than by replacing everyone's. And bisync before 1.66 aborts the whole
run on one file the cloud refuses to hand over — the device runs 1.60 and hit
that on 2026-10-29 — so the flags that tolerate it go only to a version that
has them.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sync_common  # noqa: E402


def fake_rclone(directory: str, version_line: str) -> str:
    """An executable that answers `version` like rclone and records other calls."""
    path = os.path.join(directory, "rclone-fake")
    log = os.path.join(directory, "calls.log")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "#!/bin/sh\n"
            f'if [ "$1" = version ]; then echo "{version_line}"; exit 0; fi\n'
            f'echo "$@" >> "{log}"\n'
        )
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


class TestWhichRclone(unittest.TestCase):
    def setUp(self):
        sync_common._RCLONE_VERSIONS.clear()

    def test_path_rclone_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YDM_RCLONE", None)
            self.assertEqual(sync_common.rclone_bin(), "rclone")

    def test_ydm_rclone_names_the_binary(self):
        with patch.dict(os.environ, {"YDM_RCLONE": "/opt/rclone-v1.75.2/rclone"}):
            self.assertEqual(sync_common.rclone_bin(), "/opt/rclone-v1.75.2/rclone")

    def test_bisync_runs_the_named_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = fake_rclone(tmp, "rclone v1.75.2")
            with patch.dict(os.environ, {"YDM_RCLONE": binary}):
                result = sync_common.rclone_bisync_run("yandex", tmp, os.path.join(tmp, "f"))
            self.assertEqual(result.cmd[0], binary)
            with open(os.path.join(tmp, "calls.log"), encoding="utf-8") as handle:
                self.assertIn("bisync", handle.read())


class TestVersion(unittest.TestCase):
    def setUp(self):
        sync_common._RCLONE_VERSIONS.clear()

    def test_parses_release_and_dev_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sync_common.rclone_version(fake_rclone(tmp, "rclone v1.75.2")), (1, 75, 2))
        sync_common._RCLONE_VERSIONS.clear()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sync_common.rclone_version(fake_rclone(tmp, "rclone v1.60.1-DEV")), (1, 60, 1))

    def test_missing_binary_is_unknown_not_an_error(self):
        self.assertIsNone(sync_common.rclone_version("/nonexistent/rclone"))


class TestResilienceFlags(unittest.TestCase):
    def test_new_enough_rclone_gets_them(self):
        flags = sync_common.bisync_resilience_flags((1, 75, 2))
        self.assertEqual(flags, ["--resilient", "--recover", "--max-lock", "2m"])

    def test_first_version_that_has_them(self):
        self.assertTrue(sync_common.bisync_resilience_flags((1, 66, 0)))

    def test_device_rclone_does_not(self):
        # 1.60 rejects unknown flags: passing them would break every run there.
        self.assertEqual(sync_common.bisync_resilience_flags((1, 60, 1)), [])
        self.assertEqual(sync_common.bisync_resilience_flags((1, 65, 9)), [])

    def test_unknown_version_does_not(self):
        self.assertEqual(sync_common.bisync_resilience_flags(None), [])

    def test_bisync_command_carries_them_only_for_a_new_rclone(self):
        sync_common._RCLONE_VERSIONS.clear()
        with tempfile.TemporaryDirectory() as tmp:
            new = fake_rclone(tmp, "rclone v1.75.2")
            with patch.dict(os.environ, {"YDM_RCLONE": new}):
                cmd = sync_common.rclone_bisync_run("yandex", tmp, os.path.join(tmp, "f")).cmd
            self.assertIn("--resilient", cmd)
        sync_common._RCLONE_VERSIONS.clear()
        with tempfile.TemporaryDirectory() as tmp:
            old = fake_rclone(tmp, "rclone v1.60.1-DEV")
            with patch.dict(os.environ, {"YDM_RCLONE": old}):
                cmd = sync_common.rclone_bisync_run("yandex", tmp, os.path.join(tmp, "f")).cmd
            self.assertNotIn("--resilient", cmd)


if __name__ == "__main__":
    unittest.main()
