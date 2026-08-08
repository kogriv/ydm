#!/usr/bin/env python3
"""Screen rendering for YDM menu."""
from __future__ import annotations

from typing import List, Optional

from tools.ydm_menu_config import MenuConfig
from tools.ydm_menu_status import MenuStatus


def _line(char: str, width: int) -> str:
    return char * min(width, 72)


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    keep = width - 3
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}...{text[-tail:]}"


def format_path_summary(paths: List[str], marker: str, limit: int = 2) -> str:
    if not paths:
        return ""
    shown = [f"{p}{marker}" for p in paths[:limit]]
    rest = len(paths) - limit
    if rest > 0:
        shown.append(f"+{rest} more")
    return "  ".join(shown)


def render_header(cfg: MenuConfig, status: MenuStatus) -> List[str]:
    lines = ["YDM Sync"]
    if not cfg.plain:
        lines.append(_line("─", cfg.width))
    lines.append(f"Status: {status.overall}   last bisync: {status.last_run_short}")
    lock = "yes" if status.lock_held else "no"
    resync = "needed" if status.resync_needed else "not needed"
    lines.append(f"Lock: {lock}          resync: {resync}")
    bidir = format_path_summary(status.bidirectional, "[B]")
    if bidir:
        lines.append(f"Synced: {bidir}")
    for warning in status.warnings[:2]:
        lines.append(f"WARN: {_truncate(warning, cfg.width - 6)}")
    return lines


def render_main_menu(cfg: MenuConfig, status: MenuStatus) -> List[str]:
    lines = render_header(cfg, status)
    lines.append("")
    lines.extend([
        " 1  Show sync tree",
        " 2  Add folder from cloud",
        " 3  Add LOCAL folder to sync",
        " 4  Remove folder from sync",
        " 5  Run bisync now",
        " 6  Resync baseline (after path changes)",
        " 7  Cloud scan (update snapshot)",
        " 8  Detailed status",
        " 9  Help",
        " q  Quit",
        "",
    ])
    return lines


def render_short_help() -> List[str]:
    return [
        "YDM Menu — quick help",
        "",
        "Start here: ydm",
        "Agent CLI unchanged: ydm-sync-add, ydm-tree, …",
        "",
        "Markers in tree:",
        " [B] bidirectional bisync",
        " [D] download-only",
        " [L] local orphan (on disk, not in policy)",
        " [.] cloud only",
        "",
        "Typical flow for local folder:",
        " 3 → pick orphan → bidirectional → resync if asked",
        "",
    ]
