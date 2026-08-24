#!/usr/bin/env python3
"""Sync actions orchestration for YDM menu."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from tools.sync_backends import (
    DaemonBackend,
    RcloneBackend,
    backend_from_args,
    stop_yandex_disk_daemon,
)
from tools.sync_bisync import cmd_resync, cmd_run
from tools.sync_common import delete_local_entry_contents, load_sync_filters, rclone_check_entry
from tools.sync_policy import (
    add_policy_path,
    inspect_path,
    load_policy,
    remove_policy_path,
    render_filters,
)
from tools.ydm_menu_config import MenuConfig
from tools.ydm_menu_status import MenuStatus, load_status

ROOT_DIR = Path(__file__).resolve().parents[1]


@dataclass
class ActionResult:
    ok: bool
    message: str
    details: Optional[dict] = None


def _policy_ns(cfg: MenuConfig, **extra) -> argparse.Namespace:
    is_daemon = cfg.backend_kind == "daemon"
    backend_arg = "daemon" if is_daemon else "rclone"
    base = {
        "db_path": cfg.db_path,
        "local_root": cfg.local_root,
        "remote": cfg.remote,
        "policy_path": cfg.policy_path,
        "legacy_filter_path": None,
        "download_filter_path": None,
        "bisync_filter_path": cfg.bisync_filter_path,
        "format": "json",
        "text_header": False,
        "backend": backend_arg,
        "exclude_config": cfg.exclude_config,
        "apply": False,
    }
    base.update(extra)
    return argparse.Namespace(**base)


def _bisync_ns(cfg: MenuConfig, **extra) -> argparse.Namespace:
    is_daemon = cfg.backend_kind == "daemon"
    backend_arg = "daemon" if is_daemon else "rclone"
    base = {
        "db_path": cfg.db_path,
        "local_root": cfg.local_root,
        "remote": cfg.remote,
        "filter_path": cfg.bisync_filter_path,
        "policy_path": cfg.policy_path,
        "max_delete": 20,
        "check_access": True,
        "format": "json",
        "text_header": False,
        "stream": False,
        "backend": backend_arg,
        "exclude_config": cfg.exclude_config,
    }
    base.update(extra)
    return argparse.Namespace(**base)


def bisync_scope_lines(cfg: MenuConfig) -> List[str]:
    filters = load_sync_filters(cfg.bisync_filter_path)
    paths = sorted(set(filters.include_dirs))
    lines = [
        "",
        "Bisync scope (only these folders — NOT the whole disk):",
    ]
    for path in paths:
        lines.append(f"  + /{path}/")
    lines.append(f"  ({len(paths)} folders, filter: {cfg.bisync_filter_path})")
    lines.append(
        "Resync scans all listed folders to rebuild baseline — may take several minutes."
    )
    return lines


def print_bisync_scope(cfg: MenuConfig) -> None:
    for line in bisync_scope_lines(cfg):
        print(line)


def _interactive_stream(*, apply: bool) -> bool:
    return apply and sys.stdin.isatty()


def _format_bisync_result(payload: dict, *, label: str) -> ActionResult:
    if payload.get("error"):
        bisync = payload.get("bisync") or {}
        tail = (bisync.get("stderr") or bisync.get("stdout") or "").strip()
        msg = payload["error"]
        if tail:
            lines = tail.splitlines()
            msg = f"{msg}\n(last lines:\n" + "\n".join(lines[-5:]) + ")"
        return ActionResult(False, msg, payload)
    return ActionResult(True, f"{label} completed", payload)


def render_filters_apply(cfg: MenuConfig) -> ActionResult:
    policy = load_policy(cfg.policy_path)
    if not policy:
        return ActionResult(False, f"Policy not found: {cfg.policy_path}")
    backend = backend_from_args(_policy_ns(cfg))
    write = backend.apply_policy(policy, dry_run=False)
    if write.get("error"):
        return ActionResult(False, write["error"], write)
    return ActionResult(True, f"Policy applied via {backend.name()}", write)


def action_inspect(cfg: MenuConfig, path: str) -> dict:
    return inspect_path(cfg.db_path, cfg.local_root, path)


def action_add(
    cfg: MenuConfig,
    path: str,
    mode: str,
    *,
    force_risk: bool = False,
) -> ActionResult:
    ns = _policy_ns(cfg, path=path, mode=mode, apply=True, force_risk=force_risk)
    payload = add_policy_path(ns)
    if payload.get("error"):
        return ActionResult(False, payload["error"], payload)
    backend = backend_from_args(ns)
    policy = load_policy(ns.policy_path)
    if policy:
        apply_payload = backend.apply_policy(policy, dry_run=False)
        payload["backend_apply"] = apply_payload
        if apply_payload.get("error"):
            return ActionResult(False, apply_payload["error"], payload)
    msg = f"Added {payload.get('path')} as {mode} via {cfg.backend_name}"
    return ActionResult(True, msg, payload)


def action_remove(
    cfg: MenuConfig,
    path: str,
    *,
    delete_local: bool = False,
) -> ActionResult:
    ns = _policy_ns(cfg, path=path, apply=True)
    payload = remove_policy_path(ns)
    if payload.get("error"):
        return ActionResult(False, payload["error"], payload)
    backend = backend_from_args(ns)
    policy = load_policy(ns.policy_path)
    if policy:
        apply_payload = backend.apply_policy(policy, dry_run=False)
        payload["backend_apply"] = apply_payload
        if apply_payload.get("error"):
            return ActionResult(False, apply_payload["error"], payload)

    local_info = None
    if delete_local:
        entry = path.strip("/")
        check = rclone_check_entry(cfg.remote, entry, cfg.local_root)
        if check.returncode == 0:
            local_info = delete_local_entry_contents(cfg.local_root, entry)
        else:
            local_info = {
                "deleted": False,
                "reason": "rclone check failed — local copy kept",
            }
    return ActionResult(
        True,
        f"Removed {path} from policy via {cfg.backend_name}",
        {"remove": payload, "local_delete": local_info},
    )


def resync_needed(cfg: MenuConfig) -> bool:
    return load_status(cfg).resync_needed


def offer_resync_if_needed(
    cfg: MenuConfig,
    *,
    mode: str,
    reader,
    default_yes: bool = True,
) -> None:
    if mode != "bidirectional":
        return
    if cfg.backend_kind == "daemon":
        # Daemon backend does not use rclone bisync; sync happens automatically.
        print("yandex-disk daemon will sync the folder automatically.")
        return
    if not resync_needed(cfg):
        return
    from tools.ydm_menu_prompts import prompt_yes_no

    if prompt_yes_no(
        "Filters changed. Run bisync resync now?",
        default=default_yes,
        reader=reader,
    ):
        print_bisync_scope(cfg)
        result = action_resync(cfg, apply=True)
        print(result.message)
    else:
        print("Skipped resync. Scheduled bisync may fail until resync is done.")


def action_resync(cfg: MenuConfig, *, apply: bool) -> ActionResult:
    stream = _interactive_stream(apply=apply)
    payload = cmd_resync(_bisync_ns(cfg, apply=apply, stream=stream))
    if payload.get("error"):
        return _format_bisync_result(payload, label="Bisync resync")
    if not apply:
        lines = payload.get("plan_text") or []
        scope = bisync_scope_lines(cfg)
        return ActionResult(
            True,
            "Resync plan (dry-run):\n" + "\n".join(scope + [""] + lines),
            payload,
        )
    return _format_bisync_result(payload, label="Bisync resync")


def action_bisync_run(cfg: MenuConfig, *, apply: bool) -> ActionResult:
    status = load_status(cfg)
    if status.lock_held:
        return ActionResult(False, f"Bisync busy (pid {status.lock_pid})")
    stream = _interactive_stream(apply=apply)
    backend = backend_from_args(_bisync_ns(cfg))
    if isinstance(backend, DaemonBackend):
        if not apply:
            return ActionResult(True, "Daemon sync: apply to restart yandex-disk daemon")
        payload = backend.run_sync(dry_run=False)
        if payload.get("error"):
            return ActionResult(False, payload["error"], payload)
        return ActionResult(True, "yandex-disk daemon restarted", payload)
    if apply:
        print_bisync_scope(cfg)
    payload = backend.run_sync(dry_run=not apply, stream=stream)
    if payload.get("error"):
        return _format_bisync_result(payload, label="Bisync run")
    if not apply:
        return ActionResult(True, "Bisync dry-run OK", payload)
    return _format_bisync_result(payload, label="Bisync run")


def handle_blocked_add(
    cfg: MenuConfig,
    path: str,
    inspect_payload: dict,
    *,
    reader,
) -> Optional[ActionResult]:
    from tools.ydm_menu_prompts import prompt_int

    risks = inspect_payload.get("risks") or []
    print(f"\nCannot add as bidirectional: {path}")
    for risk in risks:
        print(f"  {risk.get('code')} ({risk.get('count')})")
        for ex in (risk.get("examples") or [])[:2]:
            print(f"    - {ex}")
    print("")
    print(" 1  Add as download-only instead")
    print(" 2  Hint: run cloud scan from menu (7)")
    print(" 3  Force bidirectional (expert)")
    print(" 0  Cancel")
    choice = prompt_int("Choose", reader=reader)
    if choice == 1:
        return action_add(cfg, path, "download_only")
    if choice == 3:
        from tools.ydm_menu_prompts import prompt_confirm_name

        if prompt_confirm_name("yes", reader=reader):
            return action_add(cfg, path, "bidirectional", force_risk=True)
    return None


def snapshot_top_level(cfg: MenuConfig, limit: int = 6) -> List[str]:
    """Top-level cloud folders, as the snapshot in the database has them.

    Three screens used to offer a list typed in by hand — `/Books/Math`,
    `/DAO`, `/pro/agents`. They mean nothing on another machine, and on the
    machine they came from `/Books` is disabled outright, so the list had gone
    stale even for its author. See tasks/ydm_menu/AUDIT-2026-08-24.md C.

    Read from the database rather than through the backend on purpose: it is
    offline, deterministic, and identical under daemon and rclone. Asking the
    backend would put an `rclone lsf` against the live remote behind the act
    of opening a menu.
    """
    from tools.sync_common import create_storage
    from tools.sync_tree_cloud import fetch_child_names, select_snapshot_for_tree
    from ydm import Analyzer

    try:
        analyzer = Analyzer(create_storage(cfg.db_path))
        snapshot = select_snapshot_for_tree(analyzer, "/").snapshot
        scan_id = snapshot.base_scan_id
        names = fetch_child_names(analyzer.storage, scan_id, "/")
    except Exception:
        # A missing or empty database is an ordinary state here — the menu
        # says so elsewhere, and offering nothing is better than crashing.
        return []
    return [f"/{name}" for name in names[:limit]]


def cloud_list_dirs(cfg: MenuConfig, parent: str) -> List[str]:
    backend = backend_from_args(_policy_ns(cfg))
    try:
        return backend.list_cloud_children(parent)
    except Exception as exc:
        print(f"cloud list failed: {exc}")
        return []


def _delta_ns(cfg: MenuConfig, **overrides) -> argparse.Namespace:
    """Arguments for tools/cloud_delta.py, which takes a Namespace like the
    bisync helpers do. Defaults match its own CLI defaults."""
    ns = argparse.Namespace(
        format="json",
        remote=cfg.remote,
        token_source="auto",
        revision_state=None,
        db_path=cfg.db_path,
        save=False,
        limit=50,
        max_pages=20,
        trash_max_pages=5,
        no_trash=False,
        force=False,
        no_save=True,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def check_cloud_changes(cfg: MenuConfig) -> ActionResult:
    """One request: has anything changed on the disk at all.

    This is the cheap half of the smart scan — one call against roughly 4 600
    for a full walk. "Nothing changed" is a real and useful answer, so it is
    reported as success rather than as an empty result.
    """
    from tools.cloud_delta import check_revision

    try:
        payload = check_revision(_delta_ns(cfg))
    except (Exception, SystemExit) as exc:
        # SystemExit is deliberate: resolve_token() raises it when there is no
        # token, and it does not descend from Exception. Without this the menu
        # exits instead of saying that credentials are missing.
        return ActionResult(False, f"cannot check the cloud: {exc}")
    if payload.get("error"):
        return ActionResult(False, str(payload["error"]), payload)
    return ActionResult(True, "checked", payload)


def list_stale_folders(cfg: MenuConfig) -> ActionResult:
    """Which folders of the snapshot went stale, and what would refresh them.

    Read-only: it names the folders, it does not rescan them. Keeping one
    writer for the snapshot was a deliberate decision — see
    tasks/delta_scan/README.md, "Step 3 — decided against".
    """
    from tools.cloud_delta import report_changes

    try:
        payload = report_changes(_delta_ns(cfg, force=True))
    except (Exception, SystemExit) as exc:  # see check_cloud_changes
        return ActionResult(False, f"cannot inspect the cloud: {exc}")
    if payload.get("error"):
        return ActionResult(False, str(payload["error"]), payload)
    return ActionResult(True, "inspected", payload)


def run_cloud_scan(cfg: MenuConfig, path: str) -> ActionResult:
    backend = backend_from_args(_policy_ns(cfg))
    print(f"Running cloud scan for {path} via {backend.name()}")
    print("(Ctrl+C safe — scan is resumable)")
    try:
        result = backend.run_cloud_scan(path)
    except KeyboardInterrupt:
        return ActionResult(False, "Scan interrupted")
    if result.get("error"):
        return ActionResult(False, result["error"], result)
    return ActionResult(True, f"Cloud scan finished for {path}", result)


def run_sync_tree(cfg: MenuConfig, path: str, depth: int) -> ActionResult:
    is_daemon = cfg.backend_kind == "daemon"
    backend_arg = "daemon" if is_daemon else "rclone"
    cmd = [
        sys.executable,
        str(ROOT_DIR / "tools" / "sync_tree.py"),
        "--db-path",
        cfg.db_path,
        "--backend",
        backend_arg,
        "--local-root",
        cfg.local_root,
        "--policy-path",
        cfg.policy_path,
        "--path",
        path,
        "--depth",
        str(depth),
        "--schema",
        "sync_tree:v2",
        "--format",
        "text",
        "--text-tree",
        "--no-local-scan",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    output = proc.stdout
    if proc.returncode != 0:
        return ActionResult(False, proc.stderr or "sync_tree failed")
    if sys.stdin.isatty() and shutil_which("less"):
        subprocess.run(["less", "-F"], input=output, text=True)
    else:
        print(output)
    return ActionResult(True, "Tree shown")


def shutil_which(name: str) -> bool:
    from shutil import which
    return which(name) is not None


def print_cloud_local_diff(cfg: MenuConfig) -> None:
    """The comparison the whole project exists for, finally reachable from the menu.

    `report diff` was available only from the command line — the menu, built
    for the person who does not want the command line, could not show it. See
    tasks/ydm_menu/AUDIT-2026-08-24.md A.
    """
    from tools.sync_common import create_storage
    from ydm import Analyzer

    print("Comparing the cloud snapshot with the local copy…")
    try:
        analyzer = Analyzer(create_storage(cfg.db_path))
        result = analyzer.get_diff()
    except Exception as exc:
        print(f"  diff failed: {exc}")
        return
    if result.get("error"):
        print(f"  {result['error']}")
        return

    compare = result.get("compare_scans") or {}
    print(f"  cloud: {compare.get('cloud')}   local scan: {compare.get('local')}")
    rows = [
        ("in the cloud", result.get("cloud_files_count")),
        ("matched locally", result.get("matched_count")),
        ("excluded from sync", result.get("excluded_from_sync_count")),
        ("local, outside sync", result.get("local_ignored_count")),
        ("missing locally", result.get("missing_local_count")),
        ("missing in the cloud", result.get("missing_cloud_count")),
    ]
    for label, value in rows:
        if value is not None:
            print(f"  {label:<22} {value}")
    for sample_key, label in (("missing_local_sample", "missing locally"),
                              ("missing_cloud_sample", "missing in the cloud")):
        for path in (result.get(sample_key) or [])[:5]:
            print(f"    {label}: {path}")
    for warning in result.get("warnings") or []:
        print(f"  WARN: {warning}")


def print_detailed_status(cfg: MenuConfig) -> None:
    from tools.sync_bisync import cmd_status

    status = load_status(cfg)
    print(f"Overall: {status.overall}")
    # The header already refuses to invent bisync fields on the daemon, which
    # has no run, no lock and no resync baseline. This screen printed them
    # anyway, so the same screen was honest above and made-up below.
    if status.bisync_fields_apply:
        print(f"Last run: {status.last_run_at} ({status.last_status})")
        print(f"Lock: {status.lock_held} pid={status.lock_pid}")
        print(f"Resync needed: {status.resync_needed}")
    print("Bidirectional:")
    for p in status.bidirectional:
        print(f"  [B] {p}")
    print("Download-only:")
    for p in status.download_only:
        print(f"  [D] {p}")
    if status.disabled:
        print("Disabled:")
        for p in status.disabled:
            print(f"  [X] {p}")
    bisync = cmd_status(_bisync_ns(cfg))
    for line in (bisync.get("log_tail") or [])[-5:]:
        print(f"  log: {line}")
