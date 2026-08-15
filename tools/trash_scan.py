#!/usr/bin/env python3
"""Scan and restore a Yandex Disk Trash subtree using a YDM-style SQLite DB."""
from __future__ import annotations

import argparse
import configparser
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ydm import StorageManager  # noqa: E402


DEFAULT_TRASH_ROOT = "trash:/Books_25639b9fb1cee52a5b58811baffa033cbf2896a3"
DEFAULT_RESTORE_ROOT = "/Books"
DEFAULT_DB_PATH = str(ROOT / "var" / "trash_books.db")
API_URL = "https://cloud-api.yandex.net/v1/disk/trash/resources"
RESTORE_URL = "https://cloud-api.yandex.net/v1/disk/trash/resources/restore"


def _iso_to_datetime(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return datetime.now()


def normalize_restore_path(path: str) -> str:
    if not path:
        return "/"
    return "/" + path.strip("/")


def join_restore(parent: str, name: str) -> str:
    parent = normalize_restore_path(parent)
    if parent == "/":
        return "/" + name
    return parent.rstrip("/") + "/" + name


def parent_and_name(path: str) -> tuple[str, str]:
    path = normalize_restore_path(path)
    if path == "/":
        return "", ""
    parent, _, name = path.rpartition("/")
    return parent or "/", name


def relative_trash_path(root: str, child: str) -> str:
    root = root.rstrip("/")
    if child == root:
        return ""
    prefix = root + "/"
    if child.startswith(prefix):
        return child[len(prefix) :]
    return child.split(":/", 1)[-1].lstrip("/")


def restore_path_for(trash_root: str, restore_root: str, trash_path: str) -> str:
    rel = relative_trash_path(trash_root, trash_path)
    if not rel:
        return normalize_restore_path(restore_root)
    return join_restore(restore_root, rel)


def token_from_rclone(remote: str = "yandex") -> str:
    config_path = os.path.expanduser("~/.config/rclone/rclone.conf")
    parser = configparser.ConfigParser()
    parser.read(config_path)
    section = remote.rstrip(":")
    if section not in parser:
        raise SystemExit(f"rclone remote not found in {config_path}: {section}")
    raw = parser[section].get("token")
    if not raw:
        raise SystemExit(f"rclone remote has no token: {section}")
    token = json.loads(raw)
    access = token.get("access_token")
    if not access:
        raise SystemExit(f"rclone token has no access_token: {section}")
    return access


class YandexTrashClient:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"OAuth {token}",
            "Accept": "application/json",
        }

    def get_resources(self, trash_path: str, limit: int = 1000, offset: int = 0) -> Dict[str, object]:
        params = {
            "path": trash_path,
            "limit": str(limit),
            "offset": str(offset),
            "fields": ",".join(
                [
                    "name",
                    "path",
                    "type",
                    "_embedded.total",
                    "_embedded.items.name",
                    "_embedded.items.path",
                    "_embedded.items.type",
                    "_embedded.items.size",
                    "_embedded.items.md5",
                    "_embedded.items.created",
                    "_embedded.items.modified",
                    "_embedded.items.deleted",
                    "_embedded.items.origin_path",
                ]
            ),
        }
        url = API_URL + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.load(response)
        return data.get("_embedded", {"items": [], "total": 0})

    def restore(self, trash_path: str, overwrite: bool = False) -> tuple[int, str]:
        params = {
            "path": trash_path,
            "overwrite": "true" if overwrite else "false",
        }
        url = RESTORE_URL + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=self.headers, method="PUT")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def get_operation(self, href: str) -> Dict[str, object]:
        request = urllib.request.Request(href, headers=self.headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)


def ensure_extra_schema(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trash_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                restore_path TEXT NOT NULL,
                restore_parent_path TEXT NOT NULL,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                size INTEGER,
                md5 TEXT,
                trash_path TEXT NOT NULL,
                origin_path TEXT,
                deleted DATETIME,
                created DATETIME,
                modified DATETIME,
                FOREIGN KEY(scan_id) REFERENCES scans(id),
                UNIQUE(scan_id, trash_path)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trash_entries_scan_restore ON trash_entries(scan_id, restore_path)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trash_entries_scan_type ON trash_entries(scan_id, type)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS restore_ops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                trash_entry_id INTEGER NOT NULL,
                restore_path TEXT NOT NULL,
                trash_path TEXT NOT NULL,
                status TEXT NOT NULL,
                http_status INTEGER,
                response TEXT,
                attempted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(scan_id) REFERENCES scans(id),
                FOREIGN KEY(trash_entry_id) REFERENCES trash_entries(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_restore_ops_entry_status ON restore_ops(trash_entry_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_restore_ops_scan_status ON restore_ops(scan_id, status)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS restore_root_ops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                trash_root TEXT NOT NULL,
                restore_root TEXT NOT NULL,
                operation_href TEXT,
                status TEXT NOT NULL,
                response TEXT,
                attempted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(scan_id) REFERENCES scans(id)
            )
            """
        )
        conn.commit()


def save_trash_entries(db_path: str, rows: Iterable[tuple]) -> None:
    rows = list(rows)
    if not rows:
        return
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO trash_entries
            (scan_id, restore_path, restore_parent_path, name, type, size, md5,
             trash_path, origin_path, deleted, created, modified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()


def scan_trash(args: argparse.Namespace) -> dict:
    db_path = os.path.expanduser(args.db_path)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    storage = StorageManager(db_path, use_temp_storage=False)
    ok, message = storage.init_db()
    if not ok:
        raise SystemExit(message)
    ensure_extra_schema(db_path)

    token = token_from_rclone(args.remote)
    client = YandexTrashClient(token)

    if args.resume:
        if not args.scan_id:
            raise SystemExit("--resume requires --scan-id")
        scan_id = args.scan_id
        queue = storage.get_pending_folders(scan_id)
    else:
        scan_id = storage.start_scan("trash")
        storage.update_folder_status(scan_id, args.trash_root, "pending")
        queue = [args.trash_root]

    started = time.time()
    files_count = 0
    dirs_count = 0
    total_items = 0
    errors: List[dict] = []
    visited = set()

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        storage.update_folder_status(scan_id, current, "in_progress")
        offset = storage.get_folder_offset(scan_id, current)
        total = 0

        if args.progress:
            print(
                json.dumps(
                    {
                        "status": "folder",
                        "scan_id": scan_id,
                        "current": current,
                        "offset": offset,
                        "stats": storage.get_scan_stats(scan_id),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        while True:
            try:
                embedded = client.get_resources(current, limit=args.limit, offset=offset)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                errors.append({"path": current, "code": exc.code, "error": body})
                storage.update_folder_status(scan_id, current, "error")
                break
            except Exception as exc:
                errors.append({"path": current, "error": str(exc)})
                storage.update_folder_status(scan_id, current, "error")
                break

            items = list(embedded.get("items") or [])
            total = int(embedded.get("total") or 0)
            file_rows = []
            trash_rows = []

            for item in items:
                trash_path = item.get("path") or (current.rstrip("/") + "/" + item["name"])
                restore_path = restore_path_for(args.trash_root, args.restore_root, trash_path)
                parent_path, name = parent_and_name(restore_path)
                item_type = item.get("type") or "file"
                size = int(item.get("size") or 0)
                md5 = item.get("md5")
                created = _iso_to_datetime(item.get("created"))
                modified = _iso_to_datetime(item.get("modified"))
                deleted = _iso_to_datetime(item.get("deleted")) if item.get("deleted") else None

                file_rows.append((scan_id, parent_path, name, item_type, size, md5, created, modified))
                trash_rows.append(
                    (
                        scan_id,
                        restore_path,
                        parent_path,
                        name,
                        item_type,
                        size,
                        md5,
                        trash_path,
                        item.get("origin_path"),
                        deleted,
                        created,
                        modified,
                    )
                )

                total_items += 1
                if item_type == "dir":
                    dirs_count += 1
                    queue.append(trash_path)
                    storage.update_folder_status(scan_id, trash_path, "pending")
                else:
                    files_count += 1

            storage.save_files_batch(file_rows)
            save_trash_entries(db_path, trash_rows)

            offset += len(items)
            storage.save_checkpoint(scan_id, current, offset, total)
            if offset >= total or not items:
                storage.update_folder_status(scan_id, current, "completed")
                break

    duration = time.time() - started
    final_status = "success" if not errors and storage.get_pending_folders_count(scan_id) == 0 else "partial"
    storage.finish_scan(scan_id, final_status, duration)

    return {
        "scan_id": scan_id,
        "status": final_status,
        "duration_sec": duration,
        "items_seen_this_run": total_items,
        "files_seen_this_run": files_count,
        "dirs_seen_this_run": dirs_count,
        "progress": storage.get_scan_stats(scan_id),
        "errors": errors[:20],
        "errors_count": len(errors),
    }


def latest_scan_id(db_path: str) -> Optional[int]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT id FROM scans WHERE scan_type='trash' ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def summary(args: argparse.Namespace) -> dict:
    db_path = os.path.expanduser(args.db_path)
    scan_id = args.scan_id or latest_scan_id(db_path)
    if not scan_id:
        raise SystemExit("No trash scan found")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        counts = conn.execute(
            """
            SELECT
              COUNT(*) AS rows,
              SUM(CASE WHEN type='file' THEN 1 ELSE 0 END) AS files,
              SUM(CASE WHEN type='dir' THEN 1 ELSE 0 END) AS dirs,
              COALESCE(SUM(CASE WHEN type='file' THEN size ELSE 0 END), 0) AS bytes
            FROM trash_entries WHERE scan_id=?
            """,
            (scan_id,),
        ).fetchone()
        top = conn.execute(
            """
            SELECT restore_path, type, size
            FROM trash_entries
            WHERE scan_id=? AND restore_parent_path=?
            ORDER BY type DESC, restore_path
            LIMIT ?
            """,
            (scan_id, normalize_restore_path(args.restore_root), args.limit),
        ).fetchall()
        progress = conn.execute(
            """
            SELECT status, COUNT(*) FROM scan_progress
            WHERE scan_id=?
            GROUP BY status
            ORDER BY status
            """,
            (scan_id,),
        ).fetchall()
    return {
        "scan": dict(scan) if scan else None,
        "counts": dict(counts),
        "progress": {row[0]: row[1] for row in progress},
        "top_level_sample": [dict(row) for row in top],
    }


def compare_monitor(args: argparse.Namespace) -> dict:
    db_path = os.path.expanduser(args.db_path)
    scan_id = args.scan_id or latest_scan_id(db_path)
    if not scan_id:
        raise SystemExit("No trash scan found")

    monitor_db = os.path.expanduser(args.monitor_db)
    monitor_scan_id = args.monitor_scan_id

    with sqlite3.connect(db_path) as conn:
        conn.execute(f"ATTACH DATABASE '{monitor_db}' AS monitor")
        trash_counts = conn.execute(
            """
            SELECT COUNT(*), SUM(type='file'), SUM(type='dir'), COALESCE(SUM(CASE WHEN type='file' THEN size ELSE 0 END),0)
            FROM trash_entries WHERE scan_id=?
            """,
            (scan_id,),
        ).fetchone()
        monitor_counts = conn.execute(
            """
            SELECT COUNT(*), SUM(type='file'), SUM(type='dir'), COALESCE(SUM(CASE WHEN type='file' THEN size ELSE 0 END),0)
            FROM monitor.files
            WHERE scan_id=? AND (parent_path=? OR parent_path LIKE ?)
            """,
            (monitor_scan_id, args.restore_root.rstrip("/"), args.restore_root.rstrip("/") + "/%"),
        ).fetchone()
        missing_in_trash = conn.execute(
            """
            SELECT m.parent_path || '/' || m.name AS path
            FROM monitor.files m
            LEFT JOIN trash_entries t
              ON t.scan_id=? AND t.restore_parent_path=m.parent_path AND t.name=m.name
            WHERE m.scan_id=?
              AND (m.parent_path=? OR m.parent_path LIKE ?)
              AND t.id IS NULL
            ORDER BY path
            LIMIT ?
            """,
            (scan_id, monitor_scan_id, args.restore_root.rstrip("/"), args.restore_root.rstrip("/") + "/%", args.limit),
        ).fetchall()
        extra_in_trash = conn.execute(
            """
            SELECT t.restore_path
            FROM trash_entries t
            LEFT JOIN monitor.files m
              ON m.scan_id=? AND m.parent_path=t.restore_parent_path AND m.name=t.name
            WHERE t.scan_id=? AND m.id IS NULL
            ORDER BY t.restore_path
            LIMIT ?
            """,
            (monitor_scan_id, scan_id, args.limit),
        ).fetchall()
        conn.execute("DETACH DATABASE monitor")

    return {
        "trash_scan_id": scan_id,
        "monitor_scan_id": monitor_scan_id,
        "trash_counts": {
            "rows": trash_counts[0],
            "files": trash_counts[1],
            "dirs": trash_counts[2],
            "bytes": trash_counts[3],
        },
        "monitor_counts": {
            "rows": monitor_counts[0],
            "files": monitor_counts[1],
            "dirs": monitor_counts[2],
            "bytes": monitor_counts[3],
        },
        "missing_in_trash_sample": [row[0] for row in missing_in_trash],
        "extra_in_trash_sample": [row[0] for row in extra_in_trash],
    }


def _restore_order_clause(order: str) -> str:
    if order == "path":
        return "restore_path COLLATE NOCASE, id"
    if order == "size-asc":
        return "size ASC, restore_path COLLATE NOCASE, id"
    if order == "size-desc":
        return "size DESC, restore_path COLLATE NOCASE, id"
    raise ValueError(f"Unsupported order: {order}")


def restore_plan(args: argparse.Namespace) -> dict:
    db_path = os.path.expanduser(args.db_path)
    ensure_extra_schema(db_path)
    scan_id = args.scan_id or latest_scan_id(db_path)
    if not scan_id:
        raise SystemExit("No trash scan found")

    restore_root = normalize_restore_path(args.restore_root)
    prefix = normalize_restore_path(args.prefix) if args.prefix else restore_root
    like_prefix = prefix.rstrip("/") + "/%"
    order_by = _restore_order_clause(args.order)

    where = [
        "e.scan_id=?",
        "e.type='file'",
        "(e.restore_path=? OR e.restore_path LIKE ?)",
    ]
    params: List[object] = [scan_id, prefix, like_prefix]
    if args.retry_failed:
        skip_success_sql = ""
    else:
        skip_success_sql = """
          AND NOT EXISTS (
            SELECT 1 FROM restore_ops ro
            WHERE ro.trash_entry_id=e.id AND ro.status='success'
          )
        """

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute(
            f"""
            SELECT COUNT(*) AS files, COALESCE(SUM(e.size),0) AS bytes
            FROM trash_entries e
            WHERE {' AND '.join(where)}
            {skip_success_sql}
            """,
            params,
        ).fetchone()
        done = conn.execute(
            """
            SELECT COUNT(DISTINCT e.id) AS files, COALESCE(SUM(e.size),0) AS bytes
            FROM trash_entries e
            JOIN restore_ops ro ON ro.trash_entry_id=e.id AND ro.status='success'
            WHERE e.scan_id=? AND e.type='file'
              AND (e.restore_path=? OR e.restore_path LIKE ?)
            """,
            [scan_id, prefix, like_prefix],
        ).fetchone()
        failures = conn.execute(
            """
            SELECT ro.http_status, COUNT(*) AS count
            FROM restore_ops ro
            JOIN trash_entries e ON e.id=ro.trash_entry_id
            WHERE e.scan_id=? AND e.type='file'
              AND (e.restore_path=? OR e.restore_path LIKE ?)
              AND ro.status='error'
            GROUP BY ro.http_status
            ORDER BY count DESC
            """,
            [scan_id, prefix, like_prefix],
        ).fetchall()
        sample = conn.execute(
            f"""
            SELECT e.restore_path, e.size, e.trash_path
            FROM trash_entries e
            WHERE {' AND '.join(where)}
            {skip_success_sql}
            ORDER BY {order_by}
            LIMIT ?
            """,
            params + [args.limit],
        ).fetchall()

    return {
        "scan_id": scan_id,
        "prefix": prefix,
        "pending_files": total["files"],
        "pending_bytes": total["bytes"],
        "already_restored_files": done["files"],
        "already_restored_bytes": done["bytes"],
        "error_attempts_by_http_status": [dict(row) for row in failures],
        "sample": [dict(row) for row in sample],
    }


def restore_files(args: argparse.Namespace) -> dict:
    db_path = os.path.expanduser(args.db_path)
    ensure_extra_schema(db_path)
    scan_id = args.scan_id or latest_scan_id(db_path)
    if not scan_id:
        raise SystemExit("No trash scan found")
    if args.apply and args.yes != "RESTORE":
        raise SystemExit("--apply requires --yes RESTORE")

    restore_root = normalize_restore_path(args.restore_root)
    prefix = normalize_restore_path(args.prefix) if args.prefix else restore_root
    like_prefix = prefix.rstrip("/") + "/%"
    order_by = _restore_order_clause(args.order)
    token = token_from_rclone(args.remote) if args.apply else ""
    client = YandexTrashClient(token) if args.apply else None

    where = [
        "e.scan_id=?",
        "e.type='file'",
        "(e.restore_path=? OR e.restore_path LIKE ?)",
    ]
    params: List[object] = [scan_id, prefix, like_prefix]
    if not args.retry_failed:
        skip_success_sql = """
          AND NOT EXISTS (
            SELECT 1 FROM restore_ops ro
            WHERE ro.trash_entry_id=e.id AND ro.status='success'
          )
        """
    else:
        skip_success_sql = ""

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT e.id, e.restore_path, e.trash_path, e.size
            FROM trash_entries e
            WHERE {' AND '.join(where)}
            {skip_success_sql}
            ORDER BY {order_by}
            LIMIT ?
            """,
            params + [args.limit],
        ).fetchall()

    attempted = 0
    successes = 0
    errors: List[dict] = []
    dry_run_sample: List[dict] = []
    started = time.time()

    for row in rows:
        if not args.apply:
            dry_run_sample.append(
                {
                    "restore_path": row["restore_path"],
                    "trash_path": row["trash_path"],
                    "size": row["size"],
                }
            )
            continue

        assert client is not None
        attempted += 1
        http_status, body = client.restore(row["trash_path"], overwrite=args.overwrite)
        status = "success" if http_status in (200, 201, 202, 204) else "error"
        if status == "success":
            successes += 1
        else:
            errors.append(
                {
                    "restore_path": row["restore_path"],
                    "trash_path": row["trash_path"],
                    "http_status": http_status,
                    "response": body[:1000],
                }
            )

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO restore_ops
                (scan_id, trash_entry_id, restore_path, trash_path, status, http_status, response)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (scan_id, row["id"], row["restore_path"], row["trash_path"], status, http_status, body[:4000]),
            )
            conn.commit()

        if args.progress:
            print(
                json.dumps(
                    {
                        "status": status,
                        "attempted": attempted,
                        "successes": successes,
                        "errors": len(errors),
                        "restore_path": row["restore_path"],
                        "http_status": http_status,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        if status == "error" and args.stop_on_error:
            break
        if args.sleep_sec:
            time.sleep(args.sleep_sec)

    return {
        "scan_id": scan_id,
        "prefix": prefix,
        "apply": args.apply,
        "limit": args.limit,
        "selected": len(rows),
        "attempted": attempted,
        "successes": successes,
        "errors_count": len(errors),
        "errors_sample": errors[:20],
        "dry_run_sample": dry_run_sample[:20],
        "duration_sec": time.time() - started,
    }


def restore_root(args: argparse.Namespace) -> dict:
    db_path = os.path.expanduser(args.db_path)
    ensure_extra_schema(db_path)
    scan_id = args.scan_id or latest_scan_id(db_path)
    if not scan_id:
        raise SystemExit("No trash scan found")
    if args.apply and args.yes != "RESTORE_ROOT":
        raise SystemExit("--apply requires --yes RESTORE_ROOT")

    trash_root = args.trash_root
    restore_root_path = normalize_restore_path(args.restore_root)
    if not args.apply:
        return {
            "scan_id": scan_id,
            "apply": False,
            "trash_root": trash_root,
            "restore_root": restore_root_path,
            "would_call": "PUT /v1/disk/trash/resources/restore",
            "overwrite": args.overwrite,
        }

    client = YandexTrashClient(token_from_rclone(args.remote))
    http_status, body = client.restore(trash_root, overwrite=args.overwrite)
    operation_href = None
    operation_status = "accepted" if http_status in (200, 201, 202, 204) else "error"
    operation_payload: Dict[str, object] = {}

    if http_status == 202:
        try:
            operation_href = json.loads(body).get("href")
        except json.JSONDecodeError:
            operation_href = None
        if operation_href and args.poll:
            deadline = time.time() + args.poll_timeout_sec
            while True:
                operation_payload = client.get_operation(operation_href)
                operation_status = str(operation_payload.get("status") or "unknown")
                if args.progress:
                    print(
                        json.dumps(
                            {
                                "operation_href": operation_href,
                                "operation_status": operation_status,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                if operation_status in ("success", "failed") or time.time() >= deadline:
                    break
                time.sleep(args.poll_interval_sec)
    elif http_status in (200, 201, 204):
        operation_status = "success"

    response_record = {
        "restore_http_status": http_status,
        "restore_response": body[:2000],
        "operation": operation_payload,
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO restore_root_ops
            (scan_id, trash_root, restore_root, operation_href, status, response)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                trash_root,
                restore_root_path,
                operation_href,
                operation_status,
                json.dumps(response_record, ensure_ascii=False),
            ),
        )
        conn.commit()

    return {
        "scan_id": scan_id,
        "trash_root": trash_root,
        "restore_root": restore_root_path,
        "apply": True,
        "restore_http_status": http_status,
        "operation_href": operation_href,
        "operation_status": operation_status,
        "operation": operation_payload,
    }


def render(payload: dict, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps({"success": True, "data": payload}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scanner/restorer for a Yandex Disk Trash subtree")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--format", choices=["json", "text"], default="json")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan")
    scan.add_argument("--trash-root", default=DEFAULT_TRASH_ROOT)
    scan.add_argument("--restore-root", default=DEFAULT_RESTORE_ROOT)
    scan.add_argument("--remote", default="yandex")
    scan.add_argument("--limit", type=int, default=1000)
    scan.add_argument("--progress", action="store_true")
    scan.add_argument("--resume", action="store_true")
    scan.add_argument("--scan-id", type=int)

    summ = sub.add_parser("summary")
    summ.add_argument("--scan-id", type=int)
    summ.add_argument("--restore-root", default=DEFAULT_RESTORE_ROOT)
    summ.add_argument("--limit", type=int, default=50)

    comp = sub.add_parser("compare-monitor")
    comp.add_argument("--scan-id", type=int)
    comp.add_argument("--monitor-db", default=str(ROOT / "monitor.db"))
    comp.add_argument("--monitor-scan-id", type=int, default=1)
    comp.add_argument("--restore-root", default=DEFAULT_RESTORE_ROOT)
    comp.add_argument("--limit", type=int, default=50)

    plan = sub.add_parser("restore-plan")
    plan.add_argument("--scan-id", type=int)
    plan.add_argument("--restore-root", default=DEFAULT_RESTORE_ROOT)
    plan.add_argument("--prefix")
    plan.add_argument("--limit", type=int, default=50)
    plan.add_argument("--order", choices=["path", "size-asc", "size-desc"], default="path")
    plan.add_argument("--retry-failed", action="store_true")

    restore = sub.add_parser("restore-files")
    restore.add_argument("--scan-id", type=int)
    restore.add_argument("--restore-root", default=DEFAULT_RESTORE_ROOT)
    restore.add_argument("--prefix")
    restore.add_argument("--remote", default="yandex")
    restore.add_argument("--limit", type=int, default=10)
    restore.add_argument("--order", choices=["path", "size-asc", "size-desc"], default="path")
    restore.add_argument("--retry-failed", action="store_true")
    restore.add_argument("--overwrite", action="store_true")
    restore.add_argument("--sleep-sec", type=float, default=0.2)
    restore.add_argument("--progress", action="store_true")
    restore.add_argument("--stop-on-error", action="store_true")
    restore.add_argument("--apply", action="store_true")
    restore.add_argument("--yes", default="")

    root_restore = sub.add_parser("restore-root")
    root_restore.add_argument("--scan-id", type=int)
    root_restore.add_argument("--trash-root", default=DEFAULT_TRASH_ROOT)
    root_restore.add_argument("--restore-root", default=DEFAULT_RESTORE_ROOT)
    root_restore.add_argument("--remote", default="yandex")
    root_restore.add_argument("--overwrite", action="store_true")
    root_restore.add_argument("--poll", action="store_true")
    root_restore.add_argument("--poll-interval-sec", type=float, default=5.0)
    root_restore.add_argument("--poll-timeout-sec", type=float, default=600.0)
    root_restore.add_argument("--progress", action="store_true")
    root_restore.add_argument("--apply", action="store_true")
    root_restore.add_argument("--yes", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "scan":
        payload = scan_trash(args)
    elif args.command == "summary":
        payload = summary(args)
    elif args.command == "compare-monitor":
        payload = compare_monitor(args)
    elif args.command == "restore-plan":
        payload = restore_plan(args)
    elif args.command == "restore-files":
        payload = restore_files(args)
    elif args.command == "restore-root":
        payload = restore_root(args)
    else:
        raise SystemExit(f"Unknown command: {args.command}")
    render(payload, args.format)


if __name__ == "__main__":
    main()
