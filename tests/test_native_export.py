import os
import tempfile
import unittest
from unittest import mock
from tools.native_export import ExportSafetyError, execute_export
from tools.providers import provider_for
from tools.targets import TargetConfig, context_for


class NativeExportTests(unittest.TestCase):
    def context(self, root, mode="export"):
        return context_for(TargetConfig("synthetic", "google_drive", "rclone", "synthetic", "rclone", "synthetic", root, root + "-state", "prod", mode), "cli")
    def test_dry_run_has_exact_export_argv_and_no_writes(self):
        with tempfile.TemporaryDirectory() as root:
            plan = provider_for("google_drive").export_plan(object_id="synthetic-id", mime_type="application/vnd.google-apps.spreadsheet", artifact_root=root)
            result = execute_export(plan, self.context(root))
            self.assertTrue(result["dry_run"])
            self.assertIn("copyto", result["cmd"])
            self.assertIn("--drive-export-formats", result["cmd"])
            self.assertFalse(os.path.exists(plan["artifact_path"]))
    def test_rejects_wrong_mode_and_escape(self):
        with tempfile.TemporaryDirectory() as root:
            plan = provider_for("google_drive").export_plan(object_id="synthetic-id", mime_type="application/vnd.google-apps.document", artifact_root=root)
            with self.assertRaises(ExportSafetyError): execute_export(plan, self.context(root, "metadata"))
            plan["artifact_path"] = "/tmp/outside.docx"
            with self.assertRaises(ExportSafetyError): execute_export(plan, self.context(root))
    @mock.patch("tools.native_export.subprocess.run")
    def test_apply_uses_only_exact_export_command(self, run):
        run.return_value = mock.Mock(returncode=0)
        with tempfile.TemporaryDirectory() as root:
            plan = provider_for("google_drive").export_plan(object_id="synthetic-id", mime_type="application/vnd.google-apps.presentation", artifact_root=root)
            result = execute_export(plan, self.context(root), apply=True)
            self.assertIsNone(result["error"])
            self.assertNotIn("delete", " ".join(result["cmd"]))

