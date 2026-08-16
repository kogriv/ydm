#!/usr/bin/env python3
"""Test bench for the daemon-backend policy path (`tools/sync_policy.py`).

Everything here runs against a synthetic `monitor.db` and a throwaway
`config.cfg` in a temp dir, with `stop_start_daemon` patched out. Nothing
touches `~/.config/yandex-disk/config.cfg`, the live daemon, or the cloud —
which is exactly the safeguard that was missing when
`_policy_coerce_for_daemon()` was first exercised straight on production and
cost 113 GB (see `docs/incidents/yandex-books-delete-2026-08-14.md`).

The tree below mirrors the shape of that incident: a large excluded folder
(`Books`) from which the user wants one deep subfolder (`Books/Math/АнГем`)
and nothing else.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sync_policy  # noqa: E402
from tools.sync_backends import (  # noqa: E402
    BackendError,
    DaemonBackend,
    NotSupportedError,
)


# parent_path convention copied from the real monitor.db: "" for the root,
# "/A/B" for everything below it.
CLOUD_TREE = {
    "": ["Books", "video", "pro", "Docs"],
    "/Books": ["42", "cpp", "Math", "История"],
    "/Books/Math": ["АнГем", "Анализ", "Теорвер"],
    "/video": ["Матеша", "Обучение"],
}


def seed_cloud_db(db_path: str, tree=CLOUD_TREE, scan_id: int = 1) -> None:
    """Create a monitor.db holding one successful cloud scan of `tree`."""
    storage = sync_policy.create_storage(db_path)
    storage.init_db()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO scans (id, scan_type, status, duration) VALUES (?,'cloud','success',1.0)",
            (scan_id,),
        )
        for parent, children in tree.items():
            for name in children:
                conn.execute(
                    "INSERT INTO files (scan_id,parent_path,name,type,size,md5) "
                    "VALUES (?,?,?,'dir',0,NULL)",
                    (scan_id, parent, name),
                )
            conn.execute(
                "INSERT INTO files (scan_id,parent_path,name,type,size,md5) "
                "VALUES (?,?,?,'file',10,'0'*32)",
                (scan_id, parent, "readme.txt"),
            )
        conn.commit()
    finally:
        conn.close()


class DaemonBenchCase(unittest.TestCase):
    """Temp dir with a synthetic cloud snapshot, config.cfg and policy file."""

    tree = CLOUD_TREE

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ydm_bench_")
        self.db_path = os.path.join(self.tmpdir, "monitor.db")
        self.config_path = os.path.join(self.tmpdir, "config.cfg")
        self.policy_path = os.path.join(self.tmpdir, "sync_policy.json")
        self.local_root = os.path.join(self.tmpdir, "ya_disk")
        os.makedirs(self.local_root, exist_ok=True)
        seed_cloud_db(self.db_path, self.tree)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def write_config(self, exclude_dirs):
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write('dir="%s"\n' % self.local_root)
            handle.write("exclude-dirs=%s\n" % ",".join(exclude_dirs))

    def read_exclude_dirs(self):
        with open(self.config_path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("exclude-dirs="):
                    raw = line.split("=", 1)[1].strip()
                    return [p for p in raw.split(",") if p]
        return []

    def materialize_local(self, names):
        """Create local dirs so the deletion guard sees no missing paths."""
        for name in names:
            os.makedirs(os.path.join(self.local_root, name), exist_ok=True)

    def policy(self, paths):
        return {"schema": sync_policy.SCHEMA, "local_root": self.local_root, "paths": dict(paths)}

    def disabled(self, policy):
        return sorted(k for k, v in policy["paths"].items() if v.get("mode") == "disabled")

    def coerce(self, policy, target, mode="bidirectional"):
        return sync_policy._policy_coerce_for_daemon(policy, target, mode, self.db_path)


class TestAncestorSiblingCoercion(DaemonBenchCase):
    """Including a descendant of an excluded folder must exclude the siblings
    on *every* level between the ancestor and the target — otherwise the daemon
    pulls down the whole branch."""

    def test_direct_child_excludes_its_siblings(self):
        policy = self.policy({"Books": {"mode": "disabled"}, "Books/Math": {"mode": "bidirectional"}})
        self.coerce(policy, "Books/Math")
        self.assertEqual(self.disabled(policy), ["Books/42", "Books/cpp", "Books/История"])
        self.assertNotIn("Books", policy["paths"])

    def test_nested_target_excludes_siblings_at_every_level(self):
        policy = self.policy({
            "Books": {"mode": "disabled"},
            "Books/Math/АнГем": {"mode": "bidirectional"},
        })
        self.coerce(policy, "Books/Math/АнГем")
        self.assertEqual(
            self.disabled(policy),
            [
                "Books/42",
                "Books/Math/Анализ",
                "Books/Math/Теорвер",
                "Books/cpp",
                "Books/История",
            ],
        )
        self.assertNotIn("Books", policy["paths"])
        self.assertNotIn("Books/Math", policy["paths"])

    def test_explicitly_managed_sibling_is_not_overwritten(self):
        policy = self.policy({
            "Books": {"mode": "disabled"},
            "Books/cpp": {"mode": "bidirectional"},
            "Books/Math/АнГем": {"mode": "bidirectional"},
        })
        self.coerce(policy, "Books/Math/АнГем")
        self.assertEqual(policy["paths"]["Books/cpp"]["mode"], "bidirectional")

    def test_second_include_under_the_same_ancestor(self):
        policy = self.policy({
            "Books": {"mode": "disabled"},
            "Books/Math/АнГем": {"mode": "bidirectional"},
        })
        self.coerce(policy, "Books/Math/АнГем")
        # The user now also wants Анализ, which the first pass had excluded.
        policy["paths"]["Books/Math/Анализ"] = {"mode": "bidirectional"}
        self.coerce(policy, "Books/Math/Анализ")
        self.assertEqual(
            self.disabled(policy),
            ["Books/42", "Books/Math/Теорвер", "Books/cpp", "Books/История"],
        )
        self.assertEqual(policy["paths"]["Books/Math/АнГем"]["mode"], "bidirectional")

    def test_top_level_target_needs_no_coercion(self):
        policy = self.policy({"Books": {"mode": "disabled"}, "video": {"mode": "bidirectional"}})
        self.assertEqual(self.coerce(policy, "video"), [])
        self.assertEqual(self.disabled(policy), ["Books"])

    def test_disabled_mode_is_a_noop(self):
        policy = self.policy({"Books": {"mode": "disabled"}, "Books/Math": {"mode": "disabled"}})
        self.assertEqual(self.coerce(policy, "Books/Math", mode="disabled"), [])
        self.assertEqual(self.disabled(policy), ["Books", "Books/Math"])

    def test_download_only_target_is_coerced_too(self):
        # The daemon rejects download_only later, in _policy_to_exclude_dirs;
        # coercion itself must not silently skip it.
        policy = self.policy({
            "Books": {"mode": "disabled"},
            "Books/Math": {"mode": "download_only"},
        })
        self.coerce(policy, "Books/Math", mode="download_only")
        self.assertNotIn("Books", policy["paths"])


class TestCoercionWithoutScanData(DaemonBenchCase):
    """Without a cloud snapshot of the intermediate level, the siblings are
    unknown — dropping the ancestor anyway would hand the whole branch to the
    daemon. Refuse instead of guessing."""

    tree = {
        "": ["Books", "video"],
        "/Books": ["42", "cpp", "Math", "История"],
        # no children recorded for /Books/Math
    }

    def test_missing_level_refuses_and_leaves_policy_untouched(self):
        policy = self.policy({
            "Books": {"mode": "disabled"},
            "Books/Math/АнГем": {"mode": "bidirectional"},
        })
        with self.assertRaises(sync_policy.PolicyCoercionError) as ctx:
            self.coerce(policy, "Books/Math/АнГем")
        self.assertIn("/Books/Math", str(ctx.exception))
        self.assertEqual(policy["paths"]["Books"]["mode"], "disabled")
        self.assertEqual(self.disabled(policy), ["Books"])


class TestAddPolicyPathEndToEnd(DaemonBenchCase):
    """`sync_policy.py add --backend daemon --apply` end to end, against the
    temp config.cfg only."""

    def _ns(self, **overrides):
        ns = argparse.Namespace(
            db_path=self.db_path,
            local_root=self.local_root,
            remote="yandex",
            policy_path=self.policy_path,
            legacy_filter_path=None,
            download_filter_path=None,
            bisync_filter_path=None,
            format="json",
            text_header=False,
            backend="daemon",
            exclude_config=self.config_path,
            path="/Books/Math/АнГем",
            mode="bidirectional",
            apply=True,
            force_risk=False,
        )
        for key, value in overrides.items():
            setattr(ns, key, value)
        return ns

    def _backend(self):
        return DaemonBackend(
            config_path=self.config_path,
            db_path=self.db_path,
            local_root=self.local_root,
            policy_path=self.policy_path,
        )

    def _migrate(self):
        """The documented first step: import the live exclude-dirs into policy."""
        ns = self._ns()
        del ns.path, ns.mode, ns.force_risk
        return sync_policy.migrate_policy(ns)

    def _add_and_apply(self, **overrides):
        ns = self._ns(**overrides)
        payload = sync_policy.add_policy_path(ns)
        if payload.get("error"):
            return payload, None
        policy = sync_policy.load_policy(self.policy_path)
        with patch("tools.sync_backends.stop_start_daemon", return_value={}) as restart:
            applied = self._backend().apply_policy(policy, dry_run=False)
        payload["backend_apply"] = applied
        payload["restart_calls"] = restart.call_count
        return payload, applied

    def test_exclude_dirs_written_for_a_nested_include(self):
        self.write_config(["Books", "video", "pro", "Docs"])
        self._migrate()
        # Everything that stays synced must exist locally, or the deletion
        # guard (correctly) refuses to restart.
        self.materialize_local(["Books"])
        payload, applied = self._add_and_apply()
        self.assertIsNone(payload["error"])
        self.assertEqual(
            self.read_exclude_dirs(),
            sorted([
                "Books/42",
                "Books/Math/Анализ",
                "Books/Math/Теорвер",
                "Books/cpp",
                "Books/История",
                "Docs",
                "pro",
                "video",
            ]),
        )
        self.assertEqual(applied["removed"], ["Books"])
        self.assertEqual(payload["restart_calls"], 1)

    def test_repeated_add_is_idempotent(self):
        self.write_config(["Books", "video", "pro", "Docs"])
        self._migrate()
        self.materialize_local(["Books"])
        self._add_and_apply()
        first = self.read_exclude_dirs()
        self._add_and_apply()
        self.assertEqual(self.read_exclude_dirs(), first)

    def test_download_only_is_rejected_by_the_daemon_backend(self):
        self.write_config(["Books", "video", "pro", "Docs"])
        self._migrate()
        self.materialize_local(["Books"])
        sync_policy.add_policy_path(self._ns(mode="download_only"))
        policy = sync_policy.load_policy(self.policy_path)
        with self.assertRaises(NotSupportedError):
            self._backend().apply_policy(policy, dry_run=True)
        # The config must be untouched by the failed attempt.
        self.assertEqual(self.read_exclude_dirs(), ["Books", "video", "pro", "Docs"])

    def test_path_unknown_to_the_snapshot_is_blocked_before_the_config(self):
        """A path the cloud snapshot has never seen must not reach coercion:
        its siblings are unknowable, so excluding them is impossible."""
        self.write_config(["Books", "video", "pro", "Docs"])
        self._migrate()
        before_policy = sync_policy.load_policy(self.policy_path)
        blind_db = self.db_path + ".blind"
        seed_cloud_db(blind_db, {"": ["Books", "video"], "/Books": ["42", "Math"]})
        payload, applied = self._add_and_apply(db_path=blind_db)
        self.assertIsNotNone(payload["error"])
        self.assertIsNone(applied)
        self.assertEqual(
            [risk["code"] for risk in payload["risk"]["risks"]], ["path_not_found"]
        )
        self.assertEqual(sync_policy.load_policy(self.policy_path), before_policy)
        self.assertEqual(self.read_exclude_dirs(), ["Books", "video", "pro", "Docs"])

    def test_add_without_migrate_never_clears_the_exclude_list(self):
        """`add --apply` without a prior `migrate` used to hand the daemon an
        empty exclude list — i.e. the entire cloud."""
        self.write_config(["Books", "video", "pro", "Docs"])
        self.materialize_local(["Books", "video", "pro", "Docs"])
        with self.assertRaises(BackendError) as ctx:
            self._add_and_apply(path="/video")
        self.assertIn("migrate", str(ctx.exception))
        self.assertEqual(self.read_exclude_dirs(), ["Books", "video", "pro", "Docs"])

    def test_dry_run_reports_the_cleared_exclude_list_without_raising(self):
        self.write_config(["Books", "video", "pro", "Docs"])
        result = self._backend().apply_policy({"paths": {}}, dry_run=True)
        self.assertTrue(result["clears_exclude_dirs"])
        self.assertEqual(result["after"], [])


class TestMigrateFromDaemon(DaemonBenchCase):
    """`migrate --backend daemon` imports exclude-dirs, and rendering the
    resulting policy must reproduce the very same list."""

    def test_roundtrip(self):
        excludes = ["Books/42", "Books/Math/Анализ", "Docs", "video"]
        self.write_config(excludes)
        ns = argparse.Namespace(
            db_path=self.db_path,
            local_root=self.local_root,
            remote="yandex",
            policy_path=self.policy_path,
            legacy_filter_path=None,
            download_filter_path=None,
            bisync_filter_path=None,
            format="json",
            text_header=False,
            backend="daemon",
            exclude_config=self.config_path,
            apply=True,
        )
        result = sync_policy.migrate_policy(ns)
        self.assertIsNone(result["error"])
        self.assertEqual(result["source"], "daemon")
        policy = sync_policy.load_policy(self.policy_path)
        backend = DaemonBackend(
            config_path=self.config_path,
            db_path=self.db_path,
            local_root=self.local_root,
            policy_path=self.policy_path,
        )
        self.assertEqual(backend._policy_to_exclude_dirs(policy), sorted(excludes))


if __name__ == "__main__":
    unittest.main()
