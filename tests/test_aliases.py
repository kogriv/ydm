#!/usr/bin/env python3
"""What `tools/aliases.sh` defines, and what it refuses to guess.

The file exists because the README once documented commands that lived only
in one person's shell. It then shipped a subset — scanning and the tree — and
left out bisync, the rename guard and the policy layer, on the environment
those were written for. Nothing noticed, because nothing checked.

These are cheap checks against exactly that: the set of names, what `ydm`
means, and that a command needing the mirror stops instead of picking one.

Run directly: python tests/test_aliases.py -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ALIASES = ROOT_DIR / "tools" / "aliases.sh"

#: Every command the README's "What you get" section promises. A name dropped
#: from the file without being dropped from the docs fails here.
DOCUMENTED = [
    "ydm", "ydm-menu", "ydm-cli",
    "ydm-scan-cloud", "ydm-scan-cloud-path", "ydm-scan-local",
    "ydm-tree", "ydm-tree-path", "ydm-sync-state",
    "ydm-sync-add", "ydm-sync-pick", "ydm-sync-rm",
    "ydm-bisync-run", "ydm-bisync-resync", "ydm-bisync-status",
    "ydm-rename", "ydm-rename-detect", "ydm-rename-status",
    "ydm-rename-apply", "ydm-rename-policy", "ydm-rename-policy-set",
    "ydm-policy-status", "ydm-policy-inspect", "ydm-policy-render",
    "ydm-help",
]

#: Commands that must refuse to run rather than guess a mirror. `ydm-help` and
#: `ydm-scan-cloud*` are absent on purpose: they touch no local path.
NEED_THE_MIRROR = [
    "ydm-scan-local", "ydm-tree", "ydm-sync-state", "ydm-bisync-status",
    "ydm-policy-status", "ydm-rename-status",
]


@unittest.skipIf(shutil.which("bash") is None, "bash is required to source the aliases")
class AliasesTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ydm_aliases_")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def bash(self, snippet, local_root=None):
        """Run `snippet` with the aliases sourced, in a home of its own.

        YDM_LOCAL_ROOT is removed rather than overridden: on a developer
        machine the file itself exports it, and a check that inherited it
        would be testing the developer's setup instead of the default.
        """
        env = {k: v for k, v in os.environ.items() if k != "YDM_LOCAL_ROOT"}
        env["HOME"] = self.home
        if local_root is not None:
            env["YDM_LOCAL_ROOT"] = local_root
        return subprocess.run(
            ["bash", "-c", f"source {ALIASES}; {snippet}"],
            capture_output=True, text=True, env=env, check=False,
        )

    def test_the_file_is_sourceable(self):
        proc = subprocess.run(
            ["bash", "-n", str(ALIASES)], capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_every_documented_command_exists(self):
        missing = []
        for name in DOCUMENTED:
            proc = self.bash(f"type -t {name}")
            if proc.stdout.strip() not in ("function", "alias"):
                missing.append(name)
        self.assertEqual(missing, [], f"documented but not defined: {missing}")

    def test_ydm_is_the_menu(self):
        """`ydm` has meant the interactive menu since the menu existed.

        tasks/ydm_menu/HOW_TO_USE.md says so, and a shipped file that quietly
        rebinds it to the CLI takes the menu away from anyone who sources it.
        """
        proc = self.bash("type ydm")
        self.assertIn("ydm_menu.py", proc.stdout)

    def test_ydm_cli_is_the_command_line(self):
        proc = self.bash("type ydm-cli")
        self.assertIn("ydm.py", proc.stdout)
        self.assertNotIn("ydm_menu.py", proc.stdout)

    def test_commands_needing_the_mirror_refuse_to_guess(self):
        for name in NEED_THE_MIRROR:
            with self.subTest(command=name):
                proc = self.bash(name)
                self.assertNotEqual(proc.returncode, 0, f"{name} ran without a mirror")
                self.assertIn("YDM_LOCAL_ROOT", proc.stderr)

    def test_the_bisync_filter_follows_the_mirror(self):
        """Derived late, not baked in when the file is sourced.

        YDM_LOCAL_ROOT is commonly exported after the source line, and a value
        captured at source time would be wrong for the rest of the session.
        """
        proc = self.bash("_ydm_bisync_filter", local_root="/tmp/mirror")
        self.assertEqual(proc.stdout.strip(), "/tmp/mirror.bisync.filters")

    def test_help_lists_the_commands_and_the_paths_in_effect(self):
        proc = self.bash("ydm-help --plain", local_root="/tmp/mirror")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for name in ("ydm-bisync-resync", "ydm-rename-apply", "ydm-policy-inspect"):
            self.assertIn(name, proc.stdout)
        self.assertIn("/tmp/mirror", proc.stdout)

    def test_help_warns_about_the_commands_that_apply_immediately(self):
        proc = self.bash("ydm-help --plain", local_root="/tmp/mirror")
        self.assertIn("APPLIES IMMEDIATELY", proc.stdout)
        for name in ("ydm-sync-add", "ydm-sync-rm", "ydm-bisync-run", "ydm-rename-apply"):
            self.assertIn(name, proc.stdout)


if __name__ == "__main__":
    unittest.main()
