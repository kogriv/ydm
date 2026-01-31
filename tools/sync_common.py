#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional
import os
import subprocess
import time

from ydm import Analyzer, LocalScanner, StorageManager, load_config


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
        config_path or "~/.config/yandex-disk/config.cfg"
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
