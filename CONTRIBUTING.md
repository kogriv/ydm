# Contributing to YDM

Thanks for considering a contribution. This is a small, stdlib-only Python
utility — the bar for contributing is intentionally low.

## Development setup

No build step, no virtualenv required (the project has zero external
dependencies by design):

```bash
git clone <repository-url>
cd ydm
python3 ydm.py --help
```

To exercise the cloud-facing commands you'll need either a Yandex Disk
OAuth token (`.env`, see `.env.example`) or an `rclone` remote configured
for `--backend rclone` (see [`tasks/rclone_backend/README.md`](tasks/rclone_backend/README.md)).
Purely local commands (`init`, `scan local`, `report *` against an
existing DB) need neither.

## Running the tests

There's no unit-test framework here — tests are bash scripts that drive
the real CLI:

- `bash tests/test_ydm_fixes.sh` — regression suite, pure local, no
  credentials or network needed. Run this before opening a PR.
- `bash tests/test_rclone_backend.sh` — smoke test for `--backend rclone`.
  Needs a real, authorized `yandex:` remote in `rclone.conf`; it
  self-skips cleanly (`SKIP: ...`) if `rclone` isn't installed or no
  `yandex:` remote is configured, rather than failing.

Both scripts create and clean up their own temporary database/files.

There's also [`tests/TEST_concurrent_protection.md`](tests/TEST_concurrent_protection.md)
— a manual QA runbook (not automated) for the lock-file/tmpfs concurrency
protection described in `docs/ARCHITECTURE.md` §12. Worth running by hand
if you're touching scan locking or checkpoint recovery.

## Code style

- Stdlib only — don't add a dependency without discussing it first in an
  issue.
- Match the existing conventions in the file you're editing: dry-run by
  default + explicit `--apply` for anything that mutates state, a JSON
  envelope (`{"success": bool, "data": {...}}`) for machine-readable
  output, never `sys.exit()` for expected error conditions (convey them
  via `success: false` instead).
- Keep commit messages descriptive and focused on the "why", not just the
  "what".

## Reporting bugs / proposing features

Open a GitHub issue. If it's a security issue, see
[SECURITY.md](SECURITY.md) instead of a public issue.
