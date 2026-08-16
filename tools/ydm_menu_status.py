#!/usr/bin/env python3
"""Aggregate sync status for YDM menu."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import List, Optional

from tools.sync_bisync import cmd_status as bisync_status_payload
from tools.ydm_menu_config import MenuConfig


@dataclass
class MenuStatus:
    overall: str = "CHECK"
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None
    last_run_short: str = "unknown"
    lock_held: bool = False
    lock_pid: Optional[int] = None
    bidirectional: List[str] = field(default_factory=list)
    download_only: List[str] = field(default_factory=list)
    disabled: List[str] = field(default_factory=list)
    resync_needed: bool = False
    filter_mismatch: bool = False
    warnings: List[str] = field(default_factory=list)
    #: False on the daemon backend, which has no bisync run, lock or resync.
    bisync_fields_apply: bool = True


def _bisync_args(cfg: MenuConfig) -> argparse.Namespace:
    is_daemon = cfg.backend_kind == "daemon"
    backend_arg = "daemon" if is_daemon else "rclone"
    return argparse.Namespace(
        db_path=cfg.db_path,
        local_root=cfg.local_root,
        remote=cfg.remote,
        filter_path=cfg.bisync_filter_path,
        policy_path=cfg.policy_path,
        backend=backend_arg,
        exclude_config=cfg.exclude_config,
    )


def _daemon_status(cfg: MenuConfig) -> MenuStatus:
    """Status for the daemon backend, in the daemon's own terms.

    `resync`, `last bisync` and the process lock are rclone-bisync concepts.
    Reporting them here told a daemon user "NEEDS RESYNC" for a backend where
    resync does not exist (`run_resync` raises NotSupportedError).
    """
    from tools.sync_backends import DaemonBackend
    from tools.sync_policy import load_policy, policy_paths_by_mode

    status = MenuStatus(bisync_fields_apply=False)
    backend = DaemonBackend(
        config_path=cfg.exclude_config,
        db_path=cfg.db_path,
        local_root=cfg.local_root,
        policy_path=cfg.policy_path,
    )
    try:
        payload = backend.status()
    except Exception as exc:  # noqa: BLE001 - status must never break the menu
        status.warnings.append(f"daemon status: {exc}")
        return status

    # `yandex-disk status` prints a localized label first ("Sync core status:
    # idle" / "Статус ядра синхронизации: ожидание команды"); keep the value.
    first_line = (payload.get("stdout") or "").splitlines()
    head = first_line[0].strip() if first_line else "unknown"
    status.overall = head.split(": ", 1)[1].strip() if ": " in head else head
    if not payload.get("running"):
        status.overall = "daemon not running"
        status.warnings.append(payload.get("stderr") or "yandex-disk status failed")

    policy = load_policy(cfg.policy_path)
    if policy:
        status.bidirectional = policy_paths_by_mode(policy, "bidirectional")
        status.download_only = policy_paths_by_mode(policy, "download_only")
        status.disabled = policy_paths_by_mode(policy, "disabled")
        if status.download_only:
            status.warnings.append(
                f"{len(status.download_only)} download_only path(s): "
                f"not supported by the daemon backend"
            )
    return status


def load_status(cfg: MenuConfig) -> MenuStatus:
    if cfg.backend_kind == "daemon":
        return _daemon_status(cfg)
    status = MenuStatus()
    try:
        bisync = bisync_status_payload(_bisync_args(cfg))
    except Exception as exc:
        status.warnings.append(f"bisync status: {exc}")
        return status

    state = bisync.get("state") or {}
    policy = bisync.get("policy") or {}
    lock = bisync.get("lock") or {}

    status.last_run_at = state.get("last_run_at")
    status.last_status = state.get("last_status")
    status.last_run_short = (status.last_run_at or "never")[:16]
    status.lock_held = bool(lock.get("held"))
    status.lock_pid = lock.get("pid")
    status.bidirectional = list(policy.get("bidirectional_paths") or [])
    status.download_only = list(policy.get("download_only_paths") or [])
    status.disabled = list(policy.get("disabled_paths") or [])

    policy_resync = bool(policy.get("policy_filter_resync_needed"))
    bisync_resync = bool(bisync.get("resync_needed"))
    status.resync_needed = policy_resync or bisync_resync

    if status.lock_held:
        status.overall = f"BUSY (pid {status.lock_pid})"
    elif status.resync_needed:
        status.overall = "NEEDS RESYNC"
    elif status.last_status == "ok":
        status.overall = "OK"
    else:
        status.overall = f"CHECK ({status.last_status or 'unknown'})"

    if bisync.get("error"):
        status.warnings.append(str(bisync["error"]))
    return status
