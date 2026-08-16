#!/usr/bin/env python3
"""YDM interactive menu for human operators (agents keep ydm-sync-* CLI)."""
from __future__ import annotations

import argparse
import json
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
    cloud_list_dirs,
    handle_blocked_add,
    offer_resync_if_needed,
    print_detailed_status,
    run_cloud_scan,
    run_sync_tree,
)
from tools.sync_backends import BackendError  # noqa: E402
from tools.ydm_menu_config import MenuConfig  # noqa: E402
from tools.ydm_menu_orphans import format_orphan_label, list_orphan_paths, orphans_to_json  # noqa: E402
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
from tools.sync_policy import load_policy, policy_paths_by_mode  # noqa: E402


Reader = Callable[[str], str]


def screen_tree(cfg: MenuConfig, reader: Reader) -> None:
    presets = [
        ("/", 4),
        ("/Books/Math", 3),
    ]
    print("Tree presets:")
    for i, (path, depth) in enumerate(presets, start=1):
        print(f" {i}  {path} depth {depth}")
    print(" 3  Custom")
    print("  0  Back")
    choice = prompt_int("Choose", reader=reader)
    if choice is None:
        return
    if choice in {1, 2}:
        path, depth = presets[choice - 1]
    elif choice == 3:
        path = prompt_line("Cloud path [/]: ", reader=reader) or "/"
        depth = prompt_int("Depth", default=4, reader=reader) or 4
    else:
        return
    result = run_sync_tree(cfg, path, depth)
    if not result.ok:
        print(result.message)


def screen_add_cloud(cfg: MenuConfig, reader: Reader) -> None:
    parents = ["/Books/Math", "/DAO", "/pro", "/video"]
    print("Cloud parent:")
    for i, p in enumerate(parents, start=1):
        print(f" {i}  {p}")
    print(" 5  Custom path")
    print("  0  Back")
    choice = prompt_int("Choose", reader=reader)
    if choice is None:
        return
    if choice == 5:
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
        result = action_add(cfg, full, mode)
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
        result = action_add(cfg, path, mode)
        print(result.message)
        if result.ok:
            added_any = True
    if added_any and mode == "bidirectional":
        offer_resync_if_needed(cfg, mode=mode, reader=reader)


def screen_remove(cfg: MenuConfig, reader: Reader) -> None:
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


def screen_cloud_scan(cfg: MenuConfig, reader: Reader) -> None:
    options = ["/Books/Math", "/DAO", "/pro/agents", "/"]
    for i, p in enumerate(options, start=1):
        print(f" {i}  {p if p != '/' else '/ (full disk, slow)'}")
    print(" 5  Custom")
    print("  0  Back")
    choice = prompt_int("Scan path", reader=reader)
    if choice is None:
        return
    if choice == 5:
        path = prompt_line("Path: ", reader=reader) or "/"
    elif 1 <= choice <= len(options):
        path = options[choice - 1]
    else:
        return
    result = run_cloud_scan(cfg, path)
    print(result.message)


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
