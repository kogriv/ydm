#!/usr/bin/env python3
"""
Policy-aware sync management for the rclone backend.

This layer separates "materialize this path locally" from "include this path
in scheduled bidirectional bisync". It is intentionally dry-run by default.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import sqlite3
import sys
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.sync_common import (  # noqa: E402
    acquire_lock,
    cloud_file_info,
    exit_code,
    fetch_verified,
    place_format_flag,
    file_md5,
    rclone_bin,
    release_lock,
    build_composite_snapshot,
    create_storage,
    legacy_filter_path,
    fetch_child_dirs,
    filter_file_hash,
    load_bisync_state,
    load_sync_filters,
    normalize_path,
    path_exists_in_snapshot,
    PROBE_TIMEOUT_SEC,
    PROBE_WORKERS,
    probe_files,
    rclone_failed_paths,
    require_local_root,
    select_scan_id_for_path,
    var_path,
)
from tools.sync_backends import (  # noqa: E402
    backend_from_args,
    detect_backend,
)
from ydm import Analyzer, DEFAULT_CONFIG  # noqa: E402
from tools.targets import resolve_target, TargetConfigError  # noqa: E402


SCHEMA = "ydm_sync_policy:v2"
LEGACY_SCHEMA = "ydm_sync_policy:v1"
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
    object_id: Optional[str] = None
    mime_type: Optional[str] = None
    is_native: bool = False
    is_shortcut: bool = False

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
    if payload.get("schema") not in {SCHEMA, LEGACY_SCHEMA}:
        raise ValueError(f"Unsupported policy schema: {payload.get('schema')}")
    if payload.get("schema") == LEGACY_SCHEMA:
        # Keep old installations readable; binding is enforced only when a
        # target-aware caller supplies a target context.
        payload["schema"] = SCHEMA
        payload.setdefault("target", None)
        payload.setdefault("provider", None)
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


def empty_policy(local_root: str, remote: str, *, target: Optional[str] = None,
                 provider: Optional[str] = None, inventory_kind: Optional[str] = None,
                 sync_kind: Optional[str] = None) -> dict:
    return {
        "schema": SCHEMA,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "local_root": os.path.realpath(os.path.expanduser(local_root)),
        "remote": remote,
        "target": target,
        "provider": provider,
        "inventory_kind": inventory_kind,
        "sync_kind": sync_kind,
        "paths": {},
    }


def validate_policy_binding(args: argparse.Namespace, policy: dict) -> Optional[str]:
    """Prevent a policy for one target/root/remote being applied to another."""
    # A migrated legacy v1 policy has no target identity. Preserve historical
    # behaviour; target-managed v2 policies are immutable bindings.
    managed = bool(getattr(args, "target_managed", False))
    if policy.get("target") is None and policy.get("provider") is None and not managed:
        return None
    required = ("target", "provider", "remote", "local_root", "inventory_kind", "sync_kind")
    if managed and any(policy.get(key) in (None, "") for key in required):
        return "Policy binding is incomplete for target-managed invocation"
    for key, actual in (("target", getattr(args, "target", None)),
                        ("provider", getattr(args, "provider", None)),
                        ("remote", getattr(args, "remote", None)),
                        ("local_root", os.path.realpath(os.path.expanduser(getattr(args, "local_root", ""))) if getattr(args, "local_root", None) else None),
                        ("inventory_kind", getattr(args, "inventory_kind", None)),
                        ("sync_kind", getattr(args, "sync_kind", None))):
        expected = policy.get(key)
        if actual is not None and expected is not None and expected != actual:
            return f"Policy {key} mismatch: policy={expected!r}, active={actual!r}"
    return None


def configure_target_args(args: argparse.Namespace) -> None:
    """Resolve one immutable target and derive every writable state path."""
    cli_target = getattr(args, "target", None) or os.environ.get("YDM_TARGET")
    targets_file = getattr(args, "targets_file", None) or os.environ.get("YDM_TARGETS_FILE")
    if not cli_target and not targets_file and not os.path.exists("ydm_targets.json"):
        args.target_managed = False
        return
    context = resolve_target(cli_target=cli_target, targets_path=targets_file)
    if context is None:
        raise TargetConfigError("target-managed invocation requires a targets file")
    cfg = context.config
    args.target_managed = True
    args.target, args.provider = cfg.name, cfg.provider
    args.inventory_kind, args.sync_kind = cfg.inventory_kind, cfg.sync_kind
    args.native_docs = cfg.native_docs
    args.target_context = context
    args.remote, args.local_root, args.db_path = cfg.sync_remote, cfg.local_root, context.db_path
    args.policy_path = context.policy_path
    args.download_filter_path, args.bisync_filter_path = context.download_filter_path, context.bisync_filter_path
    # sync.kind is authoritative: Google never falls through daemon autodetect.
    args.backend = "rclone" if cfg.sync_kind == "rclone" else "daemon"


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


def quarantine_paths(policy: Optional[dict]) -> List[str]:
    """Files the sync leaves alone for now, each with the reason it was set aside.

    For a file the cloud lists but will not hand over: on 2026-10-29 Yandex
    answered 500/503 for a few percent of files, and bisync — before rclone
    1.66 — aborts the whole run on one of them. Excluding them by hand in the
    filter file worked until the next `render-filters`, which rewrites it from
    the policy and silently put them back. Kept in the policy, they survive.
    """
    return sorted((policy or {}).get("quarantine", {}))


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


#: How old a listing may be before its subfolders are trusted as the full list
#: of siblings to exclude. A day: long enough to add several paths in a row,
#: short enough that "the folder appeared since" is not a realistic gap.
SNAPSHOT_MAX_AGE_HOURS = 24


def _scan_age_hours(storage, scan_id: int) -> Optional[float]:
    """Age of a scan from `scans.timestamp` (SQLite's UTC CURRENT_TIMESTAMP)."""
    conn = storage.get_connection()
    try:
        row = conn.execute("SELECT timestamp FROM scans WHERE id = ?", (scan_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    try:
        stamp = datetime.strptime(str(row[0])[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return max(0.0, (datetime.utcnow() - stamp).total_seconds() / 3600)


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
        age = _scan_age_hours(storage, scan_id)
        if age is None or age > SNAPSHOT_MAX_AGE_HOURS:
            when = "of unknown age" if age is None else f"{age / 24:.0f} days old"
            raise PolicyCoercionError(
                f"The cloud snapshot of {parent_path} (scan {scan_id}) is {when}. "
                f"Its subfolders become exclusions, and a subfolder created since "
                f"would fall out of them and start syncing. Refresh it first: "
                f"`python3 ydm.py scan cloud --path {parent_path} --depth 1`."
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


def filter_glob_escape(path: str) -> str:
    """A path as a literal in an rclone filter rule.

    Rules are globs: `[rutor.info]` in a name is a character class unless
    escaped, and the rule then matches some other file or none.
    """
    return "".join("\\" + char if char in "\\*?[]{}" else char for char in path)


def ignore_rules(ignore: Optional[Dict[str, List[str]]]) -> List[str]:
    """Exclusion rules for generated files under a synced path.

    A pattern ending in `/` is a directory at any depth below the path
    (`build/`, `__pycache__/`); anything else is a file name glob at any depth
    (`*.log`, `*.aux`). The path is escaped, the pattern is a glob on purpose.
    `**/` in an rclone rule needs at least one directory in between, so each
    pattern gets a rule for the path's own level and one for everything below.
    """
    rules: List[str] = []
    for base in sorted(ignore or {}):
        root = "/" + filter_glob_escape(base)
        for pattern in sorted(set(ignore[base])):
            if pattern.endswith("/"):
                name = pattern.rstrip("/")
                rules += [f"- {root}/{name}/**", f"- {root}/**/{name}/**"]
            else:
                rules += [f"- {root}/{pattern}", f"- {root}/**/{pattern}"]
    return rules


def filter_lines(include_dirs: Iterable[str], *, check_access: bool = False,
                 exclude: Iterable[str] = (),
                 ignore: Optional[Dict[str, List[str]]] = None) -> List[str]:
    """Rule lines, first match wins: quarantine, ignored build output, includes, the rest out."""
    lines = [f"- /{filter_glob_escape(entry)}" for entry in sorted(set(exclude)) if entry]
    lines += ignore_rules(ignore)
    lines += [f"+ /{filter_glob_escape(entry)}/**" for entry in sorted(set(include_dirs)) if entry]
    if check_access:
        lines.append(f"+ /{CHECK_ACCESS_FILENAME}")
    lines.append("- **")
    return lines


def write_filter_file(path: str, include_dirs: Iterable[str], *, check_access: bool = False,
                      exclude: Iterable[str] = (),
                      ignore: Optional[Dict[str, List[str]]] = None) -> None:
    resolved = os.path.expanduser(path)
    os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
    lines = [line + "\n" for line in filter_lines(include_dirs, check_access=check_access,
                                                   exclude=exclude, ignore=ignore)]
    if os.path.exists(resolved):
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup = f"{resolved}.bak.{stamp}"
        if not os.access(os.path.dirname(resolved) or ".", os.W_OK):
            # The file can be ours in a directory that is not: /data/ya_disk's
            # filters sit in /data, which belongs to root. The backup then goes
            # to var/ instead of failing the write it was meant to protect.
            backup = os.path.join(var_path("filter_backups"), f"{os.path.basename(resolved)}.bak.{stamp}")
            os.makedirs(os.path.dirname(backup), exist_ok=True)
        with open(resolved, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
    with open(resolved, "w") as handle:
        handle.writelines(lines)


def render_filter_text(include_dirs: Iterable[str], *, check_access: bool = False,
                       exclude: Iterable[str] = (),
                       ignore: Optional[Dict[str, List[str]]] = None) -> str:
    return "\n".join(filter_lines(include_dirs, check_access=check_access,
                                  exclude=exclude, ignore=ignore)) + "\n"


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
            SELECT parent_path, name, COALESCE(size, 0), object_id, mime_type,
                   COALESCE(is_native, 0), COALESCE(is_shortcut, 0)
            FROM files
            WHERE scan_id = ?
              AND type IN ('file', 'shortcut')
              AND (? = '/' OR parent_path = ? OR parent_path LIKE ?)
            ORDER BY parent_path, name
            """,
            (scan_id, normalized, parent, like),
        ).fetchall()
    finally:
        conn.close()
    return scan_id, exists, [CloudFile(parent_path=row[0], name=row[1], size=int(row[2]),
                                       object_id=row[3], mime_type=row[4],
                                       is_native=bool(row[5]), is_shortcut=bool(row[6])) for row in rows]


def inspect_path(db_path: str, local_root: str, path: str, *, max_examples: int = 5) -> dict:
    entry = normalize_entry(path)
    scan_id, path_exists, files = cloud_files_for_path(db_path, f"/{entry}" if entry else "/")

    incompatible = [item for item in files if item.chars]
    native = [item for item in files if item.is_native]
    shortcuts = [item for item in files if item.is_shortcut]
    sanitized_seen: Dict[str, CloudFile] = {}
    collisions: List[dict] = []
    unicode_seen: Dict[str, str] = {}
    unicode_collisions: List[dict] = []

    for item in files:
        sanitized = item.sanitized_rel_path
        previous = sanitized_seen.get(sanitized)
        if previous and (previous.rel_path != item.rel_path
                         or previous.object_id != item.object_id):
            collisions.append({
                "sanitized_path": sanitized,
                "paths": sorted([previous.rel_path, item.rel_path]),
                "object_ids": sorted(filter(None, [previous.object_id, item.object_id])),
            })
        else:
            sanitized_seen[sanitized] = item

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
    if native:
        risks.append({
            "code": "native_documents",
            "severity": "blocker",
            "count": len(native),
            "examples": [item.rel_path for item in native[:max_examples]],
        })
    if shortcuts:
        risks.append({
            "code": "shortcuts",
            "severity": "blocker",
            "count": len(shortcuts),
            "examples": [item.rel_path for item in shortcuts[:max_examples]],
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
    validation_error = validate_policy_binding(args, policy) or validate_native_policy(args, policy)
    target_managed = getattr(args, "target_managed", False)
    result = {
        "schema": SCHEMA,
        "action": "render-filters",
        "dry_run": not args.apply,
        "policy_path": args.policy_path,
        "download_filter_path": download_path,
        "bisync_filter_path": bisync_path,
        "download_paths": download_paths,
        "bisync_paths": bisync_paths,
        "quarantine": quarantine_paths(policy),
        "ignore": policy.get("ignore", {}),
        "download_filter": render_filter_text(download_paths, exclude=quarantine_paths(policy),
                                              ignore=policy.get("ignore")),
        "bisync_filter": render_filter_text(bisync_paths, check_access=True,
                                            exclude=quarantine_paths(policy),
                                            ignore=policy.get("ignore")),
        "error": validation_error,
    }
    if target_managed:
        result["download_filter"] = None
        result["download_filter_disabled"] = (
            "target-managed downloads use download-apply with an exact inventory manifest"
        )
    if args.apply and validation_error is None:
        if not target_managed:
            write_filter_file(download_path, download_paths, exclude=quarantine_paths(policy),
                              ignore=policy.get("ignore"))
        write_filter_file(bisync_path, bisync_paths, check_access=True,
                          exclude=quarantine_paths(policy), ignore=policy.get("ignore"))
    return result


def expected_bisync_filter(policy: dict) -> str:
    """The bisync filter this policy renders to, for comparing with the file."""
    return render_filter_text(effective_bisync_paths(policy), check_access=True,
                              exclude=quarantine_paths(policy), ignore=policy.get("ignore"))


def quarantined_by(rel_path: str, quarantine: Optional[dict]) -> Optional[str]:
    """The quarantine entry that covers this path, if any."""
    for entry in quarantine or {}:
        if rel_path == entry or rel_path.startswith(entry + "/"):
            return entry
    return None


def ignored_by(rel_path: str, ignore: Optional[Dict[str, List[str]]]) -> Optional[str]:
    """The ignore pattern that covers this path, if any; `ignore_rules` in Python."""
    for base, patterns in (ignore or {}).items():
        if not rel_path.startswith(base + "/"):
            continue
        parts = rel_path[len(base) + 1:].split("/")
        for pattern in patterns:
            if pattern.endswith("/"):
                if any(fnmatch.fnmatchcase(part, pattern[:-1]) for part in parts[:-1]):
                    return pattern
            elif fnmatch.fnmatchcase(parts[-1], pattern):
                return pattern
    return None


def download_manifest_path(args: argparse.Namespace) -> str:
    """Beside the filter, or in var/ when the mirror's parent is not ours. The
    filters are rewritten in place there, but a manifest is replaced by rename."""
    path = (args.download_filter_path or download_filter_path(args.local_root)) + ".files-from"
    if os.access(os.path.dirname(path) or ".", os.W_OK):
        return path
    return var_path(os.path.basename(path))


def download_plan(args: argparse.Namespace, policy: dict) -> dict:
    """Exact, non-mutating plan for selective download-only materialization."""
    error = validate_policy_binding(args, policy) or validate_native_policy(args, policy)
    paths = policy_paths_by_mode(policy, "download_only")
    target = f"{args.remote}:"
    eligibility, excluded = [], []
    candidates = []
    for entry in paths:
        if not os.path.exists(args.db_path):
            excluded.append({"path": entry, "reason": "incomplete_or_missing_inventory"})
            continue
        try:
            scan_id, exists, files = cloud_files_for_path(args.db_path, f"/{entry}" if entry else "/")
        except Exception:
            excluded.append({"path": entry, "reason": "incomplete_or_missing_inventory"})
            continue
        if not exists:
            excluded.append({"path": entry, "reason": "incomplete_or_missing_inventory"})
            continue
        for item in files:
            if quarantined_by(item.rel_path, policy.get("quarantine")):
                excluded.append({"path": item.rel_path, "reason": "quarantined", "scan_id": scan_id})
            elif ignored_by(item.rel_path, policy.get("ignore")):
                excluded.append({"path": item.rel_path, "reason": "ignored", "scan_id": scan_id})
            elif item.is_native:
                excluded.append({"path": item.rel_path, "reason": "native_document", "scan_id": scan_id})
            elif item.is_shortcut:
                excluded.append({"path": item.rel_path, "reason": "shortcut", "scan_id": scan_id})
            elif item.chars or "\n" in item.rel_path or "\r" in item.rel_path:
                excluded.append({"path": item.rel_path, "reason": "incompatible_name", "scan_id": scan_id})
            else:
                candidates.append((item, scan_id))
    groups = {}
    for item, scan_id in candidates:
        groups.setdefault(item.sanitized_rel_path, []).append((item, scan_id))
    for rows in groups.values():
        identities = {(item.rel_path, item.object_id) for item, _scan_id in rows}
        if len(identities) > 1:
            for item, scan_id in rows:
                excluded.append({"path": item.rel_path, "reason": "materialization_collision",
                                 "scan_id": scan_id})
            continue
        item, scan_id = rows[0]
        eligibility.append({"path": item.rel_path, "object_id": item.object_id, "scan_id": scan_id})
    if error is None and any(
            item["reason"] == "incomplete_or_missing_inventory" for item in excluded):
        error = "download inventory is incomplete or missing"
    if error is None and getattr(args, "target_managed", False) and args.sync_kind != "rclone":
        error = "download-apply requires a target with rclone sync"
    manifest_path = download_manifest_path(args)
    return {
        "schema": "ydm_download_plan:v1", "action": "download-plan", "dry_run": True,
        "target": policy.get("target"), "provider": policy.get("provider"),
        "remote": args.remote, "local_root": args.local_root,
        "source": target, "destination": args.local_root, "paths": paths,
        "filter_path": args.download_filter_path or download_filter_path(args.local_root),
        "manifest_path": manifest_path,
        "eligible": eligibility, "excluded": excluded,
        "error": error,
    }


#: One file, whole. A file that does not come down in this long is not back.
RECHECK_TIMEOUT_SEC = 1800

#: A whole `rclone copy`, not one transfer: long enough for a large folder, and
#: rclone's own idle timeout covers a single stalled connection.
DOWNLOAD_TIMEOUT_SEC = 6 * 3600


def download_apply(args: argparse.Namespace, policy: dict) -> dict:
    """Download-only materialization: rclone copy is the sole write primitive."""
    plan = download_plan(args, policy)
    plan["action"] = "download-apply"
    plan["dry_run"] = not args.apply
    if plan["error"] or not args.apply:
        return plan
    if not plan["eligible"]:
        plan["nothing_to_download"] = True
        return plan
    manifest_path = plan["manifest_path"]
    os.makedirs(os.path.dirname(manifest_path) or ".", exist_ok=True)
    temp_path = f"{manifest_path}.tmp.{os.getpid()}"
    with open(temp_path, "w", encoding="utf-8") as handle:
        for item in sorted({row["path"].lstrip("/") for row in plan["eligible"]}):
            handle.write(item + "\n")
    os.replace(temp_path, manifest_path)
    # Exact eligible object paths only. No delete, sync, move, purge, or
    # reverse-transfer flags are permitted in this command.
    cmd = [rclone_bin(), "copy", f"{args.remote}:", args.local_root,
           "--files-from-raw", manifest_path]
    timeout = getattr(args, "timeout_sec", DOWNLOAD_TIMEOUT_SEC) or None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        def text(value):
            return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")
        plan.update({"command": cmd, "returncode": None, "stdout": text(exc.stdout),
                     "stderr": text(exc.stderr), "failed_paths": rclone_failed_paths(text(exc.stderr)),
                     "error": f"rclone copy stopped: no result after {timeout:g}s"})
        return plan
    plan.update({"command": cmd, "returncode": proc.returncode,
                 "stdout": proc.stdout, "stderr": proc.stderr,
                 "error": None if proc.returncode == 0 else f"rclone copy failed (rc={proc.returncode})"})
    if proc.returncode != 0:
        plan["failed_paths"] = rclone_failed_paths(proc.stderr)
    return plan


def native_export_action(args: argparse.Namespace, policy: dict) -> dict:
    """Plan or execute one policy-bound, inventory-verified native export."""
    entry = normalize_entry(args.path)
    result = {
        "schema": "ydm_native_export_result:v1", "action": "native-export",
        "dry_run": not args.apply, "path": f"/{entry}", "error": None,
    }
    error = validate_policy_binding(args, policy) or validate_native_policy(args, policy)
    if error:
        result["error"] = error
        return result
    if not getattr(args, "target_managed", False):
        result["error"] = "native export requires a target-managed invocation"
        return result
    meta = policy.get("paths", {}).get(entry)
    if not isinstance(meta, dict) or meta.get("mode") != "download_only":
        result["error"] = f"Download-only policy entry not found: /{entry}"
        return result
    plan = meta.get("export_plan")
    plan_error = validate_export_plan(plan) if plan is not None else "is missing"
    if plan_error:
        result["error"] = f"Export plan for /{entry} {plan_error}"
        return result
    try:
        scan_id, exists, files = cloud_files_for_path(args.db_path, f"/{entry}" if entry else "/")
    except Exception as exc:
        result["error"] = f"native export inventory is unavailable: {exc}"
        return result
    matches = [item for item in files if item.object_id == plan["source_object_id"]]
    if not exists or len(matches) != 1:
        result["error"] = "native export object is absent or ambiguous in the bound inventory"
        return result
    item = matches[0]
    if item.is_shortcut or not item.is_native or item.mime_type != plan["source_mime_type"]:
        result["error"] = "native export object type or MIME does not match the bound inventory"
        return result
    from tools.native_export import ExportSafetyError, execute_export
    try:
        executed = execute_export(plan, args.target_context, apply=args.apply)
    except ExportSafetyError as exc:
        result["error"] = str(exc)
        return result
    result.update(executed)
    result.update({"action": "native-export", "path": f"/{entry}", "scan_id": scan_id})
    return result


def validate_native_policy(args: argparse.Namespace, policy: dict) -> Optional[str]:
    """Reject native/shortcut materialization before policy or filters write.

    Phase 5 only plans exports. It never transfers them, and an export is
    always download-only because it is not equivalent to its native source.
    """
    for entry, meta in policy.get("paths", {}).items():
        export_plan = meta.get("export_plan")
        export_error = validate_export_plan(export_plan) if export_plan is not None else None
        if export_error:
            return f"Export plan for /{entry} {export_error}"
        if export_plan and meta.get("mode") != "download_only":
            return f"Export plan for /{entry} requires download_only mode"
        if export_plan and export_plan.get("mode") != "download_only":
            return f"Export plan for /{entry} must declare download_only mode"
        if meta.get("mode") == "bidirectional":
            _scan_id, _exists, files = cloud_files_for_path(args.db_path, f"/{entry}" if entry else "/")
            unsafe = [item.rel_path for item in files if item.is_native or item.is_shortcut]
            if unsafe:
                return f"Bidirectional mode rejects native documents or shortcuts under /{entry}"
            materialized = {}
            for item in files:
                previous = materialized.get(item.sanitized_rel_path)
                if previous and (previous.rel_path != item.rel_path
                                 or previous.object_id != item.object_id):
                    return f"Bidirectional mode rejects materialization collisions under /{entry}"
                materialized[item.sanitized_rel_path] = item
    return None


def validate_export_plan(plan: object) -> Optional[str]:
    """Validate the safe, non-executable native export plan envelope."""
    if not isinstance(plan, dict):
        return "must be an object"
    if plan.get("schema") != "ydm_native_export_plan:v1":
        return "has an unsupported schema"
    if plan.get("mode") != "download_only":
        return "must declare download_only mode"
    required = (
        "source_object_id", "source_mime_type", "format",
        "export_mime_type", "artifact_path",
    )
    if any(not isinstance(plan.get(key), str) or not plan[key] for key in required):
        return "is missing required string fields"
    return None


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
        "quarantine": (policy or {}).get("quarantine", {}),
        "ignore": (policy or {}).get("ignore", {}),
        "bisync_state": state,
        "resync_needed": (not state_hash or current_bisync_hash != state_hash),
        "error": None if policy else f"Policy not found: {args.policy_path}",
    }


def content_probe(args: argparse.Namespace, policy: dict, entry: str) -> dict:
    """Which files under a path the cloud will not hand over, as of the snapshot.

    Reads the first bytes of each file. Native documents, shortcuts and what the
    policy already leaves out are not fetched by a sync, so they are not probed.
    """
    _scan_id, _exists, files = cloud_files_for_path(args.db_path, f"/{entry}" if entry else "/")
    wanted = [(item.rel_path, item.size) for item in files
              if not (item.is_native or item.is_shortcut
                      or quarantined_by(item.rel_path, policy.get("quarantine"))
                      or ignored_by(item.rel_path, policy.get("ignore")))]
    results = probe_files(args.remote, wanted,
                          workers=getattr(args, "probe_workers", PROBE_WORKERS),
                          timeout=getattr(args, "probe_timeout", PROBE_TIMEOUT_SEC))
    unreadable = [{"path": f"/{row['path']}", "reason": row["reason"]}
                  for row in results if not row["readable"]]
    return {"checked": len(results), "readable": len(results) - len(unreadable),
            "unreadable": unreadable}


def add_policy_path(args: argparse.Namespace) -> dict:
    policy = load_policy(args.policy_path) or empty_policy(
        args.local_root, args.remote, target=getattr(args, "target", None),
        provider=getattr(args, "provider", None), inventory_kind=getattr(args, "inventory_kind", None),
        sync_kind=getattr(args, "sync_kind", None))
    entry = normalize_entry(args.path)
    risk = inspect_path(args.db_path, args.local_root, f"/{entry}")
    requested_mode = args.mode
    if requested_mode == "auto":
        requested_mode = risk["recommended_mode"]

    error = None
    absolute_blockers = {
        item.get("code") for item in risk.get("risks", [])
    } & {"native_documents", "shortcuts", "sanitization_collisions"}
    if requested_mode == "bidirectional" and absolute_blockers:
        error = "Native documents and shortcuts cannot use bidirectional mode."
    elif requested_mode == "bidirectional" and not risk["safe_for_bidirectional"] and not args.force_risk:
        error = (
            "Path is not safe for bidirectional sync. Use --mode download_only "
            "or --force-risk to override."
        )
    elif requested_mode not in VALID_MODES:
        error = f"Invalid mode: {requested_mode}"
    export_plan = getattr(args, "export_plan", None)
    export_error = validate_export_plan(export_plan) if export_plan is not None else None
    if export_error:
        error = f"Export plan {export_error}"
    elif export_plan and requested_mode != "download_only":
        error = "Export plans require download_only mode"
    elif export_plan and getattr(args, "target_managed", False):
        try:
            _scan_id, exists, files = cloud_files_for_path(
                args.db_path, f"/{entry}" if entry else "/")
            matches = [item for item in files
                       if item.object_id == export_plan["source_object_id"]]
        except Exception as exc:
            error = f"Export inventory is unavailable: {exc}"
        else:
            if (not exists or len(matches) != 1 or not matches[0].is_native
                    or matches[0].is_shortcut
                    or matches[0].mime_type != export_plan["source_mime_type"]):
                error = "Export object ID/MIME does not match one native object under the policy path"

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
    set_aside = getattr(args, "quarantine_unreadable", False)
    if error is None and requested_mode != "disabled" and (getattr(args, "preflight", False) or set_aside):
        probe = content_probe(args, policy, entry)
        result["content_probe"] = probe
        if probe["unreadable"] and args.apply and not set_aside:
            # Before the policy changes: a filter that names a file the cloud
            # refuses stops the sync of every folder until a resync gets through.
            result["error"] = (
                f"{len(probe['unreadable'])} of {probe['checked']} files under /{entry} "
                f"cannot be read from the cloud; nothing changed. Add with "
                f"--quarantine-unreadable to set them aside, or try again later."
            )
            result["blocked"] = True
            return result
    if args.apply and error is None:
        meta = {"mode": requested_mode}
        if export_plan:
            meta["export_plan"] = export_plan
        if requested_mode == "download_only" and risk["risks"]:
            meta["reason"] = risk["risks"][0]["code"]
            meta["risk_scan_id"] = risk["scan_id"]
        if requested_mode == "bidirectional" and args.force_risk:
            meta["forced_risk"] = True
        policy["paths"][entry] = meta
        if set_aside and result.get("content_probe", {}).get("unreadable"):
            since = datetime.now().isoformat(timespec="seconds")
            quarantine = dict(policy.get("quarantine", {}))
            for row in result["content_probe"]["unreadable"]:
                quarantine[row["path"].lstrip("/")] = {"reason": f"preflight: {row['reason']}", "since": since}
            policy["quarantine"] = quarantine
            result["quarantined"] = [row["path"] for row in result["content_probe"]["unreadable"]]

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


def put_back_from_quarantine(args: argparse.Namespace, entry: str, row: dict) -> None:
    """Fetch one quarantined file whole and set it in the mirror.

    While the entry is in quarantine the filter hides the file from bisync, so
    the copy cannot be picked up half-written. Setting it in place also means the
    resync that follows does not depend on the cloud serving it a second time.
    """
    local = os.path.join(os.path.expanduser(args.local_root), entry)
    os.makedirs(var_path(), exist_ok=True)
    fetched = var_path(f"recheck_{os.getpid()}.part")
    row.update(fetch_verified(args.remote, entry, fetched,
                              timeout=getattr(args, "timeout_sec", RECHECK_TIMEOUT_SEC) or None))
    row["readable"] = row.pop("verified")
    row["verified"] = "md5" if row["readable"] else None
    if not row["readable"]:
        return
    try:
        if os.path.exists(local):
            same = file_md5(local) == row["md5"]
            row["local"] = "same" if same else "differs"
            if not same:
                row["readable"] = False
                row["reason"] = "the local copy differs from the cloud; a person has to choose"
            return
        os.makedirs(os.path.dirname(local), exist_ok=True)
        shutil.move(fetched, local)
        row["local"] = "placed"
    finally:
        if os.path.exists(fetched):
            os.remove(fetched)


def recheck_quarantine(args: argparse.Namespace, policy: dict, result: dict) -> dict:
    """Which quarantined files the cloud hands over now.

    A dry run reads the first bytes, which says a file has started to be served.
    `--apply` fetches each whole, checks it against the cloud's md5, and only then
    takes it out of quarantine: on 2026-10-31 a probe passed files whose full
    download was never confirmed.
    """
    quarantine = dict(policy.get("quarantine", {}))
    result["action"] = "quarantine-recheck"
    entries = sorted(quarantine)
    if args.path is not None:
        entry = normalize_entry(args.path)
        if entry not in quarantine:
            result["error"] = f"/{entry} is not in quarantine"
            return result
        entries = [entry]
    rows: List[dict] = []
    if args.apply:
        for entry in entries:
            row = {"path": f"/{entry}", "since": quarantine[entry].get("since")}
            put_back_from_quarantine(args, entry, row)
            rows.append(row)
    else:
        for entry, probe in zip(entries, probe_files(args.remote, [(entry, None) for entry in entries])):
            rows.append({"path": f"/{entry}", "since": quarantine[entry].get("since"),
                         "readable": probe["readable"], "reason": probe["reason"],
                         "verified": "first_bytes" if probe["readable"] else None})
    result["recheck"] = rows
    result["readable"] = [row["path"] for row in rows if row["readable"]]
    result["still_unreadable"] = [row["path"] for row in rows if not row["readable"]]
    if not args.apply:
        if result["readable"]:
            result["next"] = ["sync_policy.py quarantine --recheck --backend rclone --apply"]
        return result
    if result["readable"]:
        for path in result["readable"]:
            quarantine.pop(path.lstrip("/"))
        if quarantine:
            policy["quarantine"] = quarantine
        else:
            policy.pop("quarantine", None)
        policy["updated_at"] = datetime.now().isoformat()
        write_policy(args.policy_path, policy)
        result["next"] = ["sync_policy.py render-filters --apply",
                          "sync_bisync.py resync --apply  (the filter changed)"]
    result["quarantine"] = quarantine
    return result


def import_file(args: argparse.Namespace, policy: dict) -> dict:
    """Set a file obtained elsewhere into the mirror, if it is the cloud's file.

    For a file the cloud lists but will not serve, when another device still has
    it. Getting it to this machine is the caller's business, and the design is in
    tasks/agent_interface/DESIGN_IMPORT.md. Nothing here touches the cloud or any
    other device, and a copy that does not match the cloud's md5 is refused.
    """
    entry = normalize_entry(args.path)
    source = os.path.expanduser(args.source)
    local = os.path.join(os.path.expanduser(args.local_root), entry)
    result = {"schema": SCHEMA, "action": "import-file", "dry_run": not args.apply,
              "policy_path": args.policy_path, "path": f"/{entry}", "source": source,
              "destination": local, "error": None}
    if not any(entry == p or entry.startswith(p + "/") for p in effective_download_paths(policy)):
        result["error"] = f"/{entry} is not inside any synced path"
        return result
    if not os.path.isfile(source):
        result["error"] = f"Source is not a file: {source}"
        return result
    info = cloud_file_info(args.remote, entry)
    if info["error"]:
        result["error"] = f"Cannot read the cloud's record of /{entry}: {info['error']}"
        return result
    if not info["md5"]:
        result["error"] = f"The cloud gave no md5 for /{entry}; nothing to check against"
        return result
    actual = file_md5(source)
    result.update({"md5": actual, "cloud_md5": info["md5"], "matches_cloud": actual == info["md5"].lower()})
    if not result["matches_cloud"]:
        result["error"] = f"Source is not the cloud's file: md5 {actual}, cloud {info['md5']}"
        return result
    if os.path.exists(local):
        if file_md5(local) == actual:
            result["already_there"] = True
            return result
        result["error"] = f"A different file is already at {local}; a person has to choose"
        return result
    if quarantined_by(entry, policy.get("quarantine")):
        result["next"] = [f"sync_policy.py quarantine --path /{entry} --remove --backend rclone --apply",
                          "sync_policy.py render-filters --apply",
                          "sync_bisync.py resync --apply  (the filter changed)"]
    if not args.apply:
        return result
    # bisync must not see this half-written, and outside quarantine no filter
    # hides it, so the sync's own lock covers the copy.
    from tools.sync_bisync import LOCK_PATH
    lock = acquire_lock(LOCK_PATH)
    if not lock.acquired:
        result["error"] = f"A bisync run is in progress (PID: {lock.holder_pid}); try again after it"
        result["blocked"] = True
        return result
    try:
        os.makedirs(os.path.dirname(local), exist_ok=True)
        shutil.copyfile(source, local)
        stamp = datetime.fromisoformat(re.sub(r"(\.\d{6})\d*", r"\1", info["modtime"]).replace("Z", "+00:00"))
        os.utime(local, (stamp.timestamp(), stamp.timestamp()))
    finally:
        release_lock(LOCK_PATH)
    result["imported"] = True
    return result


def quarantine_policy_path(args: argparse.Namespace) -> dict:
    """Set a file aside from sync, or put it back. Policy only: the filter files
    change on the next `render-filters --apply`, and bisync then wants a resync,
    because its baseline was built with the old rules."""
    policy = load_policy(args.policy_path)
    result = {"schema": SCHEMA, "action": "quarantine", "dry_run": not args.apply,
              "policy_path": args.policy_path, "error": None}
    if not policy:
        result["error"] = f"Policy not found: {args.policy_path}"
        return result
    quarantine = dict(policy.get("quarantine", {}))
    if getattr(args, "recheck", False):
        return recheck_quarantine(args, policy, result)
    if args.path is None:
        result["quarantine"] = quarantine
        return result
    entry = normalize_entry(args.path)
    result["path"] = f"/{entry}"
    if not entry:
        result["error"] = "Refusing to quarantine the root"
        return result
    covering = [p for p in effective_download_paths(policy)
                if entry == p or entry.startswith(p + "/")]
    if args.remove:
        if entry not in quarantine:
            result["error"] = f"/{entry} is not in quarantine"
            return result
        result["removed"] = quarantine.pop(entry)
    else:
        if not args.reason:
            result["error"] = "--reason is required: the entry has to say why, or nobody will know when to take it out"
            return result
        if not covering:
            result["error"] = f"/{entry} is not inside any synced path; quarantining it changes nothing"
            return result
        quarantine[entry] = {"reason": args.reason, "since": datetime.now().isoformat(timespec="seconds")}
        result["added"] = quarantine[entry]
    result["quarantine"] = quarantine
    result["next"] = ["sync_policy.py render-filters --apply",
                      "sync_bisync.py resync --apply  (the filter changed)"]
    if args.apply:
        if quarantine:
            policy["quarantine"] = quarantine
        else:
            policy.pop("quarantine", None)
        policy["updated_at"] = datetime.now().isoformat()
        write_policy(args.policy_path, policy)
    return result


IGNORE_PATTERN_FORBIDDEN = ("/", "**")


def ignore_policy_pattern(args: argparse.Namespace) -> dict:
    """Leave generated files under a synced path out of sync: build output that
    every device makes for itself and that would otherwise travel back and forth
    on every run between two bisync clients. Policy only, like quarantine: the
    filters change on `render-filters --apply`, and bisync then wants a resync."""
    policy = load_policy(args.policy_path)
    result = {"schema": SCHEMA, "action": "ignore", "dry_run": not args.apply,
              "policy_path": args.policy_path, "error": None}
    if not policy:
        result["error"] = f"Policy not found: {args.policy_path}"
        return result
    ignore = {base: list(patterns) for base, patterns in policy.get("ignore", {}).items()}
    if args.path is None:
        result["ignore"] = ignore
        return result
    base = normalize_entry(args.path)
    result["path"] = f"/{base}"
    if not base or not any(base == p or base.startswith(p + "/") for p in effective_download_paths(policy)):
        result["error"] = f"/{base} is not a synced path or inside one"
        return result
    patterns = args.pattern or []
    if not patterns:
        result["error"] = "--pattern is required (e.g. build/  __pycache__/  *.log)"
        return result
    for pattern in patterns:
        core = pattern[:-1] if pattern.endswith("/") else pattern
        if not core or any(bad in core for bad in IGNORE_PATTERN_FORBIDDEN) or core in (".", ".."):
            result["error"] = (f"Pattern {pattern!r}: a name or a name glob, optionally ending "
                               f"in '/' for a directory; no path separators and no '**'")
            return result
    current = set(ignore.get(base, []))
    if args.remove:
        missing = [p for p in patterns if p not in current]
        if missing:
            result["error"] = f"Not ignored under /{base}: {', '.join(missing)}"
            return result
        current -= set(patterns)
    else:
        current |= set(patterns)
    if current:
        ignore[base] = sorted(current)
    else:
        ignore.pop(base, None)
    result["ignore"] = ignore
    result["rules"] = ignore_rules({base: ignore[base]}) if base in ignore else []
    result["next"] = ["sync_policy.py render-filters --apply",
                      "sync_bisync.py resync --apply  (the filter changed)"]
    if args.apply:
        if ignore:
            policy["ignore"] = ignore
        else:
            policy.pop("ignore", None)
        policy["updated_at"] = datetime.now().isoformat()
        write_policy(args.policy_path, policy)
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
        if payload.get("download_filter") is not None:
            print(payload["download_filter"], end="")
        elif payload.get("download_filter_disabled"):
            print(f"download_filter: disabled ({payload['download_filter_disabled']})")
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
        probe = payload.get("content_probe")
        if probe:
            print(f"content_probe: {probe['readable']} of {probe['checked']} readable")
            for row in probe["unreadable"]:
                print(f"  unreadable: {row['path']} ({row['reason']})")
        backend_apply = payload.get("backend_apply")
        if backend_apply:
            print(f"backend: {backend_apply.get('backend')}")
            print(f"backend_action: {backend_apply.get('action')}")
            if backend_apply.get("error"):
                print(f"backend_error: {backend_apply['error']}")
        for step in payload.get("next", []):
            print(f"next: {step}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Policy-aware sync management for rclone backend")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common_flags(sub):
        sub.add_argument("--db-path", default=str(ROOT_DIR / "monitor.db"))
        sub.add_argument("--local-root", default=DEFAULT_CONFIG["local_root"])
        sub.add_argument("--remote", default=DEFAULT_CONFIG["rclone_remote"])
        sub.add_argument("--target", default=None)
        sub.add_argument("--targets-file", default=None)
        sub.add_argument("--provider", default=None)
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
    render_filters_parser.add_argument(
        "--reset-daemon-index", action="store_true",
        help="daemon backend: move the daemon's index aside before restarting, so "
             "a path it still remembers is downloaded instead of deleted in the cloud")

    plan = subparsers.add_parser("download-plan")
    common_flags(plan)
    apply_download = subparsers.add_parser("download-apply")
    common_flags(apply_download)
    apply_download.add_argument("--apply", action="store_true")
    apply_download.add_argument(
        "--timeout-sec", type=float, default=DOWNLOAD_TIMEOUT_SEC,
        help="stop the whole rclone copy after this long; 0 for no limit")

    native_export = subparsers.add_parser("native-export")
    native_export.add_argument("--path", required=True)
    native_export.add_argument("--apply", action="store_true")
    common_flags(native_export)

    add = subparsers.add_parser("add")
    add.add_argument("--path", required=True)
    add.add_argument("--mode", choices=["auto", "bidirectional", "download_only", "disabled"], default="auto")
    add.add_argument("--apply", action="store_true")
    add.add_argument(
        "--reset-daemon-index", action="store_true",
        help="daemon backend: move the daemon's index aside before restarting, so "
             "a path it still remembers is downloaded instead of deleted in the cloud")
    add.add_argument("--force-risk", action="store_true")
    add.add_argument("--export-object-id", default=None)
    add.add_argument("--export-mime-type", default=None)
    add.add_argument("--export-artifact-root", default=None)
    add.add_argument(
        "--preflight", action="store_true",
        help="read the first bytes of every file under the path before changing "
             "anything; with --apply, refuse if any cannot be read")
    add.add_argument(
        "--quarantine-unreadable", action="store_true",
        help="preflight, and quarantine what cannot be read instead of refusing")
    add.add_argument("--probe-workers", type=int, default=PROBE_WORKERS)
    add.add_argument("--probe-timeout", type=float, default=PROBE_TIMEOUT_SEC,
                     help="seconds to wait for one file")
    common_flags(add)

    quarantine = subparsers.add_parser(
        "quarantine", help="set a file aside from sync (the cloud will not hand it over), or put it back")
    quarantine.add_argument("--path", default=None, help="omit to list the quarantine")
    quarantine.add_argument("--reason", default=None)
    quarantine.add_argument("--remove", action="store_true")
    quarantine.add_argument(
        "--recheck", action="store_true",
        help="does the cloud serve them now? Reads the first bytes; with --apply fetches "
             "each whole, checks the md5, and only then puts it back")
    quarantine.add_argument("--timeout-sec", type=float, default=RECHECK_TIMEOUT_SEC,
                            help="--recheck --apply: seconds for one file; 0 for no limit")
    quarantine.add_argument("--apply", action="store_true")
    common_flags(quarantine)

    import_parser = subparsers.add_parser(
        "import-file", help="set a file obtained elsewhere into the mirror, if its md5 matches the cloud's")
    import_parser.add_argument("--source", required=True, help="the file, on this machine")
    import_parser.add_argument("--path", required=True, help="where it lives in the cloud")
    import_parser.add_argument("--apply", action="store_true")
    common_flags(import_parser)

    ignore = subparsers.add_parser(
        "ignore", help="leave generated files (build/, __pycache__/, *.log) under a synced path out of sync")
    ignore.add_argument("--path", default=None, help="omit to list")
    ignore.add_argument("--pattern", action="append", default=None)
    ignore.add_argument("--remove", action="store_true")
    ignore.add_argument("--apply", action="store_true")
    common_flags(ignore)

    remove = subparsers.add_parser("remove")
    remove.add_argument("--path", required=True)
    remove.add_argument("--apply", action="store_true")
    remove.add_argument(
        "--reset-daemon-index", action="store_true",
        help="daemon backend: move the daemon's index aside before restarting, so "
             "a path it still remembers is downloaded instead of deleted in the cloud")
    common_flags(remove)

    return parser.parse_args(place_format_flag(sys.argv[1:], before_subcommand=False))


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


def _policy_copy_on_live_daemon(args: argparse.Namespace) -> Optional[str]:
    """Refuse `--apply` that would carry a rehearsal policy onto the live daemon.

    `--policy-path` only chooses which policy file is written; the daemon backend
    still rewrites `--exclude-config`, which defaults to the live
    ~/.config/yandex-disk/config.cfg, and restarts the daemon. On 2026-10-29 an
    `add --apply --policy-path <copy>` meant as a rehearsal did exactly that and
    ran the real daemon with /Books out of the exclude list for 18 seconds.
    """
    if not getattr(args, "apply", False) or getattr(args, "target_managed", False):
        return None
    if args.command not in ("add", "remove", "render-filters"):
        return None
    live_config = os.path.abspath(os.path.expanduser(DEFAULT_CONFIG["exclude_config"]))
    config = os.path.abspath(os.path.expanduser(
        getattr(args, "exclude_config", None) or DEFAULT_CONFIG["exclude_config"]))
    policy = os.path.abspath(os.path.expanduser(args.policy_path))
    if config != live_config or policy == os.path.abspath(default_policy_path()):
        return None
    if _resolve_backend_name(args) != "daemon":
        return None
    return (
        f"--policy-path {args.policy_path} is not the live policy, but --apply "
        f"would rewrite the live daemon config {DEFAULT_CONFIG['exclude_config']} "
        f"and restart the daemon. To rehearse, also pass --exclude-config <copy>; "
        f"to change the real sync, drop --policy-path."
    )


def _apply_backend_policy(args: argparse.Namespace, policy: dict) -> dict:
    """Apply policy via the selected backend."""
    backend = backend_from_args(args)
    if getattr(args, "reset_daemon_index", False):
        if backend.kind != "daemon":
            raise ValueError("--reset-daemon-index applies to the yandex-disk daemon backend only")
        return backend.apply_policy(policy, dry_run=not args.apply, reset_index=True)
    return backend.apply_policy(policy, dry_run=not args.apply)


class restoring_policy:
    """Put the policy file back if the backend refuses what was written to it.

    `add` and `remove` write the policy first and apply it second. A refused
    apply used to leave the policy describing a sync the daemon never got —
    and the next apply of anything would carry it along unchecked.
    """

    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, "rb") as handle:
                self.saved: Optional[bytes] = handle.read()
        except FileNotFoundError:
            self.saved = None

    def restore(self) -> None:
        if self.saved is None:
            if os.path.exists(self.path):
                os.remove(self.path)
            return
        with open(self.path, "wb") as handle:
            handle.write(self.saved)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.restore()
        return False


def next_after_policy_change(args: argparse.Namespace, payload: dict) -> List[str]:
    """What `add` and `remove` leave to do on an rclone machine. The filters are
    already rendered; the daemon needs no second step at all."""
    if payload.get("error") or getattr(args, "target_managed", False):
        return []
    try:
        if _resolve_backend_name(args) != "rclone":
            return []
    except Exception:
        return []
    if payload.get("action") == "add" and payload.get("mode") == "download_only":
        return ["sync_policy.py download-plan", "sync_policy.py download-apply --apply"]
    return ["sync_bisync.py resync --apply  (the filter changed)"]


def _target_managed_backend_skip(args: argparse.Namespace) -> dict:
    """Explain why policy edits do not implicitly mutate a managed backend."""
    return {
        "skipped": True,
        "reason": (
            "target-managed policy edits are state-only; use download-plan/"
            "download-apply for download-only paths"
        ),
        "target": getattr(args, "target", None),
    }


def main() -> int:
    args = parse_args()
    try:
        configure_target_args(args)
    except (TargetConfigError, OSError) as exc:
        payload = {"schema": SCHEMA, "action": args.command, "dry_run": True,
                   "policy_path": getattr(args, "policy_path", None), "error": str(exc)}
        render(payload, args.format, args.text_header)
        return exit_code(payload)
    args.local_root = require_local_root(args.local_root, tool="sync_policy.py")
    try:
        refusal = _policy_copy_on_live_daemon(args)
        if refusal:
            payload = {"schema": SCHEMA, "action": args.command, "dry_run": True,
                       "policy_path": args.policy_path, "error": refusal}
            render(payload, args.format, args.text_header)
            return exit_code(payload)
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
                if (args.apply and payload.get("error") is None
                        and not getattr(args, "target_managed", False)):
                    backend_payload = _apply_backend_policy(args, policy)
                    payload["backend_apply"] = backend_payload
        elif args.command == "download-plan":
            policy = load_policy(args.policy_path)
            payload = (download_plan(args, policy) if policy else {
                "schema": "ydm_download_plan:v1", "action": "download-plan", "dry_run": True,
                "error": f"Policy not found: {args.policy_path}",
            })
        elif args.command == "download-apply":
            policy = load_policy(args.policy_path)
            payload = (download_apply(args, policy) if policy else {
                "schema": "ydm_download_plan:v1", "action": "download-apply", "dry_run": not args.apply,
                "error": f"Policy not found: {args.policy_path}"})
        elif args.command == "native-export":
            policy = load_policy(args.policy_path)
            payload = (native_export_action(args, policy) if policy else {
                "schema": "ydm_native_export_result:v1", "action": "native-export",
                "dry_run": not args.apply, "error": f"Policy not found: {args.policy_path}"})
        elif args.command == "import-file":
            policy = load_policy(args.policy_path)
            payload = (import_file(args, policy) if policy else {
                "schema": SCHEMA, "action": "import-file", "dry_run": not args.apply,
                "error": f"Policy not found: {args.policy_path}"})
        elif args.command == "ignore":
            payload = ignore_policy_pattern(args)
        elif args.command == "quarantine":
            payload = quarantine_policy_path(args)
        elif args.command == "add":
            export_object_id = getattr(args, "export_object_id", None)
            export_mime_type = getattr(args, "export_mime_type", None)
            if export_object_id or export_mime_type:
                if not (export_object_id and export_mime_type):
                    raise ValueError("native export requires both --export-object-id and --export-mime-type")
                if not getattr(args, "target_managed", False) or args.provider != "google_drive":
                    raise ValueError("native export planning requires a target-managed Google Drive target")
                if getattr(args, "native_docs", None) != "export":
                    raise ValueError("target native_docs must be export")
                from tools.providers import provider_for
                artifact_root = (getattr(args, "export_artifact_root", None)
                                 or os.path.join(args.local_root, "native-exports"))
                args.export_plan = provider_for(args.provider).export_plan(
                    object_id=export_object_id,
                    mime_type=export_mime_type,
                    artifact_root=artifact_root,
                )
            else:
                args.export_plan = None
            with restoring_policy(args.policy_path) as saved:
                payload = add_policy_path(args)
                if args.apply and payload.get("error") is None:
                    if getattr(args, "target_managed", False):
                        payload["backend_apply"] = _target_managed_backend_skip(args)
                    else:
                        policy = load_policy(args.policy_path)
                        if policy:
                            backend_payload = _apply_backend_policy(args, policy)
                            payload["backend_apply"] = backend_payload
                            if backend_payload.get("error") and not backend_payload.get("index_backup"):
                                saved.restore()
                steps = next_after_policy_change(args, payload)
                if steps:
                    payload["next"] = steps
        else:
            with restoring_policy(args.policy_path) as saved:
                payload = remove_policy_path(args)
                if args.apply and payload.get("error") is None:
                    if getattr(args, "target_managed", False):
                        payload["backend_apply"] = _target_managed_backend_skip(args)
                    else:
                        policy = load_policy(args.policy_path)
                        if policy:
                            backend_payload = _apply_backend_policy(args, policy)
                            payload["backend_apply"] = backend_payload
                            if backend_payload.get("error") and not backend_payload.get("index_backup"):
                                saved.restore()
                steps = next_after_policy_change(args, payload)
                if steps:
                    payload["next"] = steps
    except Exception as exc:
        payload = {
            "schema": SCHEMA,
            "action": args.command,
            "dry_run": True,
            "policy_path": getattr(args, "policy_path", default_policy_path()),
            "error": str(exc),
        }
    render(payload, args.format, args.text_header)
    return exit_code(payload)


if __name__ == "__main__":
    sys.exit(main())
