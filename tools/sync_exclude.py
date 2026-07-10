#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sys
from typing import List, Set


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.sync_common import (  # noqa: E402
    build_composite_snapshot,
    create_storage,
    daemon_status,
    fetch_child_dirs,
    load_exclude_dirs,
    normalize_path,
    path_exists_in_snapshot,
    restart_daemon_systemctl,
    run_local_scan,
    select_scan_id_for_path,
    sleep_sec,
    stop_start_daemon,
)
from ydm import Analyzer, DEFAULT_CONFIG  # noqa: E402


@dataclass
class ActionResult:
    action: str
    path: str
    dry_run: bool
    before: List[str]
    after: List[str]
    warnings: List[str]
    added: List[str] = None
    removed: List[str] = None
    parent: str | None = None
    children_count: int | None = None
    config_path: str | None = None
    snapshot_base_scan_id: int | None = None
    snapshot_folder_updates_count: int | None = None
    plan: List[dict] = None
    plan_text: List[str] = None
    daemon: dict | None = None
    local_scan_started: bool | None = None
    local_scan_error: str | None = None
    error: str | None = None


def normalize_exclude_entry(path: str) -> str:
    normalized = normalize_path(path)
    if normalized == "/":
        return ""
    return normalized[1:]


def normalize_exclude_list(entries: List[str]) -> List[str]:
    normalized = []
    for entry in entries:
        entry = entry.strip()
        if entry.startswith("/"):
            entry = entry[1:]
        if entry.endswith("/") and len(entry) > 1:
            entry = entry[:-1]
        if entry:
            normalized.append(entry)
    return normalized


def command_result_to_dict(result) -> dict:
    return {
        "cmd": result.cmd,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def compute_add(
    analyzer: Analyzer,
    snapshot,
    path: str,
    exclude_dirs: List[str],
) -> ActionResult:
    warnings: List[str] = []
    normalized_path = normalize_path(path)
    if normalized_path == "/":
        return ActionResult(
            action="add",
            path=normalized_path,
            dry_run=True,
            before=exclude_dirs,
            after=exclude_dirs,
            warnings=warnings,
            added=[],
            removed=[],
            parent=None,
            children_count=None,
            snapshot_base_scan_id=None,
            plan=[],
            error="Cannot add root path",
        )

    parent_path = normalize_path(str(Path(normalized_path).parent))
    if parent_path == "/.":
        parent_path = "/"

    relative_parent = normalize_exclude_entry(parent_path)
    relative_target = normalize_exclude_entry(normalized_path)

    current = normalize_exclude_list(exclude_dirs)
    before = sorted(set(current))
    after_set = set(before)

    children_count = None
    if relative_parent in after_set:
        after_set.discard(relative_parent)
        scan_id = select_scan_id_for_path(parent_path, snapshot)
        children = fetch_child_dirs(analyzer.storage, scan_id, parent_path)
        children_count = len(children)
        for child in children:
            child_path = (
                f"{relative_parent}/{child}" if relative_parent else child
            )
            if child_path != relative_target:
                after_set.add(child_path)
    else:
        after_set.discard(relative_target)

    after = sorted(after_set)
    removed = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    plan: List[dict] = []
    plan_text: List[str] = []
    if relative_parent in before:
        plan.append({"action": "remove_exclude", "path": relative_parent})
        plan_text.append(f"Remove from exclude-dirs: {relative_parent}")
        if children_count is not None:
            count = max(children_count - 1, 0)
            plan.append(
                {
                    "action": "add_exclude_children",
                    "parent": relative_parent,
                    "count": count,
                }
            )
            plan_text.append(
                f"Add to exclude-dirs: {count} siblings under {relative_parent}"
            )
    else:
        plan.append({"action": "remove_exclude", "path": relative_target})
        plan_text.append(f"Remove from exclude-dirs: {relative_target}")

    return ActionResult(
        action="add",
        path=normalized_path,
        dry_run=True,
        before=before,
        after=after,
        warnings=warnings,
        added=added,
        removed=removed,
        parent=parent_path,
        children_count=children_count,
        snapshot_base_scan_id=snapshot.base_scan_id,
        plan=plan,
        plan_text=plan_text,
    )


def compute_remove(
    path: str,
    exclude_dirs: List[str],
) -> ActionResult:
    warnings: List[str] = []
    normalized_path = normalize_path(path)
    if normalized_path == "/":
        return ActionResult(
            action="remove",
            path=normalized_path,
            dry_run=True,
            before=exclude_dirs,
            after=exclude_dirs,
            warnings=warnings,
            added=[],
            removed=[],
            parent=None,
            children_count=None,
            snapshot_base_scan_id=None,
            plan=[],
            error="Cannot remove root path",
        )

    relative_target = normalize_exclude_entry(normalized_path)
    before = sorted(set(normalize_exclude_list(exclude_dirs)))
    after_set = set(before)
    after_set.add(relative_target)

    after = sorted(after_set)
    removed = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    parent_path = normalize_path(str(Path(normalized_path).parent))
    if parent_path == "/.":
        parent_path = "/"
    plan = [{"action": "add_exclude", "path": relative_target}]
    plan_text = [f"Add to exclude-dirs: {relative_target}"]
    return ActionResult(
        action="remove",
        path=normalized_path,
        dry_run=True,
        before=before,
        after=after,
        warnings=warnings,
        added=added,
        removed=removed,
        parent=parent_path,
        children_count=None,
        snapshot_base_scan_id=None,
        plan=plan,
        plan_text=plan_text,
    )


def write_config(config_path: str, exclude_dirs: List[str]) -> None:
    backup_path = f"{config_path}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copyfile(config_path, backup_path)

    lines: List[str] = []
    found = False
    with open(config_path, "r") as handle:
        for line in handle:
            if line.startswith("exclude-dirs="):
                value = ",".join(exclude_dirs)
                lines.append(f"exclude-dirs={value}\n")
                found = True
            else:
                lines.append(line)

    if not found:
        lines.append(f"exclude-dirs={','.join(exclude_dirs)}\n")

    with open(config_path, "w") as handle:
        handle.writelines(lines)


def render(result: ActionResult, fmt: str, text_header: bool) -> None:
    payload = {
        "schema": "sync_exclude:v1",
        "action": result.action,
        "path": result.path,
        "dry_run": result.dry_run,
        "before": result.before,
        "after": result.after,
        "warnings": result.warnings,
        "added": result.added or [],
        "removed": result.removed or [],
        "parent": result.parent,
        "config_path": result.config_path,
        "snapshot_base_scan_id": result.snapshot_base_scan_id,
        "snapshot_folder_updates_count": result.snapshot_folder_updates_count,
        "plan": result.plan or [],
        "plan_text": result.plan_text or [],
        "daemon": result.daemon,
        "local_scan_started": result.local_scan_started,
        "local_scan_error": result.local_scan_error,
        "error": result.error,
    }
    if result.children_count is not None:
        payload["children_count"] = result.children_count
    if fmt == "json":
        success = result.error is None
        print(json.dumps({"success": success, "data": payload}, ensure_ascii=False, indent=2))
    else:
        if text_header:
            print(f"schema: sync_exclude:v1")
            if result.error:
                print(f"error: {result.error}")
            print(f"action: {result.action}")
            if result.path:
                print(f"path: {result.path}")
            print(f"dry_run: {result.dry_run}")
            if result.parent:
                print(f"parent: {result.parent}")
            if result.children_count is not None:
                print(f"children_count: {result.children_count}")
            if result.config_path:
                print(f"config_path: {result.config_path}")
            if result.snapshot_base_scan_id is not None:
                print(f"snapshot_base_scan_id: {result.snapshot_base_scan_id}")
            if result.snapshot_folder_updates_count is not None:
                print(f"snapshot_folder_updates_count: {result.snapshot_folder_updates_count}")
            if result.daemon is not None:
                print("daemon: attached")
            if result.local_scan_started is not None:
                print(f"local_scan_started: {result.local_scan_started}")
            if result.local_scan_error:
                print(f"local_scan_error: {result.local_scan_error}")
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
        if result.plan:
            print("Plan:")
            for step in result.plan:
                action = step.get("action", "")
                if action == "remove_exclude":
                    print(f"- Remove from exclude-dirs: {step.get('path')}")
                elif action == "add_exclude":
                    print(f"- Add to exclude-dirs: {step.get('path')}")
                elif action == "add_exclude_children":
                    count = step.get("count")
                    parent = step.get("parent")
                    print(f"- Add to exclude-dirs: {count} siblings under {parent}")
                else:
                    print(f"- {step}")
        if result.plan_text:
            print("Plan (text):")
            for line in result.plan_text:
                print(f"- {line}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simple sync exclude (transition tool)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Include folder in sync")
    add_parser.add_argument("--path", required=True, help="Path to include (e.g., /A/B)")
    add_parser.add_argument("--db-path", default="monitor.db", help="Path to SQLite DB")
    add_parser.add_argument("--format", choices=["json", "text"], default="json")
    add_parser.add_argument(
        "--text-header",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include metadata header in text output (default)",
    )
    add_parser.add_argument(
        "--restart-daemon",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Restart Yandex Disk daemon after apply (default)",
    )
    add_parser.add_argument(
        "--daemon-status",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fetch daemon status after restart (default)",
    )
    add_parser.add_argument("--restart-delay-sec", type=int, default=3)
    add_parser.add_argument("--status-delay-sec", type=int, default=3)
    add_parser.add_argument(
        "--local-scan",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run local scan after restart (default)",
    )
    add_parser.add_argument("--local-scan-delay-sec", type=int, default=3)
    add_parser.add_argument("--local-root", default=DEFAULT_CONFIG["local_root"])
    add_parser.add_argument("--apply", action="store_true", help="Apply changes")

    remove_parser = subparsers.add_parser("remove", help="Exclude folder from sync")
    remove_parser.add_argument("--path", required=True, help="Path to exclude (e.g., /A/B)")
    remove_parser.add_argument("--db-path", default="monitor.db", help="Path to SQLite DB")
    remove_parser.add_argument("--format", choices=["json", "text"], default="json")
    remove_parser.add_argument(
        "--text-header",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include metadata header in text output (default)",
    )
    remove_parser.add_argument(
        "--restart-daemon",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Restart Yandex Disk daemon after apply (default)",
    )
    remove_parser.add_argument(
        "--daemon-status",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fetch daemon status after restart (default)",
    )
    remove_parser.add_argument("--restart-delay-sec", type=int, default=3)
    remove_parser.add_argument("--status-delay-sec", type=int, default=3)
    remove_parser.add_argument(
        "--local-scan",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run local scan after restart (default)",
    )
    remove_parser.add_argument("--local-scan-delay-sec", type=int, default=3)
    remove_parser.add_argument("--local-root", default=DEFAULT_CONFIG["local_root"])
    remove_parser.add_argument("--apply", action="store_true", help="Apply changes")

    list_parser = subparsers.add_parser("list", help="List exclude-dirs")
    list_parser.add_argument("--format", choices=["json", "text"], default="json")
    list_parser.add_argument(
        "--text-header",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include metadata header in text output (default)",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    exclude_result = load_exclude_dirs()
    exclude_dirs = exclude_result.exclude_dirs
    warnings = list(exclude_result.warnings)
    config_path = exclude_result.config_path

    if args.command == "list":
        result = ActionResult(
            action="list",
            path="",
            dry_run=True,
            before=exclude_dirs,
            after=exclude_dirs,
            warnings=warnings,
            added=[],
            removed=[],
            parent=None,
            children_count=None,
            config_path=config_path,
            snapshot_base_scan_id=None,
            snapshot_folder_updates_count=None,
            plan=[],
            plan_text=[],
            error=None,
        )
        render(result, args.format, args.text_header)
        return

    storage = create_storage(args.db_path)
    analyzer = Analyzer(storage)

    try:
        snapshot = build_composite_snapshot(analyzer)
    except Exception as exc:
        message = str(exc)
        if "full scan" in message.lower() or "full" in message.lower():
            message = "No full cloud scan found. Run a full cloud scan first."
        result = ActionResult(
            action=args.command,
            path=normalize_path(args.path),
            dry_run=True,
            before=exclude_dirs,
            after=exclude_dirs,
            warnings=warnings,
            added=[],
            removed=[],
            parent=None,
            children_count=None,
            config_path=config_path,
            snapshot_base_scan_id=None,
            snapshot_folder_updates_count=None,
            plan=[],
            plan_text=[],
            error=message,
        )
        render(result, args.format, args.text_header)
        return

    if not path_exists_in_snapshot(analyzer, snapshot, args.path):
        result = ActionResult(
            action=args.command,
            path=normalize_path(args.path),
            dry_run=True,
            before=exclude_dirs,
            after=exclude_dirs,
            warnings=warnings,
            added=[],
            removed=[],
            parent=None,
            children_count=None,
            config_path=config_path,
            snapshot_base_scan_id=snapshot.base_scan_id,
            snapshot_folder_updates_count=len(snapshot.folder_updates),
            plan=[],
            plan_text=[],
            error=f"Path not found in snapshot: {args.path}",
        )
        render(result, args.format, args.text_header)
        return

    if args.command == "add":
        result = compute_add(analyzer, snapshot, args.path, exclude_dirs)
    else:
        result = compute_remove(args.path, exclude_dirs)

    result.warnings.extend(warnings)
    result.config_path = config_path
    result.snapshot_base_scan_id = snapshot.base_scan_id
    result.snapshot_folder_updates_count = len(snapshot.folder_updates)

    if args.apply and result.error is None:
        write_config(exclude_result.config_path, result.after)
        result.dry_run = False
        if args.restart_daemon:
            daemon_info = {
                "restart_attempted": True,
                "cli": {},
                "systemctl": None,
                "status_after_start": None,
                "status_after_systemctl": None,
            }
            cli_results = stop_start_daemon()
            daemon_info["cli"] = {
                key: command_result_to_dict(value)
                for key, value in cli_results.items()
            }
            sleep_sec(args.restart_delay_sec)
            if args.daemon_status:
                daemon_info["status_after_start"] = command_result_to_dict(
                    daemon_status()
                )
            daemon_info["systemctl"] = command_result_to_dict(
                restart_daemon_systemctl()
            )
            sleep_sec(args.status_delay_sec)
            if args.daemon_status:
                daemon_info["status_after_systemctl"] = command_result_to_dict(
                    daemon_status()
                )
            result.daemon = daemon_info

        if args.local_scan:
            sleep_sec(args.local_scan_delay_sec)
            local_result = run_local_scan(args.db_path, args.local_root)
            result.local_scan_started = local_result.started
            result.local_scan_error = local_result.error

    render(result, args.format, args.text_header)


if __name__ == "__main__":
    main()
