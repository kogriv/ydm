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


def cloud_list_dirs(cfg: MenuConfig, parent: str) -> List[str]:
    backend = backend_from_args(_policy_ns(cfg))
    try:
        return backend.list_cloud_children(parent)
    except Exception as exc:
        print(f"cloud list failed: {exc}")
        return []


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


def print_detailed_status(cfg: MenuConfig) -> None:
    from tools.sync_bisync import cmd_status

    status = load_status(cfg)
    print(f"Overall: {status.overall}")
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
