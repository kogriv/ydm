#!/usr/bin/env python3
import sqlite3
import argparse
import contextlib
import os
import sys
import json
from datetime import datetime, timezone
import shutil
import atexit
import signal

import urllib.request
import time
import subprocess
from abc import ABC, abstractmethod

# Global state for signal handling
_storage_for_signal = None
_scan_id_for_signal = None
_start_time_for_signal = None
_terminate_requested = False

# Global cleanup for tmpfs DB on any exit
def cleanup_tmpfs_on_exit():
    """Ensure tmpfs DB is cleaned up even if process is killed."""
    # Level 3: Process-specific path cleanup
    tmpfs_path = f'/dev/shm/ydm_scan_{os.getpid()}.db'
    if os.path.exists(tmpfs_path):
        try:
            os.remove(tmpfs_path)
        except:
            sys.exit(0 if handled and not cli.failed else 1)

# Signal handler for graceful shutdown
def handle_signal(signum, frame):
    """Handle SIGTERM/SIGUSR1 to save checkpoint before exit."""
    global _terminate_requested
    if signum == signal.SIGTERM:
        print(f"\nReceived SIGTERM. Graceful shutdown requested...", file=sys.stderr)
    elif signum == signal.SIGUSR1:
        print(f"\nReceived SIGUSR1. Force checkpoint requested...", file=sys.stderr)
    _terminate_requested = True
    # Главное — не выходить немедленно, чтобы цикл сканирования успел дописать batch и сделать checkpoint

# Register cleanup on normal exit
atexit.register(cleanup_tmpfs_on_exit)

# Register signal handlers
signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGUSR1, handle_signal)

# --- Helper Functions ---
def load_token_from_env():
    """Load token from .env manually."""
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_file):
        try:
            with open(env_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'): continue
                    if line.startswith('YANDEX_DISK_TOKEN='):
                        return line.split('=', 1)[1].strip().strip('"').strip("'")
        except: pass
    return None

def normalize_compare_path(parent_path):
    """Bring a stored `parent_path` to the form used when comparing scans.

    The two scanners disagree, and always have: cloud scans store "/pro/MuSy"
    (root as ""), local scans store "pro/MuSy" (root as ""). Comparing them
    raw — which `report diff` did — can never match, so every file was
    reported missing on both sides. Neither convention is "right"; comparison
    just has to pick one, and this is it. Storage is left as it is: 856k
    historical local rows would otherwise need rewriting, along with every
    reader of the local side.
    """
    if not parent_path:
        return ""
    return str(parent_path).strip("/")


def load_exclude_dirs(config_path=None):
    """The daemon's `exclude-dirs` as a set, or empty if it cannot be read.

    Lives here rather than inside get_diff() because the freshness check needs
    the same answer: whether a stale folder is worth warning about depends
    entirely on whether anything ever compares it. Two readers deriving the
    exclusion list separately is how they would drift apart.
    """
    path = os.path.expanduser(config_path or DEFAULT_CONFIG["exclude_config"])
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r") as handle:
            for line in handle:
                if line.startswith("exclude-dirs="):
                    dirs = line.split("=", 1)[1].strip()
                    return {d.strip() for d in dirs.split(",") if d.strip()}
    except OSError:
        pass
    return set()


def is_path_excluded(compare_path, exclude_dirs):
    """True if `compare_path` sits under any entry of exclude-dirs.

    exclude-dirs is not a list of top-level folder names: on this machine 45
    of its 55 entries are nested (`video/Обучение`, `Books/42`, …). Testing
    only the first path component — which `report diff` did — reported
    thousands of deliberately unsynced files as missing locally.
    """
    if not exclude_dirs or not compare_path:
        return False
    prefix = ""
    for part in compare_path.split("/"):
        prefix = f"{prefix}/{part}" if prefix else part
        if prefix in exclude_dirs:
            return True
    return False


def _parse_db_timestamp(value):
    """SQLite CURRENT_TIMESTAMP is UTC, 'YYYY-MM-DD HH:MM:SS'."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


def format_prune_plan(plan):
    """Readable summary of a prune plan — the full lists stay in --format json."""
    from collections import Counter

    lines = [
        f"composite base: scan #{plan['base_scan_id']} "
        f"({plan['folder_updates']} folder updates)",
        f"keeping {len(plan['kept'])} scan(s), pruning {plan['prunable_scans']}",
        f"rows: {plan['prunable_rows']} of {plan['total_rows']} "
        f"({plan['prunable_share_percent']}%)",
        "",
        "kept because:",
    ]
    for reason, count in sorted(Counter(item["reason"] for item in plan["kept"]).items()):
        lines.append(f"  {count:>4}  {reason}")

    biggest = sorted(plan["prunable"], key=lambda item: -item["rows"])[:10]
    if biggest:
        lines += ["", "largest scans to delete:"]
        for item in biggest:
            lines.append(
                f"  #{item['scan_id']:<4} {item['scan_type']:<6} {item['timestamp']}  "
                f"{item['rows']} rows"
            )
        rest = plan["prunable_scans"] - len(biggest)
        if rest > 0:
            lines.append(f"  … and {rest} more (see --format json)")

    lines.append("")
    if plan.get("applied"):
        lines.append(
            "APPLIED. " + ("VACUUM done." if plan.get("vacuumed") else
                           "Run with --vacuum to shrink the file on disk.")
        )
    else:
        lines.append("Dry run — nothing deleted. Add --apply to delete.")
    return "\n".join(lines)


def is_termination_requested():
    """Check if SIGTERM/SIGUSR1 requested graceful stop."""
    return _terminate_requested

DEFAULT_CONFIG = {
    "cloud_batch_size": 500,
    "checkpoint_files_threshold": 10000,
    "checkpoint_time_sec": 300,
    # Freshness window (in days) for considering cloud scans in full-scan heuristics
    "full_scan_fresh_window_days": 2,
    # Optional explicit reference full scan ID (can be overridden by user/config)
    "reference_full_scan_id": None,
    # Local mirror path used when no path is given. There is no default that
    # could be right on someone else's machine — it used to be the author's
    # own /data/ya_disk — so it comes from YDM_LOCAL_ROOT (which tools/aliases.sh
    # sets) and is otherwise unset, and the commands that need it say so.
    "local_root": os.environ.get("YDM_LOCAL_ROOT") or None,
    # rclone remote name used by --backend rclone (see tasks/rclone_backend/README.md)
    "rclone_remote": "yandex",
    # yandex-disk daemon's exclude-dirs config; on --backend rclone this is
    # meaningless (no daemon) — Этап 4 introduces the filter-file equivalent.
    "exclude_config": "~/.config/yandex-disk/config.cfg",
    # Sync backend for the unified CLI: "auto" detects the yandex-disk daemon
    # first, then an rclone remote. Override per profile in ydm_config.json,
    # with YDM_BACKEND, or with --backend.
    "backend": "auto",
}

def load_config(profile="prod"):
    """Load config profile from ydm_config.json (optional)."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ydm_config.json")
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
                if profile in data:
                    cfg.update(data[profile])
                elif "prod" in data:
                    cfg.update(data["prod"])
        except Exception as e:
            print(f"Warning: failed to load config {config_path}: {e}", file=sys.stderr)
    return cfg
class TerminationRequested(Exception):
    """Raised when SIGTERM/SIGUSR1 requests graceful stop."""
    pass

class _SharedConnection:
    """A connection handle whose close() returns it to the pool of one.

    Exists so `StorageManager.reuse_connection()` needs no changes at the
    hundred-odd call sites that each open, use and close a connection. Every
    other attribute is the real connection's.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_conn"), name, value)

    def close(self):
        # Not closed — handed back. `row_factory` is reset because callers set
        # it for themselves (Analyzer uses sqlite3.Row) and the next caller
        # through this same connection expects the default.
        object.__getattribute__(self, "_conn").row_factory = None


class StorageManager:
    """Handles all database operations (SQLite)."""

    def __init__(self, db_path, use_temp_storage=True, config=None):
        self.final_db_path = db_path  # Финальное место на диске
        self.use_temp = use_temp_storage
        self.config = config or DEFAULT_CONFIG
        self.last_checkpoint_time = time.time()
        self.last_checkpoint_files = 0
        self.checkpoint_files_threshold = self.config.get("checkpoint_files_threshold", DEFAULT_CONFIG["checkpoint_files_threshold"])
        self.checkpoint_time_sec = self.config.get("checkpoint_time_sec", DEFAULT_CONFIG["checkpoint_time_sec"])
        
        # Если это сканирование и доступен tmpfs - используем его
        if use_temp_storage and os.path.exists('/dev/shm'):
            # Level 3: Process-specific DB path to prevent conflicts between multiple instances
            self.db_path = f'/dev/shm/ydm_scan_{os.getpid()}.db'
            self.temp_mode = True
        else:
            self.db_path = db_path
            self.temp_mode = False

        #: Set while inside reuse_connection(); see that method.
        self._shared_connection = None

    @contextlib.contextmanager
    def reuse_connection(self):
        """Serve get_connection() from a single connection for the duration.

        Opt-in, and read-only by intent. A connection is cheap to make and
        expensive to use for the first time: `sqlite3.connect` is lazy, so the
        file is opened and its schema read on the first statement. Measured on
        the author's 138 MB database — connect and close without touching it,
        0.068 ms; connect plus one child query, 0.522 ms; the same with both
        pragmas, 0.511 ms. So the cost is the first touch, not the pragmas,
        and a depth-4 tree walk paid it 8 504 times.

        (An earlier note here blamed the pragmas, having measured them on a
        method that turned out never to run. They are free.)

        Callers are not asked to change: every DB helper in this project ends
        with `conn.close()` in a `finally`, so the handle returned inside the
        scope ignores close() and restores `row_factory` instead — otherwise
        an Analyzer method that sets `sqlite3.Row` would hand rows to the next
        caller, which expects tuples.

        Do not wrap writes in this. Nothing in a tree walk writes, and the
        scope exists for walks.
        """
        if self._shared_connection is not None:
            yield  # already inside one; the outermost scope owns the handle
            return
        conn = self.get_connection()
        self._shared_connection = _SharedConnection(conn)
        try:
            yield
        finally:
            self._shared_connection = None
            conn.close()

    def get_connection(self):
        """A connection to this manager's database, configured for its mode.

        Until 2026-08-25 this method never ran: `get_connection` was defined
        a second time further down, and Python keeps the last one. So the
        pragmas below were never applied and the "Level 2" recovery could not
        fire on the one setup it was written for — a scan into /dev/shm whose
        file has gone. The duplicate is gone and a test now reads the source
        to make sure no method of this class is written twice again.

        The pragmas cost nothing measurable: on a 138 MB database
        connect+query is 0.522 ms and connect+both pragmas+query is 0.511 ms.
        The ~0.6 ms once blamed on them is the price of first touching the
        file, which any first statement pays — see reuse_connection(), which
        is what actually removes it.

        `journal_mode=WAL` is redundant strictly speaking, since WAL is
        recorded in the database header and survives. It is issued anyway:
        it is free, it says plainly what this database expects, and it puts a
        file that somehow is not in WAL back into it.
        """
        if self._shared_connection is not None:
            return self._shared_connection
        conn = sqlite3.connect(self.db_path)
        # Оптимизация для скорости при работе в памяти
        if self.temp_mode:
            conn.execute("PRAGMA synchronous=OFF")      # Отключаем fsync во время сканирования
            conn.execute("PRAGMA journal_mode=MEMORY")   # Журнал в памяти
        else:
            conn.execute("PRAGMA synchronous=NORMAL")   # Для диска - безопаснее чем FULL
            conn.execute("PRAGMA journal_mode=WAL")      # Write-Ahead Logging

        # Level 2: Auto-recovery if schema is lost during active scan
        if self.temp_mode:
            try:
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='scan_progress'")
                if not cursor.fetchone():
                    # Schema lost! tmpfs DB was deleted or corrupted
                    print(f"CRITICAL: tmpfs DB schema lost! Attempting recovery from disk checkpoint...", file=sys.stderr)
                    conn.close()

                    # Try to recover using global scan_id if available
                    global _scan_id_for_signal
                    if _scan_id_for_signal:
                        # Restore from last checkpoint on disk
                        self.restore_from_disk(_scan_id_for_signal)
                        print(f"Recovery complete. Continuing scan {_scan_id_for_signal} from last checkpoint.", file=sys.stderr)
                    else:
                        # No active scan - just reinitialize
                        self.init_db()
                        print(f"No active scan found. Reinitialized empty schema.", file=sys.stderr)

                    # Reopen connection after recovery
                    conn = sqlite3.connect(self.db_path)
                    conn.execute("PRAGMA synchronous=OFF")
                    conn.execute("PRAGMA journal_mode=MEMORY")
            except Exception as e:
                print(f"WARNING: Schema recovery failed: {e}", file=sys.stderr)

        return conn

    def finalize(self):
        """Сохраняет данные из временной БД на диск после успешного завершения."""
        if self.temp_mode and os.path.exists(self.db_path):
            try:
                # Последний checkpoint перед финализацией
                self.checkpoint_to_disk(force=True)
                
                # Удалить временную БД - данные уже на диске через checkpoint
                self.cleanup_temp_db()
                return True
            except Exception as e:
                print(f"Warning: Failed to finalize scan to disk: {e}", file=sys.stderr)
                return False
        return True
    
    def cleanup_temp_db(self):
        """Clean up temporary database after use (either success or interruption)."""
        if self.temp_mode and os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
                print(f"Cleaned up temporary database: {self.db_path}", file=sys.stderr)
            except Exception as e:
                print(f"Warning: Failed to cleanup temp DB: {e}", file=sys.stderr)
    
    def reset_temp_db(self):
        """Delete existing tmpfs DB and reinitialize (used before starting/resuming)."""
        if self.temp_mode and os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
                print(f"Reset temporary database (old data discarded)", file=sys.stderr)
            except Exception as e:
                print(f"Warning: Failed to reset temp DB: {e}", file=sys.stderr)
        
        # Create fresh tmpfs DB
        self.init_db()

    def checkpoint_to_disk(self, force=False):
        """Save accumulated data from RAM DB to disk DB.
        
        Called rarely by default, but can be triggered by conditions:
        - force=True: unconditional save (on finalize or SIGTERM)
        - Automatic: if > 10000 files accumulated OR > 5 minutes elapsed
        """
        if not self.temp_mode:
            return  # Already on disk
        
        # Check if automatic checkpoint should trigger
        if not force:
            elapsed = time.time() - self.last_checkpoint_time
            
            # Conditions for intermediate checkpoint (protective, not on every batch)
            should_checkpoint = (
                self.last_checkpoint_files >= self.checkpoint_files_threshold or
                elapsed >= self.checkpoint_time_sec
            )
            
            if not should_checkpoint:
                return
        
        try:
            # Open connection to tmpfs DB
            temp_conn = sqlite3.connect(self.db_path)
            
            # Initialize final DB if doesn't exist
            if not os.path.exists(self.final_db_path):
                self._init_final_db()
            
            temp_conn.execute(f"ATTACH DATABASE '{self.final_db_path}' AS disk")
            
            # Copy scans (update existing using REPLACE to avoid attached DB upsert limit)
            temp_conn.execute("""
                INSERT OR REPLACE INTO disk.scans 
                SELECT * FROM main.scans
            """)
            
            # Copy disk_info (update existing)
            temp_conn.execute("""
                INSERT OR REPLACE INTO disk.disk_info 
                SELECT * FROM main.disk_info
            """)
            
            # Copy files (without id - let target DB auto-increment)
            # Use INSERT OR IGNORE to prevent duplicates when checkpoint runs multiple times
            temp_conn.execute("""
                INSERT OR IGNORE INTO disk.files 
                (scan_id, parent_path, name, type, size, md5, created, modified)
                SELECT scan_id, parent_path, name, type, size, md5, created, modified FROM main.files
            """)
            
            # Copy scan_progress (update statuses)
            temp_conn.execute("""
                INSERT OR REPLACE INTO disk.scan_progress 
                SELECT * FROM main.scan_progress
            """)
            
            temp_conn.commit()
            temp_conn.execute("DETACH DATABASE disk")
            temp_conn.close()
            
            # Reset counters after successful checkpoint
            self.last_checkpoint_time = time.time()
            self.last_checkpoint_files = 0
            
        except Exception as e:
            print(f"Warning: Checkpoint failed: {e}", file=sys.stderr)
    
    @staticmethod
    def _ensure_scan_scope_columns(conn):
        """Add `scans.scan_root` / `scans.scan_depth` to a pre-existing database.

        Both are nullable on purpose. A NULL means "this scan predates the
        columns" and readers fall back to inferring the scope from the rows,
        which is what they did before. Nothing rewrites history: a scan that
        never recorded its scope is not going to acquire one retroactively.
        """
        existing = {row[1] for row in conn.execute("PRAGMA table_info(scans)")}
        if not existing:
            return  # No scans table yet; the CREATE above carries the columns.
        if "scan_root" not in existing:
            conn.execute("ALTER TABLE scans ADD COLUMN scan_root TEXT")
        if "scan_depth" not in existing:
            conn.execute("ALTER TABLE scans ADD COLUMN scan_depth INTEGER")

    def _init_final_db(self):
        """Initialize final DB schema if it doesn't exist."""
        try:
            conn = sqlite3.connect(self.final_db_path)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA journal_mode=WAL")
            
            # Same schema as tmpfs DB
            schema = [
                """
                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    scan_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration REAL,
                    scan_root TEXT,
                    scan_depth INTEGER
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS disk_info (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id INTEGER NOT NULL,
                    total_space INTEGER,
                    used_space INTEGER,
                    trash_size INTEGER,
                    FOREIGN KEY(scan_id) REFERENCES scans(id)
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id INTEGER NOT NULL,
                    parent_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    size INTEGER,
                    md5 TEXT,
                    created DATETIME,
                    modified DATETIME,
                    FOREIGN KEY(scan_id) REFERENCES scans(id)
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS scan_progress (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    offset INTEGER DEFAULT 0,
                    total_items INTEGER,
                    last_checked DATETIME,
                    FOREIGN KEY(scan_id) REFERENCES scans(id),
                    UNIQUE(scan_id, path)
                );
                """,
                "CREATE INDEX IF NOT EXISTS idx_files_parent ON files(scan_id, parent_path);",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_files_unique ON files(scan_id, parent_path, name);",
                "CREATE INDEX IF NOT EXISTS idx_scans_type ON scans(scan_type);",
                "CREATE INDEX IF NOT EXISTS idx_progress_scan ON scan_progress(scan_id, status);"
            ]
            
            for statement in schema:
                conn.execute(statement)
            self._ensure_scan_scope_columns(conn)
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Warning: Failed to init final DB: {e}", file=sys.stderr)

    
    def restore_from_disk(self, scan_id):
        """Restore scan data from disk DB to tmpfs for resuming."""
        if not self.temp_mode:
            return  # Уже на диске, ничего копировать не надо
        
        if not os.path.exists(self.final_db_path):
            return  # Нет финальной БД
        
        try:
            # Подключиться к временной БД
            temp_conn = sqlite3.connect(self.db_path)
            
            # CRITICAL: Initialize tmpfs schema BEFORE copying data
            # This was missing and caused "no such table" error during checkpoint
            self.init_db()
            
            # Attach финальной БД
            temp_conn.execute(f"ATTACH DATABASE '{self.final_db_path}' AS disk")
            
            # Копировать данные конкретного сеанса
            temp_conn.execute("""
                INSERT OR IGNORE INTO main.scans 
                SELECT * FROM disk.scans WHERE id = ?
            """, (scan_id,))
            
            temp_conn.execute("""
                INSERT OR IGNORE INTO main.disk_info 
                SELECT * FROM disk.disk_info WHERE scan_id = ?
            """, (scan_id,))
            
            temp_conn.execute("""
                INSERT OR IGNORE INTO main.files 
                SELECT * FROM disk.files WHERE scan_id = ?
            """, (scan_id,))
            
            temp_conn.execute("""
                INSERT OR REPLACE INTO main.scan_progress 
                SELECT * FROM disk.scan_progress WHERE scan_id = ?
            """, (scan_id,))
            
            temp_conn.commit()
            temp_conn.execute("DETACH DATABASE disk")
            temp_conn.close()
            
            print(f"Restored scan {scan_id} from disk to RAM", file=sys.stderr)
            
        except Exception as e:
            print(f"Warning: Failed to restore from disk: {e}", file=sys.stderr)

    def init_db(self):
        """Creates the database schema if it doesn't exist."""
        schema = [
            """
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                scan_type TEXT NOT NULL,
                status TEXT NOT NULL,
                duration REAL,
                scan_root TEXT,
                scan_depth INTEGER
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS disk_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                total_space INTEGER,
                used_space INTEGER,
                trash_size INTEGER,
                FOREIGN KEY(scan_id) REFERENCES scans(id)
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                parent_path TEXT NOT NULL,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                size INTEGER,
                md5 TEXT,
                created DATETIME,
                modified DATETIME,
                FOREIGN KEY(scan_id) REFERENCES scans(id)
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS scan_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                offset INTEGER DEFAULT 0,
                total_items INTEGER,
                last_checked DATETIME,
                FOREIGN KEY(scan_id) REFERENCES scans(id),
                UNIQUE(scan_id, path)
            );
            """,
            "CREATE INDEX IF NOT EXISTS idx_files_parent ON files(scan_id, parent_path);",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_files_unique ON files(scan_id, parent_path, name);",
            "CREATE INDEX IF NOT EXISTS idx_scans_type ON scans(scan_type);",
            "CREATE INDEX IF NOT EXISTS idx_progress_scan ON scan_progress(scan_id, status);"
        ]
        
        try:
            # `with sqlite3.connect(...)` commits or rolls back; it does NOT
            # close. The connection then lived until the next garbage
            # collection, holding the file — which is enough to make the very
            # next `PRAGMA journal_mode` fail with "database is locked". The
            # tmpfs recovery path calls init_db() and immediately reopens, so
            # this was in the way of the case it exists for.
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                for statement in schema:
                    cursor.execute(statement)
                self._ensure_scan_scope_columns(conn)
                conn.commit()
            finally:
                conn.close()
            return True, f"Database initialized successfully at {self.db_path}"
        except Exception as e:
            return False, f"Database initialization failed: {str(e)}"

    def recover_crashed_scans(self):
        """
        Check for crashed scans (status='started' but process not running).
        Mark them as 'crashed' so they can be analyzed or resumed.
        """
        if not os.path.exists(self.final_db_path):
            return []
        
        crashed = []
        try:
            conn = sqlite3.connect(self.final_db_path)
            cursor = conn.cursor()
            
            # Find scans with 'started' status (incomplete)
            cursor.execute(
                "SELECT id, scan_type, timestamp FROM scans WHERE status = 'started' ORDER BY id DESC"
            )
            started_scans = cursor.fetchall()
            
            # Mark each as 'crashed' - they didn't complete normally
            for scan_id, scan_type, timestamp in started_scans:
                cursor.execute(
                    "UPDATE scans SET status = 'crashed' WHERE id = ?",
                    (scan_id,)
                )
                conn.commit()
                crashed.append({
                    'id': scan_id,
                    'type': scan_type,
                    'timestamp': timestamp
                })
            
            conn.close()
        except Exception as e:
            print(f"Warning: Failed to recover crashed scans: {e}", file=sys.stderr)
        
        return crashed

    def start_scan(self, scan_type, scan_root=None, scan_depth=None):
        """Creates a new scan record with 'started' status.

        `scan_root` and `scan_depth` record what the scan was *asked* to cover.
        Until they existed, scope had to be reconstructed from the rows a scan
        left behind, and that inference has been wrong twice: once picking a
        deep leaf as a partial scan's root, and once about to let a shallow
        scan of `/` pass for a full one. Recording the intent removes the
        guess for every scan written from here on.
        """
        # Получить правильный scan_id из финальной БД
        next_scan_id = self._get_next_scan_id()

        conn = self.get_connection()
        cursor = conn.cursor()

        # Если tmpfs режим - используем явный scan_id
        if self.temp_mode:
            cursor.execute(
                "INSERT INTO scans (id, scan_type, status, duration, scan_root, scan_depth)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (next_scan_id, scan_type, 'started', 0, scan_root, scan_depth)
            )
            scan_id = next_scan_id

            # IMPORTANT: Also write to disk DB immediately so scan is tracked even if interrupted before checkpoint
            try:
                if not os.path.exists(self.final_db_path):
                    self._init_final_db()
                disk_conn = sqlite3.connect(self.final_db_path)
                self._ensure_scan_scope_columns(disk_conn)
                disk_conn.execute(
                    "INSERT OR IGNORE INTO scans (id, scan_type, status, duration, scan_root, scan_depth)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (next_scan_id, scan_type, 'started', 0, scan_root, scan_depth)
                )
                disk_conn.commit()
                disk_conn.close()
            except Exception as e:
                print(f"Warning: Failed to record scan start in disk DB: {e}", file=sys.stderr)
        else:
            # The tmpfs branch above migrates the disk database before writing
            # to it; this one has to as well. It is the path every tool takes —
            # tools/sync_common.py builds a StorageManager with
            # use_temp_storage=False, and such an object never calls
            # init_db() — so without this, `scan_root` reaches an unmigrated
            # `scans` table and every tool-driven scan dies on a database that
            # only ydm.py had ever opened for writing.
            self._ensure_scan_scope_columns(conn)
            cursor.execute(
                "INSERT INTO scans (scan_type, status, duration, scan_root, scan_depth)"
                " VALUES (?, ?, ?, ?, ?)",
                (scan_type, 'started', 0, scan_root, scan_depth)
            )
            scan_id = cursor.lastrowid
        
        conn.commit()
        
        # Сброс счетчиков checkpoint
        self.last_checkpoint_time = time.time()
        self.last_checkpoint_files = 0
        
        return scan_id
    
    def _get_next_scan_id(self):
        """Get next scan ID from final DB to maintain continuity."""
        if not os.path.exists(self.final_db_path):
            return 1
        
        try:
            conn = sqlite3.connect(self.final_db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(id) FROM scans")
            result = cursor.fetchone()
            conn.close()
            return (result[0] or 0) + 1
        except:
            return 1

    def finish_scan(self, scan_id, status, duration):
        """Updates scan status and duration."""
        conn = self.get_connection()
        conn.execute("UPDATE scans SET status = ?, duration = ? WHERE id = ?", (status, duration, scan_id))
        conn.commit()

    def save_disk_info(self, scan_id, info):
        """Saves disk meta info."""
        conn = self.get_connection()
        conn.execute(
            "INSERT INTO disk_info (scan_id, total_space, used_space, trash_size) VALUES (?, ?, ?, ?)",
            (scan_id, info['total_space'], info['used_space'], info['trash_size'])
        )
        conn.commit()

    def save_files_batch(self, files_data):
        """Bulk insert files."""
        if not files_data: return
        conn = self.get_connection()
        conn.executemany(
            """INSERT INTO files 
            (scan_id, parent_path, name, type, size, md5, modified, created) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            files_data
        )
        conn.commit()
        conn.close()
        
        # Обновить счетчик для checkpoint
        self.last_checkpoint_files += len(files_data)

    def get_pending_folders(self, scan_id):
        """Gets list of folders to scan (pending + in_progress)."""
        conn = self.get_connection()
        rows = conn.execute(
            """SELECT path FROM scan_progress 
            WHERE scan_id = ? AND status IN ('pending', 'in_progress')
            ORDER BY path""",
            (scan_id,)
        ).fetchall()
        return [row[0] for row in rows] or ["/"]

    def update_folder_status(self, scan_id, path, status):
        """Updates folder status (pending/in_progress/completed)."""
        conn = self.get_connection()
        conn.execute(
            """INSERT INTO scan_progress (scan_id, path, status, last_checked) 
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(scan_id, path) DO UPDATE SET status = ?, last_checked = CURRENT_TIMESTAMP""",
            (scan_id, path, status, status)
        )
        conn.commit()

    def get_folder_offset(self, scan_id, path):
        """Gets the last offset for a folder (for resuming)."""
        conn = self.get_connection()
        row = conn.execute(
            "SELECT offset FROM scan_progress WHERE scan_id = ? AND path = ?",
            (scan_id, path)
        ).fetchone()
        return row[0] if row else 0

    def save_checkpoint(self, scan_id, path, offset, total_items):
        """Saves progress checkpoint for a folder."""
        conn = self.get_connection()
        conn.execute(
            """INSERT INTO scan_progress (scan_id, path, status, offset, total_items, last_checked)
            VALUES (?, ?, 'in_progress', ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(scan_id, path) DO UPDATE SET 
                offset = ?, total_items = ?, last_checked = CURRENT_TIMESTAMP""",
            (scan_id, path, offset, total_items, offset, total_items)
        )
        conn.commit()

    def get_scan_stats(self, scan_id):
        """Gets scan progress statistics."""
        conn = self.get_connection()
        stats = conn.execute(
            """SELECT 
            COUNT(*) as total_folders,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
            SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending
            FROM scan_progress WHERE scan_id = ?""",
            (scan_id,)
        ).fetchone()
        return {
            "total_folders": stats[0] or 0,
            "completed": stats[1] or 0,
            "in_progress": stats[2] or 0,
            "pending": stats[3] or 0
        }

    def get_scan_details(self, scan_id):
        """Gets detailed info about a scan session."""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        
        scan = conn.execute(
            "SELECT id, timestamp, scan_type, status, duration FROM scans WHERE id = ?",
            (scan_id,)
        ).fetchone()
        
        if not scan:
            return None
        
        # Получаем количество файлов
        files_count = conn.execute(
            "SELECT COUNT(*) FROM files WHERE scan_id = ?",
            (scan_id,)
        ).fetchone()[0]
        
        # Получаем общий размер
        total_size = conn.execute(
            "SELECT SUM(size) FROM files WHERE scan_id = ?",
            (scan_id,)
        ).fetchone()[0] or 0
        
        return {
            "id": scan["id"],
            "timestamp": scan["timestamp"],
            "type": scan["scan_type"],
            "status": scan["status"],
            "duration": scan["duration"],
            "files_count": files_count,
            "total_size_bytes": total_size,
            "progress": self.get_scan_stats(scan_id)
        }

    def get_all_scans(self, limit=10):
        """Gets list of recent scans."""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        
        scans = conn.execute(
            "SELECT id, timestamp, scan_type, status, duration FROM scans ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        
        result = []
        for scan in scans:
            files_count = conn.execute(
                "SELECT COUNT(*) FROM files WHERE scan_id = ?",
                (scan["id"],)
            ).fetchone()[0]
            
            progress = self.get_scan_stats(scan["id"])
            
            result.append({
                "id": scan["id"],
                "timestamp": scan["timestamp"],
                "type": scan["scan_type"],
                "status": scan["status"],
                "duration": scan["duration"],
                "files_count": files_count,
                "progress": progress
            })
        
        return result

    def get_scan_pending_folders(self, scan_id, limit=20):
        """Gets list of pending folders for a scan."""
        conn = self.get_connection()
        
        folders = conn.execute(
            """SELECT path, status, offset, total_items, last_checked 
            FROM scan_progress 
            WHERE scan_id = ? AND status IN ('pending', 'in_progress')
            ORDER BY status DESC, path
            LIMIT ?""",
            (scan_id, limit)
        ).fetchall()
        
        return [
            {
                "path": f[0],
                "status": f[1],
                "offset": f[2],
                "total_items": f[3],
                "last_checked": f[4]
            }
            for f in folders
        ]

    def add_folders_to_scan(self, scan_id, paths):
        """Добавляет папки в очередь существующего скана."""
        conn = self.get_connection()
        for path in paths:
            conn.execute(
                """INSERT OR IGNORE INTO scan_progress (scan_id, path, status)
                VALUES (?, ?, 'pending')""",
                (scan_id, path)
            )
        conn.commit()

    def get_pending_folders_count(self, scan_id):
        """Получить количество оставшихся папок."""
        conn = self.get_connection()
        row = conn.execute(
            "SELECT COUNT(*) FROM scan_progress WHERE scan_id = ? AND status IN ('pending', 'in_progress')",
            (scan_id,)
        ).fetchone()
        return row[0] if row else 0


class LocalScanner:
    """Scans local filesystem."""
    def __init__(self, root_path):
        self.root_path = root_path

    def scan(self, scan_id, storage):
        """Walks through local directory and saves to DB."""
        batch_size = 1000
        batch = []
        count = 0
        
        # Normpath убирает trailing slash, если он есть
        root_abs = os.path.abspath(self.root_path)

        for root, dirs, files in os.walk(root_abs):
            # Определяем относительный путь от корня сканирования
            rel_path = os.path.relpath(root, root_abs)
            if rel_path == ".":
                rel_path = ""
            
            # Обработка папок
            for d in dirs:
                # parent_path, name, type, size, modified, created
                full_path = os.path.join(root, d)
                stat = os.stat(full_path)
                batch.append((
                    scan_id, 
                    rel_path, 
                    d, 
                    'dir', 
                    0,
                    None, # md5
                    datetime.fromtimestamp(stat.st_mtime),
                    datetime.fromtimestamp(stat.st_ctime)
                ))
            
            # Обработка файлов
            for f in files:
                full_path = os.path.join(root, f)
                try:
                    stat = os.stat(full_path)
                    batch.append((
                        scan_id,
                        rel_path,
                        f,
                        'file',
                        stat.st_size,
                        None, # md5
                        datetime.fromtimestamp(stat.st_mtime),
                        datetime.fromtimestamp(stat.st_ctime)
                    ))
                    count += 1
                except FileNotFoundError:
                    continue # Файл мог исчезнуть во время скана

            if len(batch) >= batch_size:
                storage.save_files_batch(batch)
                batch = []

        # Сохраняем остатки
        if batch:
            storage.save_files_batch(batch)
        
        return count


class CloudResourceClient(ABC):
    """
    Backend interface for CloudScanner/scan-meta: whatever fetches Yandex
    Disk resource listings, regardless of transport (REST API vs rclone).
    Item dicts returned by get_resources() must have the same shape the
    Yandex API returns: name, type ('dir'|'file'), size, md5, created,
    modified (ISO 8601 strings, 'Z' suffix, microsecond precision or less).
    """

    @abstractmethod
    def get_disk_info(self):
        """Returns dict with total_space, used_space, trash_size (bytes)."""
        raise NotImplementedError

    @abstractmethod
    def get_resources(self, path, limit=1000, offset=0):
        """Returns {"items": [...], "total": N} for one folder (non-recursive)."""
        raise NotImplementedError


class YandexClient(CloudResourceClient):
    """Yandex Disk REST API Client."""
    API_URL = "https://cloud-api.yandex.net/v1/disk"

    def __init__(self, token):
        self.token = token
        self.headers = {
            "Authorization": f"OAuth {token}",
            "Accept": "application/json"
        }

    def get_disk_info(self):
        """Fetches general disk information (quota)."""
        req = urllib.request.Request(self.API_URL, headers=self.headers)
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode('utf-8'))
        except Exception as e:
            raise Exception(f"API Error: {str(e)}")

    def get_resources(self, path, limit=1000, offset=0):
        """Fetches file list for a specific directory."""
        params = urllib.parse.urlencode({
            "path": path,
            "limit": limit,
            "offset": offset,
            "fields": "_embedded.items.name,_embedded.items.type,_embedded.items.size,_embedded.items.md5,_embedded.items.created,_embedded.items.modified,_embedded.total"
        })
        url = f"{self.API_URL}/resources?{params}"
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode('utf-8'))
                return data.get('_embedded', {})
        except Exception as e:
            # Если папка пустая или недоступна
            print(f"Warning: Failed to list {path}: {e}")
            return {"items": [], "total": 0}


class RcloneClient(CloudResourceClient):
    """
    Yandex Disk client via rclone subprocess calls. For environments where
    the official yandex-disk daemon can't run (e.g. arm64/Android — see
    tasks/rclone_backend/README.md). Needs an authorized `rclone.conf`
    remote (default name "yandex"), not YANDEX_DISK_TOKEN/.env.

    Caveat: rclone's ModTime is the only timestamp exposed across backends,
    Yandex Disk's REST API separately reports created vs modified — here
    both fields are set to the same ModTime value.
    """

    def __init__(self, remote="yandex"):
        self.remote = remote
        self._folder_cache = {}  # path -> full normalized item list (rclone lsjson has no offset/limit)

    def get_disk_info(self):
        result = subprocess.run(
            ["rclone", "about", f"{self.remote}:", "--json"],
            capture_output=True, text=True, check=True
        )
        data = json.loads(result.stdout)
        return {
            "total_space": data.get("total", 0),
            "used_space": data.get("used", 0),
            "trash_size": data.get("trashed", 0),
        }

    def get_resources(self, path, limit=1000, offset=0):
        if path not in self._folder_cache:
            self._folder_cache[path] = self._list_folder(path)
        items = self._folder_cache[path]
        return {"items": items[offset:offset + limit], "total": len(items)}

    def _list_folder(self, path):
        remote_path = f"{self.remote}:" if path in ("", "/") else f"{self.remote}:{path}"
        try:
            result = subprocess.run(
                ["rclone", "lsjson", remote_path, "--hash"],
                capture_output=True, text=True, check=True
            )
            entries = json.loads(result.stdout)
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            print(f"Warning: Failed to list {path} via rclone: {e}")
            return []

        items = []
        for entry in entries:
            # Truncate rclone's nanosecond ModTime to microseconds for
            # datetime.fromisoformat() compatibility (CloudScanner parses
            # this with .replace('Z', '+00:00')).
            mod_time = entry.get("ModTime", "")
            if "." in mod_time:
                head, frac_and_zone = mod_time.split(".", 1)
                frac = frac_and_zone.rstrip("Z")[:6]
                mod_time = f"{head}.{frac}Z"
            is_dir = entry.get("IsDir", False)
            items.append({
                "name": entry["Name"],
                "type": "dir" if is_dir else "file",
                "size": 0 if is_dir else entry.get("Size", 0),
                "md5": (entry.get("Hashes") or {}).get("md5"),
                "created": mod_time,
                "modified": mod_time,
            })
        return items


class CloudScanner:
    """Scans Yandex Disk via API with resumable support."""
    def __init__(self, client, report_progress=False, config=None):
        self.client = client
        self.report_progress = report_progress
        self.config = config or DEFAULT_CONFIG

    @staticmethod
    def _depth_of(path, root):
        """How many levels below `root` the folder `path` sits. Root itself is 0."""
        root_parts = [p for p in str(root or "/").strip("/").split("/") if p]
        path_parts = [p for p in str(path or "/").strip("/").split("/") if p]
        return len(path_parts) - len(root_parts)

    def scan(self, scan_id, storage, resume=False, start_path=None, max_depth=None):
        """Walks through cloud recursively with resumable support.

        `max_depth` bounds the walk: depth 1 lists the start folder's own
        entries and descends no further. This is what makes the disk root
        refreshable at all — a full walk of `/` is 1.5 TB, while the handful
        of files sitting directly in the root is eight requests. A bounded
        scan covers exactly the `parent_path` buckets it visited, which is the
        unit the composite snapshot already replaces, so it slots in without
        a second kind of update.
        """
        batch_size = self.config.get("cloud_batch_size", DEFAULT_CONFIG["cloud_batch_size"])
        batch = []
        files_count = 0
        total_processed = 0

        visited = set()
        walk_root = start_path if start_path else "/"

        def flush_and_terminate(current_path, offset, total):
            # Сохранить текущий batch в RAM БД, затем сделать checkpoint на диск
            nonlocal batch
            if batch:
                storage.save_files_batch(batch)
                batch = []
            storage.save_checkpoint(scan_id, current_path, offset, total)
            storage.checkpoint_to_disk(force=True)
            raise TerminationRequested()

        # Load pending folders or start fresh
        if resume:
            pending = storage.get_pending_folders(scan_id)
            queue = pending
            if self.report_progress:
                stats = storage.get_scan_stats(scan_id)
                print(json.dumps({
                    "status": "resume",
                    "scan_id": scan_id,
                    "stats": stats
                }, ensure_ascii=False))
        else:
            # Если указана конкретная папка - начинаем с неё, иначе с корня
            queue = [walk_root]
            storage.update_folder_status(scan_id, walk_root, "pending")

        while queue:
            current_path = queue.pop(0)
            if current_path in visited:
                continue
            visited.add(current_path)
            
            # Mark as in_progress
            storage.update_folder_status(scan_id, current_path, 'in_progress')
            
            # Report progress
            if self.report_progress and total_processed % 50 == 0:
                stats = storage.get_scan_stats(scan_id)
                print(json.dumps({
                    "status": "progress",
                    "scanned": total_processed,
                    "current": current_path,
                    "stats": stats
                }, ensure_ascii=False))

            # Get last offset for resuming
            offset = storage.get_folder_offset(scan_id, current_path)
            total = 0
            limit = 1000 
            
            while True:
                if is_termination_requested():
                    flush_and_terminate(current_path, offset, total)
                res = self.client.get_resources(current_path, limit, offset)
                items = res.get('items', [])
                total = res.get('total', 0)
                
                for item in items:
                    if is_termination_requested():
                        flush_and_terminate(current_path, offset, total)
                    total_processed += 1
                    name = item['name']
                    # Normalize path
                    parent = current_path
                    if parent == "/":
                        parent = ""
                    elif parent.endswith('/'):
                        parent = parent[:-1]

                    try:
                        created = datetime.fromisoformat(item['created'].replace('Z', '+00:00'))
                        modified = datetime.fromisoformat(item['modified'].replace('Z', '+00:00'))
                    except:
                        created = datetime.now()
                        modified = datetime.now()

                    batch.append((
                        scan_id,
                        parent, 
                        name,
                        item['type'],
                        item.get('size', 0),
                        item.get('md5', None),
                        created,
                        modified
                    ))
                    
                    if item['type'] == 'dir':
                        sub_path = (parent + '/' + name) if parent else ("/" + name)
                        # The directory row is always recorded — the composite
                        # needs to know the folder exists. Only the descent is
                        # bounded, and a folder never entered is left out of
                        # scan_progress so nothing claims it was covered.
                        if max_depth is None or self._depth_of(sub_path, walk_root) < max_depth:
                            queue.append(sub_path)
                            # Mark subfolder as pending
                            storage.update_folder_status(scan_id, sub_path, 'pending')
                    else:
                        files_count += 1
                
                if len(batch) >= batch_size:
                    storage.save_files_batch(batch)
                    storage.checkpoint_to_disk()  # Check if conditions met for intermediate save
                    batch = []

                # Save checkpoint
                new_offset = offset + len(items)
                storage.save_checkpoint(scan_id, current_path, new_offset, total)

                offset = new_offset
                if offset >= total:
                    break
            
            # Mark folder as completed after processing all items
            storage.update_folder_status(scan_id, current_path, 'completed')
        
        if batch:
            storage.save_files_batch(batch)
        
        return files_count

class Analyzer:
    """Analyzes data from DB."""
    def __init__(self, storage):
        self.storage = storage
        # Cache for composite scans: {cache_key: (composite_data, timestamp)}
        self._composite_cache = {}
        self._cache_ttl = 300  # 5 minutes TTL for cache
        # Cache for composite scans: {cache_key: (composite_data, timestamp)}
        self._composite_cache = {}
        self._cache_ttl = 300  # 5 minutes TTL for cache

    def get_status(self):
        """Returns summary of recent scans."""
        conn = self.storage.get_connection()
        conn.row_factory = sqlite3.Row
        
        scans = conn.execute("""
            SELECT id, timestamp, scan_type, status, duration 
            FROM scans 
            ORDER BY id DESC LIMIT 5
        """).fetchall()
        
        return [dict(row) for row in scans]

    def get_scan_info(self, scan_id):
        """Returns detailed information about a specific scan."""
        return self.storage.get_scan_details(scan_id)

    def get_scans_list(self, limit=20):
        """Returns list of all scans with brief info."""
        return self.storage.get_all_scans(limit)

    # --- Full scan heuristics helpers ---

    def _get_config_full_scan_window_days(self):
        """Returns freshness window (days) from storage config or default."""
        config = getattr(self.storage, "config", None)
        if isinstance(config, dict):
            return int(config.get("full_scan_fresh_window_days", DEFAULT_CONFIG["full_scan_fresh_window_days"]))
        return DEFAULT_CONFIG["full_scan_fresh_window_days"]

    def _get_config_reference_full_scan_id(self):
        """Returns explicit reference_full_scan_id from config if set."""
        config = getattr(self.storage, "config", None)
        ref_id = None
        if isinstance(config, dict):
            ref_id = config.get("reference_full_scan_id", None)
        return ref_id

    @staticmethod
    def _recorded_scope(conn, scan_id):
        """`(scan_root, scan_depth)` as the scan recorded them, or None.

        None means the question cannot be answered from the scans table — the
        row is missing, or the database predates the columns and was never
        opened for writing since. Callers fall back to inferring from rows.
        """
        try:
            return conn.execute(
                "SELECT scan_root, scan_depth FROM scans WHERE id = ?", (scan_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None

    def scan_covers_root(self, scan_id):
        """True if the scan walked the whole disk, i.e. it can serve as a base.

        A scan started with `--path /Books` records nothing at the root, so it
        describes one subtree, not the disk. Such a scan may serve as a partial
        update on top of a base, never as the base itself: everything outside
        its subtree would silently vanish from the composite snapshot.

        Having a row at the root is necessary but no longer sufficient. A
        depth-bounded scan (`--path / --depth 1`) also writes root rows while
        covering almost nothing, and promoting one to base would collapse the
        snapshot from tens of thousands of files to a handful. So a recorded
        `scan_depth` disqualifies a scan outright, and a recorded `scan_root`
        other than the disk root does too. Scans written before those columns
        existed carry NULL in both and fall back to the row test, which is the
        behaviour they were built under.
        """
        conn = self.storage.get_connection()
        try:
            scope = self._recorded_scope(conn, scan_id)
            if scope is not None:
                scan_root, scan_depth = scope
                if scan_depth is not None:
                    return False
                if scan_root is not None and str(scan_root).strip("/"):
                    return False
            row = conn.execute(
                """
                SELECT 1 FROM files
                WHERE scan_id = ? AND parent_path IN ('', '/')
                LIMIT 1
                """,
                (scan_id,),
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def find_last_root_scan(self):
        """Newest successful cloud scan that covers the root, or None."""
        conn = self.storage.get_connection()
        try:
            rows = conn.execute(
                """
                SELECT id, timestamp, status
                FROM scans
                WHERE scan_type = 'cloud' AND status = 'success'
                ORDER BY id DESC
                """
            ).fetchall()
        finally:
            conn.close()
        for scan_id, timestamp, status in rows:
            if self.scan_covers_root(scan_id):
                return {"id": scan_id, "timestamp": timestamp, "status": status}
        return None

    def get_full_scan_candidates(self):
        """
        Returns list of recent cloud scans with heuristic metrics for full-scan selection.

        Used for diagnostics (CLI) and understanding why a particular scan was chosen.
        """
        recent_scans = self.get_recent_cloud_scans()
        if not recent_scans:
            return []

        # Filter only successful scans without pending/in_progress, and only
        # those that actually cover the root: a partial scan of one folder can
        # be the largest recent scan and still describe almost nothing.
        candidates = []
        for s in recent_scans:
            if s["status"] != "success":
                continue
            prog = s.get("progress") or {}
            pending = prog.get("pending", 0)
            in_progress = prog.get("in_progress", 0)
            if pending or in_progress:
                continue
            if not self.scan_covers_root(s["id"]):
                continue
            candidates.append(s)

        if not candidates:
            return []

        max_files = max(c["files_count"] for c in candidates) or 1
        alpha_files = 0.9  # threshold for "large enough" scans

        best_id = None
        best_score = -1.0

        enriched = []
        for c in candidates:
            f_files = c["files_count"] / max_files
            score = f_files
            enriched.append({
                "id": c["id"],
                "timestamp": c["timestamp"],
                "status": c["status"],
                "files_count": c["files_count"],
                "progress": c["progress"],
                "top_levels": c["top_levels"],
                "f_files": f_files,
                "score": score,
            })
            if f_files >= alpha_files and score > best_score:
                best_score = score
                best_id = c["id"]

        # If no candidate passed the threshold, pick the one with max files_count
        if best_id is None:
            best = max(enriched, key=lambda x: x["files_count"])
            best_id = best["id"]

        # Mark which candidate is currently considered the best
        for item in enriched:
            item["is_best"] = (item["id"] == best_id)

        return enriched

    def get_recent_cloud_scans(self):
        """
        Returns cloud scans within freshness window (in days) with basic metrics.

        Uses full_scan_fresh_window_days from config (default: 2 days).
        """
        conn = self.storage.get_connection()
        conn.row_factory = sqlite3.Row

        window_days = self._get_config_full_scan_window_days()

        # Select cloud scans within time window (relative to now)
        recent_scans = conn.execute(
            """
            SELECT id, timestamp, scan_type, status, duration
            FROM scans
            WHERE scan_type = 'cloud'
              AND timestamp >= datetime('now', ?)
            ORDER BY id DESC
            """,
            (f"-{window_days} days",)
        ).fetchall()

        result = []
        for scan in recent_scans:
            scan_id = scan["id"]

            # Files count for this scan
            files_count = conn.execute(
                "SELECT COUNT(*) FROM files WHERE scan_id = ? AND type = 'file'",
                (scan_id,)
            ).fetchone()[0]

            # Progress stats from scan_progress (pending/in_progress)
            progress = conn.execute(
                """
                SELECT 
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) AS in_progress,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending
                FROM scan_progress
                WHERE scan_id = ?
                """,
                (scan_id,)
            ).fetchone()

            # Coverage by top-level folders
            top_levels = conn.execute(
                """
                SELECT DISTINCT 
                    CASE 
                        WHEN parent_path LIKE '/%' THEN substr(parent_path, 2, 
                            INSTR(substr(parent_path, 2) || '/', '/') - 1)
                        ELSE parent_path
                    END AS top
                FROM files
                WHERE scan_id = ? AND type = 'file'
                """,
                (scan_id,)
            ).fetchall()
            top_set = sorted({row[0] for row in top_levels if row[0]})

            result.append({
                "id": scan_id,
                "timestamp": scan["timestamp"],
                "status": scan["status"],
                "duration": scan["duration"],
                "files_count": files_count,
                "progress": {
                    "completed": progress[0] or 0 if progress else 0,
                    "in_progress": progress[1] or 0 if progress else 0,
                    "pending": progress[2] or 0 if progress else 0,
                },
                "top_levels": top_set,
            })

        return result

    def get_scan_progress(self, scan_id):
        """Returns detailed progress info for a scan."""
        details = self.storage.get_scan_details(scan_id)
        if not details:
            return {"error": f"Scan {scan_id} not found"}
        
        pending_folders = self.storage.get_scan_pending_folders(scan_id)
        
        return {
            "scan": details,
            "pending_folders_sample": pending_folders,
            "pending_folders_count": len(pending_folders)
        }

    def is_full_scan(self, scan_id):
        """
        Determines if a cloud scan was full (started from root /) or partial.
        
        Args:
            scan_id: Scan ID to check
            
        Returns:
            bool: True if scan is full (has path='/' in scan_progress), False otherwise
        """
        conn = self.storage.get_connection()
        
        # Check if scan exists and is a cloud scan
        scan = conn.execute(
            "SELECT scan_type FROM scans WHERE id = ?",
            (scan_id,)
        ).fetchone()
        
        if not scan:
            return None  # Scan not found
        
        if scan[0] != 'cloud':
            return False  # Only cloud scans can be full/partial
        
        # Check if scan_progress has entry with path='/'
        root_entry = conn.execute(
            "SELECT COUNT(*) FROM scan_progress WHERE scan_id = ? AND path = '/'",
            (scan_id,)
        ).fetchone()
        
        return root_entry[0] > 0 if root_entry else False

    def get_scan_metadata(self, scan_id):
        """
        Returns metadata about a scan including type (full/partial) and statistics.
        
        Args:
            scan_id: Scan ID to analyze
            
        Returns:
            dict: Metadata including scan info, type, files count, folders count, root path
        """
        conn = self.storage.get_connection()
        
        # Get basic scan info
        scan = conn.execute(
            """SELECT id, timestamp, scan_type, status, duration 
            FROM scans WHERE id = ?""",
            (scan_id,)
        ).fetchone()
        
        if not scan:
            return {"error": f"Scan {scan_id} not found"}
        
        scan_id_val, timestamp, scan_type, status, duration = scan
        
        # Get files count
        files_count = conn.execute(
            "SELECT COUNT(*) FROM files WHERE scan_id = ? AND type = 'file'",
            (scan_id,)
        ).fetchone()[0]
        
        # Get folders count from scan_progress
        folders_count = conn.execute(
            "SELECT COUNT(*) FROM scan_progress WHERE scan_id = ?",
            (scan_id,)
        ).fetchone()[0]
        
        # Determine if full or partial (only for cloud scans)
        is_full = None
        root_path = None
        
        if scan_type == 'cloud':
            is_full = self.is_full_scan(scan_id)
            # Get root path from scan_progress (first path that was scanned)
            root_paths = conn.execute(
                """SELECT path FROM scan_progress 
                WHERE scan_id = ? 
                ORDER BY last_checked ASC 
                LIMIT 1""",
                (scan_id,)
            ).fetchone()
            root_path = root_paths[0] if root_paths else None
        
        return {
            "scan_id": scan_id_val,
            "timestamp": timestamp,
            "scan_type": scan_type,
            "status": status,
            "duration": duration,
            "files_count": files_count,
            "folders_count": folders_count,
            "is_full": is_full,
            "root_path": root_path
        }

    def find_last_full_scan(self, scan_id=None):
        """
        Finds the last full cloud scan.
        
        Args:
            scan_id: Optional specific scan ID to check (if provided, validates it's full)
            
        Returns:
            dict: Scan info with id, timestamp, files_count, or None if not found
        """
        conn = self.storage.get_connection()

        # 1) Если указан scan_id явно — валидируем и используем его как эталон
        if scan_id:
            scan = conn.execute(
                """
                SELECT id, timestamp, status
                FROM scans
                WHERE id = ? AND scan_type = 'cloud'
                """,
                (scan_id,),
            ).fetchone()

            if not scan:
                return {"error": f"Scan {scan_id} not found or is not a cloud scan"}

            meta = self.get_scan_metadata(scan_id)
            if meta.get("status") != "success":
                return {"error": f"Scan {scan_id} is not successful (status={meta.get('status')})"}

            return {
                "id": scan[0],
                "timestamp": scan[1],
                "status": scan[2],
                "files_count": meta.get("files_count", 0),
            }

        # 2) Если в конфиге задан reference_full_scan_id — пытаемся использовать его
        ref_id = self._get_config_reference_full_scan_id()
        if ref_id:
            try:
                ref_id_int = int(ref_id)
                return self.find_last_full_scan(scan_id=ref_id_int)
            except Exception:
                # Плохо задан id в конфиге, игнорируем и идем дальше
                pass

        # 3) Автоматический выбор эталонного скана на основе свежих cloud-сканов
        candidates = self.get_full_scan_candidates()
        if not candidates:
            # Fallback: нет свежих кандидатов — берём последний скан, который
            # реально покрывает корень, даже если он старый. Композит с базой
            # из частичного скана теряет всё, что вне его поддерева.
            root_scan = self.find_last_root_scan()
            if not root_scan:
                return None

            files_count = conn.execute(
                "SELECT COUNT(*) FROM files WHERE scan_id = ? AND type = 'file'",
                (root_scan["id"],)
            ).fetchone()[0]

            return {
                "id": root_scan["id"],
                "timestamp": root_scan["timestamp"],
                "status": root_scan["status"],
                "files_count": files_count,
            }

        # Найти помеченный как лучший
        best = None
        for c in candidates:
            if c.get("is_best"):
                best = c
                break

        if not best:
            if candidates:
                # Safety fallback: взять самый большой по количеству файлов
                best = max(candidates, key=lambda x: x["files_count"])
            else:
                # Нет кандидатов — последний скан, покрывающий корень.
                root_scan = self.find_last_root_scan()
                if not root_scan:
                    return None

                files_count = conn.execute(
                    "SELECT COUNT(*) FROM files WHERE scan_id = ? AND type = 'file'",
                    (root_scan["id"],)
                ).fetchone()[0]

                return {
                    "id": root_scan["id"],
                    "timestamp": root_scan["timestamp"],
                    "status": root_scan["status"],
                    "files_count": files_count,
                }

        return {
            "id": best["id"],
            "timestamp": best["timestamp"],
            "status": best["status"],
            "files_count": best["files_count"],
        }

    def get_folders_in_scan(self, scan_id):
        """
        Gets list of all folders (parent_paths) that were scanned in a given scan.
        
        Args:
            scan_id: Scan ID to analyze
            
        Returns:
            set: Set of parent_path values from files table for this scan
        """
        conn = self.storage.get_connection()
        
        # Get all distinct parent_paths from files table
        folders = conn.execute(
            """SELECT DISTINCT parent_path 
            FROM files 
            WHERE scan_id = ? AND type = 'file'""",
            (scan_id,)
        ).fetchall()
        
        return {row[0] for row in folders}

    @staticmethod
    def _common_ancestor(paths):
        """Deepest folder that contains every path in `paths`."""
        components = None
        for path in paths:
            if path is None:
                continue
            parts = [part for part in str(path).strip("/").split("/") if part]
            if components is None:
                components = parts
            else:
                shared = []
                for left, right in zip(components, parts):
                    if left != right:
                        break
                    shared.append(left)
                components = shared
            if not components:
                break
        if not components:
            return "/"
        return "/" + "/".join(components)

    def scan_root_path(self, scan_id):
        """Where a scan started, derived from what it recorded.

        `scan cloud --path X` does not store X anywhere, so this has to be
        inferred. It used to be read as "the scan_progress row with the
        earliest last_checked" — but last_checked marks when a folder
        *finished*, and the first folder to finish is a deep leaf, not the
        root. Scan 93 (`--path /Books`, 437 folders) resolved to
        `/Books/ментальные карты/yang_super`, and the "folder must be under
        the scan root" filter in build_composite_scan() then discarded 436 of
        its 437 folder updates. The common ancestor of everything the scan
        touched is the honest answer.

        Scans written since `scans.scan_root` exists do not need inferring at
        all — they said where they started. The common-ancestor path stays for
        the scans that came before.
        """
        conn = self.storage.get_connection()
        try:
            scope = self._recorded_scope(conn, scan_id)
            if scope is not None and scope[0] is not None:
                recorded = str(scope[0]).strip("/")
                # A recorded "/" means the scan deliberately started at the disk
                # root, which is a real scope. The inference path below returns
                # None for the same string, because there "the common ancestor
                # is /" means only "this scan spans folders with nothing in
                # common" — an answer, not a scope. Same character, opposite
                # amounts of knowledge, so they must not collapse together.
                return "/" + recorded if recorded else "/"
            paths = [
                row[0] for row in conn.execute(
                    "SELECT path FROM scan_progress WHERE scan_id = ?", (scan_id,)
                )
            ]
            paths += [
                row[0] for row in conn.execute(
                    "SELECT DISTINCT parent_path FROM files WHERE scan_id = ?", (scan_id,)
                )
            ]
        finally:
            conn.close()
        if not paths:
            return None
        root = self._common_ancestor(paths)
        return None if root == "/" else root

    def find_partial_scans_after(self, base_scan_id):
        """
        Finds all partial (non-full) cloud scans that occurred after the base scan.
        
        Args:
            base_scan_id: Base full scan ID (all partial scans must be after this)
            
        Returns:
            list: List of dicts with scan info (id, timestamp, status, root_path)
        """
        conn = self.storage.get_connection()
        
        # Verify base scan exists and is a cloud scan
        base_scan = conn.execute(
            "SELECT id, timestamp FROM scans WHERE id = ? AND scan_type = 'cloud'",
            (base_scan_id,)
        ).fetchone()
        
        if not base_scan:
            return []
        
        base_timestamp = base_scan[1]
        
        # Find all cloud scans after the base that are NOT full scans. "Full"
        # is whatever scan_covers_root() says it is — this used to be its own
        # SQL test (`has a scan_progress row for '/'`), and a depth-limited
        # scan of the root satisfies that test while covering almost nothing.
        # Such a scan would then be dropped from the updates entirely, which
        # is the opposite of what it is for. One definition, one place.
        candidates = conn.execute("""
            SELECT s.id, s.timestamp, s.status
            FROM scans s
            WHERE s.scan_type = 'cloud'
            AND s.id > ?
            ORDER BY s.id ASC
        """, (base_scan_id,)).fetchall()
        partial_scans = [row for row in candidates if not self.scan_covers_root(row[0])]

        result = []
        for scan_id, timestamp, status in partial_scans:
            # Where this partial scan started: the common ancestor of every
            # folder it recorded. See scan_root_path() for why "the first
            # scan_progress row" was wrong.
            root_path = self.scan_root_path(scan_id)
            
            result.append({
                "id": scan_id,
                "timestamp": timestamp,
                "status": status,
                "root_path": root_path
            })
        
        return result

    def build_composite_scan(self, cloud_scan_id=None, use_cache=True):
        """
        Builds a composite scan from base full scan + partial scan updates.
        Uses caching to avoid rebuilding on repeated calls.
        
        Args:
            cloud_scan_id: Optional specific full scan ID to use as base
            use_cache: If True, use cached result if available and fresh
            
        Returns:
            dict: Composite scan structure with base_scan_id and folder_updates mapping
                  {parent_path: scan_id} for folders that have newer partial scans
        """
        import time
        
        # Check cache if enabled
        if use_cache:
            cache_key = f"composite_{cloud_scan_id or 'auto'}"
            if cache_key in self._composite_cache:
                cached_data, cache_time = self._composite_cache[cache_key]
                if time.time() - cache_time < self._cache_ttl:
                    # Return cached data
                    return cached_data
                else:
                    # Cache expired, remove it
                    del self._composite_cache[cache_key]
        
        conn = self.storage.get_connection()
        
        # 1. Find base full scan
        # If cloud_scan_id is provided explicitly, use it as base (validated in find_last_full_scan)
        # Otherwise, use heuristic reference full scan based on freshness window and config.
        base_scan = self.find_last_full_scan(cloud_scan_id)
        if not base_scan or "error" in base_scan:
            # There used to be a fallback here that took the most recent cloud
            # scan, whatever it was, with no check that it covered the disk
            # root. That turned the coverage gate off at the exact moment it
            # had done its job: find_last_full_scan() returns None only when
            # every candidate was rejected, and the fallback then installed a
            # rejected one anyway — a `--path /Books` scan, a `--depth 1` scan
            # of three files, or a crashed one — as the base for the whole
            # disk. Silently. See tasks/diff_correctness/GAP.md.
            #
            # The distinction that path missed: a fallback may relax the *soft*
            # criterion, freshness, and never the *hard* one, coverage.
            # find_last_full_scan() already relaxes freshness internally, by
            # falling back to the newest root-covering scan however old it is.
            # So its None is not "try something else", it is the final answer.
            if isinstance(base_scan, dict) and "error" in base_scan:
                return {"error": base_scan["error"]}
            has_cloud_scan = conn.execute(
                "SELECT 1 FROM scans WHERE scan_type = 'cloud' LIMIT 1"
            ).fetchone()
            if not has_cloud_scan:
                return {"error": "No cloud scan found"}
            return {
                "error": (
                    "No cloud scan covers the disk root, so there is no base to "
                    "build a snapshot on. Every scan present describes one subtree "
                    "or was depth-limited; using one as the base would claim the "
                    "whole disk is that subtree. Run `python3 ydm.py scan cloud` "
                    "without --path or --depth."
                )
            }

        base_scan_id = base_scan["id"]

        # 2. Find all partial scans after base
        partial_scans = self.find_partial_scans_after(base_scan_id)

        if not partial_scans:
            # No partial scans -- just the base scan. Falls through to the
            # single cache+return path below (this used to `return` here
            # directly, which meant this specific result was never cached).
            folder_to_scan = {}
        else:
            # 3. For each partial scan, determine which folders it covers
            # Build mapping: folder -> latest scan_id that covers it
            # Strategy: Process scans in order (oldest first), then apply priority rules
            folder_to_scan = {}
            scan_metadata = {}  # Store scan metadata for priority decisions

            # Sort partial scans by scan_id (oldest first) for proper priority handling
            sorted_partial_scans = sorted(partial_scans, key=lambda x: x["id"])

            for partial_scan in sorted_partial_scans:
                scan_id = partial_scan["id"]
                root_path = partial_scan["root_path"]
                timestamp = partial_scan.get("timestamp")

                # None means the scan's scope could not be determined; skip it
                # rather than guess. "/" is a determined scope — a scan that
                # recorded starting at the disk root — and is kept.
                if root_path is None:
                    continue

                # Store metadata for this scan
                scan_metadata[scan_id] = {
                    "root_path": root_path,
                    "timestamp": timestamp,
                    "scan_id": scan_id
                }

                # Get all folders in this partial scan
                folders = self.get_folders_in_scan(scan_id)

                # Normalize root_path for comparison
                normalized_root = root_path.rstrip('/')
                if not normalized_root:
                    normalized_root = ""

                # For each folder in this scan, apply priority rules
                for folder_path in folders:
                    # Check if this folder is under the root_path of this partial scan
                    if normalized_root:
                        # Folder must start with root_path or be equal to it
                        if folder_path.startswith(normalized_root + '/') or folder_path == normalized_root:
                            # Priority rules:
                            # 1. If folder not in mapping - add it
                            # 2. If folder already mapped - use newer scan (higher scan_id)
                            # 3. If same scan_id - keep existing (shouldn't happen, but safe)
                            if folder_path not in folder_to_scan:
                                folder_to_scan[folder_path] = scan_id
                            else:
                                existing_scan_id = folder_to_scan[folder_path]
                                # Use newer scan (higher scan_id = more recent)
                                if scan_id > existing_scan_id:
                                    folder_to_scan[folder_path] = scan_id
                                # If scan_ids are equal (shouldn't happen), keep existing
                    else:
                        # Root scan - should not happen for partial scans, but handle it
                        if folder_path not in folder_to_scan:
                            folder_to_scan[folder_path] = scan_id
                        elif scan_id > folder_to_scan[folder_path]:
                            folder_to_scan[folder_path] = scan_id

            # NOTE: folder_to_scan is keyed by each folder's own exact
            # parent_path, and _compare_composite_scan() consumes it by
            # exact parent_path match, not by hierarchy/prefix -- so a
            # parent folder ("/A") and a nested folder ("/A/B") are
            # independent keys referring to disjoint sets of files (files
            # directly in /A vs files directly in /A/B), not a "conflict"
            # to resolve. A previous version of this method had a "nested
            # folder conflict" pass here that dropped the parent entry
            # whenever a more specific nested entry was also present,
            # which silently discarded legitimate updates to files
            # directly in the parent folder. Removed -- keeping every
            # distinct folder_path entry is correct; genuine collisions
            # (two scans claiming the *same* folder_path) are already
            # resolved above by preferring the higher/more recent scan_id.

            self._retire_deleted_folders(
                conn, base_scan_id, sorted_partial_scans, folder_to_scan
            )

        result = {
            "base_scan_id": base_scan_id,
            "folder_updates": folder_to_scan,
            "partial_scans_count": len(partial_scans),
            "updated_folders_count": len(folder_to_scan),
        }

        # Cache the result if enabled -- covers every outcome above,
        # including the "no partial scans" case.
        if use_cache:
            cache_key = f"composite_{cloud_scan_id or 'auto'}"
            self._composite_cache[cache_key] = (result, time.time())
            # Limit cache size (keep only last 10 entries)
            if len(self._composite_cache) > 10:
                # Remove oldest entry
                oldest_key = min(self._composite_cache.keys(),
                               key=lambda k: self._composite_cache[k][1])
                del self._composite_cache[oldest_key]

        return result
    
    def _retire_deleted_folders(self, conn, base_scan_id, partial_scans, folder_to_scan):
        """Let a partial scan report that a folder inside its subtree is gone.

        The composite serves any folder no partial scan touched from the base,
        which is right for a folder that simply was not looked at, and wrong
        for one that was looked at and no longer exists. `report diff` was
        still claiming a file under `/brtn/Запчасти/фото` on 2026-08-23; the
        folder had been deleted from the disk long enough ago to be out of the
        trash, so no delta sweep could see it either, and a rescan of the
        parent did not help because "absent" was indistinguishable from
        "uncovered".

        A scan only gets to retire a folder when it is beyond doubt that it
        would have found one: it must be `success` (not crashed or
        interrupted mid-walk), it must have recorded where it started, and it
        must not have been depth-limited. Scans predating `scans.scan_root`
        carry NULL and are trusted for nothing here — the old behaviour, which
        keeps stale rows, is the safe direction to be wrong in.

        Retirement is expressed as an ordinary folder update rather than a new
        kind of entry. The scan that proved the folder gone becomes its
        serving scan, and since that scan holds no rows for it, every existing
        reader already yields zero files without knowing this concept exists.
        """
        trustworthy = []
        for partial in partial_scans:
            scope = self._recorded_scope(conn, partial["id"])
            if scope is None or scope[0] is None or scope[1] is not None:
                continue
            if partial.get("status") != "success":
                continue
            root = str(scope[0]).strip("/")
            if not root:
                # A scan rooted at the disk root that walked everything is a
                # full scan and would have replaced the base outright. Reaching
                # here means it is partial for some other reason, and letting
                # it retire folders would put the entire snapshot in range of
                # one ambiguous row. Not worth the blast radius.
                continue
            trustworthy.append((partial["id"], root))
        if not trustworthy:
            return

        # Newest first, so a retired folder records the most recent scan that
        # looked for it. Any of them yields the same (empty) file set, but the
        # scan id is what a human reads when asking why a folder went away.
        trustworthy.reverse()

        base_folders = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT parent_path FROM files WHERE scan_id = ?",
                (base_scan_id,),
            )
        ]
        for scan_id, root in trustworthy:
            for folder in base_folders:
                if folder in folder_to_scan:
                    continue  # some scan found it; it is not gone
                normalized = str(folder).strip("/")
                if not (normalized == root or normalized.startswith(root + "/")):
                    continue  # outside this scan's subtree — it never looked
                folder_to_scan[folder] = scan_id

    def clear_composite_cache(self):
        """Clears the composite scan cache."""
        self._composite_cache.clear()

    def _composite_cloud_files(self, composite):
        """{(compare_path, name): size} for the composite cloud snapshot.

        The composite resolves per folder: each folder is served by the newest
        scan that covered it, and by the base scan otherwise. The previous
        implementation expressed that as
        `WHERE scan_id IN (...) AND parent_path IN (...)`, a cross product of
        every scan with every folder — so any scan holding rows for a folder
        could win, at random, instead of the one the composite assigned.
        """
        base_scan_id = composite["base_scan_id"]
        folder_updates = composite.get("folder_updates") or {}

        by_scan = {}
        for folder, scan_id in folder_updates.items():
            by_scan.setdefault(scan_id, set()).add(folder)

        conn = self.storage.get_connection()
        try:
            files = {}
            for parent, name, size in conn.execute(
                "SELECT parent_path, name, size FROM files "
                "WHERE scan_id = ? AND type = 'file'",
                (base_scan_id,),
            ):
                if parent in folder_updates:
                    continue  # a newer scan owns this folder
                files[(normalize_compare_path(parent), name)] = size

            for scan_id, folders in by_scan.items():
                for parent, name, size in conn.execute(
                    "SELECT parent_path, name, size FROM files "
                    "WHERE scan_id = ? AND type = 'file'",
                    (scan_id,),
                ):
                    if parent in folders:
                        files[(normalize_compare_path(parent), name)] = size
        finally:
            conn.close()
        return files

    def _scan_files(self, scan_id):
        """{(compare_path, name): size} for a single scan."""
        conn = self.storage.get_connection()
        try:
            return {
                (normalize_compare_path(parent), name): size
                for parent, name, size in conn.execute(
                    "SELECT parent_path, name, size FROM files "
                    "WHERE scan_id = ? AND type = 'file'",
                    (scan_id,),
                )
            }
        finally:
            conn.close()

    def _diff_file_sets(self, cloud_files, local_id, exclude_dirs, compare_scans, extra=None):
        """The one place cloud is compared with local.

        This logic used to exist in three copies — composite, simple, and a
        dead tail in get_diff() — which is how the path-convention mismatch
        below survived: cloud rows store parent_path as "/pro/MuSy", local
        rows as "pro/MuSy", so the join `l.parent_path = c.parent_path`
        matched nothing at all and every file was reported missing on both
        sides. normalize_compare_path() settles the convention in one place.
        """
        local_files = self._scan_files(local_id)

        missing_local = []
        excluded = 0
        for (parent, name), size in cloud_files.items():
            if (parent, name) in local_files:
                continue
            if is_path_excluded(f"{parent}/{name}".strip("/"), exclude_dirs):
                excluded += 1
                continue
            missing_local.append((parent, name, size))

        missing_cloud = []
        local_ignored = 0
        for (parent, name), size in local_files.items():
            if (parent, name) in cloud_files:
                continue
            if name.startswith(".sync") or parent == ".sync" or parent.startswith(".sync/"):
                # The daemon's own working directory: local by design, never
                # in the cloud. It used to be dropped here without being
                # counted, which left the local side of the report unable to
                # add up — the cloud side has always declared its exclusions,
                # and an undeclared hole is how the last three defects stayed
                # invisible for months.
                local_ignored += 1
                continue
            missing_cloud.append((parent, name, size))

        missing_local.sort()
        missing_cloud.sort()

        result = {
            "compare_scans": compare_scans,
            "cloud_files_count": len(cloud_files),
            "local_files_count": len(local_files),
            "matched_count": len(set(cloud_files) & set(local_files)),
            "excluded_from_sync_count": excluded,
            "local_ignored_count": local_ignored,
            "missing_local_count": len(missing_local),
            "missing_cloud_count": len(missing_cloud),
            "missing_local_sample": [f"/{p}/{n}".replace("//", "/") for p, n, _ in missing_local[:20]],
            "missing_cloud_sample": [f"/{p}/{n}".replace("//", "/") for p, n, _ in missing_cloud[:20]],
        }
        if extra:
            result.update(extra)
        return result

    def _compare_composite_scan(self, composite, local_id, exclude_dirs):
        """Compares the composite cloud snapshot (base + partial updates) with a local scan."""
        base_scan_id = composite["base_scan_id"]
        folder_updates = composite.get("folder_updates") or {}
        if not folder_updates:
            return self._compare_simple_scan(base_scan_id, local_id, exclude_dirs)

        # Same exclusion list the comparison itself uses, so "stale but never
        # compared" means exactly what this diff means by not comparing it.
        freshness = self.snapshot_freshness(composite, exclude_dirs=exclude_dirs)
        return self._diff_file_sets(
            self._composite_cloud_files(composite),
            local_id,
            exclude_dirs,
            compare_scans={
                "cloud": f"composite(base={base_scan_id}, partials={len(folder_updates)})",
                "local": local_id,
            },
            extra={
                "composite_info": {
                    "base_scan_id": base_scan_id,
                    "updated_folders_count": len(folder_updates),
                },
                # Without this, a finding reads as fact even when the evidence
                # under it is months old.
                "snapshot_freshness": freshness,
                "warnings": freshness.get("warnings", []),
            },
        )

    def _compare_simple_scan(self, cloud_id, local_id, exclude_dirs):
        """Compares one cloud scan with one local scan, no composite involved."""
        return self._diff_file_sets(
            self._scan_files(cloud_id),
            local_id,
            exclude_dirs,
            compare_scans={"cloud": cloud_id, "local": local_id},
        )

    def get_diff(self, cloud_scan_id=None, local_scan_id=None, use_composite=True,
                 exclude_dirs=None):
        """
        Compares cloud scan vs local scan with optional composite scan support.
        
        Args:
            cloud_scan_id: Specific cloud scan ID to use (optional, uses last if not specified)
            local_scan_id: Specific local scan ID to use (optional, uses last successful if not specified)
            use_composite: If True and cloud_scan_id not specified, build composite scan from base + partials
            exclude_dirs: folders this comparison must not reach. Omitted means
                the daemon's `exclude-dirs`, which is what the CLI wants; a
                caller that knows its backend passes its own list, because on
                rclone the daemon's blacklist is not in force and reading it
                would exclude folders this diff does compare.

        Returns:
            dict: Comparison results with missing files counts and samples
        """
        if exclude_dirs is None:
            exclude_dirs = load_exclude_dirs()

        conn = self.storage.get_connection()
        
        # Определяем local scan ID (независимо от режима)
        if local_scan_id:
            # Валидация указанного local scan
            local_scan = conn.execute(
                "SELECT id, scan_type, status FROM scans WHERE id = ? AND scan_type = 'local'",
                (local_scan_id,)
            ).fetchone()
            if not local_scan:
                return {"error": f"Local scan {local_scan_id} not found or is not a local scan"}
            if local_scan[2] != 'success':
                return {"error": f"Local scan {local_scan_id} is not successful (status: {local_scan[2]})"}
            local_id = local_scan_id
        else:
            # Берем последний успешный local scan
            last_local = conn.execute("SELECT id FROM scans WHERE scan_type='local' AND status='success' ORDER BY id DESC LIMIT 1").fetchone()
            if not last_local:
                return {"error": "No successful local scans found"}
            local_id = last_local[0]
        
        # Определяем cloud scan: композитный режим или простой
        if cloud_scan_id:
            # Явно указан cloud scan - используем простой режим (не композитный)
            cloud_scan = conn.execute(
                "SELECT id, scan_type, status FROM scans WHERE id = ? AND scan_type = 'cloud'",
                (cloud_scan_id,)
            ).fetchone()
            if not cloud_scan:
                return {"error": f"Cloud scan {cloud_scan_id} not found or is not a cloud scan"}
            cloud_id = cloud_scan_id
            # Простое сравнение
            return self._compare_simple_scan(cloud_id, local_id, exclude_dirs)
        
        # cloud_scan_id не указан - проверяем, использовать ли композитный режим
        if use_composite:
            # Пытаемся построить композитный снимок
            composite = self.build_composite_scan()
            if "error" in composite:
                # Не удалось построить композитный - fallback на последний скан
                last_cloud = conn.execute("SELECT id FROM scans WHERE scan_type='cloud' ORDER BY id DESC LIMIT 1").fetchone()
                if not last_cloud:
                    return {"error": "No cloud scans found"}
                cloud_id = last_cloud[0]
                return self._compare_simple_scan(cloud_id, local_id, exclude_dirs)
            
            # Используем композитный снимок
            return self._compare_composite_scan(composite, local_id, exclude_dirs)
        else:
            # Композитный режим отключен - используем последний скан
            last_cloud = conn.execute("SELECT id FROM scans WHERE scan_type='cloud' ORDER BY id DESC LIMIT 1").fetchone()
            if not last_cloud:
                return {"error": "No cloud scans found"}
            cloud_id = last_cloud[0]
            return self._compare_simple_scan(cloud_id, local_id, exclude_dirs)

    def get_long_paths(self, scan_id, limit_chars=240):
        """Find files exceeding path length limit."""
        conn = self.storage.get_connection()
        
        # Get all files for scan and compute full paths
        files = conn.execute(
            """SELECT parent_path, name, size 
            FROM files WHERE scan_id = ?""",
            (scan_id,)
        ).fetchall()
        
        long_paths = []
        for parent_path, name, size in files:
            full_path = f"{parent_path}/{name}".strip("/") if parent_path else name
            if len(full_path) > limit_chars:
                long_paths.append({
                    "path": full_path,
                    "length": len(full_path),
                    "size": size
                })
        
        # Sort by length descending
        long_paths.sort(key=lambda x: x["length"], reverse=True)
        
        return {
            "scan_id": scan_id,
            "limit_chars": limit_chars,
            "long_paths_count": len(long_paths),
            "long_paths": long_paths[:100]  # Return top 100
        }

    def analyze_scan(self, scan_id):
        """Quick integrity check for scan data."""
        conn = self.storage.get_connection()
        
        # Get scan info
        scan = conn.execute(
            """SELECT id, timestamp, scan_type, status, duration 
            FROM scans WHERE id = ?""",
            (scan_id,)
        ).fetchone()
        
        if not scan:
            return {"error": f"Scan {scan_id} not found"}
        
        # Count files
        files_count = conn.execute(
            "SELECT COUNT(*) FROM files WHERE scan_id = ?",
            (scan_id,)
        ).fetchone()[0]
        
        # Count folders
        folders_count = conn.execute(
            "SELECT COUNT(*) FROM scan_progress WHERE scan_id = ?",
            (scan_id,)
        ).fetchone()[0]
        
        # Get scan_progress status breakdown
        progress = conn.execute(
            """SELECT 
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
            SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending
            FROM scan_progress WHERE scan_id = ?""",
            (scan_id,)
        ).fetchone()
        
        warnings = []
        
        # Warn if files_count=0 but status suggests completion
        if files_count == 0 and scan[3] in ('success', 'started'):
            warnings.append(f"⚠️  No files found but status is '{scan[3]}' - possible data loss")
        
        # Warn if scan_progress has in_progress but scan is success
        if progress[1] and scan[1] > 0 and scan[3] == 'success':
            warnings.append(f"⚠️  {progress[1]} folders still in_progress but scan marked success")
        
        return {
            "scan": {
                "id": scan[0],
                "timestamp": scan[1],
                "type": scan[2],
                "status": scan[3],
                "duration": scan[4]
            },
            "statistics": {
                "files_count": files_count,
                "folders_count": folders_count,
                "completed": progress[0] or 0,
                "in_progress": progress[1] or 0,
                "pending": progress[2] or 0
            },
            "warnings": warnings
        }

    def get_duplicates(self, scan_id, by_hash=True):
        """Find duplicate files by MD5 hash or by size+name."""
        conn = self.storage.get_connection()
        
        if by_hash:
            # Find by MD5 hash
            duplicates = conn.execute(
                """SELECT md5, COUNT(*) as count, 
                GROUP_CONCAT(parent_path || '/' || name, '|') as paths,
                SUM(size) as total_size
                FROM files 
                WHERE scan_id = ? AND md5 IS NOT NULL AND md5 != ''
                GROUP BY md5 
                HAVING count > 1
                ORDER BY total_size DESC""",
                (scan_id,)
            ).fetchall()
            
            result = {
                "scan_id": scan_id,
                "method": "MD5 hash",
                "duplicates": []
            }
            
            for md5, count, paths_str, total_size in duplicates:
                result["duplicates"].append({
                    "hash": md5,
                    "file_count": count,
                    "total_size": total_size,
                    "files": paths_str.split("|")
                })
        else:
            # Find by size + name
            duplicates = conn.execute(
                """SELECT size, name, COUNT(*) as count, 
                GROUP_CONCAT(parent_path || '/' || name, '|') as paths
                FROM files 
                WHERE scan_id = ?
                GROUP BY size, name 
                HAVING count > 1
                ORDER BY size DESC""",
                (scan_id,)
            ).fetchall()
            
            result = {
                "scan_id": scan_id,
                "method": "size+name",
                "duplicates": []
            }
            
            for size, name, count, paths_str in duplicates:
                result["duplicates"].append({
                    "size": size,
                    "name": name,
                    "occurrence_count": count,
                    "files": paths_str.split("|")
                })
        
        result["total_duplicates"] = len(result["duplicates"])
        return result

    def _folder_file_counts(self, scan_id):
        """{parent_path: file count} for one scan."""
        conn = self.storage.get_connection()
        try:
            return dict(conn.execute(
                "SELECT parent_path, COUNT(*) FROM files "
                "WHERE scan_id = ? AND type = 'file' GROUP BY parent_path",
                (scan_id,),
            ).fetchall())
        finally:
            conn.close()

    def snapshot_freshness(self, composite=None, stale_after_days=30, exclude_dirs=None):
        """How old the composite snapshot actually is, and where it matters.

        Every consumer reads the composite as if it were current. It is not:
        folders nobody rescanned are served by the base, which here is five
        months old — that is why `report diff` reported a file as missing
        locally when in truth it had been deleted from the cloud in a folder
        no scan has visited since March. Reporting a finding without saying
        how old the evidence is invites chasing ghosts.

        Age alone, though, is not a reason to warn. On this disk all 36 028
        files still served by the March base sit under `exclude-dirs` —
        `downloads`, `журналы`, `music` and eighteen others — which nothing
        ever compares against the local copy. A warning about them would fire
        on every run for the rest of the project's life while changing no
        decision, and a warning that always fires is one nobody reads when it
        finally means something. So staleness is split: files that some
        comparison could actually reach, and files that no comparison touches.
        Only the first kind warns.
        """
        composite = composite or self.build_composite_scan(use_cache=False)
        if "error" in composite:
            return {"error": composite["error"]}

        # No list means nothing is excluded — not "go and read the daemon's
        # config". Until 2026-08-27 this fell back to `load_exclude_dirs()`,
        # which resolves the default path, so a caller that had deliberately
        # passed no list got the live daemon's exclusions applied to its
        # numbers. The menu is exactly such a caller: under rclone the daemon's
        # blacklist is not in force, and folders it names are compared. They
        # were being counted as "never compared", which suppresses the warning
        # — the direction that hides a stale snapshot instead of showing it.
        exclude_dirs = set() if exclude_dirs is None else exclude_dirs
        base_scan_id = composite["base_scan_id"]
        folder_updates = composite.get("folder_updates") or {}

        conn = self.storage.get_connection()
        try:
            wanted = set(folder_updates.values()) | {base_scan_id}
            times = dict(conn.execute(
                "SELECT id, timestamp FROM scans WHERE id IN (%s)"
                % ",".join("?" * len(wanted)),
                tuple(wanted),
            ).fetchall())
        finally:
            conn.close()

        base_counts = self._folder_file_counts(base_scan_id)
        files_from_base = 0
        stale_compared = 0  # served by the old base AND reachable by a diff
        stale_excluded = 0  # served by the old base but never compared
        for folder, count in base_counts.items():
            if folder in folder_updates:
                continue
            files_from_base += count
            if is_path_excluded(normalize_compare_path(folder), exclude_dirs):
                stale_excluded += count
            else:
                stale_compared += count
        by_scan = {}
        for folder, scan_id in folder_updates.items():
            by_scan.setdefault(scan_id, set()).add(folder)
        files_from_updates = 0
        for scan_id, folders in by_scan.items():
            counts = self._folder_file_counts(scan_id)
            files_from_updates += sum(counts.get(folder, 0) for folder in folders)

        def age_days(timestamp):
            parsed = _parse_db_timestamp(timestamp)
            if parsed is None:
                return None
            # scans.timestamp is UTC; comparing against local time would
            # skew the age by the machine's offset (+07 here).
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            return (now - parsed).days

        base_at = times.get(base_scan_id)
        update_times = [times[s] for s in by_scan if times.get(s)]
        total = files_from_base + files_from_updates
        base_age = age_days(base_at)

        warnings = []
        if base_age is not None and base_age > stale_after_days and stale_compared:
            share = round(stale_compared * 100 / total, 1) if total else 0.0
            warnings.append(
                f"{stale_compared} synced file(s) ({share}% of the snapshot) come "
                f"from scan #{base_scan_id}, {base_age} days old, and are compared "
                f"against the local copy. Findings there may be stale. Refresh with: "
                f"python3 tools/cloud_delta.py changes"
            )

        return {
            "base_scan_id": base_scan_id,
            "base_at": base_at,
            "base_age_days": base_age,
            "folder_updates": len(folder_updates),
            "newest_update_at": max(update_times) if update_times else None,
            "files_from_base": files_from_base,
            "files_from_updates": files_from_updates,
            "base_share_percent": round(files_from_base * 100 / total, 1) if total else 0.0,
            # The split that decides whether the age above is worth acting on.
            "stale_compared_files": stale_compared,
            "stale_excluded_files": stale_excluded,
            "warnings": warnings,
        }

    def prune_plan(self, keep_local=3, keep_root_scans=2):
        """Which scans are safe to delete, and what they cost.

        This database only grows: every partial scan adds rows, and old full
        scans are never reclaimed. Deleting is not free of meaning, though —
        tracking change over time is one of the project's purposes — so the
        rule is conservative and everything it protects is listed explicitly.

        Kept, always:
          - the composite base and every scan the composite draws a folder from
          - an explicit `reference_full_scan_id` from the config
          - the `keep_root_scans` most recent scans covering the disk root
          - every cloud scan newer than the base (they may become updates)
          - the `keep_local` most recent successful local scans
        """
        composite = self.build_composite_scan(use_cache=False)
        base_scan_id = composite.get("base_scan_id")
        folder_updates = composite.get("folder_updates") or {}

        conn = self.storage.get_connection()
        try:
            scans = conn.execute(
                "SELECT id, scan_type, status, timestamp FROM scans ORDER BY id"
            ).fetchall()
            row_counts = dict(
                conn.execute("SELECT scan_id, COUNT(*) FROM files GROUP BY scan_id").fetchall()
            )
        finally:
            conn.close()

        keep = {}

        def protect(scan_id, reason):
            if scan_id is not None:
                keep.setdefault(scan_id, reason)

        protect(base_scan_id, "composite base")
        for scan_id in set(folder_updates.values()):
            protect(scan_id, "supplies folders to the composite")
        reference = self._get_config_reference_full_scan_id()
        if reference:
            try:
                protect(int(reference), "reference_full_scan_id in config")
            except (TypeError, ValueError):
                pass

        cloud_ids = [row[0] for row in scans if row[1] == "cloud"]
        root_scans = [sid for sid in sorted(cloud_ids, reverse=True) if self.scan_covers_root(sid)]
        for scan_id in root_scans[:keep_root_scans]:
            protect(scan_id, "recent root scan")
        if base_scan_id is not None:
            for scan_id in cloud_ids:
                if scan_id > base_scan_id:
                    protect(scan_id, "newer than the composite base")

        local_ids = [row[0] for row in scans if row[1] == "local" and row[2] == "success"]
        for scan_id in sorted(local_ids, reverse=True)[:keep_local]:
            protect(scan_id, "recent local scan")

        prunable = []
        for scan_id, scan_type, status, timestamp in scans:
            if scan_id in keep:
                continue
            prunable.append({
                "scan_id": scan_id,
                "scan_type": scan_type,
                "status": status,
                "timestamp": timestamp,
                "rows": row_counts.get(scan_id, 0),
            })

        total_rows = sum(row_counts.values())
        prunable_rows = sum(item["rows"] for item in prunable)
        return {
            "base_scan_id": base_scan_id,
            "folder_updates": len(folder_updates),
            "keep_local": keep_local,
            "keep_root_scans": keep_root_scans,
            "kept": [
                {"scan_id": scan_id, "reason": reason}
                for scan_id, reason in sorted(keep.items())
            ],
            "prunable": prunable,
            "prunable_scans": len(prunable),
            "prunable_rows": prunable_rows,
            "total_rows": total_rows,
            "prunable_share_percent": round(prunable_rows * 100 / total_rows, 1) if total_rows else 0.0,
        }

    def prune(self, keep_local=3, keep_root_scans=2, apply=False, vacuum=False):
        """Delete the scans prune_plan() reports as safe. Dry-run by default."""
        plan = self.prune_plan(keep_local=keep_local, keep_root_scans=keep_root_scans)
        plan["applied"] = False
        plan["vacuumed"] = False
        if not apply or not plan["prunable"]:
            return plan

        ids = [item["scan_id"] for item in plan["prunable"]]
        conn = self.storage.get_connection()
        try:
            # Chunked so the statement stays well inside SQLite's variable limit.
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                placeholders = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM files WHERE scan_id IN ({placeholders})", chunk)
                conn.execute(f"DELETE FROM scan_progress WHERE scan_id IN ({placeholders})", chunk)
                conn.execute(f"DELETE FROM scans WHERE id IN ({placeholders})", chunk)
            conn.commit()
            plan["applied"] = True
            if vacuum:
                conn.isolation_level = None
                conn.execute("VACUUM")
                plan["vacuumed"] = True
        finally:
            conn.close()
        self._composite_cache.clear()
        return plan

    def clean_duplicates(self, scan_id=None):
        """Remove duplicate file entries from database.
        
        Args:
            scan_id: If provided, clean duplicates only for this scan. Otherwise, clean all scans.
            
        Returns:
            dict with statistics about cleaned duplicates
        """
        conn = self.storage.get_connection()
        
        # Count duplicates before cleanup
        if scan_id:
            count_query = """
                SELECT COUNT(*) - COUNT(DISTINCT scan_id || '|' || parent_path || '|' || name) as duplicate_count
                FROM files WHERE scan_id = ?
            """
            total_query = "SELECT COUNT(*) FROM files WHERE scan_id = ?"
            params = (scan_id,)
        else:
            count_query = """
                SELECT COUNT(*) - COUNT(DISTINCT scan_id || '|' || parent_path || '|' || name) as duplicate_count
                FROM files
            """
            total_query = "SELECT COUNT(*) FROM files"
            params = ()
        
        duplicate_count_before = conn.execute(count_query, params).fetchone()[0] or 0
        total_before = conn.execute(total_query, params).fetchone()[0]
        
        # Delete duplicates, keeping only the first occurrence (MIN(id))
        if scan_id:
            delete_query = """
                DELETE FROM files 
                WHERE scan_id = ? AND id NOT IN (
                    SELECT MIN(id) 
                    FROM files 
                    WHERE scan_id = ?
                    GROUP BY scan_id, parent_path, name
                )
            """
            params_delete = (scan_id, scan_id)
        else:
            delete_query = """
                DELETE FROM files 
                WHERE id NOT IN (
                    SELECT MIN(id) 
                    FROM files 
                    GROUP BY scan_id, parent_path, name
                )
            """
            params_delete = ()
        
        cursor = conn.execute(delete_query, params_delete)
        deleted_count = cursor.rowcount
        conn.commit()
        
        # Count total files after cleanup
        total_after = conn.execute(total_query, params).fetchone()[0]
        
        return {
            "scan_id": scan_id if scan_id else "all",
            "deleted_duplicates": deleted_count,
            "duplicate_count_before": duplicate_count_before,
            "total_files_before": total_before,
            "total_files_after": total_after,
            "files_removed": total_before - total_after
        }


class YDM_CLI:
    """Command Line Interface for Yandex Disk Monitor."""
    
    def __init__(self):
        #: Set by render() whenever it prints a failure. Command handlers
        #: return True for "I handled this", not for "it went well", so the
        #: exit code is derived here rather than threaded through ~21 call
        #: sites — see tasks/opensource/GAP.md G4a.
        self.failed = False
        self.parser = argparse.ArgumentParser(
            description="YDM - Yandex Disk Monitor & Auditor",
            formatter_class=argparse.RawDescriptionHelpFormatter
        )
        self.parser.add_argument("--db-path", default="monitor.db", help="Path to SQLite database")
        self.parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
        self.parser.add_argument("--config-profile", default="prod", help="Config profile to use (prod/test/custom)")
        self.parser.add_argument("--backend", choices=["api", "rclone"], default="api",
                                  help="Cloud client backend: 'api' (YANDEX_DISK_TOKEN, default) or "
                                       "'rclone' (rclone.conf remote, for environments without the "
                                       "yandex-disk daemon — see tasks/rclone_backend/README.md)")
        
        subparsers = self.parser.add_subparsers(dest="command", help="Available commands")
        
        # init command
        subparsers.add_parser("init", help="Initialize the database")
        
        # scan command
        scan_parser = subparsers.add_parser("scan", help="Scan operations")
        scan_parser.add_argument("target", choices=["meta", "local", "cloud"], help="Scan target")
        scan_parser.add_argument("--path", default=None, help="Specific path to scan (cloud: /папка, local: /path/to/dir)")
        scan_parser.add_argument(
            "--depth", type=int, default=None,
            help="Cloud only: how many levels below --path to walk. 1 lists the "
                 "folder itself and descends no further. Use it to refresh the "
                 "disk root without a full 1.5 TB walk. A depth-limited scan is "
                 "a partial update and can never become the composite base."
        )
        scan_parser.add_argument("--progress", action="store_true", help="Show progress JSON stream")
        scan_parser.add_argument("--resume", action="store_true", help="Resume interrupted cloud scan")
        scan_parser.add_argument("--scan-id", type=int, help="Specific scan ID to resume or add paths to")
        scan_parser.add_argument("--add-to-scan", type=int, help="Add path to existing scan session (instead of creating new)")

        # report command
        report_parser = subparsers.add_parser("report", help="Generate reports")
        report_parser.add_argument("type", choices=[
            "status",
            "diff",
            "scan-info",
            "scan-list",
            "scan-progress",
            "long-paths",
            "analyze-scan",
            "duplicates",
            "clean-duplicates",
            "prune",
            "full-scan-info",
            "full-scan-candidates",
        ], help="Report type")
        report_parser.add_argument("--scan-id", type=int, help="Scan ID for scan-info, scan-progress, long-paths, analyze-scan, duplicates, and clean-duplicates (optional for clean-duplicates)")
        report_parser.add_argument("--cloud-scan-id", type=int, help="Specific cloud scan ID for diff comparison (optional)")
        report_parser.add_argument("--local-scan-id", type=int, help="Specific local scan ID for diff comparison (optional)")
        report_parser.add_argument("--no-composite", action="store_true", help="Disable composite scan mode for diff (use simple last scan comparison)")
        report_parser.add_argument("--limit", type=int, default=20, help="Limit for scan-list report")
        report_parser.add_argument("--limit-chars", type=int, default=240, help="Path length limit for long-paths report")
        report_parser.add_argument("--by-hash", action="store_true", default=True, help="Find duplicates by MD5 hash (default) or --by-name")
        report_parser.add_argument("--by-name", action="store_true", help="Find duplicates by size+name instead of hash")
        report_parser.add_argument("--apply", action="store_true", help="prune: actually delete (dry-run otherwise)")
        report_parser.add_argument("--vacuum", action="store_true", help="prune: VACUUM after deleting, to shrink the file")
        report_parser.add_argument("--keep-local", type=int, default=3, help="prune: how many recent local scans to keep")
        report_parser.add_argument("--keep-root-scans", type=int, default=2, help="prune: how many recent root-covering cloud scans to keep")

    def render(self, data, success=True):
        """Renders output in chosen format, and remembers a failure."""
        if not success:
            self.failed = True
        if self.args.format == "json":
            print(json.dumps({"success": success, "data": data}, ensure_ascii=False, indent=2))
        else:
            if isinstance(data, str):
                print(data)
            else:
                print(json.dumps(data, ensure_ascii=False, indent=2))

    def run(self):
        self.args = self.parser.parse_args()
        self.args.config = load_config(self.args.config_profile)
        
        # Для report команд - всегда используем disk БД, для scan - tmpfs
        use_temp = self.args.command == "scan"
        storage = StorageManager(self.args.db_path, use_temp_storage=use_temp, config=self.args.config)
        
        # Инициализировать tmpfs БД если она используется и еще не создана
        if storage.temp_mode and not os.path.exists(storage.db_path):
            storage.init_db()
        
        handled = self.command_handler(storage)
        if not handled:
            # No handler claimed the command — argparse usually catches this
            # first, so reaching here means the help text is the answer, and
            # that is not a success.
            self.parser.print_help()
        return handled

    def command_handler(self, storage):
        if self.args.command == "init":
            success, message = storage.init_db()
            self.render(message, success=success)
            return True
        
        if self.args.command == "scan":
            # Only meta/cloud targets talk to a cloud client; local scan
            # never needed one and shouldn't require credentials for either backend.
            client = None
            if self.args.target in ("meta", "cloud"):
                if self.args.backend == "rclone":
                    if shutil.which("rclone") is None:
                        self.render("Error: rclone not found in PATH (required for --backend rclone)", success=False)
                        return True
                    remote = self.args.config.get("rclone_remote", "yandex")
                    client = RcloneClient(remote=remote)
                else:
                    token = load_token_from_env()
                    if not token:
                        self.render("Error: YANDEX_DISK_TOKEN not found in .env", success=False)
                        return True
                    client = YandexClient(token)

            # Recover any crashed scans before starting
            crashed = storage.recover_crashed_scans()
            if crashed:
                print(f"Warning: Found {len(crashed)} crashed scan(s) from previous runs:", file=sys.stderr)
                for scan in crashed:
                    print(f"  - Scan {scan['id']} ({scan['type']}) at {scan['timestamp']}", file=sys.stderr)
                print(f"They have been marked as 'crashed'. Use 'report scan-info --scan-id <ID>' to check.", file=sys.stderr)

            scan_success = False
            
            if self.args.target == "meta":
                start_time = time.time()
                scan_id = storage.start_scan("meta")
                try:
                    info = client.get_disk_info()
                    storage.save_disk_info(scan_id, info)
                    duration = time.time() - start_time
                    storage.finish_scan(scan_id, 'success', duration)
                    # Force checkpoint to ensure scan status is saved to disk
                    storage.checkpoint_to_disk(force=True)

                    self.render({
                        "scan_id": scan_id,
                        "total_space": info['total_space'],
                        "used_space": info['used_space'],
                        "trash_size": info['trash_size'],
                        "duration": f"{duration:.2f}s"
                    })
                    scan_success = True
                except Exception as e:
                    storage.finish_scan(scan_id, 'failed', time.time() - start_time)
                    self.render(f"Scan failed: {str(e)}", success=False)
                finally:
                    if scan_success:
                        storage.finalize()
                return True

            if self.args.target == "local":
                start_time = time.time()
                local_path = self.args.path or self.args.config.get("local_root")
                if not local_path:
                    self.render(
                        "No local path. Pass --path /your/mirror, or set "
                        "\"local_root\" in ydm_config.json for the active profile.",
                        success=False,
                    )
                    return True
                scan_id = storage.start_scan("local", scan_root=local_path)
                try:
                    if not os.path.exists(local_path):
                        raise Exception(f"Path not found: {local_path}")
                        
                    scanner = LocalScanner(local_path)
                    files_count = scanner.scan(scan_id, storage)

                    duration = time.time() - start_time
                    storage.finish_scan(scan_id, 'success', duration)
                    # Force checkpoint to ensure scan status is saved to disk
                    storage.checkpoint_to_disk(force=True)

                    self.render({
                        "scan_id": scan_id,
                        "files_scanned": files_count,
                        "path": local_path,
                        "duration": f"{duration:.2f}s"
                    })
                    scan_success = True
                except KeyboardInterrupt:
                    print("\nInterrupted by user. Saving to disk...", file=sys.stderr)
                    storage.checkpoint_to_disk(force=True)
                    storage.finish_scan(scan_id, 'interrupted', time.time() - start_time)
                    storage.cleanup_temp_db()
                    self.render({
                        "scan_id": scan_id,
                        "status": "interrupted",
                        "message": "Scan interrupted by user. Data saved to disk.",
                        "duration": f"{(time.time() - start_time):.2f}s"
                    }, success=False)
                    return True
                except Exception as e:
                    storage.finish_scan(scan_id, 'failed', time.time() - start_time)
                    storage.cleanup_temp_db()
                    self.render(f"Local scan failed: {str(e)}", success=False)
                finally:
                    if scan_success:
                        storage.finalize()
                return True

            if self.args.target == "cloud":
                # Проверка логики для добавления папки в существующий скан
                if self.args.add_to_scan:
                    if not self.args.path:
                        self.render("Error: --path required when using --add-to-scan", success=False)
                        return True
                    
                    scan_id = self.args.add_to_scan
                    print(f"Adding paths to scan {scan_id}...", file=sys.stderr)
                    
                    try:
                        # Добавляем папку в очередь существующего скана
                        paths = [self.args.path]
                        storage.add_folders_to_scan(scan_id, paths)
                        
                        remaining = storage.get_pending_folders_count(scan_id)
                        self.render({
                            "scan_id": scan_id,
                            "action": "added_path",
                            "path": self.args.path,
                            "remaining_folders": remaining
                        })
                    except Exception as e:
                        self.render(f"Failed to add path: {str(e)}", success=False)
                    return True

                # Level 1: Lock file to prevent concurrent scans
                LOCK_FILE = '/tmp/ydm_cloud_scan.lock'

                if os.path.exists(LOCK_FILE):
                    try:
                        with open(LOCK_FILE, 'r') as f:
                            lock_pid = int(f.read().strip())

                        # Check if process is still running
                        if os.path.exists(f'/proc/{lock_pid}'):
                            self.render({
                                "error": "concurrent_scan",
                                "message": f"Another scan is already running (PID: {lock_pid}). Wait for it to complete or kill it first.",
                                "lock_pid": lock_pid
                            }, success=False)
                            return True
                        else:
                            # Stale lock - process is dead
                            print(f"Removing stale lock file (PID {lock_pid} not running)", file=sys.stderr)
                            os.remove(LOCK_FILE)
                    except (ValueError, IOError) as e:
                        # Corrupted lock file - remove it
                        print(f"Removing corrupted lock file: {e}", file=sys.stderr)
                        try:
                            os.remove(LOCK_FILE)
                        except:
                            pass

                # Create lock with our PID
                try:
                    with open(LOCK_FILE, 'w') as f:
                        f.write(str(os.getpid()))
                    print(f"Lock acquired (PID: {os.getpid()})", file=sys.stderr)
                except Exception as e:
                    self.render(f"Failed to create lock file: {e}", success=False)
                    return True

                # Очистить старую tmpfs БД перед сканированием (всегда свежая)
                if storage.temp_mode:
                    storage.reset_temp_db()

                # Обычное сканирование (новое или resume)
                start_time = time.time()
                global _storage_for_signal, _scan_id_for_signal, _start_time_for_signal, _terminate_requested
                _terminate_requested = False
                
                # Determine scan_id: resume existing or create new
                if self.args.resume:
                    if self.args.scan_id:
                        scan_id = self.args.scan_id
                        print(f"Resuming scan {scan_id}...", file=sys.stderr)
                    else:
                        # Get last cloud scan from disk DB
                        if os.path.exists(storage.final_db_path):
                            disk_conn = sqlite3.connect(storage.final_db_path)
                            last_scan = disk_conn.execute(
                                "SELECT id FROM scans WHERE scan_type='cloud' ORDER BY id DESC LIMIT 1"
                            ).fetchone()
                            disk_conn.close()
                            if not last_scan:
                                self.render("No previous cloud scans found", success=False)
                                return True
                            scan_id = last_scan[0]
                        else:
                            self.render("No previous database found", success=False)
                            return True
                        print(f"Resuming last cloud scan {scan_id}...", file=sys.stderr)
                    
                    # Восстановить данные сеанса из disk DB в tmpfs
                    storage.restore_from_disk(scan_id)
                else:
                    scan_depth = getattr(self.args, "depth", None)
                    scan_id = storage.start_scan(
                        "cloud",
                        scan_root=self.args.path or "/",
                        scan_depth=scan_depth,
                    )
                    print(f"Starting new cloud scan {scan_id}...", file=sys.stderr)
                    if scan_depth is not None:
                        print(
                            f"Depth-limited to {scan_depth} level(s) below "
                            f"{self.args.path or '/'}; this scan can never become "
                            "the composite base.",
                            file=sys.stderr,
                        )
                    
                    # Check for data integrity issues with last scan
                    if os.path.exists(storage.final_db_path):
                        try:
                            disk_conn = sqlite3.connect(storage.final_db_path)
                            last_scan = disk_conn.execute(
                                "SELECT id, status FROM scans WHERE scan_type='cloud' AND id != ? ORDER BY id DESC LIMIT 1",
                                (scan_id,)
                            ).fetchone()
                            if last_scan:
                                last_id, last_status = last_scan
                                last_files = disk_conn.execute(
                                    "SELECT COUNT(*) FROM files WHERE scan_id = ?",
                                    (last_id,)
                                ).fetchone()[0]
                                
                                # Warn about issues
                                if last_status in ('success', 'started') and last_files == 0:
                                    print(f"⚠️  Warning: Previous scan {last_id} ({last_status}) has no files - possible data loss", file=sys.stderr)
                                elif last_status == 'crashed':
                                    print(f"⚠️  Warning: Previous scan {last_id} crashed. Use --resume --scan-id {last_id} to recover.", file=sys.stderr)
                            disk_conn.close()
                        except Exception as e:
                            pass  # Silent fail for integrity check
                
                # Если указана папка и это новый скан - начинаем с этой папки
                if self.args.path and not self.args.resume:
                    storage.update_folder_status(scan_id, self.args.path, "pending")
                    print(f"Will scan from path: {self.args.path}", file=sys.stderr)
                
                # Обновить глобальные ссылки для сигналов
                _storage_for_signal = storage
                _scan_id_for_signal = scan_id
                _start_time_for_signal = start_time
                
                try:
                    scanner = CloudScanner(client, report_progress=self.args.progress, config=self.args.config)
                    start_path = self.args.path if (self.args.path and not self.args.resume) else None
                    files_count = scanner.scan(
                        scan_id, storage,
                        resume=self.args.resume,
                        start_path=start_path,
                        max_depth=getattr(self.args, "depth", None),
                    )
                    
                    duration = time.time() - start_time
                    storage.finish_scan(scan_id, 'success', duration)
                    # Force checkpoint to ensure scan status is saved to disk
                    storage.checkpoint_to_disk(force=True)
                    
                    stats = storage.get_scan_stats(scan_id)
                    self.render({
                        "scan_id": scan_id,
                        "files_scanned": files_count,
                        "type": "cloud",
                        "duration": f"{duration:.2f}s",
                        "progress": stats
                    })
                    scan_success = True
                except TerminationRequested:
                    storage.finish_scan(scan_id, 'interrupted', time.time() - start_time)
                    storage.cleanup_temp_db()
                    self.render({
                        "scan_id": scan_id,
                        "status": "interrupted",
                        "message": "Scan interrupted by signal. Data saved to disk. Use --resume --scan-id {} to continue.".format(scan_id),
                        "duration": f"{(time.time() - start_time):.2f}s"
                    }, success=False)
                    return True
                except KeyboardInterrupt:
                    # Перехватить Ctrl+C и сохранить на диск
                    print("\nInterrupted by user. Saving to disk...", file=sys.stderr)
                    storage.checkpoint_to_disk(force=True)
                    # Обновить статус скана как прерванный
                    storage.finish_scan(scan_id, 'interrupted', time.time() - start_time)
                    # Очистить tmpfs БД после сохранения на диск
                    storage.cleanup_temp_db()
                    self.render({
                        "scan_id": scan_id,
                        "status": "interrupted",
                        "message": "Scan interrupted by user. Data saved to disk. Use --resume --scan-id {} to continue.".format(scan_id),
                        "duration": f"{(time.time() - start_time):.2f}s"
                    }, success=False)
                    return True
                except Exception as e:
                    storage.finish_scan(scan_id, 'failed', time.time() - start_time)
                    # Очистить tmpfs БД при ошибке
                    storage.cleanup_temp_db()
                    self.render(f"Cloud scan failed: {str(e)}", success=False)
                finally:
                    if scan_success:
                        storage.finalize()

                    # Level 1: Release lock file
                    if os.path.exists(LOCK_FILE):
                        try:
                            os.remove(LOCK_FILE)
                            print(f"Lock released", file=sys.stderr)
                        except Exception as e:
                            print(f"Warning: Failed to remove lock file: {e}", file=sys.stderr)
                return True

        if self.args.command == "report":
            analyzer = Analyzer(storage)
            if self.args.type == "status":
                self.render(analyzer.get_status())
                return True
            if self.args.type == "diff":
                result = analyzer.get_diff(
                    cloud_scan_id=self.args.cloud_scan_id,
                    local_scan_id=self.args.local_scan_id,
                    use_composite=not self.args.no_composite
                )
                self.render(result)
                return True
            if self.args.type == "full-scan-info":
                # Show current heuristic full scan info
                info = analyzer.find_last_full_scan()
                self.render(info)
                return True
            if self.args.type == "full-scan-candidates":
                # Show recent cloud scans in freshness window with heuristic metrics
                candidates = analyzer.get_full_scan_candidates()
                self.render(candidates)
                return True
            if self.args.type == "scan-info":
                if not self.args.scan_id:
                    self.render("Error: --scan-id required for scan-info", success=False)
                    return True
                info = analyzer.get_scan_info(self.args.scan_id)
                if not info:
                    self.render(f"Scan {self.args.scan_id} not found", success=False)
                    return True
                self.render(info)
                return True
            if self.args.type == "scan-list":
                scans = analyzer.get_scans_list(self.args.limit)
                self.render({
                    "total": len(scans),
                    "scans": scans
                })
                return True
            if self.args.type == "scan-progress":
                if not self.args.scan_id:
                    self.render("Error: --scan-id required for scan-progress", success=False)
                    return True
                progress = analyzer.get_scan_progress(self.args.scan_id)
                self.render(progress)
                return True
            if self.args.type == "long-paths":
                if not self.args.scan_id:
                    self.render("Error: --scan-id required for long-paths", success=False)
                    return True
                result = analyzer.get_long_paths(self.args.scan_id, self.args.limit_chars)
                self.render(result)
                return True
            if self.args.type == "analyze-scan":
                if not self.args.scan_id:
                    self.render("Error: --scan-id required for analyze-scan", success=False)
                    return True
                result = analyzer.analyze_scan(self.args.scan_id)
                self.render(result)
                return True
            if self.args.type == "duplicates":
                if not self.args.scan_id:
                    self.render("Error: --scan-id required for duplicates", success=False)
                    return True
                by_hash = not self.args.by_name  # If --by-name not specified, use hash (default)
                result = analyzer.get_duplicates(self.args.scan_id, by_hash=by_hash)
                self.render(result)
                return True
            if self.args.type == "clean-duplicates":
                result = analyzer.clean_duplicates(self.args.scan_id)
                self.render(result)
                return True
            if self.args.type == "prune":
                result = analyzer.prune(
                    keep_local=self.args.keep_local,
                    keep_root_scans=self.args.keep_root_scans,
                    apply=self.args.apply,
                    vacuum=self.args.vacuum,
                )
                if self.args.format == "json":
                    self.render(result)
                else:
                    self.render(format_prune_plan(result))
                return True

        return False

if __name__ == "__main__":
    cli = YDM_CLI()
    handled = cli.run()
    # Until 2026-08-24 this line was `cli.run()` with the result discarded, so
    # every command exited 0 — including "token not found" and "scan failed".
    # `ydm.py … && next-step` ran next-step regardless, and callers that
    # already checked the return code (tools/sync_backends.py) could never see
    # a failure.
    sys.exit(0 if handled and not cli.failed else 1)
