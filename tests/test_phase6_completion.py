"""Offline completion checks for target-bound download-only flow."""
import argparse
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tools import sync_policy
from tools import sync_filters
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
                safe_plan = {
                    "schema": "ydm_download_plan:v1", "action": "download-plan",
                    "dry_run": True, "target": None, "provider": None,
                    "remote": args.remote, "local_root": args.local_root,
                    "source": "synthetic:", "destination": args.local_root,
                    "paths": ["chosen"], "filter_path": args.download_filter_path,
                    "manifest_path": args.download_filter_path + ".files-from",
                    "eligible": [{"path": "chosen/file.txt", "object_id": "synthetic", "scan_id": 1}],
                    "excluded": [], "error": None,
                }
                with patch("tools.sync_policy.download_plan", return_value=safe_plan):
                    result = sync_policy.download_apply(args, policy)
                self.assertEqual(result["command"][:3], ["rclone", "copy", "synthetic:"])
                self.assertIn("--files-from-raw", result["command"])
                self.assertNotIn("delete", " ".join(result["command"]))

    def test_explicit_local_delete_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, outside = os.path.join(tmp, "root"), os.path.join(tmp, "outside")
            os.makedirs(root); os.makedirs(outside)
            os.symlink(outside, os.path.join(root, "chosen"))
            self.assertIn("symlink", delete_local_entry_contents(root, "chosen")["reason"])

    def test_explicit_local_delete_rejects_nested_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, outside = os.path.join(tmp, "root"), os.path.join(tmp, "outside")
            os.makedirs(os.path.join(root, "chosen")); os.makedirs(outside)
            os.symlink(outside, os.path.join(root, "chosen", "escape"))
            result = delete_local_entry_contents(root, "chosen")
            self.assertFalse(result["deleted"])
            self.assertIn("nested symlink", result["reason"])

    def test_legacy_filter_editor_refuses_configured_targets_before_io(self):
        args = argparse.Namespace(
            command="add", path="/chosen", format="json", text_header=False,
        )
        with patch("tools.sync_filters.parse_args", return_value=args), \
             patch("tools.sync_filters.discover_targets_file", return_value="/synthetic/targets.json"), \
             patch("tools.sync_filters.require_local_root") as require_root, \
             patch("tools.sync_filters.render") as render:
            sync_filters.main()
        require_root.assert_not_called()
        self.assertIn("target-managed", render.call_args.args[0].error)

    def test_target_managed_download_filters_are_never_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                target_managed=True, target="drive", provider="google_drive",
                inventory_kind="rclone", sync_kind="rclone", remote="synthetic",
                local_root=os.path.join(tmp, "local"), db_path=os.path.join(tmp, "db"),
                policy_path=os.path.join(tmp, "policy.json"),
                download_filter_path=os.path.join(tmp, "download.filters"),
                bisync_filter_path=os.path.join(tmp, "bisync.filters"), apply=True,
            )
            policy = sync_policy.empty_policy(
                args.local_root, args.remote, target=args.target, provider=args.provider,
                inventory_kind=args.inventory_kind, sync_kind=args.sync_kind,
            )
            policy["paths"] = {"chosen": {"mode": "download_only"}}
            result = sync_policy.render_filters(args, policy)
            self.assertIn("exact inventory manifest", result["error"])
            self.assertFalse(os.path.exists(args.download_filter_path))
            self.assertFalse(os.path.exists(args.bisync_filter_path))

    def test_render_error_and_target_edit_do_not_reach_backend(self):
        policy = {"schema": sync_policy.SCHEMA, "paths": {}}
        base = argparse.Namespace(
            command="render-filters", apply=True, format="json", text_header=False,
            local_root="/tmp/local", policy_path="/tmp/policy", target_managed=True,
            target="drive",
        )
        with patch("tools.sync_policy.parse_args", return_value=base), \
             patch("tools.sync_policy.configure_target_args"), \
             patch("tools.sync_policy.require_local_root", return_value=base.local_root), \
             patch("tools.sync_policy.load_policy", return_value=policy), \
             patch("tools.sync_policy.render_filters", return_value={"error": "blocked"}), \
             patch("tools.sync_policy._apply_backend_policy") as backend, \
             patch("tools.sync_policy.render"):
            sync_policy.main()
        backend.assert_not_called()

        add_args = argparse.Namespace(**vars(base))
        add_args.command = "add"
        with patch("tools.sync_policy.parse_args", return_value=add_args), \
             patch("tools.sync_policy.configure_target_args"), \
             patch("tools.sync_policy.require_local_root", return_value=add_args.local_root), \
             patch("tools.sync_policy.add_policy_path", return_value={"error": None}), \
             patch("tools.sync_policy._apply_backend_policy") as backend, \
             patch("tools.sync_policy.render") as render:
            sync_policy.main()
        backend.assert_not_called()
        self.assertTrue(render.call_args.args[0]["backend_apply"]["skipped"])


if __name__ == "__main__":
    unittest.main()
