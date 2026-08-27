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
    make_daemon_policy,
    read_exclude_dirs,
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


class TestBisyncFilterSelection(BenchTestCase):
    """Which filter `sync_bisync` reaches for when nobody says.

    Two filters describe different things: `<root>.download.filters` says what
    to materialize locally, `<root>.bisync.filters` says what may be sent back.
    The split arrived with the policy layer; `sync_bisync`'s defaults did not
    move with it and kept naming the pre-split `<root>.filters`, so `status`
    hashed a file the baseline was never taken from, and `resync` — the one
    command with no hash guard, because it is the one that writes the baseline
    — would happily make a bisync baseline out of a download set.

    No rclone needed: the selection and the refusal both happen before it.
    """

    def _filters(self):
        base = self.bench.local_root.rstrip("/")
        paths = {
            "legacy": f"{base}.filters",
            "download": f"{base}.download.filters",
            "bisync": f"{base}.bisync.filters",
        }
        # Distinguishable content, so a wrong pick is visible rather than
        # merely undetected: only the bidirectional one includes anything.
        with open(paths["legacy"], "w", encoding="utf-8") as handle:
            handle.write("+ /pro/**\n+ /Books/**\n- **\n")
        with open(paths["download"], "w", encoding="utf-8") as handle:
            handle.write("- **\n")
        with open(paths["bisync"], "w", encoding="utf-8") as handle:
            handle.write("+ /pro/**\n- **\n")
        return paths

    def _bisync(self, *args):
        proc = self.run_tool(
            "sync_bisync.py", *args,
            "--db-path", self.bench.db_path,
            "--local-root", self.bench.local_root,
            "--policy-path", self.bench.policy_path,
            "--remote", "cloud", "--format", "json",
        )
        body = proc.stdout[proc.stdout.index("{"):]
        payload = json.loads(body)
        return payload.get("data", payload)

    def test_status_reads_the_bidirectional_filter_by_default(self):
        paths = self._filters()
        data = self._bisync("status")
        self.assertEqual(data["filter_path"], paths["bisync"])

    def test_resync_refuses_a_download_filter(self):
        paths = self._filters()
        data = self._bisync("resync", "--apply", "--filter-path", paths["download"])
        self.assertIsNotNone(data["error"])
        self.assertIn("download filter", data["error"])

    def test_resync_refuses_the_pre_policy_filter_too(self):
        """`<root>.filters` is a download set as well — an older one."""
        paths = self._filters()
        data = self._bisync("resync", "--apply", "--filter-path", paths["legacy"])
        self.assertIsNotNone(data["error"])
        self.assertIn("download filter", data["error"])

    def test_force_filter_gets_past_the_refusal(self):
        """The override is an override, not a second opinion.

        The download filter here includes nothing, so once the guard is out of
        the way the run stops at the next check — which is how we can tell it
        was reached without letting rclone anywhere near this test.
        """
        paths = self._filters()
        data = self._bisync(
            "resync", "--apply", "--force-filter", "--filter-path", paths["download"],
        )
        self.assertIsNotNone(data["error"])
        self.assertNotIn("download filter", data["error"])
        self.assertIn("No folders included", data["error"])


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


# --- Phase 9 ---------------------------------------------------------------

class TestTreeWalkConnectionReuse(BenchTestCase):
    """The walk opens one connection, not one per query.

    PR #4 fixed the query; this is what was left. `get_connection()` opens a
    fresh sqlite3 connection *and* re-issues `PRAGMA synchronous=NORMAL` and
    `PRAGMA journal_mode=WAL` every time. Measured on the author's snapshot
    (75 510 rows, 5 506 directories): 0.92 ms of setup against 0.10 ms for the
    query it was opened for, and a depth-4 walk opened 8 504 of them — 78% of
    the wall clock spent connecting.

    `journal_mode` is a property of the database file, not of the connection,
    so re-declaring it per connection buys nothing at all.

    Counting connections rather than timing: a stopwatch assertion is flaky on
    a loaded machine and says nothing about why. The count is exact, and it is
    the thing that regressed.
    """

    def walk(self, depth=3):
        """Build the tree the way the tools do, counting connections opened.

        `sqlite3.connect` is what is counted, not `get_connection()`: the
        latter is still called once per query, it just stops opening anything.
        Counting calls instead of connections was this test's first mistake,
        and it reported success unchanged either way.
        """
        import ydm
        from tools.sync_common import create_storage
        from tools.sync_tree import build_tree
        from tools.sync_tree_cloud import select_snapshot_for_tree
        from ydm import Analyzer

        opened = []
        original = ydm.sqlite3.connect

        def counted(*args, **kwargs):
            conn = original(*args, **kwargs)
            opened.append(conn)
            return conn

        analyzer = Analyzer(create_storage(self.bench.db_path))
        selection = select_snapshot_for_tree(analyzer, "/")
        ydm.sqlite3.connect = counted
        try:
            node = build_tree(analyzer, selection.snapshot, "/", depth)
        finally:
            ydm.sqlite3.connect = original
        return node, opened

    def test_the_walk_opens_one_connection(self):
        node, opened = self.walk()
        self.assertTrue(node.children, "the walk found nothing to measure")
        self.assertLessEqual(
            len(opened), 2,
            f"the walk opened {len(opened)} connections; on a real snapshot "
            "that is thousands, and the setup costs nine times the query",
        )

    def test_the_tree_is_the_same_tree(self):
        """Reuse must not change a single answer."""
        markers = render_markers(self.bench, "rclone")
        self.assertEqual(expectations("rclone"), markers)

    def test_a_reused_connection_survives_being_closed(self):
        """Callers close what they are handed; inside the scope that is shared.

        Every DB helper in the project does `conn.close()` in a `finally`.
        If closing the shared connection really closed it, the second helper
        in the walk would fail — so the scope hands out a handle whose close()
        is a no-op, and the walk keeps working.
        """
        from tools.sync_common import create_storage

        storage = create_storage(self.bench.db_path)
        with storage.reuse_connection():
            first = storage.get_connection()
            first.execute("SELECT COUNT(*) FROM files").fetchone()
            first.close()
            second = storage.get_connection()
            # Still usable after a caller "closed" it.
            second.execute("SELECT COUNT(*) FROM files").fetchone()
            second.close()

    def test_a_row_factory_does_not_leak_to_the_next_caller(self):
        """Analyzer sets `row_factory = sqlite3.Row` on connections it opens.

        With one connection shared, that setting would outlive the caller and
        hand tuples-expecting code sqlite3.Row objects instead. Closing the
        handle has to put it back.
        """
        import sqlite3

        from tools.sync_common import create_storage

        storage = create_storage(self.bench.db_path)
        with storage.reuse_connection():
            first = storage.get_connection()
            first.row_factory = sqlite3.Row
            first.close()
            second = storage.get_connection()
            row = second.execute("SELECT COUNT(*) FROM files").fetchone()
            self.assertIsInstance(row, tuple, "row_factory leaked out")
            second.close()

    def test_outside_the_scope_nothing_changes(self):
        """The default stays one connection per call — this is opt-in."""
        from tools.sync_common import create_storage

        storage = create_storage(self.bench.db_path)
        first = storage.get_connection()
        second = storage.get_connection()
        self.assertIsNot(first, second)
        first.close()
        second.close()


# --- Phase 8.7 -------------------------------------------------------------

class TestAddPreview(BenchTestCase):
    """Adding shows what it will change before it changes it.

    Removal has three barriers; adding had none, although adding is what
    caused the 2026-08-14 incident. The first design answer was "confirm when
    `action_inspect()` sees a risk", and it was withdrawn: on 14.08 there was
    no risk to see — the path existed and was fully materialized — so a
    risk-gated prompt would have stayed silent exactly when it was needed.

    What would have spoken is the delta. `/Books` was `disabled`, and adding
    a folder beneath it makes the daemon stop excluding `Books` altogether.
    That fact is in `apply_policy(dry_run=True)` and was simply never shown.

    See tasks/ydm_menu/DESIGN-2026-08-24.md, "Решения 8.7 и 8.8".
    """

    #: The orphan inside the disabled tree — the shape of the incident.
    TARGET = "/Books/Math/АнГем"

    def setUp(self):
        super().setUp()
        self._drop_download_only()

    def _drop_download_only(self):
        """The bench policy is rclone-shaped; the daemon cannot express it.

        `download_only` has no blacklist equivalent, so `apply_policy` refuses
        the whole policy before any delta can be computed — the check would
        fail for a reason that has nothing to do with previews. A machine
        running the daemon has no such entries in the first place.
        """
        policy = self.bench.read_policy()
        policy["paths"] = {
            path: meta for path, meta in policy["paths"].items()
            if meta.get("mode") != "download_only"
        }
        with open(self.bench.policy_path, "w", encoding="utf-8") as handle:
            json.dump(policy, handle, ensure_ascii=False, indent=2)

    def cfg(self, backend="rclone"):
        from tools.ydm_menu_config import MenuConfig

        return MenuConfig.from_env_and_args(
            db_path=self.bench.db_path,
            local_root=self.bench.local_root,
            policy_path=self.bench.policy_path,
            exclude_config=self.bench.exclude_config,
            backend=backend,
            plain=True,
        )

    def preview(self, backend="daemon", path=None, mode="bidirectional"):
        from tools.ydm_menu_actions import preview_add

        return preview_add(self.cfg(backend), path or self.TARGET, mode)

    def test_the_preview_names_what_stops_being_excluded(self):
        """The line that would have stopped 14.08.

        Not "are you sure" — the specific fact that `Books`, excluded until
        now, is about to leave the exclude list and start syncing.
        """
        result = self.preview()
        self.assertTrue(result.ok, result.message)
        self.assertIn("Books", result.details["delta"]["removed"])
        self.assertIn("no longer excluded", result.message)
        self.assertRegex(result.message, r"Books(?!/)")

    def test_the_preview_counts_both_sides(self):
        """The count is shown, and on its own it is not enough.

        On this bench it goes 1 -> 1: `Books` leaves the exclude list and
        `Books/Keep` enters it, so the number is unchanged while the meaning
        is inverted. That is precisely why the named lines carry the warning
        and the count only frames it — a preview that showed the count alone
        would have been silent here too.
        """
        result = self.preview()
        delta = result.details["delta"]
        self.assertNotEqual(set(delta["before"]), set(delta["after"]))
        self.assertIn(f"{len(delta['before'])} -> {len(delta['after'])}",
                      result.message)

    def test_the_preview_touches_nothing(self):
        """It runs the production path, so it has to run it on a copy.

        The policy is copied to a temp file and `add_policy_path(apply=True)`
        is applied there — that is the only way to see the daemon coercion
        without reimplementing it. Both real files must come out byte-identical.
        """
        with open(self.bench.exclude_config, "rb") as handle:
            config_before = handle.read()
        policy_before = self.bench.read_policy()

        self.preview()

        with open(self.bench.exclude_config, "rb") as handle:
            self.assertEqual(config_before, handle.read(), "daemon config changed")
        self.assertEqual(policy_before, self.bench.read_policy(), "policy changed")

    def test_the_preview_shows_the_filter_delta_on_rclone(self):
        """The other backend has no exclude list; it has two filter files."""
        result = self.preview(backend="rclone", path="/orphans")
        self.assertTrue(result.ok, result.message)
        self.assertIn("orphans", result.details["delta"]["added"])
        self.assertIn("Download filter", result.message)
        self.assertIn("Bisync filter", result.message)

    def test_the_preview_reports_a_refusal_before_it_happens(self):
        """`deletion_risk_paths` is filled in on a dry run and raised on a real one.

        `/agents` is bidirectional in the bench policy and absent on disk, so
        restarting the daemon would read that absence as a deletion. The
        preview must say so rather than let the person find out by being
        refused after they confirm.
        """
        result = self.preview()
        self.assertIn("agents", result.details["delta"]["deletion_risk_paths"])
        self.assertIn("would be refused", result.message)

    def test_a_preview_that_cannot_be_built_says_why(self):
        """A path with no snapshot beneath it cannot have its siblings resolved."""
        result = self.preview(path="/nowhere/at/all")
        self.assertFalse(result.ok)
        self.assertTrue(result.message.strip())


class TestAddScreensConfirm(BenchTestCase):
    """The screens put the preview in front of the person, then ask once."""

    def cfg(self, backend="rclone"):
        from tools.ydm_menu_config import MenuConfig

        return MenuConfig.from_env_and_args(
            db_path=self.bench.db_path,
            local_root=self.bench.local_root,
            policy_path=self.bench.policy_path,
            exclude_config=self.bench.exclude_config,
            backend=backend,
            plain=True,
        )

    def run_screen(self, screen, answers, backend="rclone"):
        import contextlib
        import io

        from tools.ydm_menu_prompts import scripted_reader

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            screen(self.cfg(backend), scripted_reader(answers))
        return buffer.getvalue()

    def _orphan_index(self, path):
        from tools.ydm_menu_orphans import list_orphan_paths

        orphans = list_orphan_paths(
            self.bench.db_path, self.bench.local_root, self.bench.policy_path,
            root="/", max_depth=5,
        )
        for index, entry in enumerate(orphans, start=1):
            if entry.cloud_path == path:
                return str(index)
        self.fail(f"{path} is not in the orphan list: "
                  f"{[e.cloud_path for e in orphans]}")

    def test_declining_the_preview_leaves_the_policy_alone(self):
        """The barrier only counts if answering no actually stops it."""
        before = self.bench.read_policy()
        pick = self._orphan_index("/orphans")
        text = self.run_screen(
            __import__("tools.ydm_menu", fromlist=["x"]).screen_add_orphans,
            [pick, "2", "n"],
        )
        self.assertIn("Download filter", text)
        self.assertEqual(before, self.bench.read_policy())

    def test_confirming_it_applies(self):
        """And the same screen, answered yes, still does the work."""
        pick = self._orphan_index("/orphans")
        self.run_screen(
            __import__("tools.ydm_menu", fromlist=["x"]).screen_add_orphans,
            [pick, "2", "y"],
        )
        policy = self.bench.read_policy()
        self.assertIn("orphans", policy["paths"])
        self.assertEqual("download_only", policy["paths"]["orphans"]["mode"])


# --- Phase 8.8 -------------------------------------------------------------

class TestTrashAndDatabaseSurfaces(BenchTestCase):
    """`trash_scan` gets a screen; `prune` gets a line.

    Both are needed rarely and at a bad moment. The difference is who needs
    them: the trash is a person's emergency, prune is housekeeping. So the
    trash gets the menu entry and prune gets a fact in the status screen.
    """

    def cfg(self, backend="rclone"):
        from tools.ydm_menu_config import MenuConfig

        return MenuConfig.from_env_and_args(
            db_path=self.bench.db_path,
            local_root=self.bench.local_root,
            policy_path=self.bench.policy_path,
            exclude_config=self.bench.exclude_config,
            backend=backend,
            plain=True,
        )

    def capture(self, fn, *args, **kwargs):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            fn(*args, **kwargs)
        return buffer.getvalue()

    def test_the_menu_offers_the_trash(self):
        from tools.ydm_menu_screens import render_main_menu
        from tools.ydm_menu_status import load_status

        cfg = self.cfg()
        text = "\n".join(render_main_menu(cfg, load_status(cfg)))
        self.assertIn(" t  ", text)
        # Still on letters: the digits keep meaning what they meant.
        self.assertIn(" 8  Detailed status", text)

    def test_the_trash_screen_lists_what_is_there_and_how_to_restore_it(self):
        """The hashed name is the thing nobody can guess, so it is the payload."""
        import unittest.mock as mock

        from tools import ydm_menu_actions

        items = [{
            "name": "Books_20260814T093000",
            "path": "trash:/Books_20260814T093000",
            "type": "dir",
            "deleted": "2026-08-14T09:30:00+00:00",
            "origin_path": "disk:/Books",
        }]
        with mock.patch.object(ydm_menu_actions, "_trash_top_level",
                               return_value=items):
            text = self.capture(ydm_menu_actions.print_trash_overview, self.cfg())

        self.assertIn("Books_20260814T093000", text)
        self.assertIn("/Books", text)
        self.assertIn("trash_scan.py scan", text)
        self.assertIn("--trash-root trash:/Books_20260814T093000", text)
        self.assertIn("--restore-root /Books", text)

    def test_the_printed_commands_survive_a_hostile_name(self):
        """The commands are meant to be pasted, so they have to be pasteable.

        Found on the live disk the day this shipped: the recovery left
        `Books (1)` in the trash — the name Yandex gives a restore that
        collides with an existing folder. Unquoted, the space splits the
        argument and `(1)` is a shell metacharacter, so the line that was
        supposed to save someone in a hurry fails to parse.
        """
        import shlex
        import unittest.mock as mock

        from tools import ydm_menu_actions

        items = [{
            "name": "Books (1)",
            "path": "trash:/Books (1)_835c316a",
            "type": "dir",
            "deleted": "2026-08-16T10:41:00+00:00",
            "origin_path": "disk:/Books (1)",
        }]
        with mock.patch.object(ydm_menu_actions, "_trash_top_level",
                               return_value=items):
            text = self.capture(ydm_menu_actions.print_trash_overview, self.cfg())

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("python3 tools/trash_scan.py"):
                continue
            # It must parse as one command, and the two paths must arrive
            # whole rather than split on the space.
            parts = shlex.split(stripped)
            self.assertIn("trash:/Books (1)_835c316a", parts, stripped)
            self.assertIn("/Books (1)", parts, stripped)

    def test_the_trash_screen_survives_a_missing_token(self):
        """`resolve_token()` raises SystemExit, which is not an Exception.

        The same trap as the smart cloud scan: `except Exception` does not
        catch it, and the menu would exit out from under the person.
        """
        import unittest.mock as mock

        from tools import ydm_menu_actions

        with mock.patch.object(ydm_menu_actions, "_trash_top_level",
                               side_effect=SystemExit(2)):
            text = self.capture(ydm_menu_actions.print_trash_overview, self.cfg())
        self.assertTrue(text.strip())
        self.assertIn("trash", text.lower())

    def test_an_empty_trash_is_an_answer(self):
        import unittest.mock as mock

        from tools import ydm_menu_actions

        with mock.patch.object(ydm_menu_actions, "_trash_top_level",
                               return_value=[]):
            text = self.capture(ydm_menu_actions.print_trash_overview, self.cfg())
        self.assertIn("empty", text.lower())

    def test_the_status_says_how_big_the_database_is_and_what_is_droppable(self):
        """Prune stays out of the menu; the fact that invites it does not."""
        from tools.ydm_menu_actions import print_detailed_status

        text = self.capture(print_detailed_status, self.cfg())
        self.assertIn("Database:", text)
        self.assertRegex(text, r"Database:.*\d+ scan")
        self.assertIn("prunable", text)
        self.assertIn("report prune", text)

    def test_the_database_line_does_not_take_the_screen_down(self):
        """An unreadable database is a missing line, not a traceback."""
        import unittest.mock as mock

        from tools import ydm_menu_actions

        with mock.patch.object(ydm_menu_actions, "_database_facts",
                               side_effect=RuntimeError("boom")):
            text = self.capture(ydm_menu_actions.print_detailed_status, self.cfg())
        self.assertTrue(text.strip())
        self.assertNotIn("Traceback", text)


# --- Phase 10 --------------------------------------------------------------

class TestStopSyncingOnTheDaemon(BenchTestCase):
    """Menu 4 under a blacklist, where until 2026-08-27 it did the opposite.

    The daemon has no whitelist: `exclude-dirs` is all it reads, and everything
    absent from it syncs. A policy for it therefore holds exclusions and
    nothing else — `_policy_to_exclude_dirs()` drops `bidirectional` and raises
    on `download_only`. Menu 4 listed the policy under "Remove folder from
    sync", so on a real machine it offered 52 excluded folders and removing one
    *started* syncing it. `Books` sat second in that list.

    The bench never caught it because its policy is rclone-shaped and holds all
    three modes at once. `make_daemon_policy()` is the missing degenerate case.

    See tasks/ydm_menu/AUDIT-2026-08-27.md.
    """

    def setUp(self):
        super().setUp()
        self.excluded = make_daemon_policy(self.bench)

    def cfg(self, backend="daemon"):
        from tools.ydm_menu_config import MenuConfig

        return MenuConfig.from_env_and_args(
            db_path=self.bench.db_path,
            local_root=self.bench.local_root,
            policy_path=self.bench.policy_path,
            exclude_config=self.bench.exclude_config,
            backend=backend,
            plain=True,
        )

    def run_screen(self, answers, backend="daemon", screen=None):
        """Drive menu 4, with the daemon restart stubbed out.

        `apply_policy(dry_run=False)` ends in `yandex-disk stop && start`. Left
        alone that would restart the operator's live daemon from inside a unit
        test — the bench isolates files, not the machine. The stub also records
        that the restart was reached, which is the difference between "wrote
        the config" and "the daemon read it".
        """
        import contextlib
        import io
        import unittest.mock as mock

        from tools import sync_backends, ydm_menu
        from tools.ydm_menu_prompts import scripted_reader

        screen = screen or ydm_menu.screen_remove
        buffer = io.StringIO()
        with mock.patch.object(sync_backends, "stop_start_daemon",
                               return_value={}) as restart:
            with contextlib.redirect_stdout(buffer):
                screen(self.cfg(backend), scripted_reader(answers))
        self.restarts = restart.call_count
        return buffer.getvalue()

    def offered(self, text):
        """The paths the screen put in front of the person, in its order.

        Numbered lines only, whole line taken: a folder name may hold spaces,
        and the prose around the list may hold a path as an example.
        """
        paths = []
        for line in text.splitlines():
            number, _, rest = line.strip().partition("  ")
            rest = rest.strip()
            if number.isdigit() and rest.startswith("/"):
                paths.append(rest)
        return paths

    def index_of(self, text, path):
        """Match the whole rest of the line: folder names contain spaces."""
        for line in text.splitlines():
            number, _, rest = line.strip().partition("  ")
            if number.isdigit() and rest.strip() == path:
                return number
        self.fail(f"{path} is not on the screen:\n{text}")

    # --- 10.1 the degenerate policy itself ---------------------------------

    def test_the_bench_can_hold_a_policy_of_pure_exclusions(self):
        """The shape a daemon machine actually has, and the bench lacked."""
        policy = self.bench.read_policy()
        self.assertTrue(policy["paths"])
        self.assertEqual({"disabled"},
                         {meta["mode"] for meta in policy["paths"].values()})
        # Both halves have to agree, or the test would be checking a fiction.
        self.assertEqual(sorted(policy["paths"]), read_exclude_dirs(self.bench))

    # --- 10.2 the screen excludes instead of including ---------------------

    def test_the_screen_never_offers_an_excluded_folder(self):
        """The defect, stated directly.

        Every path on this screen used to be excluded; now none may be.
        `/Books` is the one that matters — second in the live list, 113 GB,
        and the folder the 2026-08-14 incident emptied.
        """
        text = self.run_screen(["0"])
        offered = self.offered(text)
        self.assertTrue(offered, f"nothing offered at all:\n{text}")
        for path in offered:
            self.assertNotIn(path.lstrip("/"), self.excluded,
                             f"{path} is excluded and was offered:\n{text}")

    def test_the_screen_offers_what_is_actually_syncing(self):
        """Under a blacklist that is everything the exclude list misses."""
        text = self.run_screen(["0"])
        offered = set(self.offered(text))
        for path in ("/pro", "/video", "/Docs", "/shared", "/mix", "/orphans"):
            self.assertIn(path, offered, text)

    def test_choosing_a_folder_excludes_it(self):
        """The action, in the direction the label promises."""
        listing = self.run_screen(["0"])
        pick = self.index_of(listing, "/video")

        self.run_screen([pick, "y"])

        self.assertIn("video", read_exclude_dirs(self.bench))
        self.assertEqual("disabled",
                         self.bench.read_policy()["paths"]["video"]["mode"])
        self.assertEqual(1, self.restarts, "the daemon never read the change")

    def test_it_can_only_ever_add_exclusions(self):
        """The invariant that makes the old behaviour unreachable.

        Not "this particular answer is safe" — no path through this screen may
        shrink the exclude list, whatever is typed into it. The old screen
        shrank it on every successful run.
        """
        before = set(read_exclude_dirs(self.bench))
        previous = None
        while True:
            listing = self.run_screen(["0"])
            paths = self.offered(listing)
            if not paths:
                break
            if previous is not None:
                # Bounds the loop, and says the other half of the invariant:
                # each exclusion takes one folder out of scope for good. A
                # screen that offered the excluded back would loop forever,
                # and a hang is not a failure anyone can read.
                self.assertLess(len(paths), previous, listing)
            previous = len(paths)
            with self.subTest(path=paths[0]):
                self.run_screen([self.index_of(listing, paths[0]), "y"])
                after = set(read_exclude_dirs(self.bench))
                self.assertTrue(before <= after,
                                f"{paths[0]} un-excluded {before - after}")
                before = after

    def test_declining_leaves_both_halves_alone(self):
        policy_before = self.bench.read_policy()
        config_before = read_exclude_dirs(self.bench)
        listing = self.run_screen(["0"])

        text = self.run_screen([self.index_of(listing, "/video"), "n"])

        self.assertEqual(policy_before, self.bench.read_policy())
        self.assertEqual(config_before, read_exclude_dirs(self.bench))
        self.assertEqual(0, self.restarts, "the daemon was restarted anyway")
        self.assertIn("Cancelled", text)

    def test_it_shows_the_delta_before_applying_it(self):
        """The 8.7 preview, on the operation that now runs here."""
        listing = self.run_screen(["0"])
        text = self.run_screen([self.index_of(listing, "/video"), "n"])
        self.assertIn("newly excluded", text)
        self.assertIn("video", text)

    def test_back_changes_nothing(self):
        before = self.bench.read_policy()
        self.run_screen(["0"])
        self.assertEqual(before, self.bench.read_policy())

    def test_a_custom_path_reaches_below_the_top_level(self):
        """The live exclude list is mostly depth 2 — `video/Математика` and kin.

        A screen that could only exclude top-level folders would not be able to
        express the configuration this machine already runs.
        """
        listing = self.run_screen(["0"])
        self.assertNotIn("/shared/live", self.offered(listing),
                         "sub-paths belong behind Custom, not in the list")

        text = self.run_screen([self._custom_index(listing), "/shared/live", "y"])
        self.assertIn("shared/live", read_exclude_dirs(self.bench), text)

    def test_the_sync_root_cannot_be_typed_in(self):
        """`normalize_entry("/")` is the empty string.

        Written into `exclude-dirs` that is not "exclude everything" — it is a
        stray comma the daemon reads as nothing at all.
        """
        listing = self.run_screen(["0"])
        before = read_exclude_dirs(self.bench)

        text = self.run_screen([self._custom_index(listing), "/"])

        self.assertEqual(before, read_exclude_dirs(self.bench))
        self.assertIn("root", text.lower())
        self.assertEqual(0, self.restarts)

    def test_a_custom_path_already_covered_says_so_instead_of_acting(self):
        """`Books` is excluded, so excluding `Books/Math` changes nothing."""
        listing = self.run_screen(["0"])
        before = read_exclude_dirs(self.bench)

        text = self.run_screen([self._custom_index(listing), "/Books/Math"])

        self.assertEqual(before, read_exclude_dirs(self.bench))
        self.assertIn("Books", text)
        self.assertEqual(0, self.restarts)

    def _custom_index(self, text):
        for line in text.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].isdigit() and "Custom" in parts[1]:
                return parts[0]
        self.fail(f"no Custom option on the screen:\n{text}")

    def test_excluding_a_parent_names_the_children_it_would_shadow(self):
        """A blacklist has no way to keep a child of an excluded folder.

        `_policy_to_exclude_dirs()` simply drops `bidirectional` entries, so a
        child left in the policy under a newly excluded parent stops syncing
        without a word. That is the same silence this whole phase is about.
        """
        policy = self.bench.read_policy()
        policy["paths"]["shared/live"] = {"mode": "bidirectional"}
        with open(self.bench.policy_path, "w", encoding="utf-8") as handle:
            json.dump(policy, handle, ensure_ascii=False, indent=2)

        listing = self.run_screen(["0"])
        text = self.run_screen([self.index_of(listing, "/shared"), "n"])
        self.assertIn("shared/live", text)

    # --- 10.3 / 10.4 what the person is told -------------------------------

    def test_the_menu_calls_it_stopping_on_the_daemon(self):
        from tools.ydm_menu_screens import render_main_menu
        from tools.ydm_menu_status import load_status

        cfg = self.cfg("daemon")
        text = "\n".join(render_main_menu(cfg, load_status(cfg)))
        self.assertIn(" 4  Stop syncing", text)
        self.assertNotIn("Remove folder from sync", text)
        # The digits keep meaning what they meant; only the wording moves.
        self.assertIn(" 2  Add folder from cloud", text)

    def test_the_menu_still_calls_it_removing_on_rclone(self):
        from tools.ydm_menu_screens import render_main_menu
        from tools.ydm_menu_status import load_status

        cfg = self.cfg("rclone")
        text = "\n".join(render_main_menu(cfg, load_status(cfg)))
        self.assertIn(" 4  Remove folder from sync", text)

    def test_the_excluded_are_counted_and_the_way_back_is_named(self):
        """They leave this screen, so the screen has to say where they went."""
        text = self.run_screen(["0"])
        self.assertIn(str(len(self.excluded)), text)
        self.assertIn("excluded", text.lower())
        self.assertRegex(text, r"menu 2|item 2|\b2\b")

    def test_it_prints_the_command_rather_than_deleting_anything(self):
        """Reclaiming the space is a separate, human-pressed step.

        The order — exclude, let the daemon restart, then delete — is the only
        safe one, and putting an irreversible step immediately after a daemon
        restart is the shape of the 2026-08-14 incident. So the screen hands
        over a pasteable command and stops.
        """
        import shlex

        listing = self.run_screen(["0"])
        text = self.run_screen([self.index_of(listing, "/video"), "y"])

        line = next((l.strip() for l in text.splitlines()
                     if l.strip().startswith("rm -rf")), None)
        self.assertIsNotNone(line, f"no reclaim command printed:\n{text}")
        self.assertIn(os.path.join(self.bench.local_root, "video"),
                      shlex.split(line))
        # And it only printed it: the folder is still there.
        self.assertTrue(os.path.isdir(os.path.join(self.bench.local_root, "video")))

    def test_the_printed_command_survives_a_hostile_name(self):
        """Same lesson as the trash screen, learned on `Books (1)` on 16.08."""
        import shlex

        os.makedirs(os.path.join(self.bench.local_root, "odd name (1)"),
                    exist_ok=True)
        listing = self.run_screen(["0"])
        text = self.run_screen([self.index_of(listing, "/odd name (1)"), "y"])

        line = next((l.strip() for l in text.splitlines()
                     if l.strip().startswith("rm -rf")), None)
        self.assertIsNotNone(line, f"no reclaim command printed:\n{text}")
        self.assertIn(os.path.join(self.bench.local_root, "odd name (1)"),
                      shlex.split(line))

    def test_a_locally_created_folder_is_offered_too(self):
        """A blacklist syncs what the snapshot has never seen.

        Made locally and not yet scanned, it is still inside the daemon's
        scope, so a screen built from the snapshot alone would fail to offer
        the one folder most likely to be a mistake.
        """
        os.makedirs(os.path.join(self.bench.local_root, "brand-new"),
                    exist_ok=True)
        self.assertIn("/brand-new", self.offered(self.run_screen(["0"])))

    def test_nothing_left_to_exclude_is_an_answer(self):
        """Every path excluded is a legitimate state, not an empty screen."""
        make_daemon_policy(self.bench, [
            entry.path for entry in SAMPLE_TREE
            if entry.path != "/" and "/" not in entry.path.strip("/")
        ] + ["brand-new"])
        text = self.run_screen(["0"])
        self.assertTrue(text.strip())
        self.assertIn("everything", text.lower())


class TestRemoveScreenOnRclone(BenchTestCase):
    """The whitelist side, where "remove" has always meant what it says.

    Kept as its own class so a change made for the daemon has to prove it left
    this one alone: under rclone a policy entry means "synced", and deleting it
    is exactly how a folder stops syncing.
    """

    def cfg(self, backend="rclone"):
        from tools.ydm_menu_config import MenuConfig

        return MenuConfig.from_env_and_args(
            db_path=self.bench.db_path,
            local_root=self.bench.local_root,
            policy_path=self.bench.policy_path,
            exclude_config=self.bench.exclude_config,
            backend=backend,
            plain=True,
        )

    def run_screen(self, answers):
        import contextlib
        import io

        from tools.ydm_menu import screen_remove
        from tools.ydm_menu_prompts import scripted_reader

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            screen_remove(self.cfg(), scripted_reader(answers))
        return buffer.getvalue()

    def _index_of(self, text, entry):
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == entry:
                return parts[0]
        self.fail(f"{entry} is not on the screen:\n{text}")

    def test_it_still_lists_the_policy_and_still_removes(self):
        listing = self.run_screen(["0"])
        self.run_screen([self._index_of(listing, "video"), "y", "video", "n"])
        self.assertNotIn("video", self.bench.read_policy()["paths"])

    def test_an_excluded_entry_is_not_called_removing_from_sync(self):
        """Under a whitelist, dropping a `[X]` entry changes no syncing at all.

        `effective_download_paths()` never looked at it: the folder was out of
        the filters because it was absent from them, not because of the entry.
        Calling that "remove from sync" is the daemon's mistake in miniature.
        """
        listing = self.run_screen(["0"])
        text = self.run_screen([self._index_of(listing, "Books"), "n"])
        self.assertNotIn("Remove /Books from sync?", text)
        self.assertIn("not synced", text.lower())


if __name__ == "__main__":
    unittest.main()
