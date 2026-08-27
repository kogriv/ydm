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
    #: One line about the composite snapshot's age, or None when there is no
    #: snapshot to describe. The menu had no freshness signal at all until
    #: 2026-08-24 — see tasks/ydm_menu/AUDIT-2026-08-24.md D.
    snapshot_line: Optional[str] = None
    #: True only when the stale part of the snapshot is actually compared
    #: against the local copy. Age alone is not a problem: on the machine this
    #: was written for, every stale file sits in an excluded folder.
    snapshot_stale: bool = False


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


def menu_exclude_dirs(cfg: MenuConfig) -> set:
    """The exclusions in force for this backend — an empty set, never None.

    Under the daemon that is `exclude-dirs` from the configured file. Under
    rclone it is nothing: a whitelist backend is not governed by the daemon's
    blacklist, and folders it names are compared like any other.

    One definition for both readers — the freshness line and the diff screen —
    because two readers deriving this separately is how they drift apart, which
    is the same reason `load_exclude_dirs` lives in ydm.py rather than inside
    `get_diff()`.

    None is not an option here on purpose. Until 2026-08-27 both callers passed
    it, and both functions downstream read it as "go and load the daemon's
    config from its *default* path" — the live one, on any machine that has
    both backends installed. The effect was to move stale folders into the
    bucket that never warns.
    """
    # `load_exclude_dirs` from ydm, not from sync_common: the two share a name
    # but not a return type, and what is wanted here is the plain set.
    from ydm import load_exclude_dirs

    if cfg.backend_kind != "daemon":
        return set()
    return load_exclude_dirs(cfg.exclude_config)


def _apply_snapshot_freshness(cfg: MenuConfig, status: MenuStatus) -> None:
    """How old the snapshot is, said where a person will see it.

    `sync_tree` has printed this in its header for weeks; the menu did not,
    so someone working only from the menu made every decision on top of a
    snapshot whose age nobody had mentioned. Same rule as the tree: age is
    reported always, warned about only when the stale part is compared.
    """
    from tools.sync_common import create_storage
    from ydm import Analyzer

    try:
        analyzer = Analyzer(create_storage(cfg.db_path))
        fresh = analyzer.snapshot_freshness(exclude_dirs=menu_exclude_dirs(cfg))
    except Exception:
        return
    if not fresh or fresh.get("base_scan_id") is None:
        return

    status.snapshot_line = (
        f"base #{fresh['base_scan_id']}, {fresh.get('base_age_days', '?')} day(s) old, "
        f"serves {fresh.get('base_share_percent', 0)}% of files"
    )
    status.snapshot_stale = bool(fresh.get("warnings"))
    for warning in fresh.get("warnings") or []:
        status.warnings.append(warning)


def load_status(cfg: MenuConfig) -> MenuStatus:
    if cfg.backend_kind == "daemon":
        status = _daemon_status(cfg)
        _apply_snapshot_freshness(cfg, status)
        return status
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
    _apply_snapshot_freshness(cfg, status)
    return status
