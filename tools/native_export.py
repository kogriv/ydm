"""Guarded execution of one validated Google native export plan."""
from __future__ import annotations
import os
import subprocess
from typing import Mapping
from tools.sync_policy import validate_export_plan


class ExportSafetyError(ValueError):
    pass


def _inside(root: str, path: str) -> bool:
    root = os.path.realpath(root)
    parent = os.path.realpath(os.path.dirname(path))
    return os.path.commonpath((root, parent)) == root


def export_command(plan: Mapping[str, object], context) -> list[str]:
    error = validate_export_plan(plan)
    if error:
        raise ExportSafetyError(error)
    cfg = context.config
    if cfg.provider != "google_drive" or cfg.native_docs != "export":
        raise ExportSafetyError("native export requires a target-managed Google Drive export target")
    if cfg.inventory_remote != cfg.sync_remote or not cfg.inventory_remote:
        raise ExportSafetyError("target inventory and sync remote must match for native export")
    artifact = str(plan["artifact_path"])
    if not os.path.isabs(artifact) or ".." in os.path.normpath(artifact).split(os.sep):
        raise ExportSafetyError("artifact path must be an absolute path without traversal")
    if not _inside(cfg.local_root, artifact):
        raise ExportSafetyError("artifact path must remain inside canonical local root")
    if os.path.lexists(artifact):
        raise ExportSafetyError("artifact destination already exists")
    source = f"{cfg.inventory_remote}:{plan['source_object_id']}"
    return ["rclone", "copyto", source, artifact, "--drive-export-formats", str(plan["format"])]


def execute_export(plan: Mapping[str, object], context, *, apply: bool = False) -> dict:
    cmd = export_command(plan, context)
    result = {"schema": "ydm_native_export_result:v1", "dry_run": not apply,
              "cmd": cmd, "deleted": False, "error": None}
    if not apply:
        return result
    # Only create the already-validated parent at execution time; this is not
    # generic copy/bisync and contains no delete flags.
    os.makedirs(os.path.dirname(str(plan["artifact_path"])), exist_ok=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    result.update({"returncode": proc.returncode,
                   "error": None if proc.returncode == 0 else "native export failed"})
    return result
