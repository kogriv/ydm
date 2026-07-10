#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.sync_common import (  # noqa: E402
    build_composite_snapshot,
    create_storage,
    fetch_child_dirs,
    get_latest_successful_scan_id,
    load_exclude_dirs,
    normalize_path,
    run_local_scan,
    select_scan_id_for_path,
    sleep_sec,
)
from ydm import Analyzer, DEFAULT_CONFIG  # noqa: E402


STATUS_FULL = "full"
STATUS_PARTIAL = "partial"
STATUS_EXCLUDED = "excluded"


@dataclass
class TreeNode:
    path: str
    name: str
    sync_status: str = STATUS_FULL
    children: List["TreeNode"] = field(default_factory=list)
    children_count: int = 0
    has_synced_descendants: bool = False
    visible_children_count: int = 0
    sync_percent: float | None = None

    def to_dict(self, include_children: bool = True) -> Dict[str, object]:
        data = {
            "path": self.path,
            "name": self.name,
            "sync_status": self.sync_status,
            "has_synced_descendants": self.has_synced_descendants,
            "visible_children_count": self.visible_children_count,
        }
        if self.sync_percent is not None:
            data["sync_percent"] = self.sync_percent
        if include_children:
            if self.children:
                data["children"] = [child.to_dict(include_children=True) for child in self.children]
            else:
                data["children"] = []
        else:
            data["children_count"] = self.children_count
        return data


def normalize_exclude_dirs(exclude_dirs: List[str]) -> Set[str]:
    normalized: Set[str] = set()
    for entry in exclude_dirs:
        entry = entry.strip()
        if entry.startswith("/"):
            entry = entry[1:]
        if entry.endswith("/") and len(entry) > 1:
            entry = entry[:-1]
        if entry:
            normalized.add(entry)
    return normalized


def is_path_excluded(path: str, exclude_dirs: Set[str]) -> bool:
    normalized = normalize_path(path)
    relative = normalized[1:] if normalized != "/" else ""
    if relative == "":
        return False
    parts = relative.split("/")
    prefix = []
    for part in parts:
        prefix.append(part)
        candidate = "/".join(prefix)
        if candidate in exclude_dirs:
            return True
    return False


def build_tree(
    analyzer: Analyzer,
    snapshot,
    root_path: str,
    depth: int,
) -> TreeNode:
    storage = analyzer.storage

    def build_node(path: str, remaining_depth: int) -> TreeNode:
        normalized = normalize_path(path)
        name = "/" if normalized == "/" else normalized.rsplit("/", 1)[-1]
        node = TreeNode(path=normalized, name=name)
        scan_id = select_scan_id_for_path(normalized, snapshot)
        children_names = fetch_child_dirs(storage, scan_id, normalized)
        node.children_count = len(children_names)

        if remaining_depth > 0:
            for child_name in children_names:
                child_path = f"/{child_name}" if normalized == "/" else f"{normalized}/{child_name}"
                node.children.append(build_node(child_path, remaining_depth - 1))

        return node

    return build_node(root_path, depth)


def _count_files_for_prefix(conn, scan_id: int, prefix: str) -> int:
    if prefix == "":
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM files
            WHERE scan_id = ?
              AND type = 'file'
            """,
            (scan_id,),
        ).fetchone()
        return row[0] if row else 0

    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM files
        WHERE scan_id = ?
          AND type = 'file'
          AND (parent_path = ? OR parent_path LIKE ?)
        """,
        (scan_id, prefix, f"{prefix}/%"),
    ).fetchone()
    return row[0] if row else 0


def count_cloud_files_for_path(analyzer: Analyzer, snapshot, path: str) -> int:
    prefix = normalize_path(path)
    if prefix == "/":
        prefix = ""
    base_scan_id = select_scan_id_for_path(path, snapshot)
    deeper_updates = [
        folder for folder in snapshot.folder_updates.keys()
        if folder and (prefix == "" or folder.startswith(prefix + "/"))
    ]

    conn = analyzer.storage.get_connection()
    try:
        base_count = _count_files_for_prefix(conn, base_scan_id, prefix)
        for folder in deeper_updates:
            base_count -= _count_files_for_prefix(conn, base_scan_id, folder)

        updated_count = 0
        for folder in deeper_updates:
            scan_id = snapshot.folder_updates[folder]
            updated_count += _count_files_for_prefix(conn, scan_id, folder)
    finally:
        conn.close()

    return max(base_count + updated_count, 0)


def count_local_files_for_path(storage, local_scan_id: int, path: str) -> int:
    prefix = normalize_path(path).lstrip("/")
    conn = storage.get_connection()
    try:
        return _count_files_for_prefix(conn, local_scan_id, prefix)
    finally:
        conn.close()


def apply_sync_percent(
    node: TreeNode,
    analyzer: Analyzer,
    snapshot,
    local_scan_id: int | None,
) -> None:
    if local_scan_id is None:
        return
    cloud_count = count_cloud_files_for_path(analyzer, snapshot, node.path)
    local_count = count_local_files_for_path(analyzer.storage, local_scan_id, node.path)
    if cloud_count == 0:
        node.sync_percent = 100.0
    else:
        node.sync_percent = round((local_count / cloud_count) * 100.0, 1)
    for child in node.children:
        apply_sync_percent(child, analyzer, snapshot, local_scan_id)


def compute_status(node: TreeNode, exclude_dirs: Set[str], collapse: bool) -> bool:
    has_synced_descendants = False
    has_excluded_descendant = False
    visible_children: List[TreeNode] = []

    for child in node.children:
        child_has_synced = compute_status(child, exclude_dirs, collapse)
        if child_has_synced:
            visible_children.append(child)
        has_synced_descendants = has_synced_descendants or child_has_synced
        if child.sync_status in {STATUS_EXCLUDED, STATUS_PARTIAL}:
            has_excluded_descendant = True

    own_excluded = is_path_excluded(node.path, exclude_dirs)
    if own_excluded:
        node.sync_status = STATUS_PARTIAL if has_synced_descendants else STATUS_EXCLUDED
    else:
        node.sync_status = STATUS_PARTIAL if has_excluded_descendant else STATUS_FULL

    node.has_synced_descendants = has_synced_descendants
    if collapse:
        node.children = visible_children
    node.visible_children_count = len(visible_children)
    return node.sync_status in {STATUS_FULL, STATUS_PARTIAL} or has_synced_descendants


def render_text(node: TreeNode, indent: str = "") -> List[str]:
    marker = {
        STATUS_FULL: "[S]",
        STATUS_PARTIAL: "[P]",
        STATUS_EXCLUDED: "[-]",
    }.get(node.sync_status, "[?]")
    suffix = f" {node.sync_percent:.1f}%" if node.sync_percent is not None else ""
    lines = [f"{marker} {indent}{node.name}{suffix}"]
    for child in node.children:
        lines.extend(render_text(child, indent + "  "))
    return lines


def render_text_tree(node: TreeNode) -> List[str]:
    marker_map = {
        STATUS_FULL: "[S]",
        STATUS_PARTIAL: "[P]",
        STATUS_EXCLUDED: "[-]",
    }

    def walk(current: TreeNode, prefix: str, is_last: bool) -> List[str]:
        marker = marker_map.get(current.sync_status, "[?]")
        connector = "└── " if is_last else "├── "
        suffix = f" {current.sync_percent:.1f}%" if current.sync_percent is not None else ""
        line = f"{marker}{prefix}{connector}{current.name}{suffix}"
        lines = [line]
        if current.children:
            next_prefix = prefix + ("    " if is_last else "│   ")
            for index, child in enumerate(current.children):
                child_last = index == len(current.children) - 1
                lines.extend(walk(child, next_prefix, child_last))
        return lines

    root_marker = marker_map.get(node.sync_status, "[?]")
    root_suffix = f" {node.sync_percent:.1f}%" if node.sync_percent is not None else ""
    output = [f"{root_marker} {node.name}{root_suffix}"]
    for index, child in enumerate(node.children):
        child_last = index == len(node.children) - 1
        output.extend(walk(child, "", child_last))
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simple sync tree (transition tool)")
    parser.add_argument("--path", default="/", help="Root path (e.g., /A/B)")
    parser.add_argument("--depth", type=int, default=2, help="Depth from root path")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    parser.add_argument(
        "--text-tree",
        action="store_true",
        help="Use tree branches in text output",
    )
    parser.add_argument(
        "--text-header",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include metadata header in text output (default)",
    )
    parser.add_argument(
        "--local-scan",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run local scan before building tree (default)",
    )
    parser.add_argument("--local-scan-delay-sec", type=int, default=3)
    parser.add_argument("--local-root", default=DEFAULT_CONFIG["local_root"])
    parser.add_argument(
        "--sync-percent",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Compute sync percent per node (default)",
    )
    parser.add_argument("--db-path", default="monitor.db", help="Path to SQLite DB")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--collapse-synced",
        action="store_true",
        help="Show only branches with synced folders (default)",
    )
    mode_group.add_argument(
        "--show-all",
        action="store_true",
        help="Show full tree regardless of exclude-dirs",
    )
    parser.set_defaults(collapse_synced=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root_path = normalize_path(args.path)
    exclude_result = load_exclude_dirs()
    exclude_dirs = normalize_exclude_dirs(exclude_result.exclude_dirs)

    storage = create_storage(args.db_path)
    analyzer = Analyzer(storage)

    snapshot = build_composite_snapshot(analyzer)
    collapse = False if args.show_all else args.collapse_synced
    local_scan_started = None
    local_scan_error = None
    local_scan_id = get_latest_successful_scan_id(storage, "local")
    if args.local_scan:
        sleep_sec(args.local_scan_delay_sec)
        local_result = run_local_scan(args.db_path, args.local_root)
        local_scan_started = local_result.started
        local_scan_error = local_result.error
        if local_result.started and local_result.scan_id is not None:
            local_scan_id = local_result.scan_id

    node = build_tree(analyzer, snapshot, root_path, args.depth)
    compute_status(node, exclude_dirs, collapse=collapse)

    if args.sync_percent:
        apply_sync_percent(node, analyzer, snapshot, local_scan_id)

    if args.format == "json":
        payload = {
            "schema": "sync_tree:v1",
            "root": node.to_dict(include_children=True),
            "root_path": root_path,
            "root_depth": args.depth,
            "root_children_count": node.children_count,
            "root_visible_children_count": node.visible_children_count,
            "config_path": exclude_result.config_path,
            "warnings": exclude_result.warnings,
            "collapsed": collapse,
            "local_scan_started": local_scan_started,
            "local_scan_error": local_scan_error,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if args.text_header:
            print("schema: sync_tree:v1")
            print(f"root_path: {root_path}")
            print(f"root_depth: {args.depth}")
            print(f"root_children_count: {node.children_count}")
            print(f"root_visible_children_count: {node.visible_children_count}")
            print(f"config_path: {exclude_result.config_path}")
            if exclude_result.warnings:
                print("warnings:")
                for warning in exclude_result.warnings:
                    print(f"- {warning}")
            print(f"collapsed: {collapse}")
            if local_scan_started is not None:
                print(f"local_scan_started: {local_scan_started}")
            if local_scan_error:
                print(f"local_scan_error: {local_scan_error}")
        if args.text_tree:
            lines = render_text_tree(node)
        else:
            lines = render_text(node)
        if args.text_header:
            print("")
        for line in lines:
            print(line)


if __name__ == "__main__":
    main()
