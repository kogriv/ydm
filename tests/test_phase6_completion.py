"""Offline completion checks for target-bound download-only flow."""
import argparse
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tools import sync_policy
from tools.sync_common import delete_local_entry_contents


class Phase6CompletionTests(unittest.TestCase):
    def target_file(self, root):
        path = os.path.join(root, "targets.json")
        with open(path, "w") as out:
            json.dump({"schema": "ydm_targets:v1", "default_target": "drive", "targets": {"drive": {
                "provider": "google_drive", "inventory": {"kind": "rclone", "remote": "synthetic"},
                "sync": {"kind": "rclone", "remote": "synthetic"}, "local_root": os.path.join(root, "local"),
                "state_dir": os.path.join(root, "state"), "config_profile": "test"}}}, out)
        return path

    def test_context_derives_all_state_paths_and_sync_is_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(target="drive", targets_file=self.target_file(tmp))
            sync_policy.configure_target_args(args)
            self.assertTrue(args.policy_path.startswith(os.path.join(tmp, "state")))
            self.assertTrue(args.download_filter_path.startswith(os.path.join(tmp, "state")))
            self.assertTrue(args.bisync_filter_path.startswith(os.path.join(tmp, "state")))
            self.assertEqual(args.backend, "rclone")

    def test_target_managed_missing_binding_fails_closed(self):
        args = argparse.Namespace(target_managed=True, target="drive", provider="google_drive",
            remote="synthetic", local_root="/tmp/root", inventory_kind="rclone", sync_kind="rclone")
        self.assertIn("incomplete", sync_policy.validate_policy_binding(args, {"target": "drive"}))

    def test_download_apply_dry_run_spawns_nothing_and_apply_uses_copy_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(target_managed=False, target=None, provider=None, inventory_kind=None,
                sync_kind=None, remote="synthetic", local_root=os.path.join(tmp, "local"),
                db_path=os.path.join(tmp, "missing.db"), policy_path=os.path.join(tmp, "policy"),
                download_filter_path=os.path.join(tmp, "state", "download.filters"),
                bisync_filter_path=os.path.join(tmp, "state", "bisync.filters"), apply=False)
            policy = sync_policy.empty_policy(args.local_root, args.remote)
            policy["paths"] = {"chosen": {"mode": "download_only"}, "off": {"mode": "disabled"}}
            with patch("tools.sync_policy.subprocess.run") as run:
                self.assertTrue(sync_policy.download_apply(args, policy)["dry_run"])
                run.assert_not_called()
                args.apply = True
                run.return_value = argparse.Namespace(returncode=0, stdout="", stderr="")
                result = sync_policy.download_apply(args, policy)
                self.assertEqual(result["command"][:3], ["rclone", "copy", "synthetic:"])
                self.assertNotIn("delete", " ".join(result["command"]))

    def test_explicit_local_delete_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, outside = os.path.join(tmp, "root"), os.path.join(tmp, "outside")
            os.makedirs(root); os.makedirs(outside)
            os.symlink(outside, os.path.join(root, "chosen"))
            self.assertIn("symlink", delete_local_entry_contents(root, "chosen")["reason"])


if __name__ == "__main__":
    unittest.main()
