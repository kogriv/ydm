#!/usr/bin/env python3
"""
Fast policy-safe rename/move path for the rclone backend.

`rclone bisync` treats a local rename as delete+upload. This tool performs an
explicit local rename plus server-side `rclone moveto`, then refreshes the
bisync baseline.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.sync_common import (  # noqa: E402
    CommandResult,
    filter_file_hash,
    load_bisync_state,
    local_encoding_flags_for_path,
    run_command,
    var_path,
)
from tools.sync_policy import (  # noqa: E402
    ANDROID_FORBIDDEN_CHARS,
    default_policy_path,
    load_policy,
    normalize_entry,
)
from ydm import DEFAULT_CONFIG  # noqa: E402


SCHEMA = "ydm_sync_rename:v1"


def default_bisync_filter_path(local_root: str) -> str:
    return f"{os.path.expanduser(local_root).rstrip('/')}.bisync.filters"


def append_json_log(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", buffering=1) as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def remote_path(remote: str, entry: str) -> str:
    return f"{remote}:{entry}"


def local_path(local_root: str, entry: str) -> str:
    return os.path.join(os.path.expanduser(local_root), entry)


def path_mode(policy: dict, entry: str) -> Tuple[Optional[str], Optional[str]]:
    best_root = None
    best_mode = None
    for root, meta in policy.get("paths", {}).items():
        if entry == root or entry.startswith(root + "/"):
            if best_root is None or len(root) > len(best_root):
                best_root = root
                best_mode = meta.get("mode")
    return best_root, best_mode


def validate_new_path_chars(entry: str, local_root: str) -> List[str]:
    resolved = os.path.abspath(os.path.expanduser(local_root))
    is_android_shared = (
        resolved == "/sdcard"
        or resolved.startswith("/sdcard/")
        or resolved == "/storage/emulated/0"
        or resolved.startswith("/storage/emulated/0/")
        or resolved == "/mnt/sdcard"
        or resolved.startswith("/mnt/sdcard/")
    )
    if not is_android_shared:
        return []
    return sorted({ch for ch in entry if ch in ANDROID_FORBIDDEN_CHARS})


def rclone_lsjson(remote: str, entry: str, local_root: str) -> CommandResult:
    cmd = ["rclone", "lsjson", remote_path(remote, entry), "--max-depth", "1"]
    cmd.extend(local_encoding_flags_for_path(local_root))
    return run_command(cmd)


def remote_exists(remote: str, entry: str, local_root: str) -> dict:
    result = rclone_lsjson(remote, entry, local_root)
    exists = result.returncode == 0
    parsed: Any = None
    if result.stdout:
        try:
            parsed = json.loads(result.stdout)
        except ValueError:
            parsed = None
    return {
        "exists": exists,
        "cmd": result.cmd,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "items": parsed if exists else None,
    }


def rclone_moveto(remote: str, old_entry: str, new_entry: str, local_root: str) -> CommandResult:
    cmd = ["rclone", "moveto", remote_path(remote, old_entry), remote_path(remote, new_entry)]
    cmd.extend(local_encoding_flags_for_path(local_root))
    return run_command(cmd)


def run_followup_bisync(args: argparse.Namespace) -> Optional[CommandResult]:
    if args.followup == "none":
        return None
    cmd = [
        sys.executable,
        str(ROOT_DIR / "tools" / "sync_bisync.py"),
        args.followup,
        "--apply",
        "--db-path",
        args.db_path,
        "--local-root",
        args.local_root,
        "--remote",
        args.remote,
        "--filter-path",
        args.bisync_filter_path,
        "--format",
        "json",
        "--max-delete",
        str(args.max_delete),
    ]
    if not args.check_access:
        cmd.append("--no-check-access")
    if args.followup == "run" and not args.notify_on_error:
        cmd.append("--no-notify-on-error")
    result = subprocess.run(cmd, capture_output=True, text=True)
    return CommandResult(
        cmd=cmd,
        returncode=result.returncode,
        stdout=result.stdout.strip(),
        stderr=result.stderr.strip(),
    )


def preflight(args: argparse.Namespace) -> dict:
    old_entry = normalize_entry(args.old)
    new_entry = normalize_entry(args.new)
    policy_path = os.path.expanduser(args.policy_path)
    bisync_filter_path = os.path.expanduser(args.bisync_filter_path)
    local_old = local_path(args.local_root, old_entry)
    local_new = local_path(args.local_root, new_entry)

    payload = {
        "schema": SCHEMA,
        "action": args.command,
        "dry_run": args.command == "plan",
        "old": f"/{old_entry}",
        "new": f"/{new_entry}",
        "local_root": args.local_root,
        "remote": args.remote,
        "policy_path": policy_path,
        "bisync_filter_path": bisync_filter_path,
        "checks": [],
        "operations": [],
        "warnings": [],
        "error": None,
    }

    def check(name: str, ok: bool, detail: Any = None) -> None:
        payload["checks"].append({"name": name, "ok": ok, "detail": None if ok else detail})
        if not ok and payload["error"] is None:
            payload["error"] = str(detail or name)

    if not old_entry or not new_entry:
        check("path_not_root", False, "Refusing to rename the sync root itself.")
        return payload
    if old_entry == new_entry:
        check("paths_differ", False, "Old and new paths are identical.")
        return payload
    if new_entry.startswith(old_entry + "/"):
        check("not_move_into_self", False, "Refusing to move a path into itself.")
        return payload

    try:
        policy = load_policy(policy_path)
    except Exception as exc:
        check("policy_load", False, f"Failed to load policy: {exc}")
        return payload
    check("policy_exists", policy is not None, f"Policy not found: {policy_path}")
    if not policy:
        return payload

    old_root, old_mode = path_mode(policy, old_entry)
    new_root, new_mode = path_mode(policy, new_entry)
    check("old_under_policy", old_root is not None, f"Old path is outside policy: /{old_entry}")
    check("new_under_policy", new_root is not None, f"New path is outside policy: /{new_entry}")
    if payload["error"]:
        return payload
    check(
        "same_policy_root",
        old_root == new_root,
        f"Moving across policy roots is not allowed: {old_root} -> {new_root}",
    )
    check(
        "bidirectional_policy",
        old_mode == "bidirectional" and new_mode == "bidirectional",
        f"Only bidirectional policy paths are eligible: {old_mode} -> {new_mode}",
    )
    if old_entry == old_root or new_entry == new_root:
        check("not_policy_root", False, "Refusing to rename a policy root in MVP.")
    if payload["error"]:
        return payload

    bad_chars = validate_new_path_chars(new_entry, args.local_root)
    check(
        "android_name_compatible",
        not bad_chars,
        f"Target path contains Android-incompatible chars: {''.join(bad_chars)}",
    )

    check("local_old_exists", os.path.exists(local_old), f"Local source not found: {local_old}")
    check("local_new_missing", not os.path.exists(local_new), f"Local target already exists: {local_new}")
    local_new_parent = os.path.dirname(local_new)
    check(
        "local_new_parent_exists",
        os.path.isdir(local_new_parent),
        f"Local target parent not found: {local_new_parent}",
    )

    remote_old = remote_exists(args.remote, old_entry, args.local_root)
    remote_new = remote_exists(args.remote, new_entry, args.local_root)
    payload["remote_old"] = {
        "exists": remote_old["exists"],
        "returncode": remote_old["returncode"],
        "stderr": remote_old["stderr"],
    }
    payload["remote_new"] = {
        "exists": remote_new["exists"],
        "returncode": remote_new["returncode"],
        "stderr": remote_new["stderr"],
    }
    check("remote_old_exists", remote_old["exists"], f"Remote source not found: {remote_path(args.remote, old_entry)}")
    check("remote_new_missing", not remote_new["exists"], f"Remote target already exists: {remote_path(args.remote, new_entry)}")

    payload["operations"] = [
        {"type": "local_mv", "from": local_old, "to": local_new},
        {
            "type": "remote_moveto",
            "cmd": ["rclone", "moveto", remote_path(args.remote, old_entry), remote_path(args.remote, new_entry)],
        },
    ]
    if args.followup != "none":
        payload["operations"].append({
            "type": f"bisync_{args.followup}",
            "cmd": [
                sys.executable,
                str(ROOT_DIR / "tools" / "sync_bisync.py"),
                args.followup,
                "--apply",
                "--filter-path",
                bisync_filter_path,
            ],
        })
    return payload


def cmd_plan(args: argparse.Namespace) -> dict:
    return preflight(args)


def cmd_apply(args: argparse.Namespace) -> dict:
    payload = preflight(args)
    payload["dry_run"] = False
    if payload["error"]:
        append_json_log(var_path("rename.log"), {
            "schema": SCHEMA,
            "timestamp": datetime.now().isoformat(),
            "action": "apply",
            "status": "blocked",
            "old": payload["old"],
            "new": payload["new"],
            "error": payload["error"],
        })
        return payload

    old_entry = normalize_entry(args.old)
    new_entry = normalize_entry(args.new)
    local_old = local_path(args.local_root, old_entry)
    local_new = local_path(args.local_root, new_entry)
    result_summary: Dict[str, Any] = {
        "local_mv": None,
        "remote_moveto": None,
        "bisync": None,
        "rollback": None,
    }
    start = time.time()

    try:
        os.rename(local_old, local_new)
        result_summary["local_mv"] = {"ok": True}
    except OSError as exc:
        payload["error"] = f"Local rename failed: {exc}"
        result_summary["local_mv"] = {"ok": False, "error": str(exc)}
        payload["results"] = result_summary
        return payload

    move_start = time.time()
    move_result = rclone_moveto(args.remote, old_entry, new_entry, args.local_root)
    result_summary["remote_moveto"] = {
        "cmd": move_result.cmd,
        "returncode": move_result.returncode,
        "stdout": move_result.stdout,
        "stderr": move_result.stderr,
        "duration_sec": round(time.time() - move_start, 3),
    }
    if move_result.returncode != 0:
        payload["error"] = f"Remote rclone moveto failed (returncode={move_result.returncode})"
        try:
            if os.path.exists(local_new) and not os.path.exists(local_old):
                os.rename(local_new, local_old)
                result_summary["rollback"] = {"local_mv": "ok"}
            else:
                result_summary["rollback"] = {"local_mv": "skipped"}
        except OSError as exc:
            result_summary["rollback"] = {"local_mv": "failed", "error": str(exc)}
        payload["results"] = result_summary
        append_json_log(var_path("rename.log"), log_record(payload, "error", start))
        return payload

    bisync_result = run_followup_bisync(args)
    if bisync_result is None:
        result_summary["bisync"] = {"skipped": True}
    else:
        result_summary["bisync"] = {
            "cmd": bisync_result.cmd,
            "returncode": bisync_result.returncode,
            "stdout": bisync_result.stdout,
            "stderr": bisync_result.stderr,
        }
    if bisync_result is not None and (
        bisync_result.returncode != 0 or '"success": false' in bisync_result.stdout
    ):
        payload["error"] = (
            "Rename succeeded locally and remotely, but follow-up bisync baseline refresh failed. "
            "Run `python3 tools/sync_bisync.py status` and recover/resync before more changes."
        )

    payload["results"] = result_summary
    payload["duration_sec"] = round(time.time() - start, 3)
    append_json_log(var_path("rename.log"), log_record(payload, "error" if payload["error"] else "ok", start))
    return payload


def log_record(payload: dict, status: str, start: float) -> dict:
    def compact_result(result: Optional[dict]) -> Optional[dict]:
        if not result:
            return result
        compact: Dict[str, Any] = {}
        if "local_mv" in result:
            compact["local_mv"] = result["local_mv"]
        remote = result.get("remote_moveto")
        if remote:
            compact["remote_moveto"] = {
                "cmd": remote.get("cmd"),
                "returncode": remote.get("returncode"),
                "duration_sec": remote.get("duration_sec"),
                "stderr": remote.get("stderr"),
            }
        bisync = result.get("bisync")
        if bisync:
            compact["bisync"] = {
                "cmd": bisync.get("cmd"),
                "returncode": bisync.get("returncode"),
                "skipped": bisync.get("skipped"),
                "stderr": bisync.get("stderr"),
            }
        if "rollback" in result:
            compact["rollback"] = result["rollback"]
        return compact

    return {
        "schema": SCHEMA,
        "timestamp": datetime.now().isoformat(),
        "action": "apply",
        "status": status,
        "old": payload.get("old"),
        "new": payload.get("new"),
        "duration_sec": round(time.time() - start, 3),
        "error": payload.get("error"),
        "results": compact_result(payload.get("results")),
    }


def cmd_status(args: argparse.Namespace) -> dict:
    log_path = var_path("rename.log")
    tail: List[dict] = []
    if os.path.exists(log_path):
        with open(log_path, "r") as handle:
            lines = handle.readlines()[-10:]
        for line in lines:
            try:
                tail.append(json.loads(line))
            except ValueError:
                tail.append({"raw": line.rstrip("\n")})

    policy = None
    try:
        policy = load_policy(args.policy_path)
    except Exception:
        policy = None

    return {
        "schema": SCHEMA,
        "action": "status",
        "dry_run": True,
        "local_root": args.local_root,
        "remote": args.remote,
        "policy_path": args.policy_path,
        "bisync_filter_path": args.bisync_filter_path,
        "bisync_filter_hash": filter_file_hash(args.bisync_filter_path),
        "bisync_state": load_bisync_state(),
        "policy_paths": policy.get("paths", {}) if policy else None,
        "log_path": log_path,
        "log_tail": tail,
        "warnings": [],
        "error": None,
    }


def render(payload: dict, fmt: str) -> None:
    success = payload.get("error") is None
    if fmt == "json":
        print(json.dumps({"success": success, "data": payload}, ensure_ascii=False, indent=2))
        return

    print(f"schema: {SCHEMA}")
    print(f"action: {payload.get('action')}")
    print(f"dry_run: {payload.get('dry_run')}")
    if payload.get("error"):
        print(f"error: {payload['error']}")
    if payload.get("old"):
        print(f"old: {payload['old']}")
        print(f"new: {payload['new']}")
    if payload.get("checks"):
        print("checks:")
        for item in payload["checks"]:
            marker = "ok" if item["ok"] else "FAIL"
            detail = f" - {item['detail']}" if item.get("detail") else ""
            print(f"- {marker} {item['name']}{detail}")
    if payload.get("operations"):
        print("operations:")
        for op in payload["operations"]:
            if op["type"] == "local_mv":
                print(f"- local mv: {op['from']} -> {op['to']}")
            elif op["type"] == "remote_moveto":
                print(f"- remote moveto: {' '.join(op['cmd'])}")
            elif op["type"].startswith("bisync_"):
                print(f"- bisync follow-up: {' '.join(op['cmd'])}")
    if payload.get("results"):
        print("results:")
        print(json.dumps(payload["results"], ensure_ascii=False, indent=2))
    if payload.get("duration_sec") is not None:
        print(f"duration_sec: {payload['duration_sec']}")
    if payload.get("bisync_state") is not None:
        print(f"bisync_state: {json.dumps(payload['bisync_state'], ensure_ascii=False)}")
    if payload.get("log_tail"):
        print("recent rename log:")
        for item in payload["log_tail"]:
            line = json.dumps(item, ensure_ascii=False)
            if len(line) > 1000:
                line = line[:1000] + "... <truncated>"
            print(line)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Policy-safe fast rename/move using rclone moveto"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common_flags(sub):
        sub.add_argument("--db-path", default="monitor.db", help="Path to SQLite DB")
        sub.add_argument("--format", choices=["json", "text"], default="text")
        sub.add_argument("--local-root", default=DEFAULT_CONFIG["local_root"])
        sub.add_argument("--remote", default=DEFAULT_CONFIG["rclone_remote"])
        sub.add_argument("--policy-path", default=default_policy_path())
        sub.add_argument("--bisync-filter-path", default=None)

    def rename_flags(sub):
        common_flags(sub)
        sub.add_argument("--old", required=True)
        sub.add_argument("--new", required=True)
        sub.add_argument("--max-delete", type=int, default=20)
        sub.add_argument("--check-access", action=argparse.BooleanOptionalAction, default=True)
        sub.add_argument("--notify-on-error", action=argparse.BooleanOptionalAction, default=True)
        sub.add_argument(
            "--followup",
            choices=["resync", "run", "none"],
            default="resync",
            help=(
                "Post-move bisync action. Default is resync because local+remote "
                "moveto changes both sides outside bisync's previous baseline."
            ),
        )

    plan_parser = subparsers.add_parser("plan", help="Validate and print the rename plan")
    rename_flags(plan_parser)

    apply_parser = subparsers.add_parser("apply", help="Apply local mv + remote rclone moveto + bisync resync")
    rename_flags(apply_parser)

    status_parser = subparsers.add_parser("status", help="Show recent rename log and bisync state")
    common_flags(status_parser)

    args = parser.parse_args()
    if args.bisync_filter_path is None:
        args.bisync_filter_path = default_bisync_filter_path(args.local_root)
    return args


def main() -> None:
    args = parse_args()
    if args.command == "plan":
        payload = cmd_plan(args)
    elif args.command == "apply":
        payload = cmd_apply(args)
    else:
        payload = cmd_status(args)
    render(payload, args.format)


if __name__ == "__main__":
    main()
