#!/usr/bin/env python3
"""Tests for YDM menu."""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.ydm_menu_config import MenuConfig  # noqa: E402
from tools.ydm_menu_prompts import (  # noqa: E402
    prompt_int,
    prompt_ints,
    prompt_yes_no,
)
from tools.ydm_menu_status import load_status  # noqa: E402


class MenuPromptTests(unittest.TestCase):
    def test_prompt_int_default(self):
        self.assertEqual(prompt_int("x", default=4, reader=lambda _: ""), 4)

    def test_prompt_ints_all(self):
        self.assertEqual(prompt_ints("x", max_n=3, reader=lambda _: "all"), [1, 2, 3])

    def test_prompt_yes_no(self):
        self.assertTrue(prompt_yes_no("x", default=True, reader=lambda _: ""))


class MenuConfigTests(unittest.TestCase):
    def test_from_env(self):
        cfg = MenuConfig.from_env_and_args(
            db_path=str(ROOT / "monitor.db"),
            local_root="/tmp/ydm-test",
            policy_path=str(ROOT / "var/sync_policy.json"),
        )
        self.assertTrue(cfg.db_path.endswith("monitor.db"))
        self.assertEqual(cfg.local_root, "/tmp/ydm-test")

    def test_no_backend_available_is_reported_not_raised(self):
        """A host with neither the daemon nor an rclone remote (CI runners,
        a fresh checkout) must still get a menu that explains itself."""
        from tools.sync_backends import BackendError
        from tools.ydm_menu_screens import render_header

        with patch(
            "tools.ydm_menu_config.detect_backend",
            side_effect=BackendError("No sync backend available: ..."),
        ):
            cfg = MenuConfig.from_env_and_args(local_root="/tmp/ydm-test")
        self.assertEqual(cfg.backend_kind, "")
        self.assertEqual(cfg.backend_name, "none available")
        self.assertIn("No sync backend available", cfg.backend_error)
        header = "\n".join(render_header(cfg, load_status(cfg)))
        self.assertIn("Backend: none available", header)
        self.assertIn("No sync backend available", header)


class MenuScopeTests(unittest.TestCase):
    def test_bisync_scope_lines(self):
        from tools.ydm_menu_actions import bisync_scope_lines

        tmpdir = tempfile.mkdtemp()
        filter_path = os.path.join(tmpdir, "test.bisync.filters")
        with open(filter_path, "w", encoding="utf-8") as handle:
            handle.write("+ /Books/Math/**\n- **\n")
        try:
            cfg = MenuConfig.from_env_and_args(
                local_root="/sdcard/Download/ya_disk",
                bisync_filter_path=filter_path,
            )
            lines = bisync_scope_lines(cfg)
            text = "\n".join(lines)
            self.assertIn("NOT the whole disk", text)
            self.assertIn("+ /Books/Math/", text)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class MenuStatusTests(unittest.TestCase):
    def test_load_status_smoke(self):
        cfg = MenuConfig.from_env_and_args(
            db_path=str(ROOT / "monitor.db"),
            local_root="/sdcard/Download/ya_disk",
            policy_path=str(ROOT / "var/sync_policy.json"),
            bisync_filter_path="/sdcard/Download/ya_disk.bisync.filters",
        )
        status = load_status(cfg)
        self.assertIn(status.overall, {"OK", "NEEDS RESYNC", "CHECK", "BUSY (pid None)"})


class MenuCliTests(unittest.TestCase):
    def test_help(self):
        import subprocess

        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools/ydm_menu.py"), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("orphans", proc.stdout)

    def test_orphans_json(self):
        import subprocess

        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools/ydm_menu.py"),
                "--db-path",
                str(ROOT / "monitor.db"),
                "--local-root",
                "/sdcard/Download/ya_disk",
                "--policy-path",
                str(ROOT / "var/sync_policy.json"),
                "orphans",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ydm_menu_orphans:v1", proc.stdout)


if __name__ == "__main__":
    unittest.main()
