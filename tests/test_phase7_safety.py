"""Offline safety checks for target-managed bidirectional preparation."""
import argparse
import json
import os
import tempfile
import unittest
from unittest import mock

from tools import sync_backends, sync_bisync, sync_policy, sync_rename
from tools.providers import provider_for
from tools.targets import TargetConfigError, load_targets_file


class Phase7SafetyTests(unittest.TestCase):
    def setUp(self):
        self._bisync_daemon = mock.patch(
            "tools.sync_bisync.daemon_conflict_error", return_value=None)
        self._bisync_daemon.start()

    def tearDown(self):
        self._bisync_daemon.stop()

    def _args(self, root, filter_path):
        return argparse.Namespace(
            target_managed=True, target="drive", provider="google_drive",
            inventory_kind="rclone", sync_kind="rclone", remote="synthetic",
            local_root=root, db_path=os.path.join(root, "inventory.db"),
            filter_path=filter_path,
        )

    def _policy(self, root):
        policy = sync_policy.empty_policy(
            root, "synthetic", target="drive", provider="google_drive",
            inventory_kind="rclone", sync_kind="rclone",
        )
        policy["paths"] = {"chosen": {"mode": "bidirectional"}}
        return policy

    def test_execution_gate_checks_marker_scope_inventory_and_object_types(self):
        with tempfile.TemporaryDirectory() as root:
            filter_path = os.path.join(root, "bisync.filters")
            with open(filter_path, "w") as handle:
                handle.write("+ /chosen/**\n+ /RCLONE_TEST\n- **\n")
            args, policy = self._args(root, filter_path), self._policy(root)
            ordinary = sync_policy.CloudFile("/chosen", "ok.bin", 1, object_id="ordinary")
            with mock.patch("tools.sync_bisync.cloud_files_for_path", return_value=(7, True, [ordinary])), \
                 mock.patch("tools.sync_bisync._inventory_complete", return_value=True):
                self.assertIn("access marker", sync_bisync.bidirectional_eligibility_error(args, policy))
                with open(os.path.join(root, sync_bisync.CHECK_ACCESS_FILENAME), "w") as handle:
                    handle.write("sentinel")
                self.assertIsNone(sync_bisync.bidirectional_eligibility_error(args, policy))

                unsafe = [
                    sync_policy.CloudFile("/chosen", "native", 0, is_native=True),
                    sync_policy.CloudFile("/chosen", "shortcut", 0, is_shortcut=True),
                    sync_policy.CloudFile("/chosen", "bad:name", 1),
                ]
                for item in unsafe:
                    with self.subTest(item=item.name), \
                         mock.patch("tools.sync_bisync.cloud_files_for_path", return_value=(7, True, [item])):
                        self.assertIsNotNone(sync_bisync.bidirectional_eligibility_error(args, policy))

                collision = [
                    sync_policy.CloudFile("/chosen", "same", 1, object_id="a"),
                    sync_policy.CloudFile("/chosen", "same", 1, object_id="b"),
                ]
                with mock.patch("tools.sync_bisync.cloud_files_for_path", return_value=(7, True, collision)):
                    self.assertIn("collision", sync_bisync.bidirectional_eligibility_error(args, policy))

            with open(filter_path, "w") as handle:
                handle.write("+ /other/**\n- **\n")
            self.assertIn("scope", sync_bisync.bidirectional_eligibility_error(args, policy))

    def test_incomplete_inventory_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            marker = os.path.join(root, sync_bisync.CHECK_ACCESS_FILENAME)
            open(marker, "w").close()
            filter_path = os.path.join(root, "bisync.filters")
            with open(filter_path, "w") as handle:
                handle.write("+ /chosen/**\n- **\n")
            args, policy = self._args(root, filter_path), self._policy(root)
            with mock.patch("tools.sync_bisync.cloud_files_for_path", return_value=(7, True, [])), \
                 mock.patch("tools.sync_bisync._inventory_complete", return_value=False):
                self.assertIn("incomplete", sync_bisync.bidirectional_eligibility_error(args, policy))

    def test_runtime_files_are_target_scoped(self):
        first = argparse.Namespace(
            target_managed=True, bisync_state_path="/state/a/state.json",
            bisync_log_path="/state/a/log", bisync_last_log_path="/state/a/last",
            bisync_lock_path="/state/a/locks/bisync.lock",
        )
        second = argparse.Namespace(
            target_managed=True, bisync_state_path="/state/b/state.json",
            bisync_log_path="/state/b/log", bisync_last_log_path="/state/b/last",
            bisync_lock_path="/state/b/locks/bisync.lock",
        )
        self.assertTrue(all(path.startswith("/state/a/") for path in sync_bisync._runtime_paths(first)))
        self.assertTrue(all(path.startswith("/state/b/") for path in sync_bisync._runtime_paths(second)))
        self.assertNotEqual(sync_bisync._runtime_paths(first), sync_bisync._runtime_paths(second))

    def test_same_target_lock_refuses_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "state", "locks", "bisync.lock")
            os.makedirs(os.path.dirname(lock_path))
            first = sync_bisync.acquire_lock(lock_path)
            try:
                second = sync_bisync.acquire_lock(lock_path)
                self.assertTrue(first.acquired)
                self.assertFalse(second.acquired)
                self.assertEqual(second.holder_pid, os.getpid())
            finally:
                sync_bisync.release_lock(lock_path)

    def test_active_daemon_only_blocks_overlapping_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon_root = os.path.join(tmp, "yandex")
            google_root = os.path.join(tmp, "google")
            os.makedirs(daemon_root)
            os.makedirs(google_root)
            config = os.path.join(tmp, "config.cfg")
            with open(config, "w") as handle:
                handle.write(f"dir={daemon_root}\n")
            with mock.patch("tools.sync_backends.daemon_is_active", return_value=True):
                self.assertIsNone(sync_backends.daemon_conflict_error(google_root, config))
                self.assertIn("overlapping", sync_backends.daemon_conflict_error(
                    os.path.join(daemon_root, "nested"), config))
                self.assertIn("cannot be verified", sync_backends.daemon_conflict_error(
                    google_root, os.path.join(tmp, "missing.cfg")))

    def test_google_fast_rename_is_blocked_by_provider_capability(self):
        capabilities = provider_for("google_drive").capabilities
        self.assertTrue(capabilities.bidirectional_sync)
        self.assertFalse(capabilities.server_side_move)
        args = argparse.Namespace(
            command="plan", old="/chosen/old", new="/chosen/new",
            local_root="/tmp/synthetic", remote="synthetic",
            policy_path="/tmp/synthetic-policy", bisync_filter_path="/tmp/synthetic-filter",
            followup="resync", target_managed=True, provider="google_drive",
        )
        with mock.patch("tools.sync_rename.remote_exists") as remote:
            result = sync_rename.preflight(args)
        self.assertIn("does not support", result["error"])
        remote.assert_not_called()

    def test_same_nested_and_symlinked_local_roots_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            os.makedirs(real)
            alias = os.path.join(tmp, "alias")
            os.symlink(real, alias)
            for right_root in (real, os.path.join(real, "nested"), alias):
                with self.subTest(root=right_root):
                    path = os.path.join(tmp, "targets.json")
                    payload = {
                        "schema": "ydm_targets:v1", "default_target": "a",
                        "targets": {
                            "a": {"provider": "google_drive", "inventory": {"kind": "rclone", "remote": "a"},
                                  "sync": {"kind": "rclone", "remote": "a"}, "local_root": real,
                                  "state_dir": os.path.join(tmp, "state-a"), "config_profile": "test"},
                            "b": {"provider": "google_drive", "inventory": {"kind": "rclone", "remote": "b"},
                                  "sync": {"kind": "rclone", "remote": "b"}, "local_root": right_root,
                                  "state_dir": os.path.join(tmp, "state-b"), "config_profile": "test"},
                        },
                    }
                    with open(path, "w") as handle:
                        json.dump(payload, handle)
                    with self.assertRaises(TargetConfigError):
                        load_targets_file(path)


if __name__ == "__main__":
    unittest.main()
