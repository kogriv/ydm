#!/usr/bin/env python3
"""
Policy-aware sync management for the rclone backend.

This layer separates "materialize this path locally" from "include this path
in scheduled bidirectional bisync". It is intentionally dry-run by default.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.sync_common import (  # noqa: E402
    build_composite_snapshot,
    create_storage,
    legacy_filter_path,
    fetch_child_dirs,
    filter_file_hash,
    load_bisync_state,
    load_sync_filters,
    normalize_path,
    path_exists_in_snapshot,
    require_local_root,
    select_scan_id_for_path,
    var_path,
)
from tools.sync_backends import (  # noqa: E402
    backend_from_args,
    detect_backend,
)
from ydm import Analyzer, DEFAULT_CONFIG  # noqa: E402


SCHEMA = "ydm_sync_policy:v1"
RISK_SCHEMA = "ydm_sync_risk:v1"
CHECK_ACCESS_FILENAME = "RCLONE_TEST"
ANDROID_FORBIDDEN_CHARS = set('<>:"|?*\\')
SANITIZE_MAP = str.maketrans({
    "<": "＜",
    ">": "＞",
    ":": "：",
    '"': "＂",
    "|": "｜",
    "?": "？",
    "*": "＊",
    "\\": "＼",
})
VALID_MODES = {"bidirectional", "download_only", "disabled"}


class PolicyCoercionError(RuntimeError):
    """Ancestor conflicts could not be resolved safely for the daemon backend."""


@dataclass
class CloudFile:
    parent_path: str
    name: str
    size: int

    @property
    def rel_path(self) -> str:
        parent = self.parent_path.strip("/")
        return f"{parent}/{self.name}" if parent else self.name

    @property
    def chars(self) -> List[str]:
        return sorted({ch for ch in self.rel_path if ch in ANDROID_FORBIDDEN_CHARS})

    @property
    def sanitized_rel_path(self) -> str:
        return self.rel_path.translate(SANITIZE_MAP)


def normalize_entry(path: str) -> str:
    normalized = normalize_path(path)
    if normalized == "/":
        return ""
    return normalized.strip("/")


def default_policy_path() -> str:
    return var_path("sync_policy.json")


def download_filter_path(local_root: str) -> str:
    return f"{os.path.expanduser(local_root).rstrip('/')}.download.filters"


def bisync_filter_path(local_root: str) -> str:
    return f"{os.path.expanduser(local_root).rstrip('/')}.bisync.filters"


def load_policy(path: str) -> Optional[dict]:
    resolved = os.path.expanduser(path)
    if not os.path.exists(resolved):
        return None
    with open(resolved, "r") as handle:
        payload = json.load(handle)
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"Unsupported policy schema: {payload.get('schema')}")
    return payload


def write_policy(path: str, policy: dict) -> None:
    resolved = os.path.expanduser(path)
    os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
    if os.path.exists(resolved):
        backup = f"{resolved}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        with open(resolved, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
    with open(resolved, "w") as handle:
        json.dump(policy, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def empty_policy(local_root: str, remote: str) -> dict:
    return {
        "schema": SCHEMA,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "local_root": local_root,
        "remote": remote,
        "paths": {},
    }


def policy_paths_by_mode(policy: dict, mode: str) -> List[str]:
    result = []
    for entry, meta in policy.get("paths", {}).items():
        if meta.get("mode") == mode:
            result.append(entry)
    return sorted(result)


def effective_download_paths(policy: dict) -> List[str]:
    return sorted(
        entry
        for entry, meta in policy.get("paths", {}).items()
        if meta.get("mode") in {"bidirectional", "download_only"}
    )


def effective_bisync_paths(policy: dict) -> List[str]:
    return policy_paths_by_mode(policy, "bidirectional")


def _is_disabled_ancestor(entry: str, target: str) -> bool:
    """Return True if entry is an ancestor path of target and would block it."""
    if not entry:
        return False
    if entry == target:
        return False
    # entry is ancestor if target starts with entry + "/"
    return target.startswith(entry + "/")


def _find_scan_with_children(storage, parent_path: str) -> Optional[int]:
    """Return the latest scan_id that actually contains directory children of parent_path."""
    from tools.sync_common import normalize_db_parent_path

    parent_path_db = normalize_db_parent_path(parent_path)
    conn = storage.get_connection()
    try:
        row = conn.execute(
            """
            SELECT scan_id
            FROM files
            WHERE parent_path = ? AND type = 'dir'
            ORDER BY scan_id DESC
            LIMIT 1
            """,
            (parent_path_db,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _policy_coerce_for_daemon(
    policy: dict,
    target: str,
    mode: str,
    db_path: str,
) -> List[str]:
    """
    Resolve ancestor conflicts for the daemon backend.

    The yandex-disk daemon uses an exclude-dirs blacklist: a disabled ancestor
    blocks every descendant. When the user adds a descendant as bidirectional
    (or download_only), we must:
      1. Remove disabled entries that are ancestors of the target.
      2. Exclude the siblings of the target's own path on *every* level between
         the removed ancestor and the target — not just the first one. For
         ancestor `Books` and target `Books/Math/АнГем` that means excluding
         `Books/*` except `Math` **and** `Books/Math/*` except `АнГем`; stopping
         after the first level leaves the whole of `Books/Math` synced.

    Both steps need a cloud snapshot of every intermediate level. Without it
    the siblings are unknown, and dropping the ancestor anyway would hand the
    entire branch to the daemon — so this raises `PolicyCoercionError` and
    leaves the policy untouched instead of guessing.

    Returns a list of human-readable changes.
    """
    changes: List[str] = []
    if mode == "disabled":
        return changes

    paths = policy.get("paths", {})
    ancestors_to_remove = sorted(
        (
            entry for entry, meta in paths.items()
            if meta.get("mode") == "disabled" and _is_disabled_ancestor(entry, target)
        ),
        key=lambda entry: entry.count("/"),
    )
    if not ancestors_to_remove:
        return changes

    storage = create_storage(db_path)
    target_parts = target.split("/")

    # Resolve everything before touching the policy: a failure halfway through
    # must not leave the ancestor removed with its siblings unaccounted for.
    siblings: List[str] = []
    shallowest = ancestors_to_remove[0]
    start_depth = len(shallowest.split("/")) if shallowest else 0
    for depth in range(start_depth, len(target_parts)):
        parent_entry = "/".join(target_parts[:depth])
        parent_path = f"/{parent_entry}" if parent_entry else "/"
        keep = target_parts[depth]

        scan_id = _find_scan_with_children(storage, parent_path)
        if scan_id is None:
            raise PolicyCoercionError(
                f"No cloud snapshot of {parent_path}: cannot tell which sibling "
                f"folders must stay excluded. Run "
                f"`python3 ydm.py scan cloud --path {parent_path}` first."
            )
        child_names = fetch_child_dirs(storage, scan_id, parent_path)
        if keep not in child_names:
            raise PolicyCoercionError(
                f"/{'/'.join(target_parts[:depth + 1])} is not in the cloud "
                f"snapshot of {parent_path} (scan {scan_id}). Rescan that path "
                f"before including it."
            )
        for child_name in child_names:
            if child_name == keep:
                continue
            siblings.append(f"{parent_entry}/{child_name}" if parent_entry else child_name)

    for ancestor in ancestors_to_remove:
        del paths[ancestor]
        changes.append(f"removed disabled ancestor: /{ancestor}")

    for sibling_entry in siblings:
        # Don't overwrite an explicitly managed path
        if sibling_entry in paths:
            continue
        paths[sibling_entry] = {"mode": "disabled"}
        changes.append(f"added disabled sibling: /{sibling_entry}")

    return changes


def write_filter_file(path: str, include_dirs: Iterable[str], *, check_access: bool = False) -> None:
    resolved = os.path.expanduser(path)
    os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
    lines = [f"+ /{entry}/**\n" for entry in sorted(set(include_dirs)) if entry]
    if check_access:
        lines.append(f"+ /{CHECK_ACCESS_FILENAME}\n")
    lines.append("- **\n")
    if os.path.exists(resolved):
        backup = f"{resolved}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        with open(resolved, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
    with open(resolved, "w") as handle:
        handle.writelines(lines)


def render_filter_text(include_dirs: Iterable[str], *, check_access: bool = False) -> str:
    lines = [f"+ /{entry}/**" for entry in sorted(set(include_dirs)) if entry]
    if check_access:
        lines.append(f"+ /{CHECK_ACCESS_FILENAME}")
    lines.append("- **")
    return "\n".join(lines) + "\n"


def cloud_files_for_path(db_path: str, path: str) -> Tuple[int, bool, List[CloudFile]]:
    storage = create_storage(db_path)
    analyzer = Analyzer(storage)
    snapshot = build_composite_snapshot(analyzer)
    normalized = normalize_path(path)
    scan_id = select_scan_id_for_path(normalized, snapshot)
    exists = normalized == "/" or path_exists_in_snapshot(analyzer, snapshot, normalized)
    parent = "" if normalized == "/" else normalized
    like = parent.rstrip("/") + "/%"

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT parent_path, name, COALESCE(size, 0)
            FROM files
            WHERE scan_id = ?
              AND type = 'file'
              AND (? = '/' OR parent_path = ? OR parent_path LIKE ?)
            ORDER BY parent_path, name
            """,
            (scan_id, normalized, parent, like),
        ).fetchall()
    finally:
        conn.close()
    return scan_id, exists, [CloudFile(parent_path=row[0], name=row[1], size=int(row[2])) for row in rows]


def inspect_path(db_path: str, local_root: str, path: str, *, max_examples: int = 5) -> dict:
    entry = normalize_entry(path)
    scan_id, path_exists, files = cloud_files_for_path(db_path, f"/{entry}" if entry else "/")

    incompatible = [item for item in files if item.chars]
    sanitized_seen: Dict[str, str] = {}
    collisions: List[dict] = []
    unicode_seen: Dict[str, str] = {}
    unicode_collisions: List[dict] = []

    for item in files:
        sanitized = item.sanitized_rel_path
        previous = sanitized_seen.get(sanitized)
        if previous and previous != item.rel_path:
            collisions.append({
                "sanitized_path": sanitized,
                "paths": sorted([previous, item.rel_path]),
            })
        else:
            sanitized_seen[sanitized] = item.rel_path

        normalized = unicodedata.normalize("NFC", sanitized).casefold()
        previous_unicode = unicode_seen.get(normalized)
        if previous_unicode and previous_unicode != item.rel_path:
            unicode_collisions.append({
                "normalized_key": normalized,
                "paths": sorted([previous_unicode, item.rel_path]),
            })
        else:
            unicode_seen[normalized] = item.rel_path

    risks = []
    if not path_exists:
        risks.append({
            "code": "path_not_found",
            "severity": "blocker",
            "count": 1,
            "examples": [f"/{entry}" if entry else "/"],
        })
    if incompatible:
        risks.append({
            "code": "android_incompatible_names",
            "severity": "blocker",
            "count": len(incompatible),
            "examples": [item.rel_path for item in incompatible[:max_examples]],
        })
    if collisions:
        risks.append({
            "code": "sanitization_collisions",
            "severity": "blocker",
            "count": len(collisions),
            "examples": collisions[:max_examples],
        })
    if unicode_collisions:
        risks.append({
            "code": "unicode_or_case_collisions",
            "severity": "warning",
            "count": len(unicode_collisions),
            "examples": unicode_collisions[:max_examples],
        })

    local_divergent = []
    for item in incompatible:
        local_sanitized = os.path.join(os.path.expanduser(local_root), item.sanitized_rel_path)
        local_original = os.path.join(os.path.expanduser(local_root), item.rel_path)
        if os.path.exists(local_sanitized) and not os.path.exists(local_original):
            local_divergent.append(item.sanitized_rel_path)
    if local_divergent:
        risks.append({
            "code": "existing_local_sanitized_names",
            "severity": "warning",
            "count": len(local_divergent),
            "examples": local_divergent[:max_examples],
        })

    blocker = any(risk["severity"] == "blocker" for risk in risks)
    return {
        "schema": RISK_SCHEMA,
        "path": f"/{entry}" if entry else "/",
        "entry": entry,
        "scan_id": scan_id,
        "path_exists": path_exists,
        "file_count": len(files),
        "safe_for_bidirectional": not blocker,
        "recommended_mode": "bidirectional" if not blocker else "download_only",
        "risks": risks,
        "choices": ["bidirectional"] if not blocker else ["download_only", "rename_cloud_plan", "abort"],
    }


def migrate_policy(args: argparse.Namespace) -> dict:
    backend_name = args.backend
    resolved = backend_name
    if resolved == "auto":
        resolved = _resolve_backend_name(args)

    if resolved == "daemon":
        return _migrate_from_daemon(args)

    filter_path = args.legacy_filter_path or legacy_filter_path(args.local_root)
    filters = load_sync_filters(filter_path)
    policy = empty_policy(args.local_root, args.remote)
    inspections = []

    for entry in filters.include_dirs:
        if entry == CHECK_ACCESS_FILENAME:
            continue
        risk = inspect_path(args.db_path, args.local_root, f"/{entry}")
        inspections.append(risk)
        mode = "bidirectional" if risk["safe_for_bidirectional"] else "download_only"
        meta = {"mode": mode}
        if mode == "download_only":
            meta["reason"] = risk["risks"][0]["code"] if risk["risks"] else "risk_detected"
            meta["risk_scan_id"] = risk["scan_id"]
        policy["paths"][entry] = meta

    policy["updated_at"] = datetime.now().isoformat()
    result = {
        "schema": SCHEMA,
        "action": "migrate",
        "dry_run": not args.apply,
        "policy_path": args.policy_path,
        "legacy_filter_path": filter_path,
        "warnings": filters.warnings,
        "policy": policy,
        "inspections": inspections,
        "error": None,
    }
    if args.apply:
        write_policy(args.policy_path, policy)
    return result


def _migrate_from_daemon(args: argparse.Namespace) -> dict:
    from tools.sync_common import load_exclude_dirs
    exclude_result = load_exclude_dirs(args.exclude_config)
    policy = empty_policy(args.local_root, args.remote)
    for entry in exclude_result.exclude_dirs:
        if entry == CHECK_ACCESS_FILENAME:
            continue
        policy["paths"][entry] = {"mode": "disabled"}
    policy["updated_at"] = datetime.now().isoformat()
    result = {
        "schema": SCHEMA,
        "action": "migrate",
        "source": "daemon",
        "dry_run": not args.apply,
        "policy_path": args.policy_path,
        "exclude_config": exclude_result.config_path,
        "warnings": exclude_result.warnings,
        "policy": policy,
        "error": None,
    }
    if args.apply:
        write_policy(args.policy_path, policy)
    return result


def render_filters(args: argparse.Namespace, policy: dict) -> dict:
    download_paths = effective_download_paths(policy)
    bisync_paths = effective_bisync_paths(policy)
    download_path = args.download_filter_path or download_filter_path(args.local_root)
    bisync_path = args.bisync_filter_path or bisync_filter_path(args.local_root)
    result = {
        "schema": SCHEMA,
        "action": "render-filters",
        "dry_run": not args.apply,
        "policy_path": args.policy_path,
        "download_filter_path": download_path,
        "bisync_filter_path": bisync_path,
        "download_paths": download_paths,
        "bisync_paths": bisync_paths,
        "download_filter": render_filter_text(download_paths),
        "bisync_filter": render_filter_text(bisync_paths, check_access=True),
        "error": None,
    }
    if args.apply:
        write_filter_file(download_path, download_paths)
        write_filter_file(bisync_path, bisync_paths, check_access=True)
    return result


def status_payload(args: argparse.Namespace) -> dict:
    policy = load_policy(args.policy_path)
    resolved_legacy_filter = args.legacy_filter_path or legacy_filter_path(args.local_root)
    download_path = args.download_filter_path or download_filter_path(args.local_root)
    bisync_path = args.bisync_filter_path or bisync_filter_path(args.local_root)
    state = load_bisync_state()
    current_bisync_hash = filter_file_hash(bisync_path)
    state_hash = state.get("last_resync_filter_hash")

    return {
        "schema": SCHEMA,
        "action": "status",
        "dry_run": True,
        "policy_path": args.policy_path,
        "policy_exists": policy is not None,
        "local_root": args.local_root,
        "remote": args.remote,
        "legacy_filter_path": resolved_legacy_filter,
        "download_filter_path": download_path,
        "bisync_filter_path": bisync_path,
        "download_filter_hash": filter_file_hash(download_path),
        "bisync_filter_hash": current_bisync_hash,
        "legacy_filter_hash": filter_file_hash(resolved_legacy_filter),
        "bidirectional_paths": effective_bisync_paths(policy) if policy else [],
        "download_only_paths": policy_paths_by_mode(policy, "download_only") if policy else [],
        "disabled_paths": policy_paths_by_mode(policy, "disabled") if policy else [],
        "bisync_state": state,
        "resync_needed": (not state_hash or current_bisync_hash != state_hash),
        "error": None if policy else f"Policy not found: {args.policy_path}",
    }


def add_policy_path(args: argparse.Namespace) -> dict:
    policy = load_policy(args.policy_path) or empty_policy(args.local_root, args.remote)
    entry = normalize_entry(args.path)
    risk = inspect_path(args.db_path, args.local_root, f"/{entry}")
    requested_mode = args.mode
    if requested_mode == "auto":
        requested_mode = risk["recommended_mode"]

    error = None
    if requested_mode == "bidirectional" and not risk["safe_for_bidirectional"] and not args.force_risk:
        error = (
            "Path is not safe for bidirectional sync. Use --mode download_only "
            "or --force-risk to override."
        )
    elif requested_mode not in VALID_MODES:
        error = f"Invalid mode: {requested_mode}"

    result = {
        "schema": SCHEMA,
        "action": "add",
        "dry_run": not args.apply,
        "policy_path": args.policy_path,
        "path": f"/{entry}",
        "mode": requested_mode,
        "risk": risk,
        "error": error,
    }
    if args.apply and error is None:
        meta = {"mode": requested_mode}
        if requested_mode == "download_only" and risk["risks"]:
            meta["reason"] = risk["risks"][0]["code"]
            meta["risk_scan_id"] = risk["scan_id"]
        if requested_mode == "bidirectional" and args.force_risk:
            meta["forced_risk"] = True
        policy["paths"][entry] = meta

        # Resolve ancestor conflicts on daemon backend
        backend_name = getattr(args, "backend", None)
        if backend_name == "auto":
            backend_name = _resolve_backend_name(args)
        if backend_name == "daemon":
            try:
                coerce_changes = _policy_coerce_for_daemon(
                    policy, entry, requested_mode, args.db_path
                )
            except PolicyCoercionError as exc:
                # Leave the policy file as it was: a half-applied coercion is
                # what un-excluded /Books on 2026-08-14.
                result["error"] = str(exc)
                return result
            if coerce_changes:
                result["coerce_changes"] = coerce_changes

        policy["updated_at"] = datetime.now().isoformat()
        write_policy(args.policy_path, policy)
        result["policy"] = policy
    return result


def remove_policy_path(args: argparse.Namespace) -> dict:
    policy = load_policy(args.policy_path)
    entry = normalize_entry(args.path)
    if not policy:
        return {
            "schema": SCHEMA,
            "action": "remove",
            "dry_run": not args.apply,
            "policy_path": args.policy_path,
            "path": f"/{entry}",
            "error": f"Policy not found: {args.policy_path}",
        }
    existed = entry in policy.get("paths", {})
    result = {
        "schema": SCHEMA,
        "action": "remove",
        "dry_run": not args.apply,
        "policy_path": args.policy_path,
        "path": f"/{entry}",
        "existed": existed,
        "error": None if existed else f"Path not in policy: /{entry}",
    }
    if args.apply and existed:
        del policy["paths"][entry]
        policy["updated_at"] = datetime.now().isoformat()
        write_policy(args.policy_path, policy)
        result["policy"] = policy
    return result


def render(payload: dict, fmt: str, text_header: bool) -> None:
    success = payload.get("error") is None
    if fmt == "json":
        print(json.dumps({"success": success, "data": payload}, ensure_ascii=False, indent=2))
        return

    if text_header:
        print(f"schema: {payload.get('schema', SCHEMA)}")
        if payload.get("error"):
            print(f"error: {payload['error']}")
        print(f"action: {payload.get('action')}")
        print(f"dry_run: {payload.get('dry_run')}")
        if payload.get("policy_path"):
            print(f"policy_path: {payload['policy_path']}")
        print("")

    action = payload.get("action")
    if action == "inspect":
        print(f"path: {payload['path']}")
        print(f"scan_id: {payload['scan_id']}")
        print(f"path_exists: {payload['path_exists']}")
        print(f"file_count: {payload['file_count']}")
        print(f"safe_for_bidirectional: {payload['safe_for_bidirectional']}")
        print(f"recommended_mode: {payload['recommended_mode']}")
        for risk in payload["risks"]:
            print(f"risk: {risk['severity']} {risk['code']} count={risk['count']}")
            for example in risk.get("examples", []):
                print(f"  - {example}")
    elif action == "status":
        print(f"policy_exists: {payload['policy_exists']}")
        print(f"bidirectional: {', '.join(payload['bidirectional_paths'])}")
        print(f"download_only: {', '.join(payload['download_only_paths'])}")
        print(f"disabled: {', '.join(payload['disabled_paths'])}")
        print(f"download_filter_path: {payload['download_filter_path']}")
        print(f"bisync_filter_path: {payload['bisync_filter_path']}")
        print(f"bisync_filter_hash: {payload['bisync_filter_hash']}")
        print(f"resync_needed: {payload['resync_needed']}")
    elif action == "render-filters":
        print(f"download_filter_path: {payload['download_filter_path']}")
        print(payload["download_filter"], end="")
        print(f"bisync_filter_path: {payload['bisync_filter_path']}")
        print(payload["bisync_filter"], end="")
        backend_apply = payload.get("backend_apply")
        if backend_apply:
            print(f"backend: {backend_apply.get('backend')}")
            print(f"backend_action: {backend_apply.get('action')}")
            if backend_apply.get("error"):
                print(f"backend_error: {backend_apply['error']}")
    elif action == "migrate":
        paths = payload["policy"].get("paths", {})
        source = payload.get("source", "legacy_filter")
        if source == "daemon":
            print(f"exclude_config: {payload.get('exclude_config')}")
        else:
            print(f"legacy_filter_path: {payload.get('legacy_filter_path')}")
        print(f"source: {source}")
        for entry, meta in sorted(paths.items()):
            print(f"{entry}: {meta['mode']}")
    elif action in {"add", "remove"}:
        print(f"path: {payload.get('path')}")
        if payload.get("mode"):
            print(f"mode: {payload['mode']}")
        if payload.get("risk"):
            print(f"safe_for_bidirectional: {payload['risk']['safe_for_bidirectional']}")
        backend_apply = payload.get("backend_apply")
        if backend_apply:
            print(f"backend: {backend_apply.get('backend')}")
            print(f"backend_action: {backend_apply.get('action')}")
            if backend_apply.get("error"):
                print(f"backend_error: {backend_apply['error']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Policy-aware sync management for rclone backend")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common_flags(sub):
        sub.add_argument("--db-path", default=str(ROOT_DIR / "monitor.db"))
        sub.add_argument("--local-root", default=DEFAULT_CONFIG["local_root"])
        sub.add_argument("--remote", default=DEFAULT_CONFIG["rclone_remote"])
        sub.add_argument("--policy-path", default=default_policy_path())
        sub.add_argument("--legacy-filter-path", default=None)
        sub.add_argument("--download-filter-path", default=None)
        sub.add_argument("--bisync-filter-path", default=None)
        sub.add_argument("--format", choices=["json", "text"], default="text")
        sub.add_argument("--text-header", action=argparse.BooleanOptionalAction, default=True)
        sub.add_argument(
            "--backend",
            choices=["daemon", "rclone", "auto"],
            default="auto",
            help="Sync backend to use: daemon (yandex-disk), rclone, or auto-detect",
        )
        sub.add_argument(
            "--exclude-config",
            default=DEFAULT_CONFIG["exclude_config"],
            help="Path to yandex-disk config.cfg (daemon backend only)",
        )

    status = subparsers.add_parser("status")
    common_flags(status)

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--path", required=True)
    common_flags(inspect)

    migrate = subparsers.add_parser("migrate")
    common_flags(migrate)
    migrate.add_argument("--apply", action="store_true")

    render_filters_parser = subparsers.add_parser("render-filters")
    common_flags(render_filters_parser)
    render_filters_parser.add_argument("--apply", action="store_true")

    add = subparsers.add_parser("add")
    add.add_argument("--path", required=True)
    add.add_argument("--mode", choices=["auto", "bidirectional", "download_only", "disabled"], default="auto")
    add.add_argument("--apply", action="store_true")
    add.add_argument("--force-risk", action="store_true")
    common_flags(add)

    remove = subparsers.add_parser("remove")
    remove.add_argument("--path", required=True)
    remove.add_argument("--apply", action="store_true")
    common_flags(remove)

    return parser.parse_args()


def _resolve_backend_name(args: argparse.Namespace) -> str:
    """Resolve effective backend name from args/env/config/auto-detect."""
    backend = getattr(args, "backend", None)
    if backend and backend != "auto":
        return backend
    env = os.environ.get("YDM_BACKEND")
    if env and env != "auto":
        return env
    return detect_backend(
        db_path=args.db_path,
        local_root=args.local_root,
        remote=args.remote,
        policy_path=args.policy_path,
        bisync_filter_path=getattr(args, "bisync_filter_path", None),
        download_filter_path=getattr(args, "download_filter_path", None),
        config_path=getattr(args, "exclude_config", DEFAULT_CONFIG["exclude_config"]),
    ).kind


def _apply_backend_policy(args: argparse.Namespace, policy: dict) -> dict:
    """Apply policy via the selected backend."""
    backend = backend_from_args(args)
    return backend.apply_policy(policy, dry_run=not args.apply)


def main() -> None:
    args = parse_args()
    args.local_root = require_local_root(args.local_root, tool="sync_policy.py")
    try:
        if args.command == "status":
            payload = status_payload(args)
        elif args.command == "inspect":
            payload = inspect_path(args.db_path, args.local_root, args.path)
            payload["action"] = "inspect"
            payload["dry_run"] = True
            payload["policy_path"] = args.policy_path
        elif args.command == "migrate":
            payload = migrate_policy(args)
        elif args.command == "render-filters":
            policy = load_policy(args.policy_path)
            if not policy:
                payload = {
                    "schema": SCHEMA,
                    "action": "render-filters",
                    "dry_run": not args.apply,
                    "policy_path": args.policy_path,
                    "error": f"Policy not found: {args.policy_path}",
                }
            else:
                payload = render_filters(args, policy)
                # Also apply to backend if requested
                if args.apply:
                    backend_payload = _apply_backend_policy(args, policy)
                    payload["backend_apply"] = backend_payload
        elif args.command == "add":
            payload = add_policy_path(args)
            if args.apply and payload.get("error") is None:
                policy = load_policy(args.policy_path)
                if policy:
                    backend_payload = _apply_backend_policy(args, policy)
                    payload["backend_apply"] = backend_payload
        else:
            payload = remove_policy_path(args)
            if args.apply and payload.get("error") is None:
                policy = load_policy(args.policy_path)
                if policy:
                    backend_payload = _apply_backend_policy(args, policy)
                    payload["backend_apply"] = backend_payload
    except Exception as exc:
        payload = {
            "schema": SCHEMA,
            "action": args.command,
            "dry_run": True,
            "policy_path": getattr(args, "policy_path", default_policy_path()),
            "error": str(exc),
        }
    render(payload, args.format, args.text_header)


if __name__ == "__main__":
    main()
