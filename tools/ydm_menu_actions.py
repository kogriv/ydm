#!/usr/bin/env python3
"""Sync actions orchestration for YDM menu."""
from __future__ import annotations

import argparse
import json
import os
import shlex
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
    normalize_entry,
    policy_paths_by_mode,
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


def _preview_lines_daemon(path: str, mode: str, delta: dict) -> List[str]:
    before, after = delta["before"], delta["after"]
    # "Adding X as disabled" is the policy's vocabulary, not the operator's:
    # under a blacklist that operation is an exclusion, and menu 4 reaches it.
    headline = (
        f"\nExcluding {path} will change the daemon config:" if mode == "disabled"
        else f"\nAdding {path} as {mode} will change the daemon config:"
    )
    lines = [
        headline,
        f"  exclude-dirs: {len(before)} -> {len(after)}",
    ]
    removed = delta.get("removed") or []
    if removed:
        # The line that would have shown /Books on 2026-08-14: a folder that
        # was excluded and is about to start syncing for real.
        lines.append("  no longer excluded (starts syncing):  "
                     + _joined(removed))
    added = delta.get("added") or []
    if added:
        lines.append("  newly excluded:                       "
                     + _joined(added))
    if not removed and not added:
        lines.append("  (no change to the exclude list)")
    return lines


def _preview_lines_rclone(path: str, mode: str, delta: dict) -> List[str]:
    lines = [f"\nAdding {path} as {mode} will change the rclone filters:"]
    for label, key in (("Download filter", "download"), ("Bisync filter", "bisync")):
        before = delta[f"{key}_before"]
        after = delta[f"{key}_after"]
        lines.append(f"  {label}: {len(before)} -> {len(after)} folders")
        for entry in sorted(set(after) - set(before)):
            lines.append(f"    + {entry}")
        for entry in sorted(set(before) - set(after)):
            lines.append(f"    - {entry}")
    return lines


def _joined(entries: List[str], limit: int = 4) -> str:
    head = ", ".join(entries[:limit])
    rest = len(entries) - limit
    return f"{head} (+{rest})" if rest > 0 else head


def preview_add(
    cfg: MenuConfig,
    path: str,
    mode: str,
    *,
    force_risk: bool = False,
) -> ActionResult:
    """What adding `path` would change — computed without changing anything.

    Removal has three barriers and adding had none, although adding is what
    caused the 2026-08-14 incident. The barrier this adds is not another
    "are you sure": it is the delta itself. On 14.08 `action_inspect()` saw no
    risk — the path existed and was materialized — so a risk-gated prompt would
    have stayed silent. What would have spoken is `no longer excluded: Books`.

    The prospective policy is built by copying the real one to a temporary
    file and running the production `add_policy_path(apply=True)` against the
    copy. That is deliberate: the daemon coercion that drops a disabled
    ancestor lives in there, and reimplementing it here to "just compute" the
    result would put a second copy of the 14.08 logic next to the first.

    Neither the policy file nor the daemon config is touched: the write lands
    on the copy, and `apply_policy(dry_run=True)` reports without applying.
    """
    import shutil
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="ydm_preview_")
    try:
        tmp_policy = os.path.join(tmpdir, "policy.json")
        real_policy = os.path.expanduser(cfg.policy_path)
        if os.path.exists(real_policy):
            shutil.copyfile(real_policy, tmp_policy)

        ns = _policy_ns(cfg, path=path, mode=mode, apply=True,
                        force_risk=force_risk, policy_path=tmp_policy)
        added = add_policy_path(ns)
        if added.get("error"):
            return ActionResult(False, added["error"], {"policy_add": added})
        prospective = load_policy(tmp_policy)
        if not prospective:
            return ActionResult(
                False,
                f"Could not work out what adding {path} would do.",
                {"policy_add": added},
            )
        try:
            backend = backend_from_args(ns)
            after = backend.apply_policy(prospective, dry_run=True)
            current = load_policy(real_policy)
            before = (
                backend.apply_policy(current, dry_run=True) if current else None
            )
        except Exception as exc:
            return ActionResult(False, f"Could not preview the change: {exc}",
                                {"policy_add": added})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if cfg.backend_kind == "daemon":
        delta = {
            "kind": "daemon",
            "before": after.get("before") or [],
            "after": after.get("after") or [],
            "added": after.get("added") or [],
            "removed": after.get("removed") or [],
            "deletion_risk_paths": after.get("deletion_risk_paths") or [],
            "clears_exclude_dirs": bool(after.get("clears_exclude_dirs")),
        }
        lines = _preview_lines_daemon(path, mode, delta)
    else:
        download_after = after.get("download_paths") or []
        bisync_after = after.get("bisync_paths") or []
        download_before = (before or {}).get("download_paths") or []
        bisync_before = (before or {}).get("bisync_paths") or []
        union_before = set(download_before) | set(bisync_before)
        union_after = set(download_after) | set(bisync_after)
        delta = {
            "kind": "rclone",
            "download_before": download_before,
            "download_after": download_after,
            "bisync_before": bisync_before,
            "bisync_after": bisync_after,
            "added": sorted(union_after - union_before),
            "removed": sorted(union_before - union_after),
            "deletion_risk_paths": [],
            "clears_exclude_dirs": False,
        }
        lines = _preview_lines_rclone(path, mode, delta)

    # These two are filled in on a dry run and *raised* on a real one, so
    # saying it here is the difference between a warning and a rejection after
    # the person has already committed.
    if delta["clears_exclude_dirs"]:
        lines.append("  WARN: this would empty the exclude list, and the real "
                     "apply would be refused — import the current exclusions "
                     "first (sync_policy.py migrate --backend daemon --apply).")
    if delta["deletion_risk_paths"]:
        lines.append("  WARN: applying would be refused — these stay in sync "
                     "but are missing locally, so the daemon would delete them "
                     "in the cloud: "
                     + _joined(delta["deletion_risk_paths"]))

    return ActionResult(
        True,
        "\n".join(lines),
        {"policy_add": added, "delta": delta, "backend": cfg.backend_name},
    )


def confirm_and_add(
    cfg: MenuConfig,
    path: str,
    mode: str,
    *,
    reader,
) -> Optional[ActionResult]:
    """Show the delta, ask once, then add. Returns None when declined."""
    from tools.ydm_menu_prompts import prompt_yes_no

    preview = preview_add(cfg, path, mode)
    print(preview.message)
    if not preview.ok:
        return preview
    if not prompt_yes_no("Apply?", default=False, reader=reader):
        print("Cancelled.")
        return None
    return action_add(cfg, path, mode)


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


def covering_exclusion(entry: str, excluded) -> Optional[str]:
    """The exclusion that already covers `entry`, shallowest first.

    A blacklist excludes a whole subtree, so `Books/Math` is out of sync the
    moment `Books` is — with no entry of its own. Anything that offers to
    exclude a path has to know that, or it offers a change that changes
    nothing and reports success.
    """
    entry = entry.strip("/")
    for candidate in sorted(excluded, key=lambda item: (item.count("/"), item)):
        if candidate and (entry == candidate or entry.startswith(candidate + "/")):
            return candidate
    return None


def daemon_excluded_entries(cfg: MenuConfig) -> List[str]:
    """Everything currently out of the daemon's scope.

    The union of two sources that are supposed to agree: the policy, which is
    what the next apply will write, and the `exclude-dirs` line the daemon is
    running on right now. When they disagree the policy is behind — the case
    `clears_exclude_dirs` refuses an apply over — and taking the union keeps
    the menu from offering to "stop syncing" something already stopped.
    """
    policy = load_policy(cfg.policy_path) or {}
    entries = set(policy_paths_by_mode(policy, "disabled"))
    try:
        backend = backend_from_args(_policy_ns(cfg))
        live = backend.apply_policy(policy, dry_run=True).get("before") or []
        entries.update(live)
    except Exception:
        # An unreadable config is not a reason to hide the screen; the policy
        # alone still describes what the next apply would do.
        pass
    return sorted(entries, key=str.lower)


def daemon_sync_scope(cfg: MenuConfig) -> dict:
    """{"synced": [...], "excluded": [...]} — top level, as the daemon sees it.

    There is no list of synced folders to read anywhere: the daemon syncs
    everything `exclude-dirs` misses, so the list has to be reconstructed. Two
    sources, and both are needed — the cloud snapshot, and the local disk. A
    folder created locally and never scanned is inside the daemon's scope too,
    and it is the likeliest one to be there by mistake.
    """
    excluded = daemon_excluded_entries(cfg)
    names = {path.strip("/") for path in snapshot_top_level(cfg, limit=100_000)}
    try:
        for name in os.listdir(cfg.local_root):
            if name.startswith("."):
                continue
            if os.path.isdir(os.path.join(cfg.local_root, name)):
                names.add(name)
    except OSError:
        pass
    synced = [
        f"/{name}" for name in sorted(names, key=str.lower)
        if name and not covering_exclusion(name, excluded)
    ]
    return {"synced": synced, "excluded": excluded}


def policy_children_of(cfg: MenuConfig, path: str) -> List[str]:
    """Policy entries beneath `path` that are not exclusions themselves.

    `_policy_to_exclude_dirs()` simply drops `bidirectional` entries, so a
    child left in the policy under a newly excluded parent stops syncing
    without a word anywhere. Naming them is the whole of the fix.
    """
    entry = normalize_entry(path)
    policy = load_policy(cfg.policy_path) or {}
    return sorted(
        child for child, meta in (policy.get("paths") or {}).items()
        if child.startswith(entry + "/") and meta.get("mode") != "disabled"
    )


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
    from tools.ydm_menu_status import menu_exclude_dirs
    from ydm import Analyzer

    print("Comparing the cloud snapshot with the local copy…")
    try:
        analyzer = Analyzer(create_storage(cfg.db_path))
        # Whose exclusions, said out loud. `get_diff()` on its own reads the
        # daemon's config from the default path — right for the CLI, wrong for
        # a menu that knows which backend it is on.
        result = analyzer.get_diff(exclude_dirs=menu_exclude_dirs(cfg))
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


def _trash_top_level(cfg: MenuConfig, limit: int = 30) -> List[dict]:
    """What is sitting at the top of the Disk trash, newest deletion first."""
    from tools.trash_scan import YandexTrashClient, resolve_token

    token = resolve_token("auto", cfg.remote)
    embedded = YandexTrashClient(token).get_resources("/", limit=limit)
    items = list(embedded.get("items") or [])
    items.sort(key=lambda item: str(item.get("deleted") or ""), reverse=True)
    return items


def _origin_of(item: dict) -> str:
    """`disk:/Books` -> `/Books`, which is what --restore-root wants."""
    origin = str(item.get("origin_path") or "")
    for prefix in ("disk:", "trash:"):
        if origin.startswith(prefix):
            origin = origin[len(prefix):]
    return origin or "/"


def print_trash_overview(cfg: MenuConfig) -> None:
    """The trash, and the exact commands to act on it.

    This is the one screen meant to be read under stress: the files are
    already gone. What is missing at that moment is not courage, it is the
    hashed name — `trash_scan.py scan` wants `--trash-root trash:/Books_<hash>`
    and nobody knows theirs. So the menu supplies the names and prints the
    commands; restoring stays in the CLI, because it changes data and should
    say so out loud. See tasks/ydm_menu/DESIGN-2026-08-24.md, 8.8.
    """
    print("\nYandex Disk trash")
    try:
        items = _trash_top_level(cfg)
    except (Exception, SystemExit) as exc:
        # resolve_token() raises SystemExit, which `except Exception` misses —
        # the same trap the smart cloud scan hit. Exiting the menu out from
        # under someone hunting for deleted files is the worst possible moment.
        print(f"  Could not read the trash: {exc}")
        print("  Needs a token: put it in .env, or use "
              "--token-source rclone with tools/trash_scan.py.")
        return
    if not items:
        print("  The trash is empty.")
        return

    print(f"  {len(items)} entr{'y' if len(items) == 1 else 'ies'}, "
          "most recently deleted first:\n")
    for index, item in enumerate(items, start=1):
        name = item.get("name") or "?"
        kind = "/" if item.get("type") == "dir" else " "
        deleted = str(item.get("deleted") or "")[:16].replace("T", " ")
        print(f" {index:2d}  {name}{kind}")
        print(f"     deleted {deleted}   from {_origin_of(item)}")

    print("\n  To inspect, then restore (the second one writes):")
    for item in items[:3]:
        # Quoted, because these lines exist to be pasted. The recovery from
        # 2026-08-14 left `Books (1)` in the trash — the name Yandex gives a
        # restore that collides with an existing folder — and unquoted, the
        # space splits the argument while `(1)` is a shell metacharacter.
        trash_root = shlex.quote(item.get("path") or f"trash:/{item.get('name')}")
        restore_root = shlex.quote(_origin_of(item))
        print(f"\n    # {item.get('name')}")
        print(f"    python3 tools/trash_scan.py scan "
              f"--trash-root {trash_root} --restore-root {restore_root}")
        print(f"    python3 tools/trash_scan.py restore-root "
              f"--trash-root {trash_root} --restore-root {restore_root} --apply")
    if len(items) > 3:
        print(f"\n  ({len(items) - 3} more — same two commands, "
              "with that entry's own paths.)")


def _human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _database_facts(cfg: MenuConfig) -> dict:
    """Size, scan count, and how much of it prune would drop.

    `prune` deliberately gets no menu entry — it is housekeeping, wanted twice
    a year, and an entry for it would only lengthen the list. But the database
    grows with every scan and nothing was telling anyone, so the fact goes
    where people already look. Measured at 0.19 s on a 138 MB, 53-scan
    database, which a detailed-status screen can afford.
    """
    import sqlite3

    db_path = os.path.expanduser(cfg.db_path)
    facts = {
        "path": db_path,
        "size_bytes": os.path.getsize(db_path),
        "scans": 0,
        "prunable_scans": None,
        "prunable_share_percent": None,
    }
    conn = sqlite3.connect(db_path)
    try:
        facts["scans"] = conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
    finally:
        conn.close()
    try:
        if str(ROOT_DIR) not in sys.path:
            sys.path.insert(0, str(ROOT_DIR))
        import ydm

        plan = ydm.Analyzer(
            ydm.StorageManager(db_path, use_temp_storage=False)
        ).prune_plan()
        facts["prunable_scans"] = plan["prunable_scans"]
        facts["prunable_share_percent"] = plan["prunable_share_percent"]
    except Exception:
        # An unreadable plan costs one number, not the screen.
        pass
    return facts


def print_database_line(cfg: MenuConfig) -> None:
    try:
        facts = _database_facts(cfg)
    except Exception as exc:
        print(f"Database: unreadable ({exc}) -> python3 ydm.py report prune")
        return
    size = _human_size(facts["size_bytes"])
    if facts["prunable_scans"] is None:
        droppable = "prunable: unknown"
    else:
        droppable = (f"{facts['prunable_scans']} prunable "
                     f"({facts['prunable_share_percent']}% of rows)")
    print(f"Database: {size}, {facts['scans']} scans, {droppable}"
          f" -> python3 ydm.py report prune")


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
    print_database_line(cfg)
    bisync = cmd_status(_bisync_ns(cfg))
    for line in (bisync.get("log_tail") or [])[-5:]:
        print(f"  log: {line}")
