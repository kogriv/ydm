#!/usr/bin/env python3
"""Unit tests for tools/sync_backends.py backend abstraction."""
from __future__ import annotations

import json
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

from tools import sync_policy  # noqa: E402
from tools.sync_backends import (  # noqa: E402
    BackendError,
    DaemonBackend,
    NotSupportedError,
    RcloneBackend,
    daemon_available,
    daemon_is_active,
    detect_backend,
    rclone_remote_exists,
)


class TestDaemonBackend(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmpdir, "config.cfg")
        self.db_path = os.path.join(self.tmpdir, "monitor.db")
        self.local_root = os.path.join(self.tmpdir, "ya_disk")
        os.makedirs(self.local_root, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_config(self, exclude_dirs=None, extras=None):
        lines = []
        if extras:
            lines.extend(extras)
        if exclude_dirs is not None:
            lines.append(f"exclude-dirs={','.join(exclude_dirs)}\n")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)

    def test_name(self):
        backend = DaemonBackend(
            config_path=self.config_path,
            db_path=self.db_path,
            local_root=self.local_root,
        )
        self.assertEqual(backend.name(), "yandex-disk daemon")

    def test_parse_exclude_dirs(self):
        self._write_config(["Sample", "Books", "video/Матеша"])
        backend = DaemonBackend(
            config_path=self.config_path,
            db_path=self.db_path,
            local_root=self.local_root,
        )
        lines = backend._read_config_lines()
        self.assertEqual(
            backend._parse_exclude_dirs(lines),
            ["Sample", "Books", "video/Матеша"],
        )

    def test_apply_policy_dry_run(self):
        self._write_config(["Sample", "Books"])
        policy = {
            "paths": {
                "Sample": {"mode": "disabled"},
                "Books": {"mode": "bidirectional"},
                "pro": {"mode": "bidirectional"},
            }
        }
        backend = DaemonBackend(
            config_path=self.config_path,
            db_path=self.db_path,
            local_root=self.local_root,
        )
        result = backend.apply_policy(policy, dry_run=True)
        self.assertEqual(result["action"], "apply_policy")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["before"], ["Sample", "Books"])
        self.assertEqual(result["after"], ["Sample"])
        self.assertEqual(result["added"], [])
        self.assertEqual(result["removed"], ["Books"])
        # Config unchanged
        with open(self.config_path, "r", encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("exclude-dirs=Sample,Books", content)

    @patch("tools.sync_backends.stop_start_daemon")
    def test_apply_policy_apply(self, mock_restart):
        mock_restart.return_value = {
            "stop": type("R", (), {"cmd": ["yandex-disk", "stop"], "returncode": 0, "stdout": "", "stderr": ""})(),
            "start": type("R", (), {"cmd": ["yandex-disk", "start"], "returncode": 0, "stdout": "", "stderr": ""})(),
        }
        self._write_config(["Sample", "Books"])
        policy = {
            "paths": {
                "Sample": {"mode": "disabled"},
                "Books": {"mode": "bidirectional"},
            }
        }
        backend = DaemonBackend(
            config_path=self.config_path,
            db_path=self.db_path,
            local_root=self.local_root,
        )
        result = backend.apply_policy(policy, dry_run=False)
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["after"], ["Sample"])
        self.assertIn("restart", result)
        with open(self.config_path, "r", encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("exclude-dirs=Sample", content)
        self.assertNotIn("exclude-dirs=Sample,Books", content)

    def test_apply_policy_download_only_raises(self):
        self._write_config([])
        policy = {"paths": {"video/Матеша": {"mode": "download_only"}}}
        backend = DaemonBackend(
            config_path=self.config_path,
            db_path=self.db_path,
            local_root=self.local_root,
        )
        with self.assertRaises(NotSupportedError) as ctx:
            backend.apply_policy(policy, dry_run=True)
        self.assertIn("download_only is not supported", str(ctx.exception))

    def test_run_resync_not_supported(self):
        backend = DaemonBackend(
            config_path=self.config_path,
            db_path=self.db_path,
            local_root=self.local_root,
        )
        with self.assertRaises(NotSupportedError):
            backend.run_resync(dry_run=True)


class TestRcloneBackend(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "monitor.db")
        self.local_root = os.path.join(self.tmpdir, "ya_disk")
        self.policy_path = os.path.join(self.tmpdir, "sync_policy.json")
        os.makedirs(self.local_root, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_name(self):
        backend = RcloneBackend(
            remote="yandex",
            db_path=self.db_path,
            local_root=self.local_root,
            policy_path=self.policy_path,
        )
        self.assertEqual(backend.name(), "rclone remote:yandex")

    def test_apply_policy_dry_run(self):
        with patch.object(sync_policy, "render_filters", return_value={
            "download_filter_path": f"{self.local_root}.download.filters",
            "bisync_filter_path": f"{self.local_root}.bisync.filters",
            "download_paths": [],
            "bisync_paths": ["Books/Math"],
            "dry_run": True,
        }) as mock_render:
            policy = {"paths": {"Books/Math": {"mode": "bidirectional"}}}
            backend = RcloneBackend(
                remote="yandex",
                db_path=self.db_path,
                local_root=self.local_root,
                policy_path=self.policy_path,
            )
            result = backend.apply_policy(policy, dry_run=True)
            self.assertEqual(result["backend"], "rclone remote:yandex")
            mock_render.assert_called_once()


class TestDetectBackend(unittest.TestCase):
    def test_explicit_daemon(self):
        backend = detect_backend(explicit="daemon")
        self.assertIsInstance(backend, DaemonBackend)

    def test_explicit_rclone(self):
        backend = detect_backend(explicit="rclone", local_root="/tmp/ydm_test")
        self.assertIsInstance(backend, RcloneBackend)

    def test_explicit_env_overrides_config(self):
        backend = detect_backend(explicit="rclone", env="daemon")
        self.assertIsInstance(backend, RcloneBackend)

    def test_env_daemon(self):
        backend = detect_backend(env="daemon")
        self.assertIsInstance(backend, DaemonBackend)

    def test_unknown_backend(self):
        with self.assertRaises(BackendError):
            detect_backend(explicit="ftp")


class TestHelpers(unittest.TestCase):
    def test_rclone_remote_exists_no_rclone(self):
        with patch("tools.sync_backends.shutil.which", return_value=None):
            self.assertFalse(rclone_remote_exists("yandex"))

    def test_daemon_available_no_daemon(self):
        with patch("tools.sync_backends.shutil.which", return_value=None):
            self.assertFalse(daemon_available())

    def test_daemon_is_active_no_daemon(self):
        with patch("tools.sync_backends.shutil.which", return_value=None):
            self.assertFalse(daemon_is_active())


if __name__ == "__main__":
    unittest.main()


class TestDeletionRiskGuard(unittest.TestCase):
    """The daemon deletes cloud folders whose local copy vanished while they
    stay inside its scope — this is what wiped /Books on 2026-08-14."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmpdir, "config.cfg")
        self.db_path = os.path.join(self.tmpdir, "monitor.db")
        self.local_root = os.path.join(self.tmpdir, "ya_disk")
        os.makedirs(self.local_root, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write("dir=\"%s\"\nexclude-dirs=Books\n" % self.local_root)
        self._seed_cloud_snapshot(["Books", "pro", "brtn"])
        self.backend = DaemonBackend(
            config_path=self.config_path,
            db_path=self.db_path,
            local_root=self.local_root,
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed_cloud_snapshot(self, names):
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE files (id INTEGER PRIMARY KEY, scan_id INTEGER, "
            "parent_path TEXT, name TEXT, type TEXT, size INTEGER)"
        )
        conn.executemany(
            "INSERT INTO files (scan_id, parent_path, name, type, size) "
            "VALUES (1, '/', ?, 'dir', 0)",
            [(n,) for n in names],
        )
        conn.commit()
        conn.close()

    def _policy(self, disabled):
        return {"paths": {name: {"mode": "disabled"} for name in disabled}}

    def test_missing_local_copy_of_synced_path_is_flagged(self):
        # 'pro' stays synced (never excluded) but has no local directory.
        os.makedirs(os.path.join(self.local_root, "brtn"), exist_ok=True)
        risky = self.backend.deletion_risk_paths(["Books"], ["Books"])
        self.assertEqual(risky, ["pro"])

    def test_newly_included_path_is_not_flagged(self):
        # 'Books' leaves the exclude list: the daemon downloads it, never deletes.
        os.makedirs(os.path.join(self.local_root, "pro"), exist_ok=True)
        os.makedirs(os.path.join(self.local_root, "brtn"), exist_ok=True)
        self.assertEqual(self.backend.deletion_risk_paths(["Books"], []), [])

    def test_apply_policy_refuses_restart_when_risky(self):
        os.makedirs(os.path.join(self.local_root, "brtn"), exist_ok=True)
        with self.assertRaises(BackendError) as ctx:
            self.backend.apply_policy(self._policy(["Books"]), dry_run=False)
        self.assertIn("/pro", str(ctx.exception))

    def test_apply_policy_dry_run_reports_risk_without_raising(self):
        os.makedirs(os.path.join(self.local_root, "brtn"), exist_ok=True)
        result = self.backend.apply_policy(self._policy(["Books"]), dry_run=True)
        self.assertEqual(result["deletion_risk_paths"], ["pro"])

    def test_apply_policy_proceeds_when_local_copies_present(self):
        for name in ("pro", "brtn"):
            os.makedirs(os.path.join(self.local_root, name), exist_ok=True)
        with patch("tools.sync_backends.stop_start_daemon", return_value={}):
            result = self.backend.apply_policy(self._policy(["Books"]), dry_run=False)
        self.assertEqual(result["deletion_risk_paths"], [])
        self.assertIsNone(result["error"])


class TestBackendKind(unittest.TestCase):
    def test_kinds_are_stable_ids(self):
        self.assertEqual(DaemonBackend.kind, "daemon")
        self.assertEqual(RcloneBackend.kind, "rclone")


class TestNoLocalRoot(unittest.TestCase):
    """Every entry point run with nothing configured, as a stranger would.

    Dropping the hardcoded `/data/ya_disk` default (tasks/opensource 2.3) left
    `None` where the code expects a path, and four tools started dying with
    `TypeError: expected str … not NoneType` deep inside path handling. Nothing
    caught it: the bench always passes `--local-root`, so no test ever ran
    these without one. That is the gap this class closes — the tools are
    exercised the way someone who has just cloned the repository runs them.
    """

    #: Each is a full command; none of them should reach real data, because
    #: none of them should get past the missing path.
    COMMANDS = [
        ["tools/sync_tree.py", "--path", "/"],
        ["tools/sync_filters.py", "list"],
        ["tools/sync_policy.py", "status"],
        ["tools/ydm_menu.py", "orphans"],
    ]

    def run_bare(self, argv):
        import subprocess

        env = {k: v for k, v in os.environ.items() if k != "YDM_LOCAL_ROOT"}
        return subprocess.run(
            [sys.executable, str(ROOT / argv[0]), *argv[1:]],
            capture_output=True, text=True, check=False, env=env, cwd=str(ROOT),
        )

    def test_each_tool_explains_itself_instead_of_crashing(self):
        for argv in self.COMMANDS:
            with self.subTest(tool=argv[0]):
                proc = self.run_bare(argv)
                combined = proc.stdout + proc.stderr
                self.assertNotIn("Traceback", combined, combined[-800:])
                self.assertNotIn("NoneType", combined, combined[-800:])
                self.assertIn("--local-root", combined, combined[-800:])
                self.assertIn("YDM_LOCAL_ROOT", combined, combined[-800:])
                self.assertNotEqual(proc.returncode, 0, combined[-400:])

    def test_the_environment_variable_is_enough(self):
        """The other direction: YDM_LOCAL_ROOT alone must satisfy them.

        tools/aliases.sh sets only that, so if the tools demanded the flag as
        well, every alias would break.
        """
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "YDM_LOCAL_ROOT": tmp}
            proc = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "sync_policy.py"), "status",
                 "--policy-path", os.path.join(tmp, "policy.json")],
                capture_output=True, text=True, check=False, env=env, cwd=str(ROOT),
            )
            combined = proc.stdout + proc.stderr
            self.assertNotIn("no local mirror path", combined, combined[-400:])
            self.assertNotIn("Traceback", combined, combined[-800:])
