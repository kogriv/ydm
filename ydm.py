#!/usr/bin/env python3
import sqlite3
import argparse
import os
import sys
import json
from datetime import datetime
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
            pass

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
    # Default local mirror path used by `scan local` when --path is omitted
    "local_root": "/data/ya_disk",
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

    def get_connection(self):
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
                    duration REAL
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

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def init_db(self):
        """Creates the database schema if it doesn't exist."""
        schema = [
            """
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                scan_type TEXT NOT NULL,
                status TEXT NOT NULL,
                duration REAL
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
            with self.get_connection() as conn:
                cursor = conn.cursor()
                for statement in schema:
                    cursor.execute(statement)
                conn.commit()
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

    def start_scan(self, scan_type):
        """Creates a new scan record with 'started' status."""
        # Получить правильный scan_id из финальной БД
        next_scan_id = self._get_next_scan_id()
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Если tmpfs режим - используем явный scan_id
        if self.temp_mode:
            cursor.execute(
                "INSERT INTO scans (id, scan_type, status, duration) VALUES (?, ?, ?, ?)",
                (next_scan_id, scan_type, 'started', 0)
            )
            scan_id = next_scan_id
            
            # IMPORTANT: Also write to disk DB immediately so scan is tracked even if interrupted before checkpoint
            try:
                if not os.path.exists(self.final_db_path):
                    self._init_final_db()
                disk_conn = sqlite3.connect(self.final_db_path)
                disk_conn.execute(
                    "INSERT OR IGNORE INTO scans (id, scan_type, status, duration) VALUES (?, ?, ?, ?)",
                    (next_scan_id, scan_type, 'started', 0)
                )
                disk_conn.commit()
                disk_conn.close()
            except Exception as e:
                print(f"Warning: Failed to record scan start in disk DB: {e}", file=sys.stderr)
        else:
            cursor.execute(
                "INSERT INTO scans (scan_type, status, duration) VALUES (?, ?, ?)",
                (scan_type, 'started', 0)
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

    def scan(self, scan_id, storage, resume=False, start_path=None):
        """Walks through cloud recursively with resumable support."""
        batch_size = self.config.get("cloud_batch_size", DEFAULT_CONFIG["cloud_batch_size"])
        batch = []
        files_count = 0
        total_processed = 0
        
        visited = set()

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
            root = start_path if start_path else "/"
            queue = [root]
            storage.update_folder_status(scan_id, root, "pending")

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

    def scan_covers_root(self, scan_id):
        """True if the scan enumerated the disk root, i.e. it is a full scan.

        A scan started with `--path /Books` records nothing at the root, so it
        describes one subtree, not the disk. Such a scan may serve as a partial
        update on top of a base, never as the base itself: everything outside
        its subtree would silently vanish from the composite snapshot.
        """
        conn = self.storage.get_connection()
        try:
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
        
        # Find all cloud scans after base scan that are NOT full scans
        partial_scans = conn.execute("""
            SELECT s.id, s.timestamp, s.status
            FROM scans s
            WHERE s.scan_type = 'cloud'
            AND s.id > ?
            AND s.id NOT IN (
                SELECT DISTINCT sp.scan_id 
                FROM scan_progress sp 
                WHERE sp.path = '/'
            )
            ORDER BY s.id ASC
        """, (base_scan_id,)).fetchall()
        
        result = []
        for scan_id, timestamp, status in partial_scans:
            # Get root path for this partial scan
            # First try from scan_progress
            root_paths = conn.execute(
                """SELECT path FROM scan_progress 
                WHERE scan_id = ? 
                ORDER BY last_checked ASC 
                LIMIT 1""",
                (scan_id,)
            ).fetchone()
            
            root_path = root_paths[0] if root_paths else None
            
            # If not found in scan_progress, try to determine from files
            if not root_path:
                file_paths = conn.execute(
                    """SELECT DISTINCT parent_path 
                    FROM files 
                    WHERE scan_id = ? 
                    ORDER BY parent_path 
                    LIMIT 1""",
                    (scan_id,)
                ).fetchone()
                
                if file_paths:
                    # Use the shortest parent_path as root (usually the top-level folder)
                    root_path = file_paths[0]
                    # If it's not empty, ensure it starts with /
                    if root_path and not root_path.startswith('/'):
                        root_path = '/' + root_path
            
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
            # Fallback: try to use the last cloud scan as base (old behavior)
            conn = self.storage.get_connection()
            last_cloud = conn.execute(
                "SELECT id, timestamp, status FROM scans WHERE scan_type = 'cloud' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not last_cloud:
                # No cloud scans at all
                return {"error": base_scan.get("error", "No full scan found") if isinstance(base_scan, dict) else "No cloud scan found"}

            # Use last cloud scan as base without heuristics
            base_scan = {
                "id": last_cloud[0],
                "timestamp": last_cloud[1],
                "status": last_cloud[2],
                "files_count": conn.execute(
                    "SELECT COUNT(*) FROM files WHERE scan_id = ? AND type = 'file'",
                    (last_cloud[0],),
                ).fetchone()[0],
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

                if not root_path:
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
    
    def clear_composite_cache(self):
        """Clears the composite scan cache."""
        self._composite_cache.clear()

    def _compare_composite_scan(self, composite, local_id, exclude_dirs):
        """
        Compares composite cloud scan (base + partial updates) with local scan.
        Optimized using temporary tables for better performance.
        
        Args:
            composite: Result from build_composite_scan()
            local_id: Local scan ID
            exclude_dirs: Set of excluded directory names
            
        Returns:
            dict: Comparison results
        """
        conn = self.storage.get_connection()
        base_scan_id = composite["base_scan_id"]
        folder_updates = composite["folder_updates"]
        
        if not folder_updates:
            # No partial scans, use simple comparison with base scan
            return self._compare_simple_scan(base_scan_id, local_id, exclude_dirs)
        
        # Optimize using temporary table for composite cloud files
        # This avoids multiple queries and improves performance
        
        try:
            # Create temporary table for composite cloud files
            conn.execute("""
                CREATE TEMP TABLE IF NOT EXISTS composite_cloud_files (
                    parent_path TEXT,
                    name TEXT,
                    size INTEGER,
                    PRIMARY KEY (parent_path, name)
                )
            """)
            conn.execute("DELETE FROM composite_cloud_files")
            
            updated_folders_list = list(folder_updates.keys())
            updated_scan_ids = list(set(folder_updates.values()))
            
            if updated_folders_list:
                # Insert files from base scan (excluding updated folders)
                if len(updated_folders_list) == 1:
                    base_insert = """
                        INSERT INTO composite_cloud_files (parent_path, name, size)
                        SELECT parent_path, name, size
                        FROM files
                        WHERE scan_id = ? AND parent_path != ? AND type = 'file'
                    """
                    conn.execute(base_insert, (base_scan_id, updated_folders_list[0]))
                else:
                    placeholders = ','.join(['?' for _ in updated_folders_list])
                    base_insert = f"""
                        INSERT INTO composite_cloud_files (parent_path, name, size)
                        SELECT parent_path, name, size
                        FROM files
                        WHERE scan_id = ? AND parent_path NOT IN ({placeholders}) AND type = 'file'
                    """
                    params = [base_scan_id] + updated_folders_list
                    conn.execute(base_insert, tuple(params))
                
                # Insert files from updated scans
                if len(updated_scan_ids) == 1 and len(updated_folders_list) == 1:
                    updated_insert = """
                        INSERT OR REPLACE INTO composite_cloud_files (parent_path, name, size)
                        SELECT parent_path, name, size
                        FROM files
                        WHERE scan_id = ? AND parent_path = ? AND type = 'file'
                    """
                    conn.execute(updated_insert, (updated_scan_ids[0], updated_folders_list[0]))
                else:
                    scan_placeholders = ','.join(['?' for _ in updated_scan_ids])
                    folder_placeholders = ','.join(['?' for _ in updated_folders_list])
                    updated_insert = f"""
                        INSERT OR REPLACE INTO composite_cloud_files (parent_path, name, size)
                        SELECT parent_path, name, size
                        FROM files
                        WHERE scan_id IN ({scan_placeholders}) 
                        AND parent_path IN ({folder_placeholders})
                        AND type = 'file'
                    """
                    params = updated_scan_ids + updated_folders_list
                    conn.execute(updated_insert, tuple(params))
            else:
                # No updated folders, just use base scan
                conn.execute("""
                    INSERT INTO composite_cloud_files (parent_path, name, size)
                    SELECT parent_path, name, size
                    FROM files
                    WHERE scan_id = ? AND type = 'file'
                """, (base_scan_id,))
            
            conn.commit()
            
            # Query missing local files using temporary table (much faster)
            missing_local = conn.execute("""
                SELECT c.parent_path, c.name, c.size
                FROM composite_cloud_files c
                WHERE NOT EXISTS (
                    SELECT 1 FROM files l
                    WHERE l.scan_id = ? 
                    AND l.parent_path = c.parent_path 
                    AND l.name = c.name
                )
            """, (local_id,)).fetchall()
            
        finally:
            # Clean up temporary table
            conn.execute("DROP TABLE IF EXISTS composite_cloud_files")
            conn.commit()
        
        # Filter excluded directories
        filtered_missing_local = []
        for row in missing_local:
            parent, name, size = row
            full_path = f"{parent}/{name}".strip("/")
            root_folder = full_path.split("/")[0] if full_path else ""
            
            if root_folder and root_folder not in exclude_dirs:
                filtered_missing_local.append(row)
        
        # Query for missing cloud files (optimized)
        # Use temporary table again for better performance
        try:
            conn.execute("""
                CREATE TEMP TABLE IF NOT EXISTS composite_cloud_files_check (
                    parent_path TEXT,
                    name TEXT,
                    PRIMARY KEY (parent_path, name)
                )
            """)
            conn.execute("DELETE FROM composite_cloud_files_check")
            
            # Rebuild composite cloud files table for checking
            if updated_folders_list:
                if len(updated_folders_list) == 1:
                    conn.execute("""
                        INSERT INTO composite_cloud_files_check (parent_path, name)
                        SELECT parent_path, name
                        FROM files
                        WHERE scan_id = ? AND parent_path != ? AND type = 'file'
                    """, (base_scan_id, updated_folders_list[0]))
                else:
                    placeholders = ','.join(['?' for _ in updated_folders_list])
                    conn.execute(f"""
                        INSERT INTO composite_cloud_files_check (parent_path, name)
                        SELECT parent_path, name
                        FROM files
                        WHERE scan_id = ? AND parent_path NOT IN ({placeholders}) AND type = 'file'
                    """, tuple([base_scan_id] + updated_folders_list))
                
                if len(updated_scan_ids) == 1 and len(updated_folders_list) == 1:
                    conn.execute("""
                        INSERT OR REPLACE INTO composite_cloud_files_check (parent_path, name)
                        SELECT parent_path, name
                        FROM files
                        WHERE scan_id = ? AND parent_path = ? AND type = 'file'
                    """, (updated_scan_ids[0], updated_folders_list[0]))
                else:
                    scan_placeholders = ','.join(['?' for _ in updated_scan_ids])
                    folder_placeholders = ','.join(['?' for _ in updated_folders_list])
                    conn.execute(f"""
                        INSERT OR REPLACE INTO composite_cloud_files_check (parent_path, name)
                        SELECT parent_path, name
                        FROM files
                        WHERE scan_id IN ({scan_placeholders}) 
                        AND parent_path IN ({folder_placeholders})
                        AND type = 'file'
                    """, tuple(updated_scan_ids + updated_folders_list))
            else:
                conn.execute("""
                    INSERT INTO composite_cloud_files_check (parent_path, name)
                    SELECT parent_path, name
                    FROM files
                    WHERE scan_id = ? AND type = 'file'
                """, (base_scan_id,))
            
            conn.commit()
            
            # Query missing cloud files using temporary table
            missing_cloud = conn.execute("""
                SELECT l.parent_path, l.name, l.size
                FROM files l
                WHERE l.scan_id = ? AND l.type = 'file'
                AND NOT EXISTS (
                    SELECT 1 FROM composite_cloud_files_check c
                    WHERE c.parent_path = l.parent_path 
                    AND c.name = l.name
                )
            """, (local_id,)).fetchall()
            
        finally:
            conn.execute("DROP TABLE IF EXISTS composite_cloud_files_check")
            conn.commit()
        
        # Filter .sync files
        filtered_missing_cloud = [
            row for row in missing_cloud 
            if not row[1].startswith(".sync") and "/.sync" not in row[0]
        ]
        
        return {
            "compare_scans": {
                "cloud": f"composite(base={base_scan_id}, partials={len(folder_updates)})",
                "local": local_id
            },
            "missing_local_count": len(filtered_missing_local),
            "missing_cloud_count": len(filtered_missing_cloud),
            "missing_local_sample": [f"{r[0]}/{r[1]}" for r in filtered_missing_local[:20]],
            "missing_cloud_sample": [f"{r[0]}/{r[1]}" for r in filtered_missing_cloud[:20]],
            "composite_info": {
                "base_scan_id": base_scan_id,
                "updated_folders_count": len(folder_updates)
            }
        }

    def _compare_simple_scan(self, cloud_id, local_id, exclude_dirs):
        """
        Simple comparison between two scans (non-composite).
        
        Args:
            cloud_id: Cloud scan ID
            local_id: Local scan ID
            exclude_dirs: Set of excluded directory names
            
        Returns:
            dict: Comparison results
        """
        conn = self.storage.get_connection()
        
        # 1. Missing Local
        missing_local = conn.execute("""
            SELECT c.parent_path, c.name, c.size 
            FROM files c 
            WHERE c.scan_id = ? 
            AND c.type = 'file'
            AND NOT EXISTS (
                SELECT 1 FROM files l 
                WHERE l.scan_id = ? 
                AND l.parent_path = c.parent_path 
                AND l.name = c.name
            )
        """, (cloud_id, local_id)).fetchall()
        
        # Фильтрация исключенных папок
        filtered_missing_local = []
        for row in missing_local:
            parent, name, size = row
            full_path = f"{parent}/{name}".strip("/")
            root_folder = full_path.split("/")[0] if full_path else ""
            
            if root_folder and root_folder not in exclude_dirs:
                filtered_missing_local.append(row)
        
        # 2. Missing Cloud
        missing_cloud = conn.execute("""
            SELECT l.parent_path, l.name, l.size 
            FROM files l 
            WHERE l.scan_id = ? 
            AND l.type = 'file'
            AND NOT EXISTS (
                SELECT 1 FROM files c 
                WHERE c.scan_id = ? 
                AND c.parent_path = l.parent_path 
                AND c.name = l.name
            )
        """, (local_id, cloud_id)).fetchall()

        # Фильтруем системные файлы (.sync) из missing_cloud
        filtered_missing_cloud = [
            row for row in missing_cloud 
            if not row[1].startswith(".sync") and "/.sync" not in row[0]
        ]

        return {
            "compare_scans": {"cloud": cloud_id, "local": local_id},
            "missing_local_count": len(filtered_missing_local),
            "missing_cloud_count": len(filtered_missing_cloud),
            "missing_local_sample": [f"{r[0]}/{r[1]}" for r in filtered_missing_local[:20]],
            "missing_cloud_sample": [f"{r[0]}/{r[1]}" for r in filtered_missing_cloud[:20]]
        }

    def get_diff(self, cloud_scan_id=None, local_scan_id=None, use_composite=True):
        """
        Compares cloud scan vs local scan with optional composite scan support.
        
        Args:
            cloud_scan_id: Specific cloud scan ID to use (optional, uses last if not specified)
            local_scan_id: Specific local scan ID to use (optional, uses last successful if not specified)
            use_composite: If True and cloud_scan_id not specified, build composite scan from base + partials
        
        Returns:
            dict: Comparison results with missing files counts and samples
        """
        # Читаем исключения из конфига
        exclude_dirs = set()
        config_path = os.path.expanduser(DEFAULT_CONFIG["exclude_config"])
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    for line in f:
                        if line.startswith("exclude-dirs="):
                            dirs = line.split("=", 1)[1].strip()
                            exclude_dirs = set(d.strip() for d in dirs.split(","))
            except: pass

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
        
        # 1. Missing Local
        missing_local = conn.execute(f"""
            SELECT c.parent_path, c.name, c.size 
            FROM files c 
            WHERE c.scan_id = ? 
            AND NOT EXISTS (
                SELECT 1 FROM files l 
                WHERE l.scan_id = ? 
                AND l.parent_path = c.parent_path 
                AND l.name = c.name
            )
        """, (cloud_id, local_id)).fetchall()
        
        # Фильтрация исключенных папок
        # Если файл лежит в папке, которая (или родитель которой) есть в exclude_dirs
        filtered_missing_local = []
        for row in missing_local:
            parent, name, size = row
            # Проверяем, начинается ли путь с исключенной папки
            # Путь в базе: "/Folder/Sub" или "" (корень) + "Folder"
            
            # Строим полный путь для проверки
            full_path = f"{parent}/{name}".strip("/")
            root_folder = full_path.split("/")[0]
            
            if root_folder in exclude_dirs:
                continue
            filtered_missing_local.append(row)

        
        # 2. Missing Cloud
        missing_cloud = conn.execute(f"""
            SELECT l.parent_path, l.name, l.size 
            FROM files l 
            WHERE l.scan_id = ? 
            AND NOT EXISTS (
                SELECT 1 FROM files c 
                WHERE c.scan_id = ? 
                AND c.parent_path = l.parent_path 
                AND c.name = l.name
            )
        """, (local_id, cloud_id)).fetchall()

        # Фильтруем системные файлы (.sync) из missing_cloud
        filtered_missing_cloud = [
            row for row in missing_cloud 
            if not row[1].startswith(".sync") and "/.sync" not in row[0]
        ]

        return {
            "compare_scans": {"cloud": cloud_id, "local": local_id},
            "missing_local_count": len(filtered_missing_local),
            "missing_cloud_count": len(filtered_missing_cloud),
            "missing_local_sample": [f"{r[0]}/{r[1]}" for r in filtered_missing_local[:20]],
            "missing_cloud_sample": [f"{r[0]}/{r[1]}" for r in filtered_missing_cloud[:20]]
        }

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

    def render(self, data, success=True):
        """Renders output in chosen format."""
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
        
        if self.command_handler(storage):
            pass
        else:
            self.parser.print_help()

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
                scan_id = storage.start_scan("local")
                local_path = self.args.path if self.args.path else self.args.config.get("local_root", "/data/ya_disk")
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
                    scan_id = storage.start_scan("cloud")
                    print(f"Starting new cloud scan {scan_id}...", file=sys.stderr)
                    
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
                    files_count = scanner.scan(scan_id, storage, resume=self.args.resume, start_path=start_path)
                    
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

        return False

if __name__ == "__main__":
    cli = YDM_CLI()
    cli.run()
