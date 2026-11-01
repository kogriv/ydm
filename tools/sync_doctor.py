#!/usr/bin/env python3
"""One read-only summary of a machine's sync, behind `sync_bisync.py doctor`.

Before this, the state of a machine took a dozen commands, and an agent assembled
them from memory. Nothing here writes anything, and nothing touches the network
unless `--check-network` asks. Every check says what it found, and a check that
is not `ok` says what to do about it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from datetime import datetime
from typing import Dict, List, Optional

from tools.sync_backends import daemon_conflict_error
from tools.sync_bisync import (
    CHECK_ACCESS_FILENAME,
    _pause_path,
    _runtime_paths,
    default_bisync_filter_path,
    default_policy_path,
    lock_holder,
    load_pause,
)
from tools.sync_common import (
    BISYNC_RESILIENCE_SINCE,
    filter_file_hash,
    load_bisync_state,
    proxy_variables,
    rclone_bin,
    rclone_version,
    run_command,
    strip_ansi,
)
from tools.sync_policy import expected_bisync_filter, load_policy

OK, WARN, FAIL, SKIPPED = "ok", "warn", "fail", "skipped"
STALE_RUN_HOURS = 3
TIMER_UNIT = "ydm-bisync"


def check(name: str, status: str, detail: str, next_step: Optional[str] = None) -> Dict[str, str]:
    row = {"name": name, "status": status, "detail": detail}
    if next_step:
        row["next"] = next_step
    return row


def check_rclone() -> Dict[str, str]:
    binary = rclone_bin()
    version = rclone_version(binary)
    if version is None:
        return check("rclone", FAIL, f"{binary}: not found or not answering",
                     "Install rclone, or set YDM_RCLONE")
    text = ".".join(str(part) for part in version)
    if version < BISYNC_RESILIENCE_SINCE:
        return check("rclone", WARN, f"{binary} is {text}: no --resilient or --recover",
                     "Set YDM_RCLONE to 1.66 or newer")
    return check("rclone", OK, f"{binary} {text}")


def check_proxy() -> Dict[str, str]:
    found = proxy_variables()
    if found:
        return check("proxy", WARN, f"{', '.join(found)} set in this shell; the timer drops them",
                     "Pass --no-proxy to manual runs")
    return check("proxy", OK, "no proxy variables")


def check_filter(policy: dict, filter_path: str) -> Dict[str, str]:
    if not os.path.exists(filter_path):
        return check("filter", FAIL, f"{filter_path} does not exist",
                     "sync_policy.py render-filters --backend rclone --apply")
    with open(filter_path, encoding="utf-8") as handle:
        actual = handle.read()
    if actual.strip() != expected_bisync_filter(policy).strip():
        return check("filter", WARN, "the filter file is not what the policy renders to",
                     "sync_policy.py render-filters --backend rclone --apply")
    return check("filter", OK, "matches the policy")


def check_baseline(state: dict, filter_path: str) -> Dict[str, str]:
    recorded = state.get("last_resync_filter_hash")
    if not recorded:
        return check("baseline", FAIL, "no successful resync on record", "sync_bisync.py resync --apply")
    if state.get("rclone_wants_resync"):
        return check("baseline", FAIL, "rclone lost its listings", "sync_bisync.py resync --apply")
    if filter_file_hash(filter_path) != recorded:
        return check("baseline", FAIL, "the filter changed since the last resync",
                     "sync_bisync.py resync --apply")
    return check("baseline", OK, f"resync at {state.get('last_resync_at')}")


def check_last_run(state: dict) -> Dict[str, str]:
    status, when = state.get("last_status"), state.get("last_run_at")
    if not when:
        return check("last_run", WARN, "no run on record", "sync_bisync.py run --apply")
    if status != "ok":
        return check("last_run", FAIL, f"{status} at {when}", "See var/bisync_last.log")
    try:
        hours = (datetime.now() - datetime.fromisoformat(when)).total_seconds() / 3600
    except ValueError:
        return check("last_run", WARN, f"unreadable time: {when}")
    if hours > STALE_RUN_HOURS:
        return check("last_run", WARN, f"ok, but {hours:.1f} hours ago",
                     "Is the timer or the scheduled job running?")
    return check("last_run", OK, f"ok at {when}")


def check_pause(args: argparse.Namespace) -> Dict[str, str]:
    pause = load_pause(_pause_path(args))
    if pause:
        return check("pause", WARN, f"paused since {pause.get('since')}: {pause.get('reason')}",
                     "sync_bisync.py resume --apply, once the reason is gone")
    return check("pause", OK, "not paused")


def check_lock(args: argparse.Namespace) -> Dict[str, str]:
    holder = lock_holder(_runtime_paths(args)[3])
    return check("lock", OK, f"a run is in progress (PID {holder})" if holder else "free")


def check_quarantine(policy: dict) -> Dict[str, str]:
    entries = policy.get("quarantine") or {}
    if not entries:
        return check("quarantine", OK, "empty")
    oldest = min((meta.get("since") or "" for meta in entries.values()), default="")
    return check("quarantine", WARN, f"{len(entries)} files, the oldest since {oldest}",
                 "sync_policy.py quarantine --recheck --backend rclone")


def check_timer() -> Dict[str, str]:
    if shutil.which("systemctl") is None:
        return check("timer", SKIPPED, "no systemd here")
    def ask(*words: str) -> str:
        try:
            return subprocess.run(["systemctl", "--user", *words], capture_output=True, text=True,
                                  timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    timer = ask("is-active", f"{TIMER_UNIT}.timer")
    if not timer:
        return check("timer", SKIPPED, "no user systemd to ask")
    if timer != "active":
        return check("timer", WARN, f"{TIMER_UNIT}.timer is {timer}",
                     "bash tools/linux/install_timer.sh install, if this machine should sync on its own")
    if ask("is-failed", f"{TIMER_UNIT}.service") == "failed":
        return check("timer", FAIL, "active, but the last run failed",
                     f"journalctl --user -u {TIMER_UNIT}.service -n 50")
    return check("timer", OK, "active")


def check_daemon(local_root: str) -> Dict[str, str]:
    conflict = daemon_conflict_error(local_root)
    if conflict:
        return check("daemon", FAIL, conflict, "Stop it: the two must never share a folder")
    return check("daemon", OK, "not running on this mirror")


def check_network(remote: str) -> Dict[str, str]:
    result = run_command([rclone_bin(), "lsf", f"{remote}:", "--max-depth", "1", "--files-only",
                          "--retries", "1"], timeout=60)
    if result.returncode != 0:
        tail = strip_ansi(result.stderr).strip().splitlines()[-1:] or ["no message"]
        return check("network", FAIL, f"cannot list {remote}: {tail[0]}")
    if CHECK_ACCESS_FILENAME not in result.stdout.splitlines():
        return check("network", WARN, f"{CHECK_ACCESS_FILENAME} is not in the cloud",
                     "See *When something fails* in AGENTS.md")
    return check("network", OK, f"{remote}: listed, {CHECK_ACCESS_FILENAME} present")


def cmd_doctor(args: argparse.Namespace) -> dict:
    filter_path = args.filter_path or default_bisync_filter_path(args.local_root)
    policy_path = getattr(args, "policy_path", None) or default_policy_path()
    state = load_bisync_state(_runtime_paths(args)[0])
    checks: List[Dict[str, str]] = [check_rclone(), check_proxy()]
    policy = load_policy(policy_path)
    if policy:
        paths = policy.get("paths", {})
        checks.append(check("policy", OK, f"{len(paths)} paths"))
        checks += [check_filter(policy, filter_path), check_quarantine(policy)]
    else:
        checks.append(check("policy", FAIL, f"not found: {policy_path}",
                            "sync_policy.py add --path /Folder --backend rclone --apply"))
    checks += [check_baseline(state, filter_path), check_last_run(state), check_pause(args),
               check_lock(args), check_timer(), check_daemon(args.local_root)]
    checks.append(check_network(args.remote) if args.check_network
                  else check("network", SKIPPED, "not checked; --check-network does"))
    failed = [row["name"] for row in checks if row["status"] == FAIL]
    return {"schema": "ydm_doctor:v1", "action": "doctor", "dry_run": True,
            "local_root": args.local_root, "remote": args.remote, "healthy": not failed,
            "failed": failed, "warned": [row["name"] for row in checks if row["status"] == WARN],
            "checks": checks,
            "error": f"{len(failed)} checks failed: {', '.join(failed)}" if failed else None}
