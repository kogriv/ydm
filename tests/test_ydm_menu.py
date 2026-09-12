#!/usr/bin/env python3
"""Tests for YDM menu.

Nothing here may read the machine it runs on. Until 2026-08-27 four of these
checks named `ROOT/monitor.db` and `ROOT/var/sync_policy.json` outright and
let the backend auto-detect, so what they asserted depended on whether a
daemon happened to be installed and on what the operator's snapshot held.
Opening the live database was even visible in the working tree: a clean close
checkpoints `monitor.db-wal` away, and `git status` reported a deletion after
every run.

That is the failure mode `TestBenchIsolation` exists to prevent — this file
had simply never been brought over. See tasks/ydm_menu/BACKLOG.md.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.ydm_menu_config import MenuConfig  # noqa: E402
from tools.ydm_menu_prompts import (  # noqa: E402
    prompt_int,
    prompt_ints,
    prompt_yes_no,
)
from tools.ydm_menu_status import load_status  # noqa: E402


def _seed_cloud_db(db_path: str) -> None:
    """A minimal monitor.db with one successful cloud scan."""
    import sqlite3

    from tools.sync_common import create_storage

    create_storage(db_path).init_db()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO scans (id, scan_type, status, duration) "
            "VALUES (1,'cloud','success',1.0)"
        )
        conn.executemany(
            "INSERT INTO files (scan_id,parent_path,name,type,size,md5) VALUES (1,?,?,?,?,?)",
            [
                ("", "Books", "dir", 0, None),
                ("", "video", "dir", 0, None),
                ("/Books", "readme.txt", "file", 10, "0" * 32),
            ],
        )
        conn.commit()
    finally:
        conn.close()


class MenuTestCase(unittest.TestCase):
    """A whole setup of its own: database, policy, daemon config, local root.

    Small on purpose — this file is about config resolution, status and
    prompts, not about rendering trees, so it does not need `tests/bench.py`.
    What it does need is the same rule: every path a MenuConfig is given here
    was made by this test and is deleted with it.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ydm_menu_")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        self.db_path = os.path.join(self.tmpdir, "monitor.db")
        _seed_cloud_db(self.db_path)

        self.local_root = os.path.join(self.tmpdir, "ya_disk")
        os.makedirs(self.local_root, exist_ok=True)

        self.policy_path = os.path.join(self.tmpdir, "sync_policy.json")
        with open(self.policy_path, "w", encoding="utf-8") as handle:
            json.dump({
                "schema": "ydm_sync_policy:v1",
                "local_root": self.local_root,
                "remote": "bench",
                "paths": {"Books": {"mode": "disabled"}},
            }, handle, ensure_ascii=False)

        self.exclude_config = os.path.join(self.tmpdir, "config.cfg")
        with open(self.exclude_config, "w", encoding="utf-8") as handle:
            handle.write(f"dir=\"{self.local_root}\"\n")
            handle.write("exclude-dirs=Books\n")

        self.bisync_filter_path = os.path.join(self.tmpdir, "ya_disk.bisync.filters")

    def cfg(self, backend="rclone", **overrides):
        """A MenuConfig pinned to this fixture.

        `backend` is always passed explicitly: left to auto-detect,
        `detect_backend()` asks the host whether a daemon is configured, and
        the two backends report status in different terms — so the assertions
        would change meaning depending on where the suite ran.
        """
        params = {
            "db_path": self.db_path,
            "local_root": self.local_root,
            "policy_path": self.policy_path,
            "exclude_config": self.exclude_config,
            "bisync_filter_path": self.bisync_filter_path,
            "backend": backend,
        }
        params.update(overrides)
        return MenuConfig.from_env_and_args(**params)


class MenuPromptTests(unittest.TestCase):
    def test_prompt_int_default(self):
        self.assertEqual(prompt_int("x", default=4, reader=lambda _: ""), 4)

    def test_prompt_ints_all(self):
        self.assertEqual(prompt_ints("x", max_n=3, reader=lambda _: "all"), [1, 2, 3])

    def test_prompt_yes_no(self):
        self.assertTrue(prompt_yes_no("x", default=True, reader=lambda _: ""))


class MenuConfigTests(MenuTestCase):
    def test_every_path_the_fixture_hands_out_is_its_own(self):
        """The guard that keeps this file from drifting back.

        `MenuConfig` resolves five paths, and each has a fallback that lands
        on the live setup. Adding a sixth field, or dropping one argument from
        `cfg()`, would restore exactly the state this rework removed — and it
        would do so silently, because reading the operator's snapshot makes
        nothing fail.
        """
        cfg = self.cfg()
        for name in ("db_path", "local_root", "policy_path",
                     "bisync_filter_path", "exclude_config"):
            value = getattr(cfg, name)
            self.assertTrue(value.startswith(self.tmpdir),
                            f"{name} points outside the fixture: {value}")

    def test_the_menu_accepts_an_exclude_config_flag(self):
        """`MenuConfig` always had the field and `YDM_EXCLUDE_CONFIG` set it.

        Only the command-line flag was missing, so the path most worth
        redirecting was the one that could not be — and `run_sync_tree()` had
        nothing to hand its child process either.
        """
        from tools.ydm_menu import parse_args

        argv = ["ydm_menu.py", "--exclude-config", self.exclude_config]
        with patch.object(sys, "argv", argv):
            args = parse_args()
        self.assertEqual(self.exclude_config, args.exclude_config)

    def test_arguments_win_over_the_environment(self):
        """Three sources, in a fixed order, and only the first was checked.

        The old version passed explicit paths and then asserted that
        `db_path` ended in "monitor.db" — true of the default too, so the
        argument could have been ignored entirely and the check would still
        have passed. The env layer sits between argument and default and had
        no check at all.
        """
        env = {
            "YDM_DB": os.path.join(self.tmpdir, "from-env.db"),
            "YDM_POLICY": os.path.join(self.tmpdir, "from-env.json"),
            "YDM_REMOTE": "env-remote",
        }
        with patch.dict(os.environ, env, clear=False):
            from_env = self.cfg(db_path=None, policy_path=None, remote=None)
            from_args = self.cfg(remote="arg-remote")

        self.assertEqual(env["YDM_DB"], from_env.db_path)
        self.assertEqual(env["YDM_POLICY"], from_env.policy_path)
        self.assertEqual("env-remote", from_env.remote)

        self.assertEqual(self.db_path, from_args.db_path)
        self.assertEqual(self.policy_path, from_args.policy_path)
        self.assertEqual("arg-remote", from_args.remote)

    def test_a_missing_local_root_is_refused_rather_than_guessed(self):
        """`require_local_root` exists because a guess here writes elsewhere."""
        # Patched where it is *used*: ydm_menu_config imported the name at
        # module load, so patching `ydm.DEFAULT_CONFIG` would rebind a dict
        # nobody reads and the check would pass for the wrong reason.
        blank = {"local_root": "", "rclone_remote": "x",
                 "exclude_config": self.exclude_config}
        # The refusal writes its instructions to stderr; captured so a passing
        # run stays readable.
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            with patch.dict(os.environ, {}, clear=True):
                with patch("tools.ydm_menu_config.DEFAULT_CONFIG", blank):
                    with self.assertRaises(SystemExit):
                        MenuConfig.from_env_and_args(local_root=None)
        self.assertIn("--local-root", err.getvalue())

    def test_no_backend_available_is_reported_not_raised(self):
        """A host with neither the daemon nor an rclone remote (CI runners,
        a fresh checkout) must still get a menu that explains itself."""
        from tools.sync_backends import BackendError
        from tools.ydm_menu_screens import render_header

        with patch(
            "tools.ydm_menu_config.detect_backend",
            side_effect=BackendError("No sync backend available: ..."),
        ):
            cfg = self.cfg()
        self.assertEqual(cfg.backend_kind, "")
        self.assertEqual(cfg.backend_name, "none available")
        self.assertIn("No sync backend available", cfg.backend_error)
        header = "\n".join(render_header(cfg, load_status(cfg)))
        self.assertIn("Backend: none available", header)
        self.assertIn("No sync backend available", header)


class MenuScopeTests(MenuTestCase):
    def test_bisync_scope_lines(self):
        from tools.ydm_menu_actions import bisync_scope_lines

        with open(self.bisync_filter_path, "w", encoding="utf-8") as handle:
            handle.write("+ /Books/Math/**\n- **\n")
        lines = bisync_scope_lines(self.cfg())
        text = "\n".join(lines)
        self.assertIn("NOT the whole disk", text)
        self.assertIn("+ /Books/Math/", text)


class MenuStatusTests(MenuTestCase):
    """The backend must be pinned: which one auto-detection picks depends on
    what is installed, and the two report status in different terms."""

    def test_rclone_status_smoke(self):
        status = load_status(self.cfg("rclone"))
        self.assertTrue(status.bisync_fields_apply)
        self.assertTrue(
            status.overall.startswith(("OK", "NEEDS RESYNC", "CHECK", "BUSY"))
        )

    def test_daemon_status_uses_the_daemon_not_bisync_terms(self):
        from tools.sync_common import CommandResult

        fake = CommandResult(
            cmd=["yandex-disk", "status"],
            returncode=0,
            stdout="Sync core status: idle\nPath to Yandex.Disk directory: '/x'",
            stderr="",
        )
        with patch("tools.sync_backends.run_command", return_value=fake):
            status = load_status(self.cfg("daemon"))
        self.assertFalse(status.bisync_fields_apply)
        self.assertEqual(status.overall, "idle")
        self.assertFalse(status.resync_needed)

    def test_daemon_not_running_is_reported(self):
        from tools.sync_common import CommandResult

        fake = CommandResult(
            cmd=["yandex-disk", "status"], returncode=1, stdout="", stderr="not started"
        )
        with patch("tools.sync_backends.run_command", return_value=fake):
            status = load_status(self.cfg("daemon"))
        self.assertEqual(status.overall, "daemon not running")
        self.assertIn("not started", status.warnings)

    def test_rclone_does_not_consult_the_daemon_config(self):
        """`None` meant two different things in two places.

        The menu passes no exclusion list under rclone, because a whitelist
        backend is not governed by the daemon's blacklist. `snapshot_freshness()`
        read that None as "load the daemon's config from its default path" —
        the live one, on any machine that has both installed. Folders rclone
        does compare were then counted as never compared, which suppresses the
        staleness warning: the direction that hides a stale snapshot.

        Found by a probe written for the isolation rework — the live config was
        the one live path still being opened after the rest were fixed.
        """
        import ydm

        with patch.object(ydm, "load_exclude_dirs") as loader:
            load_status(self.cfg("rclone"))
        loader.assert_not_called()

    def test_the_daemon_reads_the_config_it_was_given(self):
        """And the backend that *is* governed by that list still gets it —
        from the configured path, never from the default."""
        import ydm

        with patch.object(ydm, "load_exclude_dirs", return_value=set()) as loader:
            from tools.sync_common import CommandResult

            fake = CommandResult(
                cmd=["yandex-disk", "status"], returncode=0,
                stdout="Sync core status: idle\n", stderr="",
            )
            with patch("tools.sync_backends.run_command", return_value=fake):
                load_status(self.cfg("daemon"))
        loader.assert_called_once_with(self.exclude_config)

    def test_the_diff_screen_uses_this_backends_exclusions(self):
        """The same sentinel, second victim.

        `get_diff()` reads the daemon's config when told nothing, and the `d`
        screen told it nothing — so on a host with both backends installed the
        menu's comparison silently dropped whatever the daemon excludes, even
        when the menu was running on rclone.
        """
        import ydm
        from tools.ydm_menu_actions import print_cloud_local_diff

        with patch.object(ydm, "load_exclude_dirs") as loader:
            with patch("sys.stdout", new_callable=io.StringIO):
                print_cloud_local_diff(self.cfg("rclone"))
        loader.assert_not_called()

    def test_the_status_reads_this_snapshot_and_not_the_machine_s(self):
        """The finding this file was reworked for, stated as a check.

        The fixture's database holds exactly one scan, seeded here. If a
        MenuConfig in this file ever falls back to the repository's own
        `monitor.db` again, the reported base scan will not be #1.
        """
        status = load_status(self.cfg("rclone"))
        self.assertIsNotNone(status.snapshot_line, "no snapshot line at all")
        self.assertIn("base #1", status.snapshot_line)


class MenuCliTests(unittest.TestCase):
    def test_help(self):
        import subprocess

        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools/ydm_menu.py"), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("orphans", proc.stdout)

    def _run_orphans(self, db_path, tmpdir):
        import subprocess

        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools/ydm_menu.py"),
                "--db-path",
                db_path,
                "--local-root",
                os.path.join(tmpdir, "ya_disk"),
                "--policy-path",
                os.path.join(tmpdir, "sync_policy.json"),
                "orphans",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_orphans_json(self):
        # Self-contained: the repo's own monitor.db is gitignored, so a test
        # that reads it passes only on the developer's machine.
        tmpdir = tempfile.mkdtemp()
        try:
            db_path = os.path.join(tmpdir, "monitor.db")
            _seed_cloud_db(db_path)
            proc = self._run_orphans(db_path, tmpdir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("ydm_menu_orphans:v1", proc.stdout)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_orphans_without_a_cloud_scan_explains_itself(self):
        tmpdir = tempfile.mkdtemp()
        try:
            proc = self._run_orphans(os.path.join(tmpdir, "missing.db"), tmpdir)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("No successful cloud scan", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class BisyncArgumentsTests(unittest.TestCase):
    """What the menu hands to sync_bisync must be what sync_bisync reads.

    The menu builds that Namespace itself instead of going through the CLI, so
    the two can drift, and they did: `resync` grew `--force-filter` and
    `_bisync_ns` did not. The first person to accept the resync the menu offers
    got `AttributeError: 'Namespace' object has no attribute 'force_filter'` —
    after the policy and the filters had already been written, so the add was
    half-applied and the scheduled job would refuse every run until someone
    resynced by hand.

    Asserting per command rather than for one union of options: each subcommand
    defines its own, and `run` having a flag proves nothing about `resync`.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def _cfg(self):
        return MenuConfig.from_env_and_args(
            db_path=os.path.join(self.tmpdir, "monitor.db"),
            local_root=os.path.join(self.tmpdir, "mirror"),
            policy_path=os.path.join(self.tmpdir, "sync_policy.json"),
            exclude_config=os.path.join(self.tmpdir, "config.cfg"),
            bisync_filter_path=os.path.join(self.tmpdir, "mirror.bisync.filters"),
            backend="rclone",
        )

    def _parser_options(self, command):
        from tools.sync_bisync import build_parser

        return set(vars(build_parser().parse_args([command])))

    def test_every_command_gets_every_option_it_defines(self):
        from tools.ydm_menu_actions import _bisync_ns

        cfg = self._cfg()
        for command in ("resync", "run", "status"):
            with self.subTest(command=command):
                produced = set(vars(_bisync_ns(cfg, command)))
                missing = self._parser_options(command) - produced
                self.assertEqual(missing, set(), f"{command} would raise on these")

    def test_the_menu_does_not_quietly_force_a_download_filter(self):
        """`--force-filter` defaults to off, and that default has to survive.

        It is the one flag that lets a resync establish a baseline from the
        download-only filter — sending back paths chosen precisely because they
        are unsafe to send back. Inheriting the parser's default is right; making
        it true here would be a data-loss switch nobody asked for.
        """
        from tools.ydm_menu_actions import _bisync_ns

        self.assertFalse(_bisync_ns(self._cfg(), "resync").force_filter)

    def test_the_menu_still_overrides_what_it_means_to(self):
        """Taking defaults from the parser must not lose the menu's own values."""
        from tools.ydm_menu_actions import _bisync_ns

        cfg = self._cfg()
        ns = _bisync_ns(cfg, "resync", apply=True)
        self.assertEqual(ns.db_path, cfg.db_path)
        self.assertEqual(ns.filter_path, cfg.bisync_filter_path)
        self.assertEqual(ns.format, "json")
        self.assertTrue(ns.apply)
        self.assertFalse(ns.text_header)


class OrphanBrowsingTests(unittest.TestCase):
    """One level of the picker at a time — G10, and what G9 made necessary.

    The flat list ran to 32 lines on the device, 24 of them a single subtree,
    numbered across the whole thing. After G9 it got worse rather than better:
    a folder that exists only on disk brings every folder inside it along, since
    each of those is just as local and just as absent from the policy —
    `Books/Math/База2` alone added 40 rows to a list of 32.

    `browse_rows` is a pure function of the list, so none of this needs a
    terminal, a database or a filesystem.
    """

    def _entries(self, *specs):
        from tools.ydm_menu_orphans import OrphanEntry

        return [
            OrphanEntry(
                cloud_path=f"/{rel}", rel_path=rel, local_bytes=size,
                cloud_file_count=cloud, display_marker="[L]",
            )
            for rel, size, cloud in specs
        ]

    def _rows(self, entries, prefix=""):
        from tools.ydm_menu_orphans import browse_rows

        return browse_rows(entries, prefix)

    def test_the_root_shows_containers_not_every_path(self):
        entries = self._entries(
            ("pro/a", 10, 1), ("pro/b", 20, 1), ("pro/c/d", 30, 1), ("tst", 5, 2),
        )
        rows = self._rows(entries)
        self.assertEqual([row.name for row in rows], ["pro", "tst"])
        pro = rows[0]
        self.assertFalse(pro.addable, "a folder with no entry of its own is a way in")
        self.assertEqual(pro.inside, 3)
        self.assertTrue(rows[1].addable)

    def test_descending_narrows_to_that_folder(self):
        entries = self._entries(("pro/a", 10, 1), ("pro/c/d", 30, 1), ("tst", 5, 2))
        rows = self._rows(entries, "pro")
        self.assertEqual([row.name for row in rows], ["a", "c"])
        self.assertTrue(rows[0].addable)
        self.assertFalse(rows[1].addable)

    def test_a_folder_can_be_both_addable_and_a_way_in(self):
        """The case G9 produced, and the reason `inside` is not a verdict.

        Adding a folder covers everything under it, so the 39 folders inside
        `База2` are information — not a reason to make someone descend.
        """
        entries = self._entries(
            ("Books/Math/База2", 1_600_000_000, 0),
            ("Books/Math/База2/x", 900_000_000, 0),
            ("Books/Math/База2/y", 700_000_000, 0),
        )
        rows = self._rows(entries, "Books/Math")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].addable)
        self.assertEqual(rows[0].inside, 2)

    def test_a_container_does_not_count_the_same_bytes_twice(self):
        """An orphan's size already includes the orphans inside it.

        Summing the list would report a folder as several times its own size,
        which on the device meant `Books/` claiming far more than the disk holds.
        """
        entries = self._entries(
            ("Books/Math/База2", 1000, 0),
            ("Books/Math/База2/x", 600, 0),
            ("Books/Math/База2/y", 400, 0),
        )
        rows = self._rows(entries)
        self.assertEqual(rows[0].name, "Books")
        self.assertEqual(rows[0].total_bytes, 1000)

    def test_rows_are_ordered_by_name_not_by_path_length(self):
        entries = self._entries(("b", 1, 1), ("A/x", 1, 1), ("c", 1, 1))
        self.assertEqual([row.name for row in self._rows(entries)], ["A", "b", "c"])

    def test_the_current_folder_is_not_offered_inside_itself(self):
        """Otherwise every level would carry a row that goes nowhere."""
        entries = self._entries(("pro", 10, 1), ("pro/a", 5, 1))
        rows = self._rows(entries, "pro")
        self.assertEqual([row.name for row in rows], ["a"])

    def _parse(self, raw, entries, prefix=""):
        from tools.ydm_menu_orphans import parse_level_input

        return parse_level_input(raw, self._rows(entries, prefix))

    #: A level holding one of each: a folder that can only be opened, one that
    #: can only be added, and one that is both — which is the case that had no
    #: way in at all.
    MIXED = (("Books/x", 10, 1), ("tst", 5, 2), ("both", 7, 0), ("both/deep", 3, 0))

    def test_a_bare_number_adds(self):
        choice = self._parse("2", self._entries(*self.MIXED))
        self.assertEqual(choice.action, "add")
        self.assertEqual([row.name for row in choice.add], ["both"])

    def test_a_trailing_slash_opens_the_same_row(self):
        """The capability that was missing: a row that is both, opened.

        `Books/Math/База2` was addable and held 39 folders, and a bare number
        added it, so there was no way to look inside.
        """
        choice = self._parse("2/", self._entries(*self.MIXED))
        self.assertEqual(choice.action, "open")
        self.assertEqual(choice.open_row.name, "both")

    def test_a_leading_slash_works_too(self):
        self.assertEqual(self._parse("/2", self._entries(*self.MIXED)).action, "open")

    def test_a_bare_number_still_opens_what_cannot_be_added(self):
        """Plain navigation stays one keypress; there is nothing else it means."""
        choice = self._parse("1", self._entries(*self.MIXED))
        self.assertEqual(choice.action, "open")
        self.assertEqual(choice.open_row.name, "Books")

    def test_several_numbers_add_several(self):
        choice = self._parse("2,3", self._entries(*self.MIXED))
        self.assertEqual(choice.action, "add")
        self.assertEqual(sorted(row.name for row in choice.add), ["both", "tst"])

    def test_all_adds_what_can_be_added_and_names_the_rest(self):
        choice = self._parse("all", self._entries(*self.MIXED))
        self.assertEqual(choice.action, "add")
        self.assertEqual(sorted(row.name for row in choice.add), ["both", "tst"])
        self.assertEqual(choice.skipped, ["Books"])

    def test_opening_something_with_nothing_inside_says_so(self):
        choice = self._parse("2/", self._entries(("tst", 5, 2), ("x", 1, 1)))
        self.assertEqual(choice.action, "retry")
        self.assertIn("nothing inside", choice.message)

    def test_a_number_out_of_range_does_not_raise(self):
        for raw in ("9", "9/", "1,9", "x"):
            with self.subTest(raw=raw):
                self.assertEqual(self._parse(raw, self._entries(*self.MIXED)).action, "retry")

    def test_empty_and_zero_go_back(self):
        for raw in ("", "0", "q", "  "):
            with self.subTest(raw=raw):
                self.assertEqual(self._parse(raw, self._entries(*self.MIXED)).action, "up")

    def test_labels_say_which_rows_can_be_added(self):
        from tools.ydm_menu_orphans import format_browse_row

        entries = self._entries(("pro/a", 2048, 3), ("pro/c/d", 1024, 1))
        rows = self._rows(entries)
        label = format_browse_row(rows[0])
        self.assertIn("2 folders", label, label)
        self.assertTrue(label.startswith("pro/"), label)
        addable = format_browse_row(self._rows(entries, "pro")[0])
        self.assertIn("cloud files: 3", addable, addable)


if __name__ == "__main__":
    unittest.main()
