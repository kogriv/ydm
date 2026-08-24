#!/usr/bin/env python3
"""Discover local orphan folders for YDM menu."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from tools.sync_common import create_storage, get_latest_successful_scan_id
from tools.sync_policy import effective_download_paths, load_policy
from tools.sync_tree import (
    apply_policy_overlay,
    apply_sync_percent,
    build_tree,
    compute_status_whitelist,
)
from tools.sync_tree_cloud import select_snapshot_for_tree
from tools.sync_tree_policy import (
    load_policy_context,
    path_in_policy,
    synced_policy_paths_set,
)
from ydm import Analyzer

SCHEMA = "ydm_menu_orphans:v1"


@dataclass
class OrphanEntry:
    cloud_path: str
    rel_path: str
    local_bytes: int
    cloud_file_count: int
    display_marker: str

    def to_dict(self) -> dict:
        return {
            "cloud_path": self.cloud_path,
            "rel_path": self.rel_path,
            "local_bytes": self.local_bytes,
            "cloud_file_count": self.cloud_file_count,
            "display_marker": self.display_marker,
        }


def _dir_size(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _format_bytes(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    if num < 1024 * 1024 * 1024:
        return f"{num / (1024 * 1024):.1f} MB"
    return f"{num / (1024 * 1024 * 1024):.2f} GB"


def format_orphan_label(entry: OrphanEntry) -> str:
    return (
        f"{entry.rel_path}  ({_format_bytes(entry.local_bytes)}, "
        f"cloud files: {entry.cloud_file_count})"
    )


def _collect_orphans_from_node(node, local_root: str, out: List[OrphanEntry]) -> None:
    if node.display_marker == "[L]" and not node.in_policy:
        rel = node.path.lstrip("/")
        if rel:
            local_path = os.path.join(os.path.expanduser(local_root), rel)
            out.append(
                OrphanEntry(
                    cloud_path=node.path,
                    rel_path=rel,
                    local_bytes=_dir_size(local_path),
                    cloud_file_count=node.cloud_file_count,
                    display_marker=node.display_marker,
                )
            )
    for child in node.children:
        _collect_orphans_from_node(child, local_root, out)


def cloud_scan_available(db_path: str) -> bool:
    """True when db_path holds at least one successful cloud scan.

    A fresh checkout has no monitor.db at all (it is gitignored), and an
    interrupted first run leaves one without the schema. Both used to reach
    the tree builder and surface as `sqlite3.OperationalError: no such table`.
    """
    import sqlite3

    resolved = os.path.expanduser(db_path)
    if not os.path.exists(resolved):
        return False
    try:
        conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM scans WHERE scan_type = 'cloud' AND status = 'success' LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return row is not None


def list_orphan_paths(
    db_path: str,
    local_root: str,
    policy_path: str,
    *,
    root: str = "/",
    max_depth: int = 5,
) -> List[OrphanEntry]:
    policy_ctx = load_policy_context(policy_path, local_root)
    storage = create_storage(db_path)
    analyzer = Analyzer(storage)
    selection = select_snapshot_for_tree(analyzer, root)
    snapshot = selection.snapshot

    membership = set()
    if policy_ctx.policy:
        membership = set(effective_download_paths(policy_ctx.policy))

    node = build_tree(analyzer, snapshot, root, max_depth)
    compute_status_whitelist(
        node,
        membership,
        collapse=False,
        synced_paths=synced_policy_paths_set(policy_ctx) if policy_ctx.policy else None,
        local_root=local_root,
    )
    local_scan_id = get_latest_successful_scan_id(storage, "local")
    apply_sync_percent(node, analyzer, snapshot, local_scan_id, local_root)
    apply_policy_overlay(node, policy_ctx, local_root)

    orphans: List[OrphanEntry] = []
    _collect_orphans_from_node(node, local_root, orphans)

    policy = load_policy(policy_path) if os.path.exists(policy_path) else None
    deduped: dict[str, OrphanEntry] = {}
    for entry in orphans:
        if policy and path_in_policy(entry.cloud_path, policy):
            continue
        deduped[entry.rel_path] = entry
    return sorted(deduped.values(), key=lambda e: e.rel_path.lower())


def orphans_to_json(entries: List[OrphanEntry]) -> dict:
    return {
        "schema": SCHEMA,
        "count": len(entries),
        "orphans": [e.to_dict() for e in entries],
    }
