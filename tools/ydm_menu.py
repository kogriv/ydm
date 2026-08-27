#!/usr/bin/env python3
"""YDM interactive menu for human operators (agents keep ydm-sync-* CLI)."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Callable, Optional

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
    print_cloud_local_diff,
    print_detailed_status,
    print_trash_overview,
    run_cloud_scan,
    run_sync_tree,
    snapshot_top_level,
)
from tools.sync_backends import BackendError  # noqa: E402
from tools.ydm_menu_config import MenuConfig  # noqa: E402
from tools.ydm_menu_orphans import (  # noqa: E402
    cloud_scan_available,
    format_orphan_label,
    list_orphan_paths,
    orphans_to_json,
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


def screen_add_orphans(cfg: MenuConfig, reader: Reader) -> None:
    orphans = list_orphan_paths(
        cfg.db_path, cfg.local_root, cfg.policy_path, root="/", max_depth=5
    )
    if not orphans:
        print("No local orphan folders found.")
        print("Tip: folders with [L] in ydm-tree appear here.")
        return
    print("Local folders not in sync:\n")
    for i, entry in enumerate(orphans, start=1):
        print(f" {i:2d}  {format_orphan_label(entry)}")
    print("  0  Back")
    picks = prompt_ints("Choose (e.g. 1 or 1,2 or all)", max_n=len(orphans), reader=reader)
    if not picks:
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
    for index in picks:
        entry = orphans[index - 1]
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


def run_repl(cfg: MenuConfig, reader: Reader = default_reader) -> None:
    handlers = {
        "1": lambda: screen_tree(cfg, reader),
        "2": lambda: screen_add_cloud(cfg, reader),
        "3": lambda: screen_add_orphans(cfg, reader),
        "4": lambda: screen_remove(cfg, reader),
        "5": lambda: screen_bisync_run(cfg, reader),
        "6": lambda: screen_resync(cfg, reader),
        "7": lambda: screen_cloud_scan(cfg, reader),
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
    if args.non_interactive:
        print("Use subcommand, e.g. orphans", file=sys.stderr)
        return 2
    if not sys.stdin.isatty():
        print("YDM menu requires an interactive terminal.", file=sys.stderr)
        return 2
    run_repl(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
