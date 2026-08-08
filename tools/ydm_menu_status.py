#!/usr/bin/env python3
"""Aggregate sync status for YDM menu."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import List, Optional

from tools.sync_bisync import cmd_status as bisync_status_payload
from tools.sync_policy import status_payload as policy_status_payload
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


def _bisync_args(cfg: MenuConfig) -> argparse.Namespace:
    return argparse.Namespace(
        db_path=cfg.db_path,
        local_root=cfg.local_root,
        remote=cfg.remote,
        filter_path=cfg.bisync_filter_path,
        policy_path=cfg.policy_path,
    )


def _policy_args(cfg: MenuConfig) -> argparse.Namespace:
    return argparse.Namespace(
        db_path=cfg.db_path,
        local_root=cfg.local_root,
        remote=cfg.remote,
        policy_path=cfg.policy_path,
        legacy_filter_path=None,
        download_filter_path=None,
        bisync_filter_path=cfg.bisync_filter_path,
    )


def load_status(cfg: MenuConfig) -> MenuStatus:
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

    try:
        pol = policy_status_payload(_policy_args(cfg))
        if pol.get("bisync_filter_hash") and pol.get("download_filter_hash"):
            pass
    except Exception:
        pass

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
