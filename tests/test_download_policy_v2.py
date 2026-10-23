"""Synthetic Phase 6 binding and download-plan tests; no remote commands."""
import argparse
import os
import tempfile
import unittest

from tools import sync_policy


class DownloadPolicyV2Tests(unittest.TestCase):
    def test_binding_mismatch_blocks_filter_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(target="synthetic-b", provider="google_drive", remote="synthetic-b",
                local_root=os.path.join(tmp, "local-b"), db_path=os.path.join(tmp, "db"), apply=True,
                policy_path=os.path.join(tmp, "policy"), download_filter_path=os.path.join(tmp, "download"),
                bisync_filter_path=os.path.join(tmp, "bisync"))
            policy = sync_policy.empty_policy(os.path.join(tmp, "local-a"), "synthetic-a",
                                              target="synthetic-a", provider="google_drive")
            policy["paths"] = {"ordinary": {"mode": "download_only"}}
            result = sync_policy.render_filters(args, policy)
            self.assertIn("mismatch", result["error"])
            self.assertFalse(os.path.exists(args.download_filter_path))

    def test_exact_dry_run_download_plan_is_target_scoped(self):
        args = argparse.Namespace(target="synthetic", provider="google_drive", remote="synthetic",
            local_root="/tmp/synthetic-local", db_path="/tmp/synthetic-db", apply=False,
            download_filter_path="/tmp/synthetic-state/download.filters")
        policy = sync_policy.empty_policy(args.local_root, args.remote, target=args.target, provider=args.provider)
        policy["paths"] = {"keep": {"mode": "download_only"}, "off": {"mode": "disabled"}}
        plan = sync_policy.download_plan(args, policy)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["paths"], ["keep"])
        self.assertEqual(plan["source"], "synthetic:")
        self.assertIsNone(plan["error"])

    def test_remove_never_deletes_local_without_explicit_future_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, "local")
            os.makedirs(local)
            sentinel = os.path.join(local, "keep.txt")
            open(sentinel, "w").close()
            policy_path = os.path.join(tmp, "policy.json")
            policy = sync_policy.empty_policy(local, "synthetic")
            policy["paths"] = {"keep": {"mode": "download_only"}}
            sync_policy.write_policy(policy_path, policy)
            result = sync_policy.remove_policy_path(argparse.Namespace(path="/keep", apply=True, policy_path=policy_path))
            self.assertIsNone(result["error"])
            self.assertTrue(os.path.exists(sentinel))


if __name__ == "__main__":
    unittest.main()
