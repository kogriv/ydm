#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

from ydm import Analyzer, LocalScanner, StorageManager, load_config, DEFAULT_CONFIG

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


@dataclass
class LockResult:
    acquired: bool
    holder_pid: Optional[int] = None


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


# --- rclone bisync (tasks/rclone_backend, bidirectional sync) ---------------

def var_path(*parts: str) -> str:
    """Resolves a path under the project's var/ dir, regardless of cwd."""
    return os.path.join(PROJECT_ROOT, "var", *parts)


def rclone_bisync_run(
    remote: str,
    local_root: str,
    filter_path: str,
    *,
    resync: bool = False,
    max_delete: int = 20,
    check_access: bool = True,
    workdir: Optional[str] = None,
) -> CommandResult:
    """Runs `rclone bisync` between the local mirror and the cloud remote,
    scoped via bisync's own --filters-file (a distinct flag from `copy`'s
    --filter-from). Captured, not streamed: this is meant to run unattended
    (scheduled job) and its output needs to be logged, not watched live.

    --max-delete is a *global* rclone flag (must precede the `bisync`
    subcommand), not a bisync-specific one in this rclone build — it defaults
    to -1 (unlimited) if omitted, so callers should always pass an explicit
    cap."""
    resolved_root = os.path.expanduser(local_root)
    cmd = ["rclone", "--max-delete", str(max_delete), "bisync",
           resolved_root, f"{remote}:",
           "--filters-file", os.path.expanduser(filter_path)]
    if resync:
        cmd.append("--resync")
    if check_access:
        cmd.append("--check-access")
    if workdir:
        cmd.extend(["--workdir", os.path.expanduser(workdir)])
    cmd.append("-v")
    return run_command(cmd)


def filter_file_hash(filter_path: str) -> Optional[str]:
    """sha256 of the filter-file's contents, used to detect 'filters changed
    since the last --resync' before invoking rclone (which would otherwise
    just refuse to bisync with an opaque internal error)."""
    resolved_path = os.path.expanduser(filter_path)
    if not os.path.exists(resolved_path):
        return None
    with open(resolved_path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def bisync_state_path() -> str:
    return var_path("bisync_state.json")


def load_bisync_state() -> Dict[str, object]:
    path = bisync_state_path()
    if not os.path.exists(path):
        return {
            "last_resync_filter_hash": None,
            "last_resync_at": None,
            "last_run_at": None,
            "last_status": None,
        }
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {
            "last_resync_filter_hash": None,
            "last_resync_at": None,
            "last_run_at": None,
            "last_status": None,
        }


def save_bisync_state(state: Dict[str, object]) -> None:
    path = bisync_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)


def acquire_lock(lock_path: str) -> LockResult:
    """Same pattern as ydm.py's cloud-scan lock (ydm.py ~2635-2670): PID file,
    liveness checked via /proc/<pid>, stale/corrupted locks self-heal."""
    if os.path.exists(lock_path):
        try:
            with open(lock_path, "r") as handle:
                lock_pid = int(handle.read().strip())
            if os.path.exists(f"/proc/{lock_pid}"):
                return LockResult(acquired=False, holder_pid=lock_pid)
            os.remove(lock_path)  # stale lock — process is dead
        except (ValueError, IOError):
            try:
                os.remove(lock_path)  # corrupted lock file
            except OSError:
                pass

    try:
        with open(lock_path, "w") as handle:
            handle.write(str(os.getpid()))
    except OSError:
        return LockResult(acquired=False, holder_pid=None)
    return LockResult(acquired=True)


def release_lock(lock_path: str) -> None:
    """Mirrors ydm.py's lock release (ydm.py ~2796-2802): best-effort, never
    raises — meant to be called from a `finally` block."""
    if os.path.exists(lock_path):
        try:
            os.remove(lock_path)
        except OSError as exc:
            print(f"Warning: failed to remove lock file: {exc}", file=sys.stderr)


def append_text_log(path: str, line: str) -> None:
    """Plain-text, append-only, line-buffered — matches var/deleted.log's
    convention (tasks/junk/run_cleanup.py)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", buffering=1) as handle:
        handle.write(line.rstrip("\n") + "\n")


def notify(title: str, message: str) -> None:
    """Best-effort termux-notification wrapper — no-op (never raises) if the
    binary isn't available or the call fails."""
    binary = shutil.which("termux-notification")
    if not binary:
        return
    try:
        subprocess.run([binary, "--title", title, "--content", message],
                        capture_output=True)
    except OSError:
        pass
