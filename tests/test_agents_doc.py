#!/usr/bin/env python3
"""The commands quoted in the agent docs have to exist.

docs/QUICKSTART_AI.md went stale for four months: it never mentioned rclone,
bisync or quarantine, and nothing noticed. An agent follows these files to the
letter, so a subcommand or flag that was renamed is a wrong instruction, not a
typo. This reads every `python3 tools/...` and `python3 ydm.py ...` line out of
the fenced blocks and checks it against the tool's own `--help`.

Nothing runs but `--help`. No network, no credentials.
"""
import os
import re
import shlex
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = ("AGENTS.md", "docs/QUICKSTART_AI.md")
COMMAND = re.compile(r"python3 ((?:tools/[\w.]+\.py|ydm\.py)(?:\s.*)?)$")


def fenced_commands(path):
    """Yield the argv of every tool invocation inside a fenced block."""
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        text = handle.read()
    for block in re.findall(r"^```bash\n(.*?)^```", text, re.M | re.S):
        for line in block.replace("\\\n", " ").splitlines():
            line = line.split(" | ")[0].split(" 2>&1")[0].split("  #")[0].strip()
            match = COMMAND.search(line)
            if match:
                yield shlex.split(match.group(1).replace("<", "").replace(">", ""))


def help_text(argv):
    proc = subprocess.run([sys.executable] + argv + ["--help"], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout


def subcommands(text):
    """The choices of the positional section, not those of `--format {json,text}`."""
    section = text.split("positional arguments:", 1)
    if len(section) < 2:
        return set()
    choices = re.search(r"\{([^}]+)\}", section[1].split("options:", 1)[0])
    return set(choices.group(1).split(",")) if choices else set()


class AgentDocCommands(unittest.TestCase):
    def check(self, argv):
        script, tokens = argv[0], argv[1:]
        code, text = help_text([script])
        self.assertEqual(code, 0, f"{script} --help failed")
        known = text
        chain = [script]
        choices = subcommands(text)
        if choices:
            for token in tokens:
                if token in choices:
                    chain.append(token)
                    code, text = help_text(chain)
                    self.assertEqual(code, 0, " ".join(chain))
                    known += text
                    choices = subcommands(text)
                    if not choices:
                        break
            self.assertGreater(len(chain), 1, f"no known subcommand in: {' '.join(argv)}")
        for token in tokens:
            flag = token.split("=")[0]
            if flag.startswith("--"):
                self.assertIn(flag, known, f"{flag} is not an option of {' '.join(chain)}")

    def test_the_documents_quote_commands_at_all(self):
        for path in DOCS:
            self.assertGreater(len(list(fenced_commands(path))), 5, path)

    def test_every_quoted_command_exists(self):
        seen = set()
        for path in DOCS:
            for argv in fenced_commands(path):
                key = tuple(argv)
                if key in seen:
                    continue
                seen.add(key)
                with self.subTest(doc=path, command=" ".join(argv)):
                    self.check(argv)

    def test_a_renamed_subcommand_would_be_caught(self):
        with self.assertRaises(AssertionError):
            self.check(["tools/sync_bisync.py", "rebaseline", "--apply"])

    def test_a_renamed_flag_would_be_caught(self):
        with self.assertRaises(AssertionError):
            self.check(["tools/sync_bisync.py", "resync", "--confirm"])

    def test_the_documents_state_the_exit_codes_the_tools_use(self):
        sys.path.insert(0, ROOT)
        from tools.sync_common import EXIT_BLOCKED, EXIT_ERROR, EXIT_OK
        with open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8") as handle:
            text = handle.read()
        for code, word in ((EXIT_OK, "Done"), (EXIT_ERROR, "Failed"), (EXIT_BLOCKED, "guard")):
            self.assertRegex(text, rf"\| `{code}` \| [^|]*{word}")


if __name__ == "__main__":
    unittest.main()
