import os
import tempfile
import unittest
from unittest import mock
from argparse import Namespace
from tools import sync_policy
from tools.native_export import ExportSafetyError, execute_export
from tools.providers import provider_for
from tools.targets import TargetConfig, context_for


class NativeExportTests(unittest.TestCase):
    def context(self, root, mode="export", *, provider="google_drive",
                inventory_kind="rclone", sync_kind="rclone"):
        return context_for(TargetConfig("synthetic", provider, inventory_kind,
            "synthetic", sync_kind, "synthetic", root, root + "-state", "prod", mode), "cli")

    def test_dry_run_has_exact_export_argv_and_no_writes(self):
        with tempfile.TemporaryDirectory() as root:
            plan = provider_for("google_drive").export_plan(object_id="synthetic-id", mime_type="application/vnd.google-apps.spreadsheet", artifact_root=root)
            result = execute_export(plan, self.context(root))
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["cmd"][:5], [
                "rclone", "backend", "copyid", "synthetic:", "synthetic-id",
            ])
            self.assertIn("--drive-export-formats", result["cmd"])
            self.assertFalse(os.path.exists(plan["artifact_path"]))

    def test_rejects_wrong_target_plan_destination_and_symlinks(self):
        with tempfile.TemporaryDirectory() as root:
            plan = provider_for("google_drive").export_plan(object_id="synthetic-id", mime_type="application/vnd.google-apps.document", artifact_root=root)
            with self.assertRaises(ExportSafetyError): execute_export(plan, self.context(root, "metadata"))
            with self.assertRaises(ExportSafetyError): execute_export(plan, self.context(root, provider="yandex"))
            with self.assertRaises(ExportSafetyError): execute_export(plan, self.context(root, inventory_kind="api"))
            plan["provider"] = "yandex"
            with self.assertRaises(ExportSafetyError): execute_export(plan, self.context(root))
            plan["provider"] = "google_drive"
            plan["format"] = "pdf"
            with self.assertRaises(ExportSafetyError): execute_export(plan, self.context(root))
            plan["format"] = "docx"
            plan["artifact_path"] = "/tmp/outside.docx"
            with self.assertRaises(ExportSafetyError): execute_export(plan, self.context(root))
            outside = tempfile.mkdtemp()
            try:
                os.symlink(outside, os.path.join(root, "escape"))
                plan["artifact_path"] = os.path.join(root, "escape", "file.docx")
                with self.assertRaises(ExportSafetyError): execute_export(plan, self.context(root))
            finally:
                os.rmdir(outside)

    @mock.patch("tools.native_export.subprocess.run")
    def test_apply_uses_only_exact_export_command_and_reports_failure(self, run):
        run.return_value = mock.Mock(returncode=7)
        with tempfile.TemporaryDirectory() as root:
            plan = provider_for("google_drive").export_plan(object_id="synthetic-id", mime_type="application/vnd.google-apps.presentation", artifact_root=root)
            result = execute_export(plan, self.context(root), apply=True)
            self.assertEqual(result["error"], "native export failed")
            self.assertEqual(run.call_args.args[0][:5], [
                "rclone", "backend", "copyid", "synthetic:", "synthetic-id",
            ])
            self.assertNotIn("delete", " ".join(result["cmd"]))

    def test_policy_action_requires_exact_inventory_identity(self):
        with tempfile.TemporaryDirectory() as root:
            context = self.context(root)
            plan = provider_for("google_drive").export_plan(
                object_id="synthetic-id",
                mime_type="application/vnd.google-apps.spreadsheet",
                artifact_root=root,
            )
            policy = sync_policy.empty_policy(
                root, "synthetic", target="synthetic", provider="google_drive",
                inventory_kind="rclone", sync_kind="rclone",
            )
            policy["paths"] = {"docs": {"mode": "download_only", "export_plan": plan}}
            args = Namespace(
                path="/docs", apply=False, target_managed=True, target="synthetic",
                provider="google_drive", inventory_kind="rclone", sync_kind="rclone",
                remote="synthetic", local_root=root, db_path=os.path.join(root, "db"),
                target_context=context,
            )
            native = sync_policy.CloudFile(
                "/docs", "sheet", 0, object_id="synthetic-id",
                mime_type="application/vnd.google-apps.spreadsheet", is_native=True,
            )
            with mock.patch("tools.sync_policy.cloud_files_for_path", return_value=(7, True, [native])), \
                 mock.patch("tools.native_export.subprocess.run") as run:
                result = sync_policy.native_export_action(args, policy)
            self.assertIsNone(result["error"])
            self.assertEqual(result["scan_id"], 7)
            run.assert_not_called()
            wrong = sync_policy.CloudFile(
                "/docs", "sheet", 0, object_id="other-id",
                mime_type="application/vnd.google-apps.spreadsheet", is_native=True,
            )
            with mock.patch("tools.sync_policy.cloud_files_for_path", return_value=(7, True, [wrong])):
                result = sync_policy.native_export_action(args, policy)
            self.assertIn("absent or ambiguous", result["error"])
