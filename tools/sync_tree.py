#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.sync_backends import (  # noqa: E402
    backend_from_args,
    detect_backend,
)
from tools.sync_common import (  # noqa: E402
    create_storage,
    default_filter_path,
    get_latest_successful_scan_id,
    load_exclude_dirs,
    load_sync_filters,
    normalize_path,
    run_local_scan,
    select_scan_id_for_path,
    sleep_sec,
)
from tools.sync_policy import (  # noqa: E402
    default_policy_path,
    effective_download_paths,
    policy_paths_by_mode,
)
from tools.sync_tree_cloud import (  # noqa: E402
    fetch_child_names,
    select_snapshot_for_tree,
)
from tools.sync_tree_policy import (  # noqa: E402
    LEGEND_LINES,
    SCHEMA_V2,
    PolicyContext,
    display_marker,
    effective_policy_state,
    is_under_policy_path,
    load_policy_context,
    local_state,
    path_in_policy,
    policy_entry_for_path,
    policy_mode_for_path,
    policy_paths_set,
    policy_summary_line,
    rel_path_from_cloud,
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
    policy_mode: str | None = None
    policy_entry: str | None = None
    in_policy: bool = False
    local_state: str | None = None
    display_marker: str = "[?]"
    cloud_file_count: int = 0
    local_file_count: int = 0

    def to_dict(self, include_children: bool = True, schema: str = "sync_tree:v1") -> Dict[str, object]:
        if schema == SCHEMA_V2:
            data: Dict[str, object] = {
                "path": self.path,
                "name": self.name,
                "markers": {
                    "display": self.display_marker,
                    "policy_mode": self.policy_mode,
                    "local_state": self.local_state,
                    "cloud_state": "present" if self.cloud_file_count > 0 or self.children else "unknown",
                },
                "sync_percent": self.sync_percent,
                "in_policy": self.in_policy,
                "policy_entry": self.policy_entry,
                "cloud_file_count": self.cloud_file_count,
                "local_file_count": self.local_file_count,
                "sync_status_v1": self.sync_status,
            }
        else:
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
            data["children"] = [
                child.to_dict(include_children=True, schema=schema) for child in self.children
            ] if self.children else []
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


def is_path_included(path: str, include_dirs: Set[str]) -> bool:
    normalized = normalize_path(path)
    relative = normalized[1:] if normalized != "/" else ""
    if relative == "":
        return False
    parts = relative.split("/")
    prefix = []
    for part in parts:
        prefix.append(part)
        if "/".join(prefix) in include_dirs:
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
        children_names = fetch_child_names(storage, scan_id, normalized)
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

    variants = {prefix, prefix.lstrip("/"), f"/{prefix.lstrip('/')}"}
    total = 0
    for variant in variants:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM files
            WHERE scan_id = ?
              AND type = 'file'
              AND (parent_path = ? OR parent_path LIKE ?)
            """,
            (scan_id, variant, f"{variant.rstrip('/')}/%"),
        ).fetchone()
        if row:
            total = max(total, row[0])
    return total


def _folder_file_counts(conn, scan_id: int, subtree: str) -> Dict[str, int]:
    """{parent_path: file count} for one scan, restricted to a subtree.

    Cloud scans always store parent_path with a leading slash ("" at the
    root), so one GROUP BY answers for every folder at once.
    """
    if subtree:
        rows = conn.execute(
            """
            SELECT parent_path, COUNT(*)
            FROM files
            WHERE scan_id = ? AND type = 'file'
              AND (parent_path = ? OR parent_path LIKE ?)
            GROUP BY parent_path
            """,
            (scan_id, subtree, f"{subtree}/%"),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT parent_path, COUNT(*)
            FROM files
            WHERE scan_id = ? AND type = 'file'
            GROUP BY parent_path
            """,
            (scan_id,),
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def count_cloud_files_for_path(analyzer: Analyzer, snapshot, path: str) -> int:
    """Files under `path` in the composite snapshot.

    The composite resolves *per folder*: each folder is served by the newest
    scan that covered it, and by the base scan otherwise. Counting therefore
    means summing per folder, not adding and subtracting recursive totals —
    the previous implementation did the latter, which double-subtracted
    whenever one updated folder sat inside another, and matched siblings by
    raw string prefix (`/pro` also picked up `/protein`). It also issued a
    handful of COUNT queries per updated folder: 176 s for the root here once
    the snapshot legitimately carried 2 249 folder updates.
    """
    rel = rel_path_from_cloud(path)
    subtree = "" if rel in ("", "/") else normalize_path(rel)

    updates = {
        folder: scan_id
        for folder, scan_id in snapshot.folder_updates.items()
        if folder and (not subtree or folder == subtree or folder.startswith(subtree + "/"))
    }
    by_scan: Dict[int, List[str]] = {}
    for folder, scan_id in updates.items():
        by_scan.setdefault(scan_id, []).append(folder)

    conn = analyzer.storage.get_connection()
    try:
        total = sum(
            count
            for folder, count in _folder_file_counts(conn, snapshot.base_scan_id, subtree).items()
            if folder not in updates
        )
        for scan_id, folders in by_scan.items():
            counts = _folder_file_counts(conn, scan_id, subtree)
            total += sum(counts.get(folder, 0) for folder in folders)
    finally:
        conn.close()

    return total


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
    local_root: str,
) -> None:
    cloud_count = count_cloud_files_for_path(analyzer, snapshot, node.path)
    local_count = count_local_files_for_path(analyzer.storage, local_scan_id, node.path) if local_scan_id else 0
    node.cloud_file_count = cloud_count
    node.local_file_count = local_count

    if local_scan_id is None:
        node.sync_percent = None
    elif cloud_count == 0:
        node.sync_percent = 100.0 if local_count > 0 else 0.0
    else:
        node.sync_percent = round((local_count / cloud_count) * 100.0, 1)

    for child in node.children:
        apply_sync_percent(child, analyzer, snapshot, local_scan_id, local_root)


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


def _mark_full_subtree(node: TreeNode, collapse: bool) -> None:
    node.sync_status = STATUS_FULL
    node.has_synced_descendants = bool(node.children)
    for child in node.children:
        _mark_full_subtree(child, collapse)
    node.visible_children_count = len(node.children)


def compute_status_whitelist(
    node: TreeNode,
    include_dirs: Set[str],
    collapse: bool,
    policy_paths: Optional[Set[str]] = None,
    local_root: Optional[str] = None,
) -> bool:
    if is_path_included(node.path, include_dirs):
        _mark_full_subtree(node, collapse)
        return True

    has_synced_descendants = False
    visible_children: List[TreeNode] = []
    for child in node.children:
        child_has_synced = compute_status_whitelist(
            child, include_dirs, collapse, policy_paths=policy_paths, local_root=local_root
        )
        if child_has_synced:
            visible_children.append(child)
        has_synced_descendants = has_synced_descendants or child_has_synced

    node.sync_status = STATUS_PARTIAL if has_synced_descendants else STATUS_EXCLUDED
    node.has_synced_descendants = has_synced_descendants

    if collapse:
        kept = list(visible_children)
        if local_root:
            from tools.sync_tree_policy import local_dir_exists

            for child in node.children:
                if child in kept:
                    continue
                if local_dir_exists(local_root, child.path):
                    kept.append(child)
        if policy_paths and is_under_policy_path(node.path, policy_paths):
            kept = node.children
        node.children = sorted(kept, key=lambda n: n.name.lower())
    node.visible_children_count = len(node.children)
    return has_synced_descendants or (
        policy_paths is not None and is_under_policy_path(node.path, policy_paths)
    )


def apply_policy_overlay(
    node: TreeNode,
    ctx: PolicyContext,
    local_root: str,
) -> None:
    policy = ctx.policy
    entry_key, _entry_meta = policy_entry_for_path(node.path, policy)
    node.policy_entry = entry_key
    node.policy_mode, node.in_policy = effective_policy_state(node.path, ctx)
    node.local_state = local_state(
        policy_mode=node.policy_mode,
        in_policy=node.in_policy,
        cloud_count=node.cloud_file_count,
        local_count=node.local_file_count,
        local_root=local_root,
        path=node.path,
    )
    node.display_marker = display_marker(
        policy_mode=node.policy_mode,
        in_policy=node.in_policy,
        local_state_value=node.local_state,
        has_synced_descendant=node.has_synced_descendants,
        v1_sync_status=node.sync_status,
    )
    for child in node.children:
        apply_policy_overlay(child, ctx, local_root)


def render_text(node: TreeNode, indent: str = "", schema: str = "sync_tree:v1") -> List[str]:
    if schema == SCHEMA_V2:
        marker = node.display_marker
    else:
        marker = {
            STATUS_FULL: "[S]",
            STATUS_PARTIAL: "[P]",
            STATUS_EXCLUDED: "[-]",
        }.get(node.sync_status, "[?]")
    suffix = f" {node.sync_percent:.1f}%" if node.sync_percent is not None else ""
    lines = [f"{marker} {indent}{node.name}{suffix}"]
    for child in node.children:
        lines.extend(render_text(child, indent + "  ", schema=schema))
    return lines


def render_text_tree(node: TreeNode, schema: str = "sync_tree:v1") -> List[str]:
    def marker_for(current: TreeNode) -> str:
        if schema == SCHEMA_V2:
            return current.display_marker
        return {
            STATUS_FULL: "[S]",
            STATUS_PARTIAL: "[P]",
            STATUS_EXCLUDED: "[-]",
        }.get(current.sync_status, "[?]")

    def walk(current: TreeNode, prefix: str, is_last: bool) -> List[str]:
        connector = "└── " if is_last else "├── "
        suffix = f" {current.sync_percent:.1f}%" if current.sync_percent is not None else ""
        line = f"{marker_for(current)}{prefix}{connector}{current.name}{suffix}"
        lines = [line]
        if current.children:
            next_prefix = prefix + ("    " if is_last else "│   ")
            for index, child in enumerate(current.children):
                child_last = index == len(current.children) - 1
                lines.extend(walk(child, next_prefix, child_last))
        return lines

    root_suffix = f" {node.sync_percent:.1f}%" if node.sync_percent is not None else ""
    output = [f"{marker_for(node)} {node.name}{root_suffix}"]
    for index, child in enumerate(node.children):
        child_last = index == len(node.children) - 1
        output.extend(walk(child, "", child_last))
    return output


def render_v2_header(
    *,
    root_path: str,
    depth: int,
    ctx: PolicyContext,
    selection,
    local_scan_id: Optional[int],
    collapsed: bool,
    local_scan_started: Optional[bool],
    extra_warnings: List[str],
    freshness: Optional[Dict] = None,
) -> List[str]:
    lines = [
        f"schema: {SCHEMA_V2}",
        f"root_path: {root_path}",
        f"depth: {depth}",
        f"policy_path: {ctx.policy_path}",
        f"policy: {policy_summary_line(ctx)}",
        f"snapshot: base scan #{selection.base_scan_id}",
    ]
    if selection.folder_updates:
        # A composite snapshot can carry hundreds of per-folder overrides;
        # dumping the whole dict pushed the tree itself off the screen.
        # The full mapping stays available in --format json.
        updates = sorted(selection.folder_updates.items())
        shown = ", ".join(f"{path}#{scan}" for path, scan in updates[:3])
        rest = len(updates) - 3
        if rest > 0:
            shown += f", +{rest} more (see --format json)"
        lines.append(f"snapshot_updates: {len(updates)} folder(s): {shown}")
    if freshness and freshness.get("base_age_days") is not None:
        lines.append(
            f"snapshot_age: base #{freshness['base_scan_id']} is "
            f"{freshness['base_age_days']} day(s) old and serves "
            f"{freshness['base_share_percent']}% of the files"
        )
    for warning in (freshness or {}).get("warnings", []):
        lines.append(f"WARN: {warning}")
    if local_scan_id is not None:
        lines.append(f"local_scan_id: {local_scan_id}")
    if ctx.filter_mismatch:
        lines.append("WARN: bisync filter out of sync with policy — run: sync_policy render-filters --apply")
    if selection.warning:
        lines.append(f"WARN: {selection.warning}")
    for warning in selection.warnings:
        lines.append(f"WARN: {warning}")
    for warning in extra_warnings:
        lines.append(f"WARN: {warning}")
    lines.append("legend: " + "; ".join(LEGEND_LINES))
    lines.append(f"collapsed: {collapsed}")
    if local_scan_started is not None:
        lines.append(f"local_scan_started: {local_scan_started}")
    lines.append("")
    return lines


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync tree (cloud structure + policy overlay)")
    parser.add_argument("--path", default="/", help="Root path (e.g., /A/B)")
    parser.add_argument("--depth", type=int, default=2, help="Depth from root path")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    parser.add_argument("--schema", choices=["sync_tree:v1", "sync_tree:v2"], default="sync_tree:v2")
    parser.add_argument("--text-tree", action="store_true", help="Use tree branches in text output")
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
    parser.add_argument(
        "--backend",
        choices=["api", "rclone", "daemon", "auto"],
        default="auto",
        help="Sync backend: daemon (yandex-disk config.cfg), rclone (filter whitelist), api (alias for daemon), or auto",
    )
    parser.add_argument("--filter-path", default=None, help="Override rclone filter-file path")
    parser.add_argument(
        "--policy-path",
        default=None,
        help="Path to sync_policy.json (default: var/sync_policy.json)",
    )
    parser.add_argument(
        "--use-policy",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use sync_policy.json for markers (default: on for rclone backend)",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--collapse-synced", action="store_true", help="Show synced branches (default)")
    mode_group.add_argument("--show-all", action="store_true", help="Show full tree")
    parser.set_defaults(collapse_synced=True)
    return parser.parse_args()


def _resolve_backend_name(args) -> str:
    backend = args.backend
    if backend == "auto":
        try:
            backend = detect_backend(
                db_path=args.db_path,
                local_root=args.local_root,
                config_path=args.exclude_config if hasattr(args, "exclude_config") else DEFAULT_CONFIG["exclude_config"],
            ).kind
        except Exception:
            backend = "daemon"
    if backend == "api":
        backend = "daemon"
    return backend


def main() -> None:
    args = parse_args()
    root_path = normalize_path(args.path)
    schema = args.schema
    effective_backend = _resolve_backend_name(args)

    # use_policy is default True unless explicitly disabled
    use_policy = args.use_policy if args.use_policy is not None else True

    policy_path = args.policy_path or default_policy_path()
    policy_ctx = load_policy_context(
        policy_path,
        args.local_root,
        blacklist_semantics=effective_backend == "daemon",
    ) if use_policy else PolicyContext(
        policy=None,
        policy_path=policy_path,
        bidirectional=[],
        download_only=[],
        disabled=[],
        bisync_filter_path=None,
        bisync_filter_hash=None,
        filter_mismatch=False,
    )

    source_path: str
    source_warnings: List[str]

    if effective_backend == "rclone":
        if use_policy and policy_ctx.policy:
            membership_dirs = set(effective_download_paths(policy_ctx.policy))
            source_path = policy_path
            source_warnings = []
            if policy_ctx.filter_mismatch:
                source_warnings.append("bisync filter out of sync with policy")
        else:
            filter_path = args.filter_path or default_filter_path(args.local_root)
            filters_result = load_sync_filters(filter_path)
            membership_dirs = set(filters_result.include_dirs)
            source_path = filters_result.filter_path
            source_warnings = filters_result.warnings
    else:
        # The daemon is a blacklist: everything not in exclude-dirs is synced.
        # Feeding it the whitelist of "bidirectional" policy entries produced an
        # empty membership set — a daemon policy holds only `disabled` entries —
        # so every synced folder rendered as an orphan instead of [B].
        if use_policy and policy_ctx.policy:
            membership_dirs = normalize_exclude_dirs(
                policy_paths_by_mode(policy_ctx.policy, "disabled")
            )
            source_path = policy_path
            source_warnings = []
        else:
            exclude_result = load_exclude_dirs(getattr(args, "exclude_config", None))
            membership_dirs = normalize_exclude_dirs(exclude_result.exclude_dirs)
            source_path = exclude_result.config_path
            source_warnings = exclude_result.warnings

    storage = create_storage(args.db_path)
    analyzer = Analyzer(storage)
    selection = select_snapshot_for_tree(analyzer, root_path)
    snapshot = selection.snapshot

    collapse = False if args.show_all else args.collapse_synced
    policy_path_set = policy_paths_set(policy_ctx) if use_policy else None

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
    if effective_backend == "daemon":
        compute_status(node, membership_dirs, collapse=collapse)
    else:
        compute_status_whitelist(
            node, membership_dirs, collapse=collapse, policy_paths=policy_path_set,
            local_root=args.local_root,
        )

    apply_sync_percent(node, analyzer, snapshot, local_scan_id, args.local_root)

    if use_policy and schema == SCHEMA_V2:
        apply_policy_overlay(node, policy_ctx, args.local_root)

    extra_warnings: List[str] = list(source_warnings)
    if node.children_count == 0 and root_path != "/":
        extra_warnings.append(
            f"No cloud folders under {root_path}. Run: ydm-scan-cloud {root_path}"
        )
    if local_scan_error:
        extra_warnings.append(f"local_scan_error: {local_scan_error}")

    # The composite is only as fresh as the folders someone rescanned; say so
    # rather than letting the tree read as current.
    freshness = analyzer.snapshot_freshness()

    if args.format == "json":
        if schema == SCHEMA_V2:
            payload = {
                "schema": SCHEMA_V2,
                "header": {
                    "root_path": root_path,
                    "root_depth": args.depth,
                    "policy_path": policy_ctx.policy_path,
                    "policy_summary": policy_summary_line(policy_ctx),
                    "cloud_snapshot": {
                        "base_scan_id": selection.base_scan_id,
                        "folder_updates": selection.folder_updates,
                        "freshness_warning": selection.warning,
                        "snapshot_freshness": freshness,
                    },
                    "local_scan_id": local_scan_id,
                    "legend": LEGEND_LINES,
                },
                "root": node.to_dict(include_children=True, schema=schema),
                "warnings": extra_warnings + selection.warnings + freshness.get("warnings", []),
                "collapsed": collapse,
                "local_scan_started": local_scan_started,
                "local_scan_error": local_scan_error,
            }
        else:
            payload = {
                "schema": "sync_tree:v1",
                "root": node.to_dict(include_children=True, schema=schema),
                "root_path": root_path,
                "root_depth": args.depth,
                "root_children_count": node.children_count,
                "root_visible_children_count": node.visible_children_count,
                "config_path": source_path,
                "warnings": extra_warnings + selection.warnings + freshness.get("warnings", []),
                "collapsed": collapse,
                "local_scan_started": local_scan_started,
                "local_scan_error": local_scan_error,
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if args.text_header:
            if schema == SCHEMA_V2:
                for line in render_v2_header(
                    root_path=root_path,
                    depth=args.depth,
                    ctx=policy_ctx,
                    selection=selection,
                    local_scan_id=local_scan_id,
                    collapsed=collapse,
                    local_scan_started=local_scan_started,
                    extra_warnings=extra_warnings,
                    freshness=freshness,
                ):
                    print(line)
            else:
                print("schema: sync_tree:v1")
                print(f"root_path: {root_path}")
                print(f"root_depth: {args.depth}")
                print(f"root_children_count: {node.children_count}")
                print(f"root_visible_children_count: {node.visible_children_count}")
                print(f"config_path: {source_path}")
                if extra_warnings:
                    print("warnings:")
                    for warning in extra_warnings:
                        print(f"- {warning}")
                print(f"collapsed: {collapse}")
                if local_scan_started is not None:
                    print(f"local_scan_started: {local_scan_started}")
                if local_scan_error:
                    print(f"local_scan_error: {local_scan_error}")
                print("")

        lines = render_text_tree(node, schema=schema) if args.text_tree else render_text(node, schema=schema)
        for line in lines:
            print(line)


if __name__ == "__main__":
    main()
