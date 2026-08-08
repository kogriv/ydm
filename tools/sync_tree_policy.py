#!/usr/bin/env python3
"""Policy overlay and display markers for sync_tree v2."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from tools.sync_common import normalize_path
from tools.sync_policy import (
    bisync_filter_path,
    effective_bisync_paths,
    filter_file_hash,
    load_policy,
    policy_paths_by_mode,
)


SCHEMA_V2 = "sync_tree:v2"

LEGEND_LINES = [
    "[B] bidirectional (bisync)",
    "[D] download-only",
    "[L] local orphan (on disk, not in policy)",
    "[.] cloud only",
    "[P] parent of synced path",
    "[X] disabled in policy",
]


@dataclass
class PolicyContext:
    policy: Optional[dict]
    policy_path: str
    bidirectional: List[str]
    download_only: List[str]
    disabled: List[str]
    bisync_filter_path: Optional[str]
    bisync_filter_hash: Optional[str]
    filter_mismatch: bool


def load_policy_context(policy_path: str, local_root: str) -> PolicyContext:
    resolved = os.path.expanduser(policy_path)
    policy = None
    try:
        if os.path.exists(resolved):
            policy = load_policy(resolved)
    except ValueError:
        policy = None

    bisync_path = bisync_filter_path(local_root) if policy else None
    bisync_hash = filter_file_hash(bisync_path) if bisync_path and os.path.exists(bisync_path) else None
    filter_mismatch = False
    if policy and bisync_hash:
        from tools.sync_policy import render_filter_text

        expected = render_filter_text(effective_bisync_paths(policy), check_access=True)
        actual_path = bisync_path or ""
        if os.path.exists(actual_path):
            with open(actual_path, "r") as handle:
                actual = handle.read()
            if actual.strip() != expected.strip():
                filter_mismatch = True

    return PolicyContext(
        policy=policy,
        policy_path=resolved,
        bidirectional=policy_paths_by_mode(policy, "bidirectional") if policy else [],
        download_only=policy_paths_by_mode(policy, "download_only") if policy else [],
        disabled=policy_paths_by_mode(policy, "disabled") if policy else [],
        bisync_filter_path=bisync_path,
        bisync_filter_hash=bisync_hash,
        filter_mismatch=filter_mismatch,
    )


def rel_path_from_cloud(path: str) -> str:
    normalized = normalize_path(path)
    return "" if normalized == "/" else normalized.lstrip("/")


def policy_entry_for_path(path: str, policy: Optional[dict]) -> Tuple[Optional[str], Optional[dict]]:
    if not policy:
        return None, None
    rel = rel_path_from_cloud(path)
    paths = policy.get("paths") or {}
    best_key = None
    best_meta = None
    for entry, meta in paths.items():
        if rel == entry or rel.startswith(entry + "/"):
            if best_key is None or len(entry) > len(best_key):
                best_key = entry
                best_meta = meta
    return best_key, best_meta


def policy_mode_for_path(path: str, policy: Optional[dict]) -> Optional[str]:
    _, meta = policy_entry_for_path(path, policy)
    if not meta:
        return None
    return meta.get("mode")


def path_in_policy(path: str, policy: Optional[dict]) -> bool:
    key, _ = policy_entry_for_path(path, policy)
    return key is not None and rel_path_from_cloud(path) == key


def local_dir_exists(local_root: str, path: str) -> bool:
    rel = rel_path_from_cloud(path)
    local = os.path.join(os.path.expanduser(local_root), rel) if rel else os.path.expanduser(local_root)
    return os.path.isdir(local)


def local_state(
    *,
    policy_mode: Optional[str],
    in_policy: bool,
    cloud_count: int,
    local_count: int,
    local_root: str,
    path: str,
) -> str:
    has_local_dir = local_dir_exists(local_root, path)
    materialized = local_count > 0 or has_local_dir

    if in_policy and policy_mode == "disabled":
        return "disabled"
    if in_policy and policy_mode in {"bidirectional", "download_only"}:
        if local_count == 0 and not has_local_dir:
            return "missing"
        if cloud_count > 0 and local_count < cloud_count:
            return "partial"
        return "materialized"
    if materialized and not in_policy:
        return "orphan"
    if cloud_count > 0 and local_count == 0:
        return "cloud_only"
    if local_count > 0:
        return "materialized"
    return "cloud_only"


def display_marker(
    *,
    policy_mode: Optional[str],
    in_policy: bool,
    local_state_value: str,
    has_synced_descendant: bool,
    v1_sync_status: str,
) -> str:
    if in_policy and policy_mode == "disabled":
        return "[X]"
    if in_policy and policy_mode == "bidirectional":
        if local_state_value == "missing":
            return "[B?]"
        if local_state_value == "partial":
            return "[B~]"
        return "[B]"
    if in_policy and policy_mode == "download_only":
        if local_state_value in {"missing", "cloud_only"}:
            return "[D?]"
        return "[D]"
    if has_synced_descendant or v1_sync_status == "partial":
        return "[P]"
    if local_state_value == "orphan":
        return "[L]"
    if local_state_value == "materialized":
        return "[L]"
    return "[.]"


def policy_summary_line(ctx: PolicyContext) -> str:
    parts: List[str] = []
    for entry in ctx.bidirectional:
        parts.append(f"{entry}[B]")
    for entry in ctx.download_only:
        parts.append(f"{entry}[D]")
    for entry in ctx.disabled:
        parts.append(f"{entry}[X]")
    return ", ".join(parts) if parts else "(no paths in policy)"


def policy_paths_set(ctx: PolicyContext) -> Set[str]:
    result: Set[str] = set()
    for group in (ctx.bidirectional, ctx.download_only, ctx.disabled):
        result.update(group)
    return result


def is_under_policy_path(path: str, policy_paths: Set[str]) -> bool:
    rel = rel_path_from_cloud(path)
    for entry in policy_paths:
        if rel == entry or rel.startswith(entry + "/"):
            return True
    return False
