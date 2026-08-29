#!/usr/bin/env python3
"""
Fast policy-safe rename/move path for the rclone backend.

`rclone bisync` treats a local rename as delete+upload. This tool performs an
explicit local rename plus server-side `rclone moveto`, then refreshes the
bisync baseline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
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
    normalized_local_root,
    run_command,
    run_local_scan,
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
CANDIDATE_SCHEMA = "ydm_rename_candidate:v1"
POLICY_SCHEMA = "ydm_rename_policy:v1"
PREFLIGHT_SCHEMA = "ydm_rename_preflight:v1"
VALID_PREFLIGHT_MODES = {"observe", "guard", "auto"}


def default_bisync_filter_path(local_root: str) -> str:
    return f"{os.path.expanduser(local_root).rstrip('/')}.bisync.filters"


def default_rename_policy_path() -> str:
    return var_path("rename_policy.json")


def append_json_log(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", buffering=1) as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def write_json_file(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def default_rename_policy() -> dict:
    return {
        "schema": POLICY_SCHEMA,
        "default_mode": "observe",
        "max_auto_candidates": 10,
        "roots": {},
    }


def load_rename_policy(path: str) -> dict:
    resolved = os.path.expanduser(path)
    if not os.path.exists(resolved):
        return default_rename_policy()
    with open(resolved, "r") as handle:
        payload = json.load(handle)
    if payload.get("schema") != POLICY_SCHEMA:
        raise ValueError(f"Unsupported rename policy schema: {payload.get('schema')}")
    policy = default_rename_policy()
    policy.update(payload)
    policy["roots"] = payload.get("roots") or {}
    if policy.get("default_mode") not in VALID_PREFLIGHT_MODES:
        policy["default_mode"] = "observe"
    try:
        policy["max_auto_candidates"] = int(policy.get("max_auto_candidates", 10))
    except (TypeError, ValueError):
        policy["max_auto_candidates"] = 10
    return policy


def write_rename_policy(path: str, policy: dict) -> None:
    resolved = os.path.expanduser(path)
    os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
    if os.path.exists(resolved):
        backup = f"{resolved}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        with open(resolved, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
    write_json_file(resolved, policy)


def mode_for_root(rename_policy: dict, root: Optional[str]) -> str:
    mode = rename_policy.get("default_mode", "observe")
    if root and root in (rename_policy.get("roots") or {}):
        mode = (rename_policy["roots"].get(root) or {}).get("mode", mode)
    return mode if mode in VALID_PREFLIGHT_MODES else "observe"


def notify_termux(title: str, message: str) -> bool:
    candidates = [
        "/data/data/com.termux/files/usr/bin/termux-notification",
        "termux-notification",
    ]
    for binary in candidates:
        try:
            result = subprocess.run(
                [binary, "--title", title, "--content", message],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return True
        except OSError:
            continue
    return False


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


def rclone_lsjson_hash(remote: str, entry: str, local_root: str) -> CommandResult:
    cmd = ["rclone", "lsjson", remote_path(remote, entry), "--max-depth", "1", "--hash"]
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


def remote_info(remote: str, entry: str, local_root: str) -> dict:
    result = rclone_lsjson_hash(remote, entry, local_root)
    parsed: Any = None
    if result.stdout:
        try:
            parsed = json.loads(result.stdout)
        except ValueError:
            parsed = None
    item = None
    if isinstance(parsed, list) and parsed:
        item = parsed[0]
    hashes = (item or {}).get("Hashes") or {}
    return {
        "exists": result.returncode == 0,
        "cmd": result.cmd,
        "returncode": result.returncode,
        "stderr": result.stderr,
        "item": item,
        "md5": hashes.get("md5"),
    }


def rclone_moveto(remote: str, old_entry: str, new_entry: str, local_root: str) -> CommandResult:
    cmd = ["rclone", "moveto", remote_path(remote, old_entry), remote_path(remote, new_entry)]
    cmd.extend(local_encoding_flags_for_path(local_root))
    return run_command(cmd)


def md5_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def latest_successful_local_scan_id(db_path: str, local_root: Optional[str] = None) -> Optional[int]:
    """Newest successful local scan of `local_root`, or None if there is none.

    The baseline has to describe the *same tree* as the scan it will be diffed
    against. It did not used to: the query took the newest local scan whatever
    it covered, so one preflight run against a different `--local-root` left a
    scan of another directory sitting at the top of the table, and the next run
    read the difference between two unrelated trees as renames. On 2026-08-28
    that produced two `blocked` candidates pointing at `/RCLONE_TEST` — the
    bisync sentinel file, matched by size — and stopped the scheduled sync.

    A scan with `scan_root IS NULL` predates the column being written for local
    scans and cannot be attributed to any mirror, so it is not a candidate
    either. That makes the first run after this change find no baseline, which
    the caller answers by skipping one bisync cycle rather than guessing; the
    scan that run takes is attributed, so the next run has one.

    Failing to *read* the database is not the same as finding no baseline, and
    raises rather than returning None. Both end in a skipped cycle, but they
    send the reader of `var/bisync.log` to different places, and the log line
    exists to be read.
    """
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    try:
        # A database old enough to lack the column cannot answer the question,
        # and that is the only unreadable state answered with "no baseline". A
        # locked database would otherwise be reported as an unattributed
        # mirror, sending whoever reads the log to check `--local-root` for a
        # fault that is not there.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(scans)")}
        if "scan_root" not in columns:
            return None
        rows = conn.execute(
            """
            SELECT id, scan_root
            FROM scans
            WHERE scan_type = 'local' AND status = 'success'
            ORDER BY id DESC
            """
        ).fetchall()
    finally:
        conn.close()

    if local_root is None:
        return int(rows[0][0]) if rows else None

    wanted = normalized_local_root(local_root)
    for scan_id, scan_root in rows:
        if scan_root is None:
            continue
        if normalized_local_root(str(scan_root)) == wanted:
            return int(scan_id)
    return None


def load_scan_files(db_path: str, scan_id: int) -> Dict[str, dict]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT parent_path, name, type, COALESCE(size, 0), md5, modified
            FROM files
            WHERE scan_id = ?
            """,
            (scan_id,),
        ).fetchall()
    finally:
        conn.close()

    result: Dict[str, dict] = {}
    for parent, name, file_type, size, md5, modified in rows:
        entry = f"{parent.strip('/')}/{name}" if parent else name
        result[entry] = {
            "entry": entry,
            "type": file_type,
            "size": int(size or 0),
            "md5": md5,
            "modified": modified,
        }
    return result


def candidate_id(old_entry: str, new_entry: str, previous_scan_id: int, current_scan_id: int) -> str:
    raw = f"{previous_scan_id}:{current_scan_id}:{old_entry}->{new_entry}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def is_bisync_clean(args: argparse.Namespace) -> Tuple[bool, str]:
    state = load_bisync_state()
    current_hash = filter_file_hash(args.bisync_filter_path)
    if not state.get("last_resync_filter_hash"):
        return False, "no_successful_resync"
    if current_hash != state.get("last_resync_filter_hash"):
        return False, "filter_hash_changed"
    if os.path.exists("/tmp/ydm_bisync.lock"):
        return False, "bisync_lock_held"
    if state.get("last_status") not in {"ok", None}:
        return False, f"last_status_{state.get('last_status')}"
    return True, "clean"


def find_candidates(
    args: argparse.Namespace,
    previous_scan_id: int,
    current_scan_id: int,
    rename_policy: Optional[dict] = None,
) -> Tuple[List[dict], List[str]]:
    warnings: List[str] = []
    policy = load_policy(args.policy_path)
    if not policy:
        return [], [f"Policy not found: {args.policy_path}"]

    previous = load_scan_files(args.db_path, previous_scan_id)
    current = load_scan_files(args.db_path, current_scan_id)

    deleted = [item for entry, item in previous.items() if entry not in current and item["type"] == "file"]
    created = [item for entry, item in current.items() if entry not in previous and item["type"] == "file"]

    by_key: Dict[Tuple[str, int], dict] = {}
    for item in deleted:
        root, mode = path_mode(policy, item["entry"])
        by_key.setdefault((root or "", item["size"]), {"deleted": [], "created": [], "mode": mode})
        by_key[(root or "", item["size"])]["deleted"].append(item)
    for item in created:
        root, mode = path_mode(policy, item["entry"])
        by_key.setdefault((root or "", item["size"]), {"deleted": [], "created": [], "mode": mode})
        by_key[(root or "", item["size"])]["created"].append(item)

    bisync_clean, bisync_reason = is_bisync_clean(args)
    candidates: List[dict] = []

    for (root, size), group in sorted(by_key.items()):
        if not group["deleted"] or not group["created"]:
            continue
        ambiguous = len(group["deleted"]) != 1 or len(group["created"]) != 1
        for old_item in group["deleted"]:
            for new_item in group["created"]:
                old_entry = old_item["entry"]
                new_entry = new_item["entry"]
                old_root, old_mode = path_mode(policy, old_entry)
                new_root, new_mode = path_mode(policy, new_entry)
                reasons = ["same_size", "old_missing_local", "new_present_local"]
                blockers: List[str] = []
                confidence = "review"

                if not old_root or not new_root:
                    blockers.append("outside_policy")
                elif old_root != new_root:
                    blockers.append("different_policy_roots")
                elif old_mode != "bidirectional" or new_mode != "bidirectional":
                    blockers.append(f"policy_{old_mode}_to_{new_mode}")

                if ambiguous:
                    blockers.append("ambiguous_same_size_candidates")
                if not bisync_clean:
                    blockers.append(f"bisync_{bisync_reason}")

                remote_old = remote_info(args.remote, old_entry, args.local_root)
                remote_new = remote_exists(args.remote, new_entry, args.local_root)
                if remote_old["exists"]:
                    reasons.append("remote_old_exists")
                else:
                    blockers.append("remote_old_missing")
                if not remote_new["exists"]:
                    reasons.append("remote_new_missing")
                else:
                    blockers.append("remote_new_exists")

                local_hash = None
                remote_md5 = remote_old.get("md5")
                if remote_md5 and not blockers:
                    try:
                        local_hash = md5_file(local_path(args.local_root, new_entry))
                        if local_hash == remote_md5:
                            reasons.append("md5_match")
                            confidence = "high"
                        else:
                            blockers.append("md5_mismatch")
                    except OSError as exc:
                        blockers.append(f"local_hash_failed:{exc}")
                elif not remote_md5:
                    reasons.append("remote_md5_missing")

                if blockers:
                    confidence = "blocked" if any(
                        item.startswith("policy_")
                        or item in {
                            "outside_policy",
                            "different_policy_roots",
                            "remote_new_exists",
                            "remote_old_missing",
                            "md5_mismatch",
                        }
                        for item in blockers
                    ) else "review"

                root_mode = mode_for_root(rename_policy or default_rename_policy(), old_root)
                if root_mode == "auto" and old_mode != "bidirectional":
                    root_mode = "guard"
                candidates.append({
                    "schema": CANDIDATE_SCHEMA,
                    "id": candidate_id(old_entry, new_entry, previous_scan_id, current_scan_id),
                    "old": f"/{old_entry}",
                    "new": f"/{new_entry}",
                    "policy_root": old_root,
                    "mode": old_mode,
                    "preflight_mode": root_mode,
                    "size": size,
                    "confidence": confidence,
                    "reason": reasons,
                    "blockers": blockers,
                    "requires_review": confidence != "high",
                    "local_md5": local_hash,
                    "remote_md5": remote_md5,
                    "previous_scan_id": previous_scan_id,
                    "current_scan_id": current_scan_id,
                    "created_at": datetime.now().isoformat(),
                })

    return candidates, warnings


def summarize_candidates(candidates: List[dict]) -> dict:
    return {
        "total": len(candidates),
        "high": sum(1 for item in candidates if item.get("confidence") == "high"),
        "review": sum(1 for item in candidates if item.get("confidence") == "review"),
        "blocked": sum(1 for item in candidates if item.get("confidence") == "blocked"),
    }


def detect_decision_reason(detect_payload: dict) -> Optional[str]:
    decision = detect_payload.get("decision")
    if not isinstance(decision, dict):
        return None
    return decision.get("reason")


# Verdicts that mean "the guard had nothing to compare", as opposed to "the
# guard compared and found nothing". Both produce an empty candidate list, so
# `cmd_preflight` — which recomputes from that list — has to carry them across
# by name or it would collapse them into `allow_bisync (no_candidates)`.
NO_BASELINE_REASONS = frozenset({"no_comparable_baseline", "baseline_unreadable"})


def decide_no_baseline(mode: str, reason: str = "no_comparable_baseline") -> dict:
    """No local scan of this mirror to compare against: skip, do not guess.

    Renames are undetectable without a baseline, so the guard has nothing to
    say. The two honest answers are "run bisync unguarded" and "do not run
    bisync", and only the second is safe: an unguarded run is exactly the
    situation the guard exists to prevent, where a cloud-side rename reaches
    rclone as a deletion and an upload.

    Skipping cannot wedge the sync. `cmd_detect` takes its own local scan
    before this is reached, and that scan is attributed to this mirror, so the
    next run has a baseline — the cost is one cycle. `observe` mode still
    allows, because it is defined as not interfering.

    `reason` separates the two ways of having no baseline: none was recorded
    for this mirror, or the database would not answer. The decision is the
    same and the diagnosis is not.
    """
    summary = summarize_candidates([])
    summary["applied"] = 0
    if mode == "observe":
        return {
            "decision": "allow_bisync",
            "reason": "observe_mode",
            "notify": False,
            "summary": summary,
        }
    return {
        "decision": "skip_bisync",
        "reason": reason,
        "notify": False,
        "summary": summary,
    }


def decide_preflight(mode: str, candidates: List[dict], applied: int = 0, error: Optional[str] = None) -> dict:
    summary = summarize_candidates(candidates)
    summary["applied"] = applied
    if error:
        return {
            "decision": "allow_bisync",
            "reason": "error",
            "notify": mode in {"guard", "auto"},
            "summary": summary,
        }
    if mode == "observe":
        return {"decision": "allow_bisync", "reason": "observe_mode", "notify": False, "summary": summary}
    if summary["review"] or summary["blocked"]:
        return {
            "decision": "block_bisync",
            "reason": "ambiguous_candidates",
            "notify": True,
            "summary": summary,
        }
    if applied:
        return {"decision": "skip_bisync", "reason": "auto_applied", "notify": True, "summary": summary}
    if summary["high"]:
        return {"decision": "allow_bisync", "reason": "high_only", "notify": False, "summary": summary}
    return {"decision": "allow_bisync", "reason": "no_candidates", "notify": False, "summary": summary}


def validate_auto_candidates(candidates: List[dict], args: argparse.Namespace, rename_policy: dict) -> Tuple[List[dict], Optional[str]]:
    high = [item for item in candidates if item.get("confidence") == "high"]
    max_auto = int(rename_policy.get("max_auto_candidates", 10))
    if len(high) > max_auto:
        return [], f"Too many auto candidates: {len(high)} > {max_auto}"
    targets = set()
    for item in high:
        if item.get("preflight_mode") != "auto":
            return [], f"Candidate is not in auto mode: {item.get('id')}"
        if item.get("mode") != "bidirectional":
            return [], f"Candidate is not bidirectional: {item.get('id')}"
        target = item.get("new")
        if target in targets:
            return [], f"Duplicate target candidate: {target}"
        targets.add(target)
        if not os.path.exists(local_path(args.local_root, normalize_entry(item["new"]))):
            return [], f"Local renamed file not found: {item.get('new')}"
        remote_old = remote_exists(args.remote, normalize_entry(item["old"]), args.local_root)
        remote_new = remote_exists(args.remote, normalize_entry(item["new"]), args.local_root)
        if not remote_old["exists"]:
            return [], f"Remote source not found: {item.get('old')}"
        if remote_new["exists"]:
            return [], f"Remote target already exists: {item.get('new')}"
    return high, None


def apply_auto_candidates(candidates: List[dict], args: argparse.Namespace, rename_policy: dict) -> Tuple[dict, Optional[str]]:
    selected, error = validate_auto_candidates(candidates, args, rename_policy)
    result: Dict[str, Any] = {
        "selected": [item.get("id") for item in selected],
        "moves": [],
        "bisync": None,
    }
    if error:
        return result, error
    for item in selected:
        old_entry = normalize_entry(item["old"])
        new_entry = normalize_entry(item["new"])
        start = time.time()
        move = rclone_moveto(args.remote, old_entry, new_entry, args.local_root)
        move_payload = {
            "candidate_id": item.get("id"),
            "old": item.get("old"),
            "new": item.get("new"),
            "cmd": move.cmd,
            "returncode": move.returncode,
            "stdout": move.stdout,
            "stderr": move.stderr,
            "duration_sec": round(time.time() - start, 3),
        }
        result["moves"].append(move_payload)
        if move.returncode != 0:
            return result, f"Remote rclone moveto failed for {item.get('id')} (returncode={move.returncode})"

    if selected:
        followup_args = argparse.Namespace(**vars(args))
        followup_args.followup = "resync"
        bisync = run_followup_bisync(followup_args)
        result["bisync"] = None if bisync is None else {
            "cmd": bisync.cmd,
            "returncode": bisync.returncode,
            "stdout": bisync.stdout,
            "stderr": bisync.stderr,
        }
        if bisync is None or bisync.returncode != 0 or '"success": false' in bisync.stdout:
            return result, "Follow-up bisync resync failed after auto moves"
    return result, None


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


def cmd_detect(args: argparse.Namespace) -> dict:
    rename_policy = load_rename_policy(args.rename_policy_path)
    requested_mode = args.mode or rename_policy.get("default_mode", "observe")
    # A database that will not answer leaves the guard with nothing to compare,
    # exactly as an unscanned mirror does, and gets the same skip. It does not
    # get the same name: `no_comparable_baseline` reads as "wrong --local-root"
    # and would send the operator looking for a fault that is not there. The
    # scan below still runs, and normally succeeds: the database is WAL, so an
    # ordinary writer never gets here at all — reads are not blocked by one, as
    # 5.5 s of `BEGIN EXCLUSIVE` confirmed on the device (issue #18). Reaching
    # this branch takes VACUUM or a checkpoint, and by the time the scan writes
    # that is usually over.
    baseline_error = None
    try:
        previous_scan_id = latest_successful_local_scan_id(args.db_path, args.local_root)
    except sqlite3.Error as exc:
        previous_scan_id = None
        baseline_error = f"{type(exc).__name__}: {exc}"
    payload = {
        "schema": SCHEMA,
        "action": "detect",
        "mode": requested_mode,
        "dry_run": True,
        "local_root": args.local_root,
        "remote": args.remote,
        "policy_path": args.policy_path,
        "rename_policy_path": args.rename_policy_path,
        "rename_policy": rename_policy,
        "bisync_filter_path": args.bisync_filter_path,
        "previous_scan_id": previous_scan_id,
        "current_scan_id": None,
        "local_scan": None,
        "candidates": [],
        "summary": {
            "total": 0,
            "high": 0,
            "review": 0,
            "blocked": 0,
        },
        "decision": {
            "decision": "allow_bisync",
            "reason": f"{requested_mode}_mode",
            "notify": False,
        },
        "warnings": [],
        "error": None,
    }

    # The scan is taken even when there is no baseline to compare it against,
    # and that is the point: it becomes the baseline. Returning early here —
    # which is what this did — left the next run without one too, so a database
    # that had never seen an attributed local scan could never acquire one.
    local_scan = run_local_scan(args.db_path, args.local_root)
    payload["local_scan"] = vars(local_scan)
    if not local_scan.started or local_scan.scan_id is None:
        payload["error"] = f"Local scan failed: {local_scan.error}"
        payload["decision"] = decide_preflight(requested_mode, [], error=payload["error"])
        write_json_file(var_path("rename_preflight_state.json"), payload)
        return payload

    payload["current_scan_id"] = local_scan.scan_id

    if previous_scan_id is None:
        if baseline_error is None:
            payload["warnings"].append(
                f"No previous local scan of {normalized_local_root(args.local_root)} to compare "
                "against; renames cannot be detected this run. Recorded one for the next."
            )
            payload["decision"] = decide_no_baseline(requested_mode)
        else:
            payload["warnings"].append(
                f"Could not read the baseline from {args.db_path} ({baseline_error}); "
                "renames cannot be detected this run. Recorded a scan for the next."
            )
            payload["decision"] = decide_no_baseline(requested_mode, "baseline_unreadable")
        write_json_file(var_path("rename_preflight_state.json"), payload)
        return payload

    candidates, warnings = find_candidates(args, previous_scan_id, local_scan.scan_id, rename_policy)
    payload["warnings"].extend(warnings)
    payload["candidates"] = candidates
    payload["summary"] = summarize_candidates(candidates)
    payload["decision"] = decide_preflight(requested_mode, candidates)

    state_record = dict(payload)
    write_json_file(var_path("rename_preflight_state.json"), state_record)
    for candidate in candidates:
        append_json_log(var_path("rename_candidates.jsonl"), {
            "kind": "candidate",
            "detected_at": datetime.now().isoformat(),
            **candidate,
        })
    append_json_log(var_path("rename_candidates.jsonl"), {
        "kind": "decision",
        "detected_at": datetime.now().isoformat(),
        "mode": requested_mode,
        "decision": payload["decision"]["decision"],
        "reason": payload["decision"]["reason"],
        "summary": payload["summary"],
        "previous_scan_id": previous_scan_id,
        "current_scan_id": local_scan.scan_id,
    })
    return payload


def cmd_preflight(args: argparse.Namespace) -> dict:
    detect_payload = cmd_detect(args)
    mode = detect_payload.get("mode", "observe")
    candidates = detect_payload.get("candidates") or []
    rename_policy = detect_payload.get("rename_policy") or load_rename_policy(args.rename_policy_path)
    auto_result = None
    error = detect_payload.get("error")
    applied = 0

    if not error:
        eligible = [
            item for item in candidates
            if item.get("confidence") == "high" and item.get("preflight_mode") == "auto"
        ]
        if eligible:
            auto_result, auto_error = apply_auto_candidates(eligible, args, rename_policy)
            applied = len(auto_result.get("moves") or [])
            error = auto_error
            if auto_result:
                write_json_file(var_path("rename_apply_last.json"), {
                    "schema": SCHEMA,
                    "action": "auto-apply",
                    "timestamp": datetime.now().isoformat(),
                    "error": auto_error,
                    "results": auto_result,
                })
                append_json_log(var_path("rename.log"), {
                    "schema": SCHEMA,
                    "timestamp": datetime.now().isoformat(),
                    "action": "auto-apply",
                    "status": "error" if auto_error else "ok",
                    "error": auto_error,
                    "results": auto_result,
                })

    # This recomputes the decision rather than reading detect's, so a verdict
    # that does not follow from the candidate list has to be carried across
    # explicitly. "No baseline" is exactly that: zero candidates because
    # nothing could be compared, which is not the same as zero candidates
    # because nothing changed, and the two must not collapse into one answer.
    detect_reason = detect_decision_reason(detect_payload)
    if not error and detect_reason in NO_BASELINE_REASONS:
        decision = decide_no_baseline(mode, detect_reason)
    else:
        decision = decide_preflight(mode, candidates, applied=applied, error=error)
        if error and mode in {"guard", "auto"}:
            decision["decision"] = "allow_bisync"
            decision["reason"] = "error"
            decision["notify"] = True
    payload = {
        "schema": PREFLIGHT_SCHEMA,
        "action": "preflight",
        "dry_run": True,
        "mode": mode,
        "decision": decision["decision"],
        "reason": decision["reason"],
        "notify": decision["notify"],
        "summary": decision["summary"],
        "detect": detect_payload,
        "auto_result": auto_result,
        "error": error,
    }
    write_json_file(var_path("rename_preflight_state.json"), payload)
    append_json_log(var_path("rename_candidates.jsonl"), {
        "kind": "preflight",
        "timestamp": datetime.now().isoformat(),
        "mode": mode,
        "decision": payload["decision"],
        "reason": payload["reason"],
        "summary": payload["summary"],
        "error": error,
    })
    if payload["notify"] and getattr(args, "notify", True):
        notify_termux(
            "ydm rename preflight",
            f"{payload['decision']}: {payload['reason']}; run ydm-rename-status",
        )
    return payload


def cmd_policy_status(args: argparse.Namespace) -> dict:
    policy = load_rename_policy(args.rename_policy_path)
    return {
        "schema": SCHEMA,
        "action": "policy-status",
        "dry_run": True,
        "rename_policy_path": args.rename_policy_path,
        "policy_exists": os.path.exists(os.path.expanduser(args.rename_policy_path)),
        "policy": policy,
        "error": None,
    }


def cmd_policy_set(args: argparse.Namespace) -> dict:
    policy = load_rename_policy(args.rename_policy_path)
    mode = args.mode
    if args.path:
        entry = normalize_entry(args.path)
        policy.setdefault("roots", {})[entry] = {"mode": mode}
    else:
        policy["default_mode"] = mode
    policy["schema"] = POLICY_SCHEMA
    policy["updated_at"] = datetime.now().isoformat()
    write_rename_policy(args.rename_policy_path, policy)
    return {
        "schema": SCHEMA,
        "action": "policy-set",
        "dry_run": False,
        "rename_policy_path": args.rename_policy_path,
        "policy_exists": True,
        "policy": policy,
        "error": None,
    }


def load_latest_candidates() -> List[dict]:
    state_path = var_path("rename_preflight_state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path, "r") as handle:
                payload = json.load(handle)
            return payload.get("candidates") or []
        except (OSError, ValueError):
            return []
    return []


def find_candidate_by_id(candidate_id_value: str) -> Optional[dict]:
    for item in load_latest_candidates():
        if item.get("id") == candidate_id_value:
            return item
    log_path = var_path("rename_candidates.jsonl")
    if not os.path.exists(log_path):
        return None
    found = None
    with open(log_path, "r") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get("kind") == "candidate" and item.get("id") == candidate_id_value:
                found = item
    return found


def cmd_apply_detected(args: argparse.Namespace) -> dict:
    candidate = find_candidate_by_id(args.candidate_id)
    payload = {
        "schema": SCHEMA,
        "action": "apply-detected",
        "dry_run": not args.apply,
        "candidate_id": args.candidate_id,
        "candidate": candidate,
        "operations": [],
        "results": None,
        "warnings": [],
        "error": None,
    }
    if not candidate:
        payload["error"] = f"Candidate not found: {args.candidate_id}"
        return payload
    if candidate.get("confidence") == "blocked":
        payload["error"] = f"Candidate is blocked: {', '.join(candidate.get('blockers') or [])}"
        return payload

    old_entry = normalize_entry(candidate["old"])
    new_entry = normalize_entry(candidate["new"])
    local_new = local_path(args.local_root, new_entry)
    payload["operations"] = [
        {
            "type": "remote_moveto",
            "cmd": ["rclone", "moveto", remote_path(args.remote, old_entry), remote_path(args.remote, new_entry)],
        },
        {
            "type": f"bisync_{args.followup}",
            "cmd": [
                sys.executable,
                str(ROOT_DIR / "tools" / "sync_bisync.py"),
                args.followup,
                "--apply",
                "--filter-path",
                args.bisync_filter_path,
            ],
        },
    ]
    if not args.apply:
        return payload

    if not os.path.exists(local_new):
        payload["error"] = f"Local renamed file not found: {local_new}"
        return payload
    remote_old = remote_exists(args.remote, old_entry, args.local_root)
    remote_new = remote_exists(args.remote, new_entry, args.local_root)
    if not remote_old["exists"]:
        payload["error"] = f"Remote source not found: {remote_path(args.remote, old_entry)}"
        return payload
    if remote_new["exists"]:
        payload["error"] = f"Remote target already exists: {remote_path(args.remote, new_entry)}"
        return payload

    start = time.time()
    move_start = time.time()
    move_result = rclone_moveto(args.remote, old_entry, new_entry, args.local_root)
    result_summary: Dict[str, Any] = {
        "remote_moveto": {
            "cmd": move_result.cmd,
            "returncode": move_result.returncode,
            "stdout": move_result.stdout,
            "stderr": move_result.stderr,
            "duration_sec": round(time.time() - move_start, 3),
        },
        "bisync": None,
    }
    if move_result.returncode != 0:
        payload["error"] = f"Remote rclone moveto failed (returncode={move_result.returncode})"
        payload["results"] = result_summary
        append_json_log(var_path("rename.log"), {
            "schema": SCHEMA,
            "timestamp": datetime.now().isoformat(),
            "action": "apply-detected",
            "status": "error",
            "candidate_id": args.candidate_id,
            "old": candidate["old"],
            "new": candidate["new"],
            "duration_sec": round(time.time() - start, 3),
            "error": payload["error"],
            "results": result_summary,
        })
        return payload

    bisync_result = run_followup_bisync(args)
    result_summary["bisync"] = None if bisync_result is None else {
        "cmd": bisync_result.cmd,
        "returncode": bisync_result.returncode,
        "stdout": bisync_result.stdout,
        "stderr": bisync_result.stderr,
    }
    if bisync_result is not None and (
        bisync_result.returncode != 0 or '"success": false' in bisync_result.stdout
    ):
        payload["error"] = (
            "Remote rename succeeded, but follow-up bisync baseline refresh failed. "
            "Run `python3 tools/sync_bisync.py status` and recover/resync before more changes."
        )
    payload["results"] = result_summary
    payload["duration_sec"] = round(time.time() - start, 3)
    write_json_file(var_path("rename_apply_last.json"), payload)
    append_json_log(var_path("rename.log"), {
        "schema": SCHEMA,
        "timestamp": datetime.now().isoformat(),
        "action": "apply-detected",
        "status": "error" if payload["error"] else "ok",
        "candidate_id": args.candidate_id,
        "old": candidate["old"],
        "new": candidate["new"],
        "duration_sec": payload["duration_sec"],
        "error": payload["error"],
        "results": log_record({"results": result_summary}, "ok", start).get("results"),
    })
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
    if args.verbose and os.path.exists(log_path):
        with open(log_path, "r") as handle:
            lines = handle.readlines()[-10:]
        for line in lines:
            try:
                tail.append(json.loads(line))
            except ValueError:
                tail.append({"raw": line.rstrip("\n")})

    candidate_state = None
    state_path = var_path("rename_preflight_state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path, "r") as handle:
                candidate_state = json.load(handle)
        except (OSError, ValueError):
            candidate_state = None

    policy = None
    try:
        policy = load_policy(args.policy_path)
    except Exception:
        policy = None
    rename_policy = load_rename_policy(args.rename_policy_path)
    show_candidate = None
    if args.show:
        show_candidate = find_candidate_by_id(args.show)

    return {
        "schema": SCHEMA,
        "action": "status",
        "dry_run": True,
        "local_root": args.local_root,
        "remote": args.remote,
        "policy_path": args.policy_path,
        "rename_policy_path": args.rename_policy_path,
        "rename_policy": rename_policy,
        "bisync_filter_path": args.bisync_filter_path,
        "bisync_filter_hash": filter_file_hash(args.bisync_filter_path),
        "bisync_state": load_bisync_state(),
        "policy_paths": policy.get("paths", {}) if policy else None,
        "candidate_state_path": state_path,
        "candidate_state": candidate_state,
        "show_candidate": show_candidate,
        "verbose": args.verbose,
        "log_path": log_path,
        "log_tail": tail,
        "warnings": [],
        "error": None,
    }


def render(payload: dict, fmt: str) -> None:
    success = payload.get("error") is None
    if fmt == "jsonl":
        if payload.get("action") == "detect":
            for candidate in payload.get("candidates") or []:
                print(json.dumps({"kind": "candidate", **candidate}, ensure_ascii=False))
            print(json.dumps({
                "kind": "decision",
                "success": success,
                "mode": payload.get("mode"),
                "summary": payload.get("summary"),
                "decision": payload.get("decision"),
                "error": payload.get("error"),
            }, ensure_ascii=False))
        else:
            print(json.dumps({"success": success, "data": payload}, ensure_ascii=False))
        return
    if fmt == "json":
        print(json.dumps({"success": success, "data": payload}, ensure_ascii=False, indent=2))
        return

    print(f"schema: {SCHEMA}")
    print(f"action: {payload.get('action')}")
    print(f"dry_run: {payload.get('dry_run')}")
    if payload.get("mode"):
        print(f"mode: {payload['mode']}")
    if payload.get("error"):
        print(f"error: {payload['error']}")
    if payload.get("summary") is not None:
        summary = payload["summary"]
        print(
            "rename candidates: "
            f"{summary.get('total', 0)} total, "
            f"{summary.get('high', 0)} high, "
            f"{summary.get('review', 0)} review, "
            f"{summary.get('blocked', 0)} blocked"
        )
    if payload.get("decision"):
        decision = payload["decision"]
        if isinstance(decision, dict):
            print(f"bisync: {decision.get('decision') or decision.get('bisync')} ({decision.get('reason')})")
        else:
            print(f"bisync: {decision} ({payload.get('reason')})")
    if payload.get("action") in {"policy-status", "policy-set"} and payload.get("policy"):
        policy = payload["policy"]
        print(f"rename_policy_path: {payload.get('rename_policy_path')}")
        print(f"policy_exists: {payload.get('policy_exists')}")
        print(f"default_mode: {policy.get('default_mode')}")
        print(f"max_auto_candidates: {policy.get('max_auto_candidates')}")
        roots = policy.get("roots") or {}
        if roots:
            print("roots:")
            for root, meta in sorted(roots.items()):
                print(f"  {root}: {meta.get('mode')}")
    if payload.get("schema") == PREFLIGHT_SCHEMA:
        print(f"preflight decision: {payload.get('decision')} ({payload.get('reason')})")
        print(f"notify: {payload.get('notify')}")
        if payload.get("auto_result"):
            print(f"auto_result: {json.dumps(payload['auto_result'], ensure_ascii=False)}")
    if payload.get("candidates"):
        print("candidates:")
        for item in payload["candidates"][:20]:
            label = {
                "high": "AUTO",
                "review": "HOLD",
                "blocked": "BLOCK",
            }.get(item.get("confidence"), "HOLD")
            reasons = ",".join(item.get("blockers") or item.get("reason") or [])
            print(f"- {label} {item['id']} {item['old']} -> {item['new']} [{reasons}]")
    if payload.get("candidate_state"):
        state = payload["candidate_state"]
        detect = state.get("detect") if state.get("schema") == PREFLIGHT_SCHEMA else state
        summary = state.get("summary") or detect.get("summary") or {}
        decision = state.get("decision") or (detect.get("decision") or {}).get("decision")
        reason = state.get("reason") or (detect.get("decision") or {}).get("reason")
        mode = state.get("mode") or detect.get("mode")
        if payload.get("rename_policy"):
            print(f"rename policy: {payload['rename_policy'].get('default_mode')} ({payload.get('rename_policy_path')})")
        if decision:
            print(f"last decision: {decision} ({reason}) mode={mode}")
        print(
            "last detect: "
            f"prev={detect.get('previous_scan_id')} current={detect.get('current_scan_id')} "
            f"candidates={summary.get('total', 0)} "
            f"high={summary.get('high', 0)} review={summary.get('review', 0)} blocked={summary.get('blocked', 0)}"
        )
        for item in (detect.get("candidates") or [])[:20]:
            label = {
                "high": "AUTO",
                "review": "HOLD",
                "blocked": "BLOCK",
            }.get(item.get("confidence"), "HOLD")
            reasons = ",".join(item.get("blockers") or item.get("reason") or [])
            print(f"  {label} {item['id']} {item['old']} -> {item['new']} [{reasons}]")
        if summary.get("review", 0) or summary.get("blocked", 0):
            print("next: ydm-rename-status --show <ID>")
        elif summary.get("high", 0):
            print("next: ydm-rename-apply --candidate-id <ID> --apply")
    if payload.get("show_candidate") is not None:
        print("candidate:")
        print(json.dumps(payload["show_candidate"], ensure_ascii=False, indent=2))
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
        sub.add_argument("--format", choices=["json", "jsonl", "text"], default="text")
        sub.add_argument("--local-root", default=DEFAULT_CONFIG["local_root"])
        sub.add_argument("--remote", default=DEFAULT_CONFIG["rclone_remote"])
        sub.add_argument("--policy-path", default=default_policy_path())
        sub.add_argument("--rename-policy-path", default=default_rename_policy_path())
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

    detect_parser = subparsers.add_parser("detect", help="Detect local rename candidates before scheduled bisync")
    common_flags(detect_parser)
    detect_parser.add_argument("--mode", choices=["observe", "guard", "auto"], default=None)

    preflight_parser = subparsers.add_parser("preflight", help="Detect and decide scheduled bisync action")
    common_flags(preflight_parser)
    preflight_parser.add_argument("--mode", choices=["observe", "guard", "auto"], default=None)
    preflight_parser.add_argument("--max-delete", type=int, default=20)
    preflight_parser.add_argument("--check-access", action=argparse.BooleanOptionalAction, default=True)
    preflight_parser.add_argument("--notify-on-error", action=argparse.BooleanOptionalAction, default=True)
    preflight_parser.add_argument("--notify", action=argparse.BooleanOptionalAction, default=True)

    detected_parser = subparsers.add_parser("apply-detected", help="Apply a reviewed candidate from the latest detect run")
    common_flags(detected_parser)
    detected_parser.add_argument("--candidate-id", required=True)
    detected_parser.add_argument("--apply", action="store_true", help="Actually apply; omitted means dry-run")
    detected_parser.add_argument("--followup", choices=["resync", "none"], default="resync")
    detected_parser.add_argument("--max-delete", type=int, default=20)
    detected_parser.add_argument("--check-access", action=argparse.BooleanOptionalAction, default=True)
    detected_parser.add_argument("--notify-on-error", action=argparse.BooleanOptionalAction, default=True)

    status_parser = subparsers.add_parser("status", help="Show recent rename log and bisync state")
    common_flags(status_parser)
    status_parser.add_argument("--show", default=None, help="Show one candidate by id")
    status_parser.add_argument("--verbose", action="store_true", help="Include verbose rename.log tail")

    policy_status_parser = subparsers.add_parser("policy-status", help="Show rename preflight policy")
    common_flags(policy_status_parser)

    policy_set_parser = subparsers.add_parser("policy-set", help="Set rename preflight policy mode")
    common_flags(policy_set_parser)
    policy_set_parser.add_argument("--mode", choices=["observe", "guard", "auto"], required=True)
    policy_set_parser.add_argument("--path", default=None, help="Optional policy root path override")

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
    elif args.command == "detect":
        payload = cmd_detect(args)
    elif args.command == "preflight":
        payload = cmd_preflight(args)
    elif args.command == "apply-detected":
        payload = cmd_apply_detected(args)
    elif args.command == "policy-status":
        payload = cmd_policy_status(args)
    elif args.command == "policy-set":
        payload = cmd_policy_set(args)
    else:
        payload = cmd_status(args)
    render(payload, args.format)


if __name__ == "__main__":
    main()
