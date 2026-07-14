#!/usr/bin/env python3
"""
Repair files that exist in cloud storage with names Android shared storage
cannot create under /sdcard.

This intentionally does not run a broad `rclone copy`. It copies only files
whose cloud path contains Android-problem characters, mapping those characters
to full-width Unicode lookalikes in the local mirror.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List


ROOT_DIR = Path(__file__).resolve().parents[1]
ANDROID_NAME_MAP = str.maketrans({
    "<": "＜",
    ">": "＞",
    ":": "：",
    '"': "＂",
    "|": "｜",
    "?": "？",
    "*": "＊",
    "\\": "＼",
})


@dataclass
class RepairItem:
    parent_path: str
    name: str
    size: int

    @property
    def remote_rel(self) -> str:
        return f"{self.parent_path.strip('/')}/{self.name}"

    def local_path(self, local_root: str) -> str:
        parts = [part.translate(ANDROID_NAME_MAP) for part in self.remote_rel.split("/")]
        return os.path.join(os.path.expanduser(local_root), *parts)


def latest_cloud_scan_id(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT id FROM scans
            WHERE scan_type = 'cloud' AND status = 'success'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit("No successful cloud scan found")
    return int(row[0])


def load_items(db_path: str, scan_id: int, path: str) -> List[RepairItem]:
    normalized = "/" + path.strip("/")
    like_path = normalized + "%"
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT parent_path, name, COALESCE(size, 0)
            FROM files
            WHERE scan_id = ?
              AND type = 'file'
              AND (parent_path = ? OR parent_path LIKE ?)
              AND (
                parent_path GLOB '*[<>:"|?*\\]*'
                OR name GLOB '*[<>:"|?*\\]*'
              )
            ORDER BY parent_path, name
            """,
            (scan_id, normalized, like_path),
        ).fetchall()
    finally:
        conn.close()
    return [RepairItem(parent, name, int(size)) for parent, name, size in rows]


def needs_copy(item: RepairItem, local_root: str, force: bool) -> bool:
    if force:
        return True
    local = item.local_path(local_root)
    return not os.path.exists(local) or os.path.getsize(local) != item.size


def copy_item(item: RepairItem, remote: str, local_root: str) -> int:
    local = item.local_path(local_root)
    os.makedirs(os.path.dirname(local), exist_ok=True)
    cmd = ["rclone", "copyto", f"{remote}:{item.remote_rel}", local, "-P"]
    result = subprocess.run(cmd)
    return result.returncode


def render_plan(items: Iterable[RepairItem], local_root: str, force: bool) -> List[RepairItem]:
    selected: List[RepairItem] = []
    for item in items:
        local = item.local_path(local_root)
        status = "copy" if needs_copy(item, local_root, force) else "ok"
        print(f"{status}: {item.size} {item.remote_rel}")
        print(f"  -> {local}")
        if status == "copy":
            selected.append(item)
    return selected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair Android-incompatible names in the local rclone mirror")
    parser.add_argument("--db-path", default=str(ROOT_DIR / "monitor.db"))
    parser.add_argument("--local-root", default="/sdcard/Download/ya_disk")
    parser.add_argument("--remote", default="yandex")
    parser.add_argument("--path", required=True, help="Cloud path prefix, e.g. /pro/agents")
    parser.add_argument("--scan-id", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true", help="Copy even when sanitized local file already matches size")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    scan_id = args.scan_id or latest_cloud_scan_id(args.db_path)
    print(f"schema: repair_android_names:v1")
    print(f"scan_id: {scan_id}")
    print(f"path: {args.path}")
    print(f"apply: {args.apply}")
    print("")

    items = load_items(args.db_path, scan_id, args.path)
    selected = render_plan(items, args.local_root, args.force)
    print("")
    print(f"problem_paths: {len(items)}")
    print(f"to_copy: {len(selected)}")

    if not args.apply:
        return

    failures = 0
    for item in selected:
        rc = copy_item(item, args.remote, args.local_root)
        if rc != 0:
            failures += 1
            print(f"failed: rc={rc} {item.remote_rel}")
    print(f"finished_at: {datetime.now().isoformat()}")
    print(f"failures: {failures}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
