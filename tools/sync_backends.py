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
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.sync_common import (  # noqa: E402
    CommandResult,
    default_filter_path,
    load_sync_filters,
    run_command,
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
            from datetime import datetime
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

    def deletion_risk_paths(self, before: List[str], after: List[str]) -> List[str]:
        """Paths whose local copy vanished while they stay inside the daemon's scope.

        Restarting the daemon in that state makes it read the absence as a user
        deletion and propagate it to the cloud — this is exactly what moved
        /Books (113 GB) to the trash on 2026-08-14. A path that is only *now*
        leaving the exclude list is not at risk: the daemon has never tracked
        it, so it downloads instead of deleting.
        """
        before_set = set(before)
        after_set = set(after)
        risky = []
        for name in self._cloud_top_level_dirs():
            if name in after_set or name in before_set:
                continue  # excluded now, or newly included -> daemon downloads
            if not os.path.isdir(os.path.join(self.local_root, name)):
                risky.append(name)
        return sorted(risky)

    def apply_policy(
        self,
        policy: Dict,
        *,
        dry_run: bool = False,
        force_unsafe: bool = False,
    ) -> Dict:
        exclude_dirs = self._policy_to_exclude_dirs(policy)
        before = self._parse_exclude_dirs(self._read_config_lines())
        risky = self.deletion_risk_paths(before, exclude_dirs)
        if risky and not dry_run and not force_unsafe:
            raise BackendError(
                "Refusing to restart the yandex-disk daemon: these paths stay in "
                "sync but are missing locally, so the daemon would delete them in "
                "the cloud: " + ", ".join("/" + p for p in risky) + ". "
                "Restore the local copies, or exclude the paths first, or pass "
                "force_unsafe=True if the cloud deletion is intended."
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
            "daemon_restart": not dry_run,
            "deletion_risk_paths": risky,
            "error": None,
        }
        if not dry_run:
            self._write_config(exclude_dirs)
            # Restart daemon
            restart_result = stop_start_daemon()
            result["restart"] = {
                key: {
                    "cmd": value.cmd,
                    "returncode": value.returncode,
                    "stdout": value.stdout,
                    "stderr": value.stderr,
                }
                for key, value in restart_result.items()
            }
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
                ["rclone", "lsf", target, "--dirs-only", "--max-depth", "1"],
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
    if shutil.which("rclone") is None:
        return False
    try:
        proc = subprocess.run(
            ["rclone", "listremotes"],
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
    local_root: str = "/data/ya_disk",
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
        local_root=getattr(args, "local_root", DEFAULT_CONFIG["local_root"]),
        policy_path=getattr(args, "policy_path", None),
        remote=getattr(args, "remote", DEFAULT_CONFIG["rclone_remote"]),
        bisync_filter_path=getattr(args, "bisync_filter_path", None),
        download_filter_path=getattr(args, "download_filter_path", None),
        config_path=getattr(args, "exclude_config", DEFAULT_CONFIG["exclude_config"]),
    )


def ensure_backend_no_daemon_conflict(backend: SyncBackend) -> None:
    """If user chose rclone backend while yandex-disk daemon is active, warn/stop."""
    if backend.kind == "rclone" and daemon_is_active():
        print(
            "WARNING: yandex-disk daemon is active. Using rclone backend alongside it "
            "will cause sync conflicts and may lead to data loss.",
            file=sys.stderr,
        )
        # In interactive mode we would prompt; in library mode we raise.
        raise BackendError(
            "yandex-disk daemon is active. Stop it first (yandex-disk stop) "
            "or choose --backend daemon."
        )
