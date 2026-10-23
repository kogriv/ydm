"""Synthetic, offline tests for Phase 1 target configuration."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.targets import TargetConfigError, load_targets_file, resolve_target  # noqa: E402


def fixture(state_base):
    return {
        "schema": "ydm_targets:v1",
        "default_target": "synthetic-yandex",
        "targets": {
            "synthetic-yandex": {
                "provider": "yandex",
                "inventory": {"kind": "yandex_api"},
                "sync": {"kind": "daemon"},
                "local_root": str(state_base / "mirror-yandex"),
                "state_dir": str(state_base / "state-yandex"),
                "config_profile": "prod",
            },
            "synthetic-drive": {
                "provider": "google_drive",
                "inventory": {"kind": "rclone", "remote": "synthetic-drive"},
                "sync": {"kind": "rclone", "remote": "synthetic-drive"},
                "local_root": str(state_base / "mirror-drive"),
                "state_dir": str(state_base / "state-drive"),
                "config_profile": "test",
                "native_docs": "metadata",
            },
        },
    }


class TargetResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.path = self.base / "targets.json"
        self.path.write_text(json.dumps(fixture(self.base)), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolution_precedence_cli_then_env_then_default(self):
        env = {"YDM_TARGET": "synthetic-drive"}
        self.assertEqual(resolve_target(targets_path=str(self.path), environ=env).name, "synthetic-drive")
        self.assertEqual(
            resolve_target(cli_target="synthetic-yandex", targets_path=str(self.path), environ=env).name,
            "synthetic-yandex",
        )
        self.assertEqual(resolve_target(targets_path=str(self.path), environ={}).name, "synthetic-yandex")

    def test_legacy_without_file_is_none_and_preserves_callers_defaults(self):
        self.assertIsNone(resolve_target(targets_path=None, environ={}))

    def test_target_paths_are_all_scoped_under_its_state_directory(self):
        yandex = resolve_target(cli_target="synthetic-yandex", targets_path=str(self.path), environ={})
        drive = resolve_target(cli_target="synthetic-drive", targets_path=str(self.path), environ={})
        self.assertNotEqual(yandex.state_dir, drive.state_dir)
        y_paths = (yandex.db_path, yandex.policy_path, yandex.download_filter_path,
                   yandex.bisync_filter_path, yandex.bisync_state_path,
                   yandex.bisync_log_path, yandex.rename_state_path, yandex.lock_path)
        d_paths = (drive.db_path, drive.policy_path, drive.download_filter_path,
                   drive.bisync_filter_path, drive.bisync_state_path,
                   drive.bisync_log_path, drive.rename_state_path, drive.lock_path)
        for path in y_paths:
            self.assertTrue(path.startswith(yandex.state_dir + os.sep))
        for path in d_paths:
            self.assertTrue(path.startswith(drive.state_dir + os.sep))
        self.assertTrue(set(y_paths).isdisjoint(d_paths))

    def test_unknown_and_missing_keys_are_path_specific(self):
        data = fixture(self.base)
        del data["targets"]["synthetic-yandex"]["state_dir"]
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(TargetConfigError, r"\$\.targets\.synthetic-yandex\.state_dir: missing"):
            load_targets_file(str(self.path))
        data = fixture(self.base)
        data["targets"]["synthetic-drive"]["unexpected"] = True
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(TargetConfigError, r"\$\.targets\.synthetic-drive\.unexpected: unknown"):
            load_targets_file(str(self.path))

    def test_rclone_requires_remote_and_native_docs_is_checked(self):
        data = fixture(self.base)
        del data["targets"]["synthetic-drive"]["inventory"]["remote"]
        data["targets"]["synthetic-drive"]["native_docs"] = "unsafe"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(TargetConfigError, r"inventory\.remote"):
            load_targets_file(str(self.path))


class TargetCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.path = self.base / "targets.json"
        self.path.write_text(json.dumps(fixture(self.base)), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "ydm.py"), "--format", "json", "--targets-file", str(self.path), *args],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )

    def test_list_show_and_validate_are_offline_and_do_not_create_state(self):
        for args in (("targets", "list"), ("targets", "show", "synthetic-drive"), ("targets", "validate")):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["success"])
        self.assertFalse((self.base / "state-yandex").exists())
        self.assertFalse((self.base / "state-drive").exists())

    def test_target_selects_derived_db_and_inventory_backend(self):
        result = self.run_cli("--target", "synthetic-drive", "scan", "local", "--path", str(self.base / "missing"))
        self.assertNotEqual(result.returncode, 0)
        # The command made only the selected target state directory, proving
        # no other target state is selected. (The full legacy suite may have
        # its own ignored monitor.db in the repository root.)
        self.assertTrue((self.base / "state-drive").is_dir())
        self.assertFalse((self.base / "state-yandex").exists())


if __name__ == "__main__":
    unittest.main()
