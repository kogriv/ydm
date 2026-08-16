#!/usr/bin/env python3
"""
Read-only: what changed in the cloud since the composite snapshot.

A full cloud scan walks every folder (~4 600 requests here). The composite
snapshot avoids that by patching a base scan with targeted partial scans —
but it can only be as fresh as the folders someone thought to rescan. This
tool answers the missing question, "which folders went stale", in two or
three requests:

  1. `GET /v1/disk` — one global revision counter for the whole disk. If it
     has not moved since last time, nothing changed anywhere; stop.
  2. `GET /v1/disk/resources/files?sort=-modified` — the most recently
     modified files across the *whole disk*, flat, newest first. Page until
     the timestamps drop below the snapshot; no tree walk involved.
  3. `GET /v1/disk/trash/resources?sort=-deleted` — deletions, with the
     `origin_path` they came from.

Nothing is written to monitor.db. The output is a targeting list for the
existing scanner: `python3 ydm.py scan cloud --path <folder>`.

Blind spots, by construction — see tasks/delta_scan/README.md:
moves/renames and restores from trash keep their `modified`, so they do not
appear in step 2 and never enter the trash for step 3. When the disk
revision moved but this tool found nothing, that is what happened, and the
output says so instead of reporting "no changes".
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.sync_common import create_storage, normalize_path, var_path  # noqa: E402
from tools.trash_scan import resolve_token  # noqa: E402
from ydm import Analyzer  # noqa: E402


SCHEMA = "ydm_cloud_delta:v1"
DISK_URL = "https://cloud-api.yandex.net/v1/disk"
FILES_URL = "https://cloud-api.yandex.net/v1/disk/resources/files"
TRASH_URL = "https://cloud-api.yandex.net/v1/disk/trash/resources"
PAGE_SIZE = 1000
REVISION_STATE = "cloud_revision.json"


class CloudDeltaError(RuntimeError):
    pass


# --- time -------------------------------------------------------------------

def parse_api_time(value: Optional[str]) -> Optional[datetime]:
    """ISO 8601 with offset -> naive UTC, matching how scans.timestamp is stored."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def parse_db_time(value: Optional[str]) -> Optional[datetime]:
    """SQLite CURRENT_TIMESTAMP is UTC, 'YYYY-MM-DD HH:MM:SS'."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return parse_api_time(value)


# --- API --------------------------------------------------------------------

class CloudDeltaClient:
    def __init__(self, token: str, timeout: int = 60, retries: int = 3, sleep=time.sleep):
        self.headers = {"Authorization": f"OAuth {token}", "Accept": "application/json"}
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep
        self.requests = 0
        self.retried = 0

    def _get(self, url: str, params: Dict[str, str]) -> dict:
        full = url + "?" + urllib.parse.urlencode(params)
        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            request = urllib.request.Request(full, headers=self.headers)
            self.requests += 1
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:300]
                # 4xx is our fault (bad token, bad path) — no point retrying.
                if exc.code < 500 and exc.code != 429:
                    raise CloudDeltaError(f"HTTP {exc.code} for {url}: {body}") from exc
                last_error = CloudDeltaError(f"HTTP {exc.code} for {url}: {body}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = CloudDeltaError(f"{type(exc).__name__} for {url}: {exc}")
            if attempt < self.retries - 1:
                self.retried += 1
                self.sleep(2 ** attempt)
        raise CloudDeltaError(f"{last_error} (after {self.retries} attempts)")

    def disk_revision(self) -> int:
        data = self._get(DISK_URL, {"fields": "revision"})
        return int(data.get("revision") or 0)

    def recent_files(self, offset: int, limit: int = PAGE_SIZE) -> List[dict]:
        data = self._get(FILES_URL, {
            "limit": str(limit),
            "offset": str(offset),
            "sort": "-modified",
            "fields": ",".join([
                "items.path", "items.name", "items.size",
                "items.md5", "items.modified", "items.type",
            ]),
        })
        return data.get("items") or []

    def recent_trash(self, offset: int, limit: int = PAGE_SIZE) -> Tuple[List[dict], int]:
        data = self._get(TRASH_URL, {
            "path": "trash:/",
            "limit": str(limit),
            "offset": str(offset),
            "sort": "-deleted",
            "fields": ",".join([
                "_embedded.total",
                "_embedded.items.path", "_embedded.items.origin_path",
                "_embedded.items.deleted", "_embedded.items.type",
            ]),
        })
        embedded = data.get("_embedded") or {}
        return embedded.get("items") or [], int(embedded.get("total") or 0)


# --- snapshot ---------------------------------------------------------------

class Snapshot:
    """Per-folder freshness of the composite snapshot.

    The composite is not "one full scan": for every folder it uses the newest
    scan that covered that folder, falling back to the base scan for folders
    no partial scan ever visited. So staleness is a per-folder question, and
    this holds the answer for each.
    """

    def __init__(self, base_scan_id: int, folder_updates: Dict[str, int], scan_times: Dict[int, datetime]):
        self.base_scan_id = base_scan_id
        self.folder_updates = folder_updates
        self.scan_times = scan_times

    @property
    def base_time(self) -> Optional[datetime]:
        return self.scan_times.get(self.base_scan_id)

    @property
    def oldest_covered_at(self) -> Optional[datetime]:
        """Nothing older than this can be news: everything is covered by then."""
        times = [t for t in self.scan_times.values() if t is not None]
        return min(times) if times else None

    def covering_scan(self, folder: str) -> Tuple[int, Optional[datetime]]:
        scan_id = self.folder_updates.get(folder, self.base_scan_id)
        return scan_id, self.scan_times.get(scan_id)


def load_snapshot(db_path: str) -> Snapshot:
    storage = create_storage(db_path)
    analyzer = Analyzer(storage)
    composite = analyzer.build_composite_scan(use_cache=False)
    if "error" in composite:
        raise CloudDeltaError(f"Composite snapshot unavailable: {composite['error']}")
    base_scan_id = composite["base_scan_id"]
    folder_updates = dict(composite.get("folder_updates") or {})

    wanted = set(folder_updates.values()) | {base_scan_id}
    conn = sqlite3.connect(f"file:{os.path.expanduser(db_path)}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, timestamp FROM scans WHERE id IN (%s)"
            % ",".join("?" * len(wanted)),
            tuple(wanted),
        ).fetchall()
    finally:
        conn.close()
    scan_times = {row[0]: parse_db_time(row[1]) for row in rows}
    return Snapshot(base_scan_id, folder_updates, scan_times)


def cloud_path_to_db(path: str) -> str:
    """'disk:/A/B/f.txt' -> ('/A/B', 'f.txt') style parent, DB convention."""
    stripped = path.split(":", 1)[-1]
    normalized = normalize_path(stripped)
    parent = normalized.rsplit("/", 1)[0]
    return parent if parent else ""


# --- revision state ---------------------------------------------------------

def revision_state_path(explicit: Optional[str] = None) -> str:
    return os.path.expanduser(explicit) if explicit else var_path(REVISION_STATE)


def read_revision_state(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def write_revision_state(path: str, revision: int, checked_at: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {"schema": SCHEMA, "revision": revision, "checked_at": checked_at},
            handle,
            ensure_ascii=False,
            indent=2,
        )
        handle.write("\n")


# --- the actual sweep -------------------------------------------------------

def sweep_modified(
    client: CloudDeltaClient,
    snapshot: Snapshot,
    *,
    max_pages: int,
    examples_per_folder: int = 3,
) -> Tuple[Dict[str, dict], int, bool]:
    """Page /files newest-first until the timestamps predate the snapshot."""
    floor = snapshot.oldest_covered_at
    stale: Dict[str, dict] = {}
    examined = 0
    truncated = False

    for page in range(max_pages):
        items = client.recent_files(page * PAGE_SIZE)
        if not items:
            break
        reached_floor = False
        for item in items:
            examined += 1
            modified = parse_api_time(item.get("modified"))
            if modified is None:
                continue
            if floor is not None and modified < floor:
                reached_floor = True
                break
            folder = cloud_path_to_db(item.get("path") or "")
            scan_id, covered_at = snapshot.covering_scan(folder)
            if covered_at is not None and modified <= covered_at:
                continue  # this folder's snapshot is newer than the change
            bucket = stale.setdefault(folder, {
                "folder": folder or "/",
                "covering_scan_id": scan_id,
                "covered_at": covered_at.isoformat(sep=" ") if covered_at else None,
                "changed_files": 0,
                "deleted_entries": 0,
                "examples": [],
            })
            bucket["changed_files"] += 1
            if len(bucket["examples"]) < examples_per_folder:
                bucket["examples"].append({
                    "path": (item.get("path") or "").split(":", 1)[-1],
                    "modified": item.get("modified"),
                    "size": item.get("size"),
                })
        if reached_floor:
            break
        if len(items) < PAGE_SIZE:
            break
    else:
        truncated = True

    return stale, examined, truncated


def sweep_trash(
    client: CloudDeltaClient,
    snapshot: Snapshot,
    stale: Dict[str, dict],
    *,
    max_pages: int,
    examples_per_folder: int = 3,
) -> Tuple[int, bool]:
    floor = snapshot.oldest_covered_at
    examined = 0
    truncated = False

    for page in range(max_pages):
        items, total = client.recent_trash(page * PAGE_SIZE)
        if not items:
            break
        reached_floor = False
        for item in items:
            examined += 1
            deleted = parse_api_time(item.get("deleted"))
            origin = item.get("origin_path")
            if deleted is None or not origin:
                continue
            if floor is not None and deleted < floor:
                reached_floor = True
                break
            folder = cloud_path_to_db(origin)
            scan_id, covered_at = snapshot.covering_scan(folder)
            if covered_at is not None and deleted <= covered_at:
                continue
            bucket = stale.setdefault(folder, {
                "folder": folder or "/",
                "covering_scan_id": scan_id,
                "covered_at": covered_at.isoformat(sep=" ") if covered_at else None,
                "changed_files": 0,
                "deleted_entries": 0,
                "examples": [],
            })
            bucket["deleted_entries"] += 1
            if len(bucket["examples"]) < examples_per_folder:
                bucket["examples"].append({
                    "path": origin.split(":", 1)[-1],
                    "deleted": item.get("deleted"),
                    "type": item.get("type"),
                })
        if reached_floor:
            break
        if page * PAGE_SIZE + len(items) >= total:
            break
    else:
        truncated = True

    return examined, truncated


def rescan_roots(folders) -> Tuple[List[str], bool]:
    """Collapse stale folders to the fewest paths whose rescan covers them all.

    `scan cloud --path X` walks X recursively, so a stale folder with a stale
    ancestor needs no command of its own — 67 folders here collapse to a
    handful. The disk root is deliberately not treated as an ancestor: a
    rescan of "/" is the full scan this whole tool exists to avoid.
    """
    normalized = {f for f in folders if f}
    root_is_stale = any(not f for f in folders)
    roots: List[str] = []
    for folder in sorted(normalized):
        if any(folder.startswith(root + "/") for root in roots):
            continue
        roots.append(folder)
    return roots, root_is_stale


def check_revision(args: argparse.Namespace) -> dict:
    client = CloudDeltaClient(resolve_token(args.token_source, args.remote))
    revision = client.disk_revision()
    state_path = revision_state_path(args.revision_state)
    previous = read_revision_state(state_path)
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")

    payload = {
        "schema": SCHEMA,
        "action": "check",
        "disk_revision": revision,
        "previous_revision": (previous or {}).get("revision"),
        "previous_checked_at": (previous or {}).get("checked_at"),
        "changed": None if previous is None else revision != previous.get("revision"),
        "revision_state_path": state_path,
        "saved": False,
        "requests": client.requests,
        "error": None,
    }
    if args.save:
        write_revision_state(state_path, revision, now)
        payload["saved"] = True
    return payload


def report_changes(args: argparse.Namespace) -> dict:
    client = CloudDeltaClient(resolve_token(args.token_source, args.remote))
    snapshot = load_snapshot(args.db_path)
    revision = client.disk_revision()

    state_path = revision_state_path(args.revision_state)
    previous = read_revision_state(state_path)
    revision_changed = None if previous is None else revision != previous.get("revision")

    warnings: List[str] = []
    stale: Dict[str, dict] = {}
    files_examined = 0
    files_truncated = False
    trash_examined = 0
    trash_truncated = False

    if revision_changed is False and not args.force:
        warnings.append(
            "Disk revision unchanged since the last check — nothing changed anywhere. "
            "Skipped the sweeps; pass --force to run them anyway."
        )
    else:
        stale, files_examined, files_truncated = sweep_modified(
            client, snapshot, max_pages=args.max_pages
        )
        if not args.no_trash:
            trash_examined, trash_truncated = sweep_trash(
                client, snapshot, stale, max_pages=args.trash_max_pages
            )

    if files_truncated:
        warnings.append(
            f"Stopped after {args.max_pages} pages of modified files "
            f"({files_examined} examined) without reaching the snapshot date — "
            f"the list below is incomplete. Raise --max-pages."
        )
    if trash_truncated:
        warnings.append(
            f"Stopped after {args.trash_max_pages} pages of trash entries — "
            f"deletions may be missing. Raise --trash-max-pages."
        )
    if revision_changed and not stale:
        warnings.append(
            "The disk revision moved but nothing surfaced: this is what a move, "
            "a rename or a restore from trash looks like — they keep their "
            "modified time and never enter the trash. Only a folder walk "
            "(scan cloud) can see those."
        )
    if snapshot.oldest_covered_at is None:
        warnings.append("Snapshot has no usable timestamps; every change is reported.")

    folders = sorted(
        stale.values(),
        key=lambda item: (-(item["changed_files"] + item["deleted_entries"]), item["folder"]),
    )
    roots, root_is_stale = rescan_roots(stale.keys())
    if root_is_stale:
        warnings.append(
            "Files directly in the disk root changed. `scan cloud --path /` is a "
            "full scan, so that one is not in the plan below — rescan the root "
            "only when you want the full walk."
        )
    return {
        "schema": SCHEMA,
        "action": "changes",
        "disk_revision": revision,
        "previous_revision": (previous or {}).get("revision"),
        "revision_changed": revision_changed,
        "snapshot": {
            "base_scan_id": snapshot.base_scan_id,
            "base_at": snapshot.base_time.isoformat(sep=" ") if snapshot.base_time else None,
            "folder_updates": len(snapshot.folder_updates),
            "oldest_covered_at": (
                snapshot.oldest_covered_at.isoformat(sep=" ")
                if snapshot.oldest_covered_at else None
            ),
        },
        "swept": {
            "files_examined": files_examined,
            "files_truncated": files_truncated,
            "trash_examined": trash_examined,
            "trash_truncated": trash_truncated,
            "requests": client.requests,
        },
        "totals": {
            "stale_folders": len(folders),
            "changed_files": sum(f["changed_files"] for f in folders),
            "deleted_entries": sum(f["deleted_entries"] for f in folders),
        },
        "stale_folders": folders[: args.limit],
        "rescan_roots": roots,
        "rescan_commands": [
            f"python3 ydm.py scan cloud --path {root}" for root in roots
        ],
        "warnings": warnings,
        "error": None,
    }


def render(payload: dict, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(
            {"success": payload.get("error") is None, "data": payload},
            ensure_ascii=False, indent=2,
        ))
        return

    print(f"schema: {payload['schema']}")
    print(f"action: {payload['action']}")
    if payload.get("error"):
        print(f"error: {payload['error']}")
        return
    if payload["action"] == "check":
        print(f"disk_revision: {payload['disk_revision']}")
        print(f"previous_revision: {payload['previous_revision']}")
        print(f"changed: {payload['changed']}")
        print(f"state: {payload['revision_state_path']} (saved: {payload['saved']})")
        return

    snap = payload["snapshot"]
    swept = payload["swept"]
    totals = payload["totals"]
    print(f"disk_revision: {payload['disk_revision']} (changed: {payload['revision_changed']})")
    print(
        f"snapshot: base scan #{snap['base_scan_id']} at {snap['base_at']}, "
        f"{snap['folder_updates']} folder update(s), covered since {snap['oldest_covered_at']}"
    )
    print(
        f"swept: {swept['files_examined']} file(s), {swept['trash_examined']} trash "
        f"entr(ies), {swept['requests']} request(s)"
    )
    print(
        f"stale folders: {totals['stale_folders']}  "
        f"changed files: {totals['changed_files']}  "
        f"deleted: {totals['deleted_entries']}  "
        f"rescan roots: {len(payload['rescan_roots'])}"
    )
    for warning in payload["warnings"]:
        print(f"WARN: {warning}")
    if payload["stale_folders"]:
        print("")
        for item in payload["stale_folders"]:
            print(
                f"  {item['folder']}  +{item['changed_files']} changed, "
                f"-{item['deleted_entries']} deleted "
                f"(snapshot scan #{item['covering_scan_id']} at {item['covered_at']})"
            )
            for example in item["examples"][:2]:
                print(f"      {example.get('path')}")
        print("")
        print("Rescan them with:")
        for command in payload["rescan_commands"]:
            print(f"  {command}")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only: which folders of the composite snapshot went stale"
    )
    parser.add_argument("--format", choices=["json", "text"], default="text")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        # Accepted on either side of the subcommand; SUPPRESS keeps the
        # top-level value when the flag is not repeated after it.
        p.add_argument(
            "--format", choices=["json", "text"], default=argparse.SUPPRESS,
        )
        p.add_argument("--remote", default="yandex", help="rclone remote, for --token-source rclone")
        p.add_argument(
            "--token-source", choices=["auto", "env", "rclone"], default="auto",
            help="Where to read the OAuth token: .env first (auto), .env only, or rclone.conf",
        )
        p.add_argument(
            "--revision-state", default=None,
            help=f"Where the last seen disk revision is kept (default: var/{REVISION_STATE})",
        )

    check = sub.add_parser("check", help="One request: has anything changed on the disk at all")
    common(check)
    check.add_argument("--save", action="store_true", help="Store this revision as the reference")

    changes = sub.add_parser("changes", help="Which folders went stale, and what to rescan")
    common(changes)
    changes.add_argument("--db-path", default=str(ROOT_DIR / "monitor.db"))
    changes.add_argument("--limit", type=int, default=50, help="Max stale folders to report")
    changes.add_argument("--max-pages", type=int, default=20, help=f"Pages of {PAGE_SIZE} modified files")
    changes.add_argument("--trash-max-pages", type=int, default=5)
    changes.add_argument("--no-trash", action="store_true", help="Skip the deletion sweep")
    changes.add_argument(
        "--force", action="store_true",
        help="Sweep even when the disk revision says nothing changed",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        if args.command == "check":
            payload = check_revision(args)
        else:
            payload = report_changes(args)
    except (CloudDeltaError, SystemExit) as exc:
        payload = {
            "schema": SCHEMA,
            "action": args.command,
            "error": str(exc),
            "warnings": [],
            "snapshot": {},
            "swept": {},
            "totals": {},
            "stale_folders": [],
            "rescan_commands": [],
        }
        render(payload, args.format)
        return 1
    render(payload, args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
