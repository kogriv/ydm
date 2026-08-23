#!/usr/bin/env python3
"""A synthetic stand-in for the whole setup: policy, local disk, cloud snapshot.

Every check built on this touches nothing real. That is not tidiness — the
manual checklists this replaces ask for `Books/Math/База` to be made
bidirectional, which on the live daemon means dropping `Books` from
`exclude-dirs` and starting an actual sync of `/Books`. See
`tasks/sync_bench/GAP.md`.

The bench exists because the marker tests it feeds used to call a pure function
with arguments assembled by hand, proving only that function's truth table.
Here the inputs are files on disk and the output is the rendered tree, so the
seams between the layers are inside the test rather than outside it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ydm import DEFAULT_CONFIG, StorageManager  # noqa: E402


# --- the sample tree -------------------------------------------------------
#
# One tree has to reach all nine values of display_marker(). The table is the
# specification: it says what a person should see for each path, and it is
# written before the code runs. Where the code disagrees, the disagreement is
# the finding — the table is not adjusted to make a test pass.

@dataclass
class BenchPath:
    """One folder of the sample tree, and what it is supposed to look like.

    Note on nesting: the tree counts files across a folder's whole subtree, not
    just the ones directly in it. So a folder is `[B]` only when everything
    beneath it is materialized too, and a `[B?]` case can never sit inside a
    `[B]` case — the parent would correctly become `[B~]`. The first draft of
    this table got that wrong and the bench caught it, which is the sort of
    thing it is for.
    """

    path: str
    #: Policy entry mode, or None for "no entry of its own".
    policy_mode: Optional[str]
    #: Files this folder holds directly in the cloud snapshot.
    cloud_files: int
    #: Files this folder holds directly in the local scan.
    local_files: int
    #: Whether the folder exists on the local filesystem. Distinct from
    #: local_files: a folder can exist and be empty, which is what separates
    #: "partially materialized" from "missing".
    local_dir: bool
    #: What the tree should render under whitelist (rclone) semantics.
    expect_rclone: str
    #: What it should render under blacklist (daemon) semantics, where the
    #: policy holds only exclusions and everything else is synced.
    expect_daemon: str
    why: str = ""


SAMPLE_TREE: List[BenchPath] = [
    BenchPath(
        "/", None, cloud_files=0, local_files=0, local_dir=True,
        expect_rclone="[P]", expect_daemon="[B~]",
        why="the sync root: no entry of its own, synced things beneath it. "
            "Under a blacklist it is the whole disk, partially materialized",
    ),
    BenchPath(
        "/pro", "bidirectional", cloud_files=2, local_files=2, local_dir=True,
        expect_rclone="[B]", expect_daemon="[B]",
        why="in policy, fully materialized, nothing missing beneath — the "
            "ordinary synced folder. Kept childless on purpose: one missing "
            "descendant would correctly make it [B~]",
    ),
    BenchPath(
        "/agents", "bidirectional", cloud_files=1, local_files=0, local_dir=False,
        expect_rclone="[B?]", expect_daemon="[B?]",
        why="in policy but nothing on disk: the question mark is the point",
    ),
    BenchPath(
        "/video", "bidirectional", cloud_files=3, local_files=1, local_dir=True,
        expect_rclone="[B~]", expect_daemon="[B~]",
        why="in policy, some files present — partial, neither missing nor done",
    ),
    BenchPath(
        "/Docs", "download_only", cloud_files=1, local_files=1, local_dir=True,
        expect_rclone="[D]", expect_daemon="[B]",
        why="download-only exists only under whitelist; a blacklist policy has "
            "no way to say it, so the daemon reads the folder as simply synced",
    ),
    BenchPath(
        "/archive", "download_only", cloud_files=1, local_files=0, local_dir=False,
        expect_rclone="[D?]", expect_daemon="[B?]",
        why="same, with nothing on disk",
    ),
    BenchPath(
        "/shared", "download_only", cloud_files=1, local_files=1, local_dir=True,
        expect_rclone="[D]", expect_daemon="[B]",
        why="download-only with a synced folder beneath it. Its own mode has to "
            "win over 'parent of a synced path' — without this case the [P] "
            "branch can be moved above the mode branches and every test still "
            "passes, which a mutation check confirmed on 2026-08-23",
    ),
    BenchPath(
        "/shared/live", "bidirectional", cloud_files=1, local_files=1, local_dir=True,
        expect_rclone="[B]", expect_daemon="[B]",
        why="the synced child that makes /shared a candidate for [P]",
    ),
    BenchPath(
        "/Books", "disabled", cloud_files=1, local_files=0, local_dir=False,
        expect_rclone="[X]", expect_daemon="[X]",
        why="excluded on both sides — the one mode both semantics express",
    ),
    BenchPath(
        "/Books/Math", None, cloud_files=1, local_files=0, local_dir=False,
        expect_rclone="[P]", expect_daemon="[X]",
        why="inside a disabled folder, no entry of its own, and holding a "
            "descendant. Daemon inherits the exclusion and says [X]. Whitelist "
            "says [P] — 'parent of a synced path' — although nothing below it "
            "is synced: `is_under_policy_path()` counts a *disabled* ancestor "
            "as a policy path. Asserted as it behaves today, flagged in "
            "tasks/sync_bench/GAP.md G6; see also /Books/Math/АнГем",
    ),
    BenchPath(
        "/Books/Math/АнГем", None, cloud_files=1, local_files=1, local_dir=True,
        expect_rclone="[L]", expect_daemon="[X]",
        why="on disk inside a disabled folder. The daemon excludes it. "
            "Whitelist calls it an orphan, so the menu will offer to add it — "
            "arguably right (one folder inside an unsynced tree is a real "
            "wish) and arguably misleading. Same G6 note",
    ),
    BenchPath(
        "/mix", None, cloud_files=0, local_files=0, local_dir=False,
        expect_rclone="[P]", expect_daemon="[B]",
        why="no entry of its own but a synced child, so it is a parent of a "
            "synced path. Holds no files itself, so under a blacklist its "
            "subtree is fully materialized and it reads as plain [B]",
    ),
    BenchPath(
        "/mix/inner", "bidirectional", cloud_files=1, local_files=1, local_dir=True,
        expect_rclone="[B]", expect_daemon="[B]",
        why="the synced child that makes its parent a [P]",
    ),
    BenchPath(
        "/orphans", None, cloud_files=1, local_files=1, local_dir=True,
        expect_rclone="[L]", expect_daemon="[B]",
        why="on disk, not in policy: an orphan under whitelist. A blacklist has "
            "no orphans — nothing is outside it",
    ),
    BenchPath(
        "/arch", None, cloud_files=1, local_files=0, local_dir=False,
        expect_rclone="[.]", expect_daemon="[B?]",
        why="in the cloud, nowhere else. [.] is the default every unmatched "
            "case falls into, which is exactly why it needs a test",
    ),
]


def expectations(backend: str) -> Dict[str, str]:
    """{path: marker} for one backend."""
    key = "expect_daemon" if backend == "daemon" else "expect_rclone"
    return {entry.path: getattr(entry, key) for entry in SAMPLE_TREE}


# --- the bench itself ------------------------------------------------------

@dataclass
class Bench:
    root: Path
    db_path: str
    local_root: str
    policy_path: str
    exclude_config: str
    cloud_scan_id: int = 1
    local_scan_id: int = 2
    #: Paths deliberately left absent from every real location, so a test that
    #: silently fell back to the live setup would fail rather than pass.
    guarded: Tuple[str, ...] = field(default_factory=tuple)

    def cli_args(self, backend: str) -> List[str]:
        """The arguments that point sync_tree/ydm_menu at this bench."""
        return [
            "--db-path", self.db_path,
            "--local-root", self.local_root,
            "--policy-path", self.policy_path,
            "--backend", backend,
        ]

    def read_policy(self) -> dict:
        with open(self.policy_path, encoding="utf-8") as handle:
            return json.load(handle)


def _rel(path: str) -> str:
    return path.strip("/")


def _cloud_parent(path: str) -> str:
    """Cloud rows store parent_path with a leading slash; root is ''."""
    return "" if path in ("", "/") else path


def build_bench(tmpdir: str, *, base_age_days: int = 1) -> Bench:
    """Materialize policy, local disk and cloud snapshot under `tmpdir`."""
    root = Path(tmpdir)
    local_root = root / "local"
    db_path = str(root / "monitor.db")
    policy_path = str(root / "policy.json")
    exclude_config = str(root / "config.cfg")

    # 1. Policy. Entries only for paths that have a mode of their own; the
    #    others are absent on purpose, since "absent" is what [L], [P] and [.]
    #    are built on.
    paths = {
        _rel(entry.path): {"mode": entry.policy_mode}
        for entry in SAMPLE_TREE
        if entry.policy_mode
    }
    with open(policy_path, "w", encoding="utf-8") as handle:
        json.dump({
            "schema": "ydm_sync_policy:v1",
            "local_root": str(local_root),
            "remote": "bench",
            "paths": paths,
        }, handle, ensure_ascii=False, indent=2)

    # 2. The daemon's config, holding only exclusions — that is all a blacklist
    #    policy can express.
    disabled = sorted(_rel(e.path) for e in SAMPLE_TREE if e.policy_mode == "disabled")
    with open(exclude_config, "w", encoding="utf-8") as handle:
        handle.write(f"dir=\"{local_root}\"\n")
        handle.write("exclude-dirs=" + ",".join(disabled) + "\n")

    # 3. The local filesystem.
    local_root.mkdir(parents=True, exist_ok=True)
    for entry in SAMPLE_TREE:
        if entry.local_dir:
            (local_root / _rel(entry.path)).mkdir(parents=True, exist_ok=True)

    # 4. The cloud snapshot and the local scan, in one database.
    storage = StorageManager(db_path, use_temp_storage=False, config=dict(DEFAULT_CONFIG))
    ok, message = storage.init_db()
    if not ok:
        raise RuntimeError(message)
    conn = storage.get_connection()
    try:
        conn.execute(
            "INSERT INTO scans (id, timestamp, scan_type, status, duration, scan_root, scan_depth)"
            " VALUES (1, datetime('now', ?), 'cloud', 'success', 0, '/', NULL)",
            (f"-{base_age_days} days",),
        )
        conn.execute(
            "INSERT INTO scans (id, timestamp, scan_type, status, duration, scan_root, scan_depth)"
            " VALUES (2, datetime('now'), 'local', 'success', 0, ?, NULL)",
            (str(local_root),),
        )

        cloud_rows = []
        local_rows = []
        for entry in SAMPLE_TREE:
            rel = _rel(entry.path)
            if rel:
                parent, _, name = rel.rpartition("/")
                # The directory row, so the tree has a node to render at all.
                # The sync root needs none: it is the tree's starting point,
                # not a child of anything.
                cloud_rows.append((1, _cloud_parent(f"/{parent}" if parent else ""), name, "dir", 0))
            for i in range(entry.cloud_files):
                cloud_rows.append((1, _cloud_parent(entry.path), f"f{i}.txt", "file", 10))
            # Local rows use the other convention: no leading slash. Keeping
            # the bench honest about that is the point of TestPathConventions.
            for i in range(entry.local_files):
                local_rows.append((2, rel, f"f{i}.txt", "file", 10))

        conn.executemany(
            "INSERT OR IGNORE INTO files (scan_id, parent_path, name, type, size)"
            " VALUES (?, ?, ?, ?, ?)",
            cloud_rows + local_rows,
        )
        conn.commit()
    finally:
        conn.close()

    return Bench(
        root=root,
        db_path=db_path,
        local_root=str(local_root),
        policy_path=policy_path,
        exclude_config=exclude_config,
        guarded=(
            str(ROOT_DIR / "monitor.db"),
            str(ROOT_DIR / "var" / "sync_policy.json"),
        ),
    )


def render_markers(bench: Bench, backend: str, *, depth: int = 3) -> Dict[str, str]:
    """{path: marker} as the real tree pipeline produces them.

    Deliberately goes through sync_tree's own main() rather than reassembling
    the steps here: a bench that rebuilt the pipeline would verify the bench.
    """
    import subprocess

    proc = subprocess.run(
        [
            sys.executable, str(ROOT_DIR / "tools" / "sync_tree.py"),
            "--path", "/", "--depth", str(depth),
            "--format", "json", "--schema", "sync_tree:v2",
            "--no-local-scan", "--show-all",
            *bench.cli_args(backend),
        ],
        capture_output=True, text=True, check=False,
        env={**os.environ, "HOME": str(bench.root / "home")},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sync_tree failed ({proc.returncode}): {proc.stderr[-2000:]}")
    payload = json.loads(proc.stdout)

    markers: Dict[str, str] = {}

    def walk(node: dict) -> None:
        marker = (node.get("markers") or {}).get("display")
        if marker:
            markers[node["path"]] = marker
        for child in node.get("children") or []:
            walk(child)

    walk(payload["root"])
    return markers
