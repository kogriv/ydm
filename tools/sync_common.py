#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import os
import shutil
import subprocess
import time

from ydm import Analyzer, LocalScanner, StorageManager, load_config, DEFAULT_CONFIG


@dataclass
class CompositeSnapshot:
    base_scan_id: int
    folder_updates: Dict[str, int]


@dataclass
class ExcludeDirsResult:
    exclude_dirs: List[str]
    warnings: List[str]
    config_path: str


@dataclass
class SyncFiltersResult:
    include_dirs: List[str]
    warnings: List[str]
    filter_path: str


@dataclass
class CommandResult:
    cmd: List[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass
class LocalScanResult:
    started: bool
    scan_id: Optional[int]
    error: Optional[str]
    duration_sec: Optional[float]


def normalize_path(path: Optional[str]) -> str:
    if not path:
        return "/"
    path = path.strip()
    if path == "":
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def normalize_db_parent_path(path: Optional[str]) -> str:
    normalized = normalize_path(path)
    if normalized == "/":
        return ""
    return normalized


def load_exclude_dirs(config_path: Optional[str] = None) -> ExcludeDirsResult:
    resolved_path = os.path.expanduser(
        config_path or DEFAULT_CONFIG["exclude_config"]
    )
    exclude_dirs: List[str] = []
    warnings: List[str] = []

    if not os.path.exists(resolved_path):
        return ExcludeDirsResult(
            exclude_dirs=exclude_dirs,
            warnings=[f"Config not found: {resolved_path}"],
            config_path=resolved_path,
        )

    try:
        with open(resolved_path, "r") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("exclude-dirs="):
                    raw = line.split("=", 1)[1].strip()
                    if raw:
                        parts = [p.strip() for p in raw.split(",")]
                        for part in parts:
                            if part == "":
                                continue
                            exclude_dirs.append(part)
                            if "," in part:
                                warnings.append(f"Name contains comma: '{part}'")
                    break
    except Exception as exc:
        warnings.append(f"Failed to read config: {exc}")

    return ExcludeDirsResult(
        exclude_dirs=exclude_dirs,
        warnings=warnings,
        config_path=resolved_path,
    )


def default_filter_path(local_root: str) -> str:
    """rclone filter-file lives next to the local mirror by convention: <local_root>.filters"""
    return f"{local_root.rstrip('/')}.filters"


def load_sync_filters(filter_path: str) -> SyncFiltersResult:
    """
    Reads an rclone filter-file of the form:
        + /Folder/**
        + /Other/**
        - **
    Returns the whitelisted top-level entries ("Folder", "Other").
    """
    resolved_path = os.path.expanduser(filter_path)
    include_dirs: List[str] = []
    warnings: List[str] = []

    if not os.path.exists(resolved_path):
        return SyncFiltersResult(
            include_dirs=include_dirs,
            warnings=[f"Filter file not found: {resolved_path}"],
            filter_path=resolved_path,
        )

    try:
        with open(resolved_path, "r") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line == "- **":
                    continue  # expected catch-all terminator
                if line.startswith("+ /") and line.endswith("/**"):
                    entry = line[len("+ /"):-len("/**")]
                    if entry:
                        include_dirs.append(entry)
                        if "\n" in entry or entry != entry.strip():
                            warnings.append(f"Suspicious entry: '{entry}'")
                else:
                    warnings.append(f"Unrecognized filter line: '{line}'")
    except Exception as exc:
        warnings.append(f"Failed to read filter file: {exc}")

    return SyncFiltersResult(
        include_dirs=include_dirs,
        warnings=warnings,
        filter_path=resolved_path,
    )


def write_sync_filters(filter_path: str, include_dirs: List[str]) -> None:
    resolved_path = os.path.expanduser(filter_path)
    if os.path.exists(resolved_path):
        backup_path = f"{resolved_path}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copyfile(resolved_path, backup_path)

    lines = [f"+ /{entry}/**\n" for entry in sorted(set(include_dirs))]
    lines.append("- **\n")

    os.makedirs(os.path.dirname(resolved_path) or ".", exist_ok=True)
    with open(resolved_path, "w") as handle:
        handle.writelines(lines)


def rclone_copy_materialize(remote: str, local_root: str, filter_path: str) -> CommandResult:
    """Runs `rclone copy` for the whole filter-file — materializes every
    currently-included top-level entry (adding one more just re-copies the
    ones already present, which is a fast no-op check for rclone).

    Streams rclone's own `-P` progress bar straight to the terminal instead
    of capturing+buffering it (this can run for many minutes on large
    folders — capturing output would make the command look hung until it
    fully completes, with no visible progress in between)."""
    resolved_root = os.path.expanduser(local_root)
    os.makedirs(resolved_root, exist_ok=True)
    cmd = [
        "rclone", "copy", f"{remote}:", resolved_root,
        "--filter-from", os.path.expanduser(filter_path), "-P",
    ]
    result = subprocess.run(cmd)  # inherits stdout/stderr — live progress, not captured
    return CommandResult(cmd=cmd, returncode=result.returncode, stdout="", stderr="")


def rclone_check_entry(remote: str, entry: str, local_root: str) -> CommandResult:
    resolved_root = os.path.expanduser(local_root)
    return run_command([
        "rclone", "check", f"{remote}:{entry}", os.path.join(resolved_root, entry),
    ])


def delete_local_entry_contents(local_root: str, entry: str) -> Dict[str, object]:
    """Empties <local_root>/<entry> (keeps the now-empty directory itself),
    matching the verify-then-delete-contents pattern already used elsewhere
    in this project's local-copy cleanups."""
    target = os.path.join(os.path.expanduser(local_root), entry)
    if not os.path.exists(target):
        return {"deleted": False, "reason": "not found", "path": target, "files_removed": 0}

    removed = 0
    for root, dirs, files in os.walk(target, topdown=False):
        for name in files:
            os.remove(os.path.join(root, name))
            removed += 1
        for name in dirs:
            dir_path = os.path.join(root, name)
            if dir_path != target:
                os.rmdir(dir_path)
    return {"deleted": True, "path": target, "files_removed": removed}


def path_exists_in_snapshot(analyzer: Analyzer, snapshot, path: str) -> bool:
    scan_id = select_scan_id_for_path(path, snapshot)
    storage = analyzer.storage
    normalized = normalize_path(path)
    if normalized == "/":
        parent_db = ""
        target_name = ""
    else:
        parent_db = normalize_path(str(Path(normalized).parent))
        if parent_db == "/.":
            parent_db = "/"
        if parent_db == "/":
            parent_db = ""
        target_name = Path(normalized).name
    conn = storage.get_connection()
    try:
        if parent_db == "":
            if target_name == "":
                row = conn.execute(
                    "SELECT 1 FROM files WHERE scan_id = ? LIMIT 1",
                    (scan_id,),
                ).fetchone()
                return row is not None
            row = conn.execute(
                """
                SELECT 1 FROM files
                WHERE scan_id = ? AND parent_path = '' AND name = ? AND type = 'dir'
                LIMIT 1
                """,
                (scan_id, target_name),
            ).fetchone()
            return row is not None
        row = conn.execute(
            """
            SELECT 1 FROM files
            WHERE scan_id = ? AND parent_path = ? AND name = ? AND type = 'dir'
            LIMIT 1
            """,
            (scan_id, parent_db, target_name),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def create_storage(db_path: str) -> StorageManager:
    config = load_config("prod")
    return StorageManager(db_path, use_temp_storage=False, config=config)


def run_command(cmd: List[str]) -> CommandResult:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return CommandResult(
        cmd=cmd,
        returncode=result.returncode,
        stdout=result.stdout.strip(),
        stderr=result.stderr.strip(),
    )


def stop_start_daemon() -> Dict[str, CommandResult]:
    return {
        "stop": run_command(["yandex-disk", "stop"]),
        "start": run_command(["yandex-disk", "start"]),
    }


def daemon_status() -> CommandResult:
    return run_command(["yandex-disk", "status"])


def restart_daemon_systemctl() -> CommandResult:
    return run_command(["systemctl", "--user", "restart", "yandex-disk"])


def sleep_sec(seconds: int) -> None:
    time.sleep(seconds)


def ensure_db(storage: StorageManager, db_path: str) -> None:
    if not os.path.exists(db_path):
        storage.init_db()


def run_local_scan(db_path: str, local_root: str) -> LocalScanResult:
    start_time = time.time()
    storage = create_storage(db_path)
    ensure_db(storage, db_path)

    if not os.path.exists(local_root):
        return LocalScanResult(
            started=False,
            scan_id=None,
            error=f"Path not found: {local_root}",
            duration_sec=None,
        )

    scan_id = storage.start_scan("local")
    try:
        scanner = LocalScanner(local_root)
        scanner.scan(scan_id, storage)
        duration = time.time() - start_time
        storage.finish_scan(scan_id, "success", duration)
        storage.checkpoint_to_disk(force=True)
        storage.finalize()
        return LocalScanResult(
            started=True,
            scan_id=scan_id,
            error=None,
            duration_sec=duration,
        )
    except Exception as exc:
        duration = time.time() - start_time
        storage.finish_scan(scan_id, "failed", duration)
        storage.checkpoint_to_disk(force=True)
        storage.cleanup_temp_db()
        return LocalScanResult(
            started=False,
            scan_id=scan_id,
            error=str(exc),
            duration_sec=duration,
        )


def get_latest_successful_scan_id(storage: StorageManager, scan_type: str) -> Optional[int]:
    conn = storage.get_connection()
    try:
        row = conn.execute(
            """
            SELECT id
            FROM scans
            WHERE scan_type = ? AND status = 'success'
            ORDER BY id DESC
            LIMIT 1
            """,
            (scan_type,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def build_composite_snapshot(analyzer: Analyzer) -> CompositeSnapshot:
    composite = analyzer.build_composite_scan()
    base_scan_id = composite.get("base_scan_id")
    folder_updates = composite.get("folder_updates") or {}
    if base_scan_id is None:
        raise RuntimeError("Composite scan is missing base_scan_id")
    return CompositeSnapshot(
        base_scan_id=base_scan_id,
        folder_updates=folder_updates,
    )


def select_scan_id_for_path(path: str, snapshot: CompositeSnapshot) -> int:
    normalized = normalize_db_parent_path(path)
    if normalized == "":
        return snapshot.folder_updates.get("", snapshot.base_scan_id)

    best_match = None
    for candidate in snapshot.folder_updates.keys():
        if not candidate:
            continue
        if normalized == candidate or normalized.startswith(candidate + "/"):
            if best_match is None or len(candidate) > len(best_match):
                best_match = candidate

    if best_match is None:
        return snapshot.base_scan_id
    return snapshot.folder_updates[best_match]


def fetch_child_dirs(
    storage: StorageManager, scan_id: int, parent_path: str
) -> List[str]:
    parent_path_db = normalize_db_parent_path(parent_path)
    conn = storage.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT name
            FROM files
            WHERE scan_id = ?
              AND parent_path = ?
              AND type = 'dir'
            ORDER BY name
            """,
            (scan_id, parent_path_db),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]
