#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import bisect
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

from ydm import Analyzer, LocalScanner, StorageManager, load_config, DEFAULT_CONFIG

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Android shared storage rejects several ASCII characters even though cloud
# remotes can store them. Rclone's local backend can map these to lookalike
# Unicode characters on disk and decode them back when comparing/uploading.
ANDROID_SHARED_STORAGE_LOCAL_ENCODING = (
    "Slash,LtGt,DoubleQuote,Colon,Question,Asterisk,Pipe,BackSlash,"
    "Del,Ctl,InvalidUtf8,Dot"
)


@dataclass
class CompositeSnapshot:
    base_scan_id: int
    folder_updates: Dict[str, int]

    #: Keys of folder_updates in sorted order, built on first use. See
    #: folder_updates_under() for why, and sorted_folders() for the caching.
    _sorted_folders: Optional[List[str]] = field(default=None, repr=False,
                                                 compare=False)

    def sorted_folders(self) -> List[str]:
        if self._sorted_folders is None:
            self._sorted_folders = sorted(
                folder for folder in self.folder_updates if folder
            )
        return self._sorted_folders


def folder_updates_under(snapshot: CompositeSnapshot, subtree: str) -> Dict[str, int]:
    """The updated folders at or beneath `subtree`.

    Callers used to rebuild this by testing `folder.startswith(subtree + "/")`
    against every key — once per node of the tree, so on the author's snapshot
    (2 251 updated folders, 3 810 nodes) a single `orphans` run made 9.38
    million string comparisons, second only to the database itself.

    Sorted, the descendants are one contiguous slice: everything from
    `<subtree>/` up to `<subtree>0`, plus `<subtree>` itself. The boundary
    starts at the slash for the reason it does in SQL — `/Books/Math-old`
    sorts between `/Books/Math` and `/Books/Math0`, and is not a descendant.

    The sort itself happens once per snapshot; doing it per call would cost
    more than the sweep it replaces.
    """
    updates = snapshot.folder_updates
    if not subtree:
        return {folder: scan_id for folder, scan_id in updates.items() if folder}

    base = subtree.rstrip("/")
    folders = snapshot.sorted_folders()
    start = bisect.bisect_left(folders, f"{base}/")
    end = bisect.bisect_left(folders, f"{base}0")
    result = {folder: updates[folder] for folder in folders[start:end]}
    if base in updates:
        result[base] = updates[base]
    return result


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


def legacy_filter_path(local_root: str) -> str:
    """The single filter-file everything used before the policy layer.

    `<local_root>.filters` predates the split into `<local_root>.download.filters`
    and `<local_root>.bisync.filters` that `sync_policy.py render-filters`
    writes today. It is still the file `sync_filters.py` manages and the
    fallback `sync_tree.py` reads when there is no policy, so it is not dead —
    but it describes what to materialize locally, never what may be sent back,
    and the name now says so. `sync_policy.py` has called its own argument
    `--legacy-filter-path` since the split.
    """
    return f"{local_root.rstrip('/')}.filters"


#: Pre-split name, kept so out-of-tree callers do not break. New code should
#: say which filter it means.
default_filter_path = legacy_filter_path


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
                if line.startswith("+ /") and not line.endswith("/**"):
                    # Raw single-file include, used by sync_bisync.py for
                    # RCLONE_TEST check-access. It is valid rclone syntax but
                    # not a folder include for sync tree membership.
                    continue
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


def is_android_shared_storage_path(path: str) -> bool:
    resolved = os.path.abspath(os.path.expanduser(path))
    prefixes = (
        "/sdcard",
        "/storage/emulated/0",
        "/mnt/sdcard",
    )
    return any(resolved == prefix or resolved.startswith(prefix + "/") for prefix in prefixes)


def local_encoding_flags_for_path(path: str) -> List[str]:
    if is_android_shared_storage_path(path):
        return ["--local-encoding", ANDROID_SHARED_STORAGE_LOCAL_ENCODING]
    return []


def write_last_command_log(path: str, cmd: List[str], returncode: int, output: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as handle:
        handle.write(f"timestamp: {datetime.now().isoformat()}\n")
        handle.write(f"returncode: {returncode}\n")
        handle.write("cmd:\n")
        handle.write("  " + " ".join(cmd) + "\n")
        handle.write("\noutput:\n")
        handle.write(output)
        if output and not output.endswith("\n"):
            handle.write("\n")


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

    output_parts: List[str] = []
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    interrupted = False
    try:
        for line in process.stdout:
            print(line, end="")
            output_parts.append(line)
        returncode = process.wait()
    except KeyboardInterrupt:
        interrupted = True
        process.terminate()
        try:
            returncode = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait()
        if returncode == 0:
            returncode = 130
    output = "".join(output_parts)

    copy_last_log = var_path("copy_last.log")
    write_last_command_log(copy_last_log, cmd, returncode, output)
    append_text_log(
        var_path("copy.log"),
        f"{datetime.now().isoformat()} rclone copy returncode={returncode} "
        f"log={copy_last_log}",
    )
    result = CommandResult(
        cmd=cmd,
        returncode=returncode,
        stdout="",
        stderr=f"rclone output saved to {copy_last_log}",
    )
    if interrupted:
        raise KeyboardInterrupt
    return result


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
        if row is None:
            row = conn.execute(
                """
                SELECT 1 FROM files
                WHERE scan_id = ? AND parent_path = ?
                LIMIT 1
                """,
                (scan_id, normalized),
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


def run_command_stream(cmd: List[str]) -> CommandResult:
    """Run a command and stream merged stdout/stderr to the terminal."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    chunks: List[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        chunks.append(line)
    proc.wait()
    combined = "".join(chunks).strip()
    return CommandResult(
        cmd=cmd,
        returncode=proc.returncode,
        stdout=combined,
        stderr="",
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


def normalized_local_root(local_root: str) -> str:
    """Canonical form of a local mirror path, for recording and comparing.

    Written into `scans.scan_root` and compared against it, so both sides must
    agree on `~`, relative paths and trailing slashes. Symlinks are deliberately
    not resolved: the mirror is identified by the path the operator syncs, and
    on Android `/sdcard` is a symlink whose target is not stable across setups.
    """
    resolved = os.path.abspath(os.path.expanduser(local_root))
    return resolved.rstrip("/") or "/"


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

    # Record which tree this scan covered. Without it every local scan in the
    # database looks alike, and `sync_rename` — which detects renames by
    # diffing the two most recent local scans — cannot tell a scan of this
    # mirror from a scan of some other directory. One hand-run preflight with
    # the wrong `--local-root` was enough to make the next run compare two
    # unrelated trees and invent renames. See issue #14.
    scan_id = storage.start_scan("local", scan_root=normalized_local_root(local_root))
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
        # The composite says why it could not be built — most usefully when no
        # scan covers the disk root, which tells the user to run a full scan.
        # Reporting only "missing base_scan_id" throws that away and leaves
        # them with a symptom instead of a cause.
        raise RuntimeError(
            composite.get("error") or "Composite scan is missing base_scan_id"
        )
    return CompositeSnapshot(
        base_scan_id=base_scan_id,
        folder_updates=folder_updates,
    )


def select_scan_id_for_path(path: str, snapshot: CompositeSnapshot) -> int:
    """Which scan serves this folder: the newest one covering it.

    Climbing, not scanning. This used to walk every key of `folder_updates`
    keeping the longest prefix match, once per node of the tree — and on the
    author's snapshot `folder_updates` holds 2 251 entries while a depth-4
    walk visits 2 816 nodes, so the lookup alone ran 6.34 million
    `startswith` calls and outweighed every database query in the walk put
    together.

    The ancestors of a path are knowable without consulting the keys at all:
    the path, then its parent, and so on up. The first one present is by
    construction the longest, so the answer is the same and the cost is a
    handful of dict lookups instead of a full pass.
    """
    normalized = normalize_db_parent_path(path)
    updates = snapshot.folder_updates
    if normalized == "":
        return updates.get("", snapshot.base_scan_id)

    candidate = normalized
    while candidate:
        scan_id = updates.get(candidate)
        if scan_id is not None:
            return scan_id
        parent, separator, _name = candidate.rpartition("/")
        if not separator:
            break
        # `/Books` -> parent `''`: there is no shorter ancestor to try, and the
        # empty key belongs to the root, which the branch above already served.
        candidate = parent

    return snapshot.base_scan_id


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

def require_local_root(value: Optional[str], *, tool: str = "") -> str:
    """The local mirror path, or a clear exit instead of a traceback.

    No default can be right on someone else's machine, so this resolves the
    flag first and `YDM_LOCAL_ROOT` second. Until 2026-08-24 the fallback was
    the author's own `/data/ya_disk`; removing it turned "no path" from a wrong
    answer into a `TypeError` deep inside path handling, which is not an
    improvement. See tasks/opensource/BACKLOG.md 2.3.
    """
    resolved = value or os.environ.get("YDM_LOCAL_ROOT")
    if resolved:
        return resolved
    where = f"{tool}: " if tool else ""
    sys.stderr.write(
        f"{where}no local mirror path.\n"
        f"  Pass --local-root /path/to/your/mirror, or set it once:\n"
        f"      export YDM_LOCAL_ROOT=\"$HOME/YandexDisk\"\n"
        f"  tools/aliases.sh exports it for every ydm-* command.\n"
    )
    raise SystemExit(2)


def var_path(*parts: str) -> str:
    """Resolves a path under the project's var/ dir, regardless of cwd.

    `YDM_VAR_DIR` redirects that directory. It exists for the test bench: the
    log and state paths here are *derived*, never passed in, so `rclone copy`
    run against a temporary remote still appended to the live `var/copy.log`
    and overwrote the live `var/copy_last.log`. No argument could have stopped
    it — see `tasks/android_verify/GAP.md`.
    """
    base = os.environ.get("YDM_VAR_DIR") or os.path.join(PROJECT_ROOT, "var")
    return os.path.join(base, *parts)


def rclone_bisync_run(
    remote: str,
    local_root: str,
    filter_path: str,
    *,
    resync: bool = False,
    max_delete: int = 20,
    check_access: bool = True,
    workdir: Optional[str] = None,
    stream: bool = False,
) -> CommandResult:
    """Runs `rclone bisync` between the local mirror and the cloud remote,
    scoped via bisync's own --filters-file (a distinct flag from `copy`'s
    --filter-from). By default output is captured for unattended jobs; pass
    stream=True for interactive terminals (menu / manual resync).

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
    if stream:
        return run_command_stream(cmd)
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


#: One id for everything ydm says about syncing, so the shade holds at most one
#: such card. Without it every notification is a separate one that nothing can
#: take back, and a solved problem keeps shouting: on 2026-09-13 the owner had
#: "Sync is stuck… every run will fail" sitting above two older cards, hours
#: after a resync had fixed it and the job had gone back to `run OK`.
SYNC_NOTIFICATION_ID = "ydm-sync"


def notify(title: str, message: str, notification_id: str = SYNC_NOTIFICATION_ID) -> None:
    """Best-effort termux-notification wrapper — no-op (never raises) if the
    binary isn't available or the call fails.

    `--id` overwrites any previous card with the same id, which is what makes a
    notification a statement about now rather than an entry in a log.
    """
    binary = shutil.which("termux-notification")
    if not binary:
        return
    command = [binary, "--title", title, "--content", message]
    if notification_id:
        command += ["--id", notification_id, "--alert-once"]
    try:
        subprocess.run(command, capture_output=True)
    except OSError:
        pass


def dismiss_notification(notification_id: str = SYNC_NOTIFICATION_ID) -> None:
    """Take the card back once the thing it describes is no longer true.

    The other half of giving notifications an id. A card that survives its own
    problem teaches the reader to distrust all of them, which is the same damage
    as one card per run — see `notify_once`.
    """
    binary = shutil.which("termux-notification-remove")
    if not binary:
        return
    try:
        subprocess.run([binary, notification_id], capture_output=True)
    except OSError:
        pass
