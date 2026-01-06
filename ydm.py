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
    "checkpoint_time_sec": 300
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
            
            # Copy scans (if not exists)
            temp_conn.execute("""
                INSERT OR IGNORE INTO disk.scans 
                SELECT * FROM main.scans
            """)
            
            # Copy disk_info (update existing)
            temp_conn.execute("""
                INSERT OR REPLACE INTO disk.disk_info 
                SELECT * FROM main.disk_info
            """)
            
            # Copy files (without id - let target DB auto-increment)
            temp_conn.execute("""
                INSERT INTO disk.files 
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


class YandexClient:
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
        
        if batch:
            storage.save_files_batch(batch)

        # Mark scan as completed
        storage.update_folder_status(scan_id, current_path, 'completed')
        
        return files_count

class Analyzer:
    """Analyzes data from DB."""
    def __init__(self, storage):
        self.storage = storage

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

    def get_diff(self):
        """Compares last successful cloud scan vs last successful local scan."""
        # Читаем исключения из конфига
        exclude_dirs = set()
        config_path = os.path.expanduser("~/.config/yandex-disk/config.cfg")
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    for line in f:
                        if line.startswith("exclude-dirs="):
                            dirs = line.split("=", 1)[1].strip()
                            exclude_dirs = set(d.strip() for d in dirs.split(","))
            except: pass

        conn = self.storage.get_connection()
        
        # Находим ID последних успешных (или started для cloud, если хотим тестить недокачанные) сканов
        # Для чистоты берем последний cloud scan независимо от статуса
        last_cloud = conn.execute("SELECT id FROM scans WHERE scan_type='cloud' ORDER BY id DESC LIMIT 1").fetchone()
        last_local = conn.execute("SELECT id FROM scans WHERE scan_type='local' AND status='success' ORDER BY id DESC LIMIT 1").fetchone()
        
        if not last_cloud or not last_local:
            return {"error": "Need both cloud and local scans to compare"}
            
        cloud_id = last_cloud[0]
        local_id = last_local[0]
        
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
        report_parser.add_argument("type", choices=["status", "diff", "scan-info", "scan-list", "scan-progress", 
                                                     "long-paths", "analyze-scan", "duplicates"], 
                                   help="Report type")
        report_parser.add_argument("--scan-id", type=int, help="Scan ID for scan-info, scan-progress, long-paths, analyze-scan, and duplicates")
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
            token = load_token_from_env()
            if not token:
                self.render("Error: YANDEX_DISK_TOKEN not found in .env", success=False)
                return True
            
            # Recover any crashed scans before starting
            crashed = storage.recover_crashed_scans()
            if crashed:
                print(f"Warning: Found {len(crashed)} crashed scan(s) from previous runs:", file=sys.stderr)
                for scan in crashed:
                    print(f"  - Scan {scan['id']} ({scan['type']}) at {scan['timestamp']}", file=sys.stderr)
                print(f"They have been marked as 'crashed'. Use 'report scan-info --scan-id <ID>' to check.", file=sys.stderr)
            
            client = YandexClient(token)
            scan_success = False
            
            if self.args.target == "meta":
                start_time = time.time()
                scan_id = storage.start_scan("meta")
                try:
                    info = client.get_disk_info()
                    storage.save_disk_info(scan_id, info)
                    duration = time.time() - start_time
                    storage.finish_scan(scan_id, 'success', duration)
                    
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
                local_path = self.args.path if self.args.path else "/data/ya_disk"
                try:
                    if not os.path.exists(local_path):
                        raise Exception(f"Path not found: {local_path}")
                        
                    scanner = LocalScanner(local_path)
                    files_count = scanner.scan(scan_id, storage)
                    
                    duration = time.time() - start_time
                    storage.finish_scan(scan_id, 'success', duration)
                    
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
                self.render(analyzer.get_diff())
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

        return False

if __name__ == "__main__":
    cli = YDM_CLI()
    cli.run()
