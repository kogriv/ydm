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
                # The root is the one folder answered by a first-segment
                # extraction rather than a range, and it used to carry
                # `AND parent_path NOT LIKE '/%/%/%'` as well — so a top-level
                # folder whose files all sit three or more levels down was
                # invisible here, and since the walk descends into what it
                # lists, the whole subtree went missing. In `orphans` that
                # reads as a local copy with no cloud counterpart. Dropping
                # the bound costs nothing: LIKE is case-insensitive by
                # default, so SQLite never turned either form into an index
                # range and both read every row of the scan.
                # `ltrim` rather than `substr(parent_path, 2)`: the second
                # form assumes a leading slash, so a scan that wrote
                # `Books/Math` produced no root listing at all — an empty
                # tree, not a wrong one.
                rows = conn.execute(
                    """
                    SELECT DISTINCT
                        CASE
                            WHEN instr(relative, '/') > 0
                            THEN substr(relative, 1, instr(relative, '/') - 1)
                            ELSE relative
                        END AS child
                    FROM (
                        SELECT ltrim(parent_path, '/') AS relative
                        FROM files
                        WHERE scan_id = ?
                          AND type = 'file'
                          AND parent_path IS NOT NULL
                    )
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


def index_key(path: Optional[str]) -> str:
    """One spelling for a folder, whichever way the scan wrote it.

    `parent_path` is stored with a leading slash by API scans and without one
    by rclone scans, and the root is `''` in one and `'/'` in the other. Four
    spellings, one folder — which is why the per-node lookup below used to ask
    the same question up to four times.
    """
    normalized = normalize_path(path)
    return "" if normalized == "/" else normalized.lstrip("/")


class ChildIndex:
    """Every folder's children, read once per scan instead of once per node.

    The walk asked the database "what is under this folder" for each node it
    visited, and the answer took up to ten queries: one for `type='dir'` rows,
    four to infer names from file paths under both spellings, and five more in
    the fallback loop when the folder had neither. On the Android device that
    came to 50 813 queries for 3 801 nodes on 2026-08-28 — 13.4 per node, 67%
    of the run — where a single query costs half a millisecond because proot
    bills every system call through ptrace. See `tasks/ydm_menu/BACKLOG.md`,
    Phase 12.

    But which folders exist, and what each one holds, is a property of the
    *scan*, not of the node asking. Two queries per scan read it whole: the
    `type='dir'` rows, and the distinct `parent_path` of every file. A folder
    named in a path is a folder that exists, so the second query yields every
    directory the scan ever touched, including those with no row of their own.

    A composite snapshot serves different subtrees from different scans, so
    the index is built per `scan_id`, lazily — a walk that never reaches a
    subtree never pays for the scan behind it.

    The answer is the same one `fetch_child_dirs` + `infer_dirs_from_files`
    gave, in the same order and with the same precedence: dir rows recorded
    under the exact spelling the walk asks with win outright, and only when
    there are none do inferred names join in.
    """

    def __init__(self, storage) -> None:
        self._storage = storage
        self._scans: dict = {}

    def children(self, scan_id: int, parent_path: str) -> List[str]:
        dirs_exact, dirs_by_key, inferred = self._for_scan(scan_id)
        recorded = dirs_exact.get(normalize_db_parent_path(parent_path))
        if recorded:
            return sorted(recorded)
        key = index_key(parent_path)
        return sorted(dirs_by_key.get(key, frozenset()) | inferred.get(key, frozenset()))

    def _for_scan(self, scan_id: int):
        cached = self._scans.get(scan_id)
        if cached is None:
            cached = self._read(scan_id)
            self._scans[scan_id] = cached
        return cached

    def _read(self, scan_id: int):
        dirs_exact: dict = {}
        dirs_by_key: dict = {}
        inferred: dict = {}
        conn = self._storage.get_connection()
        try:
            for parent_path, name in conn.execute(
                """
                SELECT parent_path, name
                FROM files
                WHERE scan_id = ? AND type = 'dir'
                """,
                (scan_id,),
            ):
                # Keyed by the stored string as well as the normalized one:
                # `fetch_child_dirs` matched `parent_path` exactly, and a scan
                # that wrote the other spelling fell through to inference. That
                # distinction decides precedence, so it has to survive.
                dirs_exact.setdefault("" if parent_path is None else parent_path, set()).add(name)
                dirs_by_key.setdefault(index_key(parent_path), set()).add(name)

            for (parent_path,) in conn.execute(
                """
                SELECT DISTINCT parent_path
                FROM files
                WHERE scan_id = ? AND type = 'file'
                """,
                (scan_id,),
            ):
                key = index_key(parent_path)
                if not key:
                    continue
                # `/a/b/c` says that `/` holds `a`, `/a` holds `b`, and `/a/b`
                # holds `c`. Walking the segments records all three at once,
                # which is what replaces the per-node range queries.
                segments = key.split("/")
                for position, segment in enumerate(segments):
                    inferred.setdefault("/".join(segments[:position]), set()).add(segment)
        finally:
            conn.close()
        return dirs_exact, dirs_by_key, inferred


def fetch_child_names(
    storage, scan_id: int, parent_path: str, index: Optional[ChildIndex] = None
) -> List[str]:
    """Immediate child folder names of `parent_path` in one scan.

    `index` is for callers that ask about many folders — pass one `ChildIndex`
    for the whole walk and the scan is read twice instead of per node. Callers
    that ask about a single folder should leave it off: building the index
    reads every row of the scan, which is the wrong trade for one question.
    """
    if index is not None:
        return index.children(scan_id, parent_path)

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
