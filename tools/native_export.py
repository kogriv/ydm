"""Guarded execution of one validated Google native export plan."""
from __future__ import annotations
import os
import re
import subprocess
from typing import Mapping
from tools.sync_policy import validate_export_plan
from tools.providers import provider_for


class ExportSafetyError(ValueError):
    pass


def _inside(root: str, path: str) -> bool:
    root = os.path.realpath(root)
    parent = os.path.realpath(os.path.dirname(path))
    return os.path.commonpath((root, parent)) == root


def _has_symlink_component(root: str, path: str) -> bool:
    root = os.path.abspath(root)
    relative = os.path.relpath(path, root)
    current = root
    if os.path.islink(current):
        return True
    for part in relative.split(os.sep)[:-1]:
        current = os.path.join(current, part)
        if os.path.islink(current):
            return True
    return False


def export_command(plan: Mapping[str, object], context) -> list[str]:
    error = validate_export_plan(plan)
    if error:
        raise ExportSafetyError(error)
    cfg = context.config
    if cfg.provider != "google_drive" or cfg.native_docs != "export":
        raise ExportSafetyError("native export requires a target-managed Google Drive export target")
    if cfg.inventory_kind != "rclone" or cfg.sync_kind != "rclone":
        raise ExportSafetyError("native export requires rclone inventory and sync backends")
    if cfg.inventory_remote != cfg.sync_remote or not cfg.inventory_remote:
        raise ExportSafetyError("target inventory and sync remote must match for native export")
    if plan.get("provider") != "google_drive":
        raise ExportSafetyError("native export plan provider must be google_drive")
    object_id = str(plan["source_object_id"])
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", object_id):
        raise ExportSafetyError("native export source object ID has unsafe characters")
    try:
        expected = provider_for("google_drive").export_plan(
            object_id=object_id,
            mime_type=str(plan["source_mime_type"]),
            artifact_root=cfg.local_root,
        )
    except ValueError as exc:
        raise ExportSafetyError(str(exc)) from exc
    if (plan.get("format") != expected["format"]
            or plan.get("export_mime_type") != expected["export_mime_type"]):
        raise ExportSafetyError("native export format does not match source MIME type")
    artifact = str(plan["artifact_path"])
    if not os.path.isabs(artifact) or ".." in os.path.normpath(artifact).split(os.sep):
        raise ExportSafetyError("artifact path must be an absolute path without traversal")
    if not _inside(cfg.local_root, artifact):
        raise ExportSafetyError("artifact path must remain inside canonical local root")
    if _has_symlink_component(cfg.local_root, artifact):
        raise ExportSafetyError("artifact path must not traverse symlinks")
    if not os.path.isdir(os.path.dirname(artifact)):
        raise ExportSafetyError("artifact parent directory must already exist")
    if os.path.lexists(artifact):
        raise ExportSafetyError("artifact destination already exists")
    return ["rclone", "backend", "copyid", f"{cfg.inventory_remote}:",
            object_id, artifact, "--drive-export-formats", str(plan["format"])]


def execute_export(plan: Mapping[str, object], context, *, apply: bool = False) -> dict:
    cmd = export_command(plan, context)
    result = {"schema": "ydm_native_export_result:v1", "dry_run": not apply,
              "cmd": cmd, "deleted": False, "error": None}
    if not apply:
        return result
    # Revalidate immediately before spawning. Parent creation is deliberately
    # not implicit: it would open a symlink race outside the bound local root.
    cmd = export_command(plan, context)
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    result.update({"returncode": proc.returncode,
                   "error": None if proc.returncode == 0 else "native export failed"})
    return result
