#!/usr/bin/env python3
"""YDM interactive menu for human operators (agents keep ydm-sync-* CLI)."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Callable, List, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.ydm_menu_actions import (  # noqa: E402
    action_add,
    action_bisync_run,
    action_inspect,
    action_remove,
    action_resync,
    check_cloud_changes,
    cloud_list_dirs,
    confirm_and_add,
    covering_exclusion,
    daemon_sync_scope,
    handle_blocked_add,
    list_stale_folders,
    policy_children_of,
    offer_resync_if_needed,
    preview_add,
    print_bisync_scope,
    print_cloud_local_diff,
    print_detailed_status,
    print_trash_overview,
    run_cloud_scan,
    run_sync_tree,
    snapshot_top_level,
)
from tools.sync_backends import BackendError  # noqa: E402
from tools.sync_policy import VALID_MODES  # noqa: E402
from tools.ydm_menu_config import MenuConfig  # noqa: E402
from tools.ydm_menu_orphans import (  # noqa: E402
    OrphanEntry,
    browse_rows,
    cloud_scan_available,
    format_browse_row,
    list_orphan_paths,
    orphans_to_json,
    parse_level_input,
)
from tools.ydm_menu_prompts import (  # noqa: E402
    default_reader,
    pause,
    prompt_confirm_name,
    prompt_int,
    prompt_ints,
    prompt_line,
    prompt_yes_no,
)
from tools.ydm_menu_screens import render_main_menu, render_short_help  # noqa: E402
from tools.ydm_menu_status import load_status  # noqa: E402
from tools.sync_policy import (  # noqa: E402
    load_policy,
    normalize_entry,
    policy_paths_by_mode,
)


Reader = Callable[[str], str]


def screen_tree(cfg: MenuConfig, reader: Reader) -> None:
    # Built from the snapshot, not typed in: see snapshot_top_level().
    presets = [("/", 4)] + [(p, 3) for p in snapshot_top_level(cfg, limit=4)]
    print("Tree presets:")
    for i, (path, depth) in enumerate(presets, start=1):
        print(f" {i}  {path} depth {depth}")
    custom = len(presets) + 1
    print(f" {custom}  Custom")
    print("  0  Back")
    choice = prompt_int("Choose", reader=reader)
    if choice is None:
        return
    if choice == custom:
        path = prompt_line("Cloud path [/]: ", reader=reader) or "/"
        depth = prompt_int("Depth", default=4, reader=reader) or 4
    elif 1 <= choice <= len(presets):
        path, depth = presets[choice - 1]
    else:
        return
    result = run_sync_tree(cfg, path, depth)
    if not result.ok:
        print(result.message)


def screen_add_cloud(cfg: MenuConfig, reader: Reader) -> None:
    parents = snapshot_top_level(cfg)
    print("Cloud parent:")
    for i, p in enumerate(parents, start=1):
        print(f" {i}  {p}")
    custom = len(parents) + 1
    print(f" {custom}  Custom path")
    print("  0  Back")
    if not parents:
        print("(no cloud snapshot yet — use Custom, or run a cloud scan first)")
    choice = prompt_int("Choose", reader=reader)
    if choice is None:
        return
    if choice == custom:
        parent = prompt_line("Parent path: ", reader=reader)
        if not parent:
            return
    elif 1 <= choice <= len(parents):
        parent = parents[choice - 1]
    else:
        return

    dirs = cloud_list_dirs(cfg, parent)
    if not dirs:
        print("No subfolders found (or rclone error).")
        return
    labels = [f"{name}/" for name in dirs]
    print("Subfolders:")
    for i, label in enumerate(labels, start=1):
        print(f" {i:2d}  {label}")
    pick = prompt_ints("Folder number(s)", max_n=len(labels), reader=reader)
    if not pick:
        return
    print("Mode: 1=bidirectional  2=download-only  0=cancel")
    mode_choice = prompt_int("Mode", reader=reader)
    if mode_choice == 1:
        mode = "bidirectional"
    elif mode_choice == 2:
        mode = "download_only"
    else:
        return

    for index in pick:
        name = dirs[index - 1]
        full = f"{parent.rstrip('/')}/{name}"
        if mode == "bidirectional":
            inspect_payload = action_inspect(cfg, full)
            if not inspect_payload.get("safe_for_bidirectional"):
                result = handle_blocked_add(cfg, full, inspect_payload, reader=reader)
                if result and result.ok:
                    print(result.message)
                continue
        result = confirm_and_add(cfg, full, mode, reader=reader)
        if result is None:
            continue
        print(result.message)
        if result.ok and mode == "bidirectional":
            offer_resync_if_needed(cfg, mode=mode, reader=reader)


def _pick_orphans(orphans, reader: Reader) -> List[OrphanEntry]:
    """One level at a time, until the operator picks folders to add.

    The flat list this replaces ran to 32 lines here, 24 of them one subtree,
    and after G9 a single local-only folder brought 40 more along. Descending
    keeps a level short and keeps the numbers next to short names instead of
    full paths — see G10.

    Returns the folders chosen, empty if the operator backed out. The choice
    itself is `browse_rows`, which is a pure function and tested as one; what
    is here is the loop and the printing.
    """
    prefix = ""
    while True:
        rows = browse_rows(orphans, prefix)
        if not rows:
            # Only reachable if the list changed under us; going up is the one
            # answer that cannot loop.
            prefix = prefix.rpartition("/")[0]
            if not prefix:
                return []
            continue
        where = f"/{prefix}" if prefix else "/"
        print(f"\nLocal folders not in sync — {where}\n")
        for i, row in enumerate(rows, start=1):
            print(f" {i:2d}  {format_browse_row(row)}")
        print("  0  " + ("Up" if prefix else "Back"))
        print("Add: 1  or 1,2  or all      Open a folder: 1/")
        choice = parse_level_input(prompt_line("> ", reader=reader), rows)
        if choice.message:
            print(choice.message)
        if choice.action == "retry":
            continue
        if choice.action == "up":
            if not prefix:
                return []
            prefix = prefix.rpartition("/")[0]
            continue
        if choice.action == "open":
            prefix = choice.open_row.prefix
            continue
        # `all` on a level that mixes the two is the common way to get here, so
        # the containers are skipped with a word rather than refused.
        if choice.skipped:
            print(f"Not added (open with a slash to choose inside): {', '.join(choice.skipped)}")
        return [row.entry for row in choice.add]


def screen_add_orphans(cfg: MenuConfig, reader: Reader) -> None:
    orphans = list_orphan_paths(
        cfg.db_path, cfg.local_root, cfg.policy_path, root="/", max_depth=5
    )
    if not orphans:
        print("No local orphan folders found.")
        print("Tip: folders with [L] in ydm-tree appear here.")
        return
    picked = _pick_orphans(orphans, reader=reader)
    if not picked:
        return
    print("Mode: 1=bidirectional  2=download-only  0=cancel")
    mode_choice = prompt_int("Mode", reader=reader)
    if mode_choice == 1:
        mode = "bidirectional"
    elif mode_choice == 2:
        mode = "download_only"
    else:
        return

    added_any = False
    for entry in picked:
        path = entry.cloud_path
        if mode == "bidirectional":
            inspect_payload = action_inspect(cfg, path)
            if not inspect_payload.get("safe_for_bidirectional"):
                result = handle_blocked_add(cfg, path, inspect_payload, reader=reader)
                if result and result.ok:
                    print(result.message)
                    added_any = True
                continue
        result = confirm_and_add(cfg, path, mode, reader=reader)
        if result is None:
            continue
        print(result.message)
        if result.ok:
            added_any = True
    if added_any and mode == "bidirectional":
        offer_resync_if_needed(cfg, mode=mode, reader=reader)


def screen_remove(cfg: MenuConfig, reader: Reader) -> None:
    """Menu 4. What "remove" means depends on which list the backend keeps.

    Under rclone the policy is a whitelist: an entry means "synced", and
    deleting it is how a folder stops syncing. Under the daemon it is a
    blacklist — an entry means "excluded" — and deleting one *starts* a sync.
    The same screen therefore did the opposite of its own label on every
    daemon machine. See tasks/ydm_menu/AUDIT-2026-08-27.md.
    """
    if cfg.backend_kind == "daemon":
        screen_stop_syncing(cfg, reader)
        return
    screen_remove_from_whitelist(cfg, reader)


def screen_stop_syncing(cfg: MenuConfig, reader: Reader) -> None:
    """Menu 4 on the daemon: put a folder into `exclude-dirs`.

    Excluding is the only direction that is safe by construction here — it
    takes a folder out of the daemon's reach and cannot delete anything,
    locally or in the cloud. The opposite direction, un-excluding, is what
    emptied /Books on 2026-08-14, and it stays where it belongs: menu 2, which
    shows the delta first.
    """
    scope = daemon_sync_scope(cfg)
    synced, excluded = scope["synced"], scope["excluded"]
    footer = (f"Excluded now: {len(excluded)} folder(s). "
              f"To start syncing one again use menu 2.")
    if not synced:
        print("The daemon is already excluding everything it knows about — "
              "there is nothing left to stop syncing.")
        print(footer)
        return

    print("Folders the daemon is syncing (pick one to STOP syncing):\n")
    for i, path in enumerate(synced, start=1):
        print(f" {i:2d}  {path}")
    custom = len(synced) + 1
    print(f" {custom:2d}  Custom path (a subfolder, e.g. /video/Blender)")
    print("  0  Back")
    print(f"\n{footer}")

    pick = prompt_int("Stop syncing which", reader=reader)
    if pick is None:
        return
    if pick == custom:
        typed = prompt_line("Path: ", reader=reader)
        if not typed:
            return
        entry = normalize_entry(typed)
        if not entry:
            # `normalize_entry("/")` is the empty string, and an empty
            # exclude-dirs entry is not "exclude everything" — it is a stray
            # comma in the daemon's config.
            print("\nThe sync root itself cannot be excluded. "
                  "To stop the daemon entirely: yandex-disk stop")
            return
        path = f"/{entry}"
    elif 1 <= pick <= len(synced):
        path = synced[pick - 1]
    else:
        return

    covered = covering_exclusion(path, excluded)
    if covered:
        # A blacklist excludes whole subtrees, so this would add an entry that
        # changes nothing and report success for it.
        print(f"\n/{covered} is already excluded, so {path} is not syncing "
              f"either. Nothing to do.")
        return

    shadowed = policy_children_of(cfg, path)
    if shadowed:
        print(f"\nWARN: the daemon cannot keep a child of an excluded folder, "
              f"so these stop syncing too, whatever the policy says about them:")
        for child in shadowed:
            print(f"    /{child}")

    result = confirm_and_add(cfg, path, "disabled", reader=reader)
    if result is None:
        return
    print(result.message)
    if not result.ok:
        return
    local = os.path.join(cfg.local_root, path.strip("/"))
    if os.path.isdir(local):
        # Printed, not pressed. The order — exclude, let the daemon restart,
        # then delete — is the only safe one, and an irreversible step placed
        # right after a daemon restart is the shape of the 14.08 incident.
        print(f"\nThe daemon no longer touches {path}. "
              f"The local copy is untouched; to reclaim the space:")
        print(f"  rm -rf {shlex.quote(local)}")


def screen_remove_from_whitelist(cfg: MenuConfig, reader: Reader) -> None:
    """Menu 4 on rclone, where a policy entry means the folder is synced."""
    policy = load_policy(cfg.policy_path)
    if not policy:
        print("No policy file.")
        return
    entries = []
    for mode_key, marker in (
        ("bidirectional", "[B]"),
        ("download_only", "[D]"),
        ("disabled", "[X]"),
    ):
        for path in policy_paths_by_mode(policy, mode_key):
            entries.append((f"{marker} {path}", path, mode_key))
    if not entries:
        print("Nothing in policy to remove.")
        return
    for i, (label, _p, _m) in enumerate(entries, start=1):
        print(f" {i:2d}  {label}")
    print("  0  Back")
    pick = prompt_int("Remove which", reader=reader)
    if pick is None or pick < 1 or pick > len(entries):
        return
    _label, path, mode = entries[pick - 1]
    cloud_path = f"/{path}"
    if mode == "disabled":
        # `effective_download_paths()` never looked at this entry: the folder
        # is out of the filters because it is absent from them, not because of
        # the mark. Calling that "remove from sync" is the daemon's mistake in
        # miniature, and it is the one place a whitelist can make it.
        print(f"\n{cloud_path} is marked excluded and is not synced under "
              f"rclone either way — dropping the entry only removes the mark.")
        print("Drop it?")
    else:
        print(f"\nRemove {cloud_path} from sync?")
    if not prompt_yes_no("Confirm", default=False, reader=reader):
        return
    basename = path.rsplit("/", 1)[-1]
    if not prompt_confirm_name(basename, reader=reader):
        print("Cancelled.")
        return
    delete_local = prompt_yes_no("Delete local copy too?", default=False, reader=reader)
    result = action_remove(cfg, cloud_path, delete_local=delete_local)
    print(result.message)


def screen_bisync_run(cfg: MenuConfig, reader: Reader) -> None:
    if not prompt_yes_no("Run bisync now?", default=True, reader=reader):
        return
    result = action_bisync_run(cfg, apply=True)
    print(result.message)


def screen_resync(cfg: MenuConfig, reader: Reader) -> None:
    preview = action_resync(cfg, apply=False)
    print(preview.message)
    if not preview.ok:
        return
    if prompt_yes_no("Apply resync?", default=False, reader=reader):
        print_bisync_scope(cfg)
        result = action_resync(cfg, apply=True)
        print(result.message)


def _scan_one_folder(cfg: MenuConfig, reader: Reader) -> None:
    options = snapshot_top_level(cfg) + ["/"]
    for i, p in enumerate(options, start=1):
        print(f" {i}  {p if p != '/' else '/ (full disk, slow)'}")
    custom = len(options) + 1
    print(f" {custom}  Custom")
    print("  0  Back")
    choice = prompt_int("Scan path", reader=reader)
    if choice is None:
        return
    if choice == custom:
        path = prompt_line("Path: ", reader=reader) or "/"
    elif 1 <= choice <= len(options):
        path = options[choice - 1]
    else:
        return
    print(run_cloud_scan(cfg, path).message)


def screen_cloud_scan(cfg: MenuConfig, reader: Reader) -> None:
    """Ask what changed before offering to scan anything.

    The old version of this screen could scan but had no way to learn what
    needed scanning, so the smart part of the smart scan — cloud_delta, which
    answers in one request what a full walk costs ~4 600 — was reachable only
    by knowing it existed in tools/. See tasks/ydm_menu/AUDIT-2026-08-24.md B.
    """
    print("Checking what changed on the disk… (1 request)")
    check = check_cloud_changes(cfg)

    if not check.ok:
        # No token or no network: the manual paths still work, so say what is
        # missing and carry on rather than dropping the person out.
        print(f"  {check.message}")
        print("  (a token in .env or an rclone remote enables the check)")
        _scan_one_folder(cfg, reader)
        return

    payload = check.details or {}
    if payload.get("changed") is False:
        seen = payload.get("previous_checked_at") or "the last check"
        print(f"  Nothing has changed since {seen}. The snapshot is current.")
        print("")
        print(" 1  Scan one folder anyway…")
        print("  0  Back")
        if prompt_int("Choose", reader=reader) == 1:
            _scan_one_folder(cfg, reader)
        return

    if payload.get("changed") is None:
        print("  First check on this machine — nothing to compare against yet.")

    print("  Something changed. Looking for which folders…")
    stale = list_stale_folders(cfg)
    if not stale.ok:
        print(f"  {stale.message}")
        _scan_one_folder(cfg, reader)
        return

    report = stale.details or {}
    totals = report.get("totals") or {}
    roots = report.get("rescan_roots") or []
    for warning in report.get("warnings") or []:
        # cloud_delta distinguishes "nothing found" from "list was truncated"
        # and from "revision moved but the sweep saw nothing". Flattening
        # those into silence is how a stale snapshot looks current.
        print(f"  WARN: {warning}")

    print(
        f"  {totals.get('stale_folders', 0)} stale folder(s), "
        f"{totals.get('changed_files', 0)} changed file(s), "
        f"{totals.get('deleted_entries', 0)} deleted"
    )
    print("")
    if roots:
        print(f" 1  Refresh what went stale  ({len(roots)} scan(s))")
    print(" 2  Show which folders")
    print(" 3  Scan one folder…")
    print("  0  Back")
    choice = prompt_int("Choose", reader=reader)
    if choice == 1 and roots:
        for root in roots:
            print(run_cloud_scan(cfg, root).message)
    elif choice == 2:
        for folder in (report.get("stale_folders") or [])[:20]:
            print(
                f"  {folder.get('path')}  "
                f"changed: {folder.get('changed_files', 0)}  "
                f"deleted: {folder.get('deleted_entries', 0)}"
            )
    elif choice == 3:
        _scan_one_folder(cfg, reader)


def _overview_lines(cfg: MenuConfig) -> List[str]:
    """Four lines of state, gathered from three places nobody looked in together."""
    from tools.ydm_menu_actions import scan_overview

    overview = scan_overview(cfg)
    lines = []
    snapshot = overview.get("snapshot")
    if snapshot:
        updates = snapshot.get("folder_updates") or 0
        extra = f", +{updates} folder update(s)" if updates else ""
        lines.append(
            f"Cloud snapshot: base #{snapshot['base_scan_id']}, "
            f"{snapshot.get('base_at')} ({snapshot.get('base_age_days')} d)"
            f"{extra}, {snapshot.get('files_from_base', 0)} files"
        )
    else:
        lines.append(f"Cloud snapshot: none yet ({overview.get('error')})")
    local = overview.get("local_scan")
    lines.append(
        f"Local scan:     #{local['id']}, {local['timestamp']}, {local['rows']} rows"
        if local else "Local scan:     never run"
    )
    database = overview.get("database") or {}
    if database.get("error"):
        lines.append(f"Database:       unreadable ({database['error']})")
    else:
        prunable = database.get("prunable_scans")
        share = database.get("prunable_share_percent")
        drop = (f", {prunable} prunable ({share}% of rows)"
                if prunable is not None else "")
        lines.append(
            f"Database:       {database.get('size_bytes', 0) / (1024 * 1024):.1f} MB, "
            f"{database.get('scans', 0)} scans{drop}"
        )
    return lines


def _show_recent_scans(cfg: MenuConfig) -> None:
    from tools.ydm_menu_actions import recent_scans

    listed = recent_scans(cfg, limit=10)
    if not listed:
        print("No scans recorded yet.")
        return
    print("\nid      when              type   rows    role")
    for scan in listed:
        print(
            f"{scan['id']:<7} {str(scan['timestamp'])[:16]:<17} "
            f"{scan['type']:<6} {scan['rows']:<7} {scan['role']}"
        )
    print("\nThe base and its folder updates are what the tree reads;")
    print("cleaning up never touches them.")


def _clean_the_database(cfg: MenuConfig, reader: Reader) -> None:
    from tools.ydm_menu_actions import action_prune, prune_preview

    preview = prune_preview(cfg)
    print(preview.message)
    if not preview.ok:
        return
    plan = preview.details or {}
    if not plan.get("prunable_scans"):
        print("Nothing to clean up.")
        return
    if prompt_yes_no("Delete them and shrink the file?", default=False, reader=reader):
        print(action_prune(cfg).message)


def screen_scans(cfg: MenuConfig, reader: Reader) -> None:
    """Everything about scans and the database, in one place — G14.

    The parts existed and were scattered: the smart cloud check was here, the
    full walk was hidden inside "Scan one folder…", the database was a line in
    detailed status ending in a command to type, and the local scan had no entry
    at all although the tree, the counts and every marker depend on it.

    Two rules the owner asked for, which are G10 and G12 restated: no walls —
    the scan list is behind its own entry and capped at ten, not printed on
    every visit; and no double meanings — "full" and "one folder" are separate
    lines, and cleaning up shows a plan and asks rather than naming a command.
    """
    while True:
        print("")
        for line in _overview_lines(cfg):
            print(line)
        print("")
        print(" 1  Check the cloud and refresh what changed   (1 request first)")
        print(" 2  Scan one cloud folder…")
        print(" 3  Full cloud scan of the whole disk          (slow)")
        print(" 4  Rescan the local mirror now")
        print(" 5  Recent scans…")
        print(" 6  Clean up the database…")
        print("  0  Back")
        choice = prompt_int("Choose", reader=reader)
        if choice is None or choice == 0:
            return
        if choice == 1:
            screen_cloud_scan(cfg, reader)
        elif choice == 2:
            _scan_one_folder(cfg, reader)
        elif choice == 3:
            print("The whole disk, every folder. On this device the last one "
                  "took about 200 minutes.")
            if prompt_yes_no("Start a full cloud scan?", default=False, reader=reader):
                print(run_cloud_scan(cfg, "/").message)
        elif choice == 4:
            from tools.ydm_menu_actions import action_local_scan

            print(action_local_scan(cfg).message)
        elif choice == 5:
            _show_recent_scans(cfg)
        elif choice == 6:
            _clean_the_database(cfg, reader)


def run_repl(cfg: MenuConfig, reader: Reader = default_reader) -> None:
    handlers = {
        "1": lambda: screen_tree(cfg, reader),
        "2": lambda: screen_add_cloud(cfg, reader),
        "3": lambda: screen_add_orphans(cfg, reader),
        "4": lambda: screen_remove(cfg, reader),
        "5": lambda: screen_bisync_run(cfg, reader),
        "6": lambda: screen_resync(cfg, reader),
        "7": lambda: screen_scans(cfg, reader),
        "8": lambda: print_detailed_status(cfg),
        "9": lambda: print("\n".join(render_short_help())),
        # Letters, not new numbers: `2` for "add from cloud" is in the owner's
        # fingers and written into HOW_TO_USE. See tasks/ydm_menu/DESIGN-2026-08-24.md.
        "d": lambda: print_cloud_local_diff(cfg),
        "t": lambda: print_trash_overview(cfg),
        "h": lambda: print("\n".join(render_short_help())),
    }
    while True:
        status = load_status(cfg)
        print("\n".join(render_main_menu(cfg, status)))
        choice = prompt_line("> ", reader=reader).lower()
        if choice in {"q", "quit", "exit"}:
            print("Bye.")
            return
        handler = handlers.get(choice)
        if handler:
            try:
                handler()
            except KeyboardInterrupt:
                print("\nInterrupted.")
            except BackendError as exc:
                # No usable backend, or one that refused the operation: report
                # it and stay in the menu instead of dropping a traceback.
                print(f"Backend error: {exc}")
            pause(reader=reader)
        else:
            print("Unknown choice.")


def cmd_orphans_json(cfg: MenuConfig) -> int:
    if not cloud_scan_available(cfg.db_path):
        print(
            f"No successful cloud scan in {cfg.db_path}. "
            f"Run: python3 ydm.py scan cloud",
            file=sys.stderr,
        )
        return 2
    entries = list_orphan_paths(
        cfg.db_path, cfg.local_root, cfg.policy_path, root="/", max_depth=5
    )
    print(json.dumps(orphans_to_json(entries), ensure_ascii=False, indent=2))
    return 0


def cmd_add_json(cfg: MenuConfig, args: argparse.Namespace) -> int:
    """Add a path to the policy, or show what adding it would change.

    The one place that answers "add this path" for every caller. Until
    2026-08-28 `ydm-sync-add` reached the same end by running `sync_policy.py
    add --apply` and then `render-filters --apply`, and both of those apply the
    whole policy to the backend — so one add stopped and started the daemon
    twice. On a 1.5 TB disk the second restart buys nothing and costs a
    re-index.

    Without `--apply` this is the dry run the alias never had: the same delta
    the menu shows before asking, without touching anything.
    """
    if args.apply:
        result = action_add(cfg, args.path, args.mode, force_risk=args.force_risk)
    else:
        result = preview_add(cfg, args.path, args.mode)
    payload = {
        "schema": "ydm_menu_add:v1",
        "action": "add",
        "dry_run": not args.apply,
        "path": args.path,
        "mode": args.mode,
        "backend": cfg.backend_name,
        "ok": result.ok,
        "message": result.message,
        "detail": result.details,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if result.ok else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="YDM interactive sync menu")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--local-root", default=None)
    parser.add_argument("--policy-path", default=None)
    parser.add_argument("--bisync-filter-path", default=None)
    # MenuConfig has always had the field and the YDM_EXCLUDE_CONFIG variable;
    # only the flag was missing, so the one path a person is most likely to
    # want to redirect was the one they could not.
    parser.add_argument("--exclude-config", default=None)
    parser.add_argument("--remote", default=None)
    parser.add_argument(
        "--backend",
        choices=["daemon", "rclone", "auto"],
        default=None,
        help="Sync backend: daemon (yandex-disk), rclone, or auto-detect",
    )
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--non-interactive", action="store_true", help="Script subcommands only")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("orphans", help="List local orphans as JSON")
    add = sub.add_parser("add", help="Add a path to the policy, as JSON")
    add.add_argument("--path", required=True)
    add.add_argument("--mode", default="bidirectional", choices=sorted(VALID_MODES))
    add.add_argument("--force-risk", action="store_true")
    # Dry by default, like every other write in this repository: the caller
    # that wants the change says so.
    add.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = MenuConfig.from_env_and_args(
        db_path=args.db_path,
        local_root=args.local_root,
        policy_path=args.policy_path,
        bisync_filter_path=args.bisync_filter_path,
        exclude_config=args.exclude_config,
        remote=args.remote,
        backend=args.backend,
        plain=args.plain,
    )
    if args.command == "orphans":
        return cmd_orphans_json(cfg)
    if args.command == "add":
        return cmd_add_json(cfg, args)
    if args.non_interactive:
        print("Use a subcommand: orphans, add", file=sys.stderr)
        return 2
    if not sys.stdin.isatty():
        print("YDM menu requires an interactive terminal.", file=sys.stderr)
        return 2
    run_repl(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
