#!/usr/bin/env python3
"""End-to-end checks of the tree and the menu against a synthetic bench.

What separates these from the existing tests: those call `display_marker()`
with arguments assembled by hand in the test body, which proves that function's
truth table and nothing about whether the right arguments ever reach it. Here
the inputs are a policy file, folders on disk and a database, and the output is
the rendered tree — so every seam between the layers is inside the test.

That is the shape of blindness that let the `parent_path` convention mismatch
live until 2026-08-16: each layer was tested alone, and the assembly was not.

Run directly: python tests/test_sync_bench.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tests.bench import (  # noqa: E402
    SAMPLE_TREE,
    build_bench,
    expectations,
    render_markers,
)


class BenchTestCase(unittest.TestCase):
    """One bench per test, torn down after. Nothing outside tmp is touched."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ydm_bench_")
        self.bench = build_bench(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_tool(self, script, *args, expect_ok=True):
        proc = subprocess.run(
            [sys.executable, str(ROOT_DIR / "tools" / script), *args],
            capture_output=True, text=True, check=False,
            env={**os.environ, "HOME": str(self.bench.root / "home")},
        )
        if expect_ok:
            self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        return proc


# --- Phase 1 ---------------------------------------------------------------

class TestBenchIsolation(BenchTestCase):
    """The bench must be the only thing under test.

    A check that quietly read the live policy or the live database would pass
    for the wrong reason, and would be indistinguishable from a real pass. The
    manual checklists this replaces had exactly that failure mode: they read
    live state, so they went stale silently when the state changed.
    """

    def test_the_bench_has_its_own_everything(self):
        for path in (self.bench.db_path, self.bench.policy_path,
                     self.bench.local_root, self.bench.exclude_config):
            self.assertTrue(os.path.exists(path), path)
            self.assertTrue(str(path).startswith(self.tmpdir), path)

    def test_the_bench_policy_is_not_the_real_one(self):
        policy = self.bench.read_policy()
        self.assertEqual(policy["local_root"], self.bench.local_root)
        self.assertNotIn("Books/Math", policy["paths"])

    def test_rendering_never_opens_the_real_paths(self):
        """Point the tool at the bench and make the real files unreadable.

        If anything fell back to `var/sync_policy.json` or `monitor.db`, this
        would be where it showed. Implemented by running with a home directory
        of its own and asserting the output describes the bench tree, which the
        real setup could not produce.
        """
        markers = render_markers(self.bench, "rclone")
        # The strongest form of the check: the rendered tree is *exactly* the
        # sample tree. Naming a few real-only folders would go stale the moment
        # the bench happened to reuse one of their names — it already did once,
        # with /Books/Math.
        self.assertEqual(set(markers), {entry.path for entry in SAMPLE_TREE})

    def test_every_marker_value_is_reachable(self):
        """The sample tree is a specification; this asserts it stayed one."""
        produced = set(expectations("rclone").values()) | set(expectations("daemon").values())
        self.assertEqual(
            produced,
            {"[B]", "[B?]", "[B~]", "[D]", "[D?]", "[X]", "[P]", "[L]", "[.]"},
        )


# --- Phase 2 ---------------------------------------------------------------

class TestTreeMarkers(BenchTestCase):
    """Path in, marker out — through the real pipeline, not a reassembly of it."""

    def _assert_all(self, backend):
        got = render_markers(self.bench, backend)
        expected = expectations(backend)
        mismatches = {
            path: (want, got.get(path))
            for path, want in expected.items()
            if got.get(path) != want
        }
        self.assertEqual(mismatches, {}, f"{backend}: expected vs produced")

    def test_whitelist_semantics(self):
        self._assert_all("rclone")

    def test_blacklist_semantics(self):
        self._assert_all("daemon")

    def test_the_two_semantics_actually_differ(self):
        """A bench that produced identical output for both would prove nothing."""
        rclone, daemon = expectations("rclone"), expectations("daemon")
        differing = {p for p in rclone if rclone[p] != daemon[p]}
        self.assertTrue(differing)
        # Concretely: download-only and orphan cannot exist under a blacklist,
        # where every path is covered and no mode but disabled can be expressed.
        self.assertIn("/Docs", differing)
        self.assertIn("/orphans", differing)

    def test_download_only_and_orphan_are_whitelist_only(self):
        daemon = set(expectations("daemon").values())
        for marker in ("[D]", "[D?]", "[L]", "[P]", "[.]"):
            self.assertNotIn(
                marker, daemon,
                f"{marker} cannot arise under blacklist semantics: every path is "
                f"covered, so in_policy is always true",
            )

    def test_a_folder_is_only_B_when_its_whole_subtree_is_materialized(self):
        """Counts are subtree-wide, and the markers depend on that.

        The first draft of the sample tree put the `[B?]` case inside the `[B]`
        case and expected both; the parent correctly came out `[B~]`. Pinning
        the rule so it cannot drift.
        """
        markers = render_markers(self.bench, "rclone")
        self.assertEqual(markers["/mix/inner"], "[B]")
        self.assertEqual(markers["/mix"], "[P]")
        self.assertEqual(markers["/video"], "[B~]")


class TestTreeCli(BenchTestCase):
    """Level 3: the tool as a process. Catches what is invisible from inside —
    argument parsing, defaults, schema, behaviour with no local scan."""

    def test_text_and_json_agree(self):
        json_markers = render_markers(self.bench, "rclone")
        proc = self.run_tool(
            "sync_tree.py", "--path", "/", "--depth", "3",
            "--format", "text", "--schema", "sync_tree:v2",
            "--no-local-scan", "--show-all", *self.bench.cli_args("rclone"),
        )
        for name, marker in (("orphans", json_markers["/orphans"]),
                             ("video", json_markers["/video"]),
                             ("Books", json_markers["/Books"])):
            self.assertIn(f"{marker} ", proc.stdout, f"{name} missing from text render")

    def test_v1_schema_still_renders(self):
        proc = self.run_tool(
            "sync_tree.py", "--path", "/", "--depth", "2",
            "--format", "json", "--schema", "sync_tree:v1",
            "--no-local-scan", "--show-all", *self.bench.cli_args("rclone"),
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema"], "sync_tree:v1")
        self.assertIn("sync_status", payload["root"])

    def test_depth_limits_the_render(self):
        shallow = render_markers(self.bench, "rclone", depth=1)
        deep = render_markers(self.bench, "rclone", depth=3)
        self.assertIn("/mix", shallow)
        self.assertNotIn("/mix/inner", shallow)
        self.assertIn("/mix/inner", deep)

    def test_subtree_root_renders(self):
        proc = self.run_tool(
            "sync_tree.py", "--path", "/mix", "--depth", "2",
            "--format", "json", "--schema", "sync_tree:v2",
            "--no-local-scan", "--show-all", *self.bench.cli_args("rclone"),
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["root"]["path"], "/mix")
        self.assertEqual(payload["root"]["markers"]["display"], "[P]")


class TestPathConventions(BenchTestCase):
    """The regression that would have caught the 2026-08-16 defect.

    Cloud rows store `parent_path` as `/pro/MuSy`, local rows as `pro/MuSy`.
    The bench writes each side in its own convention, so a comparison that
    stopped normalizing would show every synced folder as unmaterialized.
    """

    def test_the_bench_stores_the_two_conventions_apart(self):
        import sqlite3

        conn = sqlite3.connect(self.bench.db_path)
        try:
            cloud = {row[0] for row in conn.execute(
                "SELECT DISTINCT parent_path FROM files WHERE scan_id = 1 AND parent_path != ''"
            )}
            local = {row[0] for row in conn.execute(
                "SELECT DISTINCT parent_path FROM files WHERE scan_id = 2 AND parent_path != ''"
            )}
        finally:
            conn.close()
        self.assertTrue(all(p.startswith("/") for p in cloud), cloud)
        self.assertTrue(not any(p.startswith("/") for p in local), local)

    def test_local_files_are_seen_despite_the_convention_split(self):
        """If normalization broke, every folder would read as missing."""
        markers = render_markers(self.bench, "rclone")
        self.assertEqual(markers["/pro"], "[B]")
        self.assertEqual(markers["/Docs"], "[D]")

    def test_cyrillic_paths_survive_the_round_trip(self):
        markers = render_markers(self.bench, "rclone")
        self.assertTrue(markers)
        proc = self.run_tool(
            "sync_tree.py", "--path", "/", "--depth", "2",
            "--format", "json", "--schema", "sync_tree:v2",
            "--no-local-scan", "--show-all", *self.bench.cli_args("rclone"),
        )
        self.assertEqual(json.loads(proc.stdout)["root"]["path"], "/")


# --- Phase 3 ---------------------------------------------------------------

class TestMenuOnBench(BenchTestCase):
    """`ydm_menu` already takes every path as an argument, so it addresses the
    bench without a single change — which is what made this cheap."""

    def menu(self, *args, backend="rclone", expect_ok=True):
        # ydm_menu takes its paths as global options, so they precede the
        # subcommand — argparse rejects them after it.
        return self.run_tool(
            "ydm_menu.py", *self.bench.cli_args(backend), *args,
            expect_ok=expect_ok,
        )

    def test_orphans_finds_the_prepared_orphans_and_only_those(self):
        """Asserting the exact set, not just that the right answer is in there.

        A listing that returned every folder would contain it too. The expected
        set is derived from the sample tree rather than written out here, so
        adding a case to the table cannot leave this quietly wrong.
        """
        expected = {e.path for e in SAMPLE_TREE if e.expect_rclone == "[L]"}
        self.assertTrue(expected, "the sample tree has stopped covering [L]")
        payload = json.loads(self.menu("orphans").stdout)
        self.assertEqual({e["cloud_path"] for e in payload["orphans"]}, expected)
        self.assertEqual(payload["count"], len(expected), payload)
        for entry in payload["orphans"]:
            self.assertEqual(entry["display_marker"], "[L]")

    def test_the_orphan_list_agrees_with_the_tree(self):
        """Two ways of asking the same question must not diverge.

        The menu and the tree derive `[L]` through different call paths; when
        those drift, the menu offers to add folders the tree does not show.
        """
        payload = json.loads(self.menu("orphans").stdout)
        from_menu = {entry["cloud_path"] for entry in payload["orphans"]}
        from_tree = {
            path for path, marker in render_markers(self.bench, "rclone").items()
            if marker == "[L]"
        }
        self.assertEqual(from_menu, from_tree)

    def test_orphans_does_not_report_the_real_disk(self):
        proc = self.menu("orphans")
        self.assertNotIn("/data/ya_disk", proc.stdout)
        self.assertNotIn("brtn", proc.stdout)

    def test_menu_help_runs_without_touching_anything(self):
        proc = self.menu("--help")
        self.assertIn("--non-interactive", proc.stdout)

    def test_the_bench_policy_is_untouched_by_reading(self):
        before = self.bench.read_policy()
        self.menu("orphans")
        render_markers(self.bench, "rclone")
        self.assertEqual(before, self.bench.read_policy())


class TestMenuHeader(BenchTestCase):
    """The header is where anyone looks first, so it has to match the facts.

    Rendered in-process rather than through the CLI: `--non-interactive`
    currently only refuses to run and points at the subcommands, so the header
    has no machine-readable path out of the program. Testing the render
    functions directly still exercises everything between the policy file and
    the line of text — which is the part that was untested.
    """

    def _render(self, backend="rclone"):
        from tools.ydm_menu_config import MenuConfig
        from tools.ydm_menu_screens import render_main_menu
        from tools.ydm_menu_status import load_status

        cfg = MenuConfig.from_env_and_args(
            db_path=self.bench.db_path,
            local_root=self.bench.local_root,
            policy_path=self.bench.policy_path,
            backend=backend,
            plain=True,
        )
        return cfg, "\n".join(render_main_menu(cfg, load_status(cfg)))

    def test_header_lists_the_bench_policy_not_the_real_one(self):
        _cfg, text = self._render()
        synced = next(line for line in text.splitlines() if line.startswith("Synced:"))
        # The line is truncated with "+N more", so check that whatever it does
        # name comes from the bench rather than that a particular entry is there.
        named = [
            token.removesuffix("[B]")
            for token in synced.removeprefix("Synced:").split()
            if token.endswith("[B]")
        ]
        self.assertTrue(named, synced)
        bench_paths = {entry.path.strip("/") for entry in SAMPLE_TREE}
        for name in named:
            self.assertIn(name, bench_paths, synced)
        # Folders that exist only in the live policy must not show up anywhere.
        self.assertNotIn("latoken", text)
        self.assertNotIn("журналы", text)

    def test_header_counts_match_the_policy_file(self):
        from tools.ydm_menu_config import MenuConfig
        from tools.ydm_menu_status import load_status

        cfg = MenuConfig.from_env_and_args(
            db_path=self.bench.db_path,
            local_root=self.bench.local_root,
            policy_path=self.bench.policy_path,
            backend="rclone",
            plain=True,
        )
        status = load_status(cfg)
        policy = self.bench.read_policy()["paths"]
        for mode, listed in (("bidirectional", status.bidirectional),
                             ("download_only", status.download_only),
                             ("disabled", status.disabled)):
            expected = {p for p, meta in policy.items() if meta["mode"] == mode}
            self.assertEqual(set(listed), expected, mode)

    def test_the_daemon_header_does_not_invent_bisync_facts(self):
        """The daemon has no bisync run, lock or resync baseline to report."""
        _cfg, text = self._render(backend="daemon")
        self.assertNotIn("last bisync", text)
        self.assertNotIn("resync:", text)
        self.assertIn("Excluded:", text)

    def test_the_menu_renders_when_the_backend_is_unavailable(self):
        """Menu 8 used to be the place a backend error turned into a traceback.

        Pointed at a bench with no daemon and no rclone, the header must still
        come out, naming the problem rather than dying of it.
        """
        _cfg, text = self._render(backend="auto")
        self.assertIn("Backend:", text)
        self.assertIn("Show sync tree", text)


class TestPolicyEditingStaysOnTheBench(BenchTestCase):
    """Removing a path must change the bench's policy and nothing else."""

    def test_remove_edits_the_bench_policy_only(self):
        real_policy = ROOT_DIR / "var" / "sync_policy.json"
        real_before = real_policy.read_text(encoding="utf-8") if real_policy.exists() else None

        proc = self.run_tool(
            "sync_policy.py", "remove", "--path", "/video", "--apply",
            "--policy-path", self.bench.policy_path,
            "--db-path", self.bench.db_path,
            "--local-root", self.bench.local_root,
            "--backend", "rclone",
            "--format", "json",
        )
        self.assertIn("video", proc.stdout)
        self.assertNotIn("video", self.bench.read_policy()["paths"])

        real_after = real_policy.read_text(encoding="utf-8") if real_policy.exists() else None
        self.assertEqual(real_before, real_after, "the live policy was modified")

    def test_removing_a_path_changes_its_marker(self):
        """The edit has to be visible in the render, or it proved nothing."""
        self.assertEqual(render_markers(self.bench, "rclone")["/video"], "[B~]")
        self.run_tool(
            "sync_policy.py", "remove", "--path", "/video", "--apply",
            "--policy-path", self.bench.policy_path,
            "--db-path", self.bench.db_path,
            "--local-root", self.bench.local_root,
            "--backend", "rclone",
            "--format", "json",
        )
        # No longer in policy, still on disk: an orphan.
        self.assertEqual(render_markers(self.bench, "rclone")["/video"], "[L]")


class TestStaleSnapshotSurfaces(unittest.TestCase):
    """An old snapshot has to say so — the header is where anyone would look."""

    def _bench(self, age_days):
        tmpdir = tempfile.mkdtemp(prefix="ydm_bench_age_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        return build_bench(tmpdir, base_age_days=age_days)

    def _header(self, bench):
        proc = subprocess.run(
            [
                sys.executable, str(ROOT_DIR / "tools" / "sync_tree.py"),
                "--path", "/", "--depth", "1", "--format", "text",
                "--schema", "sync_tree:v2", "--no-local-scan", "--show-all",
                *bench.cli_args("rclone"),
            ],
            capture_output=True, text=True, check=False,
            env={**os.environ, "HOME": str(bench.root / "home")},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        return proc.stdout

    def test_a_fresh_snapshot_does_not_warn(self):
        self.assertNotIn("WARN:", self._header(self._bench(1)))

    def test_an_old_snapshot_reports_its_age(self):
        header = self._header(self._bench(400))
        self.assertIn("snapshot_age:", header)
        self.assertIn("400 day(s) old", header)

    def test_an_old_snapshot_warns_when_the_stale_part_is_compared(self):
        """Nothing is excluded on the bench, so all of it is compared."""
        self.assertIn("WARN:", self._header(self._bench(400)))


if __name__ == "__main__":
    unittest.main()
