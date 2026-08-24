#!/usr/bin/env python3
"""
Sync management for --backend rclone (Этап 4, tasks/rclone_backend/README.md).

Unlike tools/sync_exclude.py (yandex-disk daemon, exclude-dirs blacklist,
config.cfg), this operates on an rclone filter-file (whitelist: only
explicitly `+`-included top-level paths are synced) and materializes/removes
the local mirror directly via `rclone copy`/`rclone check`, since there is
no daemon to restart.

No interactive prompts — dry-run by default, `--apply` to write the
filter-file and materialize, `--delete-local` (only with `--apply`, and only
after a clean `rclone check`) to actually remove the local copy on `remove`.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.sync_common import (  # noqa: E402
    build_composite_snapshot,
    create_storage,
    default_filter_path,
    delete_local_entry_contents,
    load_sync_filters,
    normalize_path,
    path_exists_in_snapshot,
    rclone_check_entry,
    rclone_copy_materialize,
    require_local_root,
    var_path,
    write_sync_filters,
)
from ydm import Analyzer, DEFAULT_CONFIG  # noqa: E402


@dataclass
class ActionResult:
    action: str
    path: str
    dry_run: bool
    before: List[str]
    after: List[str]
    warnings: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    plan_text: List[str] = field(default_factory=list)
    filter_path: Optional[str] = None
    snapshot_base_scan_id: Optional[int] = None
    materialize: Optional[dict] = None
    check: Optional[dict] = None
    local_delete: Optional[dict] = None
    error: Optional[str] = None


def normalize_entry(path: str) -> str:
    normalized = normalize_path(path)
    if normalized == "/":
        return ""
    return normalized[1:]


def is_covered(entry: str, include_dirs: List[str]) -> Optional[str]:
    """Returns the include_dirs entry covering `entry` (itself or an
    ancestor), or None if `entry` isn't synced at all."""
    if entry in include_dirs:
        return entry
    for candidate in include_dirs:
        if entry.startswith(candidate + "/"):
            return candidate
    return None


def compute_add(path: str, include_dirs: List[str]) -> ActionResult:
    entry = normalize_entry(path)
    before = sorted(set(include_dirs))
    if entry == "":
        return ActionResult(action="add", path="/", dry_run=True, before=before, after=before,
                             error="Cannot add root path")

    covering = is_covered(entry, before)
    if covering is not None:
        note = [] if covering == entry else [f"Already included via '{covering}/**'"]
        return ActionResult(action="add", path=f"/{entry}", dry_run=True, before=before, after=before,
                             warnings=note, plan_text=["Nothing to do — already synced"])

    subsumed = [d for d in before if d.startswith(entry + "/")]
    after = sorted((set(before) - set(subsumed)) | {entry})
    plan = [f"Add to filter-file: + /{entry}/**"]
    if subsumed:
        plan.append(f"Remove now-redundant entries covered by '{entry}': {', '.join(subsumed)}")
    return ActionResult(action="add", path=f"/{entry}", dry_run=True, before=before, after=after,
                         added=[entry], removed=subsumed, plan_text=plan)


def compute_remove(path: str, include_dirs: List[str]) -> ActionResult:
    entry = normalize_entry(path)
    before = sorted(set(include_dirs))
    if entry == "":
        return ActionResult(action="remove", path="/", dry_run=True, before=before, after=before,
                             error="Cannot remove root path")

    if entry not in before:
        covering = is_covered(entry, before)
        if covering is not None:
            error = (
                f"'{entry}' is only synced via ancestor rule '{covering}/**' — "
                f"removing a subfolder of an included tree isn't supported "
                f"(mirrors the exclude-dirs sibling problem, inverted). "
                f"Remove '{covering}' entirely, or re-add the siblings you want to keep."
            )
        else:
            error = f"'{entry}' is not currently included"
        return ActionResult(action="remove", path=f"/{entry}", dry_run=True, before=before, after=before,
                             error=error)

    after = sorted(set(before) - {entry})
    plan = [f"Remove from filter-file: + /{entry}/**"]
    return ActionResult(action="remove", path=f"/{entry}", dry_run=True, before=before, after=after,
                         removed=[entry], plan_text=plan)


def render(result: ActionResult, fmt: str, text_header: bool) -> None:
    payload = {
        "schema": "sync_filters:v1",
        "action": result.action,
        "path": result.path,
        "dry_run": result.dry_run,
        "before": result.before,
        "after": result.after,
        "warnings": result.warnings,
        "added": result.added,
        "removed": result.removed,
        "plan_text": result.plan_text,
        "filter_path": result.filter_path,
        "snapshot_base_scan_id": result.snapshot_base_scan_id,
        "materialize": result.materialize,
        "check": result.check,
        "local_delete": result.local_delete,
        "error": result.error,
    }
    if fmt == "json":
        success = result.error is None
        print(json.dumps({"success": success, "data": payload}, ensure_ascii=False, indent=2))
        return

    if text_header:
        print("schema: sync_filters:v1")
        if result.error:
            print(f"error: {result.error}")
        print(f"action: {result.action}")
        print(f"path: {result.path}")
        print(f"dry_run: {result.dry_run}")
        if result.filter_path:
            print(f"filter_path: {result.filter_path}")
        if result.snapshot_base_scan_id is not None:
            print(f"snapshot_base_scan_id: {result.snapshot_base_scan_id}")
        if result.warnings:
            print("warnings:")
            for warning in result.warnings:
                print(f"- {warning}")
        print("")

    if result.error and not text_header:
        print(f"ERROR: {result.error}")
    print(f"Before: {', '.join(result.before)}")
    print(f"After: {', '.join(result.after)}")
    if result.added:
        print(f"Added: {', '.join(result.added)}")
    if result.removed:
        print(f"Removed: {', '.join(result.removed)}")
    if result.plan_text:
        print("Plan:")
        for line in result.plan_text:
            print(f"- {line}")
    if result.materialize is not None:
        print(f"rclone copy: returncode={result.materialize['returncode']}")
        if result.materialize["stderr"]:
            print(result.materialize["stderr"])
    if result.check is not None:
        print(f"rclone check: returncode={result.check['returncode']}")
        if result.check["stderr"]:
            print(result.check["stderr"])
    if result.local_delete is not None:
        print(f"local delete: {result.local_delete}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync management for --backend rclone (filter-file based)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common_flags(sub):
        sub.add_argument("--db-path", default="monitor.db", help="Path to SQLite DB")
        sub.add_argument("--format", choices=["json", "text"], default="json")
        sub.add_argument("--text-header", action=argparse.BooleanOptionalAction, default=True)
        sub.add_argument("--local-root", default=DEFAULT_CONFIG["local_root"])
        sub.add_argument("--filter-path", default=None,
                          help="Defaults to <local-root>.filters")
        sub.add_argument("--remote", default=DEFAULT_CONFIG["rclone_remote"])

    add_parser = subparsers.add_parser("add", help="Include folder in sync (rclone filter-file)")
    add_parser.add_argument("--path", required=True, help="Path to include (e.g., /Projects)")
    common_flags(add_parser)
    add_parser.add_argument("--apply", action="store_true", help="Write filter-file and materialize via rclone copy")

    remove_parser = subparsers.add_parser("remove", help="Exclude folder from sync (rclone filter-file)")
    remove_parser.add_argument("--path", required=True, help="Path to exclude (e.g., /Projects)")
    common_flags(remove_parser)
    remove_parser.add_argument("--apply", action="store_true", help="Write filter-file")
    remove_parser.add_argument("--delete-local", action="store_true",
                                help="After --apply, if `rclone check` finds 0 differences, "
                                     "empty the local folder's contents (kept if check fails or differs)")

    list_parser = subparsers.add_parser("list", help="List currently synced (included) paths")
    common_flags(list_parser)

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.local_root = require_local_root(args.local_root, tool="sync_filters.py")
    filter_path = args.filter_path or default_filter_path(args.local_root)
    filters_result = load_sync_filters(filter_path)
    include_dirs = filters_result.include_dirs
    warnings = list(filters_result.warnings)

    if args.command == "list":
        result = ActionResult(action="list", path="", dry_run=True, before=include_dirs, after=include_dirs,
                               warnings=warnings, filter_path=filter_path)
        render(result, args.format, args.text_header)
        return

    storage = create_storage(args.db_path)
    analyzer = Analyzer(storage)

    try:
        snapshot = build_composite_snapshot(analyzer)
    except Exception as exc:
        message = str(exc)
        if "full" in message.lower():
            message = "No full cloud scan found. Run `--backend rclone scan cloud` first."
        result = ActionResult(action=args.command, path=normalize_path(args.path), dry_run=True,
                               before=include_dirs, after=include_dirs, warnings=warnings,
                               filter_path=filter_path, error=message)
        render(result, args.format, args.text_header)
        return

    if not path_exists_in_snapshot(analyzer, snapshot, args.path):
        result = ActionResult(action=args.command, path=normalize_path(args.path), dry_run=True,
                               before=include_dirs, after=include_dirs, warnings=warnings,
                               filter_path=filter_path, snapshot_base_scan_id=snapshot.base_scan_id,
                               error=f"Path not found in cloud snapshot: {args.path}")
        render(result, args.format, args.text_header)
        return

    if args.command == "add":
        result = compute_add(args.path, include_dirs)
    else:
        result = compute_remove(args.path, include_dirs)

    result.warnings.extend(warnings)
    result.filter_path = filter_path
    result.snapshot_base_scan_id = snapshot.base_scan_id

    policy_path = var_path("sync_policy.json")
    if args.apply and Path(policy_path).exists():
        result.warnings.append(
            "Policy file exists; sync_filters.py is a legacy low-level filter "
            "editor and does not update var/sync_policy.json. Prefer "
            "tools/sync_policy.py for bidirectional/download-only decisions."
        )

    if args.apply and result.error is None:
        write_sync_filters(filter_path, result.after)
        result.dry_run = False

        if args.command == "add":
            materialize = rclone_copy_materialize(args.remote, args.local_root, filter_path)
            result.materialize = {
                "returncode": materialize.returncode,
                "stdout": materialize.stdout,
                "stderr": materialize.stderr,
            }
        elif args.command == "remove" and args.delete_local:
            entry = result.removed[0]
            check = rclone_check_entry(args.remote, entry, args.local_root)
            result.check = {
                "returncode": check.returncode,
                "stdout": check.stdout,
                "stderr": check.stderr,
            }
            if check.returncode == 0:
                result.local_delete = delete_local_entry_contents(args.local_root, entry)
            else:
                result.local_delete = {
                    "deleted": False,
                    "reason": "rclone check found differences or failed — local copy left intact",
                }

    render(result, args.format, args.text_header)


if __name__ == "__main__":
    main()
