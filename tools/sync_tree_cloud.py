#!/usr/bin/env python3
"""Cloud snapshot helpers for sync_tree (child discovery, path normalization)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

from tools.sync_common import (
    CompositeSnapshot,
    fetch_child_dirs,
    normalize_db_parent_path,
    normalize_path,
)
from ydm import Analyzer


@dataclass
class SnapshotSelection:
    snapshot: CompositeSnapshot
    base_scan_id: int
    folder_updates: dict
    warning: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


def cloud_parent_keys(path: str) -> Tuple[str, ...]:
    """Return parent_path variants used in monitor.db (API vs rclone scans)."""
    normalized = normalize_path(path)
    if normalized == "/":
        return ("", "/")
    relative = normalized.lstrip("/")
    return (relative, normalized)


def infer_dirs_from_files(storage, scan_id: int, parent_path: str) -> List[str]:
    """Infer immediate child directory names when type='dir' rows are missing."""
    keys = cloud_parent_keys(parent_path)
    conn = storage.get_connection()
    children: Set[str] = set()
    try:
        for key in keys:
            rows = conn.execute(
                """
                SELECT name
                FROM files
                WHERE scan_id = ?
                  AND parent_path = ?
                  AND type = 'dir'
                ORDER BY name
                """,
                (scan_id, key),
            ).fetchall()
            for row in rows:
                children.add(row[0])

        normalized = normalize_path(parent_path)
        if normalized == "/":
            prefixes = ("", "/")
        else:
            prefixes = (normalized.lstrip("/"), normalized)

        for prefix in prefixes:
            if prefix == "":
                pattern = "%/%"
                rows = conn.execute(
                    """
                    SELECT DISTINCT
                        CASE
                            WHEN instr(substr(parent_path, 2), '/') > 0
                            THEN substr(substr(parent_path, 2), 1,
                                instr(substr(parent_path, 2), '/') - 1)
                            ELSE substr(parent_path, 2)
                        END AS child
                    FROM files
                    WHERE scan_id = ?
                      AND type = 'file'
                      AND parent_path LIKE '/%'
                      AND parent_path NOT LIKE '/%/%/%'
                    """,
                    (scan_id,),
                ).fetchall()
                for row in rows:
                    if row[0]:
                        children.add(row[0])
                continue

            like_prefix = prefix if prefix.startswith("/") else f"/{prefix}"
            # A range, not `parent_path LIKE '<prefix>/%'`: SQLite turns LIKE
            # into an index range only when LIKE is case-sensitive, and it is
            # not by default. The LIKE form therefore read every row of the
            # scan for every folder the tree visits — 80 295 rows per call on
            # the author's snapshot, and the walk makes one call per folder
            # without `type='dir'` rows. `idx_files_unique` covers
            # (scan_id, parent_path), so the range below seeks instead.
            #
            # '0' is the character after '/', so ['<prefix>/', '<prefix>0')
            # holds exactly the paths under <prefix> — the wider
            # ['<prefix>', '<prefix>0') would also swallow siblings like
            # '<prefix>-old'. The equality case `parent_path = <prefix>` is
            # deliberately not restored: the old query excluded it anyway via
            # `length(parent_path) > length(?)`, and it yields no child name.
            rows = conn.execute(
                """
                SELECT DISTINCT parent_path
                FROM files
                WHERE scan_id = ?
                  AND parent_path >= ?
                  AND parent_path < ?
                  AND type = 'file'
                """,
                (scan_id, f"{like_prefix}/", f"{like_prefix}0"),
            ).fetchall()
            for row in rows:
                rest = (row[0] or "")[len(like_prefix):].lstrip("/")
                name = rest.split("/", 1)[0]
                if name:
                    children.add(name)
    finally:
        conn.close()

    return sorted(children)


def fetch_child_names(storage, scan_id: int, parent_path: str) -> List[str]:
    dirs = fetch_child_dirs(storage, scan_id, parent_path)
    if dirs:
        return dirs
    inferred = infer_dirs_from_files(storage, scan_id, parent_path)
    if inferred:
        return inferred
    for key in cloud_parent_keys(parent_path):
        if key == normalize_db_parent_path(parent_path):
            continue
        alt = fetch_child_dirs(storage, scan_id, key if key.startswith("/") or key == "" else f"/{key}")
        if not alt and key:
            alt = infer_dirs_from_files(storage, scan_id, key if key.startswith("/") else f"/{key}")
        if alt:
            return alt
    return []


def _scan_covers_path(storage, scan_id: int, root_path: str) -> bool:
    root = normalize_path(root_path)
    if root == "/":
        for key in cloud_parent_keys("/"):
            if fetch_child_names(storage, scan_id, "/" if key == "" else f"/{key.lstrip('/')}" if key else "/"):
                return True
            if key == "" and fetch_child_names(storage, scan_id, "/"):
                return True
        return False

    keys = cloud_parent_keys(root)
    conn = storage.get_connection()
    try:
        for key in keys:
            row = conn.execute(
                """
                SELECT 1 FROM files
                WHERE scan_id = ?
                  AND (
                    parent_path = ?
                    OR parent_path LIKE ?
                    OR (parent_path = '' AND name = ?)
                  )
                LIMIT 1
                """,
                (
                    scan_id,
                    key,
                    f"{key.rstrip('/')}/%" if key else "%",
                    root.lstrip("/").split("/")[0] if root != "/" else "",
                ),
            ).fetchone()
            if row:
                return True
        rel = root.lstrip("/")
        if rel:
            row = conn.execute(
                """
                SELECT 1 FROM scan_progress
                WHERE scan_id = ? AND (path = ? OR path = ?)
                LIMIT 1
                """,
                (scan_id, root, f"/{rel}"),
            ).fetchone()
            if row:
                return True
    finally:
        conn.close()
    return bool(fetch_child_names(storage, scan_id, root))


def select_snapshot_for_tree(analyzer: Analyzer, root_path: str) -> SnapshotSelection:
    """Pick composite snapshot; warn when cloud data may not cover root_path."""
    from tools.sync_common import build_composite_snapshot

    warnings: List[str] = []
    snapshot = build_composite_snapshot(analyzer)
    base_id = snapshot.base_scan_id
    storage = analyzer.storage

    root = normalize_path(root_path)
    if not _scan_covers_path(storage, base_id, root):
        for folder, scan_id in sorted(snapshot.folder_updates.items(), key=lambda x: -len(x[0])):
            if root == "/" or folder == root.lstrip("/") or root.startswith(f"/{folder}") or folder.startswith(root.lstrip("/")):
                if _scan_covers_path(storage, scan_id, root):
                    return SnapshotSelection(
                        snapshot=snapshot,
                        base_scan_id=base_id,
                        folder_updates=snapshot.folder_updates,
                        warnings=warnings,
                    )

        conn = storage.get_connection()
        try:
            rows = conn.execute(
                """
                SELECT id FROM scans
                WHERE scan_type = 'cloud' AND status = 'success'
                ORDER BY id DESC
                LIMIT 20
                """
            ).fetchall()
        finally:
            conn.close()

        for (scan_id,) in rows:
            if _scan_covers_path(storage, scan_id, root):
                warnings.append(
                    f"Using cloud scan #{scan_id} for {root} (composite base #{base_id} does not cover this path)"
                )
                return SnapshotSelection(
                    snapshot=CompositeSnapshot(base_scan_id=scan_id, folder_updates={}),
                    base_scan_id=scan_id,
                    folder_updates={},
                    warnings=warnings,
                )

        hint = f"ydm-scan-cloud {root}" if root != "/" else "ydm-scan-cloud"
        warning = (
            f"No cloud scan covers {root}. Tree may be empty. Run: {hint}"
        )
        return SnapshotSelection(
            snapshot=snapshot,
            base_scan_id=base_id,
            folder_updates=snapshot.folder_updates,
            warning=warning,
            warnings=warnings,
        )

    return SnapshotSelection(
        snapshot=snapshot,
        base_scan_id=base_id,
        folder_updates=snapshot.folder_updates,
        warnings=warnings,
    )
