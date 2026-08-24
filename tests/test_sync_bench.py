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
            env=self.bench.env(),
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


class TestDisabledIsNotSynced(BenchTestCase):
    """G6: a `disabled` entry used to answer "is anything synced below here?".

    One conflation, three wrong answers — a false `[P]`, an excluded subtree
    kept whole by the mode whose job is to hide unsynced branches, and an
    orphan dropped from the menu. Each gets its own check, because each could
    come back on its own.
    """

    def _collapsed_tree(self, backend="rclone"):
        """The default view: collapse on, which is what a person actually sees."""
        proc = self.run_tool(
            "sync_tree.py", "--path", "/", "--depth", "4",
            "--format", "json", "--schema", "sync_tree:v2",
            "--no-local-scan", *self.bench.cli_args(backend),
        )
        return json.loads(proc.stdout)["root"]

    @staticmethod
    def _find(node, path):
        if node["path"] == path:
            return node
        for child in node.get("children") or []:
            found = TestDisabledIsNotSynced._find(child, path)
            if found:
                return found
        return None

    def test_nothing_inside_a_disabled_tree_claims_a_synced_child(self):
        markers = render_markers(self.bench, "rclone", depth=4)
        inside = {p: m for p, m in markers.items() if p.startswith("/Books/")}
        self.assertTrue(inside, "the sample tree has stopped covering /Books")
        self.assertNotIn("[P]", inside.values(), inside)

    def test_the_collapsed_tree_stops_expanding_a_disabled_subtree(self):
        """Collapse shows synced branches; a disabled one is the opposite.

        What survives under `[X]` is only what a separate, deliberate rule
        keeps — folders that exist on disk, so a local orphan is never hidden.
        `/Books/Keep/Old` is in the cloud and nowhere else, and it is exactly
        what the whole-subtree expansion used to drag into the view.
        """
        root = self._collapsed_tree()
        books = self._find(root, "/Books")
        self.assertIsNotNone(books)
        self.assertEqual(books["markers"]["display"], "[X]")
        self.assertIsNone(self._find(books, "/Books/Keep/Old"), "cloud-only child kept")
        for child in books.get("children") or []:
            self.assertTrue(
                os.path.isdir(os.path.join(self.bench.local_root, child["path"].lstrip("/"))),
                f"{child['path']} is under [X] and not on disk",
            )

    def test_collapsing_still_keeps_a_synced_subtree_whole(self):
        """The other direction: the fix must not turn collapse into a blunt cut."""
        root = self._collapsed_tree()
        shared = self._find(root, "/shared")
        self.assertIsNotNone(shared)
        self.assertEqual(
            [c["path"] for c in shared.get("children") or []], ["/shared/live"]
        )

    def test_a_policy_that_syncs_nothing_says_so(self):
        """The live configuration, and the one this fix moves the most.

        `var/sync_policy.json` on the working machine holds 52 disabled entries
        and not one that syncs — so the set of synced paths is empty and every
        `[P]` in that tree was false. Measured on the real snapshot on
        2026-08-24: 687 of 3810 nodes changed, 665 to `[.]` and 22 to `[L]`.
        Since the tree here is a whitelist render, this is worth its own case.
        """
        policy = self.bench.read_policy()
        policy["paths"] = {
            name: meta for name, meta in policy["paths"].items()
            if meta["mode"] == "disabled"
        }
        self.assertTrue(policy["paths"], "the sample tree has stopped covering [X]")
        with open(self.bench.policy_path, "w", encoding="utf-8") as handle:
            json.dump(policy, handle, ensure_ascii=False)

        markers = render_markers(self.bench, "rclone", depth=4)
        self.assertNotIn("[P]", markers.values(), markers)
        self.assertNotIn("[B]", markers.values(), markers)
        # Nothing is synced, so the disk is exactly what is on it and what is
        # only in the cloud.
        self.assertEqual(set(markers.values()) - {"[X]"}, {"[L]", "[.]"}, markers)

        proc = self.run_tool(
            "ydm_menu.py", *self.bench.cli_args("rclone"), "orphans"
        )
        listed = {e["cloud_path"] for e in json.loads(proc.stdout)["orphans"]}
        self.assertNotIn("/", listed, "the sync root offered as an orphan")
        self.assertTrue(listed)


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

    def test_an_orphan_is_not_hidden_by_having_a_descendant(self):
        """G6, third consequence, and the worst of the three.

        `/Books/Math` and `/Books/Keep` stand identically: on disk, inside a
        disabled tree, with no entry of their own. Only one has a child in the
        snapshot, and that alone used to decide which of them the menu offered.
        A missing row cannot be told apart from "not an orphan" by reading the
        output, so this failed silently.
        """
        listed = {e["cloud_path"] for e in json.loads(self.menu("orphans").stdout)["orphans"]}
        self.assertIn("/Books/Keep", listed)
        self.assertIn("/Books/Math", listed)

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


class TestRcloneOnTheBench(BenchTestCase):
    """The rclone-backed paths, run for real against a local remote.

    These close the cheap two thirds of "on Android `ydm-menu` works through
    rclone", which sat blocked on "needs a second machine" since 2026-08-16.
    Most of it was never about the device: it was about rclone, and rclone will
    happily address a directory. See `tasks/android_verify/GAP.md`.

    Unlike everything above, this reaches past "what does the system say" and
    into what it does with files — `sync_filters add --apply` really copies
    them.
    """

    def setUp(self):
        if shutil.which("rclone") is None:
            self.skipTest("rclone is not installed here (nor in CI)")
        super().setUp()

    def rclone(self, *args, expect_ok=True):
        proc = subprocess.run(
            ["rclone", *args], capture_output=True, text=True, check=False,
            env=self.bench.env(),
        )
        if expect_ok:
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return proc

    def test_the_bench_remote_is_the_only_one_in_reach(self):
        """The isolation guard for this class.

        `rclone listremotes` reads RCLONE_CONFIG first, so if the bench failed
        to set it, the live `yandex:` remote would answer instead — and every
        check below would pass while talking to the real cloud.
        """
        listed = sorted(self.rclone("listremotes").stdout.split())
        self.assertEqual(listed, ["android:", "cloud:"], listed)

    def test_the_fake_cloud_holds_what_the_snapshot_claims(self):
        """Two descriptions of the same cloud must not drift apart."""
        listed = {
            line.rstrip("/") for line in
            self.rclone("lsf", "cloud:", "--dirs-only").stdout.splitlines() if line.strip()
        }
        expected = {
            entry.path.strip("/") for entry in SAMPLE_TREE
            if entry.path != "/" and "/" not in entry.path.strip("/")
        }
        self.assertEqual(listed, expected)

    def test_list_cloud_children_is_the_pick_mechanism(self):
        """`ydm-sync-pick` exists so nobody types Cyrillic folder names by hand.

        It lists the children of a folder through `rclone lsf`; this is that
        call, through the backend object the menu actually uses.
        """
        from tools.sync_backends import RcloneBackend

        backend = RcloneBackend(
            remote="cloud", db_path=self.bench.db_path,
            local_root=self.bench.local_root, policy_path=self.bench.policy_path,
        )
        os.environ["RCLONE_CONFIG"] = self.bench.rclone_config
        try:
            self.assertEqual(backend.list_cloud_children("/Books"), ["Keep", "Math"])
            self.assertIn("АнГем", backend.list_cloud_children("/Books/Math"))
        finally:
            os.environ.pop("RCLONE_CONFIG", None)

    def test_the_menu_answers_the_same_with_a_remote_configured(self):
        """The checklist item as written: does the menu still work over rclone."""
        proc = self.run_tool(
            "ydm_menu.py", *self.bench.cli_args("rclone"), "--remote", "cloud", "orphans",
        )
        from_menu = {e["cloud_path"] for e in json.loads(proc.stdout)["orphans"]}
        from_tree = {
            path for path, marker in render_markers(self.bench, "rclone").items()
            if marker == "[L]"
        }
        self.assertEqual(from_menu, from_tree)

    def test_adding_a_path_actually_copies_the_files(self):
        """Policy → filter file → rclone → files on disk, end to end.

        `sync_filters add --apply` writes the filter and then materializes it
        with `rclone copy`. Nothing above this line in the file checks that a
        file ever moves.

        Note the output shape: rclone streams its own progress to stdout ahead
        of the JSON, deliberately — on a real folder the copy runs for minutes
        and buffering it would look like a hang.
        """
        target = os.path.join(self.tmpdir, "materialized")
        filter_path = os.path.join(self.tmpdir, "sync.filters")
        proc = self.run_tool(
            "sync_filters.py", "add", "--path", "/pro",
            "--db-path", self.bench.db_path, "--local-root", target,
            "--filter-path", filter_path, "--remote", "cloud",
            "--format", "json", "--apply",
        )
        payload = json.loads(proc.stdout[proc.stdout.index("{"):])["data"]
        self.assertIsNone(payload.get("error"), payload)
        self.assertEqual(payload["materialize"]["returncode"], 0, payload["materialize"])

        copied = sorted(os.listdir(os.path.join(target, "pro")))
        expected = sorted(
            f"f{i}.txt" for i in range(
                next(e.cloud_files for e in SAMPLE_TREE if e.path == "/pro")
            )
        )
        self.assertEqual(copied, expected)
        with open(filter_path, encoding="utf-8") as handle:
            self.assertIn("pro", handle.read())

    def test_the_copy_logs_land_in_the_bench_and_not_in_the_project(self):
        """The isolation hole this phase found, pinned so it cannot come back.

        `rclone_copy_materialize()` derives its log paths from PROJECT_ROOT, so
        before `YDM_VAR_DIR` existed a bench run appended to the live
        `var/copy.log` and overwrote the live `var/copy_last.log` — the
        operational logs, on a machine that syncs 1.5 TB. No argument could
        redirect it, because no argument reaches it.
        """
        live_var = ROOT_DIR / "var"
        before = {
            name: (live_var / name).stat().st_mtime
            for name in ("copy.log", "copy_last.log")
            if (live_var / name).exists()
        }

        target = os.path.join(self.tmpdir, "materialized2")
        self.run_tool(
            "sync_filters.py", "add", "--path", "/pro",
            "--db-path", self.bench.db_path, "--local-root", target,
            "--filter-path", os.path.join(self.tmpdir, "s2.filters"),
            "--remote", "cloud", "--format", "json", "--apply",
        )

        self.assertIn("copy_last.log", os.listdir(self.bench.var_dir))
        for name, mtime in before.items():
            self.assertEqual(
                (live_var / name).stat().st_mtime, mtime,
                f"the bench wrote to the live var/{name}",
            )

    def test_bisync_establishes_a_baseline_and_then_runs_dry(self):
        """The one that moves files in both directions, so the one worth pinning.

        It was an open question whether bisync could be covered here at all;
        it can. `resync --apply` establishes the baseline and materializes the
        folder, and a plain `run` stays a dry run — which is the default the
        scheduled job depends on.
        """
        target = os.path.join(self.tmpdir, "bisync_local")
        os.makedirs(target)
        filter_path = os.path.join(self.tmpdir, "bisync.filters")
        with open(filter_path, "w", encoding="utf-8") as handle:
            handle.write("+ /pro/**\n- *\n")

        def bisync(*args):
            proc = self.run_tool(
                "sync_bisync.py", *args,
                "--db-path", self.bench.db_path, "--local-root", target,
                "--filter-path", filter_path, "--remote", "cloud",
                "--policy-path", self.bench.policy_path,
                "--format", "json", "--no-check-access",
            )
            body = proc.stdout[proc.stdout.index("{"):]
            payload = json.loads(body)
            return payload.get("data", payload)

        baseline = bisync("resync", "--apply")
        self.assertIsNone(baseline.get("error"), baseline)
        self.assertFalse(baseline["dry_run"])
        self.assertEqual(sorted(os.listdir(os.path.join(target, "pro"))), ["f0.txt", "f1.txt"])

        following = bisync("run")
        self.assertIsNone(following.get("error"), following)
        self.assertTrue(following["dry_run"], "a plain `run` must not write")

    def test_a_restricted_name_survives_an_encoded_remote(self):
        """The mechanism behind the Android filename failure, modelled locally.

        Android's shared storage refuses `|` and `:`, and today the answer is
        to repair the names afterwards — by which point the original is gone.
        rclone's `encoding` option substitutes full-width characters on write
        and gives the original name back on read, so nothing is lost.

        What this does *not* show is that the same setting clears
        `operation not permitted` on a device: the refusal comes from the
        filesystem, and encoding acts before it. That check needs Android and
        is `tasks/android_verify/BACKLOG.md` 2.2.
        """
        src = os.path.join(self.tmpdir, "restricted_src")
        dst = os.path.join(self.tmpdir, "restricted_dst")
        os.makedirs(src)
        original = "Agents Week 2026 | notes:draft.pdf"
        with open(os.path.join(src, original), "w", encoding="utf-8") as handle:
            handle.write("x")

        self.rclone("copy", src, f"android:{dst}")

        on_disk = os.listdir(dst)
        self.assertEqual(len(on_disk), 1, on_disk)
        self.assertNotEqual(on_disk[0], original, "nothing was encoded")
        self.assertNotIn("|", on_disk[0])
        self.assertNotIn(":", on_disk[0])

        read_back = self.rclone("lsf", f"android:{dst}").stdout.strip()
        self.assertEqual(read_back, original, "the encoding does not round-trip")


class TestMenuScreens(BenchTestCase):
    """The screens themselves, driven by a scripted reader.

    Not one of the six screens had a test before 2026-08-24, which is where
    every hardcoded path in the menu had been sitting undisturbed. Nothing
    blocked this: `Reader` has always been injectable, and the prompt tests
    inject it. See tasks/ydm_menu/AUDIT-2026-08-24.md G.

    These check what the person is shown — the paths offered and the actions
    taken — not merely that nothing raised.
    """

    def cfg(self, backend="rclone"):
        from tools.ydm_menu_config import MenuConfig

        return MenuConfig.from_env_and_args(
            db_path=self.bench.db_path,
            local_root=self.bench.local_root,
            policy_path=self.bench.policy_path,
            backend=backend,
            plain=True,
        )

    def run_screen(self, screen, answers, backend="rclone"):
        """Drive one screen to completion and return everything it printed."""
        import contextlib
        import io

        from tools.ydm_menu_prompts import scripted_reader

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            screen(self.cfg(backend), scripted_reader(answers))
        return buffer.getvalue()

    def test_a_screen_that_over_asks_fails_loudly(self):
        """The guard that keeps a wrong script from hanging the whole run."""
        from tools.ydm_menu import screen_tree

        with self.assertRaises(AssertionError):
            self.run_screen(screen_tree, [])

    def test_back_leaves_every_screen_without_acting(self):
        """`0` is the way out of each menu, and it must change nothing.

        `screen_cloud_scan` is absent on purpose: it opens by asking the cloud
        what changed, so driving it here would put a network call inside an
        offline test. Its branches are covered in TestSmartCloudScan, where
        that call is stubbed.
        """
        from tools import ydm_menu

        before = self.bench.read_policy()
        for name in ("screen_tree", "screen_add_cloud", "screen_remove",
                     "_scan_one_folder"):
            with self.subTest(screen=name):
                self.run_screen(getattr(ydm_menu, name), ["0"])
        self.assertEqual(before, self.bench.read_policy())

    def test_the_paths_offered_are_not_from_someone_elses_machine(self):
        """The finding this class exists for.

        Three screens used to offer a list typed in by hand — `/Books/Math`,
        `/DAO`, `/pro/agents` — which mean nothing on another machine, and on
        the author's own `/Books` is disabled outright. Whatever a screen
        offers now has to come from this bench.
        """
        from tools import ydm_menu

        known = {entry.path for entry in SAMPLE_TREE} | {"/"}
        screens = ("screen_tree", "screen_add_cloud", "_scan_one_folder")
        for name in screens:
            with self.subTest(screen=name):
                text = self.run_screen(getattr(ydm_menu, name), ["0"])
                offered = [
                    token for line in text.splitlines()
                    for token in line.split()
                    if token.startswith("/")
                ]
                self.assertTrue(offered, f"{name} offered no paths at all:\n{text}")
                for path in offered:
                    normalized = path.rstrip("/") or "/"
                    self.assertIn(normalized, known, f"{name}:\n{text}")

    def test_the_diff_is_reachable_from_the_menu(self):
        """The comparison the project exists for, previously CLI-only."""
        import contextlib
        import io

        from tools.ydm_menu_actions import print_cloud_local_diff

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            print_cloud_local_diff(self.cfg())
        text = buffer.getvalue()
        self.assertIn("in the cloud", text)
        self.assertIn("matched locally", text)
        self.assertIn("missing locally", text)

    def test_the_menu_lists_the_diff_entry(self):
        from tools.ydm_menu_screens import render_main_menu
        from tools.ydm_menu_status import load_status

        cfg = self.cfg()
        text = "\n".join(render_main_menu(cfg, load_status(cfg)))
        self.assertIn(" d  ", text)
        # Numbers stayed where they were: renumbering would break the muscle
        # memory the design deliberately preserved.
        self.assertIn(" 2  Add folder from cloud", text)
        self.assertIn(" 8  Detailed status", text)

    def test_removing_a_folder_needs_its_name_typed_back(self):
        """Three barriers on remove, and the bench policy proves they held."""
        from tools.ydm_menu import screen_remove

        before = self.bench.read_policy()
        # Pick entry 1, confirm, then fail the name check.
        text = self.run_screen(screen_remove, ["1", "y", "not-the-name"])
        self.assertIn("Cancelled", text)
        self.assertEqual(before, self.bench.read_policy(), "policy changed anyway")

class TestSmartCloudScan(BenchTestCase):
    """Item 7, which now asks what changed before offering to scan.

    Every check here stubs the two cloud calls. That is not only for speed:
    written without stubs, the first version of these tests reached the live
    Yandex Disk API — in-process, so the bench's environment does not reach
    them, and the token in `.env` does. A menu test must not be able to touch
    the network at all.
    """

    def cfg(self):
        from tools.ydm_menu_config import MenuConfig

        return MenuConfig.from_env_and_args(
            db_path=self.bench.db_path,
            local_root=self.bench.local_root,
            policy_path=self.bench.policy_path,
            backend="rclone",
            plain=True,
        )

    def run_scan_screen(self, answers, *, check, stale=None):
        import contextlib
        import io
        from unittest.mock import patch

        from tools import ydm_menu
        from tools.ydm_menu_prompts import scripted_reader

        buffer = io.StringIO()
        with patch.object(ydm_menu, "check_cloud_changes", return_value=check), \
             patch.object(ydm_menu, "list_stale_folders", return_value=stale), \
             patch.object(ydm_menu, "run_cloud_scan") as scan, \
             contextlib.redirect_stdout(buffer):
            ydm_menu.screen_cloud_scan(self.cfg(), scripted_reader(answers))
        return buffer.getvalue(), scan

    @staticmethod
    def result(ok, message, details=None):
        from tools.ydm_menu_actions import ActionResult

        return ActionResult(ok, message, details)

    def test_a_missing_token_is_reported_not_exited_on(self):
        """`resolve_token()` raises SystemExit, which is not an Exception.

        Caught the wrong way, a menu without credentials does not say so — it
        quits. The screen tests stub this function out, so only a direct check
        can hold the distinction: a mutation removing SystemExit from the
        `except` passed everything until this existed.
        """
        from unittest.mock import patch

        from tools import ydm_menu_actions

        with patch.object(
            ydm_menu_actions, "_delta_ns", side_effect=SystemExit("no token")
        ):
            result = ydm_menu_actions.check_cloud_changes(self.cfg())
        self.assertFalse(result.ok)
        self.assertIn("no token", result.message)

        with patch.object(
            ydm_menu_actions, "_delta_ns", side_effect=SystemExit("no token")
        ):
            result = ydm_menu_actions.list_stale_folders(self.cfg())
        self.assertFalse(result.ok)

    def test_nothing_changed_is_an_answer_not_an_empty_screen(self):
        """One request, and the useful outcome is usually "no work to do"."""
        check = self.result(True, "checked", {
            "changed": False, "previous_checked_at": "2026-08-23 06:32:28",
        })
        text, scan = self.run_scan_screen(["0"], check=check)
        self.assertIn("Nothing has changed since 2026-08-23 06:32:28", text)
        self.assertIn("current", text)
        scan.assert_not_called()

    def test_stale_folders_are_offered_for_refresh(self):
        check = self.result(True, "checked", {"changed": True})
        stale = self.result(True, "inspected", {
            "totals": {"stale_folders": 2, "changed_files": 31, "deleted_entries": 1},
            "rescan_roots": ["/pro", "/video"],
            "stale_folders": [{"path": "/pro", "changed_files": 30, "deleted_entries": 0}],
            "warnings": [],
        })
        text, scan = self.run_scan_screen(["1"], check=check, stale=stale)
        self.assertIn("2 stale folder(s), 31 changed file(s), 1 deleted", text)
        self.assertEqual([c.args[1] for c in scan.call_args_list], ["/pro", "/video"])

    def test_a_truncated_sweep_is_not_silently_a_clean_one(self):
        """cloud_delta separates "nothing found" from "list incomplete"."""
        check = self.result(True, "checked", {"changed": True})
        stale = self.result(True, "inspected", {
            "totals": {"stale_folders": 0, "changed_files": 0, "deleted_entries": 0},
            "rescan_roots": [],
            "stale_folders": [],
            "warnings": ["modified sweep hit --max-pages; the list is incomplete"],
        })
        text, _scan = self.run_scan_screen(["0"], check=check, stale=stale)
        self.assertIn("WARN:", text)
        self.assertIn("incomplete", text)

    def test_without_a_token_the_manual_path_still_works(self):
        """No credentials is a normal state; it must not end the screen."""
        check = self.result(False, "cannot check the cloud: no token")
        text, _scan = self.run_scan_screen(["0"], check=check)
        self.assertIn("cannot check the cloud", text)
        self.assertIn("token", text)
        # It fell through to the manual chooser, which lists bench paths.
        self.assertIn("/Books", text)

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

    def test_the_header_says_how_old_the_snapshot_is(self):
        """The menu used to be the one view that never mentioned this.

        `sync_tree` printed it in its header for weeks, so someone working
        from the menu decided on top of a snapshot whose age nobody had
        stated. See tasks/ydm_menu/AUDIT-2026-08-24.md D.
        """
        _cfg, text = self._render()
        line = next(
            (l for l in text.splitlines() if l.startswith("Snapshot:")), None
        )
        self.assertIsNotNone(line, text)
        self.assertIn("day(s) old", line)
        self.assertIn("% of files", line)

    def test_a_fresh_snapshot_is_stated_without_alarm(self):
        """Age is reported always; WARN belongs to the compared part only."""
        _cfg, text = self._render()
        self.assertIn("Snapshot:", text)
        self.assertNotIn("WARN", text)

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
            env=bench.env(),
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
