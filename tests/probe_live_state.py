#!/usr/bin/env python3
"""Run test modules and report every read of this machine's live state.

Not part of the suite and not run by CI: it answers a question the tests
cannot ask about themselves. A check that quietly reads the operator's
`monitor.db`, `var/sync_policy.json` or `~/.config/yandex-disk/config.cfg`
passes for the wrong reason and goes stale in silence — that is the failure
mode `TestBenchIsolation` exists to prevent, and the only way to know it has
not crept back is to watch the file handles.

    python3 tests/probe_live_state.py                 # every module
    python3 tests/probe_live_state.py tests.test_ydm_menu

Exit code 1 if anything was touched. Written on 2026-08-27 for the isolation
rework of `tests/test_ydm_menu.py`; it found two product defects on its first
run — `snapshot_freshness()` and `get_diff()` both loading the daemon's
exclude-dirs from the default path when a caller had deliberately given them
no list. See tasks/ydm_menu/BACKLOG.md.
"""
from __future__ import annotations

import builtins
import io
import os
import sqlite3
import sys
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: The three files that describe *this* machine rather than a fixture.
WATCHED = {
    str(ROOT / "monitor.db"),
    str(ROOT / "var" / "sync_policy.json"),
    os.path.expanduser("~/.config/yandex-disk/config.cfg"),
}


def _resolve(target) -> str:
    # sqlite3 accepts `file:...?mode=ro`; both readers land here.
    text = str(target).split("?", 1)[0]
    if text.startswith("file:"):
        text = text[len("file:"):]
    try:
        return os.path.abspath(os.path.expanduser(text))
    except (TypeError, ValueError):
        return ""


def _modules(argv):
    if argv:
        return list(argv)
    return sorted(
        f"tests.{path.stem}" for path in (ROOT / "tests").glob("test_*.py")
    )


def main(argv) -> int:
    hits = []
    real_open, real_connect = builtins.open, sqlite3.connect

    def note(target):
        if _resolve(target) in WATCHED:
            hits.append((_resolve(target), "".join(traceback.format_stack()[-6:-1])))

    builtins.open = lambda f, *a, **k: (note(f), real_open(f, *a, **k))[1]
    sqlite3.connect = lambda d, *a, **k: (note(d), real_connect(d, *a, **k))[1]
    try:
        for name in _modules(argv):
            hits.clear()
            suite = unittest.defaultTestLoader.loadTestsFromName(name)
            result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
            broken = len(result.failures) + len(result.errors)
            print(f"{name}: {result.testsRun} tests, {broken} not passing, "
                  f"{len(hits)} live-state touch(es)")
            for path, stack in dict((h[1], h) for h in hits).values():
                print(f"  {path}\n{stack}")
            if hits or broken:
                return 1
    finally:
        builtins.open, sqlite3.connect = real_open, real_connect
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
