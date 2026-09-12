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
from typing import Dict, Iterable, List, Optional, Tuple

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
    #: Whether the folder is created on the local filesystem for its own sake.
    #: Distinct from local_files: a folder can exist and be empty, which is what
    #: separates "partially materialized" from "missing". Note that False does
    #: not promise absence — a folder whose child is created exists too, and
    #: `/Books/Math` is exactly that case.
    local_dir: bool
    #: What the tree should render under whitelist (rclone) semantics.
    expect_rclone: str
    #: What it should render under blacklist (daemon) semantics, where the
    #: policy holds only exclusions and everything else is synced.
    expect_daemon: str
    #: Whether the folder exists in the cloud at all. False is the case the bench
    #: had no row for until 2026-09-12: a folder copied onto the phone and never
    #: uploaded. With no snapshot row the tree — built from the snapshot — had no
    #: node to render it with, so the screen named "Add LOCAL folder to sync"
    #: could not list it. See GAP.md G9 and BACKLOG.md Phase 14.
    in_cloud: bool = True
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
        expect_rclone="[L]", expect_daemon="[X]",
        why="inside a disabled folder, no entry of its own, holding a "
            "descendant. Daemon inherits the exclusion and says [X]. Whitelist "
            "says [L]: it is on disk — not because this row asked for it, but "
            "because its child did, and a child cannot exist without its "
            "parent. Until G6 was fixed this rendered [P], 'parent of a synced "
            "path', although nothing below it syncs",
    ),
    BenchPath(
        "/Books/Math/АнГем", None, cloud_files=1, local_files=1, local_dir=True,
        expect_rclone="[L]", expect_daemon="[X]",
        why="on disk inside a disabled folder. The daemon excludes it. "
            "Whitelist calls it an orphan and the menu offers to add it — kept "
            "deliberately when G6 was fixed: being on disk and outside the "
            "policy is a local fact, not a claim about syncing, and wanting one "
            "folder inside an unsynced tree is a real wish",
    ),
    BenchPath(
        "/Books/Keep", None, cloud_files=0, local_files=0, local_dir=True,
        expect_rclone="[L]", expect_daemon="[X]",
        why="the same standing as /Books/Math/АнГем — on disk, inside a "
            "disabled tree, no entry of its own — differing only in having a "
            "descendant in the snapshot. Under G6 that difference alone turned "
            "it into [P] and dropped it from the orphan list, so the menu "
            "offered one of the two identical folders and hid the other",
    ),
    BenchPath(
        "/Books/Keep/Old", None, cloud_files=1, local_files=0, local_dir=False,
        expect_rclone="[.]", expect_daemon="[X]",
        why="the descendant that made the difference. It also gives the "
            "collapsed tree something to expand: under G6 a disabled subtree "
            "was kept whole, which is the opposite of what collapsing is for",
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
        "/fresh", None, cloud_files=0, local_files=0, local_dir=True,
        expect_rclone="[L]", expect_daemon="[B]",
        in_cloud=False,
        why="copied onto the phone and never uploaded — the only row with no "
            "cloud existence at all. Nodes come from the snapshot, so until "
            "2026-09-12 there was no node to carry a marker: the tree did not "
            "show it and the screen promising to add local folders could not "
            "list it (G9). It stands exactly as /orphans does, differing only "
            "in having no cloud row, which is why the pair belongs together. "
            "Needs no tenth marker: no cloud files, a directory on disk and no "
            "policy entry already mean `orphan` -> [L] under a whitelist, and "
            "under a blacklist nothing excludes it -> [B]",
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
    #: A real directory holding the same files the cloud snapshot describes, so
    #: an rclone remote can be pointed at it. Everything else about the bench
    #: answers "what does the system say"; this is what lets a check ask what
    #: it actually does with files.
    cloud_root: str = ""
    #: An rclone.conf of its own — never the live one. See _rclone_conf().
    rclone_config: str = ""
    #: Stands in for the project's var/. Some tools derive log and state paths
    #: from PROJECT_ROOT rather than from arguments, so without this the bench
    #: writes into the live var/ no matter what it is told.
    var_dir: str = ""
    cloud_scan_id: int = 1
    local_scan_id: int = 2
    #: Paths deliberately left absent from every real location, so a test that
    #: silently fell back to the live setup would fail rather than pass.
    guarded: Tuple[str, ...] = field(default_factory=tuple)

    def cli_args(self, backend: str) -> List[str]:
        """The arguments that point sync_tree/ydm_menu at this bench.

        `--exclude-config` among them since 2026-08-27: before the flag
        existed, a daemon-backed run resolved `~/.config/yandex-disk/config.cfg`
        and only the bench's own HOME kept it off the operator's file.
        """
        return [
            "--db-path", self.db_path,
            "--local-root", self.local_root,
            "--policy-path", self.policy_path,
            "--exclude-config", self.exclude_config,
            "--backend", backend,
        ]

    def env(self) -> Dict[str, str]:
        """Environment that keeps a subprocess inside the bench.

        A home of its own, and an rclone.conf of its own: `rclone listremotes`
        reads `RCLONE_CONFIG` before anything else, so without this a check
        would silently address the live `yandex:` remote.

        `YDM_LOCAL_ROOT` is removed rather than overridden: tools/aliases.sh
        exports it, so on a developer machine it is simply there, and a tool
        that fell back to it would read the real mirror while the test
        believed it was reading the bench.
        """
        env = {k: v for k, v in os.environ.items() if k != "YDM_LOCAL_ROOT"}
        env.update({
            "HOME": str(self.root / "home"),
            "RCLONE_CONFIG": self.rclone_config,
            "YDM_VAR_DIR": self.var_dir,
        })
        return env

    def read_policy(self) -> dict:
        with open(self.policy_path, encoding="utf-8") as handle:
            return json.load(handle)


def _rel(path: str) -> str:
    return path.strip("/")


def _cloud_parent(path: str) -> str:
    """Cloud rows store parent_path with a leading slash; root is ''."""
    return "" if path in ("", "/") else path


#: Characters Android's shared storage refuses and rclone can encode around.
#: See tasks/android_verify/GAP.md — the `android` remote below models that
#: refusal on this machine, which is the only part of it that can be modelled.
ANDROID_ENCODING = "Slash,Dot,Colon,Pipe"


def _rclone_conf(cloud_root: str) -> str:
    """Two remotes, both local, standing in for two different things.

    `cloud:` is an alias so that `cloud:pro` resolves against the bench's fake
    cloud rather than the current directory — the code under test builds
    remote paths as `{remote}:{path}`, and a bare `type = local` remote would
    quietly interpret those relative to wherever the test happened to run.
    """
    return (
        "[cloud]\n"
        "type = alias\n"
        f"remote = {cloud_root}\n"
        "\n"
        "[android]\n"
        "type = local\n"
        f"encoding = {ANDROID_ENCODING}\n"
    )


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

    # 3a. The cloud as actual files, and an rclone.conf that reaches them.
    #     The snapshot in the database says what is in the cloud; this says the
    #     same thing in a form rclone can read, so the rclone-backed code paths
    #     can run for real without a device and without the live remote.
    cloud_root = root / "cloud"
    cloud_root.mkdir(parents=True, exist_ok=True)
    for entry in SAMPLE_TREE:
        if not entry.in_cloud:
            continue
        folder = cloud_root / _rel(entry.path) if _rel(entry.path) else cloud_root
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(entry.cloud_files):
            (folder / f"f{i}.txt").write_text("x" * 10, encoding="utf-8")

    rclone_config = str(root / "rclone.conf")
    with open(rclone_config, "w", encoding="utf-8") as handle:
        handle.write(_rclone_conf(str(cloud_root)))

    var_dir = root / "var"
    var_dir.mkdir(parents=True, exist_ok=True)

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
            if rel and entry.in_cloud:
                parent, _, name = rel.rpartition("/")
                # The directory row, so the tree has a node to render at all.
                # The sync root needs none: it is the tree's starting point,
                # not a child of anything. A folder that is not in the cloud gets
                # no row at all — that absence is the case being described, and
                # the tree has to find it on disk instead.
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
        cloud_root=str(cloud_root),
        rclone_config=rclone_config,
        var_dir=str(var_dir),
        guarded=(
            str(ROOT_DIR / "monitor.db"),
            str(ROOT_DIR / "var" / "sync_policy.json"),
        ),
    )


#: The exclusions of the degenerate daemon policy — see make_daemon_policy().
#:
#: Chosen so that nothing left in sync is missing from disk: `agents`, `arch`
#: and `archive` are the three top-level folders the sample tree keeps out of
#: the local filesystem, and a folder that stays in scope while absent locally
#: is what `deletion_risk_paths` refuses a daemon restart over. Excluding them
#: here keeps that guard out of the way of checks that are about something else.
DAEMON_EXCLUDED: Tuple[str, ...] = ("Books", "agents", "arch", "archive")


def make_daemon_policy(
    bench: Bench,
    excluded: Iterable[str] = DAEMON_EXCLUDED,
) -> List[str]:
    """Rewrite the bench policy into the shape a machine with the daemon has.

    The default bench policy is rclone-shaped: it holds all three modes at
    once, so `[X]` is one entry among many and any screen listing the policy
    looks plausible. A daemon cannot express the other two — `_policy_to_
    exclude_dirs()` drops `bidirectional` and raises on `download_only` — so a
    real daemon policy is **nothing but exclusions**, and in that degenerate
    case a screen that lists the policy inverts its own meaning.

    That is the case Phase 10 exists for, and until 2026-08-27 no bench
    modelled it. See tasks/ydm_menu/AUDIT-2026-08-27.md.

    Rewrites both halves, because they have to agree: the policy file and the
    `exclude-dirs` line the daemon actually reads. Returns the exclusions.
    """
    entries = sorted({_rel(entry) for entry in excluded if _rel(entry)})
    policy = bench.read_policy()
    policy["paths"] = {entry: {"mode": "disabled"} for entry in entries}
    with open(bench.policy_path, "w", encoding="utf-8") as handle:
        json.dump(policy, handle, ensure_ascii=False, indent=2)
    with open(bench.exclude_config, "w", encoding="utf-8") as handle:
        handle.write(f"dir=\"{bench.local_root}\"\n")
        handle.write("exclude-dirs=" + ",".join(entries) + "\n")
    return entries


def read_exclude_dirs(bench: Bench) -> List[str]:
    """The `exclude-dirs` line as the daemon would parse it."""
    with open(bench.exclude_config, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("exclude-dirs="):
                raw = line.split("=", 1)[1].strip()
                return [p.strip() for p in raw.split(",") if p.strip()]
    return []


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
        env=bench.env(),
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
