#!/usr/bin/env python3
"""
Backend abstraction for unified sync management.

Provides a single interface for sync operations across two environments:
- DaemonBackend: Ubuntu/desktop with the official yandex-disk daemon
- RcloneBackend: Android/Termux/proot where yandex-disk daemon doesn't work

The policy file (var/sync_policy.json) is the single source of truth for both.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.sync_common import (  # noqa: E402
    rclone_bin,
    CommandResult,
    legacy_filter_path,
    load_sync_filters,
    daemon_running,
    run_command,
    start_daemon,
    stop_daemon,
    stop_start_daemon,
    var_path,
)
from ydm import DEFAULT_CONFIG  # noqa: E402


def _load_sync_policy_functions():
    """Lazy import to avoid circular dependency with tools/sync_policy.py."""
    from tools import sync_policy
    return sync_policy


class BackendError(Exception):
    """Backend-specific error with structured context."""
    pass


class NotSupportedError(BackendError):
    """Operation not supported by the selected backend."""
    pass


class SyncBackend(ABC):
    """Abstract sync backend."""

    #: Stable machine-readable id ("daemon"/"rclone"). Callers that need to
    #: branch on the backend must use this, never the human-readable name().
    kind: str = ""

    @abstractmethod
    def name(self) -> str:
        """Human-readable backend name."""
        raise NotImplementedError

    @abstractmethod
    def apply_policy(self, policy: Dict, *, dry_run: bool = False) -> Dict:
        """Apply policy to the environment."""
        raise NotImplementedError

    @abstractmethod
    def run_sync(self, *, dry_run: bool = False, stream: bool = False) -> Dict:
        """Run a normal sync pass."""
        raise NotImplementedError

    @abstractmethod
    def run_resync(self, *, dry_run: bool = False, stream: bool = False) -> Dict:
        """Run a baseline resync."""
        raise NotImplementedError

    @abstractmethod
    def list_cloud_children(self, parent_path: str) -> List[str]:
        """Return immediate child directory names under parent_path in cloud."""
        raise NotImplementedError

    @abstractmethod
    def run_cloud_scan(self, path: str) -> Dict:
        """Update cloud snapshot for path."""
        raise NotImplementedError

    @abstractmethod
    def status(self) -> Dict:
        """Return backend status."""
        raise NotImplementedError


def _command_payload(results: Dict) -> Dict:
    return {
        key: {"cmd": value.cmd, "returncode": value.returncode,
              "stdout": value.stdout, "stderr": value.stderr}
        for key, value in results.items()
    }


class DaemonBackend(SyncBackend):
    """Backend for the official yandex-disk daemon.

    The daemon only supports bidirectional sync. It uses an exclude-dirs
    blacklist: everything not excluded is synced both ways.
    """

    kind = "daemon"

    def __init__(
        self,
        *,
        config_path: str,
        db_path: str,
        local_root: str,
        policy_path: Optional[str] = None,
    ):
        self.config_path = os.path.expanduser(config_path)
        self.db_path = os.path.expanduser(db_path)
        self.local_root = os.path.expanduser(local_root)
        self.policy_path = policy_path or _default_policy_path()

    def name(self) -> str:
        return "yandex-disk daemon"

    def _read_config_lines(self) -> List[str]:
        if not os.path.exists(self.config_path):
            return []
        with open(self.config_path, "r", encoding="utf-8") as handle:
            return handle.readlines()

    def _parse_exclude_dirs(self, lines: List[str]) -> List[str]:
        for line in lines:
            if line.startswith("exclude-dirs="):
                raw = line.split("=", 1)[1].strip()
                if raw:
                    return [p.strip() for p in raw.split(",") if p.strip()]
                return []
        return []

    def _write_config(self, exclude_dirs: List[str]) -> None:
        """Write exclude-dirs=... to config.cfg, preserving other lines."""
        lines = self._read_config_lines()
        value = ",".join(sorted(set(exclude_dirs)))
        found = False
        new_lines: List[str] = []
        for line in lines:
            if line.startswith("exclude-dirs="):
                new_lines.append(f"exclude-dirs={value}\n")
                found = True
            else:
                new_lines.append(line)
        if not found:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"
            new_lines.append(f"exclude-dirs={value}\n")
        # Backup before write
        if os.path.exists(self.config_path):
            backup_path = f"{self.config_path}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.copyfile(self.config_path, backup_path)
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.writelines(new_lines)

    def _policy_to_exclude_dirs(self, policy: Dict) -> List[str]:
        """All disabled paths become exclude-dirs entries."""
        disabled = []
        for entry, meta in policy.get("paths", {}).items():
            mode = meta.get("mode")
            if mode == "disabled":
                disabled.append(entry)
            elif mode == "download_only":
                raise NotSupportedError(
                    "download_only is not supported by the yandex-disk daemon backend. "
                    "Use --backend rclone, or switch the path to bidirectional/disabled."
                )
        return sorted(set(disabled))

    def _cloud_top_level_dirs(self) -> List[str]:
        """Top-level directory names from the newest cloud snapshot in the DB."""
        if not os.path.exists(self.db_path):
            return []
        import sqlite3

        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return []
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT name
                FROM files
                WHERE type = 'dir' AND parent_path IN ('/', '')
                """
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return [row[0] for row in rows]

    #: The daemon's own index of what it has synced, under ``<root>/.sync``.
    #: A path it remembers but cannot find locally is a path it will delete.
    INDEX_NAMES = ("~state2", "state2")

    def _index_bytes(self) -> Optional[bytes]:
        for name in self.INDEX_NAMES:
            try:
                with open(os.path.join(self.local_root, ".sync", name), "rb") as handle:
                    return handle.read()
            except OSError:
                continue
        return None

    @staticmethod
    def _index_record(rel: str) -> bytes:
        """How the index stores one path: varint byte length, then UTF-8 bytes.

        Not a documented format — read off the file: ``\\x05Books\\x00``,
        ``\\x0aBooks/Deti\\x00``, ``9Books/0_самоконтроль…`` (57 bytes).
        """
        key = rel.encode("utf-8")
        length, prefix = len(key), bytearray()
        while True:
            byte = length & 0x7F
            length >>= 7
            prefix.append(byte | (0x80 if length else 0))
            if not length:
                return bytes(prefix) + key

    def _index_remembers(self, data: bytes, rel: str) -> bool:
        record = self._index_record(rel)
        start = data.find(record)
        while start != -1:
            end = start + len(record)
            # The path must end there: a flag byte follows, not more of a name.
            if end < len(data) and data[end] in (0, 1):
                return True
            start = data.find(record, start + 1)
        return False

    def is_live_config(self) -> bool:
        """Whether this backend's config is the one the running daemon reads.

        `yandex-disk start/stop` take no config argument here, so restarting or
        resetting the index always hits the live daemon. Against any other
        config those steps are a rehearsal and must not run.
        """
        live = os.path.abspath(os.path.expanduser(DEFAULT_CONFIG["exclude_config"]))
        return os.path.abspath(self.config_path) == live

    def reset_index(self) -> str:
        """Move the daemon's index aside; the daemon must be stopped.

        The index (`.sync/~*`) is what lets the daemon tell "deleted here" from
        "never had it". Without it the daemon merges: what is only in the cloud
        is downloaded, what is only local is uploaded, a file that differs gets
        a conflict copy. Nothing is deleted, because nothing is remembered. Logs
        stay where they are; the old index is kept in a dated folder next to
        them, so the step can be undone by moving the files back.
        """
        if daemon_running():
            raise BackendError("Refusing to reset the index of a running yandex-disk daemon")
        sync_dir = os.path.join(self.local_root, ".sync")
        names = sorted(name for name in os.listdir(sync_dir) if name.startswith("~"))
        backup = os.path.join(sync_dir, f"index-before-reset-{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(backup)
        for name in names:
            shutil.move(os.path.join(sync_dir, name), os.path.join(backup, name))
        return backup

    def deletion_risk_paths(
        self, before: List[str], after: List[str], *, index_reset: bool = False,
    ) -> List[str]:
        """Paths whose local copy is missing while the daemon will sync them.

        Restarting the daemon in that state makes it read the absence as a user
        deletion and propagate it to the cloud — this is exactly what moved
        /Books (113 GB) to the trash on 2026-08-14.

        Two ways to get there:

        - a top-level cloud folder that was never excluded has no local copy;
        - a path leaves the exclude list, has no local copy, and the daemon's
          index still remembers it. Leaving the list is safe only for a path the
          daemon never tracked: then it downloads. /Books left the list on
          2026-10-29 with 4709 index entries from before 08-14 and no local
          folder, and the guard let it through because it assumed the first case.

        The index format is read off the file, not documented. If it is there
        but none of the folders that are in sync can be found in it, the format
        has changed: every path leaving the list without a local copy is then
        treated as remembered, because guessing "safe" is the expensive mistake.

        `index_reset=True` asks about the state after `reset_index()`: nothing is
        remembered then, so leaving the list is a download. Folders that were
        never excluded and have no local copy are still reported.
        """
        before_set = set(before)
        after_set = set(after)
        risky = set()
        for name in self._cloud_top_level_dirs():
            if name in after_set or name in before_set:
                continue  # excluded now, or leaving the list -> checked below
            if not os.path.isdir(os.path.join(self.local_root, name)):
                risky.add(name)

        leaving = [rel for rel in before_set - after_set
                   if not os.path.isdir(os.path.join(self.local_root, rel))]
        if leaving and not index_reset:
            data = self._index_bytes()
            if data is not None:
                in_sync = [name for name in sorted(os.listdir(self.local_root))
                           if not name.startswith(".")
                           and os.path.isdir(os.path.join(self.local_root, name))
                           and name not in before_set]
                readable = not in_sync or any(
                    self._index_remembers(data, name) for name in in_sync)
                for rel in leaving:
                    if not readable or self._index_remembers(data, rel):
                        risky.add(rel)
        return sorted(risky)

    def apply_policy(
        self,
        policy: Dict,
        *,
        dry_run: bool = False,
        force_unsafe: bool = False,
        reset_index: bool = False,
    ) -> Dict:
        exclude_dirs = self._policy_to_exclude_dirs(policy)
        live = self.is_live_config()
        if reset_index and not live and not dry_run:
            raise BackendError(
                "Refusing to reset the daemon index for a config that is not the "
                f"live one ({self.config_path}): the reset would hit the running "
                "daemon's folder, not the rehearsal's."
            )
        before = self._parse_exclude_dirs(self._read_config_lines())
        clears_exclude_dirs = bool(before) and not exclude_dirs
        if clears_exclude_dirs and not dry_run and not force_unsafe:
            raise BackendError(
                f"Refusing to clear all {len(before)} exclude-dirs entries: the "
                f"policy has no disabled paths, so the daemon would pull the "
                f"whole cloud down. If the policy simply doesn't know about the "
                f"current exclusions yet, import them: python3 "
                f"tools/sync_policy.py migrate --backend daemon --apply. "
                f"Pass force_unsafe=True to sync everything on purpose."
            )
        risky = self.deletion_risk_paths(before, exclude_dirs, index_reset=reset_index)
        if risky and not dry_run and not force_unsafe:
            raise BackendError(
                "Refusing to restart the yandex-disk daemon: these paths stay in "
                "sync but are missing locally, so the daemon would delete them in "
                "the cloud: " + ", ".join("/" + p for p in risky) + ". "
                "Restore the local copies, or exclude the paths first. A path the "
                "daemon's index (.sync/~state2) still remembers can leave the "
                "exclude list only together with an index reset "
                "(--reset-daemon-index): without the index the daemon downloads "
                "it instead. Pass force_unsafe=True only if the cloud deletion "
                "is intended."
            )
        result = {
            "schema": "sync_backend:v1",
            "backend": self.name(),
            "action": "apply_policy",
            "dry_run": dry_run,
            "config_path": self.config_path,
            "before": before,
            "after": exclude_dirs,
            "added": sorted(set(exclude_dirs) - set(before)),
            "removed": sorted(set(before) - set(exclude_dirs)),
            "daemon_restart": not dry_run and live,
            "index_reset": reset_index,
            "deletion_risk_paths": risky,
            "clears_exclude_dirs": clears_exclude_dirs,
            "error": None,
        }
        if dry_run:
            return result
        if not live:
            # A rehearsal config: write it, leave the running daemon alone.
            self._write_config(exclude_dirs)
            result["restart_skipped"] = "config is not the live daemon's"
            return result
        if not reset_index:
            self._write_config(exclude_dirs)
            restart_result = stop_start_daemon()
            result["restart"] = _command_payload(restart_result)
            return result

        # Stop, and make sure it is stopped, before anything else changes: the
        # new exclude list must never meet the old index.
        stopped = stop_daemon()
        for _ in range(30):
            if not daemon_running():
                break
            time.sleep(1)
        else:
            raise BackendError(
                "yandex-disk did not stop; nothing was changed. "
                f"stop: rc={stopped.returncode} {stopped.stdout or stopped.stderr}"
            )
        result["index_backup"] = self.reset_index()
        self._write_config(exclude_dirs)
        started = start_daemon()
        result["restart"] = _command_payload({"stop": stopped, "start": started})
        if started.returncode != 0:
            result["error"] = (
                "Index reset and config written, but yandex-disk did not start: "
                f"{started.stdout or started.stderr}. Start it by hand; the old "
                f"index is in {result['index_backup']}."
            )
        return result

    def run_sync(self, *, dry_run: bool = False, stream: bool = False) -> Dict:
        if dry_run:
            return {
                "schema": "sync_backend:v1",
                "backend": self.name(),
                "action": "run_sync",
                "dry_run": True,
                "plan": ["yandex-disk daemon syncs automatically; restart to force sync now"],
                "error": None,
            }
        restart_result = stop_start_daemon()
        return {
            "schema": "sync_backend:v1",
            "backend": self.name(),
            "action": "run_sync",
            "dry_run": False,
            "restart": {
                key: {
                    "cmd": value.cmd,
                    "returncode": value.returncode,
                    "stdout": value.stdout,
                    "stderr": value.stderr,
                }
                for key, value in restart_result.items()
            },
            "error": None,
        }

    def run_resync(self, *, dry_run: bool = False, stream: bool = False) -> Dict:
        raise NotSupportedError(
            "Resync baseline is not a concept supported by the yandex-disk daemon backend. "
            "Use --backend rclone for rclone bisync with explicit resync."
        )

    def list_cloud_children(self, parent_path: str) -> List[str]:
        """List child directories from monitor.db snapshot."""
        from tools.sync_common import create_storage
        from tools.sync_tree_cloud import fetch_child_names, select_snapshot_for_tree
        from ydm import Analyzer

        storage = create_storage(self.db_path)
        analyzer = Analyzer(storage)
        selection = select_snapshot_for_tree(analyzer, parent_path)
        snapshot = selection.snapshot
        from tools.sync_common import select_scan_id_for_path
        scan_id = select_scan_id_for_path(parent_path, snapshot)
        children = fetch_child_names(storage, scan_id, parent_path)
        return sorted(children, key=lambda x: x.lower())

    def run_cloud_scan(self, path: str) -> Dict:
        ydm_py = str(ROOT_DIR / "ydm.py")
        cmd = [
            sys.executable,
            ydm_py,
            "--db-path",
            self.db_path,
            "--backend",
            "api",
            "scan",
            "cloud",
            "--path",
            path,
        ]
        result = run_command(cmd)
        return {
            "schema": "sync_backend:v1",
            "backend": self.name(),
            "action": "run_cloud_scan",
            "cmd": result.cmd,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": None if result.returncode == 0 else f"cloud scan failed (rc={result.returncode})",
        }

    def status(self) -> Dict:
        result = run_command(["yandex-disk", "status"])
        return {
            "schema": "sync_backend:v1",
            "backend": self.name(),
            "action": "status",
            "running": result.returncode == 0,
            "cmd": result.cmd,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": None,
        }


class RcloneBackend(SyncBackend):
    """Backend for rclone-based sync (Android/Termux environments)."""

    kind = "rclone"

    def __init__(
        self,
        *,
        remote: str,
        db_path: str,
        local_root: str,
        policy_path: Optional[str] = None,
        bisync_filter_path: Optional[str] = None,
        download_filter_path: Optional[str] = None,
    ):
        self.remote = remote
        self.db_path = os.path.expanduser(db_path)
        self.local_root = os.path.expanduser(local_root)
        self.policy_path = policy_path or _default_policy_path()
        self.bisync_filter_path = bisync_filter_path or default_bisync_filter_path(local_root)
        self.download_filter_path = download_filter_path or default_download_filter_path(local_root)

    def name(self) -> str:
        return f"rclone remote:{self.remote}"

    def _ns_for_policy(self):
        """Build argparse.Namespace for sync_policy calls."""
        import argparse
        return argparse.Namespace(
            db_path=self.db_path,
            local_root=self.local_root,
            remote=self.remote,
            policy_path=self.policy_path,
            legacy_filter_path=None,
            download_filter_path=self.download_filter_path,
            bisync_filter_path=self.bisync_filter_path,
            format="json",
            text_header=False,
        )

    def _ns_for_bisync(self):
        """Build argparse.Namespace for sync_bisync calls."""
        import argparse
        return argparse.Namespace(
            db_path=self.db_path,
            local_root=self.local_root,
            remote=self.remote,
            filter_path=self.bisync_filter_path,
            policy_path=self.policy_path,
            max_delete=20,
            check_access=True,
            format="json",
            text_header=False,
            stream=False,
        )

    def apply_policy(self, policy: Dict, *, dry_run: bool = False) -> Dict:
        ns = self._ns_for_policy()
        ns.apply = not dry_run
        sync_policy = _load_sync_policy_functions()
        result = sync_policy.render_filters(ns, policy)
        result["backend"] = self.name()
        result["schema"] = "sync_backend:v1"
        return result

    def run_sync(self, *, dry_run: bool = False, stream: bool = False) -> Dict:
        from tools.sync_bisync import cmd_run
        ns = self._ns_for_bisync()
        ns.apply = not dry_run
        ns.stream = stream
        result = cmd_run(ns)
        result["backend"] = self.name()
        result["schema"] = "sync_backend:v1"
        return result

    def run_resync(self, *, dry_run: bool = False, stream: bool = False) -> Dict:
        from tools.sync_bisync import cmd_resync
        ns = self._ns_for_bisync()
        ns.apply = not dry_run
        ns.stream = stream
        result = cmd_resync(ns)
        result["backend"] = self.name()
        result["schema"] = "sync_backend:v1"
        return result

    def list_cloud_children(self, parent_path: str) -> List[str]:
        remote_path = parent_path.strip("/")
        target = f"{self.remote}:{remote_path}" if remote_path else f"{self.remote}:"
        try:
            proc = subprocess.run(
                [rclone_bin(), "lsf", target, "--dirs-only", "--max-depth", "1"],
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise BackendError(f"rclone lsf failed: {exc}")
        items = []
        for line in proc.stdout.splitlines():
            name = line.strip().rstrip("/")
            if name:
                items.append(name)
        return sorted(items, key=lambda x: x.lower())

    def run_cloud_scan(self, path: str) -> Dict:
        ydm_py = str(ROOT_DIR / "ydm.py")
        cmd = [
            sys.executable,
            ydm_py,
            "--db-path",
            self.db_path,
            "--backend",
            "rclone",
            "scan",
            "cloud",
            "--path",
            path,
        ]
        result = run_command(cmd)
        return {
            "schema": "sync_backend:v1",
            "backend": self.name(),
            "action": "run_cloud_scan",
            "cmd": result.cmd,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": None if result.returncode == 0 else f"cloud scan failed (rc={result.returncode})",
        }

    def status(self) -> Dict:
        from tools.sync_bisync import cmd_status
        ns = self._ns_for_bisync()
        result = cmd_status(ns)
        result["backend"] = self.name()
        result["schema"] = "sync_backend:v1"
        return result


def default_bisync_filter_path(local_root: str) -> str:
    sync_policy = _load_sync_policy_functions()
    return sync_policy.bisync_filter_path(local_root)


def default_download_filter_path(local_root: str) -> str:
    sync_policy = _load_sync_policy_functions()
    return sync_policy.download_filter_path(local_root)


def _default_policy_path() -> str:
    sync_policy = _load_sync_policy_functions()
    return sync_policy.default_policy_path()


def rclone_remote_exists(remote: str) -> bool:
    """Check if a named rclone remote is configured."""
    if shutil.which(rclone_bin()) is None:
        return False
    try:
        proc = subprocess.run(
            [rclone_bin(), "listremotes"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return False
        return f"{remote}:" in [line.strip() for line in proc.stdout.splitlines()]
    except (OSError, FileNotFoundError):
        return False


def daemon_available() -> bool:
    """Check if yandex-disk daemon is installed and configured."""
    if shutil.which("yandex-disk") is None:
        return False
    config_path = os.path.expanduser("~/.config/yandex-disk/config.cfg")
    return os.path.exists(config_path)


def daemon_is_active() -> bool:
    """Check if yandex-disk daemon is currently running."""
    if shutil.which("yandex-disk") is None:
        return False
    try:
        result = run_command(["yandex-disk", "status"])
        return result.returncode == 0
    except Exception:
        return False


def stop_yandex_disk_daemon() -> Dict:
    """Stop the yandex-disk daemon. Returns command result dict."""
    result = run_command(["yandex-disk", "stop"])
    return {
        "cmd": result.cmd,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def detect_backend(
    *,
    explicit: Optional[str] = None,
    env: Optional[str] = None,
    config: Optional[Dict] = None,
    db_path: str = "monitor.db",
    #: No default: the mirror lives wherever the person put it, and a path
    #: from the author's machine is not a better guess than an empty string.
    local_root: str = "",
    policy_path: Optional[str] = None,
    remote: str = "yandex",
    bisync_filter_path: Optional[str] = None,
    download_filter_path: Optional[str] = None,
    config_path: str = "~/.config/yandex-disk/config.cfg",
) -> SyncBackend:
    """Instantiate the appropriate backend.

    Priority:
      1. explicit argument (if not 'auto')
      2. env var
      3. config dict key 'backend'
      4. auto-detect
    """
    chosen = explicit
    if chosen is None or chosen == "auto":
        chosen = env
    if chosen is None or chosen == "auto":
        cfg = config or {}
        chosen = cfg.get("backend")
    if chosen is None or chosen == "auto":
        if daemon_available():
            chosen = "daemon"
        elif rclone_remote_exists(remote):
            chosen = "rclone"
        else:
            raise BackendError(
                "No sync backend available: yandex-disk daemon not configured "
                "and rclone remote 'yandex' not found. "
                "Install yandex-disk or run 'rclone config' with a yandex remote."
            )
    chosen = chosen.lower()
    if chosen == "daemon":
        return DaemonBackend(
            config_path=config_path,
            db_path=db_path,
            local_root=local_root,
            policy_path=policy_path,
        )
    if chosen == "rclone":
        return RcloneBackend(
            remote=remote,
            db_path=db_path,
            local_root=local_root,
            policy_path=policy_path,
            bisync_filter_path=bisync_filter_path,
            download_filter_path=download_filter_path,
        )
    raise BackendError(f"Unknown backend: {chosen}. Use daemon, rclone, or auto.")


def backend_from_args(args) -> SyncBackend:
    """Convenience: build backend from argparse Namespace."""
    env = os.environ.get("YDM_BACKEND")
    config = None
    config_profile = getattr(args, "config_profile", None)
    if config_profile is None:
        # sync_policy.py and friends do not expose --config-profile, but ydm.py does.
        # For tools without explicit config profile, load prod default.
        from ydm import load_config
        config = load_config("prod")
    else:
        from ydm import load_config
        config = load_config(config_profile)
    return detect_backend(
        explicit=getattr(args, "backend", None),
        env=env,
        config=config,
        db_path=getattr(args, "db_path", "monitor.db"),
        local_root=getattr(args, "local_root", None) or DEFAULT_CONFIG["local_root"] or "",
        policy_path=getattr(args, "policy_path", None),
        remote=getattr(args, "remote", DEFAULT_CONFIG["rclone_remote"]),
        bisync_filter_path=getattr(args, "bisync_filter_path", None),
        download_filter_path=getattr(args, "download_filter_path", None),
        config_path=getattr(args, "exclude_config", DEFAULT_CONFIG["exclude_config"]),
    )


def daemon_sync_root(config_path: str = "~/.config/yandex-disk/config.cfg") -> Optional[str]:
    """Return the daemon's canonical mirror root without contacting the daemon."""
    resolved = os.path.expanduser(config_path)
    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            for raw in handle:
                if raw.startswith("dir="):
                    value = raw.split("=", 1)[1].strip()
                    return os.path.realpath(os.path.expanduser(value)) if value else None
    except OSError:
        return None
    return None


def roots_overlap(first: str, second: str) -> bool:
    left = os.path.normcase(os.path.realpath(os.path.expanduser(first)))
    right = os.path.normcase(os.path.realpath(os.path.expanduser(second)))
    try:
        shared = os.path.commonpath((left, right))
    except ValueError:
        return False
    return shared in {left, right}


def daemon_conflict_error(local_root: str,
                          config_path: str = "~/.config/yandex-disk/config.cfg") -> Optional[str]:
    """Block rclone only when an active daemon owns the same filesystem tree."""
    if not daemon_is_active():
        return None
    daemon_root = daemon_sync_root(config_path)
    if daemon_root is None:
        return "yandex-disk daemon is active but its sync root cannot be verified"
    if roots_overlap(local_root, daemon_root):
        return (
            "yandex-disk daemon is active on an overlapping local root; stop it "
            "or choose a disjoint target root"
        )
    return None


def ensure_backend_no_daemon_conflict(backend: SyncBackend) -> None:
    """Refuse only an active daemon whose mirror overlaps this rclone root."""
    if backend.kind == "rclone":
        error = daemon_conflict_error(backend.local_root)
        if error is None:
            return
        print(
            f"WARNING: {error}.",
            file=sys.stderr,
        )
        raise BackendError(error)
