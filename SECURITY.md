# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Preferred: use GitHub's private vulnerability reporting for this
repository — [Security → Report a vulnerability](../../security/advisories/new)
(if that link 404s, the repository maintainer needs to enable "Private
vulnerability reporting" under Settings → Security first).

If that's not available, open a regular issue asking for a private
contact channel, without describing the vulnerability itself.

## What's in scope

This project talks to the Yandex Disk API (directly, or via `rclone`) and
handles an OAuth token (`.env` / `rclone.conf`) with read/write/delete
access to a user's cloud storage. Relevant classes of issues:

- Anything that could leak a token or credential.
- Anything that could cause unintended deletion of cloud or local files
  beyond what the user explicitly requested (see the safety mechanisms in
  `tools/sync_bisync.py` and `tasks/junk/run_cleanup.py` for the existing
  safeguards this kind of report would be evaluated against).
- Command injection or path traversal in any of the CLI tools.

## What's not in scope

- The `--yandex-hard-delete` / hard-delete flags behaving exactly as
  documented (permanent deletion, bypassing Trash) — that's intentional,
  opt-in behavior, not a vulnerability.

## A note on tokens

Never paste a real `YANDEX_DISK_TOKEN`, `rclone.conf` contents, or any
other credential into an issue, PR, or commit — even to illustrate a bug.
Redact it first. `.env` and `rclone.conf` are gitignored for this reason;
if you accidentally commit one, revoke/rotate the token immediately, don't
just remove it in a follow-up commit (it's still in git history).
