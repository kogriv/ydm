#!/usr/bin/env python3
"""Counting database queries, because the count is what is left to cut.

Phase 9 made each query cheap. On the Android device that changed less than
it looks: proot bills every system call through ptrace, so a bare `os.stat`
costs 0.207 ms against a normal ~10 us and a single SQLite query costs
0.5 ms. The same database in RAM answers in 0.006 ms — 80x — which is the
ceiling for any fix of this class. What remains under our control is how many
queries there are: 50 813 of them for 3 801 nodes on 2026-08-28, 13.4 per
node, 67% of the run. See `tasks/ydm_menu/BACKLOG.md`, Phase 12.

Wall-clock cannot police that here. The desktop is 5-15x faster than the
device, so a real improvement moves the clock by an amount that noise on this
machine covers easily. A query count does not move with the machine: it is the
same number on both, and it is the number the device actually pays for.

Usage:

    with counting_queries() as queries:
        build_tree(...)
    self.assertLess(queries.count / nodes, 4)

`sqlite3.connect` is patched rather than the connection objects, because the
code under test opens its own — and `StorageManager.reuse_connection()` keeps
one open across a whole walk, so a counter attached after the fact would see
nothing.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Dict, Iterator
from unittest.mock import patch


class QueryCounter:
    """Every statement handed to SQLite while this was active."""

    def __init__(self) -> None:
        self.count = 0
        self.by_statement: Dict[str, int] = {}

    def record(self, statement: str) -> None:
        self.count += 1
        # Collapsed whitespace and truncated: the tree asks the same handful
        # of questions over and over, and what makes a report readable is
        # seeing which one of them runs 21 232 times.
        key = " ".join(str(statement).split())[:80]
        self.by_statement[key] = self.by_statement.get(key, 0) + 1

    def report(self, limit: int = 6) -> str:
        lines = [f"{self.count} queries"]
        ranked = sorted(self.by_statement.items(), key=lambda item: -item[1])
        for statement, times in ranked[:limit]:
            lines.append(f"  {times:6d}  {statement}")
        return "\n".join(lines)


@contextmanager
def counting_queries() -> Iterator[QueryCounter]:
    counter = QueryCounter()
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(counter.record)
        return conn

    with patch.object(sqlite3, "connect", connect):
        yield counter
