#!/usr/bin/env python3
"""
Bidirectional sync via `rclone bisync` for --backend rclone
(tasks/rclone_backend/README.md).

Unlike tools/sync_filters.py (one-way, download-only `rclone copy`), this
tool runs `rclone bisync`: new local files get uploaded, and deletions on
either side propagate to the other. Because that's inherently riskier than a
one-way copy, this tool never runs `rclone bisync --resync` on its own —
only the explicit `resync` subcommand does, and `run` refuses to proceed if
the filter-file has changed since the last successful resync (rclone itself
would otherwise just block with an opaque internal error).

`run` also always passes `--check-access` (RCLONE_TEST sentinel, ensured by
`resync`) and an explicit `--max-delete` cap (global rclone flag, default
-1/unlimited in the installed rclone build if omitted) — both exist to catch
the case where /sdcard isn't mounted this session and the local root reads
as empty, which would otherwise look like "delete everything in the cloud."

No interactive prompts — dry-run by default, `--apply` to actually touch
anything. Never calls sys.exit(); errors are conveyed via the JSON
`"success": false` envelope / text `error:` line, matching the rest of this
tool family (tools/sync_filters.py, tools/sync_exclude.py).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.sync_common import (  # noqa: E402
    acquire_lock,
    append_text_log,
    filter_file_hash,
    load_bisync_state,
    load_sync_filters,
    notify,
    rclone_bisync_run,
    release_lock,
    run_local_scan,
    save_bisync_state,
    var_path,
)
from ydm import DEFAULT_CONFIG  # noqa: E402


LOCK_PATH = "/tmp/ydm_bisync.lock"
CHECK_ACCESS_FILENAME = "RCLONE_TEST"
CHECK_ACCESS_CONTENT = "ydm sync_bisync check-access sentinel\n"
POLICY_SCHEMA = "ydm_sync_policy:v1"


def default_policy_path() -> str:
    return var_path("sync_policy.json")


def default_bisync_filter_path(local_root: str) -> str:
    return f"{os.path.expanduser(local_root).rstrip('/')}.bisync.filters"


def download_set_filter_paths(local_root: str) -> tuple:
    """The filters that describe what to *download*, not what to sync back.

    `sync_policy.py render-filters` writes `<root>.download.filters`, and
    `<root>.filters` is what everything used before the policy layer split the
    two. Neither may become a bisync baseline: both can hold paths that are
    materialized locally on purpose and unsafe to send back, which is the
    whole point of the `download_only` mode.
    """
    base = os.path.expanduser(local_root).rstrip("/")
    return (f"{base}.download.filters", f"{base}.filters")


def refuse_download_filter(filter_path: str, local_root: str, action: str) -> str | None:
    """Error text when `filter_path` is a download set, else None."""
    resolved = os.path.expanduser(filter_path)
    if resolved not in download_set_filter_paths(local_root):
        return None
    return (
        f"{filter_path} is a download filter, not the bidirectional one. "
        f"Using it for `{action}` would make a bisync baseline out of paths "
        "that are mirrored locally on purpose and unsafe to send back — see "
        "tasks/rclone_backend/POLICY_AWARE_BISYNC.md. Pass "
        "--filter-path <root>.bisync.filters, or --force-filter if you mean it."
    )


def load_sync_policy(policy_path: str) -> dict | None:
    resolved = os.path.expanduser(policy_path)
    if not os.path.exists(resolved):
        return None
    try:
        with open(resolved, "r") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if payload.get("schema") != POLICY_SCHEMA:
        return None
    return payload


def policy_paths_by_mode(policy: dict | None, mode: str) -> List[str]:
    if not policy:
        return []
    return sorted(
        entry
        for entry, meta in policy.get("paths", {}).items()
        if meta.get("mode") == mode
    )


def ensure_check_access_filter(filter_path: str) -> bool:
    """Idempotently ensures a raw `+ /RCLONE_TEST` line precedes the
    catch-all `- **` line. Inserted directly (not via load_sync_filters/
    write_sync_filters, which are folder-oriented `+ /X/**` patterns) since
    RCLONE_TEST is a single top-level file. NOTE: a later
    `sync_filters.py add/remove --apply` regenerates this file purely from
    its folder include_dirs and will drop this line — the filter-hash check
    in `run` will then correctly demand a fresh `resync --apply`, which
    restores it."""
    resolved = os.path.expanduser(filter_path)
    sentinel_line = "+ /RCLONE_TEST\n"
    lines: List[str] = []
    if os.path.exists(resolved):
        with open(resolved, "r") as handle:
            lines = handle.readlines()
    if sentinel_line in lines:
        return False
    insert_at = len(lines)
    for i, line in enumerate(lines):
        if line.strip() == "- **":
            insert_at = i
            break
    lines.insert(insert_at, sentinel_line)
    os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
    with open(resolved, "w") as handle:
        handle.writelines(lines)
    return True


def ensure_check_access_file(local_root: str) -> str:
    path = os.path.join(os.path.expanduser(local_root), CHECK_ACCESS_FILENAME)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as handle:
            handle.write(CHECK_ACCESS_CONTENT)
    return path


def write_last_log(result) -> None:
    """Full captured output of the most recent rclone bisync invocation —
    overwritten each run (debugging aid), distinct from the durable
    append-only var/bisync.log summary."""
    path = var_path("bisync_last.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(
            f"=== {datetime.now().isoformat()} ===\n"
            f"cmd: {' '.join(result.cmd)}\n"
            f"returncode: {result.returncode}\n\n"
            f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}\n"
        )


def cmd_resync(args: argparse.Namespace) -> dict:
    filter_path = args.filter_path or default_bisync_filter_path(args.local_root)
    payload = {
        "schema": "sync_bisync:v1",
        "action": "resync",
        "dry_run": not args.apply,
        "local_root": args.local_root,
        "remote": args.remote,
        "filter_path": filter_path,
        "max_delete": args.max_delete,
        "check_access": args.check_access,
        "lock": None,
        "bisync": None,
        "local_scan": None,
        "state_before": load_bisync_state(),
        "state_after": None,
        "plan_text": [],
        "warnings": [],
        "error": None,
    }

    # Before anything reads the file, let alone runs rclone. `run` is already
    # protected — it compares the filter's hash against the recorded baseline
    # and refuses a mismatch — but `resync` is the command that *writes* the
    # baseline, so it has nothing to compare against. This is the one place a
    # wrong filter turns into a real one.
    if not args.force_filter:
        refusal = refuse_download_filter(filter_path, args.local_root, "resync")
        if refusal:
            payload["error"] = refusal
            return payload

    filters_result = load_sync_filters(filter_path)
    payload["warnings"].extend(filters_result.warnings)
    if not filters_result.include_dirs:
        payload["error"] = (
            "No folders included in the filter-file yet — add at least one "
            "via `tools/sync_filters.py add --path ... --apply` before resync."
        )
        return payload

    preview_cmd = [
        "rclone", "--max-delete", str(args.max_delete), "bisync",
        os.path.expanduser(args.local_root), f"{args.remote}:",
        "--filters-file", filter_path, "--resync",
    ]
    if args.check_access:
        preview_cmd.append("--check-access")
    preview_cmd.append("-v")
    payload["plan_text"] = [
        "Ensure `+ /RCLONE_TEST` filter rule and local sentinel file exist (if --check-access)",
        f"Run: {' '.join(preview_cmd)}",
        "Path1 (local) files may overwrite Path2 (cloud) on this run — review before --apply",
    ]

    if not args.apply:
        return payload

    if args.check_access:
        ensure_check_access_filter(filter_path)
        ensure_check_access_file(args.local_root)

    stream = bool(getattr(args, "stream", False))
    if stream:
        print("Starting rclone bisync --resync (verbose log below)...", flush=True)

    result = rclone_bisync_run(
        args.remote, args.local_root, filter_path,
        resync=True, max_delete=args.max_delete, check_access=args.check_access,
        stream=stream,
    )
    payload["bisync"] = {
        "cmd": result.cmd, "returncode": result.returncode,
        "stdout": result.stdout, "stderr": result.stderr,
    }
    write_last_log(result)

    if result.returncode != 0:
        payload["error"] = f"rclone bisync --resync failed (returncode={result.returncode})"
        append_text_log(var_path("bisync.log"),
                         f"{datetime.now().isoformat()} resync FAILED rc={result.returncode}")
        return payload

    state = load_bisync_state()
    state["last_resync_filter_hash"] = filter_file_hash(filter_path)
    state["last_resync_at"] = datetime.now().isoformat()
    # A resync is the answer to "sync is paused", so finishing one ends that
    # notification. Without this the flag survives until the next successful
    # run, and if the very next run blocked for the same reason in between, the
    # card would be suppressed — the guard silent exactly when it has something
    # to say, which is the shape of issue #5.
    state["last_notified_error"] = None
    state["last_status"] = "ok"
    save_bisync_state(state)
    payload["state_after"] = state

    if stream:
        print("\nUpdating local scan index after resync...", flush=True)
    local_scan = run_local_scan(args.db_path, args.local_root)
    payload["local_scan"] = vars(local_scan)
    if stream and local_scan.duration_sec is not None:
        print(f"Local scan done in {local_scan.duration_sec:.0f}s.", flush=True)

    append_text_log(var_path("bisync.log"), f"{datetime.now().isoformat()} resync OK")
    return payload


#: What a person can actually do about an error, said in terms of the menu they
#: use. The error strings themselves stay as they are: they go into the log and
#: the JSON envelope, where a command name is the right answer.
_NOTICE_FOR_ERROR = (
    ("Filter-file changed since the last resync",
     "Sync is paused: the synced folders changed. Open ydm and choose "
     "6 (Resync baseline)."),
    ("No successful resync on record",
     "Sync has no baseline yet. Open ydm and choose 6 (Resync baseline)."),
)


def human_notice(error: str) -> str:
    for prefix, notice in _NOTICE_FOR_ERROR:
        if error.startswith(prefix):
            return notice
    return error


def notify_once(state: Dict[str, object], title: str, message: str) -> bool:
    """Notify unless this exact thing was the last thing notified.

    The scheduled job runs every half hour, and a blocked run is blocked until a
    person does something about it — so notifying per run means the same message
    arriving forever. On the device that was four identical "Filter-file changed
    since the last resync" cards stacked in the shade overnight, which is how an
    operator learns to swipe ydm's notifications away without reading them, the
    important one included. `job_run.sh` already only notifies for the one verdict
    worth interrupting someone over; this is the same rule for the other half.

    The log still records every run. It is the audit trail, and it should be
    complete; the notification is an interruption, and it should not be.

    Cleared on a successful run, so a problem that comes back after things were
    working is a new interruption rather than a silence.
    """
    if state.get("last_notified_error") == message:
        return False
    state["last_notified_error"] = message
    save_bisync_state(state)
    notify(title, message)
    return True


def cmd_run(args: argparse.Namespace) -> dict:
    filter_path = args.filter_path or default_bisync_filter_path(args.local_root)
    state = load_bisync_state()
    payload = {
        "schema": "sync_bisync:v1",
        "action": "run",
        "dry_run": not args.apply,
        "local_root": args.local_root,
        "remote": args.remote,
        "filter_path": filter_path,
        "max_delete": args.max_delete,
        "check_access": args.check_access,
        "lock": None,
        "bisync": None,
        "local_scan": None,
        "state_before": dict(state),
        "state_after": None,
        "plan_text": [],
        "warnings": [],
        "error": None,
    }

    current_hash = filter_file_hash(filter_path)
    if not state.get("last_resync_filter_hash"):
        payload["error"] = "No successful resync on record — run `sync_bisync.py resync --apply` first."
    elif current_hash != state.get("last_resync_filter_hash"):
        payload["error"] = (
            "Filter-file changed since the last resync — bisync would block "
            "on this internally. Run `sync_bisync.py resync --apply` again "
            "before the next scheduled `run`."
        )

    if payload["error"]:
        if args.apply:
            append_text_log(var_path("bisync.log"),
                             f"{datetime.now().isoformat()} run BLOCKED {payload['error']}")
            if args.notify_on_error:
                notify_once(state, "ydm sync", human_notice(payload["error"]))
        return payload

    preview_cmd = [
        "rclone", "--max-delete", str(args.max_delete), "bisync",
        os.path.expanduser(args.local_root), f"{args.remote}:",
        "--filters-file", filter_path,
    ]
    if args.check_access:
        preview_cmd.append("--check-access")
    payload["plan_text"] = [f"Run: {' '.join(preview_cmd)}"]

    if not args.apply:
        return payload

    lock = acquire_lock(LOCK_PATH)
    payload["lock"] = {"acquired": lock.acquired, "holder_pid": lock.holder_pid}
    if not lock.acquired:
        payload["error"] = (
            f"Another bisync run is already in progress (PID: {lock.holder_pid})."
            if lock.holder_pid else "Failed to acquire bisync lock."
        )
        append_text_log(var_path("bisync.log"),
                         f"{datetime.now().isoformat()} run SKIPPED {payload['error']}")
        return payload

    try:
        stream = bool(getattr(args, "stream", False))
        if stream:
            print("Starting rclone bisync...", flush=True)
        result = rclone_bisync_run(
            args.remote, args.local_root, filter_path,
            resync=False, max_delete=args.max_delete, check_access=args.check_access,
            stream=stream,
        )
        payload["bisync"] = {
            "cmd": result.cmd, "returncode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
        }
        write_last_log(result)

        if result.returncode != 0:
            payload["error"] = f"rclone bisync failed (returncode={result.returncode})"
            state["last_run_at"] = datetime.now().isoformat()
            state["last_status"] = "error"
            save_bisync_state(state)
            payload["state_after"] = dict(state)
            append_text_log(var_path("bisync.log"),
                             f"{datetime.now().isoformat()} run FAILED rc={result.returncode}")
            if args.notify_on_error:
                notify_once(state, "ydm sync", human_notice(payload["error"]))
            return payload

        local_scan = run_local_scan(args.db_path, args.local_root)
        payload["local_scan"] = vars(local_scan)

        state["last_run_at"] = datetime.now().isoformat()
        state["last_status"] = "ok"
        # A run that worked ends the notification: whatever comes next is news
        # again rather than the same card returning every half hour.
        state["last_notified_error"] = None
        save_bisync_state(state)
        payload["state_after"] = dict(state)
        append_text_log(var_path("bisync.log"), f"{datetime.now().isoformat()} run OK")
        return payload
    finally:
        release_lock(LOCK_PATH)


def cmd_status(args: argparse.Namespace) -> dict:
    filter_path = args.filter_path or default_bisync_filter_path(args.local_root)
    policy_path = getattr(args, "policy_path", None) or default_policy_path()
    policy = load_sync_policy(policy_path)
    policy_bisync_filter_path = default_bisync_filter_path(args.local_root) if policy else None
    state = load_bisync_state()
    current_hash = filter_file_hash(filter_path)
    policy_bisync_hash = filter_file_hash(policy_bisync_filter_path) if policy_bisync_filter_path else None

    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, "r") as handle:
                pid = int(handle.read().strip())
            lock_info = {"held": os.path.exists(f"/proc/{pid}"), "pid": pid}
        except (ValueError, IOError):
            lock_info = {"held": False, "pid": None, "note": "corrupted lock file"}
    else:
        lock_info = {"held": False, "pid": None}

    log_path = var_path("bisync.log")
    log_tail: List[str] = []
    if os.path.exists(log_path):
        with open(log_path, "r") as handle:
            log_tail = [line.rstrip("\n") for line in handle.readlines()[-10:]]

    resync_needed = (
        not state.get("last_resync_filter_hash")
        or current_hash != state.get("last_resync_filter_hash")
    )

    return {
        "schema": "sync_bisync:v1",
        "action": "status",
        "dry_run": True,
        "local_root": args.local_root,
        "remote": args.remote,
        "filter_path": filter_path,
        "state": state,
        "current_filter_hash": current_hash,
        "policy": {
            "path": policy_path,
            "exists": policy is not None,
            "bidirectional_paths": policy_paths_by_mode(policy, "bidirectional"),
            "download_only_paths": policy_paths_by_mode(policy, "download_only"),
            "disabled_paths": policy_paths_by_mode(policy, "disabled"),
            "bisync_filter_path": policy_bisync_filter_path,
            "bisync_filter_hash": policy_bisync_hash,
            "policy_filter_resync_needed": (
                not state.get("last_resync_filter_hash")
                or policy_bisync_hash != state.get("last_resync_filter_hash")
            ) if policy else None,
        },
        "resync_needed": resync_needed,
        "lock": lock_info,
        "log_tail": log_tail,
        "plan_text": [],
        "warnings": [],
        "error": None,
    }


def render(payload: dict, fmt: str, text_header: bool) -> None:
    success = payload.get("error") is None
    if fmt == "json":
        print(json.dumps({"success": success, "data": payload}, ensure_ascii=False, indent=2))
        return

    if text_header:
        print("schema: sync_bisync:v1")
        if payload.get("error"):
            print(f"error: {payload['error']}")
        print(f"action: {payload['action']}")
        print(f"dry_run: {payload.get('dry_run')}")
        if payload.get("filter_path"):
            print(f"filter_path: {payload['filter_path']}")
        if payload.get("warnings"):
            print("warnings:")
            for warning in payload["warnings"]:
                print(f"- {warning}")
        print("")

    if payload.get("error") and not text_header:
        print(f"ERROR: {payload['error']}")
    if payload.get("plan_text"):
        print("Plan:")
        for line in payload["plan_text"]:
            print(f"- {line}")
    if payload.get("lock") is not None:
        print(f"lock: {payload['lock']}")
    if payload.get("bisync") is not None:
        print(f"rclone bisync: returncode={payload['bisync']['returncode']}")
        if payload["bisync"]["stderr"]:
            print(payload["bisync"]["stderr"])
    if payload.get("local_scan") is not None:
        print(f"local scan: {payload['local_scan']}")
    if payload.get("state") is not None:
        print(f"state: {json.dumps(payload['state'], ensure_ascii=False)}")
    if payload.get("policy") is not None:
        policy = payload["policy"]
        print(f"policy: {policy['path']} exists={policy['exists']}")
        if policy["exists"]:
            print(f"policy bidirectional: {', '.join(policy['bidirectional_paths'])}")
            print(f"policy download_only: {', '.join(policy['download_only_paths'])}")
            print(f"policy disabled: {', '.join(policy['disabled_paths'])}")
            print(f"policy bisync_filter_path: {policy['bisync_filter_path']}")
            print(f"policy bisync_filter_hash: {policy['bisync_filter_hash']}")
            print(f"policy_filter_resync_needed: {policy['policy_filter_resync_needed']}")
    if payload.get("state_after") is not None:
        print(f"state_after: {json.dumps(payload['state_after'], ensure_ascii=False)}")
    if payload.get("resync_needed") is not None:
        print(f"resync_needed: {payload['resync_needed']}")
    if payload.get("log_tail"):
        print("recent log:")
        for line in payload["log_tail"]:
            print(f"  {line}")


def build_parser() -> argparse.ArgumentParser:
    """The CLI, without parsing anything yet.

    Separate from `parse_args` so a caller that reaches `cmd_resync` and friends
    directly can take its defaults from here instead of restating them.
    `tools/ydm_menu_actions._bisync_ns` used to restate them, drifted, and the
    menu crashed with `Namespace has no attribute 'force_filter'` the first time
    someone accepted the resync it offers. `parse_args([command])` gives every
    option that command defines, because none of them is required.
    """
    parser = argparse.ArgumentParser(
        description="Bidirectional sync via rclone bisync (--backend rclone)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common_flags(sub):
        sub.add_argument("--db-path", default="monitor.db", help="Path to SQLite DB")
        sub.add_argument("--format", choices=["json", "text"], default="json")
        sub.add_argument("--text-header", action=argparse.BooleanOptionalAction, default=True)
        sub.add_argument("--local-root", default=DEFAULT_CONFIG["local_root"])
        sub.add_argument(
            "--filter-path", default=None,
            help="Defaults to <local-root>.bisync.filters — the policy-generated "
                 "bidirectional set, not <local-root>.download.filters",
        )
        sub.add_argument("--remote", default=DEFAULT_CONFIG["rclone_remote"])
        sub.add_argument("--policy-path", default=default_policy_path())

    resync_parser = subparsers.add_parser(
        "resync", help="Establish/re-establish the bisync baseline (Path1 may overwrite Path2)"
    )
    common_flags(resync_parser)
    resync_parser.add_argument("--apply", action="store_true")
    resync_parser.add_argument("--max-delete", type=int, default=20)
    resync_parser.add_argument("--check-access", action=argparse.BooleanOptionalAction, default=True)
    resync_parser.add_argument(
        "--force-filter", action="store_true",
        help="Establish the baseline even from a download filter. You are "
             "saying the download-only paths are safe to send back.",
    )

    run_parser = subparsers.add_parser(
        "run", help="Run a normal (non-resync) bisync pass — this is what the scheduled job calls"
    )
    common_flags(run_parser)
    run_parser.add_argument("--apply", action="store_true")
    run_parser.add_argument("--max-delete", type=int, default=20)
    run_parser.add_argument("--check-access", action=argparse.BooleanOptionalAction, default=True)
    run_parser.add_argument("--notify-on-error", action=argparse.BooleanOptionalAction, default=True)

    status_parser = subparsers.add_parser(
        "status", help="Show bisync state, lock status, and recent log lines (read-only)"
    )
    common_flags(status_parser)

    return parser


def parse_args() -> argparse.Namespace:
    return build_parser().parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "resync":
        payload = cmd_resync(args)
    elif args.command == "run":
        payload = cmd_run(args)
    else:
        payload = cmd_status(args)
    render(payload, args.format, args.text_header)


if __name__ == "__main__":
    main()
